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
from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.config.settings import Settings
from corpclaw_lite.exceptions import LLMBackendUnavailableError
from corpclaw_lite.users.manager import UserManager
from corpclaw_lite.users.models import User


class APIConnectionError(Exception):
    pass


APIConnectionError.__module__ = "openai"


class InternalServerError(Exception):
    pass


InternalServerError.__module__ = "openai"


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
    assert (
        is_llm_transport_error(
            InternalServerError(
                "Error code: 502 - "
                "{'error': {'message': 'Connection refused', 'type': 'upstream_error'}}"
            )
        )
        is True
    )
    assert is_llm_transport_error(ValueError("ordinary bug")) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        APIConnectionError("Connection error."),
        InternalServerError(
            "Error code: 502 - "
            "{'error': {'message': 'Connection refused', 'type': 'upstream_error'}}"
        ),
    ],
)
async def test_agent_request_service_converts_llm_connection_error(
    tmp_path: Path, error: Exception
) -> None:
    class FailingLoop:
        async def run(self, *_args: Any, **_kwargs: Any) -> tuple[str, Any]:
            raise error

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


@pytest.mark.asyncio
async def test_agent_request_service_resets_user_context(tmp_path: Path) -> None:
    class FakeMemory:
        def __init__(self) -> None:
            self.cleared: list[str] = []

        async def clear(self, user_id: str) -> None:
            self.cleared.append(user_id)

    class FakeLoop:
        def __init__(self) -> None:
            self.memory = FakeMemory()
            self.provider = object()

    loop = FakeLoop()
    stack = AgentStack(
        loop=loop,  # type: ignore[arg-type]
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
    )
    user = User(id=7, name="Vadim", department="engineering")

    await service.reset_user_context(user)

    assert loop.memory.cleared == [user.memory_key()]


def test_web_context_usage_payload_uses_latest_total_tokens() -> None:
    from corpclaw_lite.agent.loop import RunStats

    settings = Settings()
    settings.agent.compression.max_context_tokens = 1000
    stats = RunStats()
    stats.input_tokens = 800
    stats.output_tokens = 50
    stats.total_tokens = 1700
    stats.latest_total_tokens = 850

    payload = WebChannelOrchestrator(settings)._context_usage_payload(stats)

    assert payload["latest_total_tokens"] == 850
    assert payload["total_tokens"] == 1700
    assert payload["context_limit_tokens"] == 1000
    assert payload["context_ratio"] == 0.85


@pytest.mark.asyncio
async def test_web_reset_context_respects_active_request_lock() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.active = True
            self.reset_calls = 0
            self.finished: list[int] = []

        async def try_start_user_request(self, _user_id: int) -> bool:
            return not self.active

        async def finish_user_request(self, user_id: int) -> None:
            self.finished.append(user_id)

        async def reset_user_context(self, _user: User) -> None:
            self.reset_calls += 1

    service = FakeService()
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = service  # type: ignore[assignment]
    user = User(id=7, name="Vadim", department="engineering")

    ok, message, usage = await orchestrator._reset_context_for_user(user)

    assert ok is False
    assert "Предыдущая задача" in message
    assert usage["latest_total_tokens"] == 0
    assert service.reset_calls == 0
    assert service.finished == []


@pytest.mark.asyncio
async def test_web_reset_context_clears_usage_snapshot() -> None:
    class FakeService:
        def __init__(self) -> None:
            self.reset_calls = 0
            self.finished: list[int] = []

        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, user_id: int) -> None:
            self.finished.append(user_id)

        async def reset_user_context(self, _user: User) -> None:
            self.reset_calls += 1

    service = FakeService()
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = service  # type: ignore[assignment]
    orchestrator._context_usage[7] = {
        "latest_total_tokens": 100,
        "input_tokens": 100,
        "output_tokens": 0,
        "total_tokens": 100,
        "context_limit_tokens": 1000,
        "context_ratio": 0.1,
    }
    user = User(id=7, name="Vadim", department="engineering")

    ok, message, usage = await orchestrator._reset_context_for_user(user)

    assert ok is True
    assert "Сессия сброшена" in message
    assert usage["latest_total_tokens"] == 0
    assert orchestrator._context_usage[7]["latest_total_tokens"] == 0
    assert service.reset_calls == 1
    assert service.finished == [7]


@pytest.mark.asyncio
async def test_web_orchestrator_stop_cleans_managed_containers() -> None:
    class FakeContainerManager:
        def __init__(self) -> None:
            self.stopped = False

        async def stop_managed_async(self) -> None:
            self.stopped = True

    class FakeStack:
        def __init__(self, container_manager: FakeContainerManager) -> None:
            self.mcp_manager = None
            self.container_manager = container_manager

    container_manager = FakeContainerManager()
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._stack = FakeStack(container_manager)

    await orchestrator.stop()

    assert container_manager.stopped is True
