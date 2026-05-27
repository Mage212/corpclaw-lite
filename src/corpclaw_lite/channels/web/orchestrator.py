# ruff: noqa: E501
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from corpclaw_lite.agent.factory import build_agent_stack
from corpclaw_lite.channels.service import AgentRequestCallbacks, AgentRequestService
from corpclaw_lite.channels.telegram.rate_limit import RateLimiter
from corpclaw_lite.channels.web.files import (
    delete_path,
    list_directory,
    make_directory,
    resolve_workspace_path,
    save_upload,
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


@dataclass(slots=True)
class _DownloadGrant:
    user_id: int
    path: Path
    caption: str


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
        self._cleanup_task: asyncio.Task[None] | None = None
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
        if self._runner is not None:
            await self._runner.cleanup()
        if self._stack is not None and self._stack.mcp_manager is not None:
            await self._stack.mcp_manager.disconnect_all()
        if self._started:
            logger.info("Web channel stopped cleanly.")
            self._started = False
        else:
            logger.debug("Web channel cleanup completed before full startup.")

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/login", self._handle_login_page)
        app.router.add_post("/login", self._handle_login)
        app.router.add_post("/logout", self._handle_logout)
        app.router.add_get("/ws/chat", self._handle_chat_ws)
        app.router.add_get("/api/files", self._handle_list_files)
        app.router.add_post("/api/files/upload", self._handle_upload)
        app.router.add_post("/api/files/mkdir", self._handle_mkdir)
        app.router.add_delete("/api/files", self._handle_delete)
        app.router.add_get("/api/files/download", self._handle_download_file)
        app.router.add_get("/api/download/{token}", self._handle_download_grant)
        return app

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

    async def _handle_login_page(self, request: web.Request) -> web.Response:
        if isinstance(request.get("user"), User):
            raise self._redirect("/")
        return web.Response(text=_LOGIN_HTML.replace("{{error}}", ""), content_type="text/html")

    async def _handle_login(self, request: web.Request) -> web.Response:
        if self._stack is None:
            raise web.HTTPServiceUnavailable()
        data = await request.post()
        username = str(data.get("username", ""))
        password = str(data.get("password", ""))
        user = self._stack.user_manager.authenticate_web_user(username, password)
        if user is None:
            return web.Response(
                text=_LOGIN_HTML.replace("{{error}}", "Неверный логин или пароль"),
                content_type="text/html",
                status=401,
            )
        token, _csrf = self._stack.user_manager.create_web_session(
            user.id, ttl_hours=self._web_settings.session_ttl_hours
        )
        response = self._redirect("/")
        response.set_cookie(
            self._web_settings.cookie_name,
            token,
            httponly=True,
            samesite="Strict",
            max_age=self._web_settings.session_ttl_hours * 3600,
        )
        return response

    async def _handle_logout(self, request: web.Request) -> web.Response:
        if self._stack is not None:
            token = request.cookies.get(self._web_settings.cookie_name)
            if token:
                self._stack.user_manager.delete_web_session(token)
        response = self._redirect("/login")
        response.del_cookie(self._web_settings.cookie_name)
        return response

    async def _handle_index(self, request: web.Request) -> web.Response:
        user = request.get("user")
        if not isinstance(user, User):
            raise self._redirect("/login")
        html = _APP_HTML.replace("{{csrf}}", str(request.get("csrf_token", ""))).replace(
            "{{name}}", user.name
        )
        return web.Response(text=html, content_type="text/html")

    def _require_user(self, request: web.Request) -> User:
        user = request.get("user")
        if not isinstance(user, User):
            raise web.HTTPUnauthorized()
        return user

    def _workspace_for(self, user: User) -> Path:
        if self._service is None:
            raise web.HTTPServiceUnavailable()
        return self._service.get_user_workspace(user)

    async def _handle_list_files(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        result = await list_directory(self._workspace_for(user), request.query.get("path"))
        return web.json_response(result)

    async def _handle_upload(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        reader = await request.multipart()
        field: Any = await reader.next()
        if field is None or not field.filename:
            raise web.HTTPBadRequest(text="Missing file")
        data = await field.read(decode=False)
        target_dir = request.query.get("path")
        rel = await save_upload(
            workspace=self._workspace_for(user),
            filename=field.filename,
            data=data,
            max_bytes=self._web_settings.upload_max_bytes,
            target_dir=target_dir,
        )
        return web.json_response({"path": rel})

    async def _handle_mkdir(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        payload = await request.json()
        rel = await make_directory(
            self._workspace_for(user),
            str(payload.get("path", "")),
            str(payload.get("name", "")),
        )
        return web.json_response({"path": rel})

    async def _handle_delete(self, request: web.Request) -> web.Response:
        user = self._require_user(request)
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        await delete_path(self._workspace_for(user), path)
        return web.json_response({"ok": True})

    async def _handle_download_file(self, request: web.Request) -> web.FileResponse:
        user = self._require_user(request)
        path = request.query.get("path")
        if not path:
            raise web.HTTPBadRequest(text="Missing path")
        resolved = resolve_workspace_path(self._workspace_for(user), path)
        if not resolved.exists() or not resolved.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(resolved)

    async def _handle_download_grant(self, request: web.Request) -> web.FileResponse:
        user = self._require_user(request)
        grant = self._download_grants.get(request.match_info["token"])
        if grant is None or grant.user_id != user.id:
            raise web.HTTPNotFound()
        return web.FileResponse(grant.path)

    async def _handle_chat_ws(self, request: web.Request) -> web.WebSocketResponse:
        user = self._require_user(request)
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
            if self._service is None:
                await send({"type": "error", "message": "Service is not ready"})
                return
            if not await self._rate_limiter.check(user.id):
                await send({"type": "error", "message": "Слишком много сообщений."})
                return
            if not await self._service.try_start_user_request(user.id):
                await send({"type": "error", "message": "Предыдущая задача ещё выполняется."})
                return
            try:
                result = await self._service.run(
                    user=user,
                    message=text,
                    mode=mode,
                    channel="web",
                    callbacks=AgentRequestCallbacks(
                        request_approval=approval_cb,
                        on_tool_start=lambda tool: send_task({"type": "status", "stage": tool}),
                        on_llm_stage=lambda stage: send_task({"type": "status", "stage": stage}),
                    ),
                )
                await send({"type": "assistant_message", "message": result.reply})
            except LLMBackendUnavailableError as e:
                await send(
                    {
                        "type": "warning",
                        "kind": "llm_unavailable",
                        "llm_status": "unavailable",
                        "message": e.user_message(),
                    }
                )
            except Exception as e:
                logger.exception("Web agent request failed for user %s", user.id)
                await send({"type": "error", "message": f"Ошибка: {e}"})
            finally:
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
                event_type = payload.get("type")
                if event_type == "mode_change":
                    requested = payload.get("mode")
                    if requested in {"chat", "execute"}:
                        mode = str(requested)
                        await send({"type": "mode", "mode": mode})
                elif event_type == "approve" or event_type == "deny":
                    approval_id = str(payload.get("approval_id", ""))
                    future = approvals.get(approval_id)
                    if future is not None and not future.done():
                        future.set_result(event_type == "approve")
                elif event_type == "message":
                    text = str(payload.get("message", "")).strip()
                    if text:
                        asyncio.create_task(run_message(text))
        finally:
            self._clients.pop(user.id, None)
            for future in approvals.values():
                if not future.done():
                    future.set_result(False)
        return ws

    async def _send_file_callback(self, path: Path, user: User, caption: str) -> str:
        token = secrets.token_urlsafe(18)
        self._download_grants[token] = _DownloadGrant(user_id=user.id, path=path, caption=caption)
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

    async def _session_cleanup_loop(self) -> None:
        if self._stack is None:
            return
        while True:
            await asyncio.sleep(3600)
            removed = self._stack.user_manager.prune_expired_web_sessions()
            if removed:
                logger.info("Pruned %d expired web sessions", removed)


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


_LOGIN_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>CorpClaw Lite Login</title>
<style>body{font-family:system-ui;margin:0;display:grid;place-items:center;min-height:100vh;background:#f5f5f2;color:#1f2933}form{width:320px;background:white;border:1px solid #ddd;padding:24px}input,button{width:100%;box-sizing:border-box;margin-top:10px;padding:10px}button{background:#1f6feb;color:white;border:0}.error{color:#b42318}</style>
</head><body><form method="post" action="/login"><h1>CorpClaw Lite</h1><p class="error">{{error}}</p><input name="username" placeholder="Username" autocomplete="username"><input name="password" type="password" placeholder="Password" autocomplete="current-password"><button>Войти</button></form></body></html>"""

_APP_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>CorpClaw Lite</title>
<style>
body{margin:0;font-family:system-ui;background:#f7f7f4;color:#17202a}.app{height:100vh;display:grid;grid-template-columns:320px 1fr}.files{border-right:1px solid #d8d8d2;background:#fff;padding:14px;overflow:auto}.chat{display:grid;grid-template-rows:auto 1fr auto;height:100vh}.top{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #d8d8d2;background:#fff}.messages{padding:18px;overflow:auto}.msg{max-width:840px;margin:0 0 12px;padding:10px 12px;background:#fff;border:1px solid #ddd}.user{background:#e8f1ff}.composer{display:flex;gap:8px;padding:12px;background:#fff;border-top:1px solid #d8d8d2}textarea{flex:1;min-height:54px}button,input,select{padding:8px}.file{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid #eee;padding:7px 0}.status{color:#59636e;font-size:13px}.approval{border-color:#d29922;background:#fff8c5}
</style></head><body><div class="app"><aside class="files"><h2>Файлы</h2><input id="upload" type="file"><button id="mkdir">Папка</button><div id="fileList"></div></aside><main class="chat"><div class="top"><div>CorpClaw Lite — {{name}}</div><div><select id="mode"><option value="execute">execute</option><option value="chat">chat</option></select><form method="post" action="/logout" style="display:inline"><input type="hidden" name="_csrf" value="{{csrf}}"><button>Выйти</button></form></div></div><div id="messages" class="messages"></div><div class="composer"><textarea id="input" placeholder="Введите сообщение"></textarea><button id="send">Отправить</button></div></main></div>
<script>
const csrf="{{csrf}}"; let cwd=""; const ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws/chat`);
const messages=document.getElementById("messages"); const fileList=document.getElementById("fileList");
function add(text, cls="msg"){const d=document.createElement("div");d.className=cls;d.textContent=text;messages.appendChild(d);messages.scrollTop=messages.scrollHeight;}
function api(path, opts={}){opts.headers=Object.assign({"X-CSRF-Token":csrf},opts.headers||{});return fetch(path,opts);}
async function loadFiles(path=""){cwd=path;const r=await fetch(`/api/files?path=${encodeURIComponent(path)}`);const j=await r.json();fileList.innerHTML=""; if(j.path){const up=document.createElement("button");up.textContent="..";up.onclick=()=>loadFiles(j.path.split("/").slice(0,-1).join("/"));fileList.appendChild(up);} for(const e of j.entries){const row=document.createElement("div");row.className="file";const open=document.createElement("button");open.textContent=e.is_dir?`[${e.name}]`:e.name;open.onclick=()=>e.is_dir?loadFiles(e.path):window.open(`/api/files/download?path=${encodeURIComponent(e.path)}`,"_blank");const del=document.createElement("button");del.textContent="Удалить";del.onclick=async()=>{if(confirm(`Удалить ${e.name}?`)){await api(`/api/files?path=${encodeURIComponent(e.path)}`,{method:"DELETE"});loadFiles(cwd)}};row.append(open,del);fileList.appendChild(row);}}
ws.onmessage=(ev)=>{const e=JSON.parse(ev.data); if(e.type==="assistant_message")add(e.message); else if(e.type==="status")add(`Статус: ${e.stage}`,"msg status"); else if(e.type==="warning")add(`Предупреждение: ${e.message}`,"msg status"); else if(e.type==="error")add(e.message,"msg status"); else if(e.type==="file_ready")add(`Файл готов: ${e.name} ${e.url}`); else if(e.type==="approval_required"){const d=document.createElement("div");d.className="msg approval";d.textContent=`Подтверждение: ${e.action}\n${e.details}`;const a=document.createElement("button");a.textContent="Approve";a.onclick=()=>ws.send(JSON.stringify({type:"approve",approval_id:e.approval_id}));const n=document.createElement("button");n.textContent="Deny";n.onclick=()=>ws.send(JSON.stringify({type:"deny",approval_id:e.approval_id}));d.append(a,n);messages.appendChild(d);}};
document.getElementById("send").onclick=()=>{const i=document.getElementById("input");const text=i.value.trim();if(text){add(text,"msg user");ws.send(JSON.stringify({type:"message",message:text}));i.value="";}};
document.getElementById("mode").onchange=(e)=>ws.send(JSON.stringify({type:"mode_change",mode:e.target.value}));
document.getElementById("upload").onchange=async(e)=>{const f=e.target.files[0];if(!f)return;const fd=new FormData();fd.append("file",f);await api(`/api/files/upload?path=${encodeURIComponent(cwd)}`,{method:"POST",body:fd,headers:{"X-CSRF-Token":csrf}});e.target.value="";loadFiles(cwd);};
document.getElementById("mkdir").onclick=async()=>{const name=prompt("Имя папки");if(name){await api("/api/files/mkdir",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":csrf},body:JSON.stringify({path:cwd,name})});loadFiles(cwd);}};
loadFiles();
</script></body></html>"""
