from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from corpclaw_lite.config.settings import ResearchSettings
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = [
    "ResearchFetchSourceTool",
    "ResearchFinalizeTool",
    "ResearchListFactsTool",
    "ResearchReadSourceTool",
    "ResearchRuntime",
    "ResearchSearchTool",
    "ResearchStoreFactTool",
    "build_research_tools",
    "normalize_research_mode",
]

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool, WebSearchTool
    from corpclaw_lite.users.models import User

ResearchMode = Literal["research", "deep_research"]

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_URL_RE = re.compile(r"https?://\S+")


def normalize_research_mode(value: Any) -> ResearchMode:
    """Normalize a tool/user supplied mode value."""
    return "deep_research" if str(value or "").strip() == "deep_research" else "research"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ResearchRuntime:
    """Per-run source cache and fact store for research-agent.

    The store is intentionally temporary and workspace-scoped. It keeps fetched
    pages and extracted facts out of long-term user memory while giving the
    local model a compact working set for synthesis.
    """

    def __init__(
        self,
        settings: ResearchSettings | None = None,
        workspace_base: Path | None = None,
    ) -> None:
        self._settings = settings or ResearchSettings()
        self._workspace_base = (
            Path(workspace_base) if workspace_base else PROJECT_ROOT / "workspaces"
        )

    @property
    def settings(self) -> ResearchSettings:
        return self._settings

    def run_dir(self, user: User, run_id: str | None) -> Path:
        self.cleanup_user(user)
        user_key = user.workspace_key()
        safe_run_id = _SAFE_ID_RE.sub("_", run_id or "unknown")[:80] or "unknown"
        path = self._workspace_base / f"user_{user_key}" / ".research" / safe_run_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "sources").mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_user(self, user: User) -> None:
        ttl_seconds = max(1, self._settings.cache_ttl_hours) * 3600
        cutoff = time.time() - ttl_seconds
        user_key = user.workspace_key()
        root = self._workspace_base / f"user_{user_key}" / ".research"
        if not root.exists() or not root.is_dir():
            return
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    def reserve_search(self, user: User, run_id: str | None, mode: ResearchMode) -> str | None:
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        self._upgrade_mode(state, mode)
        limit = (
            self._settings.deep_search_waves
            if state["mode"] == "deep_research"
            else self._settings.normal_search_waves
        )
        used = _as_int(state.get("search_calls"), 0)
        if used >= limit:
            return f"Error: Research search budget exceeded ({used}/{limit})."
        state["search_calls"] = used + 1
        self._write_json(run_dir / "state.json", state)
        return None

    def reserve_fetch(self, user: User, run_id: str | None, mode: ResearchMode) -> str | None:
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        self._upgrade_mode(state, mode)
        limit = (
            self._settings.deep_max_sources
            if state["mode"] == "deep_research"
            else self._settings.normal_max_sources
        )
        used = len(self.list_sources(user, run_id))
        if used >= limit:
            return f"Error: Research source budget exceeded ({used}/{limit})."
        self._write_json(run_dir / "state.json", state)
        return None

    def reserve_reread(self, user: User, run_id: str | None, mode: ResearchMode) -> str | None:
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        self._upgrade_mode(state, mode)
        limit = (
            self._settings.deep_max_rereads
            if state["mode"] == "deep_research"
            else self._settings.normal_max_rereads
        )
        used = _as_int(state.get("rereads"), 0)
        if used >= limit:
            return f"Error: Research reread budget exceeded ({used}/{limit})."
        state["rereads"] = used + 1
        self._write_json(run_dir / "state.json", state)
        return None

    def find_source_by_url(
        self,
        user: User,
        run_id: str | None,
        url: str,
    ) -> dict[str, Any] | None:
        for source in self.list_sources(user, run_id):
            if str(source.get("url") or "") == url:
                return source
        return None

    def store_source(
        self,
        user: User,
        run_id: str | None,
        url: str,
        fetch_output: str,
    ) -> dict[str, Any]:
        run_dir = self.run_dir(user, run_id)
        metadata, body = self._split_fetch_output(fetch_output)
        source_id = self._source_id(url)
        manifest = self._read_manifest(run_dir)
        sources = self._manifest_sources(manifest)
        suffix = 2
        while source_id in sources and str(sources[source_id].get("url") or "") != url:
            source_id = f"{self._source_id(url)}-{suffix}"
            suffix += 1

        title = str(metadata.get("title") or "").strip() or self._infer_title(body)
        relative_path = f"sources/{source_id}.txt"
        source_path = run_dir / relative_path
        source_path.write_text(body, encoding="utf-8")

        source = {
            "source_id": source_id,
            "url": url,
            "final_url": metadata.get("url") or url,
            "title": title,
            "status": metadata.get("status") or "",
            "content_type": metadata.get("content-type") or "",
            "size": metadata.get("size") or "",
            "path": relative_path,
            "fetched_at": _now_iso(),
        }
        sources[source_id] = source
        manifest["sources"] = sources
        self._write_json(run_dir / "manifest.json", manifest)
        return source

    def read_source_text(
        self,
        user: User,
        run_id: str | None,
        source_id: str,
    ) -> str | None:
        run_dir = self.run_dir(user, run_id)
        source = self.get_source(user, run_id, source_id)
        if source is None:
            return None
        rel_path = str(source.get("path") or "")
        source_path = (run_dir / rel_path).resolve()
        if run_dir.resolve() not in source_path.parents:
            return None
        if not source_path.exists() or not source_path.is_file():
            return None
        return source_path.read_text(encoding="utf-8", errors="replace")

    def get_source(
        self,
        user: User,
        run_id: str | None,
        source_id: str,
    ) -> dict[str, Any] | None:
        for source in self.list_sources(user, run_id):
            if str(source.get("source_id") or "") == source_id:
                return source
        return None

    def list_sources(self, user: User, run_id: str | None) -> list[dict[str, Any]]:
        run_dir = self.run_dir(user, run_id)
        manifest = self._read_manifest(run_dir)
        sources = self._manifest_sources(manifest)
        return list(sources.values())

    def store_fact(
        self,
        user: User,
        run_id: str | None,
        fact: dict[str, Any],
    ) -> int:
        run_dir = self.run_dir(user, run_id)
        facts = self.list_facts(user, run_id)
        fact_number = len(facts) + 1
        fact["fact_id"] = fact_number
        fact["stored_at"] = _now_iso()
        facts_path = run_dir / "facts.jsonl"
        with facts_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fact, ensure_ascii=False) + "\n")
        return fact_number

    def list_facts(self, user: User, run_id: str | None) -> list[dict[str, Any]]:
        run_dir = self.run_dir(user, run_id)
        facts_path = run_dir / "facts.jsonl"
        if not facts_path.exists():
            return []
        facts: list[dict[str, Any]] = []
        for line in facts_path.read_text(encoding="utf-8").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                facts.append(cast(dict[str, Any], raw))
        return facts

    def source_excerpt(self, text: str, *, query: str | None, offset: int, max_chars: int) -> str:
        limit = max(500, min(max_chars, self._settings.source_excerpt_chars * 2))
        if query:
            position = text.casefold().find(query.casefold())
            if position >= 0:
                start = max(0, position - limit // 3)
                return text[start : start + limit]
            return f"Query not found in cached source: {query}\n\n" + text[:limit]
        start = max(0, offset)
        return text[start : start + limit]

    def format_source_summary(self, source: dict[str, Any], text: str, max_chars: int) -> str:
        source_id = str(source.get("source_id") or "")
        url = str(source.get("url") or "")
        title = str(source.get("title") or "").strip() or "(untitled)"
        excerpt = text[: max(500, min(max_chars, self._settings.source_excerpt_chars * 2))]
        return (
            f"Source cached: {source_id}\n"
            f"Title: {title}\n"
            f"URL: {url}\n"
            f"Status: {source.get('status') or ''}\n"
            f"Size: {source.get('size') or ''}\n"
            "---\n"
            f"{excerpt}"
        )

    def format_facts(self, user: User, run_id: str | None, max_facts: int) -> str:
        facts = self.list_facts(user, run_id)
        if not facts:
            return "No research facts stored yet."
        lines = ["Stored research facts:", "---"]
        for fact in facts[: max(1, max_facts)]:
            lines.append(
                "[{fact_id}] {fact} | source={source_id} | confidence={confidence} | "
                "relation={relation}".format(
                    fact_id=fact.get("fact_id", "?"),
                    fact=fact.get("fact", ""),
                    source_id=fact.get("source_id", ""),
                    confidence=fact.get("confidence", ""),
                    relation=fact.get("relation", ""),
                )
            )
            evidence = str(fact.get("evidence") or "").strip()
            if evidence:
                lines.append(f"Evidence: {evidence[:500]}")
        return "\n".join(lines)

    def finalize_report(
        self,
        user: User,
        run_id: str | None,
        mode: ResearchMode,
        answer: str,
    ) -> str:
        facts = self.list_facts(user, run_id)
        sources = self.list_sources(user, run_id)
        report = answer.strip() or self._build_report(mode, facts, sources)
        lowered = report.casefold()
        if "использованные источники" not in lowered and "sources" not in lowered:
            report = (
                report.rstrip()
                + "\n\n## Использованные источники\n"
                + self._sources_markdown(sources)
            )
        elif not _URL_RE.search(report):
            report = report.rstrip() + "\n\n" + self._sources_markdown(sources)
        if not facts and "огранич" not in lowered and "limitation" not in lowered:
            report = (
                report.rstrip()
                + "\n\n## Ограничения\n"
                + "- Не удалось зафиксировать структурированные факты через research_store_fact."
            )
        return report.strip()

    def _read_state(self, run_dir: Path) -> dict[str, Any]:
        state = self._read_json(run_dir / "state.json", {"mode": "research"})
        mode = normalize_research_mode(state.get("mode"))
        state["mode"] = mode
        state.setdefault("search_calls", 0)
        state.setdefault("rereads", 0)
        return state

    def _upgrade_mode(self, state: dict[str, Any], mode: ResearchMode) -> None:
        if mode == "deep_research" or state.get("mode") == "deep_research":
            state["mode"] = "deep_research"
        else:
            state["mode"] = "research"

    def _read_manifest(self, run_dir: Path) -> dict[str, Any]:
        manifest = self._read_json(run_dir / "manifest.json", {"sources": {}})
        manifest.setdefault("sources", {})
        return manifest

    def _read_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return dict(default)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return dict(default)
        return cast(dict[str, Any], raw) if isinstance(raw, dict) else dict(default)

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _manifest_sources(self, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = manifest.get("sources")
        if not isinstance(raw, dict):
            return {}
        sources: dict[str, dict[str, Any]] = {}
        raw_sources = cast(dict[object, object], raw)
        for key_obj, value_obj in raw_sources.items():
            if isinstance(value_obj, dict):
                sources[str(key_obj)] = cast(dict[str, Any], value_obj)
        return sources

    def _split_fetch_output(self, fetch_output: str) -> tuple[dict[str, str], str]:
        header, sep, body = fetch_output.partition("---\n")
        if not sep:
            return {}, fetch_output
        metadata: dict[str, str] = {}
        for line in header.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            metadata[key.strip().casefold()] = value.strip()
        return metadata, body.strip()

    def _source_id(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]

    def _infer_title(self, body: str) -> str:
        for line in body.splitlines():
            title = line.strip()
            if title:
                return title[:200]
        return ""

    def _build_report(
        self,
        mode: ResearchMode,
        facts: list[dict[str, Any]],
        sources: list[dict[str, Any]],
    ) -> str:
        fact_lines = self._facts_markdown(facts)
        sources_block = self._sources_markdown(sources)
        if mode == "deep_research":
            return (
                "## Executive summary\n"
                "См. ключевые выводы ниже.\n\n"
                "## Методика исследования\n"
                f"- Проанализировано источников: {len(sources)}.\n"
                f"- Зафиксировано фактов: {len(facts)}.\n\n"
                "## Ключевые выводы\n"
                f"{fact_lines}\n\n"
                "## Факты и подтверждения\n"
                f"{fact_lines}\n\n"
                "## Противоречия и неопределённости\n"
                "- Явные противоречия не выделены в сохранённых фактах.\n\n"
                "## Гипотезы / пробелы\n"
                "- Дополнительные гипотезы не зафиксированы.\n\n"
                "## Практические рекомендации\n"
                "- Используйте выводы выше с учётом ограничений источников.\n\n"
                "## Использованные источники\n"
                f"{sources_block}"
            )
        return (
            "## Краткий вывод\n"
            "См. ключевые факты ниже.\n\n"
            "## Ключевые факты\n"
            f"{fact_lines}\n\n"
            "## Что говорят источники\n"
            f"{fact_lines}\n\n"
            "## Ограничения\n"
            "- Ответ построен по сохранённым фактам research-agent.\n\n"
            "## Использованные источники\n"
            f"{sources_block}"
        )

    def _facts_markdown(self, facts: list[dict[str, Any]]) -> str:
        if not facts:
            return "- Структурированные факты не сохранены."
        lines: list[str] = []
        for fact in facts:
            source_id = str(fact.get("source_id") or "")
            fact_text = str(fact.get("fact") or "").strip()
            evidence = str(fact.get("evidence") or "").strip()
            confidence = str(fact.get("confidence") or "medium")
            relation = str(fact.get("relation") or "neutral")
            line = f"- {fact_text} [{source_id}; {confidence}; {relation}]"
            if evidence:
                line += f" Evidence: {evidence[:240]}"
            lines.append(line)
        return "\n".join(lines)

    def _sources_markdown(self, sources: list[dict[str, Any]]) -> str:
        if not sources:
            return "- Источники не были успешно загружены."
        lines: list[str] = []
        for source in sources:
            source_id = str(source.get("source_id") or "")
            url = str(source.get("url") or "")
            title = str(source.get("title") or "").strip() or url
            lines.append(f"- [{source_id}] {title}: {url}")
        return "\n".join(lines)


class ResearchSearchTool(Tool):
    """Search the web inside a budgeted research run."""

    name = "research_search"
    description = (
        "Search the web for research candidates within the current research budget. "
        "Search snippets are not sufficient for a final answer; fetch relevant URLs next."
    )
    params = [
        ToolParam(name="query", type="string", description="Search query"),
        ToolParam(
            name="mode",
            type="string",
            description="Research mode: research or deep_research",
            required=False,
            enum=["research", "deep_research"],
        ),
        ToolParam(
            name="max_results",
            type="integer",
            description="Maximum number of search results",
            required=False,
        ),
        ToolParam(
            name="site", type="string", description="Optional domain restriction", required=False
        ),
        ToolParam(name="region", type="string", description="Search region code", required=False),
        ToolParam(
            name="timelimit",
            type="string",
            description="Optional time limit: d, w, m, y",
            required=False,
            enum=["d", "w", "m", "y"],
        ),
    ]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False

    def __init__(self, runtime: ResearchRuntime, search_tool: WebSearchTool) -> None:
        self._runtime = runtime
        self._search_tool = search_tool

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_search."
        query = kwargs.get("query")
        if not isinstance(query, str) or not query.strip():
            return "Error: 'query' is a required non-empty string parameter."
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        mode = normalize_research_mode(kwargs.get("mode"))
        budget_error = self._runtime.reserve_search(user, run_id, mode)
        if budget_error:
            return budget_error

        result = await self._search_tool.execute(
            query=query,
            max_results=kwargs.get("max_results", 5),
            site=kwargs.get("site"),
            region=kwargs.get("region", "wt-wt"),
            timelimit=kwargs.get("timelimit"),
        )
        if result.startswith("Error"):
            return result
        return (
            result
            + "\n---\nResearch note: choose relevant URLs and call research_fetch_source before "
            "drawing conclusions."
        )


class ResearchFetchSourceTool(Tool):
    """Fetch, normalize, and cache a source page for a research run."""

    name = "research_fetch_source"
    description = (
        "Fetch a URL once, cache its normalized text in the current research run, and return "
        "a source_id plus excerpt for fact extraction."
    )
    params = [
        ToolParam(name="url", type="string", description="URL to fetch and cache"),
        ToolParam(
            name="mode",
            type="string",
            description="Research mode: research or deep_research",
            required=False,
            enum=["research", "deep_research"],
        ),
        ToolParam(
            name="timeout",
            type="integer",
            description="Request timeout seconds",
            required=False,
        ),
        ToolParam(name="max_chars", type="integer", description="Excerpt size", required=False),
    ]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False

    def __init__(self, runtime: ResearchRuntime, fetch_tool: WebFetchTool) -> None:
        self._runtime = runtime
        self._fetch_tool = fetch_tool

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_fetch_source."
        url = kwargs.get("url")
        if not isinstance(url, str) or not url.strip():
            return "Error: 'url' is a required non-empty string parameter."
        url = url.strip()
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        mode = normalize_research_mode(kwargs.get("mode"))
        max_chars = _as_int(kwargs.get("max_chars"), self._runtime.settings.source_excerpt_chars)

        existing = self._runtime.find_source_by_url(user, run_id, url)
        if existing is not None:
            source_id = str(existing.get("source_id") or "")
            text = self._runtime.read_source_text(user, run_id, source_id)
            if text is None:
                return "Error: Cached source metadata exists but cached text is missing."
            return self._runtime.format_source_summary(existing, text, max_chars)

        budget_error = self._runtime.reserve_fetch(user, run_id, mode)
        if budget_error:
            return budget_error

        result = await self._fetch_tool.execute(
            url=url,
            timeout=kwargs.get("timeout"),
            format="text",
        )
        if result.startswith("Error"):
            return result
        source = self._runtime.store_source(user, run_id, url, result)
        source_id = str(source.get("source_id") or "")
        text = self._runtime.read_source_text(user, run_id, source_id) or ""
        return self._runtime.format_source_summary(source, text, max_chars)


class ResearchReadSourceTool(Tool):
    """Read a previously cached source without fetching it again."""

    name = "research_read_source"
    description = "Read a cached source by source_id. Intended for deep_research rereads."
    params = [
        ToolParam(name="source_id", type="string", description="Cached source ID"),
        ToolParam(
            name="mode",
            type="string",
            description="Research mode: research or deep_research",
            required=False,
            enum=["research", "deep_research"],
        ),
        ToolParam(
            name="query", type="string", description="Optional text to locate", required=False
        ),
        ToolParam(name="offset", type="integer", description="Character offset", required=False),
        ToolParam(
            name="max_chars", type="integer", description="Maximum returned chars", required=False
        ),
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_read_source."
        source_id = kwargs.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            return "Error: 'source_id' is a required non-empty string parameter."
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        mode = normalize_research_mode(kwargs.get("mode"))
        text = self._runtime.read_source_text(user, run_id, source_id.strip())
        if text is None:
            return f"Error: Cached source '{source_id}' not found."
        budget_error = self._runtime.reserve_reread(user, run_id, mode)
        if budget_error:
            return budget_error
        source = self._runtime.get_source(user, run_id, source_id.strip()) or {}
        excerpt = self._runtime.source_excerpt(
            text,
            query=kwargs.get("query") if isinstance(kwargs.get("query"), str) else None,
            offset=_as_int(kwargs.get("offset"), 0),
            max_chars=_as_int(kwargs.get("max_chars"), self._runtime.settings.source_excerpt_chars),
        )
        return (
            f"Cached source: {source_id}\n"
            f"Title: {source.get('title') or ''}\n"
            f"URL: {source.get('url') or ''}\n"
            "---\n"
            f"{excerpt}"
        )


class ResearchStoreFactTool(Tool):
    """Store an atomic evidence-backed research fact."""

    name = "research_store_fact"
    description = (
        "Store one atomic fact extracted from a cached source with evidence and confidence."
    )
    params = [
        ToolParam(name="source_id", type="string", description="Source ID supporting the fact"),
        ToolParam(name="fact", type="string", description="One atomic fact or claim"),
        ToolParam(name="evidence", type="string", description="Short supporting excerpt"),
        ToolParam(
            name="confidence",
            type="string",
            description="Confidence level",
            required=False,
            enum=["low", "medium", "high"],
        ),
        ToolParam(
            name="relation",
            type="string",
            description="How this fact relates to the research question",
            required=False,
            enum=["supports", "contradicts", "neutral"],
        ),
        ToolParam(name="notes", type="string", description="Optional notes", required=False),
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_store_fact."
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        source_id = kwargs.get("source_id")
        fact_text = kwargs.get("fact")
        evidence = kwargs.get("evidence")
        if not isinstance(source_id, str) or not source_id.strip():
            return "Error: 'source_id' is a required non-empty string parameter."
        if self._runtime.get_source(user, run_id, source_id.strip()) is None:
            return f"Error: Unknown source_id '{source_id}'. Fetch the source first."
        if not isinstance(fact_text, str) or not fact_text.strip():
            return "Error: 'fact' is a required non-empty string parameter."
        if not isinstance(evidence, str) or not evidence.strip():
            return "Error: 'evidence' is a required non-empty string parameter."
        confidence = str(kwargs.get("confidence") or "medium")
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        relation = str(kwargs.get("relation") or "neutral")
        if relation not in {"supports", "contradicts", "neutral"}:
            relation = "neutral"
        fact_id = self._runtime.store_fact(
            user,
            run_id,
            {
                "source_id": source_id.strip(),
                "fact": fact_text.strip(),
                "evidence": evidence.strip(),
                "confidence": confidence,
                "relation": relation,
                "notes": str(kwargs.get("notes") or "").strip(),
            },
        )
        return f"Stored research fact #{fact_id} from source {source_id.strip()}."


class ResearchListFactsTool(Tool):
    """List stored research facts for synthesis."""

    name = "research_list_facts"
    description = (
        "List facts stored during this research run for synthesis and contradiction checks."
    )
    params = [
        ToolParam(
            name="max_facts",
            type="integer",
            description="Maximum facts to return",
            required=False,
        )
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_list_facts."
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        return self._runtime.format_facts(user, run_id, _as_int(kwargs.get("max_facts"), 50))


class ResearchFinalizeTool(Tool):
    """Finalize a research report and return it directly."""

    name = "research_finalize"
    description = (
        "Finalize the complete user-facing research report. Include the full structured answer "
        "in 'answer'; this tool validates and appends source URLs if needed."
    )
    params = [
        ToolParam(
            name="mode",
            type="string",
            description="Research mode: research or deep_research",
            required=False,
            enum=["research", "deep_research"],
        ),
        ToolParam(
            name="answer",
            type="string",
            description="Complete final Markdown research report",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False
    terminal = True

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_finalize."
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        mode = normalize_research_mode(kwargs.get("mode"))
        raw_answer = kwargs.get("answer")
        answer: str = raw_answer if isinstance(raw_answer, str) else ""
        return self._runtime.finalize_report(user, run_id, mode, answer)


def build_research_tools(
    runtime: ResearchRuntime,
    search_tool: WebSearchTool,
    fetch_tool: WebFetchTool,
) -> list[Tool]:
    return [
        ResearchSearchTool(runtime, search_tool),
        ResearchFetchSourceTool(runtime, fetch_tool),
        ResearchReadSourceTool(runtime),
        ResearchStoreFactTool(runtime),
        ResearchListFactsTool(runtime),
        ResearchFinalizeTool(runtime),
    ]
