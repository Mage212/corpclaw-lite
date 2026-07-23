"""S2-01: runtime tool metadata must not cross extension boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.extensions.mcp.adapter import MCPToolAdapter
from corpclaw_lite.extensions.mcp.client import MCPClient, MCPToolDef
from corpclaw_lite.extensions.plugins.sandbox_proxy import PluginToolProxy
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.extensions.tools.context import get_tool_execution_context
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User


class _ContextCaptureTool(Tool):
    name = "context_capture"
    description = "Capture task-local metadata"
    params = [ToolParam(name="value", type="string", description="value")]

    def __init__(self) -> None:
        self.seen_kwargs: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.seen_kwargs.append(dict(kwargs))
        first = get_tool_execution_context()
        assert first is not None
        await asyncio.sleep(0)
        second = get_tool_execution_context()
        assert second is first
        assert second.user is not None
        return f"{second.user.id}:{second.run_id}:{kwargs['value']}"


@pytest.mark.asyncio
async def test_registry_context_is_immutable_and_isolated_between_concurrent_calls() -> None:
    registry = ToolRegistry()
    tool = _ContextCaptureTool()
    registry.register(tool)
    alice = User(id=1, name="Alice", department="engineering")
    bob = User(id=2, name="Bob", department="hr")

    first, second = await asyncio.gather(
        registry.execute("context_capture", {"value": "a"}, user=alice, run_id="run-a"),
        registry.execute("context_capture", {"value": "b"}, user=bob, run_id="run-b"),
    )

    assert {first, second} == {"1:run-a:a", "2:run-b:b"}
    assert tool.seen_kwargs == [{"value": "a"}, {"value": "b"}]
    assert get_tool_execution_context() is None

    context_holder: list[Any] = []

    class _FrozenCheck(Tool):
        name = "frozen_check"
        description = "Capture context"
        params: list[ToolParam] = []

        async def execute(self, **kwargs: Any) -> str:
            context_holder.append(get_tool_execution_context())
            return "ok"

    registry.register(_FrozenCheck())
    await registry.execute("frozen_check", {}, user=alice, run_id="immutable")
    with pytest.raises(FrozenInstanceError):
        context_holder[0].run_id = "changed"


@pytest.mark.asyncio
async def test_registry_resets_context_when_tool_raises() -> None:
    class _BrokenTool(Tool):
        name = "broken"
        description = "raises"
        params: list[ToolParam] = []

        async def execute(self, **kwargs: Any) -> str:
            assert get_tool_execution_context() is not None
            raise RuntimeError("boom")

    registry = ToolRegistry()
    registry.register(_BrokenTool())

    result = await registry.execute("broken", {}, run_id="failure")

    assert "RuntimeError: boom" in result
    assert get_tool_execution_context() is None


@pytest.mark.asyncio
async def test_registry_rbac_denial_happens_before_context_binding() -> None:
    tool = _ContextCaptureTool()
    registry = ToolRegistry()
    registry.register(tool)
    user = User(id=7, name="Denied", department="restricted")
    checker = MagicMock()
    checker.can_use_registered_tool.return_value = False

    result = await registry.execute(
        tool.name,
        {"value": "secret"},
        user=user,
        permission_checker=checker,
    )

    assert "Permission denied" in result
    assert tool.seen_kwargs == []
    assert get_tool_execution_context() is None
    checker.can_use_registered_tool.assert_called_once_with(
        user,
        tool,
        enforce_tool_allowlist=True,
    )


@pytest.mark.asyncio
async def test_mcp_receives_only_business_arguments() -> None:
    client = MagicMock(spec=MCPClient)
    client.call_tool = AsyncMock(return_value="ok")
    adapter = MCPToolAdapter(
        MCPToolDef(
            name="mcp_echo",
            description="echo",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        client,
    )
    registry = ToolRegistry()
    registry.register(adapter)
    user = User(id=9, name="MCP", department="engineering")

    result = await registry.execute(
        "mcp_echo",
        {"text": "hello"},
        user=user,
        run_id="mcp-run",
        on_subagent_tool_start=lambda _agent, _tool: None,
        parent_trajectory_recorder=object(),
    )

    assert result == "ok"
    client.call_tool.assert_awaited_once_with("mcp_echo", {"text": "hello"})


@pytest.mark.asyncio
async def test_plugin_subprocess_receives_only_business_arguments(tmp_path: Path) -> None:
    tool_path = tmp_path / "plugin_tool.py"
    tool_path.write_text(
        "from corpclaw_lite.extensions.tools.base import Tool, ToolParam\n"
        "class EchoKwargs(Tool):\n"
        "    name = 'plugin_echo'\n"
        "    description = 'echo kwargs'\n"
        "    params = [ToolParam(name='text', type='string', description='text')]\n"
        "    async def execute(self, **kwargs):\n"
        "        import json\n"
        "        return json.dumps(kwargs, sort_keys=True)\n",
        encoding="utf-8",
    )
    proxy = PluginToolProxy(
        name="plugin_echo",
        description="echo kwargs",
        params=[ToolParam(name="text", type="string", description="text")],
        risk_level=RiskLevel.LOW,
        tool_path=tool_path,
    )
    registry = ToolRegistry()
    registry.register(proxy)
    user = User(id=10, name="Plugin", department="engineering")

    try:
        result = await registry.execute(
            "plugin_echo",
            {"text": "hello"},
            user=user,
            run_id="plugin-run",
            on_subagent_tool_batch_start=lambda _agent, _tools: None,
            parent_trajectory_recorder=object(),
        )
    finally:
        await proxy.kill()

    assert result == '{"text": "hello"}'
