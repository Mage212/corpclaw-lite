"""Inline attachment materialize + compose for add-to-context (B-094 / DC-013).

Materialized content is injected into the **next** user message (message-local,
D-087). No Pin / compressor marked-blocks here (B-095).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from corpclaw_lite.agent.budget_gate import (
    BudgetDecision,
    BudgetGateResult,
    estimate_content_budget,
)
from corpclaw_lite.llm.tokenizer_client import TokenizerClient, estimate_tokens_heuristic

__all__ = [
    "CHUNKED_TEXT_TOKEN_BUDGET",
    "DEFAULT_SPREADSHEET_ROW_LIMIT",
    "InlineAttachment",
    "MaterializeError",
    "compose_attachments_with_workbook_brief",
    "compose_inline_attachments",
    "materialize_attachment",
    "truncate_text_to_token_budget",
    "ui_placeholder_for_attachments",
]

logger = logging.getLogger(__name__)

CHUNKED_TEXT_TOKEN_BUDGET = 4000
DEFAULT_SPREADSHEET_ROW_LIMIT = 50
_MAX_TEXT_BYTES = 256 * 1024
_VISION_PROMPT = "Describe this image for the agent context. Be concise but complete."

AttachMode = Literal["full", "chunked"]
DescribeImageFn = Callable[[Path], Awaitable[str]]


class MaterializeError(Exception):
    """Raised when a workspace path cannot be turned into attach content."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class InlineAttachment:
    """One pending inline file ready to inject into the next user message."""

    path: str
    kind: str
    tokens: int
    approximate: bool
    source: str
    mode: AttachMode
    content: str
    label: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "tokens": self.tokens,
            "approximate": self.approximate,
            "source": self.source,
            "mode": self.mode,
            "label": self.label,
        }

    def inject_block(self) -> str:
        flags: list[str] = [f"~{self.tokens} tokens"]
        if self.approximate:
            flags.append("approx")
        if self.mode == "chunked":
            flags.append("chunked")
        meta = ", ".join(flags)
        return f"[Attached file: {self.path} ({meta})]\n```\n{self.content}\n```"


def compose_inline_attachments(
    attachments: list[InlineAttachment],
    user_text: str,
) -> str:
    """Compose attachment blocks before user text (D-087 message-local)."""
    blocks = [item.inject_block() for item in attachments]
    body = "\n\n".join(blocks)
    text = user_text.strip()
    if body and text:
        return f"{body}\n\n{text}"
    if body:
        return body
    return text


def compose_attachments_with_workbook_brief(
    attachments: list[InlineAttachment],
    user_text: str,
    *,
    workspace: Path | None = None,
) -> str:
    """Compose attaches; merge all pending xlsx into one FILES_BRIEF.

    Non-spreadsheet attaches keep per-file inject blocks. When one or more
    spreadsheet attaches are pending, their individual briefs are replaced by a
    single aggregated ``format_files_brief_for_agent`` block.
    """
    if not attachments:
        return user_text.strip()

    sheets = [a for a in attachments if a.kind == "spreadsheet"]
    others = [a for a in attachments if a.kind != "spreadsheet"]
    if not sheets:
        return compose_inline_attachments(attachments, user_text)

    from corpclaw_lite.agent.workbook_brief import (
        build_workbook_brief,
        format_files_brief_for_agent,
    )

    paths: list[Path] = []
    rels: list[str] = []
    for item in sheets:
        if workspace is not None:
            candidate = (workspace / item.path).resolve()
            if candidate.is_file():
                paths.append(candidate)
                rels.append(item.path)
                continue
        # Fallback: materialize already put a per-file brief in content — keep
        # absolute path from content header when workspace resolve fails.
        paths.append(Path(item.path))
        rels.append(item.path)

    try:
        bundle = build_workbook_brief(paths)
        for fb, rel in zip(bundle.files, rels, strict=False):
            fb.path = rel
            fb.name = Path(rel).name
        brief_text = format_files_brief_for_agent(bundle)
        agg_flags = f"~{sum(s.tokens for s in sheets)} tokens (aggregated brief)"
        sheet_block = f"[Attached spreadsheets ({agg_flags})]\n```\n{brief_text}\n```"
    except Exception as exc:
        logger.warning("aggregated workbook brief failed: %s — per-file fallback", exc)
        return compose_inline_attachments(attachments, user_text)

    blocks = [item.inject_block() for item in others]
    blocks.append(sheet_block)
    body = "\n\n".join(blocks)
    text = user_text.strip()
    if body and text:
        return f"{body}\n\n{text}"
    if body:
        return body
    return text


