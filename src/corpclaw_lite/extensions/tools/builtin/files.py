# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false, reportUnknownArgumentType=false
"""File system tools: read, write, edit, list, search."""

from __future__ import annotations

import os
import re
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

import anyio

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.security.path_validator import resolve_and_validate_path
from corpclaw_lite.utils.fs import atomic_write_text

__all__ = [
    "EditFileTool",
    "IMAGE_EXTENSIONS",
    "ListFilesTool",
    "ReadFileTool",
    "SearchFilesTool",
    "WriteFileTool",
    "resolve_and_validate_path",
]

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Skip files larger than 1 MB in search to prevent memory spikes
_MAX_FILE_SEARCH_BYTES = 1_048_576

# NOTE: resolve_and_validate_path is now provided by
# corpclaw_lite.security.path_validator (B-059). It is re-exported above for
# backward compatibility with existing callers (`from ...files import ...`).


class ReadFileTool(Tool):
    """Tool to read the contents of a text file."""

    name = "read_file"
    description = "Read the contents of a text file. Not for images."
    params = [
        ToolParam(name="path", type="string", description="Relative or absolute path to the file"),
    ]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        if not isinstance(path, str):
            return "Error: missing required parameter 'path'"

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists():
                return f"Error: File '{resolved}' does not exist."
            if not resolved.is_file():
                return f"Error: '{resolved}' is not a file."
            if resolved.suffix.lower() in IMAGE_EXTENSIONS:
                return "Error: Use read_image tool for image files."
            size = resolved.stat().st_size
            if size > _MAX_FILE_SEARCH_BYTES:
                return (
                    f"Error: File '{resolved}' is too large to read directly "
                    f"({size} bytes, max {_MAX_FILE_SEARCH_BYTES}). "
                    "Use search_files or a narrower file-specific tool instead."
                )

            content = await anyio.to_thread.run_sync(partial(resolved.read_text, encoding="utf-8"))
            return content
        except Exception as e:
            return f"Error reading file '{path}': {e}"


class WriteFileTool(Tool):
    """Tool to create or overwrite a text file."""

    name = "write_file"
    description = "Overwrite an existing text file or create a new one with full content."
    params = [
        ToolParam(name="path", type="string", description="Relative or absolute path to the file"),
        ToolParam(name="content", type="string", description="Content to write"),
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        content = kwargs.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return "Error: missing required 'path' or 'content'"

        try:
            resolved = resolve_and_validate_path(path)
            await anyio.to_thread.run_sync(
                partial(resolved.parent.mkdir, parents=True, exist_ok=True)
            )
            await anyio.to_thread.run_sync(partial(atomic_write_text, resolved, content))
            return f"Successfully wrote {len(content)} chars to '{resolved}'"
        except Exception as e:
            return f"Error writing file '{path}': {e}"


class EditFileTool(Tool):
    """Tool to edit specific lines of a text file using exact search/replace."""

    name = "edit_file"
    description = "Edit a file by replacing old_text with new_text exactly."
    params = [
        ToolParam(name="path", type="string", description="Path to the file"),
        ToolParam(name="old_text", type="string", description="Text to find strictly"),
        ToolParam(name="new_text", type="string", description="Text to replace with"),
        ToolParam(
            name="max_replacements",
            type="integer",
            description="Max occurrences to replace (default: 1). Use 0 for unlimited.",
            required=False,
        ),
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        old_text = kwargs.get("old_text")
        new_text = kwargs.get("new_text")

        if (
            not isinstance(path, str)
            or not isinstance(old_text, str)
            or not isinstance(new_text, str)
        ):
            return "Error: missing required params 'path', 'old_text', 'new_text'"

        max_repl_raw = kwargs.get("max_replacements", 1)
        max_repl = int(max_repl_raw) if isinstance(max_repl_raw, (int, float)) else 1

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists():
                return f"Error: File '{resolved}' does not exist."

            content = await anyio.to_thread.run_sync(partial(resolved.read_text, encoding="utf-8"))
            if old_text not in content:
                return "Error: 'old_text' exactly not found in file."

            total = content.count(old_text)
            if max_repl == 0:
                # Unlimited: replace all
                content = content.replace(old_text, new_text)
                applied = total
            else:
                content = content.replace(old_text, new_text, max_repl)
                applied = min(total, max_repl)

            await anyio.to_thread.run_sync(partial(atomic_write_text, resolved, content))
            return f"Edited '{resolved}' ({applied} of {total} occurrence(s) replaced)."
        except Exception as e:
            return f"Error editing file '{path}': {e}"


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"


class ListFilesTool(Tool):
    """Tool to list files in a directory with size and modification date."""

    name = "list_files"
    description = (
        "List all files and subdirectories in a specific directory. "
        "Format output as plain lists — NEVER use tables or group by file type. "
        "Always wrap file/directory names in backticks (``). "
        "Group into two sections: files first, then directories. "
        "Example format:\n"
        "В директории найдено N файлов и M директорий:\n\n"
        "**Файлы:**\n"
        "- `filename.ext` — 4.9 KB\n"
        "- `other.txt` — 128 B\n\n"
        "**Директории:**\n"
        "- `folder_name`"
    )
    params = [
        ToolParam(name="path", type="string", description="Path to the directory (empty for root)"),
    ]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path") or "."

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists() or not resolved.is_dir():
                return f"Error: '{resolved}' is not a valid directory."

            def _list_dir() -> list[str]:
                items: list[str] = []
                for item in sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name)):
                    try:
                        stat = item.stat()
                        mdate = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
                        if item.is_dir():
                            try:
                                child_count = sum(1 for _ in item.iterdir())
                                items.append(f"[DIR]  {item.name:<30} ({child_count})  {mdate}")
                            except PermissionError:
                                items.append(f"[DIR]  {item.name:<30}              {mdate}")
                        else:
                            size_str = _format_size(stat.st_size)
                            items.append(f"[FILE] {item.name:<30} {size_str:>8}  {mdate}")
                    except OSError:
                        items.append(f"[{'DIR' if item.is_dir() else 'FILE'}] {item.name}")
                return items

            items = await anyio.to_thread.run_sync(_list_dir)
            return "\n".join(items) if items else "Directory is empty."
        except Exception as e:
            return f"Error listing directory '{path}': {e}"


