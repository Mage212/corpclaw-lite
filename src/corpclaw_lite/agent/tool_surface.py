# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Tool-surface phase control + BM25 soft-hint (B-107 / DC-036 / D-087).

Orthogonal to :mod:`phase_policy` (thinking on/off). This module:

1. Detects a deterministic :class:`ToolSurfacePhase` from user text + tools_used
2. Filters ``tools_schema`` from an immutable **base** schema (so phases can expand)
3. Injects a cache-safe BM25 soft-hint into the **messages tail** (not system prompt)

Research agents with :class:`~corpclaw_lite.agent.guards.TerminalToolMandate` enabled
skip hard filtering (mandate owns the funnel). Soft-hint can still run if enabled.

Hard filter only changes schema on **phase transition** (cache-breaking once).
Soft-hint every iteration rewrites only the tail marker message (cache-safe).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

__all__ = [
    "SOFT_HINT_MARKER",
    "ToolSurfacePhase",
    "ToolSurfaceProfile",
    "ToolSurfaceSettings",
    "apply_phase_filter",
    "detect_tool_surface_phase",
    "inject_soft_hint",
    "rank_tool_names",
    "schema_tool_name",
]

ToolSurfaceProfile = Literal["main", "office", "execution", "none"]

SOFT_HINT_MARKER = "[Tool relevance]"

_EXECUTING_KW = re.compile(
    r"\b("
    r"write|edit|save|fill|create|modify|update|delete|run|execute|script|"
    r"заполн|сохран|измен|создай|создать|удал|запуск|выполни|напиш|правк|"
    r"нормализ|convert|конверт"
    r")\w*\b",
    re.IGNORECASE,
)

# Read/analyze-safe tools (no host file mutation via write/edit/exec).
# H2 fix: READING includes these so office agents can table_query/pdf/chart
# without chicken-egg (previously ANALYZING required tools already used).
_ANALYZE_TOOLS = frozenset(
    {
        "table_query",
        "excel_workbook",  # read ranges; fill still model-driven
        "excel_inspect",
        "pdf_reader",
        "chart_generate",
        "diff_text",
    }
)
# Mutating tools — only after EXECUTING signal (keywords or prior write tools).
_MUTATING_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "exec_script",
        "normalize_excel",
        "convert_format",
    }
)
_WRITE_TOOLS = _MUTATING_TOOLS | frozenset(
    {
        # sticky EXECUTING when these ran (includes analyze that mutate outputs)
        "excel_workbook",
        "chart_generate",
        "normalize_excel",
        "convert_format",
    }
)
_MEMORY_TOOLS = frozenset({"memory_store", "memory_recall"})

_READING_ALLOW = (
    frozenset(
        {
            "read_file",
            "list_files",
            "search_files",
            "excel_inspect",
            "read_image",
            "memory_recall",
            "dispatch_subagent",
            "web_fetch",
            "web_search",
            "send_file",
        }
    )
    | _ANALYZE_TOOLS
)
_ANALYZING_ALLOW = _READING_ALLOW | frozenset({"memory_store"})
_EXECUTING_ALLOW = _ANALYZING_ALLOW | _MUTATING_TOOLS | _WRITE_TOOLS
_MEMORY_ALLOW = frozenset(
    {
        "memory_store",
        "memory_recall",
        "read_file",
        "list_files",
        "search_files",
        "dispatch_subagent",
    }
)

_PHASE_ALLOW: dict[str, frozenset[str]] = {
    "reading": _READING_ALLOW,
    "analyzing": _ANALYZING_ALLOW,
    "executing": _EXECUTING_ALLOW,
    "memory": _MEMORY_ALLOW,
}


class ToolSurfacePhase(StrEnum):
    """Task phase for tool-schema surface control (not thinking PhaseContext)."""

    READING = "reading"
    ANALYZING = "analyzing"
    EXECUTING = "executing"
    MEMORY = "memory"


@dataclass(frozen=True)
class ToolSurfaceSettings:
    """Config for tool-surface control (mirrors AgentSettings nested model)."""

    enabled: bool = True
    soft_hint_enabled: bool = True
    soft_hint_top_k: int = 5
    hard_filter_profiles: tuple[str, ...] = ("office",)


def schema_tool_name(entry: dict[str, Any]) -> str:
    """Extract tool name from an OpenAI-style tools schema entry."""
    fn = entry.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    name2 = entry.get("name")
    return name2 if isinstance(name2, str) else ""