def ui_placeholder_for_attachments(attachments: list[InlineAttachment], user_text: str) -> str:
    """Short UI-visible content (full dump lives only in LLM compose)."""
    text = user_text.strip()
    if text:
        return text
    if not attachments:
        return ""
    if len(attachments) == 1:
        return f"📎 {attachments[0].label}"
    return f"📎 {len(attachments)} файла(ов)"


def truncate_text_to_token_budget(text: str, max_tokens: int = CHUNKED_TEXT_TOKEN_BUDGET) -> str:
    """Truncate text so heuristic token estimate stays within ``max_tokens``."""
    if max_tokens < 1:
        return ""
    if estimate_tokens_heuristic(text) <= max_tokens:
        return text
    # Conservative: non-ASCII ≈ 2 bytes/token, ASCII ≈ 4 chars/token.
    encoded = text.encode("utf-8")
    non_ascii_heavy = len(encoded) > len(text) * 1.3
    char_budget = max_tokens * (2 if non_ascii_heavy else 4)
    if len(text) <= char_budget:
        # Still over estimate — shrink further.
        char_budget = max(1, int(len(text) * 0.5))
    cut = text[:char_budget]
    # Prefer break on newline/space near the end.
    for sep in ("\n", " "):
        idx = cut.rfind(sep, int(char_budget * 0.7))
        if idx > 0:
            cut = cut[:idx]
            break
    suffix = "\n… [truncated for context budget; use read_file for the rest]"
    while estimate_tokens_heuristic(cut + suffix) > max_tokens and len(cut) > 32:
        cut = cut[: max(32, int(len(cut) * 0.85))]
    return cut + suffix


