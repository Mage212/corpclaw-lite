"""Agent stack factory — builds the complete agent pipeline from config/settings.yaml + .env.

Configuration layering:
    config/settings.yaml  – all provider definitions, routing rules, agent parameters
    .env                  – secrets only (API keys, bot tokens)

Container isolation (container.enabled=true, default):
    - ContainerManager starts a Docker container per user on first message
    - File/script tools (read_file, write_file, edit_file, list_files, search_files,
      exec_script) are registered as IPCToolProxy — they execute INSIDE the container
    - Host-side tools (web_fetch, memory_*, read_image, send_file, normalize_excel,
      dispatch_subagent) run on the host as usual
    - If Docker is unavailable with container.enabled=true → RuntimeError at startup

Dev mode (container.enabled=false):
    - All tools run directly on the host (no isolation)
    - Useful for local development without Docker
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

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


def _register_sandboxed_tools(
    registry: ToolRegistry,
    ipc: Any,
) -> None:
    """Register file/script tools as IPCToolProxy (execute inside container)."""
    from corpclaw_lite.container.proxy import IPCToolProxy
    from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
    from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
    from corpclaw_lite.extensions.tools.builtin.files import (
        EditFileTool,
        ListFilesTool,
        ReadFileTool,
        SearchFilesTool,
        WriteFileTool,
    )

    sandboxed = [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
        ExecScriptTool(),
        NormalizeExcelTool(),
    ]
    for tool in sandboxed:
        registry.register(IPCToolProxy.from_tool(tool, ipc))
        logger.debug("Registered sandboxed IPCToolProxy: %s", tool.name)


def _register_local_tools(registry: ToolRegistry) -> None:
    """Register file/script tools to run directly on the host (dev/test mode)."""
    from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
    from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
    from corpclaw_lite.extensions.tools.builtin.files import (
        EditFileTool,
        ListFilesTool,
        ReadFileTool,
        SearchFilesTool,
        WriteFileTool,
    )

    for tool in [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
        ExecScriptTool(),
        NormalizeExcelTool(),
    ]:
        registry.register(tool)
        logger.debug("Registered local tool (no container): %s", tool.name)


def build_agent_stack() -> tuple[AgentLoop, UserManager, ToolRegistry]:
    """Build and return (AgentLoop, UserManager, ToolRegistry) from config + env.

    Container isolation:
        - container.enabled=true (default): file/script tools run inside Docker
        - container.enabled=false: everything runs on host (dev mode)

    LLM routing:
        - Reads config/settings.yaml → builds LLMRouter with named providers
        - Falls back to env vars if no named providers configured

    Returns:
        (AgentLoop, UserManager, ToolRegistry) ready to serve requests.

    Raises:
        RuntimeError: If container.enabled=true but Docker is not available.
    """
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.agent.vision import VisionProcessor
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.container.ipc import ContainerIPC
    from corpclaw_lite.container.manager import ContainerManager
    from corpclaw_lite.departments.manager import DepartmentManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool
    from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool
    from corpclaw_lite.extensions.tools.builtin.memory import MemoryRecallTool, MemoryStoreTool
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.security.tool_guard import ToolGuard

    # ── Load settings ─────────────────────────────────────────────────────────
    full_settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    container_cfg = full_settings.container
    agent_settings = full_settings.agent if full_settings.agent else AgentSettings()

    # ── Provider / Router ─────────────────────────────────────────────────────
    provider = _build_router()

    # ── Tool Registry ─────────────────────────────────────────────────────────
    registry = ToolRegistry()

    # ── Container Isolation ────────────────────────────────────────────────────
    container_manager: ContainerManager | None = None
    if container_cfg.enabled:
        if not ContainerManager.is_docker_available():
            raise RuntimeError(
                "Container isolation is enabled (container.enabled=true) "
                "but Docker daemon is not available. "
                "Start Docker or set container.enabled=false in settings.yaml to use dev mode."
            )
        from corpclaw_lite.security.network_policy import NetworkPolicy

        network_policy = NetworkPolicy()
        network_policy_cfg = PROJECT_ROOT / "config" / "network_policy.yaml"
        if network_policy_cfg.exists():
            network_policy.load_file(network_policy_cfg)

        workspace_base = PROJECT_ROOT / container_cfg.workspace_base
        container_manager = ContainerManager(
            settings=container_cfg,
            network_policy=network_policy,
            workspace_base=workspace_base,
        )
        # Build shared IPC client (stateless — one instance handles all users)
        try:
            from corpclaw_lite.security.ipc_auth import IPCAuth

            container_ipc = ContainerIPC(auth=IPCAuth(), timeout_seconds=120.0)
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialise ContainerIPC: {e}. Is CORPCLAW_IPC_SECRET set in .env?"
            ) from e

        _register_sandboxed_tools(registry, container_ipc)
        logger.info(
            "Container isolation ENABLED — file/script tools routed to Docker "
            "(image=%s, workspace_base=%s)",
            container_cfg.image,
            workspace_base,
        )
    else:
        _register_local_tools(registry)
        logger.warning(
            "Container isolation DISABLED (container.enabled=false) — "
            "file/script tools run on host. Dev mode only!"
        )

    # ── Security ───────────────────────────────────────────────────────────────
    guard = ToolGuard(
        provider=provider if agent_settings.approval_mode == "smart" else None,
        approval_mode=agent_settings.approval_mode,
    )
    guard_rules = PROJECT_ROOT / "config" / "tool_guard_rules.yaml"
    if guard_rules.exists():
        guard.load_file(guard_rules)

    dept_manager = DepartmentManager()
    dept_config = PROJECT_ROOT / "config" / "departments.yaml"
    if dept_config.exists():
        dept_manager.load_file(dept_config)
    permission_checker = PermissionChecker(dept_manager)

    # ── Subagents ──────────────────────────────────────────────────────────────
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

    # ── Host-side tools (always on host, regardless of container mode) ────────
    registry.register(WebFetchTool())

    vision = VisionProcessor(provider)
    registry.register(ReadImageTool(vision))

    # ── Memory Consolidation ──────────────────────────────────────────────────
    consolidator = None
    if agent_settings.consolidation_enabled:
        from corpclaw_lite.memory.consolidation import MemoryConsolidator

        consolidator = MemoryConsolidator(
            provider=provider,
            threshold=agent_settings.consolidation_threshold,
        )
        logger.info(
            "Memory consolidation enabled (threshold=%d)", agent_settings.consolidation_threshold
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

    # Return container_manager bundled into UserManager for use in runner
    # We store it as an attribute so runner.py can access it without changing the signature
    user_manager._container_manager = container_manager  # type: ignore[attr-defined]

    return loop, user_manager, registry
