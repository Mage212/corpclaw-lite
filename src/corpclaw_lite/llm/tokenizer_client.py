"""Offline-safe token estimate client (B-092 / DC-013).

Tries llama.cpp native ``POST /tokenize`` when a base URL is available; on any
failure (or ``mode=heuristic``) falls back to a byte-length heuristic.

Does **not** use :class:`~corpclaw_lite.llm.queue.LLMRequestQueue` or GPU slots —
tokenize is CPU-only on the server side.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Literal, cast

import httpx

__all__ = [
    "TokenEstimate",
    "TokenizerClient",
    "estimate_tokens_heuristic",
    "normalize_native_base_url",
]

logger = logging.getLogger(__name__)

TokenSource = Literal["tokenize", "heuristic", "cache"]
TokenizerMode = Literal["auto", "tokenize", "heuristic"]


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Result of a token estimate for budget / attach previews."""

    n_tokens: int
    approximate: bool
    source: TokenSource


def estimate_tokens_heuristic(text: str) -> int:
    """Estimate token count from UTF-8 byte length.

    - Mostly ASCII (English): ``len_bytes / 4`` ≈ BPE tokens
    - Non-ASCII heavy (Cyrillic/CJK): ``len_bytes / 2`` (conservative)
    """
    if not text:
        return 0
    encoded = text.encode("utf-8")
    # ratio > 1.3 means significant non-ASCII content
    divisor = 2 if len(encoded) > len(text) * 1.3 else 4
    return len(encoded) // max(divisor, 1)


def normalize_native_base_url(base_url: str) -> str:
    """Strip trailing slashes and a trailing ``/v1`` OpenAI-compat suffix.

    llama.cpp native endpoints (``/tokenize``, ``/slots``) live on the server root,
    not under ``/v1``.
    """
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return normalized.rstrip("/")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _parse_tokenize_payload(payload: dict[str, Any]) -> int | None:
    """Extract token count from a flexible llama.cpp-style response body."""
    tokens = payload.get("tokens")
    if isinstance(tokens, list):
        token_list = cast(list[Any], tokens)
        return len(token_list)
    for key in ("n_tokens", "count", "tokens_count"):
        raw = payload.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return max(0, raw)
        if isinstance(raw, float):
            return max(0, int(raw))
        if isinstance(raw, str) and raw.strip().isdigit():
            return int(raw.strip())
    return None


class TokenizerClient:
    """Estimate tokens via llama.cpp ``/tokenize`` with heuristic fallback + cache."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        mode: TokenizerMode = "auto",
        timeout_seconds: float = 5.0,
        cache_max_entries: int = 256,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if mode not in ("auto", "tokenize", "heuristic"):
            msg = f"unsupported tokenizer mode: {mode!r}"
            raise ValueError(msg)
        if cache_max_entries < 1:
            msg = "cache_max_entries must be >= 1"
            raise ValueError(msg)
        self._base_url = base_url.strip() if base_url else None
        self._api_key = api_key
        self._model = model.strip() if model else None
        self._mode: TokenizerMode = mode
        self._timeout = httpx.Timeout(timeout_seconds)
        self._cache_max_entries = cache_max_entries
        self._transport = transport
        self._cache: OrderedDict[str, TokenEstimate] = OrderedDict()
        # S2-15: reuse a single AsyncClient for connection pooling instead of
        # creating one per _tokenize_http call.
        self._http_client: httpx.AsyncClient | None = None

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._http_client

    async def aclose(self) -> None:
        """Close the reusable HTTP client (S2-15)."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def clear_cache(self) -> None:
        """Drop all cached estimates."""
        self._cache.clear()

    def estimate_heuristic(self, content: str) -> TokenEstimate:
        """Pure heuristic path (sync); always ``approximate=True``."""
        return TokenEstimate(
            n_tokens=estimate_tokens_heuristic(content),
            approximate=True,
            source="heuristic",
        )

    async def estimate(self, content: str) -> TokenEstimate:
        """Return token estimate; never raises for network/server failures."""
        # Model-scoped cache: same text can tokenize differently per model.
        cache_key = _content_hash(f"{self._model or ''}\0{content}")
        cached = self._cache_get(cache_key)
        if cached is not None:
            return TokenEstimate(
                n_tokens=cached.n_tokens,
                approximate=cached.approximate,
                source="cache",
            )

        if self._mode == "heuristic" or (self._mode == "auto" and not self._base_url):
            result = self.estimate_heuristic(content)
            self._cache_put(cache_key, result)
            return result

        # mode=tokenize or auto with base_url
        exact = await self._tokenize_http(content)
        if exact is not None:
            self._cache_put(cache_key, exact)
            return exact

        result = self.estimate_heuristic(content)
        self._cache_put(cache_key, result)
        return result

    def _cache_get(self, key: str) -> TokenEstimate | None:
        value = self._cache.get(key)
        if value is None:
            return None
        self._cache.move_to_end(key)
        return value

    def _cache_put(self, key: str, value: TokenEstimate) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        while len(self._cache) > self._cache_max_entries:
            self._cache.popitem(last=False)

    async def _tokenize_http(self, content: str) -> TokenEstimate | None:
        if not self._base_url:
            logger.debug("TokenizerClient: no base_url, cannot call /tokenize")
            return None
        native = normalize_native_base_url(self._base_url)
        url = f"{native}/tokenize"
        headers: dict[str, str] = {}
        if self._api_key and self._api_key != "dummy":
            headers["Authorization"] = f"Bearer {self._api_key}"
        # Multi-model llama-server (and some proxies) require ``model`` in body.
        body: dict[str, Any] = {"content": content}
        if self._model:
            body["model"] = self._model
        try:
            client = await self._get_http_client()
            response = await client.post(
                url,
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "TokenizerClient /tokenize failed (%s): %s",
                type(exc).__name__,
                exc,
            )
            return None

        if response.status_code >= 400:
            logger.warning(
                "TokenizerClient /tokenize HTTP %s: %s",
                response.status_code,
                response.text[:200],
            )
            return None

        try:
            payload_raw: Any = response.json()
        except ValueError:
            logger.warning("TokenizerClient /tokenize returned non-JSON body")
            return None

        if not isinstance(payload_raw, dict):
            logger.warning("TokenizerClient /tokenize JSON root is not an object")
            return None

        payload = cast(dict[str, Any], payload_raw)
        n_tokens = _parse_tokenize_payload(payload)
        if n_tokens is None:
            logger.warning(
                "TokenizerClient /tokenize response missing token count keys: %s",
                sorted(payload.keys())[:20],
            )
            return None

        return TokenEstimate(
            n_tokens=n_tokens,
            approximate=False,
            source="tokenize",
        )
