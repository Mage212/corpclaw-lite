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
from corpclaw_lite.logging.trace import log_event
from corpclaw_lite.paths import PROJECT_ROOT

__all__ = [
    "ResearchFetchSourceTool",
    "ResearchFinalizeTool",
    "ResearchLanguage",
    "ResearchListFactsTool",
    "ResearchReadSourceTool",
    "ResearchRuntime",
    "ResearchSearchTool",
    "ResearchStoreFactTool",
    "build_research_tools",
    "detect_language",
    "normalize_research_mode",
]

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool, WebSearchTool
    from corpclaw_lite.users.models import User

ResearchMode = Literal["research", "deep_research"]
ResearchLanguage = Literal["ru", "en"]

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_URL_RE = re.compile(r"https?://\S+")
# Cited source IDs look like "[abcdef123456]" or "[abcdef123456-2]" (12 hex + optional suffix).
_CITED_SOURCE_ID_RE = re.compile(r"\[\s*([0-9a-f]{12}(?:-\d+)?)\b", re.IGNORECASE)
# Count assertions like "проанализировано 10 источников" / "identified 8 sources".
_COUNT_ASSERTION_RE = re.compile(
    r"(?:проанализировано|изучено|исследовано|найдено|identified|analyzed|reviewed|found)\s+"
    r"(?P<n>\d+)\s+(?P<word>источник(?:а|ов)?|source)s?",
    re.IGNORECASE,
)
# After this many failed finalize retries, the next call returns a deterministic skeleton
# instead of another Error string (prevents LLM finalize loops; coexists with ProgressGuard).
_FINALIZE_MAX_ATTEMPTS = 2


# B-052: after this many consecutive web-search infrastructure failures, the run is
# considered to have web search degraded, and the agent is steered toward a
# knowledge-based answer with an explicit offline banner.
_WEB_SEARCH_DEGRADED_THRESHOLD = 2


def _is_usable_status(status: object) -> bool:
    """Whether a stored source status represents a usable (HTTP 2xx) response.

    Mirrors the ``200 <= status_code < 300`` gate in ``web.py:_fetch`` (B-054-1)
    so that any source the filter let through counts as usable here, and any
    legacy/sourceless entry (empty or non-numeric status) does not.
    """
    try:
        code = int(str(status).strip())
    except (TypeError, ValueError):
        return False
    return 200 <= code < 300


# Returned to the model once web search is deemed unavailable. Tells the model to stop
# trying to search/fetch and to not invent URLs.
_WEB_SEARCH_DEGRADED_MESSAGE = (
    "Web search is unavailable (infrastructure failure after retries). "
    "Do NOT retry research_search. Do NOT invent or guess URLs. Either finalize with "
    "already-fetched sources, or if no sources were fetched, write a detailed answer "
    "from your own knowledge. You MUST state in the report that web search was "
    "unavailable and the answer is based on model knowledge only."
)


def normalize_research_mode(value: Any) -> ResearchMode:
    """Normalize a tool/user supplied mode value."""
    return "deep_research" if str(value or "").strip() == "deep_research" else "research"


def _cyrillic_ratio(text: str) -> float:
    """Fraction of Cyrillic letters among alphabetic characters in *text*."""
    if not text:
        return 0.0
    cyrillic = 0
    total = 0
    for ch in text:
        if ch.isalpha():
            total += 1
            if "Ѐ" <= ch <= "ӿ":  # Cyrillic block (incl. ё/Ё)
                cyrillic += 1
    return cyrillic / total if total else 0.0


def detect_language(text: str) -> ResearchLanguage:
    """Heuristic target language for a research task.

    Returns ``"ru"`` when more than 30% of alphabetic characters are Cyrillic,
    otherwise ``"en"``. Covers the RU+EN user base without extra dependencies.
    """
    return "ru" if _cyrillic_ratio(text) > 0.3 else "en"


