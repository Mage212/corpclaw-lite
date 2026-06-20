"""Integration tests for B-058: record_read (ToolRegistry) + check_stale
(FileTrackedTool) end-to-end."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
from corpclaw_lite.agent.file_state import FileStateRegistry
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.file_tracked import FileTrackedTool
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.memory.file_changes import FileChangeDAO
from corpclaw_lite.users.models import User


@pytest.fixture
def user() -> User:
    return User(id=1, name="Test", department="qa")


class _ReadFileTool(Tool):
    """Minimal read_file stand-in: returns the file content."""

    name = "read_file"
    description = "read"
    params = [ToolParam(name="path", type="string", description="")]
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return Path(kwargs.get("path", "")).read_text(encoding="utf-8")


class _WriteFileTool(Tool):
    """Minimal write_file stand-in."""

    name = "write_file"
    description = "write"
    params = [
        ToolParam(name="path", type="string", description=""),
        ToolParam(name="content", type="string", description=""),
    ]
    risk_level = RiskLevel.MEDIUM

    async def execute(self, **kwargs: Any) -> str:
        Path(kwargs.get("path", "")).write_text(kwargs.get("content", ""), encoding="utf-8")
        return "ok"


def _build_registry(
    workspace: Path, file_state: FileStateRegistry
) -> tuple[ToolRegistry, FileChangeDAO, FileSnapshotStore]:
    registry = ToolRegistry()
    registry.register(_ReadFileTool())
    registry.register(_WriteFileTool())
    dao = FileChangeDAO(db_path=workspace / "test.db")
    store = FileSnapshotStore(workspace_base=workspace.parent)
    # Wrap write_file (tracks_output=True), leave read_file raw.
    raw_write = registry.get("write_file")
    assert raw_write is not None
    registry.unregister("write_file")
    registry.register(
        FileTrackedTool(
            raw_write,
            dao=dao,
            snapshot_store=store,
            path_param="path",
            tracks_output=True,
            file_state=file_state,
        ),
        allow_replace=True,
    )
    registry.set_file_state(file_state)
    return registry, dao, store


@pytest.mark.asyncio
async def test_read_then_write_same_run_no_warning(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "data.txt"
    f.write_text("original")

    file_state = FileStateRegistry()
    registry, _, _ = _build_registry(tmp_path, file_state)

    # Step 1: read the file (this run).
    read_result = await registry.execute("read_file", {"path": str(f)}, user=user, run_id="run-1")
    assert read_result == "original"
    assert file_state.has_read(path=str(f), task_id="run-1") is True

    # Step 2: write to the same file in the same run → no stale warning.
    write_result = await registry.execute(
        "write_file",
        {"path": str(f), "content": "new"},
        user=user,
        run_id="run-1",
    )
    assert "File state warning" not in write_result
    assert file_state.last_writer(str(f)) == "run-1"


@pytest.mark.asyncio
async def test_write_without_read_emits_warning(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "data.txt"
    f.write_text("original")

    file_state = FileStateRegistry()
    registry, _, _ = _build_registry(tmp_path, file_state)

    # Write without reading first.
    write_result = await registry.execute(
        "write_file",
        {"path": str(f), "content": "new"},
        user=user,
        run_id="run-1",
    )
    assert "File state warning" in write_result
    assert "have not read" in write_result


@pytest.mark.asyncio
async def test_sibling_wrote_after_read_emits_warning(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "data.txt"
    f.write_text("v1")

    file_state = FileStateRegistry()
    registry, _, _ = _build_registry(tmp_path, file_state)

    # run-1 reads.
    await registry.execute("read_file", {"path": str(f)}, user=user, run_id="run-1")
    # run-2 writes (sibling subagent).
    await registry.execute(
        "write_file",
        {"path": str(f), "content": "v2"},
        user=user,
        run_id="run-2",
    )
    # run-1 now writes without re-reading → stale warning (cross-agent).
    write_result = await registry.execute(
        "write_file",
        {"path": str(f), "content": "v3"},
        user=user,
        run_id="run-1",
    )
    assert "File state warning" in write_result
    assert "another agent" in write_result
    assert "run-2" in write_result


@pytest.mark.asyncio
async def test_record_read_for_excel_workbook_read_action(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """excel_workbook is a read tool only when action=='read'."""
    monkeypatch.chdir(tmp_path)

    class FakeExcelTool(Tool):
        name = "excel_workbook"
        description = "excel"
        params = [
            ToolParam(name="path", type="string", description=""),
            ToolParam(name="action", type="string", description=""),
        ]
        risk_level = RiskLevel.MEDIUM

        async def execute(self, **kwargs: Any) -> str:
            return "ok"

    registry = ToolRegistry()
    registry.register(FakeExcelTool())
    file_state = FileStateRegistry()
    registry.set_file_state(file_state)

    # Create the file so record_read_path can stat it.
    f = tmp_path / "f.xlsx"
    f.write_bytes(b"fake xlsx")

    # action=read → recorded.
    await registry.execute(
        "excel_workbook",
        {"path": str(f), "action": "read"},
        user=user,
        run_id="r1",
    )
    assert file_state.has_read(path=str(f), task_id="r1") is True

    # action=fill → NOT recorded as a read.
    file_state.reset()
    await registry.execute(
        "excel_workbook",
        {"path": str(f), "action": "fill"},
        user=user,
        run_id="r1",
    )
    assert file_state.has_read(path=str(f), task_id="r1") is False
