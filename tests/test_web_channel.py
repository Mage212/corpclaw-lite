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
from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
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


# --- M1 + L2: response-tone directive + shared system-prompt assembly --------


def test_tone_directive_default_is_empty() -> None:
    """'default' tone adds no directive (base SOUL.md tone line applies)."""
    from corpclaw_lite.users.manager import tone_directive

    assert tone_directive("default") == ""


def test_tone_directive_concise_and_detailed_differ() -> None:
    """concise/detailed return distinct non-empty directives."""
    from corpclaw_lite.users.manager import tone_directive

    concise = tone_directive("concise")
    detailed = tone_directive("detailed")
    assert concise and detailed
    assert concise != detailed
    assert "concise" in concise.lower()
    assert "thorough" in detailed.lower()


def test_tone_directive_unknown_returns_empty() -> None:
    """Unknown tone values fall back to empty (defensive)."""
    from corpclaw_lite.users.manager import tone_directive

    assert tone_directive("bogus") == ""
    assert tone_directive("") == ""


def _build_service(tmp_path: Path) -> tuple[AgentRequestService, UserManager]:
    """Minimal AgentRequestService with a real UserManager + empty bootstrap."""
    user_manager = UserManager(db_path=str(tmp_path / "users.db"))
    stack = AgentStack(
        loop=object(),  # type: ignore[arg-type]
        user_manager=user_manager,
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
    return service, user_manager


@pytest.mark.asyncio
async def test_build_system_prompt_injects_instructions_and_tone(tmp_path: Path) -> None:
    """Saved agent-context instructions + tone both reach the assembled prompt."""
    service, user_manager = _build_service(tmp_path)
    user = User(id=3, name="Vadim", department="engineering")

    user_manager.set_agent_context(user.id, instructions="Always cite sources.", tone="concise")
    prompt = await service.build_system_prompt(user)

    assert prompt is not None
    assert "Always cite sources." in prompt
    assert "You are talking to Vadim from the engineering department." in prompt
    # M1: the concise directive is injected (previously dead field).
    assert "Be concise" in prompt


@pytest.mark.asyncio
async def test_build_system_prompt_without_agent_context_is_not_none(tmp_path: Path) -> None:
    """With no saved agent context, the user-context line still yields a prompt."""
    service, _ = _build_service(tmp_path)
    user = User(id=4, name="Anna", department="marketing")

    prompt = await service.build_system_prompt(user)

    # No base/bootstrap files exist, so only the synthesized user_ctx part remains.
    assert prompt is not None
    assert "You are talking to Anna from the marketing department." in prompt


@pytest.mark.asyncio
async def test_tone_directive_reaches_agent_loop_via_service_run(tmp_path: Path) -> None:
    """End-to-end: run() passes the tone directive to the loop's system_prompt.

    A SpyLoop captures the system_prompt kwarg; we assert the concise directive
    survives the full run() path (bootstrap + agent-context load + tone mapping).
    """

    class SpyLoop:
        captured_system_prompt: str | None = None

        async def run(self, *_args: Any, **kwargs: Any) -> tuple[str, Any]:
            from corpclaw_lite.agent.loop import RunStats

            SpyLoop.captured_system_prompt = kwargs.get("system_prompt")
            return "ok", RunStats()

    spy = SpyLoop()
    user_manager = UserManager(db_path=str(tmp_path / "users.db"))
    stack = AgentStack(
        loop=spy,  # type: ignore[arg-type]
        user_manager=user_manager,
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
    user = User(id=5, name="Vadim", department="engineering")
    user_manager.set_agent_context(user.id, instructions="Be precise.", tone="detailed")

    await service.run(user=user, message="hi", mode="chat", channel="web")

    captured = SpyLoop.captured_system_prompt
    assert captured is not None
    assert "Be precise." in captured
    # M1 regression: tone directive is present (was missing before the fix).
    assert "Be thorough" in captured


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


# --- Issue 1/2: load_chat read_only by is_active; delete active w/ replacement --


async def _read_only_for_target(store: WebChatStore, user_id: str, session_id: int) -> bool:
    """Mirror of the orchestrator's load_chat read_only decision: editable when
    the target is the user's active session, read-only otherwise."""
    summary = await store.get_session(user_id, session_id)
    return not (summary is not None and summary.is_active)


@pytest.mark.asyncio
async def test_load_chat_active_session_is_editable(tmp_path: Path) -> None:
    """load_chat on the active chat must report read_only=False (edit mode)."""
    store = WebChatStore(tmp_path / "memory.db")
    # create_session archives the prior active one, so the LAST created is active.
    inactive_id = await store.create_session(user_id="7", section="chat")
    active_id = await store.create_session(user_id="7", section="chat")
    assert active_id != inactive_id

    active_summary = await store.get_session("7", active_id)
    inactive_summary = await store.get_session("7", inactive_id)
    assert active_summary is not None and active_summary.is_active is True
    assert inactive_summary is not None and inactive_summary.is_active is False

    assert await _read_only_for_target(store, "7", active_id) is False
    assert await _read_only_for_target(store, "7", inactive_id) is True


@pytest.mark.asyncio
async def test_load_chat_inactive_session_is_read_only(tmp_path: Path) -> None:
    """load_chat on a non-active chat reports read_only=True; reactivating flips it."""
    store = WebChatStore(tmp_path / "memory.db")
    first_id = await store.create_session(user_id="7", section="chat")
    second_id = await store.create_session(user_id="7", section="chat")
    # second is active, first is inactive
    first = await store.get_session("7", first_id)
    assert first is not None and first.is_active is False
    assert await _read_only_for_target(store, "7", first_id) is True

    # reactivate the first → it becomes editable, second becomes inactive
    await store.activate_session(user_id="7", session_id=first_id)
    assert await _read_only_for_target(store, "7", first_id) is False
    assert await _read_only_for_target(store, "7", second_id) is True


@pytest.mark.asyncio
async def test_delete_active_chat_creates_replacement(tmp_path: Path) -> None:
    """Deleting the active chat must leave a fresh active chat behind (invariant:
    exactly one active session per user) so the agent still has a place to write."""
    store = WebChatStore(tmp_path / "memory.db")
    active_id = await store.create_session(user_id="7", section="chat")
    before = await store.get_session("7", active_id)
    assert before is not None and before.is_active is True

    ok = await store.delete_session("7", active_id)
    assert ok is True

    # No active session right after deletion…
    sessions = await store.list_sessions("7")
    assert not any(s.is_active for s in sessions)

    # …so the orchestrator's delete handler calls ensure_active_session to create
    # a replacement. Verify that produces exactly one new active session.
    new_id = await store.ensure_active_session("7")
    assert new_id != active_id
    replacement = await store.get_session("7", new_id)
    assert replacement is not None and replacement.is_active is True
    sessions_after = await store.list_sessions("7")
    assert sum(1 for s in sessions_after if s.is_active) == 1


@pytest.mark.asyncio
async def test_delete_inactive_chat_makes_no_replacement(tmp_path: Path) -> None:
    """Deleting a non-active chat leaves the existing active chat untouched."""
    store = WebChatStore(tmp_path / "memory.db")
    inactive_id = await store.create_session(user_id="7", section="chat")
    active_id = await store.create_session(user_id="7", section="chat")
    assert active_id != inactive_id

    ok = await store.delete_session("7", inactive_id)
    assert ok is True

    # The active chat is unaffected.
    still_active = await store.get_session("7", active_id)
    assert still_active is not None and still_active.is_active is True
    gone = await store.get_session("7", inactive_id)
    assert gone is None


# --- C: on-demand compression of the active chat's context ---


@pytest.mark.asyncio
async def test_compress_active_context_success(tmp_path: Path) -> None:
    """_compress_active_context delegates to the service and refreshes usage."""

    class FakeCompressService:
        def __init__(self) -> None:
            self.compress_calls = 0

        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, _user_id: int) -> None:
            return None

        async def compress_user_context(
            self, _user: User, session_id: int | None = None
        ) -> tuple[bool, str]:
            self.compress_calls += 1
            return True, "Контекст сжат: 10 → 4 сообщений."

    orchestrator = WebChannelOrchestrator(Settings())
    service = FakeCompressService()
    orchestrator._service = service  # type: ignore[assignment]
    user = User(id=7, name="Vadim", department="engineering")

    ok, message, _usage = await orchestrator._compress_active_context(user)

    assert ok is True
    assert "сжат" in message
    assert service.compress_calls == 1


@pytest.mark.asyncio
async def test_compress_active_context_propagates_failure(tmp_path: Path) -> None:
    """When the service reports compression failed, _compress_active_context
    forwards the failure (ok=False) without raising."""

    class FakeCompressService:
        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, _user_id: int) -> None:
            return None

        async def compress_user_context(
            self, _user: User, session_id: int | None = None
        ) -> tuple[bool, str]:
            return False, "Слишком мало сообщений для сжатия."

    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._service = FakeCompressService()  # type: ignore[assignment]
    user = User(id=7, name="Vadim", department="engineering")

    ok, message, _usage = await orchestrator._compress_active_context(user)

    assert ok is False
    assert "мало" in message


