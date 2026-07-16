"""B-109 / DC-027 Layer 2+3: merge helpers for the memory worker.

These are pure, testable helpers for:
- backing up and atomically writing ``users/{id}.md``
- ensuring the canonical non-authoritative disclaimer header
- parsing the LLM JSON response (strict; ``None`` on any failure → no write)
- applying worker entries via :class:`~corpclaw_lite.memory.sqlite.SQLiteMemory`
  (merge-only — never deletes unmentioned entries)
- gathering a transcript excerpt from recent non-system chat sessions

No LLM call here — the service layer (:mod:`corpclaw_lite.memory.worker`) owns that.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

__all__ = [
    "MAX_WORKER_ABSTRACTION_LEN",
    "MAX_WORKER_VALUE_LEN",
    "WorkerEntry",
    "WorkerUpdate",
    "apply_worker_entries",
    "backup_user_md",
    "ensure_disclaimer",
    "gather_transcript",
    "parse_worker_response",
    "write_user_md_atomic",
]

if TYPE_CHECKING:
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore
    from corpclaw_lite.memory.sqlite import SQLiteMemory

logger = logging.getLogger(__name__)

# Length caps aligned with B-108 SQLiteMemory limits.
MAX_WORKER_ABSTRACTION_LEN = 120
MAX_WORKER_VALUE_LEN = 8000
MAX_WORKER_CUES = 16
MAX_WORKER_CUE_LEN = 64

_DISCLAIMER_MARKER = "<!-- auto-managed: memory worker"
_DISCLAIMER_HEADER = (
    "<!-- auto-managed: memory worker — non-authoritative, merge-only. "
    "Do not edit by hand unless you know what you are doing. -->\n\n"
)


@dataclass(frozen=True, slots=True)
class WorkerEntry:
    """One structured entry proposed by the worker LLM."""

    abstraction: str
    value: str
    cues: list[str]


@dataclass(frozen=True, slots=True)
class WorkerUpdate:
    """Parsed LLM response: full new ``.md`` body + structured entries."""

    md: str
    entries: list[WorkerEntry]
    summary: str


# ── .md file helpers ──────────────────────────────────────────────────────


def backup_user_md(path: Path, *, keep_history: bool = False) -> Path | None:
    """Create ``{path}.bak`` (overwrite previous) and optional timestamped history.

    Returns the backup path, or ``None`` if *path* does not exist.
    """
    if not path.exists():
        return None
    bak = path.with_suffix(path.suffix + ".bak")
    bak.write_bytes(path.read_bytes())
    if keep_history:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        hist = path.with_suffix(path.suffix + f".bak.{ts}")
        hist.write_bytes(path.read_bytes())
    return bak


def write_user_md_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (temp + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def ensure_disclaimer(md: str) -> str:
    """Prepend the canonical disclaimer header if *md* does not already carry one.

    Idempotent: if the marker is present anywhere in the text, return unchanged.
    """
    if _DISCLAIMER_MARKER in md:
        return md
    return _DISCLAIMER_HEADER + md.lstrip("\n")


# ── LLM response parsing ──────────────────────────────────────────────────


def _coerce_entry(raw: object) -> WorkerEntry | None:
    """Validate and coerce one entry dict; return None on any structural problem."""
    if not isinstance(raw, dict):
        return None
    data = cast(dict[str, object], raw)
    abstraction_raw = data.get("abstraction")
    value_raw = data.get("value")
    if not isinstance(abstraction_raw, str) or not isinstance(value_raw, str):
        return None
    abstraction = abstraction_raw.strip()[:MAX_WORKER_ABSTRACTION_LEN]
    value = value_raw.strip()[:MAX_WORKER_VALUE_LEN]
    if not abstraction or not value:
        return None
    cues_raw_obj = data.get("cues", [])
    cues_list = cast(list[object], cues_raw_obj) if isinstance(cues_raw_obj, list) else []
    cues: list[str] = []
    for c_obj in cues_list:
        if not isinstance(c_obj, str):
            continue
        c = c_obj.strip()[:MAX_WORKER_CUE_LEN]
        if c and c not in cues:
            cues.append(c)
        if len(cues) >= MAX_WORKER_CUES:
            break
    return WorkerEntry(abstraction=abstraction, value=value, cues=cues)


def parse_worker_response(raw: str, *, max_entries: int = 20) -> WorkerUpdate | None:
    """Parse the LLM JSON response.

    Tolerates surrounding markdown fences (single ``{...}`` extraction).
    Returns ``None`` on any structural failure — the caller must not write.
    """
    text = raw.strip()
    # Strip ```json ... ``` fences if present.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    # Extract first {...} block if there is surrounding prose.
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        parsed: object = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast(dict[str, object], parsed)
    md_obj = data.get("md")
    if not isinstance(md_obj, str) or not md_obj.strip():
        return None
    md = md_obj
    raw_entries_obj = data.get("entries", [])
    raw_entries = cast(list[object], raw_entries_obj) if isinstance(raw_entries_obj, list) else []
    entries: list[WorkerEntry] = []
    for re_raw in raw_entries[:max_entries]:
        coerced = _coerce_entry(re_raw)
        if coerced is not None:
            entries.append(coerced)
    summary_obj = data.get("summary", "")
    summary = summary_obj if isinstance(summary_obj, str) else ""
    return WorkerUpdate(md=md, entries=entries, summary=summary[:500])


# ── Entry application (merge-only) ────────────────────────────────────────


async def apply_worker_entries(
    memory: SQLiteMemory,
    user_id: str,
    entries: list[WorkerEntry],
) -> int:
    """Upsert worker entries via ``store_entry`` (merge-only; never deletes).

    Returns the number of entries actually stored.
    """
    count = 0
    for entry in entries:
        try:
            await memory.store_entry(
                user_id,
                primary_abstraction=entry.abstraction,
                memory_value=entry.value,
                cues=entry.cues,
            )
            count += 1
        except Exception as e:
            logger.warning(
                "memory_worker: failed to store entry '%s' for user %s: %s",
                entry.abstraction,
                user_id,
                e,
            )
    return count


# ── Transcript gathering ──────────────────────────────────────────────────


async def gather_transcript(
    *,
    chat_store: WebChatStore,
    context_store: ChatContextStore,
    user_id: str,
    max_sessions: int = 5,
    max_messages_per_session: int = 40,
    max_chars: int = 24_000,
) -> str:
    """Gather a transcript excerpt from recent non-system chat sessions.

    Sessions are ordered by activity (active first, then ``updated_at`` DESC).
    System sessions (scheduler / headless / proactive) are excluded.
    Only ``user`` and ``assistant`` roles are kept — tool noise is dropped.
    """
    sessions = await chat_store.list_sessions(user_id)
    # Exclude system sessions (scheduler / headless / proactive inbox).
    candidate_sessions = [s for s in sessions if s.section != "system"]
    candidate_sessions = candidate_sessions[:max_sessions]
    if not candidate_sessions:
        return ""
    parts: list[str] = []
    total_chars = 0
    for session in candidate_sessions:
        if total_chars >= max_chars:
            break
        title = session.title or f"Chat #{session.id}"
        try:
            msgs = await context_store.list_context(session.id, user_id=user_id)
        except Exception as e:
            logger.debug("memory_worker: skip session %s: %s", session.id, e)
            continue
        session_lines: list[str] = [f"[session: {title}]"]
        msg_count = 0
        for msg in msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str) or not content.strip():
                continue
            session_lines.append(f"{role}: {content}")
            msg_count += 1
            if msg_count >= max_messages_per_session:
                break
            total_chars += len(content)
            if total_chars >= max_chars:
                break
        if msg_count > 0:
            parts.append("\n".join(session_lines))
    return "\n\n".join(parts)[:max_chars]
