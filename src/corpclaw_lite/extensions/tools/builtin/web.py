from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx
from ddgs import DDGS  # pyright: ignore[reportUnknownVariableType]
from ddgs.exceptions import DDGSException, RatelimitException, TimeoutException

from corpclaw_lite.config.settings import WebSettings
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.utils.async_helpers import run_in_thread

__all__ = [
    "BLOCKED_HOSTS",
    "DEFAULT_TIMEOUT",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_SIZE",
    "MAX_TEXT_CHARS",
    "WebFetchTool",
    "WebSearchTool",
]

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_RESPONSE_SIZE = 1_048_576  # 1 MB
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 50_000
MAX_QUERY_CHARS = 500
MAX_SEARCH_RESULTS = 10

BLOCKED_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.internal",
        "100.100.100.200",
    }
)

_BINARY_CONTENT_TYPES = frozenset(
    {
        "application/octet-stream",
        "image/",
        "audio/",
        "video/",
        "application/zip",
        "application/gzip",
        "application/pdf",
    }
)

_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class _HTMLTextExtractor(HTMLParser):
    """Small stdlib HTML-to-text extractor for LLM-friendly fetch output."""

    _BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif normalized in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif normalized in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        text = html.unescape("".join(self._parts))
        lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
        compact = "\n".join(line for line in lines if line)
        return _BLANK_LINES_RE.sub("\n\n", compact).strip()


def _is_private_ip(ip_str: str) -> bool:
    """Check if an IP address is in a private/reserved range."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return addr.is_private or addr.is_reserved or addr.is_loopback
    except ValueError:
        return False


def _check_url_safety(url: str) -> str | None:
    """Return an error message if the URL is unsafe, otherwise None."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return "Error: Only http:// and https:// URLs are supported."

    hostname = parsed.hostname
    if not hostname:
        return "Error: Could not extract hostname from URL."

    if hostname in BLOCKED_HOSTS:
        return f"Error: Access to '{hostname}' is blocked (cloud metadata endpoint)."

    # Check if hostname is a literal IP
    if _is_private_ip(hostname):
        return f"Error: Access to private/reserved IP '{hostname}' is blocked."

    return None


def _dns_check(hostname: str) -> tuple[str | None, list[str]]:
    """Resolve hostname and check all IPs for private ranges.

    Returns ``(error_or_none, resolved_ips)``.  When *error* is ``None`` the
    caller can connect directly to one of the returned IPs — this closes the
    DNS-rebinding TOCTOU window.
    """
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        ips: list[str] = []
        for _family, _type, _proto, _canonname, sockaddr in results:
            ip = str(sockaddr[0])
            if _is_private_ip(ip):
                return (
                    f"Error: DNS for '{hostname}' resolved to private IP"
                    f" '{ip}'. Request blocked (SSRF protection).",
                    [],
                )
            ips.append(ip)
        return None, ips
    except socket.gaierror:
        return f"Error: Could not resolve hostname '{hostname}'.", []


def _is_binary_content_type(content_type: str) -> bool:
    """Check if content type indicates binary data."""
    ct = content_type.lower().split(";")[0].strip()
    return any(ct.startswith(bt) for bt in _BINARY_CONTENT_TYPES)


def _extract_text_from_html(text: str, content_type: str) -> str:
    """Return readable text for HTML responses, otherwise compact plain text."""
    if "html" not in content_type.lower():
        lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    parser = _HTMLTextExtractor()
    parser.feed(text)
    parser.close()
    return parser.get_text()


