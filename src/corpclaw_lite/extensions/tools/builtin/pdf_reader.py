"""pdf_reader — extract text content from PDF files.

Uses pypdf to extract text from PDF files with page range support.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path
from corpclaw_lite.utils.async_helpers import run_in_thread

__all__ = ["PdfReaderTool"]

_MAX_CHARS = 50_000


def _parse_page_range(pages_str: str, total_pages: int) -> list[int]:
    """Parse a page range string into 0-based page indices.

    Supported formats:
      - "all" → all pages
      - "1-5" → pages 1 through 5
      - "1,3,5" → specific pages
      - "1-3,5,7-9" → mixed
    """
    if pages_str.strip().lower() == "all":
        return list(range(total_pages))

    indices: set[int] = set()
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            start = int(start_str.strip()) - 1  # Convert to 0-based.
            end = int(end_str.strip())  # Inclusive, 1-based.
            indices.update(range(max(0, start), min(end, total_pages)))
        else:
            idx = int(part) - 1  # 0-based.
            if 0 <= idx < total_pages:
                indices.add(idx)

    return sorted(indices)


def _extract_text(path: Path, pages: str, max_chars: int) -> str:
    import pypdf

    reader = pypdf.PdfReader(str(path))

    if reader.is_encrypted:
        return "Error: PDF is password-protected."

    total_pages = len(reader.pages)
    if total_pages == 0:
        return "Error: PDF has no pages."

    try:
        page_indices = _parse_page_range(pages, total_pages)
    except (ValueError, IndexError):
        return f"Error: Invalid page range '{pages}'. Use 'all', '1-5', or '1,3,5'."

    parts: list[str] = []
    total_chars = 0
    for idx in page_indices:
        if idx >= total_pages:
            continue
        page_text = reader.pages[idx].extract_text() or ""
        header = f"--- Page {idx + 1} ---\n"
        parts.append(header + page_text)
        total_chars += len(header) + len(page_text)
        if total_chars >= max_chars:
            break

    result = "\n\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n... (truncated at {max_chars} chars)"

    info = f"PDF: {path.name} | Total pages: {total_pages} | Extracted: {len(page_indices)}"
    return f"{info}\n\n{result}"


class PdfReaderTool(Tool):
    """Extract text content from PDF files."""

    name = "pdf_reader"
    description = (
        "Extract text content from PDF files. Supports page ranges like 'all', '1-5', '1,3,5'."
    )
    params = [
        ToolParam(
            name="path",
            type="string",
            description="Path to the PDF file",
        ),
        ToolParam(
            name="pages",
            type="string",
            description="Page range: 'all', '1-5', '1,3,5' (default: all)",
            required=False,
        ),
        ToolParam(
            name="max_chars",
            type="integer",
            description=f"Maximum characters to extract (default: {_MAX_CHARS})",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        path_str = kwargs.get("path", "")
        pages = kwargs.get("pages", "all") or "all"
        max_chars = kwargs.get("max_chars", _MAX_CHARS) or _MAX_CHARS

        if not path_str:
            return "Error: 'path' is required."

        try:
            resolved = resolve_and_validate_path(path_str)
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.is_file():
            return f"Error: File not found: {path_str}"

        if resolved.suffix.lower() != ".pdf":
            return f"Error: Not a PDF file: {path_str}"

        return await run_in_thread(_extract_text, resolved, pages, max_chars)
