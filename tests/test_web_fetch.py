"""Tests for WebFetchTool with SSRF protection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.extensions.tools.builtin.web import (
    WebFetchTool,
    _check_url_safety,
    _dns_check,
    _is_private_ip,
)


@pytest.fixture
def tool() -> WebFetchTool:
    return WebFetchTool()


# ── SSRF helpers ─────────────────────────────────────────────────────────────


def test_is_private_ip() -> None:
    assert _is_private_ip("127.0.0.1")
    assert _is_private_ip("10.0.0.1")
    assert _is_private_ip("192.168.1.1")
    assert _is_private_ip("172.16.0.1")
    assert not _is_private_ip("8.8.8.8")
    assert not _is_private_ip("1.1.1.1")


def test_check_url_safety_blocks_private() -> None:
    assert _check_url_safety("http://127.0.0.1/secret") is not None
    assert _check_url_safety("http://169.254.169.254/metadata") is not None
    assert _check_url_safety("ftp://example.com/file") is not None
    assert _check_url_safety("http://example.com/ok") is None
    assert _check_url_safety("https://google.com") is None


def test_check_url_safety_no_scheme() -> None:
    err = _check_url_safety("example.com")
    assert err is not None
    assert "http" in err


def test_dns_check_private_ip() -> None:
    """Mock DNS resolving to a private IP should be blocked."""
    fake_result = [(2, 1, 6, "", ("10.0.0.1", 80))]
    with patch(
        "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo", return_value=fake_result
    ):
        err, ips = _dns_check("evil.example.com")
        assert err is not None
        assert "private IP" in err
        assert ips == []


def test_dns_check_public_ip() -> None:
    fake_result = [(2, 1, 6, "", ("93.184.216.34", 80))]
    with patch(
        "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo", return_value=fake_result
    ):
        err, ips = _dns_check("example.com")
        assert err is None
        assert "93.184.216.34" in ips


# ── WebFetchTool ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_url_param(tool: WebFetchTool) -> None:
    """Missing url parameter returns an error."""
    res = await tool.execute()
    assert "Error" in res


@pytest.mark.asyncio
async def test_rejects_no_scheme(tool: WebFetchTool) -> None:
    res = await tool.execute(url="example.com")
    assert "Error" in res


@pytest.mark.asyncio
async def test_rejects_private_ip(tool: WebFetchTool) -> None:
    res = await tool.execute(url="http://127.0.0.1/secret")
    assert "Error" in res
    assert "private" in res.lower() or "blocked" in res.lower()


@pytest.mark.asyncio
async def test_rejects_blocked_hosts(tool: WebFetchTool) -> None:
    # Mock DNS to return a public IP (so it doesn't fail on DNS before host check)
    fake_dns = [(2, 1, 6, "", ("169.254.169.254", 80))]
    with patch(
        "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo", return_value=fake_dns
    ):
        res = await tool.execute(url="http://169.254.169.254/latest/meta-data/")
    assert "Error" in res
    assert "blocked" in res.lower()


@pytest.mark.asyncio
async def test_content_length_non_numeric(tool: WebFetchTool) -> None:
    """Non-numeric Content-Length header must not crash."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {
        "content-type": "text/html",
        "content-length": "not-a-number",
    }
    mock_response.text = "<html>Ok</html>"
    mock_response.is_redirect = False

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    fake_dns = [(2, 1, 6, "", ("93.184.216.34", 80))]
    with (
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo",
            return_value=fake_dns,
        ),
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        res = await tool.execute(url="https://example.com")

    assert "Status: 200" in res
    assert "Ok" in res


@pytest.mark.asyncio
async def test_success(tool: WebFetchTool) -> None:
    """Mock a successful HTTP response."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html; charset=utf-8"}
    mock_response.text = "<html>Hello</html>"
    mock_response.is_redirect = False

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    fake_dns = [(2, 1, 6, "", ("93.184.216.34", 80))]
    with (
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo", return_value=fake_dns
        ),
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.httpx.AsyncClient", return_value=mock_client
        ),
    ):
        res = await tool.execute(url="https://example.com")

    assert "Status: 200" in res
    assert "Hello" in res
