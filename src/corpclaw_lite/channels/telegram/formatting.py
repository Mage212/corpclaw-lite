# pyright: reportMissingTypeStubs=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportPrivateUsage=false, reportAttributeAccessIssue=false, reportMissingImports=false, reportPrivateImportUsage=false, reportUnknownArgumentType=false
"""Telegram message formatting and splitting layer.

Provides utilities for:
  - Splitting long texts to fit within Telegram's 4096-character limit
    while preserving code blocks.
  - Converting standard Markdown to Telegram MarkdownV2 format.
  - Converting Markdown tables to card-style text (key-value approach),
    as Telegram does not support tables natively.
"""

from __future__ import annotations

import re

import mistletoe
from mistletoe.block_token import BlockCode, remove_token
from telegramify_markdown import _update_block, escape_latex
from telegramify_markdown.render import TelegramMarkdownRenderer

# Limits
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
# Conservative max length when splitting to account for MarkdownV2 expansion
SPLIT_MAX_LENGTH = 3800

_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to card-style key-value format.

    Telegram has no table rendering. This converts each row into a card
    with **Header**: value pairs, separated by horizontal lines.
    Skips tables inside code blocks.
    """
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track code blocks
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue

        if in_code_block:
            result.append(line)
            i += 1
            continue

        # Check if this looks like a table header row
        if stripped.startswith("|") and stripped.endswith("|") and "|" in stripped[1:-1]:
            headers = _split_table_row(stripped)

            # Next line must be separator (---|---|---)
            if i + 1 < len(lines):
                sep_line = lines[i + 1].strip()
                if sep_line.startswith("|") and _TABLE_SEP_RE.match(sep_line):
                    i += 2  # Skip header + separator
                    rows: list[list[str]] = []
                    while i < len(lines):
                        data_line = lines[i].strip()
                        if data_line.startswith("|") and data_line.endswith("|"):
                            rows.append(_split_table_row(data_line))
                            i += 1
                        else:
                            break

                    # Build card-style output
                    separator = "────────────"
                    cards: list[str] = []
                    for row in rows:
                        card_lines: list[str] = []
                        for j, header in enumerate(headers):
                            value = row[j] if j < len(row) else ""
                            if value:
                                card_lines.append(f"**{header}**: {value}")
                            else:
                                card_lines.append(f"**{header}**: —")
                        cards.append("\n".join(card_lines))

                    result.append(f"\n{separator}\n".join(cards))
                    continue

        result.append(line)
        i += 1

    return "\n".join(result)


def _markdownify(text: str) -> str:
    """Custom markdownify with our rendering rules.

    Wraps TelegramMarkdownRenderer directly so we can tweak token rules
    inside the context manager.

    Custom rules:
      - Disable indented code blocks (only fenced ``` blocks are code).
    """
    with TelegramMarkdownRenderer(normalize_whitespace=False) as renderer:
        # Avoid treating indented lines as code blocks
        remove_token(BlockCode)
        content = escape_latex(text)
        document = mistletoe.Document(content)
        _update_block(document)
        return str(renderer.render(document))


def convert_markdown(text: str) -> str:
    """Convert standard Markdown to Telegram MarkdownV2 format."""
    text = convert_markdown_tables(text)
    return _markdownify(text)


def split_message(text: str, max_length: int = SPLIT_MAX_LENGTH) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting.
    When a split occurs inside a fenced code block (```), the block is
    closed at the end of the current chunk and re-opened at the start
    of the next chunk so each chunk remains valid markdown.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk = ""
    in_code_block = False
    code_fence = ""  # e.g. "```python"

    for line in text.split("\n"):
        stripped = line.strip()

        # Track code block state
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_fence = stripped  # remember "```lang"
            else:
                in_code_block = False

        # If single line exceeds max, split it forcefully
        if len(line) > max_length:
            if current_chunk:
                chunk_text = current_chunk.rstrip("\n")
                if in_code_block:
                    chunk_text += "\n```"
                chunks.append(chunk_text)
                current_chunk = (code_fence + "\n") if in_code_block else ""

            # Split long line into fixed-size pieces
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
        elif len(current_chunk) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunk_text = current_chunk.rstrip("\n")
            if in_code_block:
                chunk_text += "\n```"
            chunks.append(chunk_text)

            # Re-open code block in the new chunk
            current_chunk = code_fence + "\n" + line + "\n" if in_code_block else line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks


def build_response_parts(text: str) -> list[str]:
    """Build paginated response messages for Telegram.

    Splits the text into chunks fitting Telegram's size boundaries and appends
    pagination indicators [1/N] if the text requires multiple messages.
    """
    text = text.strip()
    text = convert_markdown_tables(text)

    text_chunks = split_message(text, max_length=SPLIT_MAX_LENGTH)
    total = len(text_chunks)

    if total == 1:
        return [text_chunks[0]]

    parts = []
    for i, chunk in enumerate(text_chunks, 1):
        parts.append(f"{chunk}\n\n[{i}/{total}]")

    return parts
