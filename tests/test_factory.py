"""Tests for agent stack factory — covers build_agent_stack() wiring."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.container.proxy import IPCToolProxy
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.manager import UserManager

# Minimal PROVIDER_* env vars for tests — single local provider
_PROVIDER_ENV = {
    "PROVIDER_OLLAMA__TYPE": "openai",
    "PROVIDER_OLLAMA__BASE_URL": "http://test:11434/v1",
    "PROVIDER_OLLAMA__API_KEY": "ollama",
}


@pytest.fixture(autouse=True)
def _clean_env() -> None:  # type: ignore[misc]
    """Ensure no stale env vars leak between tests."""
    with patch.dict(os.environ, {}, clear=False):
        # Remove all PROVIDER_* vars
        for k in list(os.environ):
            if k.startswith("PROVIDER_"):
                del os.environ[k]
        # Remove legacy vars
        for k in ["ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY"]:
            os.environ.pop(k, None)
        yield  # type: ignore[misc]


@pytest.fixture(autouse=True)
def _disable_containers() -> None:  # type: ignore[misc]
    """Disable container isolation for all factory tests.

    Also rewrites routing rules to use 'ollama' provider (matching _PROVIDER_ENV)
    since settings.yaml may reference different provider names (e.g. 'lmstudio').
    """
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import (
        ContainerSettings,
        LLMSettings,
        RoutingRule,
        Settings,
    )

    _original_load = config_loader.load_settings

    def _mock_load(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=False)
        # Rewrite all routing rules to use 'ollama' provider for test env vars
        settings.llm = LLMSettings(
            routing=[
                RoutingRule(
                    task_kind="default",
                    provider="ollama",
                    model="test-model",
                ),
            ]
        )
        return settings

    with patch.object(config_loader, "load_settings", side_effect=_mock_load):
        yield  # type: ignore[misc]


def test_build_agent_stack_local_provider() -> None:
    """With PROVIDER_OLLAMA env vars, should create OpenAIProvider."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
        stack = build_agent_stack()
        loop, user_manager, registry, mcp_manager = (
            stack.loop,
            stack.user_manager,
            stack.tool_registry,
            stack.mcp_manager,
        )

    assert isinstance(loop, AgentLoop)
    assert isinstance(user_manager, UserManager)
    assert isinstance(registry, ToolRegistry)
    _ = mcp_manager

    # Check builtin tools are registered (local mode — not IPCToolProxy)
    tool_names = [t.name for t in registry.list_all()]
    # Main agent has lightweight inspection/routing tools
    assert "read_file" in tool_names
    assert "list_files" in tool_names
    assert "search_files" in tool_names
    assert "excel_inspect" in tool_names
    # Host-side tools registered separately
    assert "web_fetch" in tool_names
    assert "web_search" not in tool_names
    assert "read_image" in tool_names
    assert "memory_store" in tool_names
    assert "memory_recall" in tool_names
    assert "dispatch_subagent" in tool_names
    # Subagent-only tools NOT in main registry
    assert "write_file" not in tool_names
    assert "exec_script" not in tool_names
    assert "normalize_excel" not in tool_names
    assert "table_query" not in tool_names
    assert "excel_workbook" not in tool_names
    assert "diff_text" not in tool_names
    assert "pdf_reader" not in tool_names

    # In dev mode (containers disabled), tools run on host (not IPCToolProxy)
    read_file_tool = registry.get("read_file")
    assert not isinstance(read_file_tool, IPCToolProxy)

    # Web search is subagent-only: the main agent delegates research, while
    # research-agent keeps its internal search workflow.
    assert stack.full_tool_registry is not None
    assert stack.full_tool_registry.get("web_search") is not None
    assert stack.full_tool_registry.get("research_search") is not None
    assert stack.full_tool_registry.get("research_fetch_source") is not None


def test_build_agent_stack_anthropic_provider() -> None:
    """With PROVIDER_ANTHROPIC env vars, should create AnthropicProvider."""
    from corpclaw_lite.agent.factory import build_agent_stack
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import ContainerSettings, LLMSettings, RoutingRule, Settings

    _original_load = config_loader.load_settings

    def _mock_load(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=False)
        settings.llm = LLMSettings(
            routing=[
                RoutingRule(
                    task_kind="default",
                    provider="anthropic",
                    model="claude-3-haiku-20240307",
                ),
            ]
        )
        return settings

    anthropic_env = {
        "PROVIDER_ANTHROPIC__TYPE": "anthropic",
        "PROVIDER_ANTHROPIC__API_KEY": "sk-ant-test",
    }
    with (
        patch.object(config_loader, "load_settings", side_effect=_mock_load),
        patch.dict(os.environ, anthropic_env, clear=False),
    ):
        stack = build_agent_stack()

    assert isinstance(stack.loop, AgentLoop)
    assert stack.loop._provider is not None


def test_no_providers_raises_error() -> None:
    """Without any PROVIDER_* env vars, must raise RuntimeError."""
    from corpclaw_lite.agent.factory import build_agent_stack

    # _clean_env fixture already removes all PROVIDER_* vars
    with pytest.raises(RuntimeError, match="No LLM providers configured"):
        build_agent_stack()


def test_compressor_enabled_by_default() -> None:
    """ContextCompressor should be wired when compression.enabled=True (default)."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
        stack = build_agent_stack()

    assert stack.loop._compressor is not None


def test_consolidator_enabled_by_default() -> None:
    """MemoryConsolidator should be wired when consolidation_enabled=True (default)."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
        stack = build_agent_stack()

    assert stack.loop._consolidator is not None