@pytest.mark.asyncio
async def test_compress_active_context_with_explicit_session_id(tmp_path: Path) -> None:
    """B-063 S3: _compress_active_context(user, session_id=N) passes the explicit
    session_id to the service (does NOT resolve the active session). The session
    must be owned by the user (S3-audit ownership check)."""

    class SpyService:
        def __init__(self) -> None:
            self.passed_session_id: int | None = None

        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, _user_id: int) -> None:
            return None

        async def compress_user_context(
            self, _user: User, session_id: int | None = None
        ) -> tuple[bool, str]:
            self.passed_session_id = session_id
            return True, "Контекст сжат."

    db = tmp_path / "compress_own.db"
    ws = WebChatStore(db)
    user = User(id=7, name="Vadim", department="engineering")
    session_id = await ws.create_session(user_id=user.memory_key(), section="chat")

    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._chat_store = ws  # type: ignore[assignment]
    spy = SpyService()
    orchestrator._service = spy  # type: ignore[assignment]

    await orchestrator._compress_active_context(user, session_id=session_id)

    assert spy.passed_session_id == session_id


@pytest.mark.asyncio
async def test_compress_rejects_foreign_session_id(tmp_path: Path) -> None:
    """B-063 S3-audit F1: compressing a session owned by ANOTHER user is rejected
    (IDOR protection). Without the ownership check, the caller could destroy
    another user's context-store via replace_context."""

    class SpyService:
        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, _user_id: int) -> None:
            return None

        async def compress_user_context(
            self, _user: User, session_id: int | None = None
        ) -> tuple[bool, str]:
            return True, "SHOULD NOT BE CALLED"

    db = tmp_path / "compress_foreign.db"
    ws = WebChatStore(db)
    user_a = User(id=7, name="Alice", department="engineering")
    user_b = User(id=20, name="Bob", department="marketing")
    # Session owned by user B.
    foreign_session = await ws.create_session(user_id=user_b.memory_key(), section="chat")

    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._chat_store = ws  # type: ignore[assignment]
    orchestrator._service = SpyService()  # type: ignore[assignment]

    # User A tries to compress user B's chat.
    ok, message, _usage = await orchestrator._compress_active_context(
        user_a, session_id=foreign_session
    )

    assert ok is False
    assert "не найден" in message.lower() or "нет доступа" in message.lower()


