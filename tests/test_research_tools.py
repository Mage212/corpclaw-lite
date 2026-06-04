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
    assert "## Executive summary" in deep_report
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
        "research_store_fact",
        "research_list_facts",
        "research_finalize",
    ]
