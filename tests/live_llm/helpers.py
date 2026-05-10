from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from corpclaw_lite.llm.cache import SlotCacheActionResult

_MANAGED_PREFIX = "corpclaw_live_"


@dataclass(frozen=True)
class LiveLlmConfig:
    base_url: str
    model: str
    api_key: str
    slots: tuple[int, ...]
    prompt_tokens: int
    large_prompt_tokens: int
    max_tokens: int
    keep_cache: bool
    cache_root: Path | None
    report_dir: Path
    timeout_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> LiveLlmConfig:
        slots = tuple(
            int(part.strip())
            for part in os.environ.get("CORPCLAW_LIVE_LLM_SLOTS", "0,1,2,3").split(",")
            if part.strip()
        )
        cache_root_raw = os.environ.get("CORPCLAW_LIVE_LLM_CACHE_ROOT", "").strip()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_dir = Path("reports") / "live_llm" / timestamp
        return cls(
            base_url=os.environ.get("CORPCLAW_LIVE_LLM_BASE_URL", "http://192.168.193.178:8080"),
            model=os.environ.get("CORPCLAW_LIVE_LLM_MODEL", "gpt-oss-20b-UD-Q4_K_XL"),
            api_key=os.environ.get("CORPCLAW_LIVE_LLM_API_KEY", "dummy"),
            slots=slots,
            prompt_tokens=int(os.environ.get("CORPCLAW_LIVE_LLM_PROMPT_TOKENS", "1000")),
            large_prompt_tokens=int(
                os.environ.get("CORPCLAW_LIVE_LLM_LARGE_PROMPT_TOKENS", "5000")
            ),
            max_tokens=int(os.environ.get("CORPCLAW_LIVE_LLM_MAX_TOKENS", "24")),
            keep_cache=os.environ.get("CORPCLAW_LIVE_LLM_KEEP_CACHE", "0") == "1",
            cache_root=Path(cache_root_raw) if cache_root_raw else None,
            report_dir=report_dir,
        )


@dataclass(frozen=True)
class LiveChatMetrics:
    status: int
    slot_id: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int
    prompt_n: int
    prompt_ms: float
    predicted_n: int
    predicted_ms: float
    predicted_per_second: float
    ttft_any_s: float | None
    ttft_content_s: float | None
    total_s: float
    chunks: int
    content_chars: int
    reasoning_chars: int
    finish_reason: str | None
    error: str | None = None

    @property
    def cache_reuse_ratio(self) -> float:
        if self.prompt_tokens <= 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


@dataclass(frozen=True)
class SlotActionSummary:
    ok: bool
    status_code: int
    action: str
    slot_id: int
    filename: str | None
    n_tokens: int
    n_bytes: int
    elapsed_ms: float
    server_ms: float
    error: str | None


def managed_cache_filename(test_name: str, slot_id: int) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in test_name)
    return f"{_MANAGED_PREFIX}{timestamp}_{safe_name}_slot{slot_id}.bin"


