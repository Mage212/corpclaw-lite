# ruff: noqa: E501
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from aiohttp import WSMsgType, hdrs, web
from aiohttp.helpers import content_disposition_header

from corpclaw_lite.agent.factory import build_agent_stack
from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.channels.service import AgentRequestCallbacks, AgentRequestService
from corpclaw_lite.channels.status import (
    INITIAL_STATUS_TEXT,
    READY_STATUS_TEXT,
    format_llm_queue_status,
    format_llm_stage_status,
    format_subagent_llm_queue_status,
    format_subagent_llm_stage_status,
    format_subagent_tool_batch_status,
    format_subagent_tool_status,
    format_tool_batch_status,
    format_tool_status,
)
from corpclaw_lite.channels.telegram.rate_limit import RateLimiter
from corpclaw_lite.channels.web.chat_store import (
    ChatSessionSummary,
    WebChatFile,
    WebChatMessage,
    WebChatStore,
)
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
    save_upload_stream,
    search_files,
)
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.config.settings import Settings, WebChannelSettings
from corpclaw_lite.exceptions import LLMBackendUnavailableError
from corpclaw_lite.extensions.mcp.watcher import MCPHotReloader
from corpclaw_lite.extensions.plugins.watcher import PluginHotReloader

# Etap 4: hot-reload watchers (lazy imports to avoid circular deps at module load).
from corpclaw_lite.extensions.skills.watcher import SkillHotReloader
from corpclaw_lite.extensions.subagents.watcher import SubagentHotReloader
from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool
from corpclaw_lite.logging.agent_logger import AgentLogger, setup_logging
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.runtime.shutdown import install_signal_handlers
from corpclaw_lite.users.models import User

__all__ = [
    "WebChannelOrchestrator",
]

logger = logging.getLogger(__name__)

_MAX_WS_MESSAGE_CHARS = 20_000
_MAX_BATCH_PATHS = 100
_DOWNLOAD_GRANT_TTL_SECONDS = 24 * 60 * 60
# Interval for the idle-container pruner background loop (seconds). Half the
# default idle_timeout (600s) so a container is reaped within ~2 cycles.
_CONTAINER_PRUNE_INTERVAL_SECONDS = 300
_INLINE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_FRONTEND_DIST = PROJECT_ROOT / "frontend" / "web" / "dist"
# Auto-naming: first user message truncated to this many chars (no LLM call).
_CHAT_TITLE_MAX_CHARS = 25


