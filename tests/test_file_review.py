"""B-117: FileReviewService + web handlers for review/revert."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from corpclaw_lite.agent.file_review import FileReviewError, FileReviewService
from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
from corpclaw_lite.channels.service import RunningRequest
from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.settings import Settings
from corpclaw_lite.memory.file_changes import FileChangeDAO
from corpclaw_lite.users.models import User


class FakeWorkspaceService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def get_user_workspace(self, _user: User) -> Path:
        return self.workspace

    async def get_running_request(self, _user_id: int) -> RunningRequest | None:
        return None


def _web_request(
    method: str,
    path: str,
    user: User,
    *,
    match_info: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    request = make_mocked_request(method, path, match_info=match_info or {})
    request["user"] = user
    if payload is not None:
        request.json = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    return request


@pytest.fixture
def review_stack(tmp_path: Path) -> tuple[FileReviewService, Path, User, FileChangeDAO]:
    workspace_base = tmp_path / "workspaces"
    user = User(id=77, name="Vadim", department="engineering")
    ws = workspace_base / f"user_{user.workspace_key()}"
    ws.mkdir(parents=True)
    dao = FileChangeDAO(db_path=tmp_path / "mem.db")
    snap = FileSnapshotStore(workspace_base=workspace_base)
    service = FileReviewService(dao=dao, snapshot_store=snap, workspace_base=workspace_base)
    return service, ws, user, dao


@pytest.mark.asyncio
async def test_review_modify_diff_and_revert(
    review_stack: tuple[FileReviewService, Path, User, FileChangeDAO],
) -> None:
    service, ws, user, dao = review_stack
    target = ws / "note.md"
    target.write_text("hello\n", encoding="utf-8")
    snap = service._snap  # type: ignore[attr-defined]
    backup_rel = snap.backup(user, "run-1", target)
    target.write_text("hello world\n", encoding="utf-8")
    cid = await dao.record_change(
        user_id=user.memory_key(),
        run_id="run-1",
        tool_name="write_file",
        file_path="note.md",
        op="modify",
        before_hash="b",
        after_hash="a",
        backup_path=backup_rel,
        size_bytes=target.stat().st_size,
    )
    assert cid is not None

    changes = await service.list_changes(user, limit=10, status="open")
    assert len(changes) == 1
    assert changes[0].change_id == cid

    diff = await service.build_diff(user, cid)
    assert diff.kind == "text"
    assert diff.unified_diff is not None
    assert "hello world" in (diff.unified_diff or "")

    result = await service.revert_change(user, cid)
    assert result.action == "restored"
    assert target.read_text(encoding="utf-8") == "hello\n"
    again = await service.revert_change(user, cid)
    assert again.action == "already_reverted"


@pytest.mark.asyncio
async def test_review_create_delete_on_revert(
    review_stack: tuple[FileReviewService, Path, User, FileChangeDAO],
) -> None:
    service, ws, user, dao = review_stack
    target = ws / "new.txt"
    target.write_text("brand new", encoding="utf-8")
    cid = await dao.record_change(
        user_id=user.memory_key(),
        run_id="run-2",
        tool_name="write_file",
        file_path="new.txt",
        op="create",
        before_hash=None,
        after_hash="x",
        backup_path=None,
        size_bytes=9,
    )
    assert cid is not None
    result = await service.revert_change(user, cid)
    assert result.action == "deleted"
    assert not target.exists()


@pytest.mark.asyncio
async def test_review_ownership_404(
    review_stack: tuple[FileReviewService, Path, User, FileChangeDAO],
) -> None:
    service, ws, user, dao = review_stack
    cid = await dao.record_change(
        user_id=user.memory_key(),
        run_id="run-3",
        tool_name="t",
        file_path="a.txt",
        op="create",
        before_hash=None,
        after_hash="x",
        backup_path=None,
        size_bytes=1,
    )
    assert cid is not None
    other = User(id=99, name="Other", department="engineering")
    with pytest.raises(FileReviewError) as exc:
        await service.get_owned_change(other, cid)
    assert exc.value.status == 404


@pytest.mark.asyncio
async def test_web_list_diff_revert_handlers(tmp_path: Path) -> None:
    workspace_base = tmp_path / "workspaces"
    user = User(id=88, name="Vadim", department="engineering")
    ws = workspace_base / f"user_{user.workspace_key()}"
    ws.mkdir(parents=True)
    target = ws / "doc.txt"
    target.write_text("v1\n", encoding="utf-8")

    dao = FileChangeDAO(db_path=tmp_path / "web.db")
    snap = FileSnapshotStore(workspace_base=workspace_base)
    backup_rel = snap.backup(user, "run-w", target)
    target.write_text("v2\n", encoding="utf-8")
    cid = await dao.record_change(
        user_id=user.memory_key(),
        run_id="run-w",
        tool_name="write_file",
        file_path="doc.txt",
        op="modify",
        before_hash="1",
        after_hash="2",
        backup_path=backup_rel,
        size_bytes=3,
    )
    assert cid is not None

    settings = Settings()
    settings.web_channel.workspace_base = str(workspace_base)
    orch = WebChannelOrchestrator(settings)
    orch._service = FakeWorkspaceService(ws)  # type: ignore[assignment]
    orch._file_review = FileReviewService(
        dao=dao, snapshot_store=snap, workspace_base=workspace_base
    )

    r_list = await orch._handle_list_file_changes(
        _web_request("GET", "/api/files/changes?status=open", user)
    )
    body = json.loads(r_list.text or "{}")
    assert r_list.status == 200
    assert len(body["changes"]) == 1
    assert body["changes"][0]["change_id"] == cid

    r_diff = await orch._handle_file_change_diff(
        _web_request(
            "GET",
            f"/api/files/changes/{cid}/diff",
            user,
            match_info={"change_id": cid},
        )
    )
    dbody = json.loads(r_diff.text or "{}")
    assert dbody["kind"] == "text"
    assert "v2" in dbody.get("unified_diff", "")

    r_rev = await orch._handle_file_change_revert(
        _web_request(
            "POST",
            f"/api/files/changes/{cid}/revert",
            user,
            match_info={"change_id": cid},
            payload={},
        )
    )
    rbody = json.loads(r_rev.text or "{}")
    assert rbody["ok"] is True
    assert rbody["action"] == "restored"
    assert target.read_text(encoding="utf-8") == "v1\n"