class SearchFilesTool(Tool):
    """Tool to search for text patterns across files in a directory."""

    name = "search_files"
    description = "Search for a regex pattern within files in a directory."
    params = [
        ToolParam(name="path", type="string", description="Directory to search in"),
        ToolParam(name="pattern", type="string", description="Regex pattern to search for"),
    ]
    risk_level = RiskLevel.LOW

    max_results: int = 100

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path") or "."
        pattern = kwargs.get("pattern")
        if not isinstance(path, str) or not isinstance(pattern, str):
            return "Error: 'pattern' and 'path' must be strings."

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists() or not resolved.is_dir():
                return f"Error: '{resolved}' is not a valid directory."

            def _search() -> list[str]:
                if len(pattern) > 200:
                    return ["Error: pattern too long (max 200 chars)."]
                try:
                    regex = re.compile(pattern)
                except re.error as e:
                    return [f"Error: invalid regex pattern: {e}"]

                results: list[str] = []
                _skip_dirs = {
                    ".git",
                    "__pycache__",
                    "node_modules",
                    ".venv",
                    "venv",
                    ".mypy_cache",
                    ".ruff_cache",
                    ".pytest_cache",
                    ".tox",
                    "dist",
                    "build",
                    ".eggs",
                }
                for root, dirs, files in os.walk(resolved):
                    # Prune system/hidden directories in-place (prevents os.walk from descending)
                    dirs[:] = [d for d in dirs if d not in _skip_dirs and not d.startswith(".")]
                    for file_name in files:
                        file_path = Path(root) / file_name
                        if (
                            file_path.name.startswith(".")
                            or file_path.suffix.lower() in IMAGE_EXTENSIONS
                        ):
                            continue

                        try:
                            if file_path.stat().st_size > _MAX_FILE_SEARCH_BYTES:
                                continue
                            matches: list[str] = []
                            with open(file_path, encoding="utf-8", errors="ignore") as fh:
                                for i, line in enumerate(fh, start=1):
                                    if regex.search(line):
                                        matches.append(f"{i}: {line.strip()[:100]}")

                            if matches:
                                rel_path = file_path.relative_to(resolved)
                                results.append(f"--- {rel_path.as_posix()} ---")
                                results.extend(matches)
                                if len(results) > self.max_results:
                                    results.append("... search truncated.")
                                    return results
                        except Exception as e:
                            if len(results) <= self.max_results:
                                rel_path = file_path.relative_to(resolved)
                                results.append(
                                    f"Skipped unreadable file {rel_path.as_posix()}: "
                                    f"{type(e).__name__}: {e}"
                                )
                return results

            results = await anyio.to_thread.run_sync(_search)
            return "\n".join(results) if results else "No matches found."
        except Exception as e:
            return f"Error searching files in '{path}': {e}"