def detect_tool_surface_phase(
    *,
    user_message: str,
    tools_used: list[str],
    prev_phase: ToolSurfacePhase | str | None,
    closing_mode: bool,
    mandate_enabled: bool,
    profile: ToolSurfaceProfile,
) -> ToolSurfacePhase | None:
    """Return target phase, or None when hard-filter should be disabled.

    Sticky: never automatically go EXECUTING → READING (monotonic for writes).
    """
    if mandate_enabled or profile in ("none", "execution") or closing_mode:
        return None

    prev = ToolSurfacePhase(prev_phase) if prev_phase else None

    # Sticky EXECUTING once write-like tools ran
    if any(t in _WRITE_TOOLS for t in tools_used) or (prev is ToolSurfacePhase.EXECUTING):
        if _EXECUTING_KW.search(user_message) or any(t in _WRITE_TOOLS for t in tools_used):
            return ToolSurfacePhase.EXECUTING
        if prev is ToolSurfacePhase.EXECUTING:
            return ToolSurfacePhase.EXECUTING

    if _EXECUTING_KW.search(user_message):
        return ToolSurfacePhase.EXECUTING

    if any(t in _ANALYZE_TOOLS for t in tools_used):
        return ToolSurfacePhase.ANALYZING

    if tools_used and all(t in _MEMORY_TOOLS for t in tools_used):
        return ToolSurfacePhase.MEMORY

    if (
        any(t in _MEMORY_TOOLS for t in tools_used)
        and not any(t in _ANALYZE_TOOLS | _WRITE_TOOLS for t in tools_used)
        and tools_used
        and tools_used[-1] in _MEMORY_TOOLS
    ):
        return ToolSurfacePhase.MEMORY

    return ToolSurfacePhase.READING


def apply_phase_filter(
    base_schema: list[dict[str, Any]] | None,
    phase: ToolSurfacePhase | str | None,
    *,
    profile: ToolSurfaceProfile,
    enabled: bool,
) -> list[dict[str, Any]] | None:
    """Filter *base* schema by phase allowlist.

    Unknown tool names (MCP/plugins) are **kept** (fail-open for extensions).
    ``dispatch_subagent`` is always kept on main profile when present in base.
    """
    if not enabled or base_schema is None or phase is None:
        return list(base_schema) if base_schema is not None else None
    if profile not in ("office", "main"):
        return list(base_schema)

    phase_key = str(phase)
    allow = _PHASE_ALLOW.get(phase_key)
    if allow is None:
        return list(base_schema)

    # Main profile: never drop dispatch_subagent (routing)
    keep_always: frozenset[str] = (
        frozenset({"dispatch_subagent"}) if profile == "main" else frozenset()
    )

    known: set[str] = set()
    for allow_set in _PHASE_ALLOW.values():
        known |= allow_set
    out: list[dict[str, Any]] = []
    for entry in base_schema:
        name = schema_tool_name(entry)
        if not name:
            out.append(entry)
            continue
        if name in keep_always or name not in known or name in allow:
            out.append(entry)
    return out


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9_]{2,}", text.lower())


def rank_tool_names(
    query: str,
    schema: list[dict[str, Any]] | None,
    *,
    top_k: int = 5,
) -> list[str]:
    """Rank tool names by simple BM25-ish score against schema text.

    Uses name + description + param names only (no JSON structural noise).
    """
    if not schema or top_k <= 0:
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        # fall back to schema order first top_k names
        names = [schema_tool_name(e) for e in schema]
        return [n for n in names if n][:top_k]

    docs: list[tuple[str, list[str]]] = []
    for entry in schema:
        name = schema_tool_name(entry)
        if not name:
            continue
        # searchable_text: name + description only (no JSON structural noise)
        raw_fn = entry.get("function")
        desc = ""
        if isinstance(raw_fn, dict):
            raw_desc = raw_fn.get("description")
            if isinstance(raw_desc, str):
                desc = raw_desc
        docs.append((name, _tokenize(f"{name} {desc}")))

    if not docs:
        return []

    n_docs = len(docs)
    df: dict[str, int] = {}
    for _, toks in docs:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1

    # BM25 params tuned for short tool texts (Ratel ADR-0004 style lower b)
    k1 = 0.9
    b = 0.4
    avgdl = sum(len(t) for _, t in docs) / max(n_docs, 1)

    scored: list[tuple[float, str]] = []
    for name, toks in docs:
        if not toks:
            scored.append((0.0, name))
            continue
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        score = 0.0
        dl = len(toks)
        for qt in q_tokens:
            if qt not in tf:
                continue
            n_qi = df.get(qt, 0)
            idf = math.log(1.0 + (n_docs - n_qi + 0.5) / (n_qi + 0.5))
            freq = tf[qt]
            denom = freq + k1 * (1.0 - b + b * dl / max(avgdl, 1e-6))
            score += idf * (freq * (k1 + 1.0)) / max(denom, 1e-6)
        scored.append((score, name))

    scored.sort(key=lambda x: (-x[0], x[1]))
    # Always return top_k names (even zero score) so soft-hint has content.
    return [name for _, name in scored[:top_k]]


def inject_soft_hint(
    messages: list[dict[str, Any]],
    ranked_names: list[str],
    *,
    enabled: bool,
) -> list[dict[str, Any]]:
    """Return messages with a single soft-hint at the tail (cache-safe).

    Removes any previous message containing :data:`SOFT_HINT_MARKER`, then
    appends a fresh user-role hint. Does not touch system_prompt.
    """
    if not enabled:
        return messages
    cleaned = [
        m
        for m in messages
        if not (isinstance(m.get("content"), str) and SOFT_HINT_MARKER in str(m.get("content")))
    ]
    if not ranked_names:
        return cleaned
    hint = (
        f"{SOFT_HINT_MARKER} Prefer if needed: {', '.join(ranked_names)}. "
        "Only call tools that appear in your tool list."
    )
    cleaned.append({"role": "user", "content": hint})
    return cleaned
