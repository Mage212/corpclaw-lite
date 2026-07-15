"""B-117 / DC-042: user-facing review + revert over B-040 file journal.

Thin service: list changes, text/binary diff, restore backup + mark_reverted.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
from corpclaw_lite.memory.file_changes import FileChange, FileChangeDAO
from corpclaw_lite.security.path_validator import resolve_and_validate_path
from corpclaw_lite.users.models import User

__all__ = [
    "ChangePublic",
    "DiffPayload",
    "FileReviewError",
    "FileReviewService",
    "RevertResult",
]

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".csv",
        ".log",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".sh",
        ".sql",
        ".rs",
        ".go",
        ".java",
        ".kt",
    }
)
_MAX_DIFF_BYTES = 200 * 1024
_MAX_DIFF_LINES = 5000


class FileReviewError(Exception):
    """User-facing review/revert failure with HTTP-ish status."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class ChangePublic:
    """JSON-safe change row for the web UI."""

    change_id: str
    run_id: str
    path: str
    op: str
    tool_name: str
    status: str
    size_bytes: int
    created_at: int
    has_backup: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "run_id": self.run_id,
            "path": self.path,
            "op": self.op,
            "tool_name": self.tool_name,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "has_backup": self.has_backup,
        }


@dataclass(frozen=True, slots=True)
class DiffPayload:
    """Diff or binary stub for one change."""

    change_id: str
    path: str
    kind: Literal["text", "binary", "create"]
    unified_diff: str | None = None
    truncated: bool = False
    before_hash: str | None = None
    after_hash: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "change_id": self.change_id,
            "path": self.path,
            "kind": self.kind,
            "truncated": self.truncated,
        }
        if self.unified_diff is not None:
            body["unified_diff"] = self.unified_diff
        if self.before_hash is not None:
            body["before_hash"] = self.before_hash
        if self.after_hash is not None:
            body["after_hash"] = self.after_hash
        if self.message is not None:
            body["message"] = self.message
        return body


@dataclass(frozen=True, slots=True)
class RevertResult:
    """Outcome of a revert attempt."""

    ok: bool
    change_id: str
    action: Literal["restored", "deleted", "already_reverted"]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "change_id": self.change_id,
            "action": self.action,
        }


def _to_public(change: FileChange) -> ChangePublic:
    return ChangePublic(
        change_id=change.id,
        run_id=change.run_id,
        path=change.file_path,
        op=change.op,
        tool_name=change.tool_name,
        status=change.status,
        size_bytes=change.size_bytes,
        created_at=change.created_at,
        has_backup=bool(change.backup_path),
    )