@pytest.mark.asyncio
async def test_compress_rejects_nonexistent_session_id(tmp_path: Path) -> None:
    """B-063 S3-audit F1: compressing a non-existent session_id is rejected."""

    class SpyService:
        async def try_start_user_request(self, _user_id: int) -> bool:
            return True

        async def finish_user_request(self, _user_id: int) -> None:
            return None

        async def compress_user_context(
            self, _user: User, session_id: int | None = None
        ) -> tuple[bool, str]:
            return True, "SHOULD NOT BE CALLED"

    db = tmp_path / "compress_nonexist.db"
    ws = WebChatStore(db)
    user = User(id=7, name="Vadim", department="engineering")

    orchestrator = WebChannelOrchestrator(Settings())
    orchestrator._chat_store = ws  # type: ignore[assignment]
    orchestrator._service = SpyService()  # type: ignore[assignment]

    ok, message, _usage = await orchestrator._compress_active_context(user, session_id=99999)

    assert ok is False
    assert "не найден" in message.lower() or "нет доступа" in message.lower()


# --- B-063 S1: ChatContextStore (full LLM-context persistence per chat) ------


def test_chat_context_store_schema_created(tmp_path: Path) -> None:
    """The web_chat_context table + index exist after init."""
    db = tmp_path / "memory.db"
    # WebChatStore must exist first so the sessions table (FK target) is present.
    WebChatStore(db)
    ChatContextStore(db)  # creates web_chat_context schema
    import sqlite3

    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "web_chat_context" in tables
    assert "idx_web_chat_context_session" in indexes


