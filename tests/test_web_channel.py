from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from aiohttp import hdrs, web
from aiohttp.test_utils import make_mocked_request

from corpclaw_lite.agent.factory import AgentStack
from corpclaw_lite.channels.service import AgentRequestService, is_llm_transport_error
from corpclaw_lite.channels.web.chat_store import WebChatFile, WebChatStore
from corpclaw_lite.channels.web.files import (
    build_tree,
    copy_paths,
    delete_path,
    delete_paths,
    list_directory,
    list_recent_files,
    make_directory,
    move_paths,
    preview_file,
    rename_path,
    resolve_workspace_path,
    save_upload,
    search_files,
)
from corpclaw_lite.channels.web.orchestrator import WebChannelOrchestrator, _DownloadGrant
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.config.settings import RoutingRule, Settings
from corpclaw_lite.exceptions import LLMBackendUnavailableError
from corpclaw_lite.users.manager import UserManager
from corpclaw_lite.users.models import User


class APIConnectionError(Exception):
    pass


APIConnectionError.__module__ = "openai"


class InternalServerError(Exception):
    pass


InternalServerError.__module__ = "openai"


class FakeWorkspaceService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def get_user_workspace(self, _user: User) -> Path:
        return self.workspace


def web_request(
    method: str,
    path: str,
    user: User,
    *,
    match_info: dict[str, str] | None = None,
) -> web.Request:
    request = make_mocked_request(method, path, match_info=match_info or {})
    request["user"] = user
    return request


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
async def test_web_recent_files_are_workspace_relative_and_recent_first(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "archive").mkdir()
    older = workspace / "archive" / "older.txt"
    newer = workspace / "newer.txt"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_010_000, 1_700_010_000))

    recent = await list_recent_files(workspace, limit=2)

    assert [entry["path"] for entry in recent] == ["newer.txt", "archive/older.txt"]
    assert all(entry["is_dir"] is False for entry in recent)


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


