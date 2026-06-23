"""Loop integration tests for PhasePolicy (D-056 PR2).

Verifies that the loop:
  - invokes the injected PhasePolicy before each LLM call,
  - sets the returned RequestOptions on the per-call contextvar for the
    duration of the call (visible to the provider),
  - tracks prev_turn_tools so the policy sees the previous turn's tool names,
  - leaves the contextvar unset when the policy returns None.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.agent.phase_policy import (
    DefaultPhasePolicy,
    PhaseContext,
    PhasePolicy,
)
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import (
    LLMResponse,
    RequestOptions,
    StreamChunk,
    ThinkingOverride,
    ToolCall,
    get_request_options,
)
from corpclaw_lite.users.models import User


class _OptionsCapturingProvider:
    """Mock provider that records the RequestOptions contextvar on each chat().

    Also accepts a scripted sequence of LLMResponses so the loop progresses
    (tool calls then a final answer).
    """

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.call_count = 0
        self.observed_options: list[RequestOptions | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        # Snapshot the per-call contextvar value at the moment of the call.
        self.observed_options.append(get_request_options())
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError()


class _RecordingPhasePolicy(PhasePolicy):
    """Records each invocation's PhaseContext and returns a scripted override."""

    def __init__(self, return_opts: RequestOptions | None) -> None:
        self.return_opts = return_opts
        self.invocations: list[PhaseContext] = []

    def options_for_phase(self, ctx: PhaseContext) -> RequestOptions | None:
        self.invocations.append(ctx)
        return self.return_opts


@pytest.fixture
def test_user() -> User:
    return User(id=1, name="Test", department="QA")


@pytest.fixture
def empty_registry() -> ToolRegistry:
    return ToolRegistry()


def _fake_tool(name: str) -> Any:
    class FakeTool:
        # match the Tool surface used by AgentLoop
        def __init__(self_inner: Any) -> None:
            self_inner.name = name
            self_inner.description = f"fake {name}"
            self_inner.params: list[Any] = []
            self_inner.terminal = False

        async def execute(self_inner: Any, **kwargs: Any) -> str:
            return f"{name} output"

    return FakeTool()


# ── PhasePolicy hook invocation ───────────────────────────────────────────────


async def test_phase_policy_invoked_before_llm_call(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """The injected PhasePolicy.options_for_phase is called before each LLM call."""
    provider = _OptionsCapturingProvider(responses=[LLMResponse(content="done")])
    policy = _RecordingPhasePolicy(return_opts=None)  # no override
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings(), phase_policy=policy))
    await loop.run(test_user, "hi")
    assert len(policy.invocations) == 1
    # No override returned → contextvar stays unset.
    assert provider.observed_options == [None]


async def test_phase_policy_override_visible_to_provider(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """A non-None RequestOptions override is set on the contextvar during chat()."""
    provider = _OptionsCapturingProvider(responses=[LLMResponse(content="done")])
    override = RequestOptions(thinking=ThinkingOverride(mode="off"))
    policy = _RecordingPhasePolicy(return_opts=override)
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings(), phase_policy=policy))
    await loop.run(test_user, "hi")
    assert provider.observed_options == [override]


async def test_phase_policy_contextvar_reset_after_call(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """After the LLM call, the per-call contextvar is reset (no leak)."""
    provider = _OptionsCapturingProvider(responses=[LLMResponse(content="done")])
    policy = _RecordingPhasePolicy(
        return_opts=RequestOptions(thinking=ThinkingOverride(mode="off"))
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings(), phase_policy=policy))
    await loop.run(test_user, "hi")
    # Outside the run, the contextvar is back to its default (None).
    assert get_request_options() is None


# ── prev_turn_tools tracking ──────────────────────────────────────────────────


async def test_prev_turn_tools_fed_to_phase_policy(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """On the 2nd LLM call, prev_tool_calls holds the previous turn's tool names."""
    # Register a fake tool the model will call on turn 1.
    empty_registry.register(_fake_tool("gather_tool"))  # type: ignore[arg-type]

    provider = _OptionsCapturingProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="gather_tool", arguments={})],
            ),
            LLMResponse(content="final answer after tool"),
        ]
    )
    policy = _RecordingPhasePolicy(return_opts=None)
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings(), phase_policy=policy))
    await loop.run(test_user, "call the tool then answer")

    assert len(policy.invocations) == 2
    # First invocation: no previous turn.
    assert policy.invocations[0].prev_tool_calls == []
    # Second invocation: the tool from turn 1 is now "previous".
    assert policy.invocations[1].prev_tool_calls == ["gather_tool"]


# ── DefaultPhasePolicy wired by default ───────────────────────────────────────


async def test_default_phase_policy_used_when_none_injected(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """When AgentConfig.phase_policy is None, DefaultPhasePolicy is constructed.

    For a main agent in its default phase, the policy returns None (no override),
    so the contextvar stays unset.
    """
    provider = _OptionsCapturingProvider(responses=[LLMResponse(content="done")])
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    # Default policy instance is on the loop.
    assert isinstance(loop._phase_policy, DefaultPhasePolicy)
    await loop.run(test_user, "hi")
    # Main agent default phase → no override.
    assert provider.observed_options == [None]
