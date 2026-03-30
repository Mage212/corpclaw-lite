"""Agent stack factory — builds the complete agent pipeline from environment/config.

Extracted from telegram/runner.py to enable reuse across channels (CLI, Telegram, future).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.users.manager import UserManager

__all__ = [
    "PROJECT_ROOT",
    "build_agent_stack",
]

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Project root: corpclaw-lite/ — 4 levels up from this file.
# Supports CORPCLAW_ROOT env var override for Docker/systemd deployments.
PROJECT_ROOT = Path(
    os.environ.get("CORPCLAW_ROOT", "") or Path(__file__).parent.parent.parent.parent
)


def build_agent_stack() -> tuple[AgentLoop, UserManager, ToolRegistry]:
    """Build and return (AgentLoop, UserManager, ToolRegistry) using env/config settings.

    Provider selection:
        - If ANTHROPIC_API_KEY is set → AnthropicProvider
        - Otherwise → OpenAIProvider (Ollama/vLLM/LM Studio)

    All built-in tools are registered. ToolGuard, permissions, subagents,
    memory, and vision are wired automatically from config files.
    """
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.agent.vision import VisionProcessor
    from corpclaw_lite.config.settings import AgentSettings, ProviderSettings
    from corpclaw_lite.departments.manager import DepartmentManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool
    from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
    from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
    from corpclaw_lite.extensions.tools.builtin.files import (
        EditFileTool,
        ListFilesTool,
        ReadFileTool,
        SearchFilesTool,
        WriteFileTool,
    )
    from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool
    from corpclaw_lite.extensions.tools.builtin.memory import MemoryRecallTool, MemoryStoreTool
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.security.tool_guard import ToolGuard

    # ── Provider ──────────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        anthropic_model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        provider = AnthropicProvider(
            ProviderSettings(type="anthropic", model=anthropic_model, api_key=api_key)
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
    for tool in [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
    ]:
        registry.register(tool)

    # ── Security ───────────────────────────────────────────────────────────────────
    guard = ToolGuard()
    guard_rules = PROJECT_ROOT / "config" / "tool_guard_rules.yaml"
    if guard_rules.exists():
        guard.load_file(guard_rules)

    dept_manager = DepartmentManager()
    dept_config = PROJECT_ROOT / "config" / "departments.yaml"
    if dept_config.exists():
        dept_manager.load_file(dept_config)
    permission_checker = PermissionChecker(dept_manager)

    # ── Agent Settings ─────────────────────────────────────────────────────────
    agent_settings = AgentSettings()

    # ── Subagents ──────────────────────────────────────────────────────────────────
    subagent_registry = SubagentRegistry()
    subagent_dir = PROJECT_ROOT / "config" / "subagents"
    if subagent_dir.exists():
        subagent_registry.load_directory(subagent_dir)

    if subagent_registry.list_all():
        dispatcher = SubagentDispatcher(
            provider=provider,
            main_registry=registry,
            settings=agent_settings,
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

    # ── Memory Consolidation ──────────────────────────────────────────────────
    consolidator = None
    if agent_settings.consolidation_enabled:
        from corpclaw_lite.memory.consolidation import MemoryConsolidator

        consolidator = MemoryConsolidator(
            provider=provider,
            threshold=agent_settings.consolidation_threshold,
        )
        logger.info(
            "Memory consolidation enabled (threshold=%d)",
            agent_settings.consolidation_threshold,
        )

    # ── Context Compression ────────────────────────────────────────────────────
    compressor = None
    if agent_settings.compression.enabled:
        from corpclaw_lite.agent.compressor import ContextCompressor

        compressor = ContextCompressor(provider, agent_settings.compression)
        logger.info(
            "Context compression enabled (threshold_ratio=%.2f)",
            agent_settings.compression.threshold_ratio,
        )

    # ── Agent Loop ────────────────────────────────────────────────────────────
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        settings=agent_settings,
        memory=memory,
        tool_guard=guard,
        permission_checker=permission_checker,
        consolidator=consolidator,
        compressor=compressor,
    )
    user_manager = UserManager()
    return loop, user_manager, registry
