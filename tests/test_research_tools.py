from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.config.settings import ResearchSettings
from corpclaw_lite.extensions.tools.builtin.research import (
    ResearchFetchSourceTool,
    ResearchFinalizeTool,
    ResearchListFactsTool,
    ResearchListSourcesTool,
    ResearchReadSourceTool,
    ResearchRuntime,
    ResearchSearchTool,
    ResearchStoreFactTool,
    build_research_tools,
    normalize_research_mode,
)
from corpclaw_lite.users.models import User


class FakeSearchTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "1. Example result\nURL: https://example.com/a"


class FakeFetchTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return (
            "url: https://example.com/a\n"
            "status: 200\n"
            "content-type: text/html\n"
            "size: 42\n"
            "---\n"
            "Example title\n"
            "Important paragraph with evidence."
        )


def _user() -> User:
    return User(id=42, telegram_id=42, name="Researcher", department="engineering")


def test_runtime_budgets_sources_facts_and_reports(tmp_path: Path) -> None:
    user = _user()
    settings = ResearchSettings(
        cache_ttl_hours=1,
        normal_search_waves=1,
        deep_search_waves=2,
        normal_max_sources=1,
        deep_max_sources=2,
        source_excerpt_chars=1200,
    )
    runtime = ResearchRuntime(settings=settings, workspace_base=tmp_path)
    runtime.initialize_run_mode(user, "run", "research", language="ru")

    assert normalize_research_mode("deep_research") == "deep_research"
    assert normalize_research_mode("anything else") == "research"

    assert runtime.reserve_search(user, "run", "research") is None
    assert "search budget exceeded" in (runtime.reserve_search(user, "run", "research") or "")
    assert runtime.reserve_search(user, "run", "deep_research") is None
    assert "search budget exceeded" in (runtime.reserve_search(user, "run", "deep_research") or "")

    source = runtime.store_source(
        user,
        "run",
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nTitle\nBody text with Alpha.",
    )
    assert source["title"] == "Title"
    assert runtime.find_source_by_url(user, "run", "https://example.com/a") == source

    source_id = str(source["source_id"])
    text = runtime.read_source_text(user, "run", source_id)
    assert text is not None
    assert runtime.source_excerpt(text, query="alpha", offset=0, max_chars=600).endswith("Alpha.")
    assert "Query not found" in runtime.source_excerpt(
        text, query="missing", offset=0, max_chars=600
    )

    fact_id = runtime.store_fact(
        user,
        "run",
        {
            "source_id": source_id,
            "fact": "Alpha is mentioned.",
            "evidence": "Body text with Alpha.",
            "confidence": "high",
            "relation": "supports",
        },
    )
    assert fact_id == 1
    assert "Alpha is mentioned" in runtime.format_facts(user, "run", max_facts=10)

    report = runtime.finalize_report(user, "run", "research", "Краткий ответ.")
    assert "## Использованные источники" in report
    assert "https://example.com/a" in report

    deep_report = runtime.finalize_report(user, "run", "deep_research", "")
    assert "## Краткий вывод" in deep_report
    assert "## Практические рекомендации" in deep_report


def test_runtime_ignores_invalid_json_and_cleans_expired_runs(tmp_path: Path) -> None:
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(cache_ttl_hours=1),
        workspace_base=tmp_path,
    )
    run_dir = runtime.run_dir(user, "old/run")
    (run_dir / "state.json").write_text("{broken", encoding="utf-8")
    (run_dir / "manifest.json").write_text('{"sources": []}', encoding="utf-8")

    assert runtime.reserve_fetch(user, "old/run", "research") is None
    assert runtime.list_sources(user, "old/run") == []

    old_time = time.time() - 7200
    os.utime(run_dir, (old_time, old_time))
    runtime.cleanup_user(user)
    assert not run_dir.exists()