@pytest.mark.asyncio
async def test_web_download_forces_attachment_with_original_filename(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    filename = "отчет май.txt"
    (workspace / filename).write_text("hello", encoding="utf-8")
    user = User(id=1, name="Vadim", department="engineering")
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeWorkspaceService(workspace)  # type: ignore[assignment]

    response = await orchestrator._handle_download_file(
        web_request("GET", f"/api/files/download?path={quote(filename)}", user)
    )

    disposition = response.headers[hdrs.CONTENT_DISPOSITION]
    assert disposition.startswith("attachment;")
    assert "download" not in disposition
    assert "filename*=UTF-8''" in disposition
    assert quote(filename, safe="") in disposition
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_web_inline_endpoint_allows_only_images(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "image.jpg").write_bytes(b"jpg")
    (workspace / "note.txt").write_text("hello", encoding="utf-8")
    user = User(id=1, name="Vadim", department="engineering")
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeWorkspaceService(workspace)  # type: ignore[assignment]

    response = await orchestrator._handle_inline_file(
        web_request("GET", "/api/files/inline?path=image.jpg", user)
    )

    assert response.headers[hdrs.CONTENT_DISPOSITION].startswith("inline;")
    with pytest.raises(web.HTTPNotFound):
        await orchestrator._handle_inline_file(
            web_request("GET", "/api/files/inline?path=note.txt", user)
        )


@pytest.mark.asyncio
async def test_web_image_preview_uses_inline_endpoint(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "image.jpg").write_bytes(b"jpg")
    user = User(id=1, name="Vadim", department="engineering")
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeWorkspaceService(workspace)  # type: ignore[assignment]

    response = await orchestrator._handle_preview_file(
        web_request("GET", "/api/files/preview?path=image.jpg", user)
    )
    payload = response.text

    assert payload is not None
    assert "/api/files/inline?path=image.jpg" in payload
    assert "/api/files/download" not in payload


@pytest.mark.asyncio
async def test_web_chat_file_payload_includes_path_only_for_available_workspace_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.txt").write_text("hello", encoding="utf-8")
    user = User(id=7, name="Vadim", department="engineering")
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeWorkspaceService(workspace)  # type: ignore[assignment]
    store = WebChatStore(tmp_path / "memory.db")
    available = await store.append_message(
        user_id=user.memory_key(),
        role="system",
        content="Файл готов к скачиванию.",
        tone="file",
        file=WebChatFile(name="report.txt", path="report.txt", caption="done"),
    )
    missing = await store.append_message(
        user_id=user.memory_key(),
        role="system",
        content="Файл готов к скачиванию.",
        tone="file",
        file=WebChatFile(name="missing.txt", path="missing.txt", caption="missing"),
    )

    available_payload = await orchestrator._chat_message_payload(available, user)
    missing_payload = await orchestrator._chat_message_payload(missing, user)

    assert available_payload["file"]["path"] == "report.txt"  # type: ignore[index]
    assert available_payload["file"]["available"] is True  # type: ignore[index]
    assert str(available_payload["file"]["url"]).startswith("/api/download/")  # type: ignore[index]
    assert "path" not in missing_payload["file"]  # type: ignore[operator]
    assert missing_payload["file"]["available"] is False  # type: ignore[index]


@pytest.mark.asyncio
async def test_web_workspace_overview_returns_safe_operational_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.txt").write_text("hello", encoding="utf-8")
    user = User(id=7, name="Vadim", department="engineering")
    settings = Settings()
    settings.llm.routing = [
        RoutingRule(task_kind="default", provider="llamacpp", model="qwen-local")
    ]
    orchestrator = WebChannelOrchestrator(settings)
    orchestrator._service = FakeWorkspaceService(workspace)  # type: ignore[assignment]
    store = WebChatStore(tmp_path / "memory.db")
    orchestrator._chat_store = store
    await store.append_message(
        user_id=user.memory_key(),
        role="system",
        content="Файл готов к скачиванию.",
        tone="file",
        file=WebChatFile(name="report.txt", path="report.txt", caption="done"),
    )

    response = await orchestrator._handle_workspace_overview(
        web_request("GET", "/api/workspace/overview", user)
    )
    payload = json.loads(response.text or "{}")

    assert payload["user"]["id"] == user.id
    assert payload["llm"] == {"provider": "llamacpp", "model": "qwen-local"}
    assert payload["recent_files"][0]["path"] == "report.txt"
    assert payload["recent_outputs"][0]["path"] == "report.txt"
    assert payload["recent_outputs"][0]["available"] is True
    assert payload["recent_outputs"][0]["url"].startswith("/api/download/")


@pytest.mark.asyncio
async def test_web_download_grant_is_owner_scoped_and_expires(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "report.pdf"
    target.write_bytes(b"pdf")
    owner = User(id=1, name="Vadim", department="engineering")
    other = User(id=2, name="Guest", department="engineering")
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeWorkspaceService(workspace)  # type: ignore[assignment]
    orchestrator._download_grants["valid"] = _DownloadGrant(
        user_id=owner.id,
        path=target,
        filename=target.name,
        caption="",
        expires_at=time.time() + 60,
    )
    orchestrator._download_grants["expired"] = _DownloadGrant(
        user_id=owner.id,
        path=target,
        filename=target.name,
        caption="",
        expires_at=time.time() - 1,
    )

    response = await orchestrator._handle_download_grant(
        web_request("GET", "/api/download/valid", owner, match_info={"token": "valid"})
    )

    assert response.headers[hdrs.CONTENT_DISPOSITION] == 'attachment; filename="report.pdf"'
    with pytest.raises(web.HTTPNotFound):
        await orchestrator._handle_download_grant(
            web_request("GET", "/api/download/valid", other, match_info={"token": "valid"})
        )
    with pytest.raises(web.HTTPNotFound):
        await orchestrator._handle_download_grant(
            web_request("GET", "/api/download/expired", owner, match_info={"token": "expired"})
        )
    assert "expired" not in orchestrator._download_grants


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


@pytest.mark.asyncio
async def test_web_chat_store_persists_and_pages_history(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    user_id = "7"

    first = await store.append_message(user_id=user_id, role="user", content="hello")
    second = await store.append_message(
        user_id=user_id,
        role="assistant",
        content="hi",
        request_id="req1",
        metadata={
            "usage": {
                "latest_total_tokens": 10,
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
                "context_limit_tokens": 100,
                "context_ratio": 0.1,
            }
        },
    )
    await store.append_message(
        user_id=user_id,
        role="system",
        content="Файл готов к скачиванию.",
        tone="file",
        file=WebChatFile(name="report.pdf", path="report.pdf", caption="done"),
    )

    page = await store.list_recent(user_id, limit=2)
    assert page.has_more is True
    assert [message.content for message in page.messages] == ["hi", "Файл готов к скачиванию."]
    assert page.messages[-1].file is not None
    assert page.messages[-1].file.name == "report.pdf"

    older = await store.list_before(user_id, before_id=second.id, limit=10)
    assert older.has_more is False
    assert [message.id for message in older.messages] == [first.id]

    usage = await store.latest_usage(user_id)
    assert usage is not None
    assert usage["latest_total_tokens"] == 10

    files = await store.latest_file_messages(user_id)
    assert [message.file.name for message in files if message.file is not None] == ["report.pdf"]


@pytest.mark.asyncio
async def test_web_chat_store_reset_archives_current_session(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    user_id = "7"
    first_session = await store.ensure_active_session(user_id)
    await store.append_message(user_id=user_id, role="user", content="old")

    second_session = await store.reset_session(user_id)
    page = await store.list_recent(user_id, limit=10)

    assert second_session != first_session
    assert page.session_id == second_session
    assert page.messages == []


@pytest.mark.asyncio
async def test_web_chat_store_prunes_active_messages_by_quota(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db", active_max_messages=2)
    user_id = "7"

    await store.append_message(user_id=user_id, role="user", content="one")
    await store.append_message(user_id=user_id, role="assistant", content="two")
    await store.append_message(user_id=user_id, role="user", content="three")

    page = await store.list_recent(user_id, limit=10)
    assert [message.content for message in page.messages] == ["two", "three"]


@pytest.mark.asyncio
async def test_web_chat_store_prunes_archived_sessions_by_quota(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    user_id = "7"

    for idx in range(3):
        await store.append_message(user_id=user_id, role="user", content=f"session {idx}")
        await store.reset_session(user_id)

    removed = await store.prune_retention(
        archived_session_ttl_days=30,
        max_archived_sessions_per_user=1,
    )

    assert removed == 2
    with sqlite3.connect(tmp_path / "memory.db") as conn:
        archived_count = conn.execute(
            "SELECT COUNT(*) FROM web_chat_sessions WHERE user_id = ? AND ended_at IS NOT NULL",
            (user_id,),
        ).fetchone()[0]
    assert archived_count == 1


@pytest.mark.asyncio
async def test_web_chat_store_backfills_visible_agent_memory(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    store = WebChatStore(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO messages (user_id, role, content)
            VALUES
                ('7', 'user', 'old question'),
                ('7', 'system', 'Tools called in this turn: none'),
                ('7', 'assistant', '[Conversation summary]: compacted'),
                ('7', 'assistant', 'old answer')
            """
        )

    inserted = await store.backfill_from_memory("7")
    page = await store.list_recent("7", limit=10)

    assert inserted == 2
    assert [(message.role, message.content) for message in page.messages] == [
        ("user", "old question"),
        ("assistant", "old answer"),
    ]


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


def test_web_login_failures_lock_out_key() -> None:
    orchestrator = WebChannelOrchestrator(Settings())
    request = make_mocked_request("POST", "/api/login", headers={"Host": "localhost"})
    key = orchestrator._login_attempt_key(request, "Alice")

    retry_after = 0
    for _ in range(orchestrator._web_settings.login_lockout_threshold):
        retry_after = orchestrator._record_login_failure(key)

    assert retry_after == orchestrator._web_settings.login_lockout_seconds
    assert orchestrator._login_retry_after(key) > 0
    orchestrator._record_login_success(key)
    assert orchestrator._login_retry_after(key) == 0


def test_web_session_cookie_auto_secure_local_http() -> None:
    settings = Settings()
    settings.web_channel.cookie_secure = "auto"
    orchestrator = WebChannelOrchestrator(settings)
    request = make_mocked_request("GET", "/", headers={"Host": "127.0.0.1"})
    response = web.Response()

    orchestrator._set_session_cookie(request, response, "token")

    cookie_header = response.cookies.output(header="")
    assert "HttpOnly" in cookie_header
    assert "SameSite=Strict" in cookie_header
    assert "Secure" not in cookie_header


def test_web_session_cookie_auto_secure_forwarded_https() -> None:
    settings = Settings()
    settings.web_channel.cookie_secure = "auto"
    orchestrator = WebChannelOrchestrator(settings)
    request = make_mocked_request(
        "GET",
        "/",
        headers={"Host": "corpclaw.example", "X-Forwarded-Proto": "https"},
    )
    response = web.Response()

    orchestrator._set_session_cookie(request, response, "token")

    assert "Secure" in response.cookies.output(header="")


@pytest.mark.asyncio
async def test_websocket_ticket_is_single_use_and_owner_scoped() -> None:
    owner = User(id=1, name="Vadim", department="engineering")
    other = User(id=2, name="Guest", department="engineering")
    orchestrator = WebChannelOrchestrator(Settings())

    response = await orchestrator._handle_ws_ticket(web_request("POST", "/api/ws-ticket", owner))
    payload = json.loads(response.text or "{}")
    ticket = payload["ticket"]

    assert orchestrator._consume_ws_ticket(ticket, other) is False
    assert orchestrator._consume_ws_ticket(ticket, owner) is False

    ticket, _ttl = orchestrator._create_ws_ticket(owner)
    assert orchestrator._consume_ws_ticket(ticket, owner) is True
    assert orchestrator._consume_ws_ticket(ticket, owner) is False


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
async def test_web_reset_context_archives_web_chat_session(tmp_path: Path) -> None:
    class FakeService:
        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, _user_id: int) -> None:
            return None

        async def reset_user_context(self, _user: User) -> None:
            return None

    store = WebChatStore(tmp_path / "memory.db")
    await store.append_message(user_id="7", role="user", content="old")
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeService()  # type: ignore[assignment]
    orchestrator._chat_store = store
    user = User(id=7, name="Vadim", department="engineering")

    ok, _message, _usage = await orchestrator._reset_context_for_user(user)
    page = await store.list_recent(user.memory_key(), limit=10)

    assert ok is True
    assert page.messages == []


@pytest.mark.asyncio
async def test_web_context_usage_restored_from_chat_store(tmp_path: Path) -> None:
    store = WebChatStore(tmp_path / "memory.db")
    await store.append_message(
        user_id="7",
        role="assistant",
        content="answer",
        metadata={
            "usage": {
                "latest_total_tokens": 55,
                "input_tokens": 40,
                "output_tokens": 15,
                "total_tokens": 55,
                "context_limit_tokens": 1000,
                "context_ratio": 0.055,
            }
        },
    )
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._chat_store = store
    user = User(id=7, name="Vadim", department="engineering")

    usage = await orchestrator._context_usage_for_user(user)

    assert usage["latest_total_tokens"] == 55
    assert usage["context_ratio"] == 0.055


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


@pytest.mark.asyncio
async def test_web_container_prune_loop_calls_prune_and_survives_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container pruner calls prune_idle each pass and keeps running on error."""

    class FakeContainerManager:
        def __init__(self) -> None:
            self.calls = 0

        async def prune_idle(self) -> int:
            self.calls += 1
            # First pass raises a transient Docker error — the loop must survive.
            if self.calls == 1:
                raise RuntimeError("docker daemon hiccup")
            return 0

    class FakeStack:
        def __init__(self, container_manager: FakeContainerManager) -> None:
            self.container_manager = container_manager

    cm = FakeContainerManager()
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._stack = FakeStack(cm)

    # Drive the loop with a near-zero interval instead of the real 300s.
    monkeypatch.setattr(
        "corpclaw_lite.channels.web.orchestrator._CONTAINER_PRUNE_INTERVAL_SECONDS", 0
    )

    # Cancel after enough iterations to exercise both the error and the success paths.
    task = asyncio.create_task(orchestrator._container_prune_loop())
    # Yield control so the loop runs a couple of passes.
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert cm.calls >= 2  # one erroring pass + one healthy pass


@pytest.mark.asyncio
async def test_web_container_prune_loop_noop_without_container_manager() -> None:
    """The pruner returns immediately when there is no container_manager (dev mode)."""
    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._stack = None  # dev mode: no stack at all
    # Should return without entering the loop (no await asyncio.sleep).
    await asyncio.wait_for(orchestrator._container_prune_loop(), timeout=1.0)
