"""pdf_reader — extract text content from PDF files.

Uses pypdf to extract text from PDF files with page range support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.builtin.files import resolve_and_validate_path
from corpclaw_lite.utils.async_helpers import run_in_thread

__all__ = ["PdfReaderTool", "_sanitize_pdf_text"]

_MAX_CHARS = 50_000
# When saving to a file (output_path), extract the entire document without
# truncation — the file is what the user wants, not a context-window-sized chunk.
_MAX_INT = 2**31 - 1
_ALLOWED_OUTPUT_SUFFIXES = {".md", ".markdown", ".txt"}
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_HYPHENATED_LINE_BREAK_RE = re.compile(r"(?<=\w)-\n(?=\w)")
_TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class SanitizedPdfText:
    text: str
    removed_control_chars: int


@dataclass(frozen=True)
class PdfExtractionResult:
    text: str
    total_pages: int
    requested_pages: int
    extracted_pages: int
    removed_control_chars: int
    truncated: bool


def _sanitize_pdf_text(text: str) -> SanitizedPdfText:
    """Clean PDF extraction artifacts that are unsafe for LLM/tool contexts."""
    removed_control_chars = len(_CONTROL_CHARS_RE.findall(text))
    cleaned = _CONTROL_CHARS_RE.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _HYPHENATED_LINE_BREAK_RE.sub("", cleaned)
    cleaned = _TRAILING_SPACE_RE.sub("\n", cleaned)
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    return SanitizedPdfText(text=cleaned.strip(), removed_control_chars=removed_control_chars)


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


def _extract_text(path: Path, pages: str, max_chars: int) -> PdfExtractionResult | str:
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
    extracted_pages = 0
    removed_control_chars = 0
    for idx in page_indices:
        if idx >= total_pages:
            continue
        raw_page_text = reader.pages[idx].extract_text() or ""
        sanitized = _sanitize_pdf_text(raw_page_text)
        page_text = sanitized.text
        removed_control_chars += sanitized.removed_control_chars
        header = f"--- Page {idx + 1} ---\n"
        parts.append(header + page_text)
        total_chars += len(header) + len(page_text)
        extracted_pages += 1
        if total_chars >= max_chars:
            break

    result = "\n\n".join(parts)
    truncated = False
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n... (truncated at {max_chars} chars)"
        truncated = True

    return PdfExtractionResult(
        text=result,
        total_pages=total_pages,
        requested_pages=len(page_indices),
        extracted_pages=extracted_pages,
        removed_control_chars=removed_control_chars,
        truncated=truncated,
    )


def _format_extraction_result(path: Path, result: PdfExtractionResult) -> str:
    info = (
        f"PDF: {path.name} | Total pages: {result.total_pages} | "
        f"Extracted: {result.requested_pages}"
    )
    warnings: list[str] = []
    if result.removed_control_chars:
        warnings.append(
            f"Warning: removed {result.removed_control_chars} control character(s) from PDF text."
        )
    if result.truncated:
        warnings.append("Warning: extraction was truncated by max_chars.")
    warning_text = ("\n".join(warnings) + "\n\n") if warnings else ""
    return f"{info}\n\n{warning_text}{result.text}"


def _write_extraction_result(path: Path, output_path: Path, result: PdfExtractionResult) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.text + "\n", encoding="utf-8")
    lines = [
        f"Extracted PDF text from {path.name} to {output_path.name}",
        f"Total pages: {result.total_pages}",
        f"Requested pages: {result.requested_pages}",
        f"Extracted pages: {result.extracted_pages}",
        f"Characters written: {len(result.text)}",
    ]
    if result.removed_control_chars:
        lines.append(f"Removed control characters: {result.removed_control_chars}")
    if result.truncated:
        lines.append("Warning: extraction was truncated by max_chars.")
    return "\n".join(lines)


class PdfReaderTool(Tool):
    """Extract text content from PDF files."""

    name = "pdf_reader"
    description = (
        "Extract text content from PDF files. Supports page ranges like 'all', '1-5', '1,3,5'. "
        "When output_path is given, the ENTIRE document is saved to the file (no truncation) "
        "— use this for PDF-to-Markdown conversion in one step. Without output_path, the "
        "extracted text is returned (truncated to max_chars to protect context window)."
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
        ToolParam(
            name="output_path",
            type="string",
            description="Optional .md, .markdown, or .txt path to save cleaned extracted text",
            required=False,
        ),
    ]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        path_str = kwargs.get("path", "")
        pages = kwargs.get("pages", "all") or "all"
        max_chars = kwargs.get("max_chars", _MAX_CHARS) or _MAX_CHARS
        output_str = kwargs.get("output_path")

        if not path_str:
            return "Error: 'path' is required."
        if not isinstance(max_chars, int):
            return "Error: 'max_chars' must be an integer."

        try:
            resolved = resolve_and_validate_path(path_str)
        except PermissionError as e:
            return f"Error: {e}"

        if not resolved.is_file():
            return f"Error: File not found: {path_str}"

        if resolved.suffix.lower() != ".pdf":
            return f"Error: Not a PDF file: {path_str}"

        output_path: Path | None = None
        if output_str:
            if not isinstance(output_str, str):
                return "Error: 'output_path' must be a string."
            try:
                output_path = resolve_and_validate_path(output_str)
            except PermissionError as e:
                return f"Error: {e}"
            if output_path.suffix.lower() not in _ALLOWED_OUTPUT_SUFFIXES:
                allowed = ", ".join(sorted(_ALLOWED_OUTPUT_SUFFIXES))
                return f"Error: output_path must end with one of: {allowed}"

        # When saving to a file, extract the ENTIRE document — truncation makes
        # no sense for a file the model explicitly asked to create. The per-call
        # max_chars limit applies only to the summary string returned into the
        # agent context (to protect the context window). Without this, a
        # "convert PDF to .md" task loops for 4-6 iterations re-extracting
        # truncated chunks instead of completing in one step.
        extract_max = max_chars if output_path is None else _MAX_INT

        result = await run_in_thread(_extract_text, resolved, pages, extract_max)
        if isinstance(result, str):
            return result
        if output_path is not None:
            return await run_in_thread(_write_extraction_result, resolved, output_path, result)
        return _format_extraction_result(resolved, result)