@pytest.mark.asyncio()
async def test_research_tools_success_paths_and_budget_errors(tmp_path: Path) -> None:
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(
            normal_search_waves=1,
            normal_max_sources=1,
            deep_max_rereads=2,
            source_excerpt_chars=1000,
        ),
        workspace_base=tmp_path,
    )
    search_backend = FakeSearchTool()
    fetch_backend = FakeFetchTool()
    runtime.initialize_run_mode(user, "fetch", "research", language="ru")

    search_tool = ResearchSearchTool(runtime, search_backend)  # type: ignore[arg-type]
    assert await search_tool.execute(query="x") == (
        "Error: User context is required for research_search."
    )
    assert "required non-empty" in await search_tool.execute(user=user, query="")
    search_result = await search_tool.execute(user=user, query="corpclaw", run_id="tool")
    assert "Research note" in search_result
    assert search_backend.calls[0]["query"] == "corpclaw"
    assert "search budget exceeded" in await search_tool.execute(
        user=user, query="again", run_id="tool"
    )

    fetch_tool = ResearchFetchSourceTool(runtime, fetch_backend)  # type: ignore[arg-type]
    assert await fetch_tool.execute(url="https://example.com/a") == (
        "Error: User context is required for research_fetch_source."
    )
    assert "required non-empty" in await fetch_tool.execute(user=user, url="")
    fetch_result = await fetch_tool.execute(
        user=user,
        url="https://example.com/a",
        run_id="fetch",
        max_chars=900,
    )
    assert "Source cached:" in fetch_result
    assert "Example title" in fetch_result
    cached_result = await fetch_tool.execute(user=user, url="https://example.com/a", run_id="fetch")
    assert "Source cached:" in cached_result
    assert len(fetch_backend.calls) == 1
    assert "source budget exceeded" in await fetch_tool.execute(
        user=user, url="https://example.com/b", run_id="fetch"
    )

    source_id = str(runtime.list_sources(user, "fetch")[0]["source_id"])
    read_tool = ResearchReadSourceTool(runtime)
    assert await read_tool.execute(source_id=source_id) == (
        "Error: User context is required for research_read_source."
    )
    assert "required non-empty" in await read_tool.execute(user=user, source_id="")
    assert "not found" in await read_tool.execute(user=user, source_id="missing")
    read_result = await read_tool.execute(
        user=user,
        source_id=source_id,
        run_id="fetch",
        mode="deep_research",
        query="evidence",
        max_chars=900,
    )
    assert "Important paragraph with evidence" in read_result

    store_tool = ResearchStoreFactTool(runtime)
    assert await store_tool.execute(source_id=source_id) == (
        "Error: User context is required for research_store_fact."
    )
    assert "Unknown source_id" in await store_tool.execute(
        user=user,
        source_id="missing",
        fact="x",
        evidence="y",
    )
    assert "required non-empty" in await store_tool.execute(
        user=user,
        source_id=source_id,
        fact="",
        evidence="y",
        run_id="fetch",
    )
    stored = await store_tool.execute(
        user=user,
        source_id=source_id,
        fact="The page contains evidence.",
        evidence="Important paragraph with evidence.",
        confidence="invalid",
        relation="invalid",
        run_id="fetch",
    )
    assert stored == f"Stored research fact #1 from source {source_id}."

    list_tool = ResearchListFactsTool(runtime)
    assert await list_tool.execute(max_facts=1) == (
        "Error: User context is required for research_list_facts."
    )
    facts = await list_tool.execute(user=user, run_id="fetch", max_facts=1)
    assert "The page contains evidence." in facts
    assert "confidence=medium" in facts
    assert "relation=neutral" in facts

    finalize_tool = ResearchFinalizeTool(runtime)
    assert await finalize_tool.execute(answer="x") == (
        "Error: User context is required for research_finalize."
    )
    final = await finalize_tool.execute(user=user, run_id="fetch", answer="Ответ без ссылок.")
    assert "## Использованные источники" in final
    assert "https://example.com/a" in final

    tools = build_research_tools(runtime, search_backend, fetch_backend)  # type: ignore[arg-type]
    assert [tool.name for tool in tools] == [
        "research_search",
        "research_fetch_source",
        "research_read_source",
        "research_list_sources",
        "research_store_fact",
        "research_list_facts",
        "research_finalize",
    ]


@pytest.mark.asyncio()
async def test_research_search_uses_persisted_deep_research_mode(tmp_path: Path) -> None:
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(normal_search_waves=1, deep_search_waves=2),
        workspace_base=tmp_path,
    )
    search_backend = FakeSearchTool()
    search_tool = ResearchSearchTool(runtime, search_backend)  # type: ignore[arg-type]

    runtime.initialize_run_mode(user, "deep-run", "deep_research")

    first = await search_tool.execute(user=user, query="first", run_id="deep-run")
    second = await search_tool.execute(user=user, query="second", run_id="deep-run")
    third = await search_tool.execute(user=user, query="third", run_id="deep-run")

    assert "Research note" in first
    assert "Research note" in second
    assert "search budget exceeded" in third
    assert len(search_backend.calls) == 2