@pytest.mark.asyncio
async def test_chat_context_store_append_list_roundtrip(tmp_path: Path) -> None:
    """Append user/assistant+tool_calls/tool messages; list reconstructs them
    with full structure (role/content/tool_calls/tool_call_id/name/reasoning)."""
    db = tmp_path / "memory.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id="7", section="chat")

    await store.append_context(
        session_id=session_id, user_id="7", role="user", content="нормализуй Excel"
    )
    await store.append_context(
        session_id=session_id,
        user_id="7",
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "normalize_excel", "arguments": '{"path":"a.xlsx"}'},
            }
        ],
        reasoning="thinking about the file",
    )
    await store.append_context(
        session_id=session_id,
        user_id="7",
        role="tool",
        content="normalized 4 rows",
        tool_call_id="call_1",
        name="normalize_excel",
    )
    await store.append_context(
        session_id=session_id, user_id="7", role="assistant", content="Готово!"
    )

    ctx = await store.list_context(session_id)
    assert len(ctx) == 4
    assert ctx[0] == {"role": "user", "content": "нормализуй Excel"}
    assert ctx[1]["role"] == "assistant"
    assert ctx[1]["content"] == ""
    assert ctx[1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "normalize_excel", "arguments": '{"path":"a.xlsx"}'},
        }
    ]
    assert ctx[1]["reasoning"] == "thinking about the file"
    assert ctx[2] == {
        "role": "tool",
        "content": "normalized 4 rows",
        "tool_call_id": "call_1",
        "name": "normalize_excel",
    }
    assert ctx[3] == {"role": "assistant", "content": "Готово!"}


