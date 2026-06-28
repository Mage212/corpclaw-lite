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
    - If Docker is unavailable with container.enabled=true → typed startup exception

Dev mode (container.enabled=false):
    - All tools run directly on the host (no isolation)
    - Useful for local development without Docker
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.exceptions import StartupConfigurationError
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.users.manager import UserManager

__all__ = [
    "PROJECT_ROOT",
    "AgentStack",
    "build_agent_stack",
]

if TYPE_CHECKING:
    from corpclaw_lite.agent.compressor import ContextCompressor
    from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
    from corpclaw_lite.agent.file_state import FileStateRegistry
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.config.settings import AgentSettings, ResearchSettings, Settings, WebSettings
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
    from corpclaw_lite.memory.file_changes import FileChangeDAO
    from corpclaw_lite.memory.sqlite import SQLiteMemory
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)


@dataclass
class AgentStack:
    """Assembled agent pipeline — all components ready to serve requests."""

    loop: AgentLoop
    user_manager: UserManager
    tool_registry: ToolRegistry
    full_tool_registry: ToolRegistry | None
    mcp_manager: MCPManager | None
    container_manager: ContainerManager | None
    few_shots: list[dict[str, Any]] | None = None
    subagent_registry: SubagentRegistry | None = None
    skill_registry: SkillRegistry | None = None
    plugin_registry: PluginRegistry | None = None
    skill_matcher: SkillMatcher | None = None
    chat_context_store: ChatContextStore | None = None


def _build_router(settings: Settings | None = None) -> Provider:
    """Build an LLMRouter from ProviderRegistry.

    Args:
        settings: Pre-loaded Settings. If None, loads from config/settings.yaml.
    """
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.providers import ProviderRegistry
    from corpclaw_lite.llm.presets import PresetRegistry
    from corpclaw_lite.llm.router import LLMRouter

    if settings is None:
        settings = load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    provider_registry = ProviderRegistry.from_env()

    if not provider_registry.list_all():
        raise RuntimeError(
            "No LLM providers configured. Set PROVIDER_* env vars in .env.\n"
            "Example:\n"
            "  PROVIDER_OLLAMA__TYPE=openai\n"
            '  PROVIDER_OLLAMA__BASE_URL="http://localhost:11434/v1"\n'
            '  PROVIDER_OLLAMA__API_KEY="ollama"'
        )

    # Load model presets (optional — file may not exist)
    presets_path = PROJECT_ROOT / "config" / "model_presets.yaml"
    preset_registry = PresetRegistry.from_yaml(presets_path) if presets_path.exists() else None

    logger.info(
        "Building LLMRouter (%d providers from env, %d routing rules)",
        len(provider_registry),
        len(settings.llm.routing),
    )
    return LLMRouter.from_settings(
        settings.llm,
        provider_registry=provider_registry,
        preset_registry=preset_registry,
    )


def _all_tool_classes() -> list[Any]:
    """Return ALL tool classes (for subagent filtering and container registry)."""
    from corpclaw_lite.extensions.tools.builtin.chart_generate import ChartGenerateTool
    from corpclaw_lite.extensions.tools.builtin.convert_format import ConvertFormatTool
    from corpclaw_lite.extensions.tools.builtin.diff_text import DiffTextTool
    from corpclaw_lite.extensions.tools.builtin.excel import NormalizeExcelTool
    from corpclaw_lite.extensions.tools.builtin.excel_inspect import ExcelInspectTool
    from corpclaw_lite.extensions.tools.builtin.excel_workbook import ExcelWorkbookTool
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
        ExcelInspectTool(),
        ExcelWorkbookTool(),
    ]


# Heavy tools only available to subagents (not exposed to main agent).
_SUBAGENT_ONLY_TOOLS = {
    "normalize_excel",
    "table_query",
    "convert_format",
    "chart_generate",
    "excel_workbook",
    "write_file",
    "edit_file",
    "exec_script",
    "diff_text",
    "pdf_reader",
}


