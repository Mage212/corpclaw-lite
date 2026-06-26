"""Per-run checkpoint journal for long-running agent/subagent tasks (B-036).

A ``TaskRun`` is a stateless-on-disk reader/writer keyed by ``(user, run_id)``.
It mirrors the on-disk layout of ``ResearchRuntime`` (``.task_runs/<run_id>/``)
so that a timed-out or cancelled run leaves a recoverable journal + handoff
instead of a bare error string.

All public methods are async and delegate blocking filesystem I/O to a thread
pool via ``anyio.to_thread.run_sync`` — mirroring ``SQLiteMemory`` — so the agent
event loop is never stalled by journal writes (which fire on every tool call).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio

from corpclaw_lite.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User

__all__ = [
    "PHASE_FAILED",
    "PHASE_FINALIZED",
    "PHASE_PARTIAL",
    "PHASE_STARTED",
    "PHASE_TOOL_EXECUTED",
    "TaskRun",
]

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")

# Generic phase vocabulary derived from tool/journal evidence (not LLM percentage).
PHASE_STARTED = "started"
PHASE_TOOL_EXECUTED = "tool_executed"
PHASE_PARTIAL = "partial"
PHASE_FINALIZED = "finalized"
PHASE_FAILED = "failed"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class TaskRun:
    """Checkpoint journal for a single agent/subagent run.

    Layout::

        workspaces/user_<key>/.task_runs/<run_id>/
            state.json      # phase, status, timestamps, soft_deadline flag
            journal.jsonl   # append-only tool-call records
            handoff.md      # generated on soft deadline / hard timeout
    """

    def __init__(self, workspace_base: Path | None = None) -> None:
        self._workspace_base = (
            Path(workspace_base) if workspace_base else PROJECT_ROOT / "workspaces"
        )

    def run_dir(self, user: User, run_id: str | None) -> Path:
        user_key = user.workspace_key()
        safe_run_id = _SAFE_ID_RE.sub("_", run_id or "unknown")[:80] or "unknown"
        path = self._workspace_base / f"user_{user_key}" / ".task_runs" / safe_run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def initialize(
        self,
        user: User,
        run_id: str | None,
        *,
        subagent_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> Path:
        run_dir = await anyio.to_thread.run_sync(
            partial(
                self._sync_initialize,
                user,
                run_id,
                subagent_id=subagent_id,
                parent_run_id=parent_run_id,
            )
        )
        return run_dir

    async def set_phase(self, user: User, run_id: str | None, phase: str) -> None:
        await anyio.to_thread.run_sync(partial(self._sync_set_phase, user, run_id, phase))

    async def mark_soft_deadline(self, user: User, run_id: str | None) -> None:
        await anyio.to_thread.run_sync(partial(self._sync_mark_soft_deadline, user, run_id))

    async def record_tool_call(
        self,
        user: User,
        run_id: str | None,
        *,
        name: str,
        args_hash: str,
        status: str,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        await anyio.to_thread.run_sync(
            partial(
                self._sync_record_tool_call,
                user,
                run_id,
                name=name,
                args_hash=args_hash,
                status=status,
                duration_ms=duration_ms,
                error=error,
            )
        )

    async def generate_handoff(
        self,
        user: User,
        run_id: str | None,
        *,
        partial_result: str,
        reason: str,
    ) -> str:
        return await anyio.to_thread.run_sync(
            partial(
                self._sync_generate_handoff,
                user,
                run_id,
                partial_result=partial_result,
                reason=reason,
            )
        )

    # ── Sync implementations (run inside the thread pool) ───────────────────

    def _sync_initialize(
        self,
        user: User,
        run_id: str | None,
        *,
        subagent_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> Path:
        run_dir = self.run_dir(user, run_id)
        self._write_json(
            run_dir / "state.json",
            {
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "subagent_id": subagent_id,
                "phase": PHASE_STARTED,
                "status": "running",
                "started_at": _now_iso(),
                "soft_deadline_reached": False,
            },
        )
        return run_dir

    def _sync_set_phase(self, user: User, run_id: str | None, phase: str) -> None:
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        state["phase"] = phase
        self._write_json(run_dir / "state.json", state)

    def _sync_mark_soft_deadline(self, user: User, run_id: str | None) -> None:
        run_dir = self.run_dir(user, run_id)
        state = self._read_state(run_dir)
        state["soft_deadline_reached"] = True
        self._write_json(run_dir / "state.json", state)

    def _sync_record_tool_call(
        self,
        user: User,
        run_id: str | None,
        *,
        name: str,
        args_hash: str,
        status: str,
        duration_ms: float,
        error: str | None = None,
    ) -> None:
        run_dir = self.run_dir(user, run_id)
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "tool": name,
            "args_hash": args_hash,
            "status": status,
            "duration_ms": round(duration_ms, 1),
        }
        if error:
            entry["error"] = error[:500]
        with (run_dir / "journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _sync_generate_handoff(
        self,
        user: User,
        run_id: str | None,
        *,
        partial_result: str,
        reason: str,
    ) -> str:
        run_dir = self.run_dir(user, run_id)
        summary = self._journal_summary(run_dir)
        handoff = (
            "# Partial handoff\n\n"
            f"- Run ID: {run_id}\n"
            f"- Reason: {reason}\n"
            f"- Generated: {_now_iso()}\n\n"
            f"## Tool-call journal ({summary['total']} calls)\n"
            f"{summary['summary']}\n\n"
            "## Partial result\n\n"
            f"{partial_result}\n"
        )
        (run_dir / "handoff.md").write_text(handoff, encoding="utf-8")
        self._sync_set_phase(user, run_id, PHASE_PARTIAL)
        return handoff

    def _journal_summary(self, run_dir: Path) -> dict[str, Any]:
        journal = run_dir / "journal.jsonl"
        if not journal.exists():
            return {"total": 0, "summary": "- No tool calls recorded."}
        entries: list[dict[str, Any]] = []
        for line in journal.read_text(encoding="utf-8").splitlines():
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                entries.append(cast(dict[str, Any], raw))
        if not entries:
            return {"total": 0, "summary": "- No tool calls recorded."}
        lines = [
            f"- {e.get('tool', '?')} [{e.get('status', '?')}, {e.get('duration_ms', 0)}ms]"
            for e in entries[-20:]
        ]
        return {"total": len(entries), "summary": "\n".join(lines)}

    def _read_state(self, run_dir: Path) -> dict[str, Any]:
        path = run_dir / "state.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
