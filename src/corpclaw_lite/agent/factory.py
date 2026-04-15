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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.users.manager import UserManager

__all__ = [
    "PROJECT_ROOT",
    "AgentStack",
    "build_agent_stack",
]

if TYPE_CHECKING:
    from corpclaw_lite.agent.compressor import ContextCompressor
    from corpclaw_lite.config.settings import AgentSettings, Settings
    from corpclaw_lite.container.ipc import ContainerIPC
    from corpclaw_lite.container.manager import ContainerManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.mcp.manager import MCPManager
    from corpclaw_lite.extensions.plugins.registry import PluginRegistry
    from corpclaw_lite.extensions.skills.matcher import SkillMatcher
    from corpclaw_lite.extensions.skills.registry import SkillRegistry
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.memory.consolidation import MemoryConsolidator
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)


@dataclass
class AgentStack:
    """Assembled agent pipeline — all components ready to serve requests."""

    loop: AgentLoop
    user_manager: UserManager
    tool_registry: ToolRegistry
    mcp_manager: MCPManager | None
    container_manager: ContainerManager | None
    few_shots: list[dict[str, Any]] | None = None
    subagent_registry: SubagentRegistry | None = None
    skill_registry: SkillRegistry | None = None
    plugin_registry: PluginRegistry | None = None
    skill_matcher: SkillMatcher | None = None


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
    from corpclaw_lite.llm.presets import PresetRegistry
    from corpclaw_lite.llm.router import LLMRouter

    settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")

    # Load model presets (optional — file may not exist)
    presets_path = PROJECT_ROOT / "config" / "model_presets.yaml"
    preset_registry = PresetRegistry.from_yaml(presets_path) if presets_path.exists() else None

    if settings.llm.named:
        logger.info("Building LLMRouter from settings.yaml (%d providers)", len(settings.llm.named))
        return LLMRouter.from_settings(settings.llm, preset_registry=preset_registry)

    logger.info(
        "No named providers in settings.yaml — falling back to env-based provider selection"
    )
    return _build_provider_from_env()


def _sandboxed_tool_classes() -> list[Any]:
    """Return the list of tool classes that need container isolation."""
    from corpclaw_lite.extensions.tools.builtin.chart_generate import ChartGenerateTool
    from corpclaw_lite.extensions.tools.builtin.convert_format import ConvertFormatTool
    from corpclaw_lite.extensions.tools.builtin.diff_text import DiffTextTool
    from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
    from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool
    from corpclaw_lite.extensions.tools.builtin.files import (
        EditFileTool,
        ListFilesTool,
        ReadFileTool,
        SearchFilesTool,
        WriteFileTool,
    )
    from corpclaw_lite.extensions.tools.builtin.pdf_reader import PdfReaderTool
    from corpclaw_lite.extensions.tools.builtin.table_query import TableQueryTool

    return [
        ReadFileTool(),
        WriteFileTool(),
        EditFileTool(),
        ListFilesTool(),
        SearchFilesTool(),
        ExecScriptTool(),
        NormalizeExcelTool(),
        DiffTextTool(),
        ConvertFormatTool(),
        TableQueryTool(),
        ChartGenerateTool(),
        PdfReaderTool(),
    ]


def _register_sandboxed_tools(
    registry: ToolRegistry,
    ipc: ContainerIPC,
) -> None:
    """Register file/script tools as IPCToolProxy (execute inside container)."""
    from corpclaw_lite.container.proxy import IPCToolProxy

    for tool in _sandboxed_tool_classes():
        registry.register(IPCToolProxy.from_tool(tool, ipc))
        logger.debug("Registered sandboxed IPCToolProxy: %s", tool.name)


def _register_local_tools(registry: ToolRegistry) -> None:
    """Register file/script tools to run directly on the host (dev/test mode)."""
    for tool in _sandboxed_tool_classes():
        registry.register(tool)
        logger.debug("Registered local tool (no container): %s", tool.name)


def _build_security_stack(
    settings: Settings, provider: Provider
) -> tuple[ToolGuard, PermissionChecker]:
    """Build ToolGuard + PermissionChecker from config."""
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.departments.manager import DepartmentManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.security.tool_guard import ToolGuard

    agent_settings = settings.agent if settings.agent else AgentSettings()
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
    return guard, PermissionChecker(dept_manager)


def _build_extensions_stack(
    agent_settings: AgentSettings,
    provider: Provider,
    registry: ToolRegistry,
    guard: ToolGuard,
    permission_checker: PermissionChecker,
    workspace_base: Path | None = None,
    skill_matcher: SkillMatcher | None = None,
    skill_registry: SkillRegistry | None = None,
) -> SubagentRegistry:
    """Register subagents, MCP, host-side tools."""
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.agent.vision import VisionProcessor
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool
    from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool

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
            skill_matcher=skill_matcher,
            skill_registry=skill_registry,
        )
        registry.register(DispatchSubagentTool(dispatcher, subagent_registry))
        logger.info(
            "dispatch_subagent registered (%d subagents available)",
            len(subagent_registry.list_all()),
        )

    registry.register(WebFetchTool())
    registry.register(ReadImageTool(VisionProcessor(provider), workspace_base=workspace_base))
    return subagent_registry