def _main_agent_tool_classes() -> list[Any]:
    """Return tool classes for the main agent (lightweight, routing-oriented).

    Heavy data tools (table_query, normalize_excel, etc.) are excluded —
    the main agent should use ``excel_inspect`` to understand a file and
    then ``dispatch_subagent`` for actual work.
    """
    return [t for t in _all_tool_classes() if t.name not in _SUBAGENT_ONLY_TOOLS]


def _register_sandboxed_tools(
    registry: ToolRegistry,
    ipc: ContainerIPC,
    tools: list[Any] | None = None,
) -> None:
    """Register file/script tools as IPCToolProxy (execute inside container)."""
    from corpclaw_lite.container.proxy import IPCToolProxy

    tool_list = tools or _main_agent_tool_classes()
    for tool in tool_list:
        registry.register(IPCToolProxy.from_tool(tool, ipc))
        logger.debug("Registered sandboxed IPCToolProxy: %s", tool.name)


def _register_local_tools(
    registry: ToolRegistry,
    tools: list[Any] | None = None,
) -> None:
    """Register file/script tools to run directly on the host (dev/test mode)."""
    tool_list = tools or _main_agent_tool_classes()
    for tool in tool_list:
        registry.register(tool)
        logger.debug("Registered local tool (no container): %s", tool.name)


def _build_security_stack(
    settings: Settings, provider: Provider
) -> tuple[ToolGuard, PermissionChecker]:
    """Build ToolGuard + PermissionChecker from config."""
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.departments.manager import (
        DepartmentManager,
        resolve_department_files,
    )
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
    dept_paths = resolve_department_files(settings, PROJECT_ROOT)
    for index, dept_path in enumerate(dept_paths):
        if index == 0:
            # Default file: replace-mode (backward-compatible). Skip if absent.
            if dept_path.exists():
                dept_manager.load_file(dept_path)
        else:
            # Overlay files: union-merge. resolve_department_files already
            # filtered these to existing paths.
            dept_manager.load_file(dept_path, merge=True)
    return guard, PermissionChecker(dept_manager)


def _build_extensions_stack(
    settings: Settings,
    project_root: Path,
    agent_settings: AgentSettings,
    web_settings: WebSettings,
    research_settings: ResearchSettings,
    provider: Provider,
    registry: ToolRegistry,
    guard: ToolGuard,
    permission_checker: PermissionChecker,
    workspace_base: Path | None = None,
    skill_matcher: SkillMatcher | None = None,
    skill_registry: SkillRegistry | None = None,
    full_tool_registry: ToolRegistry | None = None,
) -> SubagentRegistry:
    """Register subagents, MCP, host-side tools."""
    from corpclaw_lite.agent.subagent import SubagentDispatcher
    from corpclaw_lite.agent.vision import VisionProcessor
    from corpclaw_lite.extensions.paths import resolve_dirs
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool
    from corpclaw_lite.extensions.tools.builtin.image import ReadImageTool
    from corpclaw_lite.extensions.tools.builtin.research import (
        ResearchRuntime,
        build_research_tools,
    )
    from corpclaw_lite.extensions.tools.builtin.web import WebFetchTool, WebSearchTool

    subagent_registry = SubagentRegistry()
    for subagent_dir in resolve_dirs("subagents", settings, project_root):
        if not subagent_dir.exists():
            continue
        subagent_registry.load_directory(subagent_dir)

    web_fetch_tool = WebFetchTool(web_settings)
    web_search_tool = WebSearchTool(web_settings)
    read_image_tool = ReadImageTool(
        VisionProcessor(provider, max_image_bytes=agent_settings.vision_max_image_bytes),
        workspace_base=workspace_base,
    )
    research_runtime = ResearchRuntime(research_settings, workspace_base=workspace_base)
    research_tools = build_research_tools(research_runtime, web_search_tool, web_fetch_tool)

    if subagent_registry.list_all():
        dispatcher = SubagentDispatcher(
            provider=provider,
            main_registry=full_tool_registry or registry,
            settings=agent_settings,
            tool_guard=guard,
            permission_checker=permission_checker,
            skill_matcher=skill_matcher,
            skill_registry=skill_registry,
            research_runtime=research_runtime,
            workspace_base=workspace_base,
        )
        registry.register(
            DispatchSubagentTool(
                dispatcher,
                subagent_registry,
                permission_checker=permission_checker,
            )
        )
        logger.info(
            "dispatch_subagent registered (%d subagents available)",
            len(subagent_registry.list_all()),
        )

    registry.register(web_fetch_tool)
    registry.register(read_image_tool)

    # Also register host-side tools on full_tool_registry so subagents
    # can access them when listed in allowed_tools (e.g. research-agent → web_fetch).
    if full_tool_registry is not None:
        full_tool_registry.register(web_fetch_tool)
        full_tool_registry.register(web_search_tool)
        full_tool_registry.register(read_image_tool)
        for tool in research_tools:
            full_tool_registry.register(tool)
        # B-057: universal explicit terminator for subagent inner loops.
        # Registered on full_tool_registry only (not the main registry) — the
        # main agent terminates naturally via a final answer without tool calls,
        # so it does not need submit_report. SubagentDispatcher forces this tool
        # into every isolated subagent registry regardless of allowed_tools.
        from corpclaw_lite.extensions.tools.builtin.submit_report import SubmitReportTool

        full_tool_registry.register(SubmitReportTool())

    return subagent_registry


