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


def test_web_search_defaults_are_conservative() -> None:
    settings = WebSettings()
    tool = WebSearchTool(settings)

    assert settings.search_max_concurrent == 1
    assert tool.parallel_safe is False


@pytest.mark.asyncio
async def test_web_search_normalizes_ddgs_results() -> None:
    _FakeDDGS.calls = []
    tool = WebSearchTool(WebSettings(timeout_seconds=7))

    with patch("corpclaw_lite.extensions.tools.builtin.web.DDGS", _FakeDDGS):
        res = await tool.execute(query="python programming", max_results=5)

    assert "Example result" in res
    assert "https://example.com/page" in res
    assert "Example snippet" in res
    assert _FakeDDGS.calls[0]["backend"] == "auto"
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


# ── B-052: retry + unavailable marker ────────────────────────────────────────


class _FlakyDDGS:
    """Fake DDGS that fails N times then succeeds (or fails forever if fail_forever).

    Used as a *class* to patch the ``DDGS`` name: ``DDGS(timeout=...)`` instantiates it.
    Counts calls at the class level so the test can assert retry behaviour.
    """

    fail_times: int = 0
    fail_forever: bool = False
    call_count: int = 0

    def __init__(self, *, timeout: int) -> None:
        self.timeout = timeout

    def __enter__(self) -> _FlakyDDGS:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        from ddgs.exceptions import DDGSException

        type(self).call_count += 1
        if type(self).fail_forever or type(self).call_count <= type(self).fail_times:
            raise DDGSException("No results found.")
        return [{"title": "ok", "href": "https://example.com", "body": "snippet"}]


@pytest.mark.asyncio
async def test_web_search_retries_then_succeeds() -> None:
    """A transient DDGSException on the first attempt is retried; the tool returns results."""
    _FlakyDDGS.fail_times = 1
    _FlakyDDGS.fail_forever = False
    _FlakyDDGS.call_count = 0
    tool = WebSearchTool(WebSettings(search_retry_attempts=3, search_retry_backoff_seconds=0.0))
    with patch("corpclaw_lite.extensions.tools.builtin.web.DDGS", _FlakyDDGS):
        res = await tool.execute(query="python", max_results=5)
    assert "ok" in res
    assert _FlakyDDGS.call_count == 2  # 1 failed + 1 succeeded


@pytest.mark.asyncio
async def test_web_search_unavailable_after_all_retries() -> None:
    """When all retry attempts fail, the tool returns an 'unavailable (infrastructure)' error
    that the research layer uses to refund the budget."""
    _FlakyDDGS.fail_times = 0
    _FlakyDDGS.fail_forever = True
    _FlakyDDGS.call_count = 0
    tool = WebSearchTool(WebSettings(search_retry_attempts=3, search_retry_backoff_seconds=0.0))
    with patch("corpclaw_lite.extensions.tools.builtin.web.DDGS", _FlakyDDGS):
        res = await tool.execute(query="python", max_results=5)
    assert res.startswith("Error")
    assert "unavailable" in res
    assert "infrastructure" in res
    assert _FlakyDDGS.call_count == 3  # exactly search_retry_attempts


@pytest.mark.asyncio
async def test_web_search_retry_count_respected() -> None:
    """The tool makes exactly search_retry_attempts calls, no more."""
    _FlakyDDGS.fail_times = 0
    _FlakyDDGS.fail_forever = True
    _FlakyDDGS.call_count = 0
    tool = WebSearchTool(WebSettings(search_retry_attempts=2, search_retry_backoff_seconds=0.0))
    with patch("corpclaw_lite.extensions.tools.builtin.web.DDGS", _FlakyDDGS):
        await tool.execute(query="python", max_results=5)
    assert _FlakyDDGS.call_count == 2
