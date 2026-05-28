from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.agent.factory import AgentStack
from corpclaw_lite.channels.service import AgentRequestService, is_llm_transport_error
from corpclaw_lite.channels.web.files import (
    build_tree,
    copy_paths,
    delete_path,
    delete_paths,
    list_directory,
    make_directory,
    move_paths,
    preview_file,
    rename_path,
    resolve_workspace_path,
    save_upload,
    search_files,
)
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.exceptions import LLMBackendUnavailableError
from corpclaw_lite.users.manager import UserManager
from corpclaw_lite.users.models import User


class APIConnectionError(Exception):
    pass


APIConnectionError.__module__ = "openai"


def test_resolve_workspace_path_blocks_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert resolve_workspace_path(workspace, "docs").is_relative_to(workspace)

    with pytest.raises(PermissionError):
        resolve_workspace_path(workspace, "../outside.txt")


@pytest.mark.asyncio
async def test_web_file_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    folder = await make_directory(workspace, "", "reports")
    assert folder == "reports"

    rel = await save_upload(
        workspace=workspace,
        filename="report.txt",
        data=b"hello",
        max_bytes=100,
        target_dir="reports",
    )
    assert rel == "reports/report.txt"

    listing = await list_directory(workspace, "reports")
    entries = listing["entries"]
    assert isinstance(entries, list)
    assert entries[0]["name"] == "report.txt"
    assert entries[0]["kind"] == "text"
    assert entries[0]["extension"] == ".txt"

    await delete_path(workspace, "reports/report.txt")
    listing = await list_directory(workspace, "reports")
    assert listing["entries"] == []


@pytest.mark.asyncio
async def test_web_file_manager_operations(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    await make_directory(workspace, "", "reports")
    await make_directory(workspace, "", "archive")
    await save_upload(
        workspace=workspace,
        filename="report.txt",
        data=b"hello",
        max_bytes=100,
        target_dir="reports",
    )

    renamed = await rename_path(workspace, "reports/report.txt", "summary.txt")
    assert renamed == "reports/summary.txt"

    copied = await copy_paths(workspace, ["reports/summary.txt"], "archive")
    assert copied == ["archive/summary.txt"]
    assert (workspace / "archive" / "summary.txt").exists()

    moved = await move_paths(workspace, ["reports/summary.txt"], "archive")
    assert moved == ["archive/summary_1.txt"]
    assert not (workspace / "reports" / "summary.txt").exists()

    search = await search_files(workspace, "summary")
    entries = search["entries"]
    assert isinstance(entries, list)
    assert {entry["path"] for entry in entries} == {"archive/summary.txt", "archive/summary_1.txt"}

    preview = await preview_file(workspace, "archive/summary.txt")
    assert preview["type"] == "text"
    assert preview["content"] == "hello"

    tree = await build_tree(workspace)
    assert tree["name"] == "workspace"
    assert [child["name"] for child in tree["children"]] == ["archive", "reports"]

    deleted = await delete_paths(
        workspace,
        ["archive/summary.txt", "archive/summary_1.txt"],
        recursive=False,
    )
    assert deleted == ["archive/summary.txt", "archive/summary_1.txt"]


@pytest.mark.asyncio
async def test_web_upload_rejects_bad_extension(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(ValueError, match="File type"):
        await save_upload(
            workspace=workspace,
            filename="payload.exe",
            data=b"x",
            max_bytes=100,
        )


def test_llm_transport_error_detection() -> None:
    assert is_llm_transport_error(APIConnectionError("Connection error.")) is True
    assert is_llm_transport_error(ValueError("ordinary bug")) is False


@pytest.mark.asyncio
async def test_agent_request_service_converts_llm_connection_error(tmp_path: Path) -> None:
    class FailingLoop:
        async def run(self, *_args: Any, **_kwargs: Any) -> tuple[str, Any]:
            raise APIConnectionError("Connection error.")

    stack = AgentStack(
        loop=FailingLoop(),  # type: ignore[arg-type]
        user_manager=UserManager(db_path=str(tmp_path / "users.db")),
        tool_registry=None,  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
        skill_registry=None,
        plugin_registry=None,
        skill_matcher=None,
    )
    service = AgentRequestService(
        stack=stack,
        bootstrap=BootstrapLoader(tmp_path / "bootstrap"),
        workspace_base=tmp_path / "workspaces",
        llm_provider_name="llamacpp",
        llm_base_url="http://127.0.0.1:4000/v1",
    )
    user = User(id=1, name="Vadim", department="engineering")

    with pytest.raises(LLMBackendUnavailableError) as exc_info:
        await service.run(user=user, message="hello", mode="chat", channel="web")

    assert exc_info.value.provider_name == "llamacpp"
    assert exc_info.value.base_url == "http://127.0.0.1:4000/v1"
    assert "LLM backend недоступен" in exc_info.value.user_message()