def generate_prompt(target_tokens: int, *, label: str = "A") -> str:
    # Approximate local-model token count. The server-reported prompt_tokens is authoritative.
    block = (
        f"{label} CorpClaw Lite live benchmark context. "
        "This repeated text exists only to create prompt processing load for llama.cpp slot, "
        "persistent KV cache save, restore, validation, and concurrency measurements. "
        "The answer must stay short. "
    )
    repeats = max(4, target_tokens // 55)
    context = "\n".join(f"{i:04d}. {block}" for i in range(repeats))
    return (
        "Use this context only for live LLM infrastructure benchmarking.\n\n"
        f"{context}\n\n"
        "Final task: answer with exactly one short sentence: live cache benchmark acknowledged."
    )


class LiveLlamaClient:
    def __init__(self, config: LiveLlmConfig) -> None:
        self.config = config
        self._base_url = config.base_url.rstrip("/")
        self._timeout = httpx.Timeout(config.timeout_seconds)

    async def get_models(self) -> list[str]:
        payload = await self._get_json("/v1/models")
        data = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[str] = []
        if isinstance(data, list):
            models.extend(
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            )
        return models

    async def get_slots(self) -> list[int]:
        payload = await self._get_json(f"/slots?model={self.config.model}")
        slots_raw: Any = payload
        if isinstance(payload, dict):
            slots_raw = payload.get("slots", payload.get("data", []))
        slots: list[int] = []
        if isinstance(slots_raw, list):
            for item in slots_raw:
                if isinstance(item, dict):
                    raw_id = item.get("id_slot", item.get("slot_id", item.get("id")))
                    if isinstance(raw_id, int):
                        slots.append(raw_id)
        return slots

    async def slot_save(self, slot_id: int, filename: str) -> SlotActionSummary:
        return await self._slot_action("save", slot_id, filename=filename)

    async def slot_restore(self, slot_id: int, filename: str) -> SlotActionSummary:
        return await self._slot_action("restore", slot_id, filename=filename)

    async def slot_erase(self, slot_id: int) -> SlotActionSummary:
        return await self._slot_action("erase", slot_id)

    async def cache_save(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        summary = await self.slot_save(slot_id, filename)
        return _to_cache_action(summary)

    async def cache_restore(
        self, slot_id: int, *, model: str, filename: str
    ) -> SlotCacheActionResult:
        summary = await self.slot_restore(slot_id, filename)
        return _to_cache_action(summary)

    async def cache_erase(self, slot_id: int, *, model: str) -> SlotCacheActionResult:
        summary = await self.slot_erase(slot_id)
        return _to_cache_action(summary)

    async def save(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        return await self.cache_save(slot_id, model=model, filename=filename)

    async def restore(self, slot_id: int, *, model: str, filename: str) -> SlotCacheActionResult:
        return await self.cache_restore(slot_id, model=model, filename=filename)

    async def erase(self, slot_id: int, *, model: str) -> SlotCacheActionResult:
        return await self.cache_erase(slot_id, model=model)

    async def chat_streamed(
        self,
        *,
        slot_id: int,
        prompt: str,
        system: str = "You are a deterministic live benchmark responder.",
        cache_prompt: bool = True,
        max_tokens: int | None = None,
    ) -> LiveChatMetrics:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": max_tokens or self.config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "id_slot": slot_id,
            "cache_prompt": cache_prompt,
            "timings_per_token": True,
        }
        headers = self._headers()
        started = time.perf_counter()
        first_any: float | None = None
        first_content: float | None = None
        chunks = 0
        content_chars = 0
        reasoning_chars = 0
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}

        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream(
                "POST",
                f"{self._base_url}/v1/chat/completions",
                json=body,
                headers=headers,
            ) as response,
        ):
            if response.status_code >= 400:
                error_bytes = await response.aread()
                return LiveChatMetrics(
                    status=response.status_code,
                    slot_id=slot_id,
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                    cached_tokens=0,
                    prompt_n=0,
                    prompt_ms=0.0,
                    predicted_n=0,
                    predicted_ms=0.0,
                    predicted_per_second=0.0,
                    ttft_any_s=None,
                    ttft_content_s=None,
                    total_s=time.perf_counter() - started,
                    chunks=0,
                    content_chars=0,
                    reasoning_chars=0,
                    finish_reason=None,
                    error=error_bytes.decode(errors="replace")[:500],
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                if not raw:
                    continue
                event = json.loads(raw)
                chunks += 1
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if isinstance(event.get("timings"), dict):
                    timings = event["timings"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if isinstance(choice, dict):
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if content or reasoning:
                        now = time.perf_counter()
                        first_any = first_any or now
                    if content:
                        first_content = first_content or time.perf_counter()
                        content_chars += len(str(content))
                    if reasoning:
                        reasoning_chars += len(str(reasoning))

        ended = time.perf_counter()
        details = usage.get("prompt_tokens_details") or {}
        return LiveChatMetrics(
            status=200,
            slot_id=slot_id,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
            prompt_n=int(timings.get("prompt_n") or 0),
            prompt_ms=float(timings.get("prompt_ms") or 0.0),
            predicted_n=int(timings.get("predicted_n") or 0),
            predicted_ms=float(timings.get("predicted_ms") or 0.0),
            predicted_per_second=float(timings.get("predicted_per_second") or 0.0),
            ttft_any_s=(first_any - started) if first_any else None,
            ttft_content_s=(first_content - started) if first_content else None,
            total_s=ended - started,
            chunks=chunks,
            content_chars=content_chars,
            reasoning_chars=reasoning_chars,
            finish_reason=finish_reason,
        )

    async def delete_managed_cache_file(self, filename: str) -> bool:
        if self.config.keep_cache or self.config.cache_root is None:
            return False
        if not filename.startswith(_MANAGED_PREFIX):
            return False
        path = self.config.cache_root / filename
        if not path.exists():
            return False
        path.unlink()
        return True

    async def _slot_action(
        self,
        action: Literal["save", "restore", "erase"],
        slot_id: int,
        *,
        filename: str | None = None,
    ) -> SlotActionSummary:
        body: dict[str, Any] = {"model": self.config.model}
        if filename is not None:
            body["filename"] = filename
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/slots/{slot_id}?action={action}",
                json=body,
                headers=self._headers(),
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        timings = payload.get("timings") if isinstance(payload.get("timings"), dict) else {}
        n_tokens = int(
            payload.get("n_saved") or payload.get("n_restored") or payload.get("n_erased") or 0
        )
        n_bytes = int(payload.get("n_written") or payload.get("n_read") or 0)
        return SlotActionSummary(
            ok=response.status_code < 400,
            status_code=response.status_code,
            action=action,
            slot_id=slot_id,
            filename=filename,
            n_tokens=n_tokens,
            n_bytes=n_bytes,
            elapsed_ms=elapsed_ms,
            server_ms=float(timings.get(f"{action}_ms") or 0.0),
            error=None if response.status_code < 400 else response.text[:500],
        )

    async def _get_json(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._base_url}{path}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    def _headers(self) -> dict[str, str]:
        if self.config.api_key and self.config.api_key != "dummy":
            return {"Authorization": f"Bearer {self.config.api_key}"}
        return {}


def write_report(report_dir: Path, test_name: str, payload: dict[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{test_name}.json"
    normalized = _jsonable(payload)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def erase_slots(client: LiveLlamaClient, slots: tuple[int, ...]) -> list[SlotActionSummary]:
    return await asyncio.gather(*(client.slot_erase(slot_id) for slot_id in slots))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _to_cache_action(summary: SlotActionSummary) -> SlotCacheActionResult:
    return SlotCacheActionResult(
        ok=summary.ok,
        status_code=summary.status_code,
        action=summary.action,
        slot_id=summary.slot_id,
        filename=summary.filename,
        n_tokens=summary.n_tokens,
        n_bytes=summary.n_bytes,
        elapsed_ms=summary.elapsed_ms,
        server_ms=summary.server_ms,
        error=summary.error,
    )
