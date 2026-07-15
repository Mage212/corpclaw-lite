"""Unit tests for B-094 inline attach materialize/compose."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.agent.budget_gate import BudgetDecision
from corpclaw_lite.agent.inline_attach import (
    InlineAttachment,
    MaterializeError,
    compose_inline_attachments,
    materialize_attachment,
    truncate_text_to_token_budget,
    ui_placeholder_for_attachments,
)
from corpclaw_lite.channels.web.pending_attachments import PendingAttachmentsStore
from corpclaw_lite.llm.tokenizer_client import TokenizerClient, estimate_tokens_heuristic


def test_compose_order_blocks_then_text() -> None:
    a = InlineAttachment(
        path="a.md",
        kind="text",
        tokens=10,
        approximate=True,
        source="heuristic",
        mode="full",
        content="AAA",
        label="a.md",
    )
    b = InlineAttachment(
        path="b.md",
        kind="text",
        tokens=5,
        approximate=True,
        source="heuristic",
        mode="full",
        content="BBB",
        label="b.md",
    )
    out = compose_inline_attachments([a, b], "hello")
    assert out.index("[Attached file: a.md") < out.index("[Attached file: b.md")
    assert out.index("AAA") < out.index("BBB")
    assert out.endswith("hello")
    assert "```" in out


def test_compose_empty_user_text() -> None:
    a = InlineAttachment(
        path="a.md",
        kind="text",
        tokens=1,
        approximate=True,
        source="heuristic",
        mode="full",
        content="X",
        label="a.md",
    )
    out = compose_inline_attachments([a], "  ")
    assert "Attached file" in out
    assert out.strip()


def test_truncate_text_to_token_budget() -> None:
    text = "word " * 5000
    cut = truncate_text_to_token_budget(text, max_tokens=100)
    assert estimate_tokens_heuristic(cut) <= 120  # small slack for suffix
    assert "truncated" in cut.lower()


def test_ui_placeholder() -> None:
    a = InlineAttachment(
        path="a.md",
        kind="text",
        tokens=1,
        approximate=True,
        source="heuristic",
        mode="full",
        content="X",
        label="a.md",
    )
    assert ui_placeholder_for_attachments([a], "hi") == "hi"
    assert "📎" in ui_placeholder_for_attachments([a], "")


def test_pending_store_max_and_replace() -> None:
    store = PendingAttachmentsStore(max_pending=2)

    def item(path: str) -> InlineAttachment:
        return InlineAttachment(
            path=path,
            kind="text",
            tokens=1,
            approximate=True,
            source="heuristic",
            mode="full",
            content="x",
            label=path,
        )

    store.add(1, 10, item("a.md"))
    store.add(1, 10, item("b.md"))
    with pytest.raises(ValueError, match="Too many"):
        store.add(1, 10, item("c.md"))
    # Replace same path does not increase count
    store.add(1, 10, item("a.md"))
    assert store.count(1, 10) == 2
    popped = store.pop_all(1, 10)
    assert len(popped) == 2
    assert store.count(1, 10) == 0


@pytest.mark.asyncio
async def test_materialize_text_and_block(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.txt").write_text("hello world", encoding="utf-8")
    tokenizer = TokenizerClient(mode="heuristic")
    attachment, gate = await materialize_attachment(
        workspace=workspace,
        raw_path="note.txt",
        baseline=0,
        limit=10_000,
        tokenizer=tokenizer,
    )
    assert attachment.kind == "text"
    assert "hello world" in attachment.content
    assert gate.decision is BudgetDecision.ALLOW

    # Force BLOCK with tiny limit
    (workspace / "big.txt").write_text("x" * 2000, encoding="utf-8")
    with pytest.raises(MaterializeError, match="Не поместится|контекст"):
        await materialize_attachment(
            workspace=workspace,
            raw_path="big.txt",
            baseline=0,
            limit=50,
            tokenizer=tokenizer,
            chunked=False,
        )


@pytest.mark.asyncio
async def test_materialize_chunked(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "long.txt").write_text("word " * 20_000, encoding="utf-8")
    tokenizer = TokenizerClient(mode="heuristic")
    attachment, _gate = await materialize_attachment(
        workspace=workspace,
        raw_path="long.txt",
        baseline=0,
        limit=50_000,
        tokenizer=tokenizer,
        chunked=True,
    )
    assert attachment.mode == "chunked"
    assert "truncated" in attachment.content.lower()


@pytest.mark.asyncio
async def test_materialize_image_with_mock_vision(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

    async def describe(_path: Path) -> str:
        return "A red square on white background."

    tokenizer = TokenizerClient(mode="heuristic")
    attachment, gate = await materialize_attachment(
        workspace=workspace,
        raw_path="pic.png",
        baseline=0,
        limit=10_000,
        tokenizer=tokenizer,
        describe_image=describe,
        vision_cache={},
    )
    assert attachment.kind == "image"
    assert "red square" in attachment.content
    assert gate.decision is BudgetDecision.ALLOW


@pytest.mark.asyncio
async def test_materialize_missing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(MaterializeError) as exc:
        await materialize_attachment(
            workspace=workspace,
            raw_path="nope.txt",
            baseline=0,
            limit=1000,
            tokenizer=TokenizerClient(mode="heuristic"),
        )
    assert exc.value.status == 404