@pytest.mark.asyncio()
async def test_research_search_defaults_to_normal_budget_without_persisted_mode(
    tmp_path: Path,
) -> None:
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(normal_search_waves=1, deep_search_waves=2),
        workspace_base=tmp_path,
    )
    search_backend = FakeSearchTool()
    search_tool = ResearchSearchTool(runtime, search_backend)  # type: ignore[arg-type]

    first = await search_tool.execute(user=user, query="first", run_id="normal-run")
    second = await search_tool.execute(user=user, query="second", run_id="normal-run")

    assert "Research note" in first
    assert "search budget exceeded" in second
    assert len(search_backend.calls) == 1


# ── B-052: web-search resilience (refund / degraded / offline) ───────────────


class _UnavailableSearchTool:
    """Simulates WebSearchTool returning an infrastructure-unavailable error."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "Error: Web search unavailable (infrastructure): No results found."


@pytest.mark.asyncio()
async def test_research_search_infrastructure_failure_does_not_charge_budget(
    tmp_path: Path,
) -> None:
    """An infrastructure 'unavailable' error does not consume the search budget (B-052)."""
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(normal_search_waves=1, deep_search_waves=3),
        workspace_base=tmp_path,
    )
    backend = _UnavailableSearchTool()
    search_tool = ResearchSearchTool(runtime, backend)  # type: ignore[arg-type]

    # Three infrastructure failures — none should charge the budget.
    for _ in range(3):
        await search_tool.execute(user=user, query="q", run_id="run")

    state = runtime._read_state(runtime.run_dir(user, "run"))
    assert state["search_calls"] == 0  # budget NOT charged on infrastructure failure
    assert state["search_failures"] == 3


@pytest.mark.asyncio()
async def test_research_search_cascading_failure_returns_degraded_message(
    tmp_path: Path,
) -> None:
    """After the degraded threshold is reached, the model gets a steer-to-offline message."""
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(normal_search_waves=5, deep_search_waves=5),
        workspace_base=tmp_path,
    )
    backend = _UnavailableSearchTool()
    search_tool = ResearchSearchTool(runtime, backend)  # type: ignore[arg-type]

    first = await search_tool.execute(user=user, query="q1", run_id="run")
    second = await search_tool.execute(user=user, query="q2", run_id="run")

    # First failure: plain unavailable error, budget not charged.
    assert "unavailable" in first
    # Second failure crosses the threshold → degraded message steering the model offline.
    assert "Do NOT retry research_search" in second
    assert "based on model knowledge" in second
    assert runtime.is_web_search_degraded(user, "run") is True


@pytest.mark.asyncio()
async def test_research_search_budget_check_runs_before_request(
    tmp_path: Path,
) -> None:
    """An over-budget call short-circuits without hitting the search backend (B-052)."""
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(normal_search_waves=1, deep_search_waves=1),
        workspace_base=tmp_path,
    )
    backend = FakeSearchTool()
    search_tool = ResearchSearchTool(runtime, backend)  # type: ignore[arg-type]

    first = await search_tool.execute(user=user, query="first", run_id="run")
    second = await search_tool.execute(user=user, query="second", run_id="run")

    assert "Research note" in first
    assert "search budget exceeded" in second
    # Second call must NOT reach the backend (cheap short-circuit).
    assert len(backend.calls) == 1


def test_finalize_report_prepends_offline_banner_when_degraded(tmp_path: Path) -> None:
    """When web_search_degraded is set, finalize_report prepends an offline banner."""
    user = _user()
    runtime = ResearchRuntime(
        settings=ResearchSettings(normal_search_waves=1, deep_search_waves=3),
        workspace_base=tmp_path,
    )
    runtime.initialize_run_mode(user, "run", "research", language="en")
    # Simulate the degraded flag being set by repeated infrastructure failures.
    runtime.mark_search_failure(user, "run")
    runtime.mark_search_failure(user, "run")  # crosses threshold → degraded
    assert runtime.is_web_search_degraded(user, "run") is True

    report = runtime.finalize_report(user, "run", "research", "## Summary\nKnowledge answer.")
    assert "web search was unavailable" in report.casefold()
    assert "model knowledge" in report.casefold()


def test_finalize_report_no_offline_banner_when_search_healthy(tmp_path: Path) -> None:
    """No offline banner when web search was not degraded."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    runtime.initialize_run_mode(user, "run", "research", language="en")
    report = runtime.finalize_report(user, "run", "research", "## Summary\nNormal answer.")
    assert "web search was unavailable" not in report.casefold()


