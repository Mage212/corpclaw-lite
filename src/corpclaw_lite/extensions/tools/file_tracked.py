"""File-tracked tool wrapper (B-040 / B-058).

Transparent decorator over office tools that records file mutations into the
file-change journal (B-040) and the cross-agent file-state registry (B-058).
Subclasses :class:`ScopedTool` so all attributes (``name``, ``description``,
``params``, ``risk_level``, ``parallel_safe``, ``terminal``,
``should_return_direct``) pass through unchanged; only ``execute`` is wrapped.

Flow (B-040, on every tracked tool call)::

    user/run_id/source_path = kwargs
    before_hash = sha256(source) if exists else None
    backup_rel = backup(...) if before_hash and tracks_output
    result = tool.execute(**kwargs)
    after_path = resolve_output_path(...)   # per-tool rule
    if after_path exists and after_hash != before_hash:
        record_change(op, before_hash, after_hash, backup_rel, size)
    return result

B-058 adds a pre-write ``check_stale`` warning and a post-write ``note_write``
when a :class:`FileStateRegistry` is wired in (see B-058 commit).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.extensions.tools.scoped import ScopedTool
from corpclaw_lite.security.path_validator import resolve_and_validate_path

if TYPE_CHECKING:
    from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
    from corpclaw_lite.extensions.tools.base import Tool
    from corpclaw_lite.memory.file_changes import FileChangeDAO

__all__ = ["FileTrackedTool"]

logger = logging.getLogger(__name__)


def _sha256_file(path: Path) -> str:
    """SHA-256 of file bytes, truncated to 12 hex chars (matches _payload_hash style)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


