"""Agent stack factory — builds the complete agent pipeline from config/settings.yaml + .env.

Configuration layering:
    config/settings.yaml  – all provider definitions, routing rules, agent parameters
    .env                  – secrets only (API keys, bot tokens)

Environment variables in settings.yaml are interpolated as ${VAR:-default}.

Provider selection (from settings.yaml llm.named):
    - Define any number of named providers: default, vision, cloud, ...
    - Routing rules in llm.routing steer task_kind/subagent_id to a specific provider
    - The router itself implements the Provider protocol — drop-in for all components

Fallback (no settings.yaml or empty llm.named):
    - Reads ANTHROPIC_API_KEY → AnthropicProvider
    - Otherwise reads OPENAI_BASE_URL + OPENAI_MODEL + OPENAI_API_KEY → OpenAIProvider
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
    from corpclaw_lite.llm.base import Provider

logger = logging.getLogger(__name__)

# Project root: corpclaw-lite/ — 4 levels up from this file.
# Supports CORPCLAW_ROOT env var override for Docker/systemd deployments.
PROJECT_ROOT = Path(
    os.environ.get("CORPCLAW_ROOT", "") or Path(__file__).parent.parent.parent.parent
)


def _build_provider_from_env() -> Provider:
    """Fallback: build a single provider directly from environment variables.

    Used when config/settings.yaml is missing or has no llm.named providers.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        from corpclaw_lite.config.settings import ProviderSettings
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        logger.info("Fallback provider: Anthropic model=%s", model)
        return AnthropicProvider(
            ProviderSettings(type="anthropic", model=model, api_key=api_key, base_url=base_url)
        )

    from corpclaw_lite.config.settings import ProviderSettings
    from corpclaw_lite.llm.openai import OpenAIProvider

    base_url = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("OPENAI_MODEL", "qwen2.5:7b")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "ollama")
    logger.info("Fallback provider: OpenAI-compatible base_url=%s model=%s", base_url, model)
    return OpenAIProvider(
        ProviderSettings(type="openai", model=model, api_key=openai_api_key, base_url=base_url)
    )


def _build_router() -> Provider:
    """Load settings.yaml and build an LLMRouter (or a single fallback provider)."""
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.llm.router import LLMRouter

    settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")

    if settings.llm.named:
        logger.info("Building LLMRouter from settings.yaml (%d providers)", len(settings.llm.named))
        return LLMRouter.from_settings(settings.llm)

    logger.info(
        "No named providers in settings.yaml — falling back to env-based provider selection"
    )
    return _build_provider_from_env()


def build_agent_stack() -> tuple[AgentLoop, UserManager, ToolRegistry]:
    """Build and return (AgentLoop, UserManager, ToolRegistry) from config + env.

    The returned AgentLoop uses an LLMRouter that automatically routes:
    - task_kind "vision" → vision provider (or default if not configured)
    - subagent_id rules  → subagent-specific providers
    - everything else    → default provider

    To add a new provider: add it to config/settings.yaml llm.named and optionally
    a routing rule in llm.routing. No code changes required.
    """
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.agent.vision import VisionProcessor
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.settings import AgentSettings
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

    # ── Provider / Router ─────────────────────────────────────────────────────
    provider = _build_router()

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

    # ── Agent Settings (from settings.yaml, with env-overrides) ───────────────
    full_settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    agent_settings = full_settings.agent if full_settings.agent else AgentSettings()

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