@pytest.mark.asyncio
async def test_chat_context_store_seq_ordering(tmp_path: Path) -> None:
    """seq is monotonic per session and list returns insertion order."""
    db = tmp_path / "memory.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id="7", section="chat")

    s1 = await store.append_context(session_id=session_id, user_id="7", role="user", content="a")
    s2 = await store.append_context(
        session_id=session_id, user_id="7", role="assistant", content="b"
    )
    s3 = await store.append_context(session_id=session_id, user_id="7", role="user", content="c")
    assert (s1, s2, s3) == (1, 2, 3)

    ctx = await store.list_context(session_id)
    assert [m["content"] for m in ctx] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_chat_context_store_clear_and_replace(tmp_path: Path) -> None:
    """clear empties the session; replace atomically swaps the full context."""
    db = tmp_path / "memory.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id="7", section="chat")

    await store.append_context(session_id=session_id, user_id="7", role="user", content="old1")
    await store.append_context(session_id=session_id, user_id="7", role="assistant", content="old2")
    assert store.has_context(session_id)

    deleted = await store.clear_context(session_id)
    assert deleted == 2
    assert not store.has_context(session_id)
    assert await store.list_context(session_id) == []

    await store.replace_context(
        session_id=session_id,
        user_id="7",
        messages=[
            {"role": "user", "content": "new1"},
            {"role": "assistant", "content": "new2", "reasoning": "summarized"},
        ],
    )
    ctx = await store.list_context(session_id)
    assert len(ctx) == 2
    assert ctx[0]["content"] == "new1"
    assert ctx[1]["content"] == "new2"
    assert ctx[1]["reasoning"] == "summarized"


@pytest.mark.asyncio
async def test_chat_context_store_cascade_on_session_delete(tmp_path: Path) -> None:
    """Deleting a web chat session cascades to its context rows."""
    db = tmp_path / "memory.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id="7", section="chat")
    await store.append_context(session_id=session_id, user_id="7", role="user", content="x")
    assert store.has_context(session_id)

    await ws.delete_session("7", session_id)
    assert not store.has_context(session_id)
    assert await store.list_context(session_id) == []


@pytest.mark.asyncio
async def test_chat_context_store_replace_does_not_relabel_foreign_rows(tmp_path: Path) -> None:
    """B-067 store-layer defense-in-depth: replace_context DELETE is scoped by
    user_id, so calling it with a foreign user_id on a session the caller doesn't
    own cannot delete/relabel another user's rows. (The service-layer get_session
    check is the primary control; this is the belt-and-suspenders.)"""
    db = tmp_path / "memory.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    # Victim owns the session and has context rows stamped user_id="8".
    victim_session = await ws.create_session(user_id="8", section="chat")
    await store.append_context(
        session_id=victim_session, user_id="8", role="user", content="victim row"
    )

    # Attacker (user "7") calls replace_context on the victim's session_id.
    # Before B-067 the DELETE was WHERE session_id=? only, so this would wipe
    # the victim's rows and re-insert them stamped user_id="7" (re-attribution).
    # After B-067 the DELETE is scoped by user_id, so it matches nothing; the
    # subsequent INSERT then collides on UNIQUE(session_id, seq) — fail-closed.
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await store.replace_context(
            session_id=victim_session,
            user_id="7",  # attacker
            messages=[{"role": "user", "content": "attacker row"}],
        )

    # Victim's original rows must be untouched — no silent re-attribution.
    ctx = await store.list_context(victim_session)
    contents = [m["content"] for m in ctx]
    assert contents == ["victim row"]
    assert "attacker row" not in contents


@pytest.mark.asyncio
async def test_chat_context_store_unique_seq_under_concurrent_append(tmp_path: Path) -> None:
    """B-063 S1 audit: concurrent appends to the same session must not collide
    on seq. The UNIQUE(session_id, seq) index + retry-on-IntegrityError turns a
    race into a recovered insert, keeping ordering deterministic."""
    import asyncio

    db = tmp_path / "memory.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id="7", section="chat")

    # Fire several appends concurrently; with a plain MAX+1 and no UNIQUE these
    # would produce duplicate seqs. With UNIQUE + retry, all succeed distinctly.
    await asyncio.gather(
        *(
            store.append_context(
                session_id=session_id, user_id="7", role="user", content=f"msg {i}"
            )
            for i in range(8)
        )
    )

    ctx = await store.list_context(session_id)
    assert len(ctx) == 8
    # Every content is distinct and present (no lost inserts).
    contents = {m["content"] for m in ctx}
    assert contents == {f"msg {i}" for i in range(8)}


