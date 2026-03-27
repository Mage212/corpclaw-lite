"""Tests for agent stack factory — covers build_agent_stack() wiring."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.manager import UserManager


@pytest.fixture(autouse=True)
def _clean_env() -> None:  # type: ignore[misc]
    """Ensure no stale env vars leak between tests."""
    env_keys = ["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "OPENAI_BASE_URL", "OPENAI_MODEL"]
    with patch.dict(os.environ, {}, clear=False):
        for k in env_keys:
            os.environ.pop(k, None)
        yield  # type: ignore[misc]


def test_build_agent_stack_local_provider() -> None:
    """Default config (no ANTHROPIC_API_KEY) should create OpenAIProvider."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(
        os.environ,
        {"OPENAI_BASE_URL": "http://test:11434/v1", "OPENAI_MODEL": "test-model"},
        clear=False,
    ):
        # Remove anthropic key if present
        os.environ.pop("ANTHROPIC_API_KEY", None)
        loop, user_manager, registry = build_agent_stack()

    assert isinstance(loop, AgentLoop)
    assert isinstance(user_manager, UserManager)
    assert isinstance(registry, ToolRegistry)

    # Check builtin tools are registered
    tool_names = [t.name for t in registry.list_all()]
    assert "read_file" in tool_names
    assert "write_file" in tool_names
    assert "exec_script" in tool_names
    assert "web_fetch" in tool_names
    assert "read_image" in tool_names
    assert "memory_store" in tool_names
    assert "memory_recall" in tool_names
    assert "normalize_excel" in tool_names


def test_build_agent_stack_anthropic_provider() -> None:
    """With ANTHROPIC_API_KEY set, should create AnthropicProvider."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-ant-test", "ANTHROPIC_MODEL": "claude-3-haiku-20240307"},
        clear=False,
    ):
        loop, _, _ = build_agent_stack()

    assert isinstance(loop, AgentLoop)
    # Provider is internal, but we can verify the loop was created successfully
    assert loop._provider is not None


def test_compressor_enabled_by_default() -> None:
    """ContextCompressor should be wired when compression.enabled=True (default)."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://test:11434/v1"}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        loop, _, _ = build_agent_stack()

    # Compressor should be set (default CompressionSettings.enabled=True)
    assert loop._compressor is not None


def test_consolidator_enabled_by_default() -> None:
    """MemoryConsolidator should be wired when consolidation_enabled=True (default)."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://test:11434/v1"}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        loop, _, _ = build_agent_stack()

    # Consolidator should be set (default AgentSettings.consolidation_enabled=True)
    assert loop._consolidator is not None


def test_tool_guard_loaded() -> None:
    """ToolGuard should be created (may or may not have rules loaded)."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://test:11434/v1"}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        loop, _, _ = build_agent_stack()

    assert loop._tool_guard is not None


def test_memory_wired() -> None:
    """SQLiteMemory should be wired into the loop."""
    from corpclaw_lite.agent.factory import build_agent_stack

    with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://test:11434/v1"}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        loop, _, _ = build_agent_stack()

    assert loop.memory is not None
