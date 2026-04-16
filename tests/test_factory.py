"""Tests for agent stack factory — covers build_agent_stack() wiring."""

from __future__ import annotations

import os
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

    Factory tests run without Docker. Container integration is tested separately
    in test_container_proxy.py and test_container_manager.py.
    """
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import ContainerSettings, Settings

    _original_load = config_loader.load_settings

    def _mock_load(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=False)
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
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "exec_script" in tool_names
    assert "web_fetch" in tool_names
    assert "read_image" in tool_names
    assert "memory_store" in tool_names
    assert "memory_recall" in tool_names
    assert "normalize_excel" in tool_names

    # In dev mode (containers disabled), tools run on host (not IPCToolProxy)
    read_file_tool = registry.get("read_file")
    assert not isinstance(read_file_tool, IPCToolProxy)


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
    from corpclaw_lite.config.settings import ContainerSettings, Settings

    _original_load = config_loader.load_settings

    def _mock_load_enabled(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=True)
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
    from corpclaw_lite.config.settings import ContainerSettings, Settings

    _original_load = config_loader.load_settings

    def _mock_load_enabled(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original_load(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=True)
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
    exec_tool = registry.get("exec_script")
    assert isinstance(exec_tool, IPCToolProxy)

    # Host-side tools should NOT be proxied
    web_fetch = registry.get("web_fetch")
    assert not isinstance(web_fetch, IPCToolProxy)
