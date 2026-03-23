from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
MAX_RESPONSE_SIZE = 1_048_576  # 1 MB
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 50_000

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
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        url = kwargs.get("url")
        timeout = kwargs.get("timeout", DEFAULT_TIMEOUT)

        if not isinstance(url, str):
            return "Error: 'url' is a required string parameter."

        if not isinstance(timeout, int):
            timeout = DEFAULT_TIMEOUT

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
            return await self._fetch(url, timeout, resolved_ips)
        except httpx.TimeoutException:
            return f"Error: Request to '{url}' timed out after {timeout}s."
        except Exception as e:
            return f"Error fetching '{url}': {e}"

    async def _fetch(self, url: str, timeout: int, resolved_ips: list[str] | None = None) -> str:
        """Fetch URL with manual redirect following and per-hop SSRF checks.

        ``resolved_ips`` pins the connection to pre-resolved addresses,
        preventing DNS-rebinding attacks (TOCTOU).
        """
        current_url = url
        current_ips = resolved_ips
        for _ in range(MAX_REDIRECTS):
            # Pin DNS resolution to pre-resolved IPs when available
            if current_ips:
                parsed_u = urlparse(current_url)
                hostname = parsed_u.hostname or ""
                # Replace hostname with resolved IP in URL
                url_to_fetch = current_url.replace(f"://{hostname}", f"://{current_ips[0]}", 1)
                headers = {"Host": hostname}
                verify = parsed_u.scheme == "https"
            else:
                url_to_fetch = current_url
                headers = {}
                verify = True

            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, verify=verify
            ) as client:
                response = await client.get(url_to_fetch, headers=headers)

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

                text = response.text[:MAX_TEXT_CHARS]
                truncated = " (truncated)" if len(response.text) > MAX_TEXT_CHARS else ""

                header = (
                    f"URL: {current_url}\n"
                    f"Status: {response.status_code}\n"
                    f"Content-Type: {content_type}\n"
                    f"Size: {len(response.text)} chars{truncated}\n"
                    f"---\n"
                )
                return header + text

        return f"Error: Too many redirects (max {MAX_REDIRECTS})."