def _derive_chat_title(text: str) -> str | None:
    """Derive a chat title from the first user message via truncation.

    Collapses whitespace, caps at _CHAT_TITLE_MAX_CHARS, appends an ellipsis when
    truncated. Returns None for blank/whitespace-only input (no title to set).
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    if len(cleaned) <= _CHAT_TITLE_MAX_CHARS:
        return cleaned
    return cleaned[:_CHAT_TITLE_MAX_CHARS].rstrip() + "…"


@dataclass(slots=True)
class _DownloadGrant:
    user_id: int
    path: Path
    filename: str
    caption: str
    expires_at: float


@dataclass(slots=True)
class _WebSocketTicket:
    user_id: int
    expires_at: float


@dataclass(slots=True)
class _LoginAttemptState:
    failures: list[float]
    lockout_until: float = 0.0


@dataclass(slots=True)
class _PendingApproval:
    user_id: int
    approval_id: str
    action: str
    details: str
    future: asyncio.Future[bool]
    created_at: float


class WebChannelOrchestrator:
    """Aiohttp-based web channel with local auth, files and agent chat."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._web_settings: WebChannelSettings = settings.web_channel
        self._stack: Any | None = None
        self._service: AgentRequestService | None = None
        self._runner: web.AppRunner | None = None
        self._shutdown_event = asyncio.Event()
        self._rate_limiter = RateLimiter(self._web_settings.rate_limit_per_minute)
        self._clients: dict[int, set[web.WebSocketResponse]] = {}
        self._download_grants: dict[str, _DownloadGrant] = {}
        self._ws_tickets: dict[str, _WebSocketTicket] = {}
        self._login_attempts: dict[str, _LoginAttemptState] = {}
        self._context_usage: dict[int, dict[str, object]] = {}
        self._active_request_state: dict[int, dict[str, object]] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._chat_store: WebChatStore | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._container_prune_task: asyncio.Task[None] | None = None
        # Etap 4: hot-reload watchers (started in start(), stopped in stop()).
        self._skill_reloader: SkillHotReloader | None = None
        self._subagent_reloader: SubagentHotReloader | None = None
        self._plugin_reloader: PluginHotReloader | None = None
        self._mcp_reloader: MCPHotReloader | None = None
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._started = False

    async def start(self) -> None:
        log_cfg = self._settings.logging
        setup_logging(
            log_dir=PROJECT_ROOT / log_cfg.log_dir,
            level=log_cfg.level,
            console_level=log_cfg.console_level,
            trace_enabled=log_cfg.trace_enabled,
            trace_level=log_cfg.trace_level,
            trace_preview_chars=log_cfg.trace_preview_chars,
            capture_enabled=log_cfg.capture_enabled,
            capture_fields=log_cfg.capture_fields,
            capture_dir=PROJECT_ROOT / (log_cfg.capture_dir or log_cfg.log_dir),
        )

        stack = build_agent_stack(self._settings)
        self._stack = stack
        workspace_base = (PROJECT_ROOT / self._web_settings.workspace_base).resolve()
        llm_provider_name, llm_base_url = _default_llm_endpoint(self._settings)
        self._service = AgentRequestService(
            stack=stack,
            bootstrap=BootstrapLoader(PROJECT_ROOT / "config" / "bootstrap"),
            workspace_base=workspace_base,
            activity_logger=AgentLogger(log_dir=PROJECT_ROOT / log_cfg.log_dir),
            llm_provider_name=llm_provider_name,
            llm_base_url=llm_base_url,
        )
        memory = stack.loop.memory
        memory_db_path = getattr(memory, "db_path", PROJECT_ROOT / "data" / "memory.db")
        self._chat_store = WebChatStore(
            memory_db_path,
            active_max_messages=self._web_settings.chat_active_max_messages,
        )

        stack.tool_registry.register(
            SendFileTool(self._send_file_callback, workspace_base=workspace_base),
            allow_replace=True,
        )

        if stack.mcp_manager is not None:
            try:
                count = await stack.mcp_manager.connect_all(stack.tool_registry)
                logger.info("MCP: connected, %d tools registered", count)
            except Exception as e:
                logger.error("MCP: failed to connect — %s (continuing without MCP tools)", e)

        # Etap 4: start hot-reload watchers (same pattern as Telegram orchestrator).
        from corpclaw_lite.extensions.paths import resolve_dirs as _resolve_dirs

        skills_dirs = list(_resolve_dirs("skills", self._settings, PROJECT_ROOT))
        plugins_dirs = list(_resolve_dirs("plugins", self._settings, PROJECT_ROOT))
        if stack.skill_registry is not None:
            self._skill_reloader = SkillHotReloader(skills_dirs, stack.skill_registry)
            self._skill_reloader.start()
            logger.info("Skill hot-reloader started watching %s", skills_dirs)
        if stack.plugin_registry is not None and stack.skill_registry is not None:
            self._plugin_reloader = PluginHotReloader(
                plugins_dirs, stack.plugin_registry, stack.tool_registry, stack.skill_registry
            )
            self._plugin_reloader.start()
            logger.info("Plugin hot-reloader started watching %s", plugins_dirs)
        if stack.mcp_manager is not None:
            mcp_cfg_paths = list(_resolve_dirs("mcp", self._settings, PROJECT_ROOT))
            self._mcp_reloader = MCPHotReloader(
                mcp_cfg_paths, stack.mcp_manager, stack.tool_registry
            )
            self._mcp_reloader.start()
            logger.info("MCP hot-reloader started watching %s", mcp_cfg_paths)
        if stack.subagent_registry is not None:
            subagents_dirs = [
                d for d in _resolve_dirs("subagents", self._settings, PROJECT_ROOT) if d.exists()
            ]
            if subagents_dirs:
                self._subagent_reloader = SubagentHotReloader(
                    subagents_dirs, stack.subagent_registry
                )
                self._subagent_reloader.start()
                logger.info("Subagent hot-reloader started watching %s", subagents_dirs)

        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._web_settings.host, self._web_settings.port)
        await site.start()
        install_signal_handlers(self._shutdown_event)
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
        # Background idle-container pruner (prevents container accumulation in
        # server mode — prune_idle was previously CLI-only).
        if self._stack is not None and self._stack.container_manager is not None:
            self._container_prune_task = asyncio.create_task(self._container_prune_loop())
            logger.info("Container prune loop started (interval=300s)")
        self._started = True
        logger.info(
            "Web channel started at http://%s:%d",
            self._web_settings.host,
            self._web_settings.port,
        )

    async def run_until_shutdown(self) -> None:
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        if self._container_prune_task is not None:
            self._container_prune_task.cancel()
        # Etap 4: stop hot-reload watchers.
        for reloader in (
            self._skill_reloader,
            self._subagent_reloader,
            self._plugin_reloader,
            self._mcp_reloader,
        ):
            if reloader is not None:
                reloader.stop()
        for sockets in list(self._clients.values()):
            for ws in list(sockets):
                await ws.close()
        for task in list(self._request_tasks):
            task.cancel()
        if self._request_tasks:
            await asyncio.gather(*self._request_tasks, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
        if self._stack is not None and self._stack.mcp_manager is not None:
            await self._stack.mcp_manager.disconnect_all()
        if self._stack is not None and self._stack.container_manager is not None:
            try:
                await self._stack.container_manager.stop_managed_async()
                logger.info("Web channel containers stopped.")
            except Exception as e:
                logger.warning("Web container cleanup failed: %s", e)
        if self._started:
            logger.info("Web channel stopped cleanly.")
            self._started = False
        else:
            logger.debug("Web channel cleanup completed before full startup.")

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._error_middleware, self._auth_middleware])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/favicon.svg", self._handle_favicon)
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/login", self._handle_login)
        app.router.add_post("/logout", self._handle_logout)
        app.router.add_get("/api/session", self._handle_session)
        app.router.add_get("/api/workspace/overview", self._handle_workspace_overview)
        app.router.add_get("/api/chats", self._handle_list_chats)
        app.router.add_post("/api/chats", self._handle_create_chat)
        app.router.add_get("/api/extensions", self._handle_list_extensions)
        app.router.add_post("/api/extensions/reload", self._handle_reload_extensions)
        app.router.add_post("/api/chats/{id}/activate", self._handle_activate_chat)
        app.router.add_patch("/api/chats/{id}", self._handle_update_chat)
        app.router.add_delete("/api/chats/{id}", self._handle_delete_chat)
        app.router.add_post("/api/login", self._handle_api_login)
        app.router.add_post("/api/logout", self._handle_api_logout)
        app.router.add_post("/api/ws-ticket", self._handle_ws_ticket)
        app.router.add_get("/ws/chat", self._handle_chat_ws)
        app.router.add_get("/api/files", self._handle_list_files)
        app.router.add_get("/api/files/tree", self._handle_tree_files)
        app.router.add_get("/api/files/search", self._handle_search_files)
        app.router.add_get("/api/files/preview", self._handle_preview_file)
        app.router.add_post("/api/files/upload", self._handle_upload)
        app.router.add_post("/api/files/mkdir", self._handle_mkdir)
        app.router.add_post("/api/files/rename", self._handle_rename)
        app.router.add_post("/api/files/move", self._handle_move)
        app.router.add_post("/api/files/copy", self._handle_copy)
        app.router.add_post("/api/files/delete", self._handle_delete_batch)
        app.router.add_delete("/api/files", self._handle_delete)
        app.router.add_get("/api/files/download", self._handle_download_file)
        app.router.add_get("/api/files/inline", self._handle_inline_file)
        app.router.add_get("/api/download/{token}", self._handle_download_grant)
        assets_dir = _FRONTEND_DIST / "assets"
        if assets_dir.exists():
            app.router.add_static("/assets", assets_dir, show_index=False)
        return app

    @web.middleware
    async def _error_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except PermissionError as e:
            if request.path.startswith("/api/"):
                return web.json_response({"error": str(e)}, status=403)
            raise web.HTTPForbidden(text=str(e)) from e
        except FileNotFoundError as e:
            if request.path.startswith("/api/"):
                return web.json_response({"error": str(e)}, status=404)
            raise web.HTTPNotFound(text=str(e)) from e
        except (FileExistsError, ValueError) as e:
            if request.path.startswith("/api/"):
                return web.json_response({"error": str(e)}, status=400)
            raise web.HTTPBadRequest(text=str(e)) from e

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        token = request.cookies.get(self._web_settings.cookie_name)
        request["user"] = None
        request["csrf_token"] = ""
        if token and self._stack is not None:
            session = self._stack.user_manager.get_user_by_session(token)
            if session is not None:
                user, csrf_token = session
                request["user"] = user
                request["csrf_token"] = csrf_token

        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path not in {
            "/api/login",
            "/api/logout",
            "/login",
            "/logout",
        }:
            user = request.get("user")
            if not isinstance(user, User):
                raise web.HTTPUnauthorized()
            header = request.headers.get("X-CSRF-Token", "")
            if not header or header != request.get("csrf_token"):
                raise web.HTTPForbidden(text="CSRF validation failed")
        return await handler(request)

    @staticmethod
    def _redirect(location: str) -> web.HTTPFound:
        return web.HTTPFound(location=location)

    @staticmethod
    def _user_payload(user: User) -> dict[str, object]:
        return {
            "id": user.id,
            "name": user.name,
            "username": user.username,
            "department": user.department,
            "is_admin": user.is_admin,
        }

    @staticmethod
    async def _json_payload(request: web.Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except Exception as e:
            raise web.HTTPBadRequest(text="Некорректный JSON в запросе.") from e
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Ожидался JSON-объект.")
        return payload

    @staticmethod
    def _paths_from_payload(payload: dict[str, object]) -> list[str]:
        raw_paths = payload.get("paths")
        if raw_paths is None:
            raw_path = payload.get("path")
            raw_paths = [raw_path] if raw_path is not None else []
        if not isinstance(raw_paths, list):
            raise web.HTTPBadRequest(text="paths must be a list")
        if len(raw_paths) > _MAX_BATCH_PATHS:
            raise web.HTTPBadRequest(text="Too many paths")
        paths: list[str] = []
        for raw_path in raw_paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise web.HTTPBadRequest(text="Invalid path")
            paths.append(raw_path)
        if not paths:
            raise web.HTTPBadRequest(text="Missing path")
        return paths

    def _session_cookie_secure(self, request: web.Request) -> bool:
        setting = self._web_settings.cookie_secure
        if isinstance(setting, bool):
            return setting
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        first_proto = forwarded_proto.split(",", 1)[0].strip().lower()
        return request.secure or first_proto == "https"

    def _set_session_cookie(
        self, request: web.Request, response: web.StreamResponse, token: str
    ) -> None:
        response.set_cookie(
            self._web_settings.cookie_name,
            token,
            httponly=True,
            samesite="Strict",
            max_age=self._web_settings.session_ttl_hours * 3600,
            secure=self._session_cookie_secure(request),
        )

    def _clear_session_cookie(self, response: web.StreamResponse) -> None:
        response.del_cookie(self._web_settings.cookie_name)

    def _authenticate(self, username: str, password: str) -> User | None:
        if self._stack is None:
            raise web.HTTPServiceUnavailable()
        return self._stack.user_manager.authenticate_web_user(username, password)

    def _create_session_response(self, user: User) -> tuple[str, str]:
        if self._stack is None:
            raise web.HTTPServiceUnavailable()
        return self._stack.user_manager.create_web_session(
            user.id, ttl_hours=self._web_settings.session_ttl_hours
        )

    def _login_attempt_key(self, request: web.Request, username: str) -> str:
        clean_username = username.strip().lower()
        remote = request.remote or "unknown"
        return f"{remote}:{clean_username}"

    def _login_retry_after(self, key: str) -> int:
        state = self._login_attempts.get(key)
        if state is None:
            return 0
        now = time.time()
        if state.lockout_until > now:
            return max(1, int(state.lockout_until - now))
        window_start = now - 60
        state.failures = [ts for ts in state.failures if ts >= window_start]
        if len(state.failures) >= self._web_settings.login_rate_limit_per_minute:
            state.lockout_until = now + self._web_settings.login_lockout_seconds
            return self._web_settings.login_lockout_seconds
        return 0

    def _record_login_failure(self, key: str) -> int:
        now = time.time()
        window_start = now - 60
        state = self._login_attempts.setdefault(key, _LoginAttemptState(failures=[]))
        state.failures = [ts for ts in state.failures if ts >= window_start]
        state.failures.append(now)
        if (
            len(state.failures) >= self._web_settings.login_lockout_threshold
            or len(state.failures) >= self._web_settings.login_rate_limit_per_minute
        ):
            state.lockout_until = now + self._web_settings.login_lockout_seconds
            return self._web_settings.login_lockout_seconds
        return 0

    def _record_login_success(self, key: str) -> None:
        self._login_attempts.pop(key, None)

    def _login_failure_json(self, retry_after: int = 0) -> web.Response:
        status = 429 if retry_after > 0 else 401
        response = web.json_response({"error": "Неверный логин или пароль"}, status=status)
        if retry_after > 0:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def _login_failure_html(self, retry_after: int = 0) -> web.Response:
        status = 429 if retry_after > 0 else 401
        response = web.Response(
            text=_LOGIN_HTML.replace("{{error}}", "Неверный логин или пароль"),
            content_type="text/html",
            status=status,
        )
        if retry_after > 0:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def _create_ws_ticket(self, user: User) -> tuple[str, int]:
        self._prune_ws_tickets()
        token = secrets.token_urlsafe(24)
        ttl = max(1, int(self._web_settings.ws_ticket_ttl_seconds))
        self._ws_tickets[token] = _WebSocketTicket(
            user_id=user.id,
            expires_at=time.time() + ttl,
        )
        return token, ttl

    def _consume_ws_ticket(self, token: str, user: User) -> bool:
        ticket = self._ws_tickets.pop(token, None)
        if ticket is None:
            return False
        return ticket.user_id == user.id and ticket.expires_at > time.time()

    def _prune_ws_tickets(self) -> int:
        now = time.time()
        expired = [token for token, ticket in self._ws_tickets.items() if ticket.expires_at <= now]
        for token in expired:
            self._ws_tickets.pop(token, None)
        return len(expired)

    @staticmethod
    def _origin_matches_request(request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return bool(parsed.scheme and parsed.netloc and parsed.netloc == request.host)

    def _frontend_ready(self) -> bool:
        return (_FRONTEND_DIST / "index.html").exists()

    def _frontend_response(self) -> web.FileResponse:
        return web.FileResponse(_FRONTEND_DIST / "index.html")

    async def _handle_favicon(self, _request: web.Request) -> web.StreamResponse:
        favicon = _FRONTEND_DIST / "favicon.svg"
        if not favicon.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(favicon)

    async def _handle_login_page(self, request: web.Request) -> web.StreamResponse:
        if isinstance(request.get("user"), User):
            raise self._redirect("/")
        if self._frontend_ready():
            return self._frontend_response()
        return web.Response(text=_LOGIN_HTML.replace("{{error}}", ""), content_type="text/html")

    async def _handle_login(self, request: web.Request) -> web.Response:
        data = await request.post()
        username = str(data.get("username", ""))
        password = str(data.get("password", ""))
        attempt_key = self._login_attempt_key(request, username)
        retry_after = self._login_retry_after(attempt_key)
        if retry_after > 0:
            return self._login_failure_html(retry_after)
        user = self._authenticate(username, password)
        if user is None:
            return self._login_failure_html(self._record_login_failure(attempt_key))
        self._record_login_success(attempt_key)
        token, _csrf = self._create_session_response(user)
        response = self._redirect("/")
        self._set_session_cookie(request, response, token)
        return response

    async def _handle_logout(self, request: web.Request) -> web.Response:
        if self._stack is not None:
            token = request.cookies.get(self._web_settings.cookie_name)
            if token:
                self._stack.user_manager.delete_web_session(token)
        response = self._redirect("/login")
        self._clear_session_cookie(response)
        return response

    async def _handle_session(self, request: web.Request) -> web.Response:
        user = request.get("user")
        if not isinstance(user, User):
            return web.json_response({"authenticated": False, "user": None, "csrf_token": ""})
        return web.json_response(
            {
                "authenticated": True,
                "user": self._user_payload(user),
                "csrf_token": str(request.get("csrf_token", "")),
            }
        )

    async def _handle_api_login(self, request: web.Request) -> web.Response:
        payload = await self._json_payload(request)
        username = payload.get("username")
        password = payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise web.HTTPBadRequest(text="username and password are required")
        attempt_key = self._login_attempt_key(request, username)
        retry_after = self._login_retry_after(attempt_key)
        if retry_after > 0:
            return self._login_failure_json(retry_after)
        user = self._authenticate(username, password)
        if user is None:
            return self._login_failure_json(self._record_login_failure(attempt_key))
        self._record_login_success(attempt_key)
        token, csrf_token = self._create_session_response(user)
        response = web.json_response(
            {
                "authenticated": True,
                "user": self._user_payload(user),
                "csrf_token": csrf_token,
            }
        )
        self._set_session_cookie(request, response, token)
        return response

    async def _handle_api_logout(self, request: web.Request) -> web.Response:
        if self._stack is not None:
            token = request.cookies.get(self._web_settings.cookie_name)
            if token:
                self._stack.user_manager.delete_web_session(token)
        response = web.json_response({"ok": True})
        self._clear_session_cookie(response)
        return response

    async def _handle_ws_ticket(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        ticket, ttl = self._create_ws_ticket(user)
        return web.json_response({"ticket": ticket, "expires_in_seconds": ttl})

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        user = request.get("user")
        if not isinstance(user, User):
            raise self._redirect("/login")
        if not self._frontend_ready():
            return web.Response(text=_BUILD_MISSING_HTML, content_type="text/html", status=503)
        return self._frontend_response()

    def _require_user(self, request: web.Request) -> User:
        user = request.get("user")
        if not isinstance(user, User):
            raise web.HTTPUnauthorized()
        return user

    def _workspace_for(self, user: User) -> Path:
        if self._service is None:
            raise web.HTTPServiceUnavailable()
        return self._service.get_user_workspace(user)

    def _context_usage_payload(self, stats: RunStats | None = None) -> dict[str, object]:
        limit = max(1, int(self._settings.agent.compression.max_context_tokens))
        latest_total = 0
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        if stats is not None:
            latest_total = int(stats.latest_total_tokens or stats.total_tokens or 0)
            input_tokens = int(stats.input_tokens)
            output_tokens = int(stats.output_tokens)
            total_tokens = int(stats.total_tokens)
        ratio = min(1.0, max(0.0, latest_total / limit))
        return {
            "latest_total_tokens": latest_total,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "context_limit_tokens": limit,
            "context_ratio": ratio,
        }

    def _normalize_context_usage_payload(self, usage: dict[str, Any]) -> dict[str, object]:
        limit = max(
            1,
            int(
                usage.get("context_limit_tokens")
                or self._settings.agent.compression.max_context_tokens
            ),
        )
        latest_total = int(usage.get("latest_total_tokens") or usage.get("total_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        ratio = min(1.0, max(0.0, float(usage.get("context_ratio") or latest_total / limit)))
        return {
            "latest_total_tokens": latest_total,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "context_limit_tokens": limit,
            "context_ratio": ratio,
        }

    async def _reset_context_for_user(self, user: User) -> tuple[bool, str, dict[str, object]]:
        if self._service is None:
            raise web.HTTPServiceUnavailable()
        if not await self._service.try_start_user_request(user.id):
            return (
                False,
                "Предыдущая задача ещё выполняется. Дождитесь ответа перед сбросом контекста.",
                self._context_usage.get(user.id, self._context_usage_payload()),
            )
        try:
            await self._service.reset_user_context(user)
            if self._chat_store is not None:
                await self._chat_store.reset_session(user.memory_key(), reason="/new")
            usage = self._context_usage_payload()
            self._context_usage[user.id] = usage
            self._active_request_state.pop(user.id, None)
            logger.info("Web user %s reset session", user.id)
            return True, "Сессия сброшена. Можно начать заново.", usage
        finally:
            await self._service.finish_user_request(user.id)

    async def _handle_list_files(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        result = await list_directory(
            self._workspace_for(user),
            request.query.get("path"),
            sort=request.query.get("sort", "name"),
            order=request.query.get("order", "asc"),
        )
        return web.json_response(result)

    async def _handle_tree_files(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        raw_depth = request.query.get("depth", "3")
        try:
            depth = int(raw_depth)
        except ValueError as e:
            raise web.HTTPBadRequest(text="Invalid depth") from e
        result = await build_tree(self._workspace_for(user), depth=depth)
        return web.json_response(result)

    async def _handle_search_files(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        raw_limit = request.query.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError as e:
            raise web.HTTPBadRequest(text="Invalid limit") from e
        result = await search_files(
            self._workspace_for(user),
            request.query.get("query"),
            limit=limit,
        )
        return web.json_response(result)

    async def _handle_preview_file(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        result = await preview_file(self._workspace_for(user), path)
        if result.get("type") == "image":
            result["url"] = f"/api/files/inline?path={quote(path)}"
        return web.json_response(result)

    async def _handle_upload(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        reader = await request.multipart()
        target_dir = request.query.get("path")
        uploaded: list[dict[str, str]] = []
        while True:
            field: Any = await reader.next()
            if field is None:
                break
            filename = getattr(field, "filename", None)
            if not filename:
                continue
            rel = await save_upload_stream(
                workspace=self._workspace_for(user),
                filename=str(filename),
                field=field,
                max_bytes=self._web_settings.upload_max_bytes,
                target_dir=target_dir,
            )
            uploaded.append({"name": Path(rel).name, "path": rel})
        if not uploaded:
            raise web.HTTPBadRequest(text="Missing file")
        return web.json_response({"uploaded": uploaded, "path": uploaded[0]["path"]})

    async def _handle_mkdir(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        payload = await self._json_payload(request)
        raw_parent = payload.get("path")
        raw_name = payload.get("name")
        if raw_parent is not None and not isinstance(raw_parent, str):
            raise web.HTTPBadRequest(text="path must be a string")
        if not isinstance(raw_name, str):
            raise web.HTTPBadRequest(text="name is required")
        rel = await make_directory(
            self._workspace_for(user),
            raw_parent or "",
            raw_name,
        )
        return web.json_response({"path": rel})

    async def _handle_rename(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        payload = await self._json_payload(request)
        path = payload.get("path")
        new_name = payload.get("new_name")
        if not isinstance(path, str) or not isinstance(new_name, str):
            raise web.HTTPBadRequest(text="path and new_name are required")
        rel = await rename_path(self._workspace_for(user), path, new_name)
        return web.json_response({"path": rel})

    async def _handle_move(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        payload = await self._json_payload(request)
        target_dir = payload.get("target_dir")
        if target_dir is not None and not isinstance(target_dir, str):
            raise web.HTTPBadRequest(text="target_dir must be a string")
        paths = self._paths_from_payload(payload)
        moved = await move_paths(self._workspace_for(user), paths, target_dir)
        return web.json_response({"paths": moved})

    async def _handle_copy(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        payload = await self._json_payload(request)
        target_dir = payload.get("target_dir")
        if target_dir is not None and not isinstance(target_dir, str):
            raise web.HTTPBadRequest(text="target_dir must be a string")
        paths = self._paths_from_payload(payload)
        copied = await copy_paths(self._workspace_for(user), paths, target_dir)
        return web.json_response({"paths": copied})

    async def _handle_delete_batch(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        payload = await self._json_payload(request)
        paths = self._paths_from_payload(payload)
        recursive = payload.get("recursive")
        if not isinstance(recursive, bool):
            raise web.HTTPBadRequest(text="recursive confirmation is required")
        deleted = await delete_paths(self._workspace_for(user), paths, recursive=recursive)
        return web.json_response({"paths": deleted})

    async def _handle_delete(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        await delete_path(self._workspace_for(user), path, recursive=True)
        return web.json_response({"ok": True})

    async def _handle_download_file(self, request: web.Request) -> web.FileResponse:
        user = self._require_user(request)
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        resolved = resolve_workspace_path(self._workspace_for(user), path)
        if not resolved.exists() or not resolved.is_file():
            raise web.HTTPNotFound()
        return _file_response(resolved, disposition="attachment")

    async def _handle_inline_file(self, request: web.Request) -> web.FileResponse:
        user = self._require_user(request)
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        resolved = resolve_workspace_path(self._workspace_for(user), path)
        if not resolved.exists() or not resolved.is_file():
            raise web.HTTPNotFound()
        if resolved.suffix.lower() not in _INLINE_IMAGE_EXTENSIONS:
            raise web.HTTPNotFound()
        return _file_response(resolved, disposition="inline")

    async def _handle_download_grant(self, request: web.Request) -> web.FileResponse:
        user = self._require_user(request)
        self._prune_download_grants()
        token = request.match_info["token"]
        grant = self._download_grants.get(token)
        if grant is None:
            raise web.HTTPNotFound()
        if grant.expires_at <= time.time():
            self._download_grants.pop(token, None)
            raise web.HTTPNotFound()
        if grant.user_id != user.id:
            raise web.HTTPNotFound()
        if not grant.path.exists() or not grant.path.is_file():
            self._download_grants.pop(token, None)
            raise web.HTTPNotFound()
        if not _is_path_inside(grant.path, self._workspace_for(user)):
            raise web.HTTPNotFound()
        return _file_response(grant.path, disposition="attachment", filename=grant.filename)

    async def _send_ws(self, ws: web.WebSocketResponse, payload: dict[str, object]) -> None:
        if not ws.closed:
            await ws.send_str(json.dumps(payload, ensure_ascii=False))

    async def _broadcast_to_user(self, user_id: int, payload: dict[str, object]) -> None:
        sockets = self._clients.get(user_id)
        if not sockets:
            return
        for ws in list(sockets):
            if ws.closed:
                sockets.discard(ws)
                continue
            try:
                await self._send_ws(ws, payload)
            except Exception as e:
                logger.debug("WebSocket send failed; dropping client: %s", e)
                sockets.discard(ws)
        if not sockets:
            self._clients.pop(user_id, None)

    def _create_download_grant(
        self,
        *,
        user: User,
        path: Path,
        filename: str,
        caption: str,
    ) -> str:
        self._prune_download_grants()
        token = secrets.token_urlsafe(18)
        self._download_grants[token] = _DownloadGrant(
            user_id=user.id,
            path=path,
            filename=filename,
            caption=caption,
            expires_at=time.time() + _DOWNLOAD_GRANT_TTL_SECONDS,
        )
        return f"/api/download/{token}"

    async def _chat_message_payload(self, message: WebChatMessage, user: User) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": f"db_{message.id}",
            "db_id": message.id,
            "session_id": message.session_id,
            "role": message.role,
            "text": message.content,
            "created_at": message.created_at,
        }
        if message.tone:
            payload["tone"] = message.tone
        if message.request_id:
            payload["request_id"] = message.request_id
        if message.file is not None:
            file_payload: dict[str, object] = {
                "name": message.file.name,
                "caption": message.file.caption,
                "available": False,
            }
            if message.file.path:
                try:
                    resolved = resolve_workspace_path(self._workspace_for(user), message.file.path)
                    if resolved.exists() and resolved.is_file():
                        file_payload["path"] = message.file.path
                        file_payload["url"] = self._create_download_grant(
                            user=user,
                            path=resolved,
                            filename=message.file.name,
                            caption=message.file.caption,
                        )
                        file_payload["available"] = True
                except Exception as e:
                    logger.debug("Web chat file payload unavailable: %s", e)
            payload["file"] = file_payload
        return payload

    @staticmethod
    def _session_summary_payload(summary: ChatSessionSummary) -> dict[str, object]:
        """Serialize a chat session for the sidebar list (REST + WS)."""
        payload: dict[str, object] = {
            "id": summary.id,
            "section": summary.section,
            "title": summary.title,
            "created_at": summary.created_at,
            "active": summary.is_active,
            "msg_count": summary.msg_count,
        }
        # updated_at drives the sidebar's "recently active" ordering + time-range
        # grouping. Omit it only when genuinely unknown (legacy rows).
        if summary.updated_at is not None:
            payload["updated_at"] = summary.updated_at
        return payload

    def _llm_summary_payload(self) -> dict[str, object]:
        rule = next(
            (item for item in self._settings.llm.routing if item.task_kind == "default"),
            self._settings.llm.routing[0] if self._settings.llm.routing else None,
        )
        if rule is None:
            return {"provider": None, "model": None}
        return {"provider": rule.provider, "model": rule.model}

    async def _recent_outputs_payload(
        self,
        user: User,
        *,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        if self._chat_store is None:
            return []
        messages = await self._chat_store.latest_file_messages(user.memory_key(), limit=limit)
        outputs: list[dict[str, object]] = []
        for message in messages:
            payload = await self._chat_message_payload(message, user)
            raw_file = payload.get("file")
            if not isinstance(raw_file, dict):
                continue
            outputs.append(
                {
                    "name": raw_file.get("name", "file"),
                    "path": raw_file.get("path"),
                    "url": raw_file.get("url"),
                    "caption": raw_file.get("caption", ""),
                    "available": raw_file.get("available", False),
                    "created_at": message.created_at,
                }
            )
        return outputs

    async def _handle_workspace_overview(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        workspace = self._workspace_for(user)
        payload = {
            "user": self._user_payload(user),
            "llm": self._llm_summary_payload(),
            "recent_files": await list_recent_files(workspace, limit=8),
            "recent_outputs": await self._recent_outputs_payload(user, limit=8),
        }
        return web.json_response(payload)

    # ------------------------------------------------------------------
    # Etap 4: Extensions management endpoints (list / reload). Read-only.
    # ------------------------------------------------------------------

    async def _handle_list_extensions(self, request: web.Request) -> web.Response:
        self._require_user(request)
        stack = self._stack
        if stack is None:
            return web.json_response({"skills": [], "subagents": [], "mcp": [], "plugins": []})
        skills = [
            {
                "id": s.id,
                "name": s.id,
                "description": s.description,
                "version": s.version,
                "status": "loaded",
                "always": s.always,
                "keywords": s.keywords,
            }
            for s in (stack.skill_registry.list_all() if stack.skill_registry else [])
        ]
        subagents = [
            {
                "id": spec.id,
                "name": spec.name,
                "description": spec.description,
                "version": None,
                "status": "loaded",
                "capabilities": spec.capabilities,
            }
            for spec in (stack.subagent_registry.list_all() if stack.subagent_registry else [])
        ]
        mcp: list[dict[str, object]] = []
        if stack.mcp_manager is not None:
            tools_map = stack.mcp_manager.get_server_tools()
            status_map = stack.mcp_manager.get_server_status()
            mcp = [
                {
                    "id": name,
                    "name": name,
                    "description": None,
                    "version": None,
                    "status": status_map[name],
                    "tools": tools_map.get(name, []),
                }
                for name in sorted(status_map.keys())
            ]
        plugins = [
            {
                "id": p.manifest.name,
                "name": p.manifest.name,
                "description": p.manifest.description,
                "version": p.manifest.version,
                "status": "loaded",
                "type": p.manifest.type,
            }
            for p in (stack.plugin_registry.list_all() if stack.plugin_registry else [])
        ]
        return web.json_response(
            {"skills": skills, "subagents": subagents, "mcp": mcp, "plugins": plugins}
        )

    async def _handle_reload_extensions(self, request: web.Request) -> web.Response:
        self._require_user(request)
        errors: list[str] = []
        for label, reloader in (
            ("skills", self._skill_reloader),
            ("subagents", self._subagent_reloader),
            ("plugins", self._plugin_reloader),
            ("mcp", self._mcp_reloader),
        ):
            if reloader is None:
                continue
            try:
                await reloader.reload_now()
            except Exception as e:
                logger.error("Extensions reload failed for %s: %s", label, e)
                errors.append(f"{label}: {e}")
        return web.json_response({"ok": len(errors) == 0, "errors": errors})

    # ------------------------------------------------------------------
    # Etap 2: chat history REST endpoints (list / create / activate).
    # rename/delete are Etap 2B. All require auth + CSRF (enforced by middleware).
    # ------------------------------------------------------------------

    async def _handle_list_chats(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        if self._chat_store is None:
            return web.json_response({"chats": []})
        section = request.query.get("section")
        if section not in {"chat", "work", None}:
            section = None
        summaries = await self._chat_store.list_sessions(user.memory_key(), section=section)
        return web.json_response({"chats": [self._session_summary_payload(s) for s in summaries]})

    async def _handle_create_chat(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        if self._chat_store is None or self._service is None:
            raise web.HTTPServiceUnavailable()
        try:
            body = await request.json()
        except Exception:
            body = {}
        section = body.get("section", "chat") if isinstance(body, dict) else "chat"
        if section not in {"chat", "work"}:
            section = "chat"
        # A new chat implies a clean agent context. Block while a run is in-flight.
        if not await self._service.try_start_user_request(user.id):
            return web.json_response(
                {"error": "Дождитесь завершения активного чата перед созданием нового."},
                status=409,
            )
        try:
            await self._service.reset_user_context(user)
            session_id = await self._chat_store.create_session(user.memory_key(), section=section)
            summary = await self._chat_store.get_session(user.memory_key(), session_id)
        finally:
            await self._service.finish_user_request(user.id)
        if summary is None:
            raise web.HTTPInternalServerError(text="Failed to create chat session")
        self._context_usage.pop(user.id, None)
        await self._broadcast_to_user(user.id, {"type": "chat_list_changed"})
        return web.json_response({"chat": self._session_summary_payload(summary)})

    async def _handle_activate_chat(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        if self._chat_store is None or self._service is None:
            raise web.HTTPServiceUnavailable()
        try:
            session_id = int(request.match_info["id"])
        except (KeyError, ValueError, TypeError) as e:
            raise web.HTTPBadRequest(text="Invalid chat id.") from e
        summary = await self._chat_store.get_session(user.memory_key(), session_id)
        if summary is None:
            raise web.HTTPNotFound(text="Chat not found.")
        if summary.is_active:
            # Already active — nothing to switch, just confirm current state.
            return web.json_response(
                {"chat": self._session_summary_payload(summary), "activated": False}
            )
        # Switching the active chat resets agent context (memory is single-thread
        # per user). Block while a run is in-flight.
        if not await self._service.try_start_user_request(user.id):
            return web.json_response(
                {"error": "Дождитесь завершения активного чата перед переключением."},
                status=409,
            )
        try:
            await self._service.reset_user_context(user)
            new_id = await self._chat_store.activate_session(user.memory_key(), session_id)
        finally:
            await self._service.finish_user_request(user.id)
        if new_id is None:
            raise web.HTTPNotFound(text="Chat not found.")
        activated = await self._chat_store.get_session(user.memory_key(), new_id)
        if activated is None:
            raise web.HTTPInternalServerError(text="Failed to reload chat session")
        self._context_usage.pop(user.id, None)
        # mode is now derived from the activated chat's section (Etap 2):
        # Work → execute (tools on), Chat → chat (tools off).
        mode = "execute" if activated.section == "work" else "chat"
        await self._broadcast_to_user(
            user.id,
            {
                "type": "chat_activated",
                "session_id": new_id,
                "section": activated.section,
                "mode": mode,
            },
        )
        await self._broadcast_to_user(user.id, {"type": "chat_list_changed"})
        return web.json_response(
            {"chat": self._session_summary_payload(activated), "activated": True, "mode": mode}
        )

    # ------------------------------------------------------------------
    # Etap 2B: chat management endpoints (rename / delete).
    # ------------------------------------------------------------------

    async def _handle_update_chat(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        if self._chat_store is None:
            raise web.HTTPServiceUnavailable()
        try:
            session_id = int(request.match_info["id"])
        except (KeyError, ValueError, TypeError) as e:
            raise web.HTTPBadRequest(text="Invalid chat id.") from e
        try:
            body = await request.json()
        except Exception:
            body = {}
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str):
            raise web.HTTPBadRequest(text="Поле title обязательно.")
        title = title.strip()[:200]
        if not title:
            raise web.HTTPBadRequest(text="Поле title не может быть пустым.")
        ok = await self._chat_store.rename_session(user.memory_key(), session_id, title)
        if not ok:
            raise web.HTTPNotFound(text="Chat not found.")
        await self._broadcast_to_user(
            user.id,
            {"type": "chat_renamed", "session_id": session_id, "title": title},
        )
        return web.json_response({"ok": True, "session_id": session_id, "title": title})

    async def _handle_delete_chat(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        if self._chat_store is None or self._service is None:
            raise web.HTTPServiceUnavailable()
        try:
            session_id = int(request.match_info["id"])
        except (KeyError, ValueError, TypeError) as e:
            raise web.HTTPBadRequest(text="Invalid chat id.") from e
        # Take the single-in-flight lock for consistency with create/activate —
        # all chat mutations that can interact with an active run serialize here.
        if not await self._service.try_start_user_request(user.id):
            return web.json_response(
                {"error": "Дождитесь завершения активного чата перед удалением."}, status=409
            )
        try:
            summary = await self._chat_store.get_session(user.memory_key(), session_id)
            if summary is None:
                raise web.HTTPNotFound(text="Chat not found.")
            if summary.is_active:
                return web.json_response({"error": "Нельзя удалить активный чат."}, status=409)
            ok = await self._chat_store.delete_session(user.memory_key(), session_id)
            if not ok:
                raise web.HTTPNotFound(text="Chat not found.")
        finally:
            await self._service.finish_user_request(user.id)
        await self._broadcast_to_user(user.id, {"type": "chat_list_changed"})
        return web.json_response({"ok": True, "session_id": session_id})

    async def _chat_messages_payload(
        self, messages: list[WebChatMessage], user: User
    ) -> list[dict[str, object]]:
        return [await self._chat_message_payload(message, user) for message in messages]

    async def _context_usage_for_user(self, user: User) -> dict[str, object]:
        cached = self._context_usage.get(user.id)
        if cached is not None:
            return cached
        if self._chat_store is not None:
            try:
                latest = await self._chat_store.latest_usage(user.memory_key())
                if latest is not None:
                    usage = self._normalize_context_usage_payload(latest)
                    self._context_usage[user.id] = usage
                    return usage
            except Exception as e:
                logger.warning("Failed to restore web context usage for user %s: %s", user.id, e)
        return self._context_usage_payload()

    async def _handle_chat_ws(self, request: web.Request) -> web.WebSocketResponse:
        user = self._require_user(request)
        if not self._origin_matches_request(request):
            raise web.HTTPForbidden(text="Origin validation failed")
        ticket = request.query.get("ticket", "")
        if not ticket or not self._consume_ws_ticket(ticket, user):
            raise web.HTTPForbidden(text="WebSocket ticket validation failed")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients.setdefault(user.id, set()).add(ws)
        mode = "execute"
        # Etap 3: depth mode (Fast/Think) is a per-connection UI state, separate
        # from `mode` (tools on/off, derived from chat section). Default = think.
        depth_mode: str = "think"

        async def send(payload: dict[str, object]) -> None:
            await self._send_ws(ws, payload)

        def broadcast_task(payload: dict[str, object]) -> None:
            asyncio.create_task(self._broadcast_to_user(user.id, payload))

        if self._chat_store is not None:
            try:
                await self._chat_store.backfill_from_memory(user.memory_key(), limit=100)
                page = await self._chat_store.list_recent(user.memory_key(), limit=100)
                await send(
                    {
                        "type": "chat_history",
                        "session_id": page.session_id,
                        "messages": await self._chat_messages_payload(page.messages, user),
                        "has_more": page.has_more,
                    }
                )
            except Exception as e:
                logger.warning("Failed to load web chat history for user %s: %s", user.id, e)

        await send({"type": "llm_status", "status": "unknown"})
        await send(
            {
                "type": "context_usage",
                "usage": await self._context_usage_for_user(user),
            }
        )
        active_state = self._active_request_state.get(user.id)
        if active_state is not None:
            await send({"type": "request_state", **active_state})
        for approval in self._pending_approvals.values():
            if approval.user_id == user.id and not approval.future.done():
                await send(
                    {
                        "type": "approval_required",
                        "approval_id": approval.approval_id,
                        "action": approval.action,
                        "details": approval.details,
                    }
                )

        def send_status(
            *,
            request_id: str,
            phase: str,
            key: str,
            label: str,
        ) -> None:
            self._active_request_state[user.id] = {
                "request_id": request_id,
                "phase": phase,
                "key": key,
                "label": label,
            }
            broadcast_task(
                {
                    "type": "status_update",
                    "request_id": request_id,
                    "phase": phase,
                    "key": key,
                    "label": label,
                }
            )

        def send_llm_status(*, request_id: str, stage: str) -> None:
            label = format_llm_stage_status(stage)
            if label is None:
                return
            send_status(request_id=request_id, phase="llm", key=stage, label=label)

        def send_llm_queue_status(*, request_id: str, status: object) -> None:
            from corpclaw_lite.llm.queue import LLMQueueStatus

            if not isinstance(status, LLMQueueStatus):
                return
            send_status(
                request_id=request_id,
                phase="queue",
                key="llm_slot",
                label=format_llm_queue_status(status),
            )

        def send_subagent_llm_status(*, request_id: str, subagent_name: str, stage: str) -> None:
            label = format_subagent_llm_stage_status(subagent_name, stage)
            if label is None:
                return
            send_status(
                request_id=request_id,
                phase="subagent",
                key=f"{subagent_name}:{stage}",
                label=label,
            )

        def send_subagent_llm_queue_status(
            *,
            request_id: str,
            subagent_name: str,
            status: object,
        ) -> None:
            from corpclaw_lite.llm.queue import LLMQueueStatus

            if not isinstance(status, LLMQueueStatus):
                return
            send_status(
                request_id=request_id,
                phase="queue",
                key=f"{subagent_name}:llm_slot",
                label=format_subagent_llm_queue_status(subagent_name, status),
            )

        async def approval_cb(action: str, details: str) -> bool:
            approval_id = secrets.token_urlsafe(12)
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._pending_approvals[approval_id] = _PendingApproval(
                user_id=user.id,
                approval_id=approval_id,
                action=action,
                details=details,
                future=future,
                created_at=time.time(),
            )
            await self._broadcast_to_user(
                user.id,
                {
                    "type": "approval_required",
                    "approval_id": approval_id,
                    "action": action,
                    "details": details,
                },
            )
            try:
                return await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                return False
            finally:
                self._pending_approvals.pop(approval_id, None)

        async def persist_and_broadcast(
            *,
            role: str,
            content: str,
            request_id: str | None = None,
            tone: str | None = None,
            metadata: dict[str, Any] | None = None,
            file: WebChatFile | None = None,
        ) -> WebChatMessage | None:
            if self._chat_store is None:
                return None
            try:
                message = await self._chat_store.append_message(
                    user_id=user.memory_key(),
                    role=role,
                    content=content,
                    tone=tone,
                    request_id=request_id,
                    metadata=metadata,
                    file=file,
                )
                await self._broadcast_to_user(
                    user.id,
                    {
                        "type": "chat_message",
                        "message": await self._chat_message_payload(message, user),
                    },
                )
                return message
            except Exception as e:
                logger.warning("Failed to persist web chat message for user %s: %s", user.id, e)
                return None

        async def run_message(text: str) -> None:
            request_id = secrets.token_urlsafe(10)
            request_started = False
            request_acquired = False
            if self._service is None:
                await send({"type": "error", "message": "Сервис ещё не готов."})
                return
            if not await self._rate_limiter.check(user.id):
                await send({"type": "error", "message": "Слишком много сообщений."})
                return
            if not await self._service.try_start_user_request(user.id):
                await send({"type": "error", "message": "Предыдущая задача ещё выполняется."})
                return
            request_acquired = True
            try:
                user_message = await persist_and_broadcast(
                    role="user", content=text, request_id=request_id
                )
                # Etap 2: auto-name the chat from the first user message if untitled.
                # Truncation only (no LLM) — cheap and deterministic for local models.
                if user_message is not None and self._chat_store is not None:
                    title = _derive_chat_title(text)
                    if title is not None:
                        updated = await self._chat_store.set_session_title(
                            user.memory_key(), user_message.session_id, title
                        )
                        if updated:
                            await self._broadcast_to_user(
                                user.id,
                                {
                                    "type": "chat_renamed",
                                    "session_id": user_message.session_id,
                                    "title": title,
                                },
                            )
                            broadcast_task({"type": "chat_list_changed"})
                request_started = True
                self._active_request_state[user.id] = {
                    "request_id": request_id,
                    "phase": "request",
                    "label": INITIAL_STATUS_TEXT,
                }
                await self._broadcast_to_user(
                    user.id,
                    {
                        "type": "request_started",
                        "request_id": request_id,
                        "label": INITIAL_STATUS_TEXT,
                    },
                )
                # Etap 2: mode is now derived from the active chat's section,
                # not the legacy WS mode_change toggle. Work → execute (tools on),
                # Chat → chat (tools off). Falls back to the WS-local mode if the
                # session lookup fails (defensive — shouldn't happen).
                effective_mode = mode
                if user_message is not None and self._chat_store is not None:
                    active_session = await self._chat_store.get_session(
                        user.memory_key(), user_message.session_id
                    )
                    if active_session is not None:
                        effective_mode = "execute" if active_session.section == "work" else "chat"
                result = await self._service.run(
                    user=user,
                    message=text,
                    mode=effective_mode,
                    depth_mode=depth_mode,
                    channel="web",
                    callbacks=AgentRequestCallbacks(
                        request_approval=approval_cb,
                        on_tool_start=lambda tool: send_status(
                            request_id=request_id,
                            phase="tool",
                            key=tool,
                            label=format_tool_status(tool),
                        ),
                        on_tool_batch_start=lambda tools: send_status(
                            request_id=request_id,
                            phase="tool",
                            key="parallel_tools",
                            label=format_tool_batch_status(tools),
                        ),
                        on_llm_stage=lambda stage: send_llm_status(
                            request_id=request_id,
                            stage=stage,
                        ),
                        on_llm_queue_status=lambda status: send_llm_queue_status(
                            request_id=request_id,
                            status=status,
                        ),
                        on_subagent_tool_start=lambda subagent, tool: send_status(
                            request_id=request_id,
                            phase="subagent",
                            key=f"{subagent}:{tool}",
                            label=format_subagent_tool_status(subagent, tool),
                        ),
                        on_subagent_tool_batch_start=lambda subagent, tools: send_status(
                            request_id=request_id,
                            phase="subagent",
                            key=f"{subagent}:parallel_tools",
                            label=format_subagent_tool_batch_status(subagent, tools),
                        ),
                        on_subagent_llm_stage=lambda subagent, stage: send_subagent_llm_status(
                            request_id=request_id,
                            subagent_name=subagent,
                            stage=stage,
                        ),
                        on_subagent_llm_queue_status=(
                            lambda subagent, status: send_subagent_llm_queue_status(
                                request_id=request_id,
                                subagent_name=subagent,
                                status=status,
                            )
                        ),
                    ),
                )
                usage = self._context_usage_payload(result.stats)
                self._context_usage[user.id] = usage
                await persist_and_broadcast(
                    role="assistant",
                    content=result.reply,
                    request_id=request_id,
                    metadata={
                        "usage": usage,
                        "status": result.stats.status,
                        "tools_used": result.stats.tools_used,
                    },
                )
                await self._broadcast_to_user(
                    user.id,
                    {
                        "type": "request_finished",
                        "request_id": request_id,
                        "status": "ok",
                        "label": READY_STATUS_TEXT,
                        "usage": usage,
                    },
                )
            except LLMBackendUnavailableError as e:
                warning = e.user_message()
                await persist_and_broadcast(
                    role="system",
                    content=warning,
                    request_id=request_id,
                    tone="warning",
                    metadata={"kind": "llm_unavailable"},
                )
                await self._broadcast_to_user(
                    user.id,
                    {
                        "type": "warning",
                        "request_id": request_id,
                        "kind": "llm_unavailable",
                        "llm_status": "unavailable",
                        "message": warning,
                    },
                )
                if request_started:
                    await self._broadcast_to_user(
                        user.id,
                        {
                            "type": "request_finished",
                            "request_id": request_id,
                            "status": "warning",
                            "label": "⚠️ Требуется внимание",
                            "usage": self._context_usage.get(
                                user.id, self._context_usage_payload()
                            ),
                        },
                    )
            except Exception:
                logger.exception(
                    "Web agent request failed for user %s request_id=%s",
                    user.id,
                    request_id,
                )
                error_message = f"Задача завершилась ошибкой. ID: {request_id}"
                await persist_and_broadcast(
                    role="system",
                    content=error_message,
                    request_id=request_id,
                    tone="error",
                )
                await self._broadcast_to_user(
                    user.id,
                    {"type": "error", "request_id": request_id, "message": error_message},
                )
                if request_started:
                    await self._broadcast_to_user(
                        user.id,
                        {
                            "type": "request_finished",
                            "request_id": request_id,
                            "status": "error",
                            "label": "Ошибка выполнения",
                            "usage": self._context_usage.get(
                                user.id, self._context_usage_payload()
                            ),
                        },
                    )
            finally:
                self._active_request_state.pop(user.id, None)
                if request_acquired:
                    await self._service.finish_user_request(user.id)

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send({"type": "error", "message": "Некорректный формат сообщения."})
                    continue
                if not isinstance(payload, dict):
                    await send({"type": "error", "message": "Некорректный формат сообщения."})
                    continue
                event_type = payload.get("type")
                if event_type == "mode_change":
                    requested = payload.get("mode")
                    if requested in {"chat", "execute"}:
                        mode = str(requested)
                        await send({"type": "mode", "mode": mode})
                elif event_type == "depth_mode_change":
                    # Etap 3: Fast/Think/Research depth selector. Validated
                    # against the known set; unknown values are ignored.
                    requested_depth = payload.get("depth_mode")
                    if requested_depth in {"fast", "think", "research"}:
                        depth_mode = str(requested_depth)
                        await send({"type": "depth_mode", "depth_mode": depth_mode})
                elif event_type == "load_chat":
                    # Read-only load of a specific chat's transcript. Does NOT
                    # activate the chat or touch agent context — the client uses
                    # POST /api/chats/{id}/activate to switch the active chat.
                    if self._chat_store is None:
                        await send({"type": "error", "message": "История чата недоступна."})
                        continue
                    try:
                        target_session = int(payload.get("session_id") or 0)
                    except (TypeError, ValueError):
                        await send({"type": "error", "message": "Некорректный чат."})
                        continue
                    if target_session <= 0:
                        await send({"type": "error", "message": "Некорректный чат."})
                        continue
                    page = await self._chat_store.list_messages(
                        user.memory_key(), session_id=target_session, limit=100
                    )
                    await send(
                        {
                            "type": "chat_history",
                            "session_id": target_session,
                            "messages": await self._chat_messages_payload(page.messages, user),
                            "has_more": page.has_more,
                            "read_only": True,
                        }
                    )
                elif event_type == "load_history_before":
                    if self._chat_store is None:
                        await send({"type": "error", "message": "История чата недоступна."})
                        continue
                    try:
                        before_id = int(payload.get("before_id") or 0)
                        limit = int(payload.get("limit") or 100)
                    except (TypeError, ValueError):
                        await send({"type": "error", "message": "Некорректный указатель истории."})
                        continue
                    if before_id <= 0:
                        await send({"type": "error", "message": "Некорректный указатель истории."})
                        continue
                    page = await self._chat_store.list_before(
                        user.memory_key(), before_id=before_id, limit=limit
                    )
                    await send(
                        {
                            "type": "history_page",
                            "session_id": page.session_id,
                            "messages": await self._chat_messages_payload(page.messages, user),
                            "has_more": page.has_more,
                        }
                    )
                elif event_type == "reset_context":
                    ok, message, usage = await self._reset_context_for_user(user)
                    payload_out: dict[str, object] = {
                        "type": "context_reset" if ok else "error",
                        "message": message,
                        "usage": usage,
                    }
                    if ok:
                        await self._broadcast_to_user(user.id, payload_out)
                    else:
                        await send(payload_out)
                elif event_type == "approve" or event_type == "deny":
                    approval_id = str(payload.get("approval_id", ""))
                    approval = self._pending_approvals.get(approval_id)
                    if (
                        approval is not None
                        and approval.user_id == user.id
                        and not approval.future.done()
                    ):
                        approval.future.set_result(event_type == "approve")
                        await self._broadcast_to_user(
                            user.id,
                            {"type": "approval_resolved", "approval_id": approval_id},
                        )
                elif event_type == "message":
                    text = str(payload.get("message", "")).strip()
                    if len(text) > _MAX_WS_MESSAGE_CHARS:
                        await send({"type": "error", "message": "Сообщение слишком длинное."})
                        continue
                    if text:
                        if text.lower() == "/new":
                            ok, message, usage = await self._reset_context_for_user(user)
                            payload_out: dict[str, object] = {
                                "type": "context_reset" if ok else "error",
                                "message": message,
                                "usage": usage,
                            }
                            if ok:
                                await self._broadcast_to_user(user.id, payload_out)
                            else:
                                await send(payload_out)
                            continue
                        task = asyncio.create_task(run_message(text))
                        self._request_tasks.add(task)
                        task.add_done_callback(self._request_tasks.discard)
        finally:
            sockets = self._clients.get(user.id)
            if sockets is not None:
                sockets.discard(ws)
                if not sockets:
                    self._clients.pop(user.id, None)
        return ws

    async def _send_file_callback(self, path: Path, user: User, caption: str) -> str:
        rel_path: str | None = None
        try:
            workspace = self._workspace_for(user)
            if _is_path_inside(path, workspace):
                rel_path = path.resolve().relative_to(workspace.resolve()).as_posix()
        except Exception as e:
            logger.debug("Web send_file path is not in workspace: %s", e)

        if self._chat_store is not None:
            try:
                message = await self._chat_store.append_message(
                    user_id=user.memory_key(),
                    role="system",
                    content="Файл готов к скачиванию.",
                    tone="file",
                    file=WebChatFile(name=path.name, path=rel_path, caption=caption),
                )
                await self._broadcast_to_user(
                    user.id,
                    {
                        "type": "chat_message",
                        "message": await self._chat_message_payload(message, user),
                    },
                )
                return f"File '{path.name}' is ready for download."
            except Exception as e:
                logger.warning("Failed to persist web file message for user %s: %s", user.id, e)

        url = self._create_download_grant(user=user, path=path, filename=path.name, caption=caption)
        await self._broadcast_to_user(
            user.id,
            {
                "type": "file_ready",
                "name": path.name,
                "caption": caption,
                "url": url,
                "path": rel_path,
            },
        )
        return f"File '{path.name}' is ready for download."

    def _prune_download_grants(self) -> int:
        now = time.time()
        expired = [
            token for token, grant in self._download_grants.items() if grant.expires_at <= now
        ]
        for token in expired:
            self._download_grants.pop(token, None)
        return len(expired)

    async def _container_prune_loop(self) -> None:
        """Periodic idle-container pruning (server-mode reaper).

        prune_idle was previously CLI-only, so containers accumulated for the
        lifetime of a web process. Each pass is wrapped in try/except so a
        transient Docker hiccup never kills the reaper.
        """
        if self._stack is None or self._stack.container_manager is None:
            return
        cm = self._stack.container_manager
        while True:
            await asyncio.sleep(_CONTAINER_PRUNE_INTERVAL_SECONDS)
            try:
                removed = await cm.prune_idle()
                if removed:
                    logger.info("Pruned %d idle container(s)", removed)
            except Exception as exc:
                logger.warning("Container prune pass failed: %s", exc)

    async def _session_cleanup_loop(self) -> None:
        if self._stack is None:
            return
        while True:
            await asyncio.sleep(3600)
            removed = self._stack.user_manager.prune_expired_web_sessions()
            grants_removed = self._prune_download_grants()
            tickets_removed = self._prune_ws_tickets()
            chat_removed = 0
            if self._chat_store is not None:
                try:
                    chat_removed = await self._chat_store.prune_retention(
                        archived_session_ttl_days=self._web_settings.chat_archived_session_ttl_days,
                        max_archived_sessions_per_user=(
                            self._web_settings.chat_max_archived_sessions_per_user
                        ),
                    )
                except Exception as e:
                    logger.warning("Failed to prune web chat transcripts: %s", e)
            if removed:
                logger.info("Pruned %d expired web sessions", removed)
            if grants_removed:
                logger.info("Pruned %d expired web download grants", grants_removed)
            if tickets_removed:
                logger.info("Pruned %d expired web socket tickets", tickets_removed)
            if chat_removed:
                logger.info("Pruned %d archived web chat session(s)", chat_removed)


def _default_llm_endpoint(settings: Settings) -> tuple[str | None, str | None]:
    """Return default provider name and configured base URL without exposing secrets."""
    provider_name = None
    for rule in settings.llm.routing:
        if rule.task_kind == "default":
            provider_name = rule.provider
            break
    if provider_name is None and settings.llm.routing:
        provider_name = settings.llm.routing[0].provider
    if provider_name is None:
        return None, None
    env_name = f"PROVIDER_{provider_name.upper()}__BASE_URL"
    return provider_name, os.environ.get(env_name)


def _safe_ascii_filename(filename: str) -> str:
    source = (Path(filename).name or "file").replace("/", "_").replace("\\", "_")
    path = Path(source)
    stem = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\", ";"} else "_"
        for char in path.stem
    ).strip(" ._")
    suffix = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\", ";"} else "_"
        for char in path.suffix
    ).strip(" ")
    return f"{stem or 'file'}{suffix}"


def _content_disposition(disposition: str, filename: str) -> str:
    safe_name = (Path(filename).name or "file").replace("/", "_").replace("\\", "_")
    fallback = _safe_ascii_filename(safe_name)
    header = content_disposition_header(disposition, filename=fallback)
    if safe_name != fallback:
        header = f"{header}; filename*=UTF-8''{quote(safe_name, safe='')}"
    return header


def _is_path_inside(path: Path, workspace: Path) -> bool:
    resolved = path.resolve()
    ws = workspace.resolve()
    return resolved == ws or ws in resolved.parents


def _file_response(
    path: Path,
    *,
    disposition: str,
    filename: str | None = None,
) -> web.FileResponse:
    return web.FileResponse(
        path,
        headers={
            hdrs.CONTENT_DISPOSITION: _content_disposition(disposition, filename or path.name),
            "X-Content-Type-Options": "nosniff",
        },
    )


_LOGIN_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>CorpClaw Lite Login</title>
<style>body{font-family:system-ui;margin:0;display:grid;place-items:center;min-height:100vh;background:#f5f5f2;color:#1f2933}form{width:320px;background:white;border:1px solid #ddd;padding:24px}input,button{width:100%;box-sizing:border-box;margin-top:10px;padding:10px}button{background:#1f6feb;color:white;border:0}.error{color:#b42318}</style>
</head><body><form method="post" action="/login"><h1>CorpClaw Lite</h1><p class="error">{{error}}</p><input name="username" placeholder="Username" autocomplete="username"><input name="password" type="password" placeholder="Password" autocomplete="current-password"><button>Войти</button></form></body></html>"""

_BUILD_MISSING_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>CorpClaw Lite</title>
<style>body{margin:0;font-family:system-ui;background:#f6f7f8;color:#17202a;display:grid;place-items:center;min-height:100vh}.box{max-width:560px;background:#fff;border:1px solid #d8dee4;padding:28px;box-shadow:0 18px 50px rgba(27,31,36,.08)}code{background:#f0f3f6;padding:2px 5px;border-radius:4px}</style>
</head><body><main class="box"><h1>Web UI не собран</h1><p>Backend web-канала запущен, но React/Vite сборка не найдена в <code>frontend/web/dist</code>.</p><p>Соберите интерфейс командой <code>cd frontend/web && npm ci && npm run build</code>, затем перезапустите <code>uv run corpclaw-lite web</code>.</p></main></body></html>"""