_REPORT_STRINGS: dict[ResearchLanguage, dict[str, str]] = {
    "ru": {
        "no_facts": "- Структурированные факты не сохранены.",
        "no_sources": "- Источники не были успешно загружены.",
        "sources_section": "## Использованные источники",
        "limitations_section": "## Ограничения",
        "limitations_no_facts": (
            "- Не удалось зафиксировать структурированные факты через research_store_fact."
        ),
        # B-052: prepended when web search was unavailable for the whole run.
        "offline_banner": (
            "⚠️ Веб-поиск был недоступен во время исследования. "
            "Ответ основан на знаниях модели без веб-источников."
        ),
        # B-045: honest marker for a research run that hit the wall-clock limit before
        # the model could synthesize. The body below this banner is stored facts only.
        "interrupted_banner": (
            "⚠️ Исследование прервано по лимиту времени. Ниже приведены собранные факты "
            "без синтеза и выводов — это не готовый отчёт."
        ),
        "interrupted_limitation": (
            "- Синтез не выполнен: исследование прервано до research_finalize."
        ),
    },
    "en": {
        "no_facts": "- No structured facts stored.",
        "no_sources": "- No sources were successfully fetched.",
        "sources_section": "## Sources",
        "limitations_section": "## Limitations",
        "limitations_no_facts": ("- Could not record structured facts via research_store_fact."),
        "offline_banner": (
            "⚠️ Web search was unavailable during this research. "
            "The answer is based on model knowledge without web sources."
        ),
        "interrupted_banner": (
            "⚠️ Research interrupted by the time limit. The facts below were gathered but "
            "not synthesized — this is not a finished report."
        ),
        "interrupted_limitation": (
            "- No synthesis: research was interrupted before research_finalize."
        ),
    },
}


def _report_strings(language: ResearchLanguage) -> dict[str, str]:
    return _REPORT_STRINGS[language if language in ("ru", "en") else "en"]