async def _read_response_text_limited(response: httpx.Response) -> tuple[str | None, str, int]:
    """Read a text response with a hard byte limit."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_SIZE:
            return (
                f"Error: Response too large (exceeded {MAX_RESPONSE_SIZE} bytes).",
                "",
                total,
            )
        chunks.append(chunk)

    raw = b"".join(chunks)
    encoding = getattr(response, "encoding", None)
    if not isinstance(encoding, str) or not encoding:
        encoding = "utf-8"
    return None, raw.decode(encoding, errors="replace"), total


class WebFetchTool(Tool):
    """Fetch content from a URL with SSRF protection."""

    name = "web_fetch"
    description = "Fetch content from a URL and return the response body as text."
    params = [
        ToolParam(
            name="url",
            type="string",
            description="URL to fetch (must start with http:// or https://)",
        ),
        ToolParam(
            name="timeout",
            type="integer",
            description="Request timeout in seconds (default: 30)",
            required=False,
        ),
        ToolParam(
            name="format",
            type="string",
            description=(
                "Output format: raw returns response text, text extracts readable text from HTML"
            ),
            required=False,
            enum=["raw", "text"],
        ),
    ]
    risk_level = RiskLevel.MEDIUM

    def __init__(self, settings: WebSettings | None = None) -> None:
        self._settings = settings or WebSettings()
        self._semaphore = asyncio.Semaphore(max(1, self._settings.fetch_max_concurrent))

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url")
        timeout = kwargs.get("timeout", self._settings.timeout_seconds or DEFAULT_TIMEOUT)
        output_format = kwargs.get("format", "raw")

        if not isinstance(url, str):
            return "Error: 'url' is a required string parameter."

        if not isinstance(timeout, int):
            timeout = self._settings.timeout_seconds or DEFAULT_TIMEOUT

        if output_format not in {"raw", "text"}:
            return "Error: 'format' must be one of: raw, text."

        # 1. URL safety check
        err = _check_url_safety(url)
        if err:
            return err

        # 2. DNS resolution check — resolve once and pin the IPs
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        err, resolved_ips = _dns_check(hostname)
        if err:
            return err

        # 3. Fetch with redirect following, using pre-resolved IPs
        try:
            async with self._semaphore:
                return await self._fetch(url, timeout, resolved_ips, str(output_format))
        except httpx.TimeoutException:
            return f"Error: Request to '{url}' timed out after {timeout}s."
        except Exception as e:
            return f"Error fetching '{url}': {e}"

    async def _fetch(
        self,
        url: str,
        timeout: int,
        resolved_ips: list[str] | None = None,
        output_format: str = "raw",
    ) -> str:
        """Fetch URL with manual redirect following and per-hop SSRF checks.

        ``resolved_ips`` pins the connection to pre-resolved addresses,
        preventing DNS-rebinding attacks (TOCTOU).
        """
        current_url = url
        current_ips = resolved_ips
        for _ in range(MAX_REDIRECTS):
            # Pin DNS resolution to pre-resolved IPs when available.
            # Skip IP-pinning for HTTPS — TLS cert validation already prevents
            # DNS-rebinding (cert won't match a rogue IP).
            parsed_u = urlparse(current_url)
            if current_ips and parsed_u.scheme != "https":
                hostname = parsed_u.hostname or ""
                url_to_fetch = current_url.replace(f"://{hostname}", f"://{current_ips[0]}", 1)
                headers = {"Host": hostname, "User-Agent": self._settings.user_agent}
                verify = False
            else:
                url_to_fetch = current_url
                headers = {"User-Agent": self._settings.user_agent}
                verify = True

            async with (
                httpx.AsyncClient(timeout=timeout, follow_redirects=False, verify=verify) as client,
                client.stream("GET", url_to_fetch, headers=headers) as response,
            ):
                if response.is_redirect:
                    location = response.headers.get("location", "")
                    if not location:
                        return "Error: Redirect with no Location header."

                    # Resolve relative redirects
                    if not location.startswith(("http://", "https://")):
                        from urllib.parse import urljoin

                        location = urljoin(current_url, location)

                    # SSRF check on redirect target
                    err = _check_url_safety(location)
                    if err:
                        return err
                    rp = urlparse(location)
                    if rp.hostname:
                        dns_err, new_ips = _dns_check(rp.hostname)
                        if dns_err:
                            return dns_err
                        current_ips = new_ips

                    current_url = location
                    continue

                # Non-redirect response
                content_type = response.headers.get("content-type", "")
                if _is_binary_content_type(content_type):
                    return (
                        f"Error: Response is binary ({content_type})."
                        " web_fetch only supports text content."
                    )

                # Size check (gracefully handle non-numeric Content-Length)
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        size = int(content_length)
                    except (ValueError, TypeError):
                        size = 0
                    if size > MAX_RESPONSE_SIZE:
                        return (
                            f"Error: Response too large ({content_length} bytes,"
                            f" max {MAX_RESPONSE_SIZE})."
                        )

                read_error, full_text, byte_count = await _read_response_text_limited(response)
                if read_error:
                    return read_error

                body_text = (
                    _extract_text_from_html(full_text, content_type)
                    if output_format == "text"
                    else full_text
                )
                text = body_text[:MAX_TEXT_CHARS]
                truncated = " (truncated)" if len(full_text) > MAX_TEXT_CHARS else ""

                header = (
                    f"URL: {current_url}\n"
                    f"Status: {response.status_code}\n"
                    f"Content-Type: {content_type}\n"
                    f"Size: {len(full_text)} chars / {byte_count} bytes{truncated}\n"
                    f"---\n"
                )
                return header + text

        return f"Error: Too many redirects (max {MAX_REDIRECTS})."


class WebSearchTool(Tool):
    """Search the web via ddgs using an explicit DuckDuckGo backend."""

    name = "web_search"
    description = (
        "Search the web and return candidate URLs with snippets. "
        "Use web_fetch to read any result page."
    )
    params = [
        ToolParam(name="query", type="string", description="Search query"),
        ToolParam(
            name="max_results",
            type="integer",
            description="Maximum number of results to return (default: 5, max: 10)",
            required=False,
        ),
        ToolParam(
            name="site",
            type="string",
            description="Optional domain restriction, e.g. example.com",
            required=False,
        ),
        ToolParam(
            name="region",
            type="string",
            description="DuckDuckGo region code, e.g. wt-wt, us-en, ru-ru (default: wt-wt)",
            required=False,
        ),
        ToolParam(
            name="timelimit",
            type="string",
            description="Optional time limit: d, w, m, y",
            required=False,
            enum=["d", "w", "m", "y"],
        ),
    ]
    risk_level = RiskLevel.MEDIUM

    def __init__(self, settings: WebSettings | None = None) -> None:
        self._settings = settings or WebSettings()
        self._semaphore = asyncio.Semaphore(max(1, self._settings.search_max_concurrent))

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query")
        max_results = kwargs.get("max_results", 5)
        site = kwargs.get("site")
        region = kwargs.get("region", "wt-wt")
        timelimit = kwargs.get("timelimit")

        if not isinstance(query, str) or not query.strip():
            return "Error: 'query' is a required non-empty string parameter."

        query = query.strip()
        if len(query) > MAX_QUERY_CHARS:
            return f"Error: Query too long (max {MAX_QUERY_CHARS} characters)."

        if not isinstance(max_results, int):
            max_results = 5
        max_results = min(max(1, max_results), MAX_SEARCH_RESULTS)

        if site is not None:
            if not isinstance(site, str) or not _DOMAIN_RE.match(site.strip()):
                return "Error: 'site' must be a domain like example.com."
            query = f"site:{site.strip()} {query}"

        if not isinstance(region, str) or not region.strip():
            region = "wt-wt"

        if timelimit is not None and timelimit not in {"d", "w", "m", "y"}:
            return "Error: 'timelimit' must be one of: d, w, m, y."

        try:
            async with self._semaphore:
                results = await run_in_thread(
                    self._search_sync,
                    query,
                    max_results,
                    region.strip(),
                    timelimit if isinstance(timelimit, str) else None,
                )
        except TimeoutException:
            return f"Error: Web search timed out after {self._settings.timeout_seconds}s."
        except RatelimitException:
            return "Error: Web search rate limit reached. Please retry later."
        except DDGSException as e:
            return f"Error: Web search failed: {e}"
        except Exception as e:
            return f"Error: Web search failed: {type(e).__name__}: {e}"

        if not results:
            return "No search results found."

        lines = [f"Search results for: {query}", "---"]
        for idx, item in enumerate(results, start=1):
            title = str(item.get("title") or "").strip()
            url = str(item.get("href") or item.get("url") or "").strip()
            snippet = str(item.get("body") or item.get("snippet") or "").strip()
            if not title and not url:
                continue
            lines.extend(
                [
                    f"{idx}. {title or '(untitled)'}",
                    f"URL: {url}",
                    f"Snippet: {snippet or '(none)'}",
                ]
            )
        return "\n".join(lines)

    def _search_sync(
        self,
        query: str,
        max_results: int,
        region: str,
        timelimit: str | None,
    ) -> list[dict[str, Any]]:
        with DDGS(timeout=self._settings.timeout_seconds) as ddgs:
            results = ddgs.text(
                query,
                region=region,
                safesearch="moderate",
                timelimit=timelimit,
                max_results=max_results,
                backend=self._settings.search_backend,
            )
        return [dict(item) for item in results[:max_results]]
