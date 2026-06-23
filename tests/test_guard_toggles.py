"""B-060: guard toggle configuration via AgentSettings.

The Phase 0 guards (B-055 result-dedup, B-056 planning-text) are configurable on
:class:`~corpclaw_lite.config.settings.AgentSettings` so the eval harness can run
A/B passes with guards enabled/disabled. These tests pin the toggle behaviour:
when a guard's config has ``enabled=False``, the loop must NOT inject the guard's
recovery/correction message even when the triggering condition is present.
"""

from __future__ import annotations

from typing import Any

import pytest

from corpclaw_lite.agent.guards import (
    PlanningTextGuardConfig,
    ResultDedupGuardConfig,
)
from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import LLMResponse, Provider, ToolCall
from corpclaw_lite.users.models import User


class _MockProvider(Provider):
    """Minimal canned-response provider for loop-level tests."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


@pytest.fixture
def test_user() -> User:
    return User(id=1, name="Test", department="QA")


@pytest.fixture
def empty_registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.mark.asyncio
async def test_result_dedup_disabled_does_not_inject_instruction(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """With result_dedup_guard.enabled=False, repeated identical results must NOT
    trigger the dedup recovery hint."""

    class StaleTool:
        name = "stale_tool"
        description = ""
        params: list[Any] = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "rows: 42"

    empty_registry._tools["stale_tool"] = StaleTool()  # type: ignore

    saw_dedup_instruction: list[bool] = []

    class TrackingProvider(_MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            system: str | None = None,
        ) -> LLMResponse:
            saw_dedup_instruction.append(
                "returned the same result it returned before" in str(system or "")
            )
            return await super().chat(messages, tools, system)

    provider = TrackingProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="0", name="stale_tool", arguments={})],
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="stale_tool", arguments={})],
            ),
            LLMResponse(content="done"),
        ]
    )
    settings = AgentSettings(
        max_steps=20,
        max_tool_calls=100,
        result_dedup_guard=ResultDedupGuardConfig(enabled=False),
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "do it")

    assert result == "done"
    assert stats.status == "ok"
    assert not any(saw_dedup_instruction), (
        "dedup instruction must NOT be injected when guard is disabled"
    )


@pytest.mark.asyncio
async def test_planning_text_disabled_does_not_correct(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """With planning_text_guard.enabled=False, a planning-text final answer must
    be returned as-is (no correction turn)."""

    saw_correction: list[bool] = []

    class TrackingProvider(_MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            system: str | None = None,
        ) -> LLMResponse:
            saw_correction.append(
                any("statement of intent" in str(m.get("content", "")) for m in messages)
            )
            return await super().chat(messages, tools, system)

    provider = TrackingProvider(responses=[LLMResponse(content="Let me now check the file.")])
    settings = AgentSettings(
        max_steps=10,
        max_tool_calls=20,
        planning_text_guard=PlanningTextGuardConfig(enabled=False),
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "check it")

    # No correction → model answer returned directly, only one LLM call.
    assert result == "Let me now check the file."
    assert provider.call_count == 1
    assert not any(saw_correction), (
        "planning-text correction must NOT be injected when guard is disabled"
    )


def test_agent_settings_default_guards_enabled() -> None:
    """Default AgentSettings has both guards enabled with original thresholds,
    preserving pre-B-060 behaviour."""
    settings = AgentSettings()
    assert settings.result_dedup_guard.enabled is True
    assert settings.result_dedup_guard.max_repeats == 2
    assert settings.planning_text_guard.enabled is True
    assert settings.planning_text_guard.max_corrections == 2


def test_agent_settings_guards_independently_toggled() -> None:
    """Each guard can be toggled independently."""
    s = AgentSettings(
        result_dedup_guard=ResultDedupGuardConfig(enabled=False),
        planning_text_guard=PlanningTextGuardConfig(enabled=True, max_corrections=5),
    )
    assert s.result_dedup_guard.enabled is False
    assert s.planning_text_guard.enabled is True
    assert s.planning_text_guard.max_corrections == 5