async def materialize_attachment(
    *,
    workspace: Path,
    raw_path: str,
    baseline: int,
    limit: int,
    tokenizer: TokenizerClient,
    chunked: bool | None = None,
    describe_image: DescribeImageFn | None = None,
    vision_cache: dict[str, str] | None = None,
) -> tuple[InlineAttachment, BudgetGateResult]:
    """Read path, materialize text, run budget gate, optionally chunk.

    Returns attachment + gate result. Raises :class:`MaterializeError` on hard
    failures; gate BLOCK is also raised as MaterializeError(status=400).
    """
    from corpclaw_lite.channels.web.files import (
        resolve_workspace_path,
    )

    # Import helpers lazily to avoid cycles at module import time.
    target = resolve_workspace_path(workspace, raw_path)
    if not target.exists() or not target.is_file():
        raise MaterializeError("File not found", status=404)

    rel = _relative(workspace, target)
    kind = _entry_kind(target)
    label = target.name

    raw_content, content_source = await _materialize_kind(
        workspace=workspace,
        target=target,
        kind=kind,
        rel=rel,
        describe_image=describe_image,
        vision_cache=vision_cache,
    )

    gate_full = await estimate_content_budget(
        raw_content, baseline=baseline, limit=limit, tokenizer=tokenizer
    )

    mode: AttachMode = "full"
    content = raw_content
    gate = gate_full

    if gate_full.decision is not BudgetDecision.BLOCK:
        # Full content fits (ALLOW/WARN). Optionally still chunk if offered/forced.
        use_chunked = chunked is True or (chunked is None and gate_full.offer_chunked)
        if use_chunked and kind == "text":
            content = truncate_text_to_token_budget(raw_content)
            mode = "chunked"
            gate = await estimate_content_budget(
                content, baseline=baseline, limit=limit, tokenizer=tokenizer
            )
            if gate.decision is BudgetDecision.BLOCK:
                raise MaterializeError(gate.reason, status=400)
        elif use_chunked and kind == "spreadsheet":
            # Already row-limited; mark chunked when gate asked for it.
            mode = "chunked"
    elif kind == "text" and chunked is not False:
        # H2: full content would BLOCK — try a truncated portion before failing.
        content = truncate_text_to_token_budget(raw_content)
        mode = "chunked"
        gate = await estimate_content_budget(
            content, baseline=baseline, limit=limit, tokenizer=tokenizer
        )
        if gate.decision is BudgetDecision.BLOCK:
            raise MaterializeError(gate.reason, status=400)
    else:
        # Non-text full BLOCK, or text with chunked=False.
        raise MaterializeError(gate_full.reason, status=400)

    source = content_source
    if gate.source in ("tokenize", "heuristic", "cache"):
        # Prefer tokenizer estimate source when content was re-estimated.
        source = gate.source if content_source != "vision" else "vision"

    attachment = InlineAttachment(
        path=rel,
        kind=kind,
        tokens=gate.file_tokens,
        approximate=gate.approximate or content_source in {"heuristic", "vision"},
        source=source,
        mode=mode,
        content=content,
        label=label,
    )
    return attachment, gate


def _relative(workspace: Path, path: Path) -> str:
    rel = path.resolve().relative_to(workspace.resolve())
    return str(rel).replace("\\", "/")


def _entry_kind(path: Path) -> str:
    from mimetypes import guess_type

    suffix = path.suffix.lower()
    mime_type, _enc = guess_type(path.name)
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return "image"
    if suffix in {".xlsx", ".xls", ".csv"}:
        return "spreadsheet"
    if suffix == ".pdf":
        return "pdf"
    text_ext = {
        ".csv",
        ".json",
        ".log",
        ".md",
        ".markdown",
        ".py",
        ".txt",
        ".yaml",
        ".yml",
        ".xml",
        ".html",
        ".htm",
        ".toml",
        ".ini",
        ".cfg",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".css",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".sh",
        ".sql",
    }
    if suffix in text_ext or (mime_type is not None and mime_type.startswith("text/")):
        return "text"
    return "file"


async def _materialize_kind(
    *,
    workspace: Path,
    target: Path,
    kind: str,
    rel: str,
    describe_image: DescribeImageFn | None,
    vision_cache: dict[str, str] | None,
) -> tuple[str, str]:
    if kind == "text":
        content = await _read_text_file(target)
        return content, "heuristic"
    if kind == "image":
        return await _materialize_image(
            target, describe_image=describe_image, vision_cache=vision_cache
        )
    if kind == "spreadsheet":
        return _materialize_spreadsheet(target, rel), "heuristic"
    if kind == "pdf":
        return _materialize_pdf(target, rel), "heuristic"
    # Generic binary stub
    size = target.stat().st_size
    stub = (
        f"Binary file {rel} ({size} bytes). "
        "Content not inlined; use tools (read_file / specialized) as needed."
    )
    return stub, "heuristic"


async def _read_text_file(target: Path) -> str:
    import anyio

    size = target.stat().st_size
    if size > _MAX_TEXT_BYTES:
        raise MaterializeError(
            f"File is too large to attach (max {_MAX_TEXT_BYTES} bytes)",
            status=413,
        )
    data = await anyio.Path(target).read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


