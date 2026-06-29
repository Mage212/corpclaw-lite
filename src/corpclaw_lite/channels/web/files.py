# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from mimetypes import guess_type
from pathlib import Path
from typing import Any

import anyio

from corpclaw_lite.channels.telegram.file_manager import is_protected_delete_target
from corpclaw_lite.channels.telegram.upload import is_safe_extension, sanitize_filename
from corpclaw_lite.security.path_validator import _reject_symlink_ancestors

__all__ = [
    "WebFileEntry",
    "build_tree",
    "copy_paths",
    "delete_path",
    "delete_paths",
    "list_directory",
    "list_recent_files",
    "make_directory",
    "move_paths",
    "preview_file",
    "rename_path",
    "resolve_workspace_path",
    "save_upload",
    "save_upload_stream",
    "search_files",
]

_MAX_PATH_CHARS = 1024
_MAX_SEARCH_CHARS = 120
_MAX_TREE_DEPTH = 6
_MAX_PREVIEW_BYTES = 256 * 1024
_MAX_RECENT_SCAN = 5000
_TEXT_EXTENSIONS = {
    ".csv",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass(slots=True)
class WebFileEntry:
    name: str
    path: str
    is_dir: bool
    size_bytes: int
    modified_at: str
    kind: str
    extension: str
    mime_type: str | None
    protected: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "kind": self.kind,
            "extension": self.extension,
            "mime_type": self.mime_type,
            "protected": self.protected,
        }


def resolve_workspace_path(workspace: Path, raw_path: str | None) -> Path:
    """Resolve a web path under the user's workspace."""
    relative = _clean_relative_path(raw_path)
    target = (workspace / relative).resolve()
    ws = workspace.resolve()
    if target != ws and ws not in target.parents:
        raise PermissionError("Path escapes user workspace")
    return target


def _clean_relative_path(raw_path: str | None) -> str:
    relative = (raw_path or ".").strip().lstrip("/\\")
    if "\x00" in relative:
        raise ValueError("Invalid path")
    if len(relative) > _MAX_PATH_CHARS:
        raise ValueError("Path is too long")
    return relative or "."


def _relative(workspace: Path, path: Path) -> str:
    rel = path.resolve().relative_to(workspace.resolve())
    return str(rel).replace("\\", "/")


def _entry_kind(path: Path) -> str:
    if path.is_dir():
        return "folder"
    suffix = path.suffix.lower()
    mime_type, _encoding = guess_type(path.name)
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return "image"
    if suffix in {".xlsx", ".xls", ".csv"}:
        return "spreadsheet"
    if suffix == ".pdf":
        return "pdf"
    if suffix in _TEXT_EXTENSIONS or (mime_type is not None and mime_type.startswith("text/")):
        return "text"
    if suffix in {".zip", ".tar", ".gz", ".7z"}:
        return "archive"
    return "file"


def _build_entry(workspace: Path, child: Path) -> WebFileEntry:
    stat = child.stat()
    mime_type, _encoding = guess_type(child.name)
    return WebFileEntry(
        name=child.name,
        path=_relative(workspace, child),
        is_dir=child.is_dir(),
        size_bytes=0 if child.is_dir() else stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        kind=_entry_kind(child),
        extension=child.suffix.lower(),
        mime_type=mime_type,
        protected=is_protected_delete_target(child, workspace),
    )


def _sort_entries(entries: list[WebFileEntry], sort: str, order: str) -> list[WebFileEntry]:
    sort_key = sort if sort in {"name", "kind", "size", "modified"} else "name"
    reverse = order == "desc"

    def key(entry: WebFileEntry) -> tuple[bool, object]:
        if sort_key == "kind":
            return (not entry.is_dir, entry.kind.lower())
        if sort_key == "size":
            return (not entry.is_dir, entry.size_bytes)
        if sort_key == "modified":
            return (not entry.is_dir, entry.modified_at)
        return (not entry.is_dir, entry.name.lower())

    return sorted(entries, key=key, reverse=reverse)


async def list_directory(
    workspace: Path,
    raw_path: str | None = None,
    *,
    sort: str = "name",
    order: str = "asc",
) -> dict[str, object]:
    """List a user workspace directory."""
    path = resolve_workspace_path(workspace, raw_path)
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError("Directory not found")

    def _list() -> list[WebFileEntry]:
        entries: list[WebFileEntry] = []
        for child in path.iterdir():
            try:
                child.resolve().relative_to(workspace.resolve())
            except ValueError:
                continue
            entries.append(_build_entry(workspace, child))
        return _sort_entries(entries, sort, order)

    entries = await anyio.to_thread.run_sync(_list)
    current = _relative(workspace, path) if path != workspace.resolve() else ""
    return {"path": current, "entries": [entry.to_dict() for entry in entries]}


