"""Tests for B-054: source availability filter + dynamic budget.

Covers:
- B-054-1: ``web.py:_fetch`` returns an Error for non-2xx responses (so a 404/403
  page is never stored or cited), while 2xx responses pass through as before.
- B-054-2: the research runtime exposes a dynamic source/search budget that grows
  when many fetches return non-2xx, bounded by ``dynamic_budget_max_multiplier``.
- B-054-3: ``finalize_report`` rejects an answer that cites an unavailable source
  (HTTP 4xx/5xx) and accepts one that cites only usable (2xx) sources.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.config.settings import ResearchSettings
from corpclaw_lite.extensions.tools.builtin.research import (
    ResearchRuntime,
    _is_usable_status,
)
from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool
from corpclaw_lite.users.models import User

# ── helpers ──────────────────────────────────────────────────────────────────


def _user() -> User:
    return User(id=11, telegram_id=11, name="Tester", department="engineering")


def _runtime(
    tmp_path: Path,
    *,
    strict: bool = True,
    target_usable_sources: int = 5,
    dynamic_budget_max_multiplier: float = 2.5,
    deep_max_sources: int = 10,
    normal_max_sources: int = 5,
    deep_search_waves: int = 3,
    normal_search_waves: int = 1,
) -> ResearchRuntime:
    return ResearchRuntime(
        settings=ResearchSettings(
            finalize_strict=strict,
            target_usable_sources=target_usable_sources,
            dynamic_budget_max_multiplier=dynamic_budget_max_multiplier,
            deep_max_sources=deep_max_sources,
            normal_max_sources=normal_max_sources,
            deep_search_waves=deep_search_waves,
            normal_search_waves=normal_search_waves,
        ),
        workspace_base=tmp_path,
    )


def _add_source(
    runtime: ResearchRuntime,
    user: User,
    run_id: str,
    url: str,
    *,
    status: int = 200,
    body: str = "Evidence.",
    title: str = "Title",
) -> dict[str, object]:
    return runtime.store_source(
        user,
        run_id,
        url,
        f"url: {url}\nstatus: {status}\nsize: 10\n---\n{title}\n{body}",
    )


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
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.is_redirect = is_redirect
        self.encoding = "utf-8"
        self._body = text.encode("utf-8")

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self._body


_FAKE_DNS = [(2, 1, 6, "", ("93.184.216.34", 80))]


async def _fetch_with_status(tool: WebFetchTool, url: str, status_code: int) -> str:
    mock_response = _MockResponse(
        status_code=status_code,
        headers={"content-type": "text/html", "content-length": "0"},
        text=f"<html>error {status_code}</html>",
    )
    mock_client = MagicMock()
    mock_client.stream.return_value = _StreamContext(mock_response)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    with (
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo",
            return_value=_FAKE_DNS,
        ),
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        return await tool.execute(url=url)


# ── B-054-1: HTTP filter in _fetch ───────────────────────────────────────────


@pytest.mark.parametrize("status", [404, 403, 500, 502, 410, 451])
@pytest.mark.asyncio
async def test_fetch_rejects_non_2xx(status: int) -> None:
    """Non-2xx responses must return an Error and never a stored body."""
    res = await _fetch_with_status(WebFetchTool(), "https://example.com/page", status)
    assert res.startswith("Error")
    assert f"HTTP {status}" in res
    assert "unavailable" in res.lower()
    # The error body must not leak the 4xx/5xx page content.
    assert f"error {status}</html>" not in res


@pytest.mark.parametrize("status", [200, 201, 204])
@pytest.mark.asyncio
async def test_fetch_accepts_2xx(status: int) -> None:
    """2xx responses must pass through with the normal header + body."""
    res = await _fetch_with_status(WebFetchTool(), "https://example.com/ok", status)
    assert not res.startswith("Error")
    assert f"Status: {status}" in res


@pytest.mark.asyncio
async def test_fetch_2xx_body_preserved() -> None:
    """A 200 response still returns its body text (no regression)."""
    tool = WebFetchTool()
    mock_response = _MockResponse(
        status_code=200,
        headers={"content-type": "text/html"},
        text="<html>Real content here</html>",
    )
    mock_client = MagicMock()
    mock_client.stream.return_value = _StreamContext(mock_response)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    with (
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.socket.getaddrinfo",
            return_value=_FAKE_DNS,
        ),
        patch(
            "corpclaw_lite.extensions.tools.builtin.web.httpx.AsyncClient",
            return_value=mock_client,
        ),
    ):
        res = await tool.execute(url="https://example.com/real")
    assert "Real content here" in res


# ── B-054-2: dynamic source budget ───────────────────────────────────────────


def test_is_usable_status() -> None:
    for ok in ("200", "201", 204, "206", 299):
        assert _is_usable_status(ok) is True
    for bad in ("404", "403", 500, "", None, "not-a-number", "0", "300", "199"):
        assert _is_usable_status(bad) is False


def test_available_sources_counts_only_2xx(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    _add_source(runtime, user, "r1", "https://ex.com/a", status=200)
    _add_source(runtime, user, "r1", "https://ex.com/b", status=200)
    _add_source(runtime, user, "r1", "https://ex.com/c", status=404)
    _add_source(runtime, user, "r1", "https://ex.com/d", status=403)
    _add_source(runtime, user, "r1", "https://ex.com/e", status=500)
    # Only the two 2xx sources count.
    assert runtime.available_sources_count(user, "r1") == 2
    assert len(runtime.list_sources(user, "r1")) == 5


def test_effective_max_sources_grows_with_failures(tmp_path: Path) -> None:
    """Each failed (non-2xx) source raises the fetch cap by one, up to the cap.

    Failure-driven: limit = min(max(base, base + failed), cap).
    """
    user = _user()
    runtime = _runtime(
        tmp_path,
        deep_max_sources=10,
        target_usable_sources=5,
        dynamic_budget_max_multiplier=2.5,  # cap = 25
    )
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    # Clean run: no sources yet -> base limit applies.
    assert runtime.effective_max_sources(user, "r1", "deep_research") == 10
    # 8 failures (no usable) -> base(10) + failed(8) = 18, still < cap(25).
    for i in range(8):
        _add_source(runtime, user, "r1", f"https://ex.com/{i}", status=404)
    assert runtime.effective_max_sources(user, "r1", "deep_research") == 18
    # Push past the cap: 20 failures -> base(10)+20=30 -> capped at 25.
    for i in range(8, 20):
        _add_source(runtime, user, "r1", f"https://ex.com/{i}", status=404)
    assert runtime.effective_max_sources(user, "r1", "deep_research") == 25


def test_effective_max_sources_never_below_base(tmp_path: Path) -> None:
    """Usable-only fetches keep the limit at base (no inflation)."""
    user = _user()
    runtime = _runtime(
        tmp_path,
        deep_max_sources=10,
        target_usable_sources=5,
        dynamic_budget_max_multiplier=2.5,
    )
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    for i in range(10):
        _add_source(runtime, user, "r1", f"https://ex.com/{i}", status=200)
    # base(10) + failed(0) = 10.
    assert runtime.effective_max_sources(user, "r1", "deep_research") == 10


def test_reserve_fetch_uses_dynamic_limit(tmp_path: Path) -> None:
    """Failed fetches grant extra fetch slots, but the hard cap still blocks."""
    user = _user()
    runtime = _runtime(
        tmp_path,
        deep_max_sources=3,
        target_usable_sources=3,
        dynamic_budget_max_multiplier=2.0,  # cap = 6
    )
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    # 3 failed sources: limit = base(3) + failed(3) = 6, used=3 < 6 -> ok.
    for i in range(3):
        _add_source(runtime, user, "r1", f"https://ex.com/f{i}", status=404)
    assert runtime.reserve_fetch(user, "r1", "deep_research") is None
    # Fill up to the cap (6 stored): next fetch must be blocked.
    for i in range(3, 6):
        _add_source(runtime, user, "r1", f"https://ex.com/f{i}", status=403)
    blocked = runtime.reserve_fetch(user, "r1", "deep_research")
    assert blocked is not None
    assert "budget exceeded" in blocked.lower()


def test_effective_search_waves_grows_under_pressure(tmp_path: Path) -> None:
    """Search waves expand after failed fetches (non-2xx), up to the cap.

    Failure-driven, not gap-driven: at run start there are always zero usable
    sources, so waves must NOT inflate until the agent has actually fetched bad
    pages and needs more attempts to find alternatives.
    """
    user = _user()
    runtime = _runtime(
        tmp_path,
        deep_search_waves=3,
        target_usable_sources=5,
        dynamic_budget_max_multiplier=2.0,  # cap = 6
    )
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    # Run start: no sources at all -> base limit, no inflation.
    assert runtime.effective_search_waves(user, "r1", "deep_research") == 3
    # Usable sources alone do not inflate waves.
    _add_source(runtime, user, "r1", "https://ex.com/a", status=200)
    _add_source(runtime, user, "r1", "https://ex.com/b", status=200)
    assert runtime.effective_search_waves(user, "r1", "deep_research") == 3
    # Each failed fetch grants one more wave: base(3) + failed(2) = 5.
    _add_source(runtime, user, "r1", "https://ex.com/c", status=404)
    _add_source(runtime, user, "r1", "https://ex.com/d", status=403)
    assert runtime.effective_search_waves(user, "r1", "deep_research") == 5
    # Push past the cap: base(3) + failed(5) = 8 -> capped at 6.
    for i in range(5):
        _add_source(runtime, user, "r1", f"https://ex.com/f{i}", status=404)
    assert runtime.effective_search_waves(user, "r1", "deep_research") == 6


# ── B-054-3: finalize rejects citations of unavailable sources ───────────────


def test_strict_rejects_unavailable_cited_source(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    src = _add_source(runtime, user, "r1", "https://ex.com/dead", status=404)
    sid = str(src["source_id"])
    answer = f"## Summary\nResult [{sid}] demonstrates the effect."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "unavailable" in result.lower()
    assert sid in result


def test_strict_accepts_usable_cited_source(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    src = _add_source(runtime, user, "r1", "https://ex.com/live", status=200)
    sid = str(src["source_id"])
    answer = f"## Summary\nResult [{sid}] demonstrates the effect."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert not result.startswith("Error")
    assert "## Summary" in result


def test_strict_rejects_unavailable_cited_url(tmp_path: Path) -> None:
    """A cited URL pointing at an unavailable source is also rejected."""
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    _add_source(runtime, user, "r1", "https://ex.com/blocked", status=403)
    answer = "## Summary\nSee https://ex.com/blocked for the analysis."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "unavailable" in result.lower()


def test_strict_rejects_mixed_citation_only_for_unavailable(tmp_path: Path) -> None:
    """Citing a usable source is fine; citing an unavailable one fails."""
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    good = _add_source(runtime, user, "r1", "https://ex.com/good", status=200)
    bad = _add_source(runtime, user, "r1", "https://ex.com/bad", status=404)
    good_sid = str(good["source_id"])
    bad_sid = str(bad["source_id"])
    # Citing the usable source only -> ok.
    ok_answer = f"## Summary\nResult [{good_sid}] is reliable."
    assert not runtime.finalize_report(user, "r1", "research", ok_answer).startswith("Error")
    # Citing both -> blocked on the unavailable one.
    bad_answer = f"## Summary\nResult [{good_sid}] and [{bad_sid}] agree."
    result = runtime.finalize_report(user, "r1", "research", bad_answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert bad_sid in result