def _build_memory_stack(
    agent_settings: AgentSettings,
    provider: Provider,
    registry: ToolRegistry,
    full_tool_registry: ToolRegistry | None = None,
) -> tuple[SQLiteMemory, MemoryConsolidator | None, ContextCompressor | None]:
    """Build memory, consolidation, compression."""
    from corpclaw_lite.agent.compressor import ContextCompressor
    from corpclaw_lite.extensions.tools.builtin.memory import MemoryRecallTool, MemoryStoreTool
    from corpclaw_lite.memory.consolidation import MemoryConsolidator
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    memory = SQLiteMemory()
    store_tool = MemoryStoreTool(memory)
    recall_tool = MemoryRecallTool(memory)

    registry.register(store_tool)
    registry.register(recall_tool)

    if full_tool_registry is not None:
        full_tool_registry.register(store_tool)
        full_tool_registry.register(recall_tool)

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


# B-040: office tools wrapped with file-change tracking.
# path_param: which kwarg holds the input path.
# tracks_output: True when the tool mutates the input file in place (so a
#   pre-write backup makes sense). False for tools that emit a *new* file —
#   only the after-snapshot is recorded there.
_OFFICE_TRACKED_TOOLS: dict[str, dict[str, Any]] = {
    "excel_workbook": {"path_param": "path", "tracks_output": True},
    "normalize_excel": {"path_param": "path", "tracks_output": False},
    "convert_format": {"path_param": "input_path", "tracks_output": False},
    "write_file": {"path_param": "path", "tracks_output": True},
    "edit_file": {"path_param": "path", "tracks_output": True},
}


def _wrap_office_tools_with_file_tracking(
    registry: ToolRegistry,
    workspace_base: Path | None,
    *,
    dao: FileChangeDAO | None = None,
    snapshot_store: FileSnapshotStore | None = None,
    file_state: FileStateRegistry | None = None,
) -> tuple[FileChangeDAO | None, FileSnapshotStore | None, FileStateRegistry | None]:
    """Wrap office tools with file-change tracking (B-040) + cross-agent
    stale-write detection (B-058).

    The DAO, snapshot store and file-state registry are created once (first
    call) and reused on subsequent calls (e.g. for full_tool_registry) so both
    registries share the same journal, backup dir and stale-write state.
    """
    from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
    from corpclaw_lite.agent.file_state import FileStateRegistry
    from corpclaw_lite.extensions.tools.file_tracked import FileTrackedTool
    from corpclaw_lite.memory.file_changes import FileChangeDAO

    if dao is None:
        dao = FileChangeDAO()
    if snapshot_store is None:
        snapshot_store = FileSnapshotStore(workspace_base=workspace_base)
    if file_state is None:
        file_state = FileStateRegistry()

    # Wire the registry so read tools (read_file/excel_inspect/pdf_reader)
    # record their reads into the shared file-state.
    registry.set_file_state(file_state)

    for tname, cfg in _OFFICE_TRACKED_TOOLS.items():
        raw = registry.get(tname)
        if raw is None or isinstance(raw, FileTrackedTool):
            continue
        with contextlib.suppress(KeyError):
            registry.unregister(tname)
        registry.register(
            FileTrackedTool(
                raw,
                dao=dao,
                snapshot_store=snapshot_store,
                file_state=file_state,
                **cfg,
            ),
            allow_replace=True,
        )
    return dao, snapshot_store, file_state