# Report skeleton templates keyed by (mode, language). Placeholders:
#   {n_sources}, {n_facts}, {facts} (brief, no evidence), {evidence} (full, with
#   evidence excerpt), {sources}, {sources_section}.
# B-048: deep_research previously rendered {facts} twice (Key findings + Facts and
# evidence). The second section now uses {evidence} so the two sections differ and
# the body is not duplicated.
_REPORT_TEMPLATES: dict[tuple[ResearchMode, ResearchLanguage], str] = {
    ("deep_research", "ru"): (
        "## Краткий вывод\n"
        "См. ключевые выводы ниже.\n\n"
        "## Методология исследования\n"
        "- Проанализировано источников: {n_sources}.\n"
        "- Зафиксировано фактов: {n_facts}.\n\n"
        "## Ключевые выводы\n"
        "{facts}\n\n"
        "## Факты и подтверждения\n"
        "{evidence}\n\n"
        "## Противоречия и неопределённости\n"
        "- Явные противоречия не выделены в сохранённых фактах.\n\n"
        "## Гипотезы / пробелы\n"
        "- Дополнительные гипотезы не зафиксированы.\n\n"
        "## Практические рекомендации\n"
        "- Используйте выводы выше с учётом ограничений источников.\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
    ("deep_research", "en"): (
        "## Executive summary\n"
        "See key findings below.\n\n"
        "## Methodology\n"
        "- Sources analyzed: {n_sources}.\n"
        "- Facts recorded: {n_facts}.\n\n"
        "## Key findings\n"
        "{facts}\n\n"
        "## Facts and evidence\n"
        "{evidence}\n\n"
        "## Contradictions and uncertainties\n"
        "- No explicit contradictions identified in stored facts.\n\n"
        "## Hypotheses / gaps\n"
        "- No additional hypotheses recorded.\n\n"
        "## Practical recommendations\n"
        "- Use the conclusions above with source limitations in mind.\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
    ("research", "ru"): (
        "## Краткий вывод\n"
        "См. ключевые факты ниже.\n\n"
        "## Ключевые факты\n"
        "{facts}\n\n"
        "## Что говорят источники\n"
        "{evidence}\n\n"
        "## Ограничения\n"
        "- Ответ построен по сохранённым фактам research-agent.\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
    ("research", "en"): (
        "## Summary\n"
        "See key facts below.\n\n"
        "## Key facts\n"
        "{facts}\n\n"
        "## What sources say\n"
        "{evidence}\n\n"
        "## Limitations\n"
        "- The answer is built from research-agent stored facts.\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
}


# B-045: interrupted-run templates. Rendered when finalize_report is called with
# interrupted=True (research-agent timed out before calling research_finalize). These
# are deliberately short and honest: a banner, the gathered facts (with evidence), the
# sources, and a single limitation line stating synthesis did not happen. They do NOT
# include the analysis sections (Contradictions / Hypotheses / Recommendations) that a
# finished deep_research report promises, because no synthesis was performed.
# Placeholders: {banner}, {n_sources}, {n_facts}, {evidence}, {sources_section},
# {sources}, {limitations_section}, {interrupted_limitation}.
_INTERRUPTED_REPORT_TEMPLATES: dict[tuple[ResearchMode, ResearchLanguage], str] = {
    ("deep_research", "ru"): (
        "{banner}\n\n"
        "## Методология\n"
        "- Проанализировано источников: {n_sources}.\n"
        "- Зафиксировано фактов: {n_facts}.\n\n"
        "## Собранные факты\n"
        "{evidence}\n\n"
        "{limitations_section}\n"
        "{interrupted_limitation}\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
    ("deep_research", "en"): (
        "{banner}\n\n"
        "## Methodology\n"
        "- Sources analyzed: {n_sources}.\n"
        "- Facts recorded: {n_facts}.\n\n"
        "## Gathered facts\n"
        "{evidence}\n\n"
        "{limitations_section}\n"
        "{interrupted_limitation}\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
    ("research", "ru"): (
        "{banner}\n\n"
        "## Собранные факты\n"
        "{evidence}\n\n"
        "{limitations_section}\n"
        "{interrupted_limitation}\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
    ("research", "en"): (
        "{banner}\n\n"
        "## Gathered facts\n"
        "{evidence}\n\n"
        "{limitations_section}\n"
        "{interrupted_limitation}\n\n"
        "{sources_section}\n"
        "{sources}"
    ),
}


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

    def initialize_run_mode(
        self,
        user: User,
        run_id: str | None,
        mode: ResearchMode,
        *,
        language: ResearchLanguage | None = None,
    ) -> None:
        """Persist the intended research mode (and optional target language) for a run."""
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        self._upgrade_mode(state, mode)
        if language in ("ru", "en"):
            state["language"] = language
        self._write_json(run_dir / "state.json", state)

    def resolve_mode(self, user: User, run_id: str | None, value: Any) -> ResearchMode:
        """Resolve mode from the tool call or the persisted run state.

        Explicit tool-call mode upgrades the run, but never downgrades an
        existing deep_research run back to normal research.
        """
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        if value is not None and str(value).strip():
            self._upgrade_mode(state, normalize_research_mode(value))
            self._write_json(run_dir / "state.json", state)
            return normalize_research_mode(state.get("mode"))
        return normalize_research_mode(state.get("mode"))

    def get_language(self, user: User, run_id: str | None) -> ResearchLanguage:
        """Read the persisted target language for a run (default ``"en"``)."""
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        language = str(state.get("language") or "en")
        return language if language in ("ru", "en") else "en"

    def mark_list_facts_called(self, user: User, run_id: str | None) -> None:
        """Record that research_list_facts was invoked in this run."""
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        state["list_facts_called"] = True
        self._write_json(run_dir / "state.json", state)

    def mark_list_sources_called(self, user: User, run_id: str | None) -> None:
        """Record that research_list_sources was invoked in this run (B-053).

        Mirrors mark_list_facts_called: store_fact uses this flag to steer the model
        toward the exact cached source_id values instead of hallucinating IDs.
        """
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        state["list_sources_called"] = True
        self._write_json(run_dir / "state.json", state)

    def is_list_sources_called(self, user: User, run_id: str | None) -> bool:
        """Whether research_list_sources has been called in this run (B-053)."""
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        return bool(state.get("list_sources_called"))

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
        limit = self.effective_search_waves(user, run_id, state["mode"])
        used = _as_int(state.get("search_calls"), 0)
        if used >= limit:
            return (
                f"Error: Research search budget exceeded ({used}/{limit}). "
                "Do not retry research_search in this run. Use research_list_facts, then "
                "research_finalize with the available evidence and limitations."
            )
        state["search_calls"] = used + 1
        self._write_json(run_dir / "state.json", state)
        return None

    def search_budget_exceeded(
        self, user: User, run_id: str | None, mode: ResearchMode
    ) -> str | None:
        """B-052: check whether the search budget is already exhausted WITHOUT consuming it.

        Use this before issuing the (potentially slow / failing) web request so an
        over-budget call short-circuits cheaply. The actual unit is consumed by
        reserve_search only after a successful (non-infrastructure) search.
        """
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        self._upgrade_mode(state, mode)
        limit = self.effective_search_waves(user, run_id, state["mode"])
        used = _as_int(state.get("search_calls"), 0)
        if used >= limit:
            return (
                f"Error: Research search budget exceeded ({used}/{limit}). "
                "Do not retry research_search in this run. Use research_list_facts, then "
                "research_finalize with the available evidence and limitations."
            )
        return None

    def refund_search(self, user: User, run_id: str | None) -> None:
        """Return one search unit to the budget (B-052).

        Called when the underlying web search failed with an infrastructure error
        (not when the query legitimately returned no results). This keeps a transient
        web-search outage from exhausting the research budget and forcing the agent
        to finalize with no sources.
        """
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        used = _as_int(state.get("search_calls"), 0)
        state["search_calls"] = max(0, used - 1)
        self._write_json(run_dir / "state.json", state)

    def mark_search_failure(self, user: User, run_id: str | None) -> None:
        """Record a web-search infrastructure failure (B-052)."""
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        state["search_failures"] = _as_int(state.get("search_failures"), 0) + 1
        # Once search has failed repeatedly, mark the run so finalize_report prepends an
        # offline banner and the agent prompt steers toward a knowledge-based answer.
        if _as_int(state["search_failures"], 0) >= _WEB_SEARCH_DEGRADED_THRESHOLD:
            state["web_search_degraded"] = True
        self._write_json(run_dir / "state.json", state)

    def is_web_search_degraded(self, user: User, run_id: str | None) -> bool:
        """Whether web search has failed enough times to be considered unavailable."""
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        return bool(state.get("web_search_degraded"))

    # -- B-054: dynamic source budget ---------------------------------------

    def available_sources_count(self, user: User, run_id: str | None) -> int:
        """Number of fetched sources with a usable HTTP status (2xx).

        Used by the dynamic budget to decide whether the agent may keep
        fetching. Sources that returned 4xx/5xx are excluded: they were never
        a reliable basis for the report.
        """
        return sum(1 for s in self.list_sources(user, run_id) if _is_usable_status(s.get("status")))

    def _base_max_sources(self, mode: ResearchMode) -> int:
        return (
            self._settings.deep_max_sources
            if mode == "deep_research"
            else self._settings.normal_max_sources
        )

    def _base_search_waves(self, mode: ResearchMode) -> int:
        return (
            self._settings.deep_search_waves
            if mode == "deep_research"
            else self._settings.normal_search_waves
        )

    def effective_max_sources(self, user: User, run_id: str | None, mode: ResearchMode) -> int:
        """Dynamic cap on the number of fetchable sources (B-054-2).

        Failure-driven: the limit starts at the operator-configured ``base``
        and grows by one slot for each failed (non-2xx) fetch so far, so the
        agent can retry and still reach ``target_usable_sources`` USABLE
        sources. It is hard-capped at ``base * dynamic_budget_max_multiplier``::

            limit = min(max(base, base + failed), cap)

        A clean run stops at ``base``; a run full of 404/403 pages may store
        up to ``base * multiplier`` before being blocked. ``target_usable_sources``
        is the soft goal the agent is steered toward (see the research prompt),
        not a value that can override the base ceiling.
        """
        base = self._base_max_sources(mode)
        cap = max(base, int(round(base * self._settings.dynamic_budget_max_multiplier)))
        sources = self.list_sources(user, run_id)
        used = len(sources)
        usable = sum(1 for s in sources if _is_usable_status(s.get("status")))
        failed = max(0, used - usable)
        return min(max(base, base + failed), cap)

    def effective_search_waves(self, user: User, run_id: str | None, mode: ResearchMode) -> int:
        """Dynamic cap on the number of search waves (B-054-2).

        Expansion is driven by actual fetch *failures* (non-2xx stored sources),
        not by the mere absence of usable sources: at the start of a run there
        are always zero usable sources, so gap-based expansion would immediately
        inflate the budget. Instead the budget only grows after the agent has
        fetched some bad pages (404/403) and needs more waves to find
        alternatives. Bounded by ``base * multiplier``::

            limit = min(max(base, base + failed), cap)
        """
        base = self._base_search_waves(mode)
        cap = max(base, int(round(base * self._settings.dynamic_budget_max_multiplier)))
        sources = self.list_sources(user, run_id)
        used = len(sources)
        usable = sum(1 for s in sources if _is_usable_status(s.get("status")))
        failed = max(0, used - usable)
        return min(max(base, base + failed), cap)

    def reserve_fetch(self, user: User, run_id: str | None, mode: ResearchMode) -> str | None:
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        self._upgrade_mode(state, mode)
        # B-054-2: dynamic cap — grows when many fetches return non-2xx, so the
        # agent can still reach target_usable_sources, but is hard-capped.
        limit = self.effective_max_sources(user, run_id, state["mode"])
        used = len(self.list_sources(user, run_id))
        if used >= limit:
            return (
                f"Error: Research source budget exceeded ({used}/{limit}). "
                "Do not retry research_fetch_source in this run. Use cached sources, "
                "research_list_facts, then research_finalize with limitations if needed."
            )
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
            return (
                f"Error: Research reread budget exceeded ({used}/{limit}). "
                "Do not retry research_read_source in this run. Use research_list_facts, then "
                "research_finalize with the available evidence and limitations."
            )
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

    def format_sources_list(self, user: User, run_id: str | None, max_sources: int) -> str:
        """Return a compact list of all cached sources with their exact source_id (B-053).

        Used by research_list_sources and by store_fact's Unknown-source_id error so the
        model can anchor to the real IDs instead of hallucinating them.
        """
        sources = self.list_sources(user, run_id)
        if not sources:
            return "No research sources fetched yet."
        lines = ["Cached research sources (use these exact source_id values):", "---"]
        for s_obj in sources[: max(1, max_sources)]:
            title = str(s_obj.get("title") or "").strip() or "(untitled)"
            lines.append(
                "[{source_id}] {title} | url={url} | status={status}".format(
                    source_id=s_obj.get("source_id", ""),
                    title=title,
                    url=s_obj.get("url", ""),
                    status=s_obj.get("status", ""),
                )
            )
        return "\n".join(lines)

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

    def _validate_report(
        self,
        user: User,
        run_id: str | None,
        mode: ResearchMode,
        answer: str,
        language: ResearchLanguage,
        state: dict[str, Any],
    ) -> str | None:
        """Return an Error reason if the final answer violates grounding rules.

        Returns ``None`` when acceptable. Checks: target-language match for ru
        tasks, cited source_id/URL integrity, deep_research list_facts mandate,
        and count assertions vs. actually fetched sources.
        """
        if not answer.strip():
            return None  # empty answer -> deterministic skeleton, nothing to validate

        sources = self.list_sources(user, run_id)
        source_ids = {str(s.get("source_id") or "") for s in sources}
        source_urls = {str(s.get("url") or "") for s in sources}

        # 1. Language mismatch: ru task answered in English.
        if language == "ru":
            lowered = answer.casefold()
            en_signatures = (
                "executive summary",
                "## methodology",
                "key findings",
                "## summary",
            )
            if any(sig in lowered for sig in en_signatures):
                return (
                    "answer appears to be in English but the target language is Russian (ru). "
                    "Rewrite the report in Russian with Russian headings "
                    "(## Краткий вывод, ## Методология, ## Источники) and call "
                    "research_finalize again."
                )
            if _cyrillic_ratio(answer) < 0.15:
                return (
                    "answer is mostly Latin text but the target language is Russian (ru). "
                    "Write the report in Russian and call research_finalize again."
                )

        # 2. Cited source_id / URL must exist in the manifest.
        cited_ids = {m.group(1) for m in _CITED_SOURCE_ID_RE.finditer(answer)}
        invented_ids = sorted(c for c in cited_ids if c not in source_ids)
        if invented_ids:
            return (
                f"answer cites unknown source_id(s): {', '.join(invented_ids)}. "
                "Only cite source IDs returned by research_fetch_source or "
                "research_list_facts, or mark them as search-only limitations. "
                "Call research_finalize again."
            )
        cited_urls = {u.rstrip(").,]") for u in _URL_RE.findall(answer)}
        invented_urls = sorted(u for u in cited_urls if u and u not in source_urls)
        if invented_urls:
            return (
                f"answer cites {len(invented_urls)} URL(s) not present in fetched sources. "
                "Mark unfetched sources as a limitation instead. "
                "Call research_finalize again."
            )

        # 2b. B-054-3: a cited source must be USABLE (HTTP 2xx). A 4xx/5xx page
        # is typically a 404/403/Cloudflare error body that was stored before
        # the _fetch filter existed; the report must not lean on it. Build a
        # status map keyed by both source_id and url so either citation form is
        # caught.
        status_by_key: dict[str, str] = {}
        for s in sources:
            sid = str(s.get("source_id") or "")
            surl = str(s.get("url") or "")
            status = str(s.get("status") or "").strip()
            if status:
                if sid:
                    status_by_key[sid] = status
                if surl:
                    status_by_key[surl] = status
        unavailable_keys = sorted(
            {k for k in (*cited_ids, *cited_urls) if not _is_usable_status(status_by_key.get(k))}
        )
        if unavailable_keys:
            shown = ", ".join(unavailable_keys[:5])
            extra = "" if len(unavailable_keys) <= 5 else f" (and {len(unavailable_keys) - 5} more)"
            return (
                f"answer cites {len(unavailable_keys)} unavailable source(s): {shown}{extra}. "
                "These sources returned an HTTP error (4xx/5xx) and are not reliable. "
                "Remove them from the citations and the analysis, rely on usable sources "
                "(verify with research_list_sources), then call research_finalize again."
            )

        # 3. deep_research must consult research_list_facts before synthesis.
        if mode == "deep_research" and not state.get("list_facts_called"):
            return (
                "deep_research requires calling research_list_facts before finalize to "
                "verify stored facts. Call research_list_facts, then research_finalize again."
            )

        # 4. Count assertions must not exceed actually fetched sources.
        n_sources = len(sources)
        for m in _COUNT_ASSERTION_RE.finditer(answer):
            claimed = int(m.group("n"))
            if claimed > n_sources:
                return (
                    f"answer claims {claimed} {m.group('word')} but only {n_sources} "
                    f"source(s) were actually fetched. Correct the count to {n_sources} "
                    "(or fewer) and call research_finalize again."
                )

        return None

    def _handle_validation_failure(
        self,
        user: User,
        run_id: str | None,
        mode: ResearchMode,
        language: ResearchLanguage,
        facts: list[dict[str, Any]],
        sources: list[dict[str, Any]],
        state: dict[str, Any],
        reason: str,
    ) -> str | None:
        """Recovery for a failed validation.

        Returns the report string when the caller should return it immediately
        (strict Error or skeleton backstop), or ``None`` to continue normal
        finalization (soft-mode warning).
        """
        if not self._settings.finalize_strict:
            log_event(
                "research_finalize_validation_warning",
                run_id or "unknown",
                mode=mode,
                language=language,
                fetched_sources=len(sources),
                facts_total=len(facts),
                reason=reason,
            )
            return None

        attempts = _as_int(state.get("finalize_attempts"), 0)
        self._bump_finalize_attempts(self.run_dir(user, run_id), state)
        if attempts >= _FINALIZE_MAX_ATTEMPTS:
            skeleton = self._build_report(mode, facts, sources, language)
            log_event(
                "research_finalize_skeleton_fallback",
                run_id or "unknown",
                mode=mode,
                language=language,
                fetched_sources=len(sources),
                facts_total=len(facts),
                finalize_attempts=attempts + 1,
                reason=reason,
            )
            return skeleton.strip()

        log_event(
            "research_finalize_validation_failed",
            run_id or "unknown",
            mode=mode,
            language=language,
            fetched_sources=len(sources),
            facts_total=len(facts),
            list_facts_called=bool(state.get("list_facts_called")),
            finalize_attempts=attempts + 1,
            reason=reason,
        )
        return f"Error: research_finalize_validation_failed: {reason}"

    def _bump_finalize_attempts(self, run_dir: Path, state: dict[str, Any]) -> None:
        state["finalize_attempts"] = _as_int(state.get("finalize_attempts"), 0) + 1
        self._write_json(run_dir / "state.json", state)

    def finalize_report(
        self,
        user: User,
        run_id: str | None,
        mode: ResearchMode,
        answer: str,
        *,
        interrupted: bool = False,
    ) -> str:
        # B-045: interrupted=True is set by SubagentDispatcher on a research timeout.
        # In that case we skip grounding validation (the model never produced an answer
        # to validate) and build the honest interrupted skeleton directly.
        if interrupted:
            return self._build_report(
                mode,
                self.list_facts(user, run_id),
                self.list_sources(user, run_id),
                self.get_language(user, run_id),
                interrupted=True,
            ).strip()

        facts = self.list_facts(user, run_id)
        sources = self.list_sources(user, run_id)
        language = self.get_language(user, run_id)
        strings = _report_strings(language)
        strict = self._settings.finalize_strict
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)

        validation_reason = self._validate_report(user, run_id, mode, answer, language, state)
        if validation_reason is not None:
            recovery = self._handle_validation_failure(
                user, run_id, mode, language, facts, sources, state, validation_reason
            )
            if recovery is not None:
                return recovery
        else:
            log_event(
                "research_finalize_validation_passed",
                run_id or "unknown",
                mode=mode,
                language=language,
                fetched_sources=len(sources),
                facts_total=len(facts),
                list_facts_called=bool(state.get("list_facts_called")),
                strict=strict,
            )

        report = answer.strip() or self._build_report(mode, facts, sources, language)
        lowered = report.casefold()
        if "использованные источники" not in lowered and "sources" not in lowered:
            report = (
                report.rstrip()
                + "\n\n"
                + strings["sources_section"]
                + "\n"
                + self._sources_markdown(sources, language)
            )
        elif not _URL_RE.search(report):
            report = report.rstrip() + "\n\n" + self._sources_markdown(sources, language)
        if not facts and "огранич" not in lowered and "limitation" not in lowered:
            report = (
                report.rstrip()
                + "\n\n"
                + strings["limitations_section"]
                + "\n"
                + strings["limitations_no_facts"]
            )
        # B-052: if web search was unavailable for the whole run, prepend an honest
        # offline banner regardless of whether the model added one.
        if state.get("web_search_degraded") and "web search was unavailable" not in (
            report.casefold()
        ):
            report = f"{strings['offline_banner']}\n\n{report}"
        return report.strip()

    def _read_state(self, run_dir: Path) -> dict[str, Any]:
        state = self._read_json(run_dir / "state.json", {"mode": "research"})
        mode = normalize_research_mode(state.get("mode"))
        state["mode"] = mode
        state.setdefault("search_calls", 0)
        state.setdefault("rereads", 0)
        language = str(state.get("language") or "en")
        state.setdefault("language", language if language in ("ru", "en") else "en")
        state.setdefault("finalize_attempts", 0)
        state.setdefault("list_facts_called", False)
        # B-052: web-search resilience accounting.
        state.setdefault("search_failures", 0)
        state.setdefault("web_search_degraded", False)
        state.setdefault("list_sources_called", False)
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
        language: ResearchLanguage = "en",
        *,
        interrupted: bool = False,
    ) -> str:
        strings = _report_strings(language)
        # B-048: two renderings of the facts — brief (no evidence) and full (with
        # evidence) — so the two sections of a normal template no longer duplicate
        # the same body. Interrupted reports (B-045) only use the full rendering.
        facts_brief = self._facts_markdown(facts, language, with_evidence=False)
        facts_full = self._facts_markdown(facts, language, with_evidence=True)
        sources_block = self._sources_markdown(sources, language)
        if interrupted:
            template = _INTERRUPTED_REPORT_TEMPLATES[(mode, language)]
            return template.format(
                banner=strings["interrupted_banner"],
                facts=facts_brief,
                evidence=facts_full,
                sources=sources_block,
                n_sources=len(sources),
                n_facts=len(facts),
                sources_section=strings["sources_section"],
                limitations_section=strings["limitations_section"],
                interrupted_limitation=strings["interrupted_limitation"],
            )
        template = _REPORT_TEMPLATES[(mode, language)]
        return template.format(
            facts=facts_brief,
            evidence=facts_full,
            sources=sources_block,
            n_sources=len(sources),
            n_facts=len(facts),
            sources_section=strings["sources_section"],
        )

    def _facts_markdown(
        self,
        facts: list[dict[str, Any]],
        language: ResearchLanguage = "en",
        *,
        with_evidence: bool = True,
    ) -> str:
        if not facts:
            return _report_strings(language)["no_facts"]
        lines: list[str] = []
        for fact in facts:
            source_id = str(fact.get("source_id") or "")
            fact_text = str(fact.get("fact") or "").strip()
            confidence = str(fact.get("confidence") or "medium")
            relation = str(fact.get("relation") or "neutral")
            line = f"- {fact_text} [{source_id}; {confidence}; {relation}]"
            if with_evidence:
                evidence = str(fact.get("evidence") or "").strip()
                if evidence:
                    line += f" Evidence: {evidence[:240]}"
            lines.append(line)
        return "\n".join(lines)

    def _sources_markdown(
        self, sources: list[dict[str, Any]], language: ResearchLanguage = "en"
    ) -> str:
        if not sources:
            return _report_strings(language)["no_sources"]
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
        mode = self._runtime.resolve_mode(user, run_id, kwargs.get("mode"))

        # B-052: short-circuit cheaply if the budget is already exhausted (no web request).
        budget_error = self._runtime.search_budget_exceeded(user, run_id, mode)
        if budget_error:
            return budget_error

        # Run the search before reserving, so an infrastructure failure does not charge
        # the research budget. Only a successful (or non-infrastructure-error) search
        # consumes a budget unit via reserve_search below.
        result = await self._search_tool.execute(
            query=query,
            max_results=kwargs.get("max_results", 5),
            site=kwargs.get("site"),
            region=kwargs.get("region", "wt-wt"),
            timelimit=kwargs.get("timelimit"),
        )

        # Infrastructure failure: the web search itself broke (transient outage / block /
        # timeout that survived the tool-level retries). Record the failure; the budget is
        # NOT charged (reserve_search was not called). Steer the model once it cascades.
        if "unavailable" in result:
            self._runtime.mark_search_failure(user, run_id)
            if self._runtime.is_web_search_degraded(user, run_id):
                return _WEB_SEARCH_DEGRADED_MESSAGE
            return result

        # Success or a non-infrastructure error: consume the budget unit now.
        self._runtime.reserve_search(user, run_id, mode)
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
        mode = self._runtime.resolve_mode(user, run_id, kwargs.get("mode"))
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
        mode = self._runtime.resolve_mode(user, run_id, kwargs.get("mode"))
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
            # B-053: surface the real cached source_id values so the model can
            # self-correct instead of retrying hallucinated IDs. If list_sources was
            # never called, steer harder: require it first.
            available = self._runtime.format_sources_list(user, run_id, 50)
            if not self._runtime.is_list_sources_called(user, run_id):
                return (
                    f"Error: Unknown source_id '{source_id}'. You have not reviewed the "
                    f"cached sources yet. Call research_list_sources first to get the exact "
                    f"source_id values, then retry research_store_fact.\n\n{available}"
                )
            return (
                f"Error: Unknown source_id '{source_id}'. Do not invent or retry unknown "
                f"source IDs. Use only the source_id values below, then retry "
                f"research_store_fact.\n\n{available}"
            )
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


class ResearchListSourcesTool(Tool):
    """List all cached sources with their exact source_id (B-053).

    A deterministic anchor: the model calls this before research_store_fact to get the
    correct source_id values instead of hallucinating them on long contexts.
    """

    name = "research_list_sources"
    description = (
        "List all cached sources for this research run with their exact source_id. "
        "Call this before research_store_fact to use the correct source_id values."
    )
    params = [
        ToolParam(
            name="max_sources",
            type="integer",
            description="Maximum sources to return",
            required=False,
        )
    ]
    risk_level = RiskLevel.LOW
    parallel_safe = False

    def __init__(self, runtime: ResearchRuntime) -> None:
        self._runtime = runtime

    async def execute(self, *, user: User | None = None, **kwargs: Any) -> str:
        if user is None:
            return "Error: User context is required for research_list_sources."
        run_id = kwargs.get("run_id") if isinstance(kwargs.get("run_id"), str) else None
        self._runtime.mark_list_sources_called(user, run_id)
        return self._runtime.format_sources_list(
            user, run_id, _as_int(kwargs.get("max_sources"), 50)
        )


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
        self._runtime.mark_list_facts_called(user, run_id)
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
        mode = self._runtime.resolve_mode(user, run_id, kwargs.get("mode"))
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
        ResearchListSourcesTool(runtime),
        ResearchStoreFactTool(runtime),
        ResearchListFactsTool(runtime),
        ResearchFinalizeTool(runtime),
    ]