async def list_recent_files(workspace: Path, *, limit: int = 8) -> list[dict[str, object]]:
    """Return recently modified files in a user workspace."""
    safe_limit = max(1, min(limit, 20))

    def _list_recent() -> list[WebFileEntry]:
        entries: list[WebFileEntry] = []
        scanned = 0
        for child in workspace.rglob("*"):
            scanned += 1
            if scanned > _MAX_RECENT_SCAN:
                break
            try:
                child.resolve().relative_to(workspace.resolve())
            except ValueError:
                continue
            if child.is_file():
                entries.append(_build_entry(workspace, child))
        return sorted(entries, key=lambda entry: entry.modified_at, reverse=True)[:safe_limit]

    entries = await anyio.to_thread.run_sync(_list_recent)
    return [entry.to_dict() for entry in entries]


async def make_directory(workspace: Path, raw_parent: str | None, name: str) -> str:
    safe_name = sanitize_filename(name)
    if safe_name is None:
        raise ValueError("Invalid directory name")
    parent = resolve_workspace_path(workspace, raw_parent)
    target = (parent / safe_name).resolve()
    resolve_workspace_path(workspace, _relative(workspace, target))
    await anyio.to_thread.run_sync(lambda: target.mkdir(parents=True, exist_ok=True))
    return _relative(workspace, target)


async def rename_path(workspace: Path, raw_path: str, new_name: str) -> str:
    target = resolve_workspace_path(workspace, raw_path)
    if not target.exists():
        raise FileNotFoundError("Path not found")
    if target == workspace.resolve() or is_protected_delete_target(target, workspace):
        raise PermissionError("Protected path cannot be renamed")
    safe_name = sanitize_filename(new_name)
    if safe_name is None:
        raise ValueError("Invalid filename")
    if target.is_file() and not is_safe_extension(safe_name):
        raise ValueError("File type is not allowed")
    destination = (target.parent / safe_name).resolve()
    resolve_workspace_path(workspace, _relative(workspace, destination))
    if destination.exists():
        raise FileExistsError("Target already exists")
    ws_root = workspace.resolve()
    await anyio.to_thread.run_sync(
        lambda: (
            # B-072: re-validate source + destination symlinks right before rename.
            _reject_symlink_ancestors(ws_root, target.resolve(), raw_path),
            _reject_symlink_ancestors(ws_root, destination.resolve(), new_name),
            target.rename(destination),
        )
    )
    return _relative(workspace, destination)


def _unique_destination(parent: Path, name: str) -> Path:
    destination = (parent / name).resolve()
    if not destination.exists():
        return destination
    stem = destination.stem
    suffix = destination.suffix
    counter = 1
    while destination.exists():
        destination = (parent / f"{stem}_{counter}{suffix}").resolve()
        counter += 1
    return destination


async def move_paths(workspace: Path, raw_paths: list[str], target_dir: str | None) -> list[str]:
    parent = resolve_workspace_path(workspace, target_dir)
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("Target directory not found")

    def _move() -> list[str]:
        moved: list[str] = []
        for raw_path in raw_paths:
            source = resolve_workspace_path(workspace, raw_path)
            if not source.exists():
                raise FileNotFoundError("Path not found")
            if source == workspace.resolve() or is_protected_delete_target(source, workspace):
                raise PermissionError("Protected path cannot be moved")
            if parent == source or source in parent.parents:
                raise ValueError("Cannot move a directory into itself")
            destination = _unique_destination(parent, source.name)
            resolve_workspace_path(workspace, _relative(workspace, destination))
            # B-072: re-validate source + destination symlinks right before rename.
            _reject_symlink_ancestors(ws_root, source.resolve(), raw_path)
            _reject_symlink_ancestors(ws_root, destination.resolve(), destination.name)
            source.rename(destination)
            moved.append(_relative(workspace, destination))
        return moved

    ws_root = workspace.resolve()
    return await anyio.to_thread.run_sync(_move)


async def copy_paths(workspace: Path, raw_paths: list[str], target_dir: str | None) -> list[str]:
    parent = resolve_workspace_path(workspace, target_dir)
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("Target directory not found")

    def _copy() -> list[str]:
        copied: list[str] = []
        for raw_path in raw_paths:
            source = resolve_workspace_path(workspace, raw_path)
            if not source.exists():
                raise FileNotFoundError("Path not found")
            if source == workspace.resolve() or is_protected_delete_target(source, workspace):
                raise PermissionError("Protected path cannot be copied")
            destination = _unique_destination(parent, source.name)
            resolve_workspace_path(workspace, _relative(workspace, destination))
            # B-072: re-validate source symlink right before copy. symlinks=False
            # dereferences any symlinks inside a copied tree (copies content,
            # not the link), so a symlink pointing outside is followed only if
            # its target already passed the ancestor-walk check.
            _reject_symlink_ancestors(ws_root, source.resolve(), raw_path)
            _reject_symlink_ancestors(ws_root, destination.resolve(), destination.name)
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=False)
            else:
                shutil.copy2(source, destination)
            copied.append(_relative(workspace, destination))
        return copied

    ws_root = workspace.resolve()
    return await anyio.to_thread.run_sync(_copy)