def _build_system_prompt(settings: Settings, project_root: Path) -> str | None:
    """Load bootstrap system prompt."""
    from corpclaw_lite.config.bootstrap import BootstrapLoader
    from corpclaw_lite.extensions.paths import resolve_dirs as _resolve_dirs

    bootstrap_dirs: list[str | Path] = list(_resolve_dirs("bootstrap", settings, project_root))
    bootstrap = BootstrapLoader(bootstrap_dirs)
    system_prompt = bootstrap.get_system_prompt() or None
    if system_prompt:
        logger.info(
            "Loaded system prompt from %s (%d chars)",
            bootstrap_dirs,
            len(system_prompt),
        )
    else:
        logger.warning("No bootstrap/*.md files found — using minimal default system prompt")
    return system_prompt


def _load_calibrated_tool_overrides(*registries: ToolRegistry) -> None:
    """Apply calibrated tool description overrides to all active registries."""
    overrides_path = PROJECT_ROOT / "config" / "calibrated" / "tool_overrides.yaml"
    if not overrides_path.exists():
        return
    for registry in registries:
        registry.load_overrides(overrides_path)
    logger.info("Loaded calibrated tool overrides from %s", overrides_path)


def build_agent_stack(
    settings: Settings | None = None,
    *,
    router_override: Provider | None = None,
) -> AgentStack:
    """Build and return the complete agent stack from config + env.

    Args:
        settings: Pre-loaded Settings. If None, loads from config/settings.yaml.
        router_override: A pre-built Provider (usually an LLMRouter, e.g. one
            produced by ``LLMRouter.with_overrides(...)``) to use instead of
            building one from settings. When None (default), the router is built
            from settings via ``_build_router``. Used by the eval harness and
            other callers that need a programmatically overridden router
            (D-056 PR3) without mutating YAML.
    """
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.providers import ProviderRegistry
    from corpclaw_lite.config.settings import AgentSettings
    from corpclaw_lite.container.ipc import ContainerIPC
    from corpclaw_lite.container.manager import ContainerManager
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.llm.presets import PresetRegistry

    full_settings: Settings = (
        settings
        if settings is not None
        else load_settings(PROJECT_ROOT / "config" / "settings.yaml")
    )
    container_cfg = full_settings.container
    agent_settings = full_settings.agent if full_settings.agent else AgentSettings()

    provider = (
        router_override if router_override is not None else _build_router(settings=full_settings)
    )
    # Etap 3: resolve registries for depth-mode override (Fast/Think). These are
    # independent of router_override — env providers + model_presets.yaml.
    depth_provider_registry = ProviderRegistry.from_env()
    depth_presets_path = PROJECT_ROOT / "config" / "model_presets.yaml"
    depth_preset_registry = (
        PresetRegistry.from_yaml(depth_presets_path) if depth_presets_path.exists() else None
    )
    registry = ToolRegistry()

    container_manager: ContainerManager | None = None
    workspace_base: Path | None = None
    if container_cfg.enabled:
        if not ContainerManager.is_docker_available():
            raise StartupConfigurationError(
                "Container isolation is enabled (container.enabled=true), "
                "but Docker daemon is not available.",
                hint=(
                    "Start Docker, or set container.enabled=false in config/settings.yaml "
                    "for local development without isolation."
                ),
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
        container_ipc = None
        _register_local_tools(registry)
        logger.warning(
            "Container isolation DISABLED (container.enabled=false) — "
            "file/script tools run on host. Dev mode only!"
        )

    # Build a separate registry with ALL tools for subagent filtering.
    # Subagents need access to heavy tools (table_query, normalize_excel, etc.)
    # that are NOT registered in the main agent's registry.
    full_tool_reg = ToolRegistry()
    all_tools = _all_tool_classes()
    if container_cfg.enabled and container_ipc is not None:
        _register_sandboxed_tools(full_tool_reg, container_ipc, tools=all_tools)
    else:
        _register_local_tools(full_tool_reg, tools=all_tools)

    guard, permission_checker = _build_security_stack(full_settings, provider)

    # Load extensions (skills, plugins, skill matcher) BEFORE building extensions stack
    # so that SubagentDispatcher gets access to SkillMatcher for skill injection.
    from corpclaw_lite.extensions.bootstrap import load_extensions

    skill_registry, plugin_registry, skill_matcher = load_extensions(
        full_settings,
        PROJECT_ROOT,
        registry,
        full_settings.skills,
        full_tool_registry=full_tool_reg,
    )

    subagent_registry = _build_extensions_stack(
        full_settings,
        PROJECT_ROOT,
        agent_settings,
        full_settings.web,
        full_settings.research,
        provider,
        registry,
        guard,
        permission_checker,
        workspace_base=workspace_base,
        skill_matcher=skill_matcher,
        skill_registry=skill_registry,
        full_tool_registry=full_tool_reg,
    )
    memory, consolidator, compressor = _build_memory_stack(
        agent_settings, provider, registry, full_tool_registry=full_tool_reg
    )
    file_change_dao, snapshot_store, file_state = _wrap_office_tools_with_file_tracking(
        registry, workspace_base
    )
    if full_tool_reg is not registry:
        _wrap_office_tools_with_file_tracking(
            full_tool_reg,
            workspace_base,
            dao=file_change_dao,
            snapshot_store=snapshot_store,
            file_state=file_state,
        )
    _load_calibrated_tool_overrides(registry, full_tool_reg)

    mcp_manager: MCPManager | None = None
    from corpclaw_lite.extensions.mcp.manager import MCPManager
    from corpclaw_lite.extensions.paths import resolve_dirs as _resolve_mcp_dirs

    mcp_paths: list[str | Path] = [
        p for p in _resolve_mcp_dirs("mcp", full_settings, PROJECT_ROOT) if p.exists()
    ]
    if mcp_paths:
        mcp_manager = MCPManager(config_path=mcp_paths)
        logger.info(
            "MCPManager ready (configs=%s) — callers must await connect_all()",
            ", ".join(str(p) for p in mcp_paths),
        )

    system_prompt = _build_system_prompt(full_settings, PROJECT_ROOT)

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

    # B-063 S1: per-chat full-LLM-context store. Shares memory.db with
    # SQLiteMemory; the loop writes the full message schema (tool_calls/
    # reasoning/tool-role) here on every turn so any chat can later be restored.
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore

    chat_context_store = ChatContextStore(memory.db_path)

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
            workspace_base=workspace_base,
            file_change_dao=file_change_dao,
            # Etap 3: registries + depth mapping for Fast/Think override.
            preset_registry=depth_preset_registry,
            provider_registry=depth_provider_registry,
            depth_modes=agent_settings.depth_modes,
            chat_context_store=chat_context_store,
        )
    )

    # Etap 4 audit: warn at startup if depth_modes references models not in routing.
    if agent_settings.depth_modes.fast or agent_settings.depth_modes.think:
        route_models = {r.model for r in full_settings.llm.routing}
        for depth_name, mapping in [
            ("fast", agent_settings.depth_modes.fast),
            ("think", agent_settings.depth_modes.think),
        ]:
            for model_key in mapping:
                if model_key not in route_models:
                    logger.warning(
                        "depth_modes.%s references model '%s' not found in routing rules; "
                        "depth override for this model will be a no-op.",
                        depth_name,
                        model_key,
                    )

    user_manager = UserManager()

    return AgentStack(
        loop=loop,
        user_manager=user_manager,
        chat_context_store=chat_context_store,
        tool_registry=registry,
        full_tool_registry=full_tool_reg,
        mcp_manager=mcp_manager,
        container_manager=container_manager,
        few_shots=few_shots,
        subagent_registry=subagent_registry,
        skill_registry=skill_registry,
        plugin_registry=plugin_registry,
        skill_matcher=skill_matcher,
    )