# --- B-063 S2: restore_user_context (activate=load) ---


@pytest.mark.asyncio
async def test_restore_user_context_loads_into_memory(tmp_path: Path) -> None:
    """restore_user_context clears memory and re-adds the chat's context-store
    messages (text only; tool-role skipped — reconstructed at run() time)."""
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    memory = SQLiteMemory(db_path=str(tmp_path / "m.db"))
    store = ChatContextStore(tmp_path / "m.db")
    ws = WebChatStore(tmp_path / "m.db")
    session_id = await ws.create_session(user_id="7", section="chat")
    # Seed the context store with a full turn (user/assistant+tool_calls/tool/assistant).
    await store.append_context(session_id=session_id, user_id="7", role="user", content="hi")
    await store.append_context(
        session_id=session_id,
        user_id="7",
        role="assistant",
        content="",
        tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "echo", "arguments": "{}"}}
        ],
    )
    await store.append_context(
        session_id=session_id,
        user_id="7",
        role="tool",
        content="echo:hi",
        tool_call_id="c1",
        name="echo",
    )
    await store.append_context(session_id=session_id, user_id="7", role="assistant", content="done")

    # Stub loop: restore_user_context only needs .memory and .provider.
    class _StubLoop:
        def __init__(self, mem):
            self.memory = mem
            self.provider = None

    stack = AgentStack(
        loop=_StubLoop(memory),  # type: ignore[arg-type]
        user_manager=UserManager(db_path=str(tmp_path / "u.db")),
        chat_context_store=store,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
    )
    service = AgentRequestService(stack=stack, bootstrap=None, workspace_base=tmp_path / "ws")  # type: ignore[arg-type]
    user = User(id=7, name="Vadim", department="engineering")

    # Pre-seed memory with something else, so we can confirm it's cleared.
    await memory.add_message(user.memory_key(), "user", "STALE")
    restored = await service.restore_user_context(user, session_id)

    assert restored is True
    history = await memory.get_history(user.memory_key(), limit=50)
    # tool-role is skipped in memory (text-only); user + assistant text present.
    contents = [m["content"] for m in history]
    assert "hi" in contents
    assert "done" in contents
    assert "STALE" not in contents  # cleared
    assert "echo:hi" not in contents  # tool-role skipped


@pytest.mark.asyncio
async def test_restore_user_context_returns_false_for_empty_store(tmp_path: Path) -> None:
    """When the context-store has no data for the session, restore returns False
    (caller falls back to reset_user_context)."""
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

    store = ChatContextStore(tmp_path / "e.db")
    WebChatStore(tmp_path / "e.db")

    class _StubLoop:
        def __init__(self):
            self.memory = None
            self.provider = None

    stack = AgentStack(
        loop=_StubLoop(),  # type: ignore[arg-type]
        user_manager=UserManager(db_path=str(tmp_path / "u.db")),
        chat_context_store=store,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
    )
    service = AgentRequestService(stack=stack, bootstrap=None, workspace_base=tmp_path / "ws")  # type: ignore[arg-type]
    user = User(id=7, name="Vadim", department="engineering")

    restored = await service.restore_user_context(user, session_id=999)  # never created
    assert restored is False


