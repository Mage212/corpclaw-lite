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
from urllib.parse import quote

from aiohttp import WSMsgType, hdrs, web
from aiohttp.helpers import content_disposition_header

from corpclaw_lite.agent.factory import build_agent_stack
from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.channels.service import AgentRequestCallbacks, AgentRequestService
from corpclaw_lite.channels.status import (
    INITIAL_STATUS_TEXT,
    READY_STATUS_TEXT,
    format_llm_stage_status,
    format_tool_status,
)
from corpclaw_lite.channels.telegram.rate_limit import RateLimiter
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
    save_upload_stream,
    search_files,
)
from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.config.settings import Settings, WebChannelSettings
from corpclaw_lite.exceptions import LLMBackendUnavailableError
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
_INLINE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_FRONTEND_DIST = PROJECT_ROOT / "frontend" / "web" / "dist"


@dataclass(slots=True)
class _DownloadGrant:
    user_id: int
    path: Path
    filename: str
    caption: str
    expires_at: float


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
        self._clients: dict[int, web.WebSocketResponse] = {}
        self._download_grants: dict[str, _DownloadGrant] = {}
        self._context_usage: dict[int, dict[str, object]] = {}
        self._cleanup_task: asyncio.Task[None] | None = None
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

        app = self._build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._web_settings.host, self._web_settings.port)
        await site.start()
        install_signal_handlers(self._shutdown_event)
        self._cleanup_task = asyncio.create_task(self._session_cleanup_loop())
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
        for ws in list(self._clients.values()):
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
        app.router.add_post("/api/login", self._handle_api_login)
        app.router.add_post("/api/logout", self._handle_api_logout)
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
            raise web.HTTPBadRequest(text="Invalid JSON payload") from e
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON object expected")
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

    def _set_session_cookie(self, response: web.StreamResponse, token: str) -> None:
        response.set_cookie(
            self._web_settings.cookie_name,
            token,
            httponly=True,
            samesite="Strict",
            max_age=self._web_settings.session_ttl_hours * 3600,
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
        user = self._authenticate(username, password)
        if user is None:
            return web.Response(
                text=_LOGIN_HTML.replace("{{error}}", "Неверный логин или пароль"),
                content_type="text/html",
                status=401,
            )
        token, _csrf = self._create_session_response(user)
        response = self._redirect("/")
        self._set_session_cookie(response, token)
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
        user = self._authenticate(username, password)
        if user is None:
            return web.json_response({"error": "Неверный логин или пароль"}, status=401)
        token, csrf_token = self._create_session_response(user)
        response = web.json_response(
            {
                "authenticated": True,
                "user": self._user_payload(user),
                "csrf_token": csrf_token,
            }
        )
        self._set_session_cookie(response, token)
        return response

    async def _handle_api_logout(self, request: web.Request) -> web.Response:
        if self._stack is not None:
            token = request.cookies.get(self._web_settings.cookie_name)
            if token:
                self._stack.user_manager.delete_web_session(token)
        response = web.json_response({"ok": True})
        self._clear_session_cookie(response)
        return response

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
            usage = self._context_usage_payload()
            self._context_usage[user.id] = usage
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

    async def _handle_chat_ws(self, request: web.Request) -> web.WebSocketResponse:
        user = self._require_user(request)
        csrf_token = request.query.get("csrf", "")
        if not csrf_token or csrf_token != request.get("csrf_token"):
            raise web.HTTPForbidden(text="CSRF validation failed")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._clients[user.id] = ws
        mode = "execute"
        approvals: dict[str, asyncio.Future[bool]] = {}

        async def send(payload: dict[str, object]) -> None:
            if not ws.closed:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))

        def send_task(payload: dict[str, object]) -> None:
            asyncio.create_task(send(payload))

        await send({"type": "llm_status", "status": "unknown"})
        await send(
            {
                "type": "context_usage",
                "usage": self._context_usage.get(user.id, self._context_usage_payload()),
            }
        )

        def send_status(
            *,
            request_id: str,
            phase: str,
            key: str,
            label: str,
        ) -> None:
            send_task(
                {
                    "type": "status_update",
                    "request_id": request_id,
                    "phase": phase,
                    "key": key,
                    "label": label,
                }
            )

        async def approval_cb(action: str, details: str) -> bool:
            approval_id = secrets.token_urlsafe(12)
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            approvals[approval_id] = future
            await send(
                {
                    "type": "approval_required",
                    "approval_id": approval_id,
                    "action": action,
                    "details": details,
                }
            )
            try:
                return await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                return False
            finally:
                approvals.pop(approval_id, None)

        async def run_message(text: str) -> None:
            request_id = secrets.token_urlsafe(10)
            request_started = False
            request_acquired = False
            if self._service is None:
                await send({"type": "error", "message": "Service is not ready"})
                return
            if not await self._rate_limiter.check(user.id):
                await send({"type": "error", "message": "Слишком много сообщений."})
                return
            if not await self._service.try_start_user_request(user.id):
                await send({"type": "error", "message": "Предыдущая задача ещё выполняется."})
                return
            request_acquired = True
            try:
                request_started = True
                await send(
                    {
                        "type": "request_started",
                        "request_id": request_id,
                        "label": INITIAL_STATUS_TEXT,
                    }
                )
                result = await self._service.run(
                    user=user,
                    message=text,
                    mode=mode,
                    channel="web",
                    callbacks=AgentRequestCallbacks(
                        request_approval=approval_cb,
                        on_tool_start=lambda tool: send_status(
                            request_id=request_id,
                            phase="tool",
                            key=tool,
                            label=format_tool_status(tool),
                        ),
                        on_llm_stage=lambda stage: send_status(
                            request_id=request_id,
                            phase="llm",
                            key=stage,
                            label=format_llm_stage_status(stage) or INITIAL_STATUS_TEXT,
                        ),
                    ),
                )
                await send(
                    {
                        "type": "assistant_message",
                        "request_id": request_id,
                        "message": result.reply,
                    }
                )
                await send(
                    {
                        "type": "request_finished",
                        "request_id": request_id,
                        "status": "ok",
                        "label": READY_STATUS_TEXT,
                        "usage": self._context_usage_payload(result.stats),
                    }
                )
                self._context_usage[user.id] = self._context_usage_payload(result.stats)
            except LLMBackendUnavailableError as e:
                await send(
                    {
                        "type": "warning",
                        "request_id": request_id,
                        "kind": "llm_unavailable",
                        "llm_status": "unavailable",
                        "message": e.user_message(),
                    }
                )
                if request_started:
                    await send(
                        {
                            "type": "request_finished",
                            "request_id": request_id,
                            "status": "warning",
                            "label": "⚠️ Требуется внимание",
                            "usage": self._context_usage.get(
                                user.id, self._context_usage_payload()
                            ),
                        }
                    )
            except Exception as e:
                logger.exception("Web agent request failed for user %s", user.id)
                await send({"type": "error", "request_id": request_id, "message": f"Ошибка: {e}"})
                if request_started:
                    await send(
                        {
                            "type": "request_finished",
                            "request_id": request_id,
                            "status": "error",
                            "label": "Ошибка выполнения",
                            "usage": self._context_usage.get(
                                user.id, self._context_usage_payload()
                            ),
                        }
                    )
            finally:
                if request_acquired:
                    await self._service.finish_user_request(user.id)

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send({"type": "error", "message": "Invalid JSON"})
                    continue
                if not isinstance(payload, dict):
                    await send({"type": "error", "message": "Invalid JSON"})
                    continue
                event_type = payload.get("type")
                if event_type == "mode_change":
                    requested = payload.get("mode")
                    if requested in {"chat", "execute"}:
                        mode = str(requested)
                        await send({"type": "mode", "mode": mode})
                elif event_type == "reset_context":
                    ok, message, usage = await self._reset_context_for_user(user)
                    await send(
                        {
                            "type": "context_reset" if ok else "error",
                            "message": message,
                            "usage": usage,
                        }
                    )
                elif event_type == "approve" or event_type == "deny":
                    approval_id = str(payload.get("approval_id", ""))
                    future = approvals.get(approval_id)
                    if future is not None and not future.done():
                        future.set_result(event_type == "approve")
                elif event_type == "message":
                    text = str(payload.get("message", "")).strip()
                    if len(text) > _MAX_WS_MESSAGE_CHARS:
                        await send({"type": "error", "message": "Сообщение слишком длинное."})
                        continue
                    if text:
                        if text.lower() == "/new":
                            ok, message, usage = await self._reset_context_for_user(user)
                            await send(
                                {
                                    "type": "context_reset" if ok else "error",
                                    "message": message,
                                    "usage": usage,
                                }
                            )
                            continue
                        task = asyncio.create_task(run_message(text))
                        self._request_tasks.add(task)
                        task.add_done_callback(self._request_tasks.discard)
        finally:
            self._clients.pop(user.id, None)
            for future in approvals.values():
                if not future.done():
                    future.set_result(False)
        return ws

    async def _send_file_callback(self, path: Path, user: User, caption: str) -> str:
        self._prune_download_grants()
        token = secrets.token_urlsafe(18)
        self._download_grants[token] = _DownloadGrant(
            user_id=user.id,
            path=path,
            filename=path.name,
            caption=caption,
            expires_at=time.time() + _DOWNLOAD_GRANT_TTL_SECONDS,
        )
        ws = self._clients.get(user.id)
        if ws is not None and not ws.closed:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "file_ready",
                        "name": path.name,
                        "caption": caption,
                        "url": f"/api/download/{token}",
                    },
                    ensure_ascii=False,
                )
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

    async def _session_cleanup_loop(self) -> None:
        if self._stack is None:
            return
        while True:
            await asyncio.sleep(3600)
            removed = self._stack.user_manager.prune_expired_web_sessions()
            grants_removed = self._prune_download_grants()
            if removed:
                logger.info("Pruned %d expired web sessions", removed)
            if grants_removed:
                logger.info("Pruned %d expired web download grants", grants_removed)


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
