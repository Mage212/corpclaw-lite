"""
Telegram bot runner — bootstraps the full agent stack and starts polling.

Environment variables required:
    TELEGRAM_BOT_TOKEN      — Telegram bot token
    CORPCLAW_IPC_SECRET     — IPC HMAC secret (fail-fast if absent)
    ANTHROPIC_API_KEY       — (optional) Anthropic key; falls back to OpenAI/local
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.users.manager import UserManager

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def _build_agent_loop() -> tuple[AgentLoop, UserManager, ToolRegistry]:
    """Build and return (AgentLoop, UserManager, ToolRegistry) using env/config settings."""
    import os

    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.config.settings import AgentSettings, LLMSettings, ProviderSettings
    from corpclaw_lite.departments.manager import DepartmentManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool
    from corpclaw_lite.extensions.tools.builtin.files import (
        EditFileTool,
        ListFilesTool,
        ReadFileTool,
        SearchFilesTool,
        WriteFileTool,
    )
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.security.tool_guard import ToolGuard

    # ── Provider ──────────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        provider = AnthropicProvider(
            ProviderSettings(type="anthropic", model="claude-3-haiku-20240307", api_key=api_key)
        )
    else:
        base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
        model = os.environ.get("OPENAI_MODEL", "qwen2.5:7b")
        from corpclaw_lite.llm.openai import OpenAIProvider

        provider = OpenAIProvider(
            ProviderSettings(type="openai", model=model, api_key="ollama", base_url=base_url)
        )
        logger.info("Using local LLM at %s model=%s", base_url, model)

    # ── Tools ─────────────────────────────────────────────────────────────────
    registry = ToolRegistry()
    builtin_tools = [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
    ]
    for tool in builtin_tools:
        registry.register(tool)

    # ── Memory tools ───────────────────────────────────────────────────────────
    # ── Vision / ReadImage ─────────────────────────────────────────────────────
    from corpclaw_lite.agent.vision import VisionProcessor

    # ── Exec / Excel ─────────────────────────────────────────────────────────
    from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
    from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
    from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool
    from corpclaw_lite.extensions.tools.builtin.memory import MemoryRecallTool, MemoryStoreTool

    # ── Web tool ───────────────────────────────────────────────────────────────
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool

    # ── Security ──────────────────────────────────────────────────────────────
    guard = ToolGuard()
    guard_rules = Path("config/tool_guard_rules.yaml")
    if guard_rules.exists():
        guard.load_file(guard_rules)

    dept_manager = DepartmentManager()
    dept_config = Path("config/departments.yaml")
    if dept_config.exists():
        dept_manager.load_file(dept_config)
    permission_checker = PermissionChecker(dept_manager)

    # ── Subagents ─────────────────────────────────────────────────────────────
    subagent_registry = SubagentRegistry()
    subagent_dir = Path("config/subagents")
    if subagent_dir.exists():
        subagent_registry.load_directory(subagent_dir)

    if subagent_registry.list_all():
        dispatcher = SubagentDispatcher(
            provider=provider,
            main_registry=registry,
            settings=AgentSettings(),
            tool_guard=guard,
            permission_checker=permission_checker,
        )
        registry.register(DispatchSubagentTool(dispatcher, subagent_registry))
        logger.info(
            "dispatch_subagent registered (%d subagents available)",
            len(subagent_registry.list_all()),
        )

    # ── Memory ────────────────────────────────────────────────────────────────
    memory = SQLiteMemory()
    registry.register(MemoryStoreTool(memory))
    registry.register(MemoryRecallTool(memory))

    # ── Web ────────────────────────────────────────────────────────────────────
    registry.register(WebFetchTool())

    # ── Vision ─────────────────────────────────────────────────────────────────
    vision = VisionProcessor(provider)
    registry.register(ReadImageTool(vision))

    # ── Exec / Excel ─────────────────────────────────────────────────────────
    registry.register(ExecScriptTool())
    registry.register(NormalizeExcelTool())

    # ── Settings ──────────────────────────────────────────────────────────────
    agent_settings = AgentSettings()
    _ = LLMSettings()  # imported for completeness; not passed directly

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        settings=agent_settings,
        memory=memory,
        tool_guard=guard,
        permission_checker=permission_checker,
        # approval_callback set later in run_telegram_bot() after channel is created
    )
    user_manager = UserManager()
    return loop, user_manager, registry


async def run_telegram_bot(token: str) -> None:
    """Start the Telegram bot and run until interrupted."""
    from corpclaw_lite.channels.telegram_channel import TelegramChannel
    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.users.models import User

    agent_loop, user_manager, tool_registry = _build_agent_loop()
    bootstrap = BootstrapLoader(Path("config/bootstrap"))

    async def _handle_message(telegram_id: str, message: str) -> str:
        tid = int(telegram_id)
        user = user_manager.get_by_telegram_id(tid)
        if not user:
            user = user_manager.create_user(telegram_id=tid, department="default")
            logger.info("Auto-registered new user telegram_id=%d", tid)

        # Build system prompt: bootstrap base + user context (fallback to default if dir empty)
        base_prompt = bootstrap.get_system_prompt()
        user_context = f"You are talking to {user.name} from the {user.department} department."
        system_prompt: str | None = f"{base_prompt}\n\n{user_context}" if base_prompt else None

        # Per-request closure — captures this user and channel; thread-safe (no shared mutation)
        async def approval_cb(action: str, details: str) -> bool:
            return await channel.request_approval(user, action, details)

        try:
            return await agent_loop.run(
                user, message, system_prompt=system_prompt, approval_callback=approval_cb
            )
        except Exception as e:
            logger.error("AgentLoop error for user %d: %s", tid, e)
            return f"Sorry, I encountered an error: {e}"

    channel = TelegramChannel(token=token, message_handler=_handle_message)

    # ── SendFile tool (needs channel reference) ───────────────────────────────
    from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool

    async def _send_file_cb(path: Path, user: User, caption: str) -> str:
        await channel.send_file(user, path, caption)
        return f"File '{path.name}' sent to user."

    tool_registry.register(SendFileTool(_send_file_cb))

    # Wire send_message back so agent can reply; per-request closure captures user + channel
    async def _handle_and_reply(telegram_id: str, message: str) -> None:
        tid = int(telegram_id)
        user = user_manager.get_by_telegram_id(tid) or User(
            id=0, name=f"user_{tid}", department="default", telegram_id=tid
        )
        reply = await _handle_message(telegram_id, message)
        await channel.send_message(user, reply)

    # Replace handler with the reply-capable version
    channel._on_message = _handle_and_reply  # type: ignore[assignment]

    # ── Health endpoint ────────────────────────────────────────────────────────
    try:
        from corpclaw_lite.logging import health

        asyncio.create_task(health.run_health_server())
        logger.info("Health endpoint started on :8080/health")
    except ImportError:
        logger.info("aiohttp not installed — health endpoint disabled")

    # ── Skill Hot-Reloader ────────────────────────────────────────────────────
    from corpclaw_lite.extensions.skills.registry import SkillRegistry
    from corpclaw_lite.extensions.skills.watcher import SkillHotReloader

    skill_registry = SkillRegistry()
    skills_dir = Path("skills")
    if skills_dir.exists():
        skill_registry.load_directory(skills_dir)
    reloader = SkillHotReloader(skills_dir, skill_registry)
    reloader.start()
    logger.info("Skill hot-reloader started watching %s", skills_dir)

    logger.info("Starting Telegram bot...")
    try:
        await channel.start()
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        reloader.stop()
        await channel.stop()
        logger.info("Telegram bot stopped.")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-token", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(run_telegram_bot(args.telegram_token))