async def _materialize_image(
    target: Path,
    *,
    describe_image: DescribeImageFn | None,
    vision_cache: dict[str, str] | None,
) -> tuple[str, str]:
    if describe_image is None:
        raise MaterializeError("Vision is unavailable for image attach", status=400)
    raw = target.read_bytes()
    key = hashlib.sha256(raw).hexdigest()
    if vision_cache is not None and key in vision_cache:
        return vision_cache[key], "vision"
    description = await describe_image(target)
    if description.startswith("Error:"):
        raise MaterializeError(description, status=400)
    if vision_cache is not None:
        vision_cache[key] = description
    return description, "vision"


def _materialize_spreadsheet(target: Path, rel: str) -> str:
    suffix = target.suffix.lower()
    try:
        if suffix == ".csv":
            return _excerpt_csv(target, rel)
        return _excerpt_xlsx(target, rel)
    except Exception as exc:
        logger.warning("spreadsheet materialize failed for %s: %s", rel, exc)
        size = target.stat().st_size
        return (
            f"Spreadsheet {rel} ({size} bytes). "
            f"Could not extract preview ({type(exc).__name__}); use excel_inspect/tools."
        )


def _excerpt_csv(target: Path, rel: str) -> str:
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    head = lines[: DEFAULT_SPREADSHEET_ROW_LIMIT + 1]
    more = max(0, len(lines) - len(head))
    body = "\n".join(head)
    note = f"\n… ({more} more lines)" if more else ""
    return f"CSV preview of {rel} (first {len(head)} lines):\n{body}{note}"


def _excerpt_xlsx(target: Path, rel: str) -> str:
    """Compact deterministic FILES_BRIEF for xlsx (replaces 50-row dump)."""
    from corpclaw_lite.agent.workbook_brief import (
        build_workbook_brief,
        format_files_brief_for_agent,
    )

    try:
        bundle = build_workbook_brief([target])
        if not bundle.files:
            raise ValueError("empty brief")
        # Present path as the workspace-relative attach path for the model.
        bundle.files[0].path = rel
        bundle.files[0].name = Path(rel).name
        return format_files_brief_for_agent(bundle)
    except Exception as exc:
        logger.warning("workbook brief failed for %s: %s — falling back to row preview", rel, exc)
        from openpyxl import load_workbook

        wb = load_workbook(target, read_only=True, data_only=True)
        try:
            parts: list[str] = [f"Excel preview of {rel}. Sheets: {', '.join(wb.sheetnames)}"]
            for sheet_name in wb.sheetnames[:3]:
                ws = wb[sheet_name]
                rows: list[str] = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i >= DEFAULT_SPREADSHEET_ROW_LIMIT:
                        rows.append(f"… (more rows; limit {DEFAULT_SPREADSHEET_ROW_LIMIT})")
                        break
                    cells = ["" if c is None else str(c) for c in row[:20]]
                    rows.append("\t".join(cells))
                parts.append(f"## Sheet: {sheet_name}\n" + "\n".join(rows))
            return "\n\n".join(parts)
        finally:
            wb.close()


def _materialize_pdf(target: Path, rel: str) -> str:
    size = target.stat().st_size
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(target))
        n_pages = len(reader.pages)
        chunks: list[str] = []
        for i, page in enumerate(reader.pages[:3]):
            text = page.extract_text() or ""
            text = text.strip()
            if text:
                chunks.append(f"--- page {i + 1} ---\n{text[:2000]}")
        body = "\n\n".join(chunks) if chunks else "(no extractable text)"
        return f"PDF preview of {rel} ({n_pages} pages, {size} bytes):\n{body}"
    except Exception as exc:
        logger.warning("pdf materialize failed for %s: %s", rel, exc)
        return (
            f"PDF file {rel} ({size} bytes). "
            f"Could not extract text ({type(exc).__name__}); use pdf tools if available."
        )


def attachment_metadata_list(attachments: list[InlineAttachment]) -> list[dict[str, Any]]:
    return [item.to_public_dict() for item in attachments]
