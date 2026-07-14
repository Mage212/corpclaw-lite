"""B-078 / 2A.2: isolated tests for auto_finalize_cascade (no full AgentLoop)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from corpclaw_lite.agent.adaptations.finalize import (
    auto_finalize_cascade,
    resolve_target_provider,
)
from corpclaw_lite.llm.base import LLMResponse, Provider, ToolCall


class _MockProvider(Provider):
    def __init__(self, response: LLMResponse | None = None, hang: bool = False) -> None:
        self.response = response or LLMResponse(content="fallback text")
        self.hang = hang
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        self.calls += 1
        if self.hang:
            await asyncio.sleep(3600)
        return self.response

    def stream(self, messages, tools=None, system=None):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _TerminalTool:
    name = "research_finalize"
    description = "finalize"
    params: list[Any] = []
    terminal = True
    parallel_safe = True

    def __init__(self) -> None:
        self.executed_with: dict[str, Any] | None = None

    async def execute(self, **kwargs: Any) -> str:
        self.executed_with = kwargs
        answer = kwargs.get("answer", "")
        return f"## Report\n{answer}" if answer else "## Report\nempty"


def _registry(tool: _TerminalTool) -> MagicMock:
    reg = MagicMock()
    reg.to_schemas.return_value = [
        {
            "type": "function",
            "function": {
                "name": "research_finalize",
                "description": "finalize",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    reg.get.return_value = tool
    return reg


def _context() -> MagicMock:
    ctx = MagicMock()
    ctx.messages = [{"role": "user", "content": "task"}]
    ctx.system_prompt = "sys"
    ctx.add_user_message = MagicMock()
    return ctx


def _stats() -> MagicMock:
    st = MagicMock()
    st.run_id = "run-f"
    st.iterations = 3
    return st


@pytest.mark.asyncio
async def test_stage_b_model_calls_terminal() -> None:
    tool = _TerminalTool()
    reg = _registry(tool)
    provider = _MockProvider(
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="1", name="research_finalize", arguments={"answer": "from B"})],
        )
    )
    executed: list[ToolCall] = []

    async def exec_tc(tc: ToolCall, user: Any, stats: Any) -> str:
        executed.append(tc)
        return "executed-B"

    async def call_llm(p: Provider, **kwargs: Any) -> LLMResponse:
        return await p.chat(
            messages=kwargs["messages"],
            tools=kwargs.get("tools"),
            system=kwargs.get("system"),
        )

    user = MagicMock()
    user.id = 1
    out = await auto_finalize_cascade(
        _context(),
        _stats(),
        user,
        "research_finalize",
        RuntimeError("budget"),
        registry=reg,
        provider=provider,
        llm_timeout_seconds=5.0,
        notify_position=False,
        notify_interval_seconds=30.0,
        call_llm=call_llm,
        execute_tool_call=exec_tc,
    )
    assert out == "executed-B"
    assert len(executed) == 1
    assert executed[0].name == "research_finalize"


@pytest.mark.asyncio
async def test_stage_c_programmatic_when_b_returns_text() -> None:
    tool = _TerminalTool()
    reg = _registry(tool)
    provider = _MockProvider(LLMResponse(content="partial findings"))

    async def call_llm(p: Provider, **kwargs: Any) -> LLMResponse:
        return await p.chat(
            messages=kwargs["messages"],
            tools=kwargs.get("tools"),
            system=kwargs.get("system"),
        )

    async def exec_tc(tc: ToolCall, user: Any, stats: Any) -> str:
        raise AssertionError("stage B should not execute tool when no tool_calls")

    user = MagicMock()
    user.id = 2
    out = await auto_finalize_cascade(
        _context(),
        _stats(),
        user,
        "research_finalize",
        RuntimeError("budget"),
        registry=reg,
        provider=provider,
        llm_timeout_seconds=5.0,
        notify_position=False,
        notify_interval_seconds=30.0,
        call_llm=call_llm,
        execute_tool_call=exec_tc,
    )
    assert out is not None
    assert "partial findings" in out
    assert tool.executed_with is not None
    assert tool.executed_with.get("answer") == "partial findings"


@pytest.mark.asyncio
async def test_stage_b_timeout_falls_to_stage_c() -> None:
    tool = _TerminalTool()
    reg = _registry(tool)
    provider = _MockProvider(hang=True)

    async def call_llm(p: Provider, **kwargs: Any) -> LLMResponse:
        return await p.chat(
            messages=kwargs["messages"],
            tools=kwargs.get("tools"),
            system=kwargs.get("system"),
        )

    async def exec_tc(tc: ToolCall, user: Any, stats: Any) -> str:
        raise AssertionError("should not run")

    user = MagicMock()
    user.id = 3
    out = await auto_finalize_cascade(
        _context(),
        _stats(),
        user,
        "research_finalize",
        RuntimeError("budget"),
        registry=reg,
        provider=provider,
        llm_timeout_seconds=0.05,
        notify_position=False,
        notify_interval_seconds=30.0,
        call_llm=call_llm,
        execute_tool_call=exec_tc,
    )
    assert out is not None
    assert "empty" in out or "Report" in out
    assert tool.executed_with is not None


@pytest.mark.asyncio
async def test_missing_terminal_returns_none() -> None:
    reg = MagicMock()
    reg.to_schemas.return_value = []
    reg.get.return_value = None
    provider = _MockProvider()

    async def call_llm(p: Provider, **kwargs: Any) -> LLMResponse:
        raise AssertionError("should not call LLM")

    async def exec_tc(tc: ToolCall, user: Any, stats: Any) -> str:
        raise AssertionError("should not run")

    user = MagicMock()
    user.id = 4
    out = await auto_finalize_cascade(
        _context(),
        _stats(),
        user,
        "research_finalize",
        RuntimeError("budget"),
        registry=reg,
        provider=provider,
        llm_timeout_seconds=5.0,
        notify_position=False,
        notify_interval_seconds=30.0,
        call_llm=call_llm,
        execute_tool_call=exec_tc,
    )
    assert out is None


def test_resolve_target_provider_passthrough() -> None:
    p = _MockProvider()
    assert resolve_target_provider(p) is p
