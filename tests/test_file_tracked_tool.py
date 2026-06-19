"""Tests for B-040: FileTrackedTool wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.file_tracked import FileTrackedTool
from corpclaw_lite.memory.file_changes import FileChangeDAO
from corpclaw_lite.users.models import User

# ─── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def user() -> User:
    return User(id=1, name="Test", department="qa")


@pytest.fixture
def dao(tmp_path: Path) -> FileChangeDAO:
    return FileChangeDAO(db_path=tmp_path / "test.db")


@pytest.fixture
def store(tmp_path: Path) -> FileSnapshotStore:
    return FileSnapshotStore(workspace_base=tmp_path)


@pytest.fixture
def workspace(tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make CWD = user workspace so resolve_and_validate_path uses it."""
    ws = tmp_path / f"user_{user.workspace_key()}"
    ws.mkdir(parents=True)
    monkeypatch.chdir(ws)
    return ws


# ─── Fake tools ──────────────────────────────────────────────────────────────


class FakeWriteTool(Tool):
    """Simulates write_file: writes content to the given path."""

    name = "write_file"
    description = "write file"
    params = [
        ToolParam(name="path", type="string", description=""),
        ToolParam(name="content", type="string", description=""),
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        path = kwargs.get("path")
        content = kwargs.get("content", "")
        Path(path).write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars"


class FakeNormalizeTool(Tool):
    """Simulates normalize_excel: writes to <stem>_normalized.xlsx (new file)."""

    name = "normalize_excel"
    description = "normalize"
    params = [ToolParam(name="path", type="string", description="")]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        path = Path(kwargs.get("path", ""))
        out = path.with_name(f"{path.stem}_normalized{path.suffix or '.xlsx'}")
        out.write_bytes(b"normalized")
        return f"normalized → {out.name}"


# ─── tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_file_wrapped_records_modify(
    dao: FileChangeDAO, store: FileSnapshotStore, workspace: Path, user: User
) -> None:
    target = workspace / "f.txt"
    target.write_text("original")  # pre-existing

    wrapped = FileTrackedTool(
        FakeWriteTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=True,
    )
    result = await wrapped.execute(path=str(target), content="new", user=user, run_id="run-1")
    assert "wrote" in result
    changes = await dao.list_for_run("run-1")
    assert len(changes) == 1
    c = changes[0]
    assert c.op == "modify"
    assert c.tool_name == "write_file"
    assert c.before_hash is not None
    assert c.after_hash is not None
    assert c.before_hash != c.after_hash
    assert c.backup_path == "f.txt"


@pytest.mark.asyncio
async def test_write_file_wrapped_records_create_for_new_file(
    dao: FileChangeDAO, store: FileSnapshotStore, workspace: Path, user: User
) -> None:
    target = workspace / "new.txt"
    assert not target.exists()

    wrapped = FileTrackedTool(
        FakeWriteTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=True,
    )
    await wrapped.execute(path=str(target), content="data", user=user, run_id="run-1")
    changes = await dao.list_for_run("run-1")
    assert len(changes) == 1
    assert changes[0].op == "create"
    assert changes[0].before_hash is None
    # No backup for a create (nothing to back up).
    assert changes[0].backup_path is None


@pytest.mark.asyncio
async def test_hash_dedup_no_change_skips_record(
    dao: FileChangeDAO, store: FileSnapshotStore, workspace: Path, user: User
) -> None:
    """If the write produces identical bytes, no change is recorded."""

    target = workspace / "f.txt"
    target.write_text("same")

    class NoOpWriteTool(Tool):
        name = "write_file"
        description = ""
        params: list[ToolParam] = []
        risk_level = RiskLevel.MEDIUM

        async def execute(self, **kwargs: Any) -> str:
            # Overwrites with identical content.
            Path(kwargs.get("path", "")).write_text("same", encoding="utf-8")
            return "ok"

    wrapped = FileTrackedTool(
        NoOpWriteTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=True,
    )
    await wrapped.execute(path=str(target), user=user, run_id="run-1")
    assert await dao.list_for_run("run-1") == []


@pytest.mark.asyncio
async def test_normalize_excel_new_file_no_backup_records_after(
    dao: FileChangeDAO, store: FileSnapshotStore, workspace: Path, user: User
) -> None:
    """normalize_excel creates a new file — no backup, but after-snapshot recorded."""
    source = workspace / "data.xlsx"
    source.write_bytes(b"raw xlsx")

    wrapped = FileTrackedTool(
        FakeNormalizeTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=False,
    )
    await wrapped.execute(path=str(source), user=user, run_id="run-1")

    changes = await dao.list_for_run("run-1")
    assert len(changes) == 1
    c = changes[0]
    assert c.op == "create"  # new file produced
    assert c.before_hash is None
    assert c.after_hash is not None
    assert c.backup_path is None  # no backup (tracks_output=False)
    assert "normalized" in c.file_path


@pytest.mark.asyncio
async def test_no_run_id_passes_through_untracked(
    dao: FileChangeDAO, store: FileSnapshotStore, workspace: Path, user: User
) -> None:
    """Without run_id (e.g. calibration/direct call), the wrapper is transparent."""
    target = workspace / "f.txt"
    target.write_text("old")

    wrapped = FileTrackedTool(
        FakeWriteTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=True,
    )
    # No run_id → no tracking
    result = await wrapped.execute(path=str(target), content="new", user=user)
    assert "wrote" in result
    assert await dao.list_for_run("any") == []


@pytest.mark.asyncio
async def test_no_user_passes_through_untracked(
    dao: FileChangeDAO, store: FileSnapshotStore, workspace: Path
) -> None:
    target = workspace / "f.txt"
    target.write_text("old")

    wrapped = FileTrackedTool(
        FakeWriteTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=True,
    )
    # No user → no tracking
    result = await wrapped.execute(path=str(target), content="new", run_id="r")
    assert "wrote" in result


@pytest.mark.asyncio
async def test_attributes_passthrough_from_wrapped_tool(
    dao: FileChangeDAO, store: FileSnapshotStore
) -> None:
    """ScopedTool-style passthrough: name/description/risk_level/terminal."""
    inner = FakeWriteTool()
    wrapped = FileTrackedTool(
        inner, dao=dao, snapshot_store=store, path_param="path", tracks_output=True
    )
    assert wrapped.name == "write_file"
    assert wrapped.description == "write file"
    assert wrapped.risk_level == RiskLevel.MEDIUM
    assert wrapped.terminal is False
    assert wrapped.params == inner.params
