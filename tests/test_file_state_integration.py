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

    # action=fill → NOT recorded as a read via ToolRegistry (fill is a write).
    file_state.reset()
    await registry.execute(
        "excel_workbook",
        {"path": str(f), "action": "fill"},
        user=user,
        run_id="r1",
    )
    assert file_state.has_read(path=str(f), task_id="r1") is False


@pytest.mark.asyncio
async def test_excel_workbook_fill_by_date_no_unread_warning(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileTrackedTool treats fill_by_date as an implicit source read (no false warning)."""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "template.xlsx"
    f.write_bytes(b"fake")

    class FakeExcelTool(Tool):
        name = "excel_workbook"
        description = "excel"
        params = [
            ToolParam(name="path", type="string", description=""),
            ToolParam(name="action", type="string", description=""),
        ]
        risk_level = RiskLevel.MEDIUM

        async def execute(self, **kwargs: Any) -> str:
            return "SUCCESS: wrote 1 gap value(s) to 'Daily' → completed.xlsx."

    file_state = FileStateRegistry()
    dao = FileChangeDAO(db_path=tmp_path / "test.db")
    store = FileSnapshotStore(workspace_base=tmp_path)
    wrapped = FileTrackedTool(
        FakeExcelTool(),
        dao=dao,
        snapshot_store=store,
        path_param="path",
        tracks_output=True,
        file_state=file_state,
    )

    result = await wrapped.execute(
        path=str(f),
        action="fill_by_date",
        user=user,
        run_id="r-fill",
    )
    assert "File state warning" not in result
    assert "have not read" not in result
    assert "SUCCESS:" in result
    assert file_state.has_read(path=str(f), task_id="r-fill") is True


# ── apply_fill_plan + FileTrackedTool integration ────────────────────────────


class _FakeApplyFillPlanTool(Tool):
    """Minimal apply_fill_plan stand-in: parses plan, writes output file."""

    name = "apply_fill_plan"
    description = "apply fill plan"
    params = [ToolParam(name="plan", type="string", description="")]
    risk_level = RiskLevel.MEDIUM
    parallel_safe = False
    terminal = False

    async def execute(self, **kwargs: Any) -> str:
        import json

        plan = kwargs.get("plan")
        if isinstance(plan, str):
            plan = json.loads(plan)
        output = plan.get("output_path", "output.xlsx")
        Path(output).write_bytes(b"fake-output-content")
        return f"SUCCESS: apply_fill_plan finished.\nwrote to {output}"


def _make_simple_plan_json(template: str, output: str) -> str:
    """Build a minimal valid plan JSON for tests."""
    import json

    return json.dumps(
        {
            "template": template,
            "output_path": output,
            "cutoff": "2026-07-12",
            "sheets": [
                {
                    "sheet": "Daily",
                    "match_mode": "date",
                    "date_column": "A",
                    "value_column": "B",
                    "aggregate": "sum",
                    "sources": [
                        {
                            "file": "source_a.xlsx",
                            "sheet": "Report",
                            "date_column": "A",
                            "value_column": "B",
                        }
                    ],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_apply_fill_plan_creates_output_and_journals(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_fill_plan wrapped in FileTrackedTool: output created + change journaled."""

    monkeypatch.chdir(tmp_path)
    # Prepare template + source.
    (tmp_path / "template.xlsx").write_bytes(b"fake-template")
    (tmp_path / "source_a.xlsx").write_bytes(b"fake-source")

    file_state = FileStateRegistry()
    dao = FileChangeDAO(db_path=tmp_path / "test.db")
    store = FileSnapshotStore(workspace_base=tmp_path)
    wrapped = FileTrackedTool(
        _FakeApplyFillPlanTool(),
        dao=dao,
        snapshot_store=store,
        path_param="plan",
        tracks_output=False,
        file_state=file_state,
    )

    plan_json = _make_simple_plan_json("template.xlsx", "target_report.xlsx")
    result = await wrapped.execute(plan=plan_json, user=user, run_id="r1")

    assert "SUCCESS:" in result
    assert (tmp_path / "target_report.xlsx").exists()

    # Change should be journaled (op=create since output didn't exist before).
    changes = await dao.list_for_run("r1")
    assert any(c.file_path.endswith("target_report.xlsx") for c in changes)


@pytest.mark.asyncio
async def test_apply_fill_plan_detects_stale_write(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a sibling run wrote to the output path, apply_fill_plan gets a warning."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "template.xlsx").write_bytes(b"fake-template")
    (tmp_path / "source_a.xlsx").write_bytes(b"fake-source")
    # Pre-create output and register a sibling write.
    (tmp_path / "target_report.xlsx").write_bytes(b"sibling-content")

    file_state = FileStateRegistry()
    file_state.note_write(path=str(tmp_path / "target_report.xlsx"), task_id="sibling-run")

    dao = FileChangeDAO(db_path=tmp_path / "test.db")
    store = FileSnapshotStore(workspace_base=tmp_path)
    wrapped = FileTrackedTool(
        _FakeApplyFillPlanTool(),
        dao=dao,
        snapshot_store=store,
        path_param="plan",
        tracks_output=False,
        file_state=file_state,
    )

    plan_json = _make_simple_plan_json("template.xlsx", "target_report.xlsx")
    result = await wrapped.execute(plan=plan_json, user=user, run_id="r2")

    assert "[File state warning]" in result


@pytest.mark.asyncio
async def test_apply_fill_plan_implicit_read_no_false_warning(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply_fill_plan reads template + sources internally; no false 'have not read'."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "template.xlsx").write_bytes(b"fake-template")
    (tmp_path / "source_a.xlsx").write_bytes(b"fake-source")

    file_state = FileStateRegistry()
    dao = FileChangeDAO(db_path=tmp_path / "test.db")
    store = FileSnapshotStore(workspace_base=tmp_path)
    wrapped = FileTrackedTool(
        _FakeApplyFillPlanTool(),
        dao=dao,
        snapshot_store=store,
        path_param="plan",
        tracks_output=False,
        file_state=file_state,
    )

    plan_json = _make_simple_plan_json("template.xlsx", "target_report.xlsx")
    result = await wrapped.execute(plan=plan_json, user=user, run_id="r3")

    assert "have not read" not in result
    assert "SUCCESS:" in result
    # Template and source should be recorded as reads.
    assert file_state.has_read(path=str(tmp_path / "template.xlsx"), task_id="r3")
    assert file_state.has_read(path=str(tmp_path / "source_a.xlsx"), task_id="r3")


@pytest.mark.asyncio
async def test_apply_fill_plan_backup_on_modify(
    tmp_path: Path, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When output already exists, apply_fill_plan takes a backup before overwriting."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "template.xlsx").write_bytes(b"fake-template")
    (tmp_path / "source_a.xlsx").write_bytes(b"fake-source")
    # Pre-existing output → should be backed up.
    (tmp_path / "target_report.xlsx").write_bytes(b"old-content")

    file_state = FileStateRegistry()
    dao = FileChangeDAO(db_path=tmp_path / "test.db")
    store = FileSnapshotStore(workspace_base=tmp_path)
    wrapped = FileTrackedTool(
        _FakeApplyFillPlanTool(),
        dao=dao,
        snapshot_store=store,
        path_param="plan",
        tracks_output=False,
        file_state=file_state,
    )

    plan_json = _make_simple_plan_json("template.xlsx", "target_report.xlsx")
    result = await wrapped.execute(plan=plan_json, user=user, run_id="r4")

    assert "SUCCESS:" in result
    # Change should be journaled as op=modify (output existed before).
    changes = await dao.list_for_run("r4")
    target_changes = [c for c in changes if c.file_path.endswith("target_report.xlsx")]
    assert target_changes, "expected a journaled change for target_report.xlsx"
    assert target_changes[0].op == "modify"
