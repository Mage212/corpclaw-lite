"""Cross-agent file-state registry (B-058).

Tracks, per run (``task_id = run_id``), which files an agent has read and
which agent last wrote to each path — so a write can detect two classes of
stale-ness that local LLMs hit constantly:

1. **write-without-read** — the agent is about to modify a file it has not
   read this run, which on a hallucinating local model means it is likely
   overwriting real content with fabricated bytes.
2. **cross-agent stale overwrite** — a sibling subagent (different run_id)
   wrote to the file *after* this agent read it, so the cached read is stale.

Warnings are *model-facing* and non-blocking: the wrapped tool still runs,
but the warning is prepended to its result so the next LLM turn sees it and
re-reads. This mirrors the existing read-before-write warning in
excel_workbook.py.

Disabled wholesale via ``CORPCLAW_FILE_STATE_GUARD=0`` (mirrors hermes
``HERMES_DISABLE_FILE_STATE_GUARD``).

Ported and adapted from hermes-agent ``tools/file_state.py``. Identity is
``run_id`` (unique per main/subagent loop) per the Phase-1 design decision —
no parent_run_id / agent-tree plumbing is added.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

__all__ = ["FileStateRegistry"]


# Bounded per-agent path cache (matches hermes _MAX_PATHS_PER_AGENT).
_MAX_PATHS_PER_AGENT = 4096


def _guard_disabled() -> bool:
    """Env-flag escape hatch (for tests / debugging)."""
    return os.environ.get("CORPCLAW_FILE_STATE_GUARD", "1") == "0"


class FileStateRegistry:
    """Per-run read stamps + global writer tracking.

    Thread-safe via a single global lock. Contention is negligible: calls are
    infrequent (one per tool execution) and the critical sections are short
    (dict updates).
    """

    def __init__(self) -> None:
        # task_id → {path: (mtime_ns, size)}
        self._read_stamps: dict[str, dict[str, tuple[int, int]]] = {}
        # path → (task_id, write_ts_monotonic)
        self._last_writer: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # ─── read tracking ───────────────────────────────────────────────────────

    def record_read(self, *, path: str, task_id: str, mtime_ns: int, size: int) -> None:
        """Record that ``task_id`` read ``path`` at the given signature.

        Called by ToolRegistry.execute after read tools (read_file,
        excel_inspect, excel_workbook(action=read), pdf_reader).
        """
        if _guard_disabled():
            return
        with self._lock:
            stamps = self._read_stamps.setdefault(task_id, {})
            # FIFO evict at the cap so a runaway loop cannot grow unbounded.
            if task_id in self._read_stamps and len(stamps) >= _MAX_PATHS_PER_AGENT:
                first_key = next(iter(stamps))
                stamps.pop(first_key, None)
            stamps[path] = (mtime_ns, size)

    # ─── write tracking ──────────────────────────────────────────────────────

    def note_write(self, *, path: str, task_id: str) -> None:
        """Record that ``task_id`` wrote to ``path``.

        Called by FileTrackedTool after a successful write.
        """
        if _guard_disabled():
            return
        with self._lock:
            self._last_writer[path] = (task_id, time.monotonic())

    # ─── stale check ─────────────────────────────────────────────────────────

    def check_stale(self, *, path: str, task_id: str) -> str | None:
        """Return a model-facing warning if writing to ``path`` would be stale.

        Returns None when the write is safe (the agent read this file this run
        and no other agent wrote to it since).
        """
        if _guard_disabled():
            return None
        with self._lock:
            stamps = self._read_stamps.get(task_id, {})
            if path not in stamps:
                return (
                    f"You are about to modify '{path}' but have not read it in "
                    "this run. Read the file first to avoid overwriting it with "
                    "hallucinated content."
                )
            last_writer, _ = self._last_writer.get(path, (None, 0.0))
            if last_writer is not None and last_writer != task_id:
                return (
                    f"File '{path}' was modified by another agent (run "
                    f"{last_writer}) since you last read it. Re-read the file "
                    "before writing to avoid a stale overwrite."
                )
        return None

    # ─── queries ─────────────────────────────────────────────────────────────

    def writes_since(self, *, exclude_task_id: str, paths: list[str]) -> list[str]:
        """Return the subset of ``paths`` written by a task other than
        ``exclude_task_id``. Used to remind a parent agent that a subagent
        modified files it previously read (full integration is B-012)."""
        if _guard_disabled():
            return []
        with self._lock:
            out: list[str] = []
            for p in paths:
                writer, _ = self._last_writer.get(p, (None, 0.0))
                if writer is not None and writer != exclude_task_id:
                    out.append(p)
            return out

    def has_read(self, *, path: str, task_id: str) -> bool:
        """Test helper: did ``task_id`` record a read of ``path``?"""
        with self._lock:
            return path in self._read_stamps.get(task_id, {})

    def last_writer(self, path: str) -> str | None:
        """Test helper: task_id of the last writer, or None."""
        with self._lock:
            writer, _ = self._last_writer.get(path, (None, 0.0))
            return writer

    def reset(self) -> None:
        """Clear all state (test helper / between-process runs)."""
        with self._lock:
            self._read_stamps.clear()
            self._last_writer.clear()

    # Convenience for tools that want to update a read stamp from a Path.
    def record_read_path(self, path: Any, *, task_id: str) -> None:
        """Record a read using a Path-like; reads its stat for the signature."""
        try:
            st = os.stat(path)
        except OSError:
            return
        self.record_read(path=str(path), task_id=task_id, mtime_ns=st.st_mtime_ns, size=st.st_size)
