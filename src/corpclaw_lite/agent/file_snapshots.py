"""On-disk backup copies for file-change tracking (B-040).

Stores pre-write copies of files under
``<workspace_base>/user_<key>/.snapshots/<safe_run_id>/<rel_path>`` — the same
per-user/per-run layout used by :class:`TaskRun` (``.task_runs/<run_id>/``).

The DAO (:class:`FileChangeDAO`) keeps only metadata; the actual bytes of the
backup live here. Restore copies the backup back to the workspace atomically
via :func:`atomic_write_text` / a direct ``shutil.copy2`` into a temp + rename
for binary files.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from corpclaw_lite.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from corpclaw_lite.users.models import User

__all__ = ["FileSnapshotStore"]

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]+")


class FileSnapshotStore:
    """Backup-копии файлов для revert-операций (B-040)."""

    def __init__(self, workspace_base: Path | None = None) -> None:
        self._workspace_base = (
            Path(workspace_base) if workspace_base else PROJECT_ROOT / "workspaces"
        )

    # ─── paths ───────────────────────────────────────────────────────────────

    def _safe_run_id(self, run_id: str) -> str:
        return _SAFE_ID_RE.sub("_", run_id)[:80] or "unknown"

    def _snapshot_dir(self, user: User, run_id: str) -> Path:
        user_key = user.workspace_key()
        safe_run = self._safe_run_id(run_id)
        path = self._workspace_base / f"user_{user_key}" / ".snapshots" / safe_run
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _user_workspace_root(self, user: User) -> Path:
        return (self._workspace_base / f"user_{user.workspace_key()}").resolve()

    # ─── backup ──────────────────────────────────────────────────────────────

    def backup(self, user: User, run_id: str, source: Path) -> str:
        """Copy ``source`` into ``.snapshots/<run_id>/<rel_path>``.

        Returns the *relative* backup path (forward-slash-joined, as stored in
        the DAO's ``backup_path`` column). The source must already live inside
        the user workspace; we preserve its path relative to the workspace root
        so restore can write it back to the same place.
        """
        ws_root = self._user_workspace_root(user)
        source = Path(source).resolve()
        try:
            rel = source.relative_to(ws_root)
        except ValueError:
            # Source is outside the workspace (shouldn't happen — the caller
            # validated the path). Fail loudly.
            raise ValueError(
                f"backup source '{source}' is outside user workspace '{ws_root}'"
            ) from None

        backup_target = self._snapshot_dir(user, run_id) / rel
        backup_target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic copy: write to a temp in the same dir, then rename.
        tmp = backup_target.with_name(backup_target.name + ".tmp")
        shutil.copy2(source, tmp)
        tmp.replace(backup_target)
        return rel.as_posix()

    # ─── restore ─────────────────────────────────────────────────────────────

    def restore(self, user: User, run_id: str, backup_rel: str, target: Path) -> None:
        """Copy a backup back to ``target`` atomically.

        ``backup_rel`` is the relative path returned by :meth:`backup`.
        """
        ws_root = self._user_workspace_root(user)
        backup_path = self._snapshot_dir(user, run_id) / backup_rel
        if not backup_path.exists():
            raise FileNotFoundError(f"backup '{backup_rel}' for run '{run_id}' not found")

        target = Path(target).resolve()
        # Target must also be inside the workspace.
        try:
            target.relative_to(ws_root)
        except ValueError:
            raise ValueError(
                f"restore target '{target}' is outside user workspace '{ws_root}'"
            ) from None

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".restore.tmp")
        shutil.copy2(backup_path, tmp)
        tmp.replace(target)

    # ─── prune ───────────────────────────────────────────────────────────────

    def prune_run(self, user: User, run_id: str) -> None:
        """Remove the whole ``.snapshots/<run_id>/`` directory for a user."""
        safe_run = self._safe_run_id(run_id)
        run_dir = self._workspace_base / f"user_{user.workspace_key()}" / ".snapshots" / safe_run
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