def _looks_like_text(path: str, sample: bytes) -> bool:
    suffix = Path(path).suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return True
    if suffix in {".xlsx", ".xls", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip"}:
        return False
    if not sample:
        return True
    if b"\x00" in sample[:8192]:
        return False
    try:
        sample[:8192].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


class FileReviewService:
    """List / diff / revert agent file changes for a workspace user."""

    def __init__(
        self,
        *,
        dao: FileChangeDAO,
        snapshot_store: FileSnapshotStore,
        workspace_base: Path,
    ) -> None:
        self._dao = dao
        self._snap = snapshot_store
        self._workspace_base = Path(workspace_base)

    def _workspace_for(self, user: User) -> Path:
        return (self._workspace_base / f"user_{user.workspace_key()}").resolve()

    def _resolve_target(self, user: User, file_path: str) -> Path:
        """Map journal path to a path under the user workspace."""
        workspace = self._workspace_for(user)
        raw = file_path.strip().replace("\\", "/")
        # Journal may store workspace-relative or absolute paths (file_tracked quirk).
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                rel = candidate.resolve().relative_to(workspace)
                raw = rel.as_posix()
            except ValueError:
                raw = candidate.name
        try:
            return resolve_and_validate_path(raw, workspace_root=workspace)
        except (PermissionError, ValueError, OSError) as e:
            raise FileReviewError(str(e), status=403) from e

    def _backup_abs(self, user: User, run_id: str, backup_rel: str) -> Path:
        return self._snap.resolve_backup_path(user, run_id, backup_rel)

    async def list_changes(
        self,
        user: User,
        *,
        limit: int = 50,
        status: str | None = "open",
    ) -> list[ChangePublic]:
        safe_limit = max(1, min(200, int(limit)))
        rows = await self._dao.list_recent_for_user(
            user.memory_key(), limit=safe_limit, status=status
        )
        return [_to_public(r) for r in rows]

    async def get_owned_change(self, user: User, change_id: str) -> FileChange:
        change = await self._dao.get_change(change_id)
        if change is None or change.user_id != user.memory_key():
            raise FileReviewError("Change not found.", status=404)
        return change

    async def build_diff(self, user: User, change_id: str) -> DiffPayload:
        change = await self.get_owned_change(user, change_id)
        target = self._resolve_target(user, change.file_path)
        after_bytes = b""
        if target.exists() and target.is_file():
            after_bytes = target.read_bytes()

        before_bytes = b""
        if change.backup_path:
            backup_abs = self._backup_abs(user, change.run_id, change.backup_path)
            if backup_abs.exists():
                before_bytes = backup_abs.read_bytes()

        if change.op == "create" and not change.backup_path:
            if not _looks_like_text(change.file_path, after_bytes):
                return DiffPayload(
                    change_id=change.id,
                    path=change.file_path,
                    kind="binary",
                    before_hash=change.before_hash,
                    after_hash=change.after_hash,
                    message="New binary file. Revert will delete it.",
                )
            after_text = _decode_text(after_bytes)
            lines = after_text.splitlines(keepends=True)
            truncated = False
            if len(after_bytes) > _MAX_DIFF_BYTES or len(lines) > _MAX_DIFF_LINES:
                truncated = True
                lines = lines[:_MAX_DIFF_LINES]
            preview = "".join(lines)
            if truncated:
                preview += "\n… [truncated]"
            return DiffPayload(
                change_id=change.id,
                path=change.file_path,
                kind="create",
                unified_diff=f"+++ {change.file_path} (new file)\n{preview}",
                truncated=truncated,
                after_hash=change.after_hash,
                message="New file created by the agent.",
            )

        sample = before_bytes[:8192] or after_bytes[:8192]
        if not _looks_like_text(change.file_path, sample):
            return DiffPayload(
                change_id=change.id,
                path=change.file_path,
                kind="binary",
                before_hash=change.before_hash,
                after_hash=change.after_hash,
                message="Binary file; use Revert to restore the pre-write backup.",
            )

        before_text = _decode_text(before_bytes)
        after_text = _decode_text(after_bytes)
        before_lines = before_text.splitlines(keepends=True)
        after_lines = after_text.splitlines(keepends=True)
        truncated = False
        if (
            len(before_bytes) + len(after_bytes) > _MAX_DIFF_BYTES
            or len(before_lines) + len(after_lines) > _MAX_DIFF_LINES * 2
        ):
            truncated = True
            before_lines = before_lines[:_MAX_DIFF_LINES]
            after_lines = after_lines[:_MAX_DIFF_LINES]

        diff_lines = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{change.file_path}",
                tofile=f"b/{change.file_path}",
            )
        )
        unified = "".join(diff_lines)
        if not unified.strip():
            unified = f"(no textual differences for {change.file_path})\n"
        if truncated:
            unified += "\n… [diff truncated]\n"
        return DiffPayload(
            change_id=change.id,
            path=change.file_path,
            kind="text",
            unified_diff=unified,
            truncated=truncated,
            before_hash=change.before_hash,
            after_hash=change.after_hash,
        )

    async def revert_change(self, user: User, change_id: str) -> RevertResult:
        change = await self.get_owned_change(user, change_id)
        if change.status == "reverted":
            return RevertResult(ok=True, change_id=change.id, action="already_reverted")

        target = self._resolve_target(user, change.file_path)
        action: Literal["restored", "deleted"]

        if change.backup_path:
            try:
                self._snap.restore(user, change.run_id, change.backup_path, target)
            except FileNotFoundError as e:
                raise FileReviewError(str(e), status=400) from e
            except ValueError as e:
                raise FileReviewError(str(e), status=403) from e
            action = "restored"
        elif change.op == "create":
            if target.exists():
                if not target.is_file():
                    raise FileReviewError("Cannot revert: path is not a file", status=400)
                target.unlink()
            action = "deleted"
        else:
            raise FileReviewError(
                "Cannot revert: no backup available for this change",
                status=400,
            )

        marked = await self._dao.mark_reverted(change.run_id, change.id)
        if not marked:
            logger.error(
                "revert: disk updated but mark_reverted failed change_id=%s",
                change.id,
            )
            raise FileReviewError(
                "File restored but failed to update journal; refresh and check status.",
                status=500,
            )
        return RevertResult(ok=True, change_id=change.id, action=action)
