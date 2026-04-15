"""diff_text — compare two texts or files and show differences.

Supports three diff modes:
  - unified: standard unified diff (default)
  - words:   line-level diff with intra-line change markers
  - chars:   character-level diff
"""

from __future__ import annotations

import difflib
from typing import Any

import anyio

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path

__all__ = ["DiffTextTool"]

_MAX_DIFF_CHARS = 50_000


def _read_if_path(text_or_path: str) -> str:
    """Read file contents if *text_or_path* is a workspace path, else return as-is."""
    try:
        resolved = resolve_and_validate_path(text_or_path)
    except (PermissionError, ValueError):
        # Not a valid path — treat as literal text.
        return text_or_path

    if resolved.is_file():
        return resolved.read_text(encoding="utf-8", errors="replace")

    # Path resolved but is not a file — treat original string as literal text.
    return text_or_path


def _unified_diff(
    source_lines: list[str],
    target_lines: list[str],
    context_lines: int,
) -> str:
    return "\n".join(difflib.unified_diff(source_lines, target_lines, n=context_lines, lineterm=""))


def _words_diff(
    source_lines: list[str],
    target_lines: list[str],
) -> str:
    return "\n".join(difflib.ndiff(source_lines, target_lines))


def _chars_diff(source: str, target: str) -> str:
    """Character-level diff using ndiff on single-char lines."""
    source_chars = list(source.replace("\n", "\u00b6"))  # pilcrow for newlines
    target_chars = list(target.replace("\n", "\u00b6"))
    return "\n".join(difflib.ndiff(source_chars, target_chars))


def _compute_diff(
    source: str,
    target: str,
    mode: str,
    context_lines: int,
) -> str:
    source_lines = source.splitlines(keepends=True)
    target_lines = target.splitlines(keepends=True)

    if mode == "unified":
        result = _unified_diff(source_lines, target_lines, context_lines)
    elif mode == "words":
        result = _words_diff(source_lines, target_lines)
    elif mode == "chars":
        result = _chars_diff(source, target)
    else:
        result = _unified_diff(source_lines, target_lines, context_lines)

    if len(result) > _MAX_DIFF_CHARS:
        result = result[:_MAX_DIFF_CHARS] + f"\n... (truncated at {_MAX_DIFF_CHARS} chars)"

    return result or "No differences found."


class DiffTextTool(Tool):
    """Compare two texts or files and show their differences."""

    name = "diff_text"
    description = (
        "Compare two texts or files and show their differences. "
        "Accepts file paths (resolved in workspace) or literal text strings."
    )
    params = [
        ToolParam(
            name="source",
            type="string",
            description="First text or file path",
        ),
        ToolParam(
            name="target",
            type="string",
            description="Second text or file path",
        ),
        ToolParam(
            name="mode",
            type="string",
            description="Diff mode: unified (default), words, or chars",
            required=False,
            enum=["unified", "words", "chars"],
        ),
        ToolParam(
            name="context_lines",
            type="integer",
            description="Context lines around changes in unified mode (default: 3)",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        source_raw = kwargs.get("source", "")
        target_raw = kwargs.get("target", "")
        mode = kwargs.get("mode", "unified") or "unified"
        context_lines = kwargs.get("context_lines", 3) or 3

        if not source_raw or not target_raw:
            return "Error: both 'source' and 'target' are required."

        try:
            source_text = _read_if_path(source_raw)
            target_text = _read_if_path(target_raw)
            return await anyio.to_thread.run_sync(
                _compute_diff, source_text, target_text, mode, context_lines
            )
        except Exception as e:
            return f"Error: {e}"
