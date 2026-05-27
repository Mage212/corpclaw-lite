# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import anyio

from corpclaw_lite.channels.telegram.file_manager import is_protected_delete_target
from corpclaw_lite.channels.telegram.upload import is_safe_extension, sanitize_filename

__all__ = [
    "WebFileEntry",
    "delete_path",
    "list_directory",
    "make_directory",
    "resolve_workspace_path",
    "save_upload",
]


@dataclass(slots=True)
class WebFileEntry:
    name: str
    path: str
    is_dir: bool
    size_bytes: int
    modified_at: str

    def to_dict(self) -> dict[str, str | int | bool]:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
        }


def resolve_workspace_path(workspace: Path, raw_path: str | None) -> Path:
    """Resolve a web path under the user's workspace."""
    relative = (raw_path or ".").strip().lstrip("/\\")
    target = (workspace / relative).resolve()
    ws = workspace.resolve()
    if target != ws and ws not in target.parents:
        raise PermissionError("Path escapes user workspace")
    return target


def _relative(workspace: Path, path: Path) -> str:
    rel = path.resolve().relative_to(workspace.resolve())
    return str(rel).replace("\\", "/")


async def list_directory(workspace: Path, raw_path: str | None = None) -> dict[str, object]:
    """List a user workspace directory."""
    path = resolve_workspace_path(workspace, raw_path)
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError("Directory not found")

    def _list() -> list[WebFileEntry]:
        entries: list[WebFileEntry] = []
        children = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
        for child in children:
            try:
                child.resolve().relative_to(workspace.resolve())
            except ValueError:
                continue
            stat = child.stat()
            entries.append(
                WebFileEntry(
                    name=child.name,
                    path=_relative(workspace, child),
                    is_dir=child.is_dir(),
                    size_bytes=0 if child.is_dir() else stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                )
            )
        return entries

    entries = await anyio.to_thread.run_sync(_list)
    current = _relative(workspace, path) if path != workspace.resolve() else ""
    return {"path": current, "entries": [entry.to_dict() for entry in entries]}


async def make_directory(workspace: Path, raw_parent: str | None, name: str) -> str:
    safe_name = sanitize_filename(name)
    if safe_name is None:
        raise ValueError("Invalid directory name")
    parent = resolve_workspace_path(workspace, raw_parent)
    target = (parent / safe_name).resolve()
    resolve_workspace_path(workspace, _relative(workspace, target))
    await anyio.to_thread.run_sync(lambda: target.mkdir(parents=True, exist_ok=True))
    return _relative(workspace, target)


async def delete_path(workspace: Path, raw_path: str) -> None:
    target = resolve_workspace_path(workspace, raw_path)
    if not target.exists():
        raise FileNotFoundError("Path not found")
    if target == workspace.resolve() or is_protected_delete_target(target, workspace):
        raise PermissionError("Protected path cannot be deleted")

    def _delete() -> None:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    await anyio.to_thread.run_sync(_delete)


async def save_upload(
    *,
    workspace: Path,
    filename: str,
    data: bytes,
    max_bytes: int,
    target_dir: str | None = None,
) -> str:
    if len(data) > max_bytes:
        raise ValueError("File too large")
    safe_name = sanitize_filename(filename)
    if safe_name is None:
        raise ValueError("Invalid filename")
    if not is_safe_extension(safe_name):
        raise ValueError("File type is not allowed")

    parent = resolve_workspace_path(workspace, target_dir)
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("Upload directory not found")
    target = (parent / safe_name).resolve()
    resolve_workspace_path(workspace, _relative(workspace, target))

    base = target.stem
    ext = target.suffix
    counter = 1
    while target.exists():
        target = (parent / f"{base}_{counter}{ext}").resolve()
        counter += 1

    await anyio.Path(target).write_bytes(data)
    return _relative(workspace, target)
