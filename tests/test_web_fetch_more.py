"""Extra web fetch tool tests."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.config.settings import WebSettings
from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool


class _StreamContext:
    def __init__(self, response: object) -> None:
        self._response = response

    async def __aenter__(self) -> object:
        return self._response

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _MockResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        is_redirect: bool = False,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = is_redirect
        self.encoding = "utf-8"
        self._chunks = chunks if chunks is not None else [text.encode("utf-8")]

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


@pytest.mark.asyncio
async def test_web_fetch_binary_response():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()
        mock_response = _MockResponse(
            status_code=200,
            headers={"content-type": "application/pdf"},
        )
        mock_client.stream.return_value = _StreamContext(mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com")
        assert "binary" in res


@pytest.mark.asyncio
async def test_web_fetch_too_large():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()
        mock_response = _MockResponse(
            status_code=200,
            headers={"content-type": "text/html", "content-length": "2000000"},
        )
        mock_client.stream.return_value = _StreamContext(mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com")
        assert "too large" in res


@pytest.mark.asyncio
async def test_web_fetch_redirect():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()

        redir_response = _MockResponse(is_redirect=True, headers={"location": "http://other.com"})
        final_response = _MockResponse(
            status_code=200,
            headers={"content-type": "text/html"},
            text="Redirected text",
        )

        mock_client.stream.side_effect = [
            _StreamContext(redir_response),
            _StreamContext(final_response),
        ]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com")
        assert "Redirected text" in res


@pytest.mark.asyncio
async def test_web_fetch_stream_body_too_large_without_content_length():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()
        mock_response = _MockResponse(
            status_code=200,
            headers={"content-type": "text/html"},
            chunks=[b"x" * 600_000, b"y" * 600_000],
        )
        mock_client.stream.return_value = _StreamContext(mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com")
        assert "too large" in res


@pytest.mark.asyncio
async def test_web_fetch_text_format_extracts_html_text():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()
        mock_response = _MockResponse(
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><script>ignore()</script><body><h1>Hello</h1>"
                "<p>Readable text</p></body></html>"
            ),
        )
        mock_client.stream.return_value = _StreamContext(mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com", format="text")

    assert "Hello" in res
    assert "Readable text" in res
    assert "ignore()" not in res


@pytest.mark.asyncio
async def test_web_fetch_raw_format_preserves_html():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()
        mock_response = _MockResponse(
            status_code=200,
            headers={"content-type": "text/html"},
            text="<html><body>Hello</body></html>",
        )
        mock_client.stream.return_value = _StreamContext(mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com")

    assert "<html>" in res


@pytest.mark.asyncio
async def test_web_fetch_sends_user_agent():
    tool = WebFetchTool(WebSettings(user_agent="CorpClawTest/1.0"))
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = MagicMock()
        mock_response = _MockResponse(
            status_code=200,
            headers={"content-type": "text/plain"},
            text="ok",
        )
        mock_client.stream.return_value = _StreamContext(mock_response)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        await tool.execute(url="https://test.com")

    _method, _url = mock_client.stream.call_args.args
    headers = mock_client.stream.call_args.kwargs["headers"]
    assert headers["User-Agent"] == "CorpClawTest/1.0"


@pytest.mark.asyncio
async def test_web_fetch_concurrency_limited():
    tool = WebFetchTool(WebSettings(fetch_max_concurrent=1))
    active = 0
    max_active = 0

    async def fake_fetch(
        url: str,
        timeout: int,
        resolved_ips: list[str] | None = None,
        output_format: str = "raw",
    ) -> str:
        nonlocal active, max_active
        _ = (url, timeout, resolved_ips, output_format)
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    with patch(
        "corpclaw_lite.extensions.tools.builtin.web._dns_check",
        return_value=(None, ["8.8.8.8"]),
    ):
        tool._fetch = fake_fetch  # type: ignore[method-assign]
        await asyncio.gather(
            tool.execute(url="https://example.com/1"),
            tool.execute(url="https://example.com/2"),
        )

    assert max_active == 1