def test_tool_guard_loaded() -> None:
    """ToolGuard should be created (may or may not have rules loaded)."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
        stack = build_agent_stack()

    assert stack.loop._tool_guard is not None


def test_memory_wired() -> None:
    """SQLiteMemory should be wired into the loop."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
        stack = build_agent_stack()

    assert stack.loop.memory is not None


def test_container_enabled_requires_docker() -> None:
    """When container.enabled=true and Docker is not available, must raise RuntimeError."""
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import (
        ContainerSettings,
        LLMSettings,
        RoutingRule,
        Settings,
    )

    _original_load = config_loader.load_settings

    def _mock_load_enabled(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=True)
        settings.llm = LLMSettings(
            routing=[RoutingRule(task_kind="default", provider="ollama", model="test-model")]
        )
        return settings

    from corpclaw_lite.agent.factory import build_agent_stack
    from corpclaw_lite.container.manager import ContainerManager

    with (
        patch.object(config_loader, "load_settings", side_effect=_mock_load_enabled),
        patch.object(ContainerManager, "is_docker_available", return_value=False),
        patch.dict(os.environ, _PROVIDER_ENV, clear=False),
        pytest.raises(RuntimeError, match="Docker daemon is not available"),
    ):
        build_agent_stack()


def test_container_enabled_registers_ipc_proxies() -> None:
    """When container.enabled=true and Docker is available, file tools are IPCToolProxy."""
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import (
        ContainerSettings,
        LLMSettings,
        RoutingRule,
        Settings,
    )

    _original_load = config_loader.load_settings

    def _mock_load_enabled(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=True)
        settings.llm = LLMSettings(
            routing=[RoutingRule(task_kind="default", provider="ollama", model="test-model")]
        )
        return settings

    from corpclaw_lite.agent.factory import build_agent_stack
    from corpclaw_lite.container.manager import ContainerManager

    mock_docker = MagicMock()
    mock_docker.from_env.return_value.ping.return_value = True

    provider_env = {
        **_PROVIDER_ENV,
        "CORPCLAW_IPC_SECRET": "test-secret-for-unit-test",
    }
    with (
        patch.object(config_loader, "load_settings", side_effect=_mock_load_enabled),
        patch.object(ContainerManager, "is_docker_available", return_value=True),
        patch.dict(os.environ, provider_env, clear=False),
        patch("corpclaw_lite.container.manager.docker", mock_docker),
    ):
        stack = build_agent_stack()

    # File tools should now be IPCToolProxy instances
    registry = stack.tool_registry
    read_file_tool = registry.get("read_file")
    assert isinstance(read_file_tool, IPCToolProxy)

    # exec_script is subagent-only, not in main registry
    exec_tool = registry.get("exec_script")
    assert exec_tool is None

    # Host-side tools should NOT be proxied
    web_fetch = registry.get("web_fetch")
    assert not isinstance(web_fetch, IPCToolProxy)

    # Main agent has no direct web_search; subagents still get it through the
    # full registry, and it must remain host-side because containers have no net.
    web_search = registry.get("web_search")
    assert web_search is None
    assert stack.full_tool_registry is not None
    full_web_search = stack.full_tool_registry.get("web_search")
    assert full_web_search is not None
    assert not isinstance(full_web_search, IPCToolProxy)


def test_main_agent_tool_classes_has_four_factory_tools() -> None:
    """Main agent should have exactly 4 factory tools (inspection + routing)."""
    from corpclaw_lite.agent.factory import _main_agent_tool_classes

    main_tools = {t.name for t in _main_agent_tool_classes()}
    assert main_tools == {"read_file", "list_files", "search_files", "excel_inspect"}


def test_all_tool_classes_has_full_set() -> None:
    """Full tool set should include all 14 factory tools."""
    from corpclaw_lite.agent.factory import _all_tool_classes

    all_names = {t.name for t in _all_tool_classes()}
    expected = {
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "search_files",
        "exec_script",
        "normalize_excel",
        "diff_text",
        "convert_format",
        "table_query",
        "chart_generate",
        "pdf_reader",
        "excel_inspect",
        "excel_workbook",
    }
    assert all_names == expected


def test_calibrated_tool_overrides_apply_to_both_registries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime should apply calibrated tool descriptions to main and full registries."""
    from corpclaw_lite.agent import factory
    from corpclaw_lite.extensions.tools.builtin.files import ReadFileTool

    calibrated = tmp_path / "config" / "calibrated"
    calibrated.mkdir(parents=True)
    (calibrated / "tool_overrides.yaml").write_text(
        "overrides:\n  read_file:\n    description: Calibrated read description\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(factory, "PROJECT_ROOT", tmp_path)

    main_registry = ToolRegistry()
    full_registry = ToolRegistry()
    main_registry.register(ReadFileTool())
    full_registry.register(ReadFileTool())

    factory._load_calibrated_tool_overrides(main_registry, full_registry)

    assert main_registry.to_schemas()[0]["function"]["description"] == "Calibrated read description"
    assert full_registry.to_schemas()[0]["function"]["description"] == "Calibrated read description"


def test_build_agent_stack_uses_router_override() -> None:
    """D-056 PR3: router_override is used instead of building one from settings."""
    from corpclaw_lite.agent.factory import build_agent_stack

    sentinel = MagicMock(name="override-provider")
    with patch.dict(os.environ, _PROVIDER_ENV, clear=False):
        stack = build_agent_stack(router_override=sentinel)
    # The loop's provider is the injected override, not a freshly built router.
    assert stack.loop.provider is sentinel
