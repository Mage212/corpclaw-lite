"""Extra web fetch tool tests."""

from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

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