def _build_memory_stack(
    agent_settings: AgentSettings,
    provider: Provider,
    registry: ToolRegistry,
) -> tuple[SQLiteMemory, MemoryConsolidator | None, ContextCompressor | None]:
    """Build memory, consolidation, compression."""
    from corpclaw_lite.agent.compressor import ContextCompressor
    from corpclaw_lite.extensions.tools.builtin.memory import MemoryRecallTool, MemoryStoreTool
    from corpclaw_lite.memory.consolidation import MemoryConsolidator
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    memory = SQLiteMemory()
    registry.register(MemoryStoreTool(memory))
    registry.register(MemoryRecallTool(memory))

    consolidator = None
    if agent_settings.consolidation_enabled:
        consolidator = MemoryConsolidator(
            provider=provider,
            threshold=agent_settings.consolidation_threshold,
        )
        logger.info(
            "Memory consolidation enabled (threshold=%d)", agent_settings.consolidation_threshold
        )

    compressor = None
    if agent_settings.compression.enabled:
        compressor = ContextCompressor(provider, agent_settings.compression)
        logger.info(
            "Context compression enabled (threshold_ratio=%.2f)",
            agent_settings.compression.threshold_ratio,
        )
    return memory, consolidator, compressor


def _build_system_prompt() -> str | None:
    """Load bootstrap system prompt."""
    from corpclaw_lite.config.bootstrap import BootstrapLoader

    bootstrap_dir = PROJECT_ROOT / "config" / "bootstrap"
    bootstrap = BootstrapLoader(bootstrap_dir)
    system_prompt = bootstrap.get_system_prompt() or None
    if system_prompt:
        logger.info("Loaded system prompt from %s (%d chars)", bootstrap_dir, len(system_prompt))
    else:
        logger.warning("No bootstrap/*.md files found — using minimal default system prompt")
    return system_prompt


def build_agent_stack(
    settings: Settings | None = None,
) -> AgentStack:
    """Build and return the complete agent stack from config + env."""
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.container.ipc import ContainerIPC
    from corpclaw_lite.container.manager import ContainerManager
    from corpclaw_lite.extensions.tools.registry import ToolRegistry

    full_settings: Settings = (
        settings
        if settings is not None
        else load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    )
    container_cfg = full_settings.container
    agent_settings = full_settings.agent if full_settings.agent else AgentSettings()

    provider = _build_router()
    registry = ToolRegistry()

    container_manager: ContainerManager | None = None
    workspace_base: Path | None = None
    if container_cfg.enabled:
        if not ContainerManager.is_docker_available():
            raise RuntimeError(
                "Container isolation is enabled (container.enabled=true) "
                "but Docker daemon is not available. "
                "Start Docker or set container.enabled=false in settings.yaml to use dev mode."
            )
        from corpclaw_lite.security.ipc_auth import IPCAuth
        from corpclaw_lite.security.network_policy import NetworkPolicy

        network_policy = NetworkPolicy()

        try:
            container_ipc = ContainerIPC(
                auth=IPCAuth(), timeout_seconds=container_cfg.ipc_timeout_seconds
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to initialise ContainerIPC: {e}. Is CORPCLAW_IPC_SECRET set in .env?"
            ) from e

        workspace_base = (PROJECT_ROOT / container_cfg.workspace_base).resolve()
        container_manager = ContainerManager(
            settings=container_cfg,
            network_policy=network_policy,
            workspace_base=workspace_base,
            ipc=container_ipc,
        )
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

    guard, permission_checker = _build_security_stack(full_settings, provider)

    # Load extensions (skills, plugins, skill matcher) BEFORE building extensions stack
    # so that SubagentDispatcher gets access to SkillMatcher for skill injection.
    from corpclaw_lite.extensions.bootstrap import load_extensions

    skill_registry, plugin_registry, skill_matcher = load_extensions(
        PROJECT_ROOT, registry, full_settings.skills
    )

    subagent_registry = _build_extensions_stack(
        agent_settings,
        provider,
        registry,
        guard,
        permission_checker,
        workspace_base=workspace_base,
        skill_matcher=skill_matcher,
        skill_registry=skill_registry,
    )
    memory, consolidator, compressor = _build_memory_stack(agent_settings, provider, registry)

    mcp_manager: MCPManager | None = None
    mcp_config = PROJECT_ROOT / "config" / "mcp_servers.yaml"
    if mcp_config.exists():
        from corpclaw_lite.extensions.mcp.manager import MCPManager

        mcp_manager = MCPManager(config_path=mcp_config)
        logger.info("MCPManager ready (config=%s) — callers must await connect_all()", mcp_config)

    system_prompt = _build_system_prompt()

    # Load calibrated few-shots (if any) for injection into every run()
    few_shots: list[dict[str, Any]] | None = None
    calibrated_few_shots_path = PROJECT_ROOT / "config" / "calibrated" / "few_shots.yaml"
    if calibrated_few_shots_path.exists():
        import yaml as _yaml

        _raw_data: dict[str, Any] = (
            _yaml.safe_load(calibrated_few_shots_path.read_text(encoding="utf-8")) or {}
        )
        _examples: list[dict[str, Any]] = list(_raw_data.get("examples", []))
        if _examples:
            few_shots = _examples
            logger.info("Loaded %d calibrated few-shot examples", len(_examples))

    loop = AgentLoop(
        AgentConfig(
            provider=provider,
            registry=registry,
            settings=agent_settings,
            memory=memory,
            tool_guard=guard,
            permission_checker=permission_checker,
            consolidator=consolidator,
            compressor=compressor,
            default_system_prompt=system_prompt,
        )
    )
    user_manager = UserManager()

    return AgentStack(
        loop=loop,
        user_manager=user_manager,
        tool_registry=registry,
        mcp_manager=mcp_manager,
        container_manager=container_manager,
        few_shots=few_shots,
        subagent_registry=subagent_registry,
        skill_registry=skill_registry,
        plugin_registry=plugin_registry,
        skill_matcher=skill_matcher,
    )
