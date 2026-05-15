"""Tests for WebSearchTool."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from corpclaw_lite.config.settings import WebSettings
from corpclaw_lite.extensions.tools.builtin.web import WebSearchTool


class _FakeDDGS:
    calls: list[dict[str, Any]] = []

    def __init__(self, *, timeout: int) -> None:
        self.timeout = timeout

    def __enter__(self) -> _FakeDDGS:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"query": query, **kwargs, "timeout": self.timeout})
        return [
            {
                "title": "Example result",
                "href": "https://example.com/page",
                "body": "Example snippet",
            }
        ]


@pytest.mark.asyncio
async def test_web_search_normalizes_ddgs_results() -> None:
    _FakeDDGS.calls = []
    tool = WebSearchTool(WebSettings(timeout_seconds=7))

    with patch("corpclaw_lite.extensions.tools.builtin.web.DDGS", _FakeDDGS):
        res = await tool.execute(query="python programming", max_results=5)

    assert "Example result" in res
    assert "https://example.com/page" in res
    assert "Example snippet" in res
    assert _FakeDDGS.calls[0]["backend"] == "duckduckgo"
    assert _FakeDDGS.calls[0]["timeout"] == 7
    assert _FakeDDGS.calls[0]["max_results"] == 5


@pytest.mark.asyncio
async def test_web_search_site_restriction() -> None:
    _FakeDDGS.calls = []
    tool = WebSearchTool()

    with patch("corpclaw_lite.extensions.tools.builtin.web.DDGS", _FakeDDGS):
        await tool.execute(query="docs", site="example.com")

    assert _FakeDDGS.calls[0]["query"] == "site:example.com docs"


@pytest.mark.asyncio
async def test_web_search_validation_errors() -> None:
    tool = WebSearchTool()

    assert "query" in await tool.execute(query="")
    assert "Query too long" in await tool.execute(query="x" * 501)
    assert "site" in await tool.execute(query="docs", site="https://example.com")
    assert "timelimit" in await tool.execute(query="docs", timelimit="hour")


@pytest.mark.asyncio
async def test_web_search_concurrency_limited() -> None:
    tool = WebSearchTool(WebSettings(search_max_concurrent=1))
    active = 0
    max_active = 0

    def fake_search(
        query: str,
        max_results: int,
        region: str,
        timelimit: str | None,
    ) -> list[dict[str, Any]]:
        nonlocal active, max_active
        _ = (query, max_results, region, timelimit)
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.02)
        active -= 1
        return [{"title": "ok", "href": "https://example.com", "body": "snippet"}]

    tool._search_sync = fake_search  # type: ignore[method-assign]
    await asyncio.gather(
        tool.execute(query="one"),
        tool.execute(query="two"),
    )

    assert max_active == 1
