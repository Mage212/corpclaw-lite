"""Extra web fetch tool tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool


@pytest.mark.asyncio
async def test_web_fetch_binary_response():
    tool = WebFetchTool()
    with (
        patch("httpx.AsyncClient") as mock_client_cls,
        patch("socket.getaddrinfo", return_value=[(1, 2, 3, "", ("8.8.8.8", 80))]),
    ):
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/pdf"}
        mock_response.is_redirect = False
        mock_client.get.return_value = mock_response
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
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html", "content-length": "2000000"}
        mock_response.is_redirect = False
        mock_client.get.return_value = mock_response
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
        mock_client = AsyncMock()

        redir_response = MagicMock()
        redir_response.is_redirect = True
        redir_response.headers = {"location": "http://other.com"}

        final_response = MagicMock()
        final_response.is_redirect = False
        final_response.headers = {"content-type": "text/html"}
        final_response.text = "Redirected text"
        final_response.status_code = 200

        mock_client.get.side_effect = [redir_response, final_response]
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        res = await tool.execute(url="http://test.com")
        assert "Redirected text" in res