# ── B-053: research_list_sources + source-anchor in store_fact ───────────────


def test_format_sources_list_empty(tmp_path: Path) -> None:
    """No sources cached → a clear 'no sources' message."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    out = runtime.format_sources_list(user, "run", 50)
    assert out == "No research sources fetched yet."


def test_format_sources_list_returns_ids_and_titles(tmp_path: Path) -> None:
    """Cached sources are listed with exact source_id, title, url, status."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    src = runtime.store_source(
        user,
        "run",
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nAlpha Title\nBody.",
    )
    out = runtime.format_sources_list(user, "run", 50)
    assert src["source_id"] in out
    assert "Alpha Title" in out
    assert "https://example.com/a" in out
    assert "200" in out
    assert "use these exact source_id values" in out.casefold()


@pytest.mark.asyncio()
async def test_research_list_sources_tool_sets_flag_and_returns_ids(
    tmp_path: Path,
) -> None:
    """research_list_sources returns the cached sources and sets list_sources_called."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    runtime.store_source(
        user,
        "run",
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nTitle A\nBody.",
    )
    tool = ResearchListSourcesTool(runtime)
    assert runtime.is_list_sources_called(user, "run") is False
    out = await tool.execute(user=user, run_id="run")
    assert "Title A" in out
    assert runtime.is_list_sources_called(user, "run") is True


@pytest.mark.asyncio()
async def test_store_fact_unknown_id_without_list_sources_steers_to_list(
    tmp_path: Path,
) -> None:
    """When list_sources was NOT called and source_id is unknown, the error tells the
    model to call research_list_sources first AND includes the real IDs."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    runtime.store_source(
        user,
        "run",
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nReal Title\nBody.",
    )
    store = ResearchStoreFactTool(runtime)
    res = await store.execute(
        user=user,
        run_id="run",
        source_id="deadbeefdead",
        fact="f",
        evidence="e",
    )
    assert res.startswith("Error")
    assert "research_list_sources first" in res.casefold()
    # The real cached source_id must be surfaced so the model can self-correct.
    real_id = runtime.list_sources(user, "run")[0]["source_id"]
    assert real_id in res


@pytest.mark.asyncio()
async def test_store_fact_unknown_id_with_list_sources_shows_real_ids(
    tmp_path: Path,
) -> None:
    """When list_sources WAS called and source_id is still unknown, the error shows the
    real IDs (softer nudge, not the 'call list_sources first' gate)."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    runtime.store_source(
        user,
        "run",
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nReal Title\nBody.",
    )
    runtime.mark_list_sources_called(user, "run")
    store = ResearchStoreFactTool(runtime)
    res = await store.execute(
        user=user,
        run_id="run",
        source_id="deadbeefdead",
        fact="f",
        evidence="e",
    )
    assert res.startswith("Error")
    assert "Do not invent" in res
    real_id = runtime.list_sources(user, "run")[0]["source_id"]
    assert real_id in res


@pytest.mark.asyncio()
async def test_store_fact_valid_id_is_not_blocked_by_flag(tmp_path: Path) -> None:
    """A valid source_id works even when list_sources was never called — the flag only
    affects the Unknown-source_id error path, not the happy path (fetch→store)."""
    user = _user()
    runtime = ResearchRuntime(workspace_base=tmp_path)
    src = runtime.store_source(
        user,
        "run",
        "https://example.com/a",
        "url: https://example.com/a\nstatus: 200\nsize: 10\n---\nTitle\nBody.",
    )
    store = ResearchStoreFactTool(runtime)
    res = await store.execute(
        user=user,
        run_id="run",
        source_id=src["source_id"],
        fact="A fact",
        evidence="An excerpt",
    )
    assert res.startswith("Stored research fact")
    assert runtime.is_list_sources_called(user, "run") is False