@pytest.mark.asyncio
async def test_restore_user_context_rejects_foreign_session(tmp_path: Path) -> None:
    """B-067: restore_user_context verifies session ownership at the service
    layer. A foreign session_id (owned by another user) returns False without
    reading the transcript — closing the IDOR-by-read gap for any caller that
    forgets the orchestrator's get_session pre-check."""
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    db = tmp_path / "idor.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    # Attacker is user 7; victim is user 8. Victim owns session_id.
    victim_session = await ws.create_session(user_id="8", section="chat")
    await store.append_context(
        session_id=victim_session, user_id="8", role="user", content="victim secret"
    )

    memory = SQLiteMemory(db_path=str(tmp_path / "m.db"))

    class _StubLoop:
        def __init__(self, mem):
            self.memory = mem
            self.provider = None

    stack = AgentStack(
        loop=_StubLoop(memory),  # type: ignore[arg-type]
        user_manager=UserManager(db_path=str(tmp_path / "u.db")),
        chat_context_store=store,
        chat_store=ws,  # B-067: wired so the service can verify ownership
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
    )
    service = AgentRequestService(stack=stack, bootstrap=None, workspace_base=tmp_path / "ws")  # type: ignore[arg-type]
    attacker = User(id=7, name="Attacker", department="engineering")

    restored = await service.restore_user_context(attacker, victim_session)
    assert restored is False
    # Memory must be untouched (not cleared, no victim content leaked).
    history = await memory.get_history(attacker.memory_key(), limit=50)
    assert all("victim secret" not in m.get("content", "") for m in history)


@pytest.mark.asyncio
async def test_compress_user_context_rejects_foreign_session(tmp_path: Path) -> None:
    """B-067: compress_user_context verifies ownership at the service layer.
    A foreign session_id returns (False, msg) without invoking compress_now."""
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

    db = tmp_path / "idor_c.db"
    ws = WebChatStore(db)
    ChatContextStore(db)
    victim_session = await ws.create_session(user_id="8", section="chat")

    class _StubLoop:
        def __init__(self):
            self.memory = None
            self.provider = None

        async def compress_now(self, user, session_id=None):  # type: ignore[no-untyped-def]
            raise AssertionError("compress_now must not run for a foreign session")

    stack = AgentStack(
        loop=_StubLoop(),  # type: ignore[arg-type]
        user_manager=UserManager(db_path=str(tmp_path / "u.db")),
        chat_context_store=None,
        chat_store=ws,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
    )
    service = AgentRequestService(stack=stack, bootstrap=None, workspace_base=tmp_path / "ws")  # type: ignore[arg-type]
    attacker = User(id=7, name="Attacker", department="engineering")

    ok, message = await service.compress_user_context(attacker, session_id=victim_session)
    assert ok is False
    assert "не найден" in message.lower() or "нет доступа" in message.lower()


@pytest.mark.asyncio
async def test_restore_user_context_handles_memory_failure(tmp_path: Path) -> None:
    """B4 fix: restore_user_context wraps clear+add in try/except. If memory
    sync fails mid-loop, the restore returns True (non-fatal — the context store
    is the source of truth)."""
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    db = tmp_path / "memfail.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id="7", section="chat")
    await store.append_context(session_id=session_id, user_id="7", role="user", content="hi")
    await store.append_context(
        session_id=session_id, user_id="7", role="assistant", content="hello"
    )

    memory = SQLiteMemory(db_path=str(db))

    # Patch add_message to raise after the first call (simulates mid-loop failure).
    original_add = memory.add_message
    call_count = 0

    async def failing_add(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            raise RuntimeError("simulated storage failure")
        return await original_add(*args, **kwargs)

    memory.add_message = failing_add  # type: ignore[assignment]

    class _StubLoop:
        def __init__(self, mem):
            self.memory = mem
            self.provider = None

    stack = AgentStack(
        loop=_StubLoop(memory),  # type: ignore[arg-type]
        user_manager=UserManager(db_path=str(tmp_path / "u.db")),
        chat_context_store=store,
        tool_registry=ToolRegistry(),  # type: ignore[arg-type]
        full_tool_registry=None,
        mcp_manager=None,
        container_manager=None,
    )
    service = AgentRequestService(
        stack=stack,
        bootstrap=None,
        workspace_base=tmp_path / "ws",  # type: ignore[arg-type]
    )
    user = User(id=7, name="Vadim", department="engineering")

    # Should return True despite the memory failure (non-fatal).
    restored = await service.restore_user_context(user, session_id)
    assert restored is True
