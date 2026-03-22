from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def resolve_and_validate_path(path_str: str) -> Path:
    """Resolve path to absolute and ensure it exists within allowed workspace boundaries.
    For Phase 1 (CLI mode), we allow access to CWD and its subdirectories.
    """
    # Use CWD for initial Phase 1 boundary
    workspace_root = Path.cwd().resolve()
    target_path = Path(path_str)

    # If it's relative, it relates to workspace_root
    if not target_path.is_absolute():
        target_path = workspace_root / target_path

    resolved = target_path.resolve()

    # Path traversal check (string startswith is bypassable — compare parents instead)
    if not (resolved == workspace_root or workspace_root in resolved.parents):
        raise PermissionError(
            f"Access denied: Path '{path_str}' is outside of workspace '{workspace_root}'."
        )

    return resolved


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

            return resolved.read_text(encoding="utf-8")
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
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} chars to '{resolved}'"
        except Exception as e:
            return f"Error writing file '{path}': {e}"


class EditFileTool(Tool):
    """Tool to edit specific lines of a text file using exact search/replace."""

    name = "edit_file"
    description = "Edit a file by replacing old_text with new_text exactly."
    params = [
        ToolParam(name="path", type="string", description="Path to the file"),
        ToolParam(name="old_text", type="string", description="Text to finding strictly"),
        ToolParam(name="new_text", type="string", description="Text to replace with"),
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

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists():
                return f"Error: File '{resolved}' does not exist."

            content = resolved.read_text(encoding="utf-8")
            if old_text not in content:
                return "Error: 'old_text' exactly not found in file."

            count = content.count(old_text)
            content = content.replace(old_text, new_text)
            resolved.write_text(content, encoding="utf-8")

            return f"Successfully edited file '{resolved}' (replaced {count} occurrence(s))."
        except Exception as e:
            return f"Error editing file '{path}': {e}"


class ListFilesTool(Tool):
    """Tool to list files in a directory."""

    name = "list_files"
    description = "List all files and subdirectories in a specific directory."
    params = [
        ToolParam(name="path", type="string", description="Path to the directory (empty for root)"),
    ]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path", ".")

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists() or not resolved.is_dir():
                return f"Error: '{resolved}' is not a valid directory."

            items: list[str] = []
            for item in resolved.iterdir():
                type_name = "DIR" if item.is_dir() else "FILE"
                items.append(f"[{type_name}] {item.name}")
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

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path", ".")
        pattern = kwargs.get("pattern")
        if not isinstance(path, str) or not isinstance(pattern, str):
            return "Error: 'pattern' and 'path' must be strings."

        try:
            resolved = resolve_and_validate_path(path)
            if not resolved.exists() or not resolved.is_dir():
                return f"Error: '{resolved}' is not a valid directory."

            regex = re.compile(pattern)
            results: list[str] = []

            for root, _, files in os.walk(resolved):
                for file_name in files:
                    file_path = Path(root) / file_name
                    # Skip hidden or large typical non-text files quickly
                    if (
                        file_path.name.startswith(".")
                        or file_path.suffix.lower() in IMAGE_EXTENSIONS
                    ):
                        continue

                    try:
                        content = file_path.read_text(encoding="utf-8", errors="ignore")
                        matches: list[str] = []
                        for i, line in enumerate(content.splitlines(), start=1):
                            if regex.search(line):
                                matches.append(f"{i}: {line.strip()[:100]}")

                        if matches:
                            rel_path = file_path.relative_to(resolved)
                            results.append(f"--- {rel_path} ---")
                            results.extend(matches)
                            if len(results) > 100:
                                results.append("... search truncated.")
                                return "\n".join(results)
                    except Exception:
                        pass  # Ignore read errors

            return "\n".join(results) if results else "No matches found."
        except Exception as e:
            return f"Error searching files in '{path}': {e}"