async def delete_path(workspace: Path, raw_path: str, *, recursive: bool = True) -> None:
    target = resolve_workspace_path(workspace, raw_path)
    if not target.exists():
        raise FileNotFoundError("Path not found")
    if target == workspace.resolve() or is_protected_delete_target(target, workspace):
        raise PermissionError("Protected path cannot be deleted")
    ws_root = workspace.resolve()

    def _delete() -> None:
        # B-072: re-validate inside the thread, right before the op, to close
        # the TOCTOU window between resolve_workspace_path and the destructive
        # rmtree/unlink (a symlink swapped in between would otherwise escape).
        _reject_symlink_ancestors(ws_root, target.resolve(), raw_path)
        if target.is_dir():
            if not recursive and any(target.iterdir()):
                raise ValueError("Directory is not empty")
            shutil.rmtree(target)
        else:
            target.unlink()

    await anyio.to_thread.run_sync(_delete)


async def delete_paths(workspace: Path, raw_paths: list[str], *, recursive: bool) -> list[str]:
    deleted: list[str] = []
    for raw_path in raw_paths:
        await delete_path(workspace, raw_path, recursive=recursive)
        deleted.append(raw_path)
    return deleted


def _clean_search_query(raw_query: str | None) -> str:
    query = (raw_query or "").strip()
    if "\x00" in query:
        raise ValueError("Invalid search query")
    if len(query) > _MAX_SEARCH_CHARS:
        raise ValueError("Search query is too long")
    if not query:
        raise ValueError("Missing search query")
    return query.lower()


async def search_files(
    workspace: Path,
    query: str | None,
    *,
    limit: int = 100,
) -> dict[str, object]:
    needle = _clean_search_query(query)
    safe_limit = max(1, min(limit, 500))

    def _search() -> list[WebFileEntry]:
        matches: list[WebFileEntry] = []
        for child in workspace.rglob("*"):
            try:
                child.resolve().relative_to(workspace.resolve())
            except ValueError:
                continue
            if needle in child.name.lower():
                matches.append(_build_entry(workspace, child))
                if len(matches) >= safe_limit:
                    break
        return _sort_entries(matches, "name", "asc")

    entries = await anyio.to_thread.run_sync(_search)
    return {"query": query or "", "entries": [entry.to_dict() for entry in entries]}


async def build_tree(workspace: Path, *, depth: int = 3) -> dict[str, object]:
    safe_depth = max(1, min(depth, _MAX_TREE_DEPTH))

    def _node(path: Path, current_depth: int) -> dict[str, object]:
        entry = _build_entry(workspace, path) if path != workspace.resolve() else None
        children: list[dict[str, object]] = []
        if current_depth < safe_depth:
            children_iter = sorted(
                path.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
            for child in children_iter:
                try:
                    child.resolve().relative_to(workspace.resolve())
                except ValueError:
                    continue
                if child.is_dir():
                    children.append(_node(child, current_depth + 1))
        if entry is None:
            return {"name": "workspace", "path": "", "is_dir": True, "children": children}
        data = entry.to_dict()
        data["children"] = children
        return data

    return await anyio.to_thread.run_sync(lambda: _node(workspace.resolve(), 0))


async def preview_file(workspace: Path, raw_path: str) -> dict[str, object]:
    target = resolve_workspace_path(workspace, raw_path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError("File not found")
    entry = _build_entry(workspace, target)
    if entry.kind == "image":
        return {"type": "image", "entry": entry.to_dict()}
    if entry.kind != "text":
        return {"type": "metadata", "entry": entry.to_dict()}
    if target.stat().st_size > _MAX_PREVIEW_BYTES:
        return {
            "type": "text",
            "entry": entry.to_dict(),
            "truncated": True,
            "content": "",
            "error": "File is too large for preview",
        }

    data = await anyio.Path(target).read_bytes()
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = data.decode("cp1251", errors="replace")
    return {"type": "text", "entry": entry.to_dict(), "truncated": False, "content": content}


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

    target = _prepare_upload_target(workspace, safe_name, target_dir)

    await anyio.Path(target).write_bytes(data)
    return _relative(workspace, target)


def _prepare_upload_target(workspace: Path, safe_name: str, target_dir: str | None = None) -> Path:
    parent = resolve_workspace_path(workspace, target_dir)
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("Upload directory not found")
    target = (parent / safe_name).resolve()
    resolve_workspace_path(workspace, _relative(workspace, target))
    return _unique_destination(parent, safe_name)


async def save_upload_stream(
    *,
    workspace: Path,
    filename: str,
    field: Any,
    max_bytes: int,
    target_dir: str | None = None,
) -> str:
    safe_name = sanitize_filename(filename)
    if safe_name is None:
        raise ValueError("Invalid filename")
    if not is_safe_extension(safe_name):
        raise ValueError("File type is not allowed")

    target = _prepare_upload_target(workspace, safe_name, target_dir)
    written = 0
    async with await anyio.open_file(target, "wb") as output:
        while True:
            chunk = await field.read_chunk(size=64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                await output.aclose()
                target.unlink(missing_ok=True)
                raise ValueError("File too large")
            await output.write(chunk)
    return _relative(workspace, target)