class FileTrackedTool(ScopedTool):
    """Records file mutations made by an office tool into the change journal.

    Args:
        tool: The wrapped tool (e.g. ``WriteFileTool``, ``ExcelWorkbookTool``).
        dao: File-change journal DAO (B-040). May be None to disable tracking.
        snapshot_store: On-disk backup store (B-040). May be None.
        path_param: Name of the kwarg holding the *input* path
            (``"path"`` / ``"input_path"`` / ``"data_path"``).
        tracks_output: True when the tool mutates the input file in place
            (``write_file``, ``edit_file``, ``excel_workbook(fill, in_place)``).
            When False, no backup is taken — the tool creates a *new* file, and
            only the after-snapshot is recorded.
        file_state: Cross-agent stale-write registry (B-058). Optional; when
            None, stale-write checks are skipped.
    """

    def __init__(
        self,
        tool: Tool,
        *,
        dao: FileChangeDAO | None,
        snapshot_store: FileSnapshotStore | None,
        path_param: str = "path",
        tracks_output: bool = False,
    ) -> None:
        super().__init__(tool, source_kind="builtin", source_name=tool.name)
        self._dao = dao
        self._snap = snapshot_store
        self._path_param = path_param
        self._tracks_output = tracks_output
        # B-058: cross-agent stale-write registry. Wired in a follow-up commit;
        # stays None here so this wrapper is B-040-only for now.
        self._file_state: Any = None

    # ─── output path resolution ──────────────────────────────────────────────

    def _resolve_output_path(
        self,
        source_path: Path,
        kwargs: dict[str, Any],
    ) -> Path | None:
        """Compute the actual file written by this tool call.

        Per-tool rules. Falls back to the input path for tools that mutate
        in place (``tracks_output=True``).
        """
        name = self._tool.name
        if self._tracks_output:
            return source_path

        # Tools that emit a new file alongside the input.
        explicit = kwargs.get("output_path")
        if isinstance(explicit, str) and explicit:
            try:
                return resolve_and_validate_path(explicit)
            except Exception:
                return None

        # Default-named outputs computed by the tool itself.
        stem = source_path.stem
        suffix = source_path.suffix
        defaults: dict[str, str] = {
            "excel_workbook": f"{stem}_filled{suffix or '.xlsx'}",
            "normalize_excel": f"{stem}_normalized{suffix or '.xlsx'}",
        }
        default_name = defaults.get(name)
        if default_name is not None:
            return source_path.with_name(default_name)
        return None

    # ─── execute ─────────────────────────────────────────────────────────────

    async def execute(self, **kwargs: Any) -> str:
        user = kwargs.get("user")
        run_id = kwargs.get("run_id")
        source_raw = kwargs.get(self._path_param)

        # No tracking context → pass through untouched.
        if not (
            user is not None
            and isinstance(run_id, str)
            and isinstance(source_raw, str)
            and source_raw
        ):
            return await self._tool.execute(**kwargs)

        try:
            source_path = resolve_and_validate_path(source_raw)
        except Exception:
            # Invalid path → let the wrapped tool surface its own error.
            return await self._tool.execute(**kwargs)

        before_hash: str | None = None
        if source_path.exists():
            try:
                before_hash = _sha256_file(source_path)
            except OSError:
                before_hash = None

        # For tools that write to a *different* path than the input (normalize,
        # convert, fill-default), determine that output path up front so we can
        # (a) take a backup of the pre-existing output if any, and (b) compute
        # op='create' vs 'modify' based on whether the output existed before.
        output_path_pre = (
            source_path if self._tracks_output else self._resolve_output_path(source_path, kwargs)
        )
        output_existed_before = output_path_pre is not None and output_path_pre.exists()
        output_before_hash: str | None = None
        if output_existed_before and output_path_pre is not None:
            try:
                output_before_hash = _sha256_file(output_path_pre)
            except OSError:
                output_before_hash = None

        # Back up the file we are about to overwrite. For in-place tools that
        # is the source itself; for new-file tools it is the pre-existing
        # output (rare, but e.g. re-running normalize on the same name).
        backup_target: Path | None = None
        if self._tracks_output and before_hash is not None:
            backup_target = source_path
        elif not self._tracks_output and output_existed_before and output_path_pre is not None:
            backup_target = output_path_pre
        backup_rel: str | None = None
        if backup_target is not None and self._snap is not None:
            try:
                backup_rel = self._snap.backup(user, run_id, backup_target)
            except Exception:
                logger.warning(
                    "file_tracked: backup failed for %s (run %s)",
                    backup_target,
                    run_id,
                    exc_info=True,
                )
                backup_rel = None

        result = await self._tool.execute(**kwargs)

        # Record the change. Best-effort: never let journaling break the tool.
        try:
            after_path = self._resolve_output_path(source_path, kwargs)
            if after_path is not None and after_path.exists():
                after_hash = _sha256_file(after_path)
                # op is decided by whether the *output* file existed before the
                # call, not the input — normalize_excel reads data.xlsx but
                # creates data_normalized.xlsx, which is a create, not a modify.
                op = "create" if not output_existed_before else "modify"
                # before_hash for the journal is the output's pre-hash when
                # available (modify case), else None (create case).
                journal_before_hash = output_before_hash if output_existed_before else None
                if after_hash != journal_before_hash and self._dao is not None:
                    user_id = (
                        user.memory_key()
                        if hasattr(user, "memory_key")
                        else str(getattr(user, "id", "unknown"))
                    )
                    try:
                        ws_root = Path.cwd().resolve()
                        rel_path = (
                            str(after_path.relative_to(ws_root))
                            if ws_root in after_path.parents
                            else str(after_path)
                        )
                    except Exception:
                        rel_path = str(after_path)
                    await self._dao.record_change(
                        user_id=user_id,
                        run_id=run_id,
                        tool_name=self._tool.name,
                        file_path=rel_path,
                        op=op,
                        before_hash=journal_before_hash,
                        after_hash=after_hash,
                        backup_path=backup_rel,
                        size_bytes=after_path.stat().st_size,
                    )
        except Exception:
            logger.warning(
                "file_tracked: record_change failed for %s (run %s)",
                source_path,
                run_id,
                exc_info=True,
            )

        return result
