import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.agent.loop import AgentConfig, AgentLoop, RunStats
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import (
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamChunk,
    TokenUsage,
    ToolCall,
)
from corpclaw_lite.users.models import User


class MockProvider(Provider):
    def __init__(self, responses: list[LLMResponse]):
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

    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        raise NotImplementedError()


@pytest.fixture
def test_user() -> User:
    return User(id=1, name="Test", department="QA")


@pytest.fixture
def empty_registry() -> ToolRegistry:
    return ToolRegistry()


@pytest.mark.asyncio
async def test_agent_loop_basic(test_user: User, empty_registry: ToolRegistry) -> None:
    provider = MockProvider(
        responses=[
            LLMResponse(content="Hello! How can I help?"),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    res, stats = await loop.run(test_user, "Hi")
    assert res == "Hello! How can I help?"
    assert provider.call_count == 1
    assert isinstance(stats, RunStats)
    assert stats.status == "ok"
    assert stats.iterations == 1
    assert stats.duration_ms >= 0
    assert stats.run_id


@pytest.mark.asyncio
async def test_agent_loop_accepts_explicit_run_id(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    provider = MockProvider(responses=[LLMResponse(content="ok")])
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    _, stats = await loop.run(test_user, "Hi", run_id="fixed-run-id")

    assert stats.run_id == "fixed-run-id"


@pytest.mark.asyncio
async def test_agent_loop_uses_backend_streaming_when_available(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    class StreamingMockProvider(MockProvider):
        def __init__(self) -> None:
            super().__init__([LLMResponse(content="fallback")])
            self.streamed_call_count = 0

        async def chat_streamed(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            system: str | None = None,
            on_event: Any | None = None,
        ) -> LLMResponse:
            self.streamed_call_count += 1
            if on_event is not None:
                on_event(LLMStreamEvent(stage="started"))
                on_event(LLMStreamEvent(stage="answer", content_delta="Hello", content_chars=5))
                on_event(LLMStreamEvent(stage="finished", content_chars=5))
            return LLMResponse(content="Hello")

    provider = StreamingMockProvider()
    seen_stages: list[str] = []
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    res, stats = await loop.run(test_user, "Hi", on_llm_stage=seen_stages.append)

    assert res == "Hello"
    assert provider.streamed_call_count == 1
    assert provider.call_count == 0
    assert seen_stages == ["started", "answer", "finished"]
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_agent_loop_trace_and_token_stats(
    tmp_path: Path, test_user: User, empty_registry: ToolRegistry
) -> None:
    import json

    from corpclaw_lite.logging.trace import setup_trace_logging

    setup_trace_logging(tmp_path, enabled=True)
    provider = MockProvider(
        responses=[
            LLMResponse(
                content="Hello!",
                usage=TokenUsage(input_tokens=11, output_tokens=7, total_tokens=19),
            ),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    _, stats = await loop.run(test_user, "Hi", channel="test")

    records = [
        json.loads(line)
        for line in (tmp_path / "agent_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    events = [r["event"] for r in records]
    assert events == [
        "request_started",
        "context_built",
        "llm_call_started",
        "llm_call_finished",
        "request_finished",
    ]
    assert {r["run_id"] for r in records} == {stats.run_id}
    assert stats.llm_calls == 1
    assert stats.input_tokens == 11
    assert stats.output_tokens == 7
    assert stats.total_tokens == 19
    assert stats.latest_total_tokens == 19
    llm_finished = [r for r in records if r["event"] == "llm_call_finished"][0]
    assert llm_finished["total_tokens"] == 19

    setup_trace_logging(tmp_path, enabled=False)


@pytest.mark.asyncio
async def test_agent_loop_tool_call(test_user: User, empty_registry: ToolRegistry) -> None:
    # A fake tool that just returns "tool output"
    class FakeTool:
        name = "test_tool"
        description = "A fake tool"
        params = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "tool output"

    empty_registry._tools["test_tool"] = FakeTool()  # type: ignore

    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="test_tool", arguments={})]),
            LLMResponse(content="I ran the tool and it worked."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    res, stats = await loop.run(test_user, "Run test tool")
    assert res == "I ran the tool and it worked."
    assert provider.call_count == 2
    assert stats.tools_used == ["test_tool"]
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_agent_loop_trace_tool_events(
    tmp_path: Path, test_user: User, empty_registry: ToolRegistry
) -> None:
    import json

    from corpclaw_lite.logging.trace import setup_trace_logging

    class FakeTool:
        name = "test_tool"
        description = "A fake tool"
        params = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "tool output"

    empty_registry._tools["test_tool"] = FakeTool()  # type: ignore
    setup_trace_logging(tmp_path, enabled=True)
    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="test_tool", arguments={})]),
            LLMResponse(content="done"),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    _, stats = await loop.run(test_user, "Run test tool")

    records = [
        json.loads(line)
        for line in (tmp_path / "agent_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    events = [r["event"] for r in records]
    assert "tool_call_started" in events
    assert "tool_call_finished" in events
    tool_finished = [r for r in records if r["event"] == "tool_call_finished"][0]
    assert tool_finished["run_id"] == stats.run_id
    assert tool_finished["status"] == "ok"

    setup_trace_logging(tmp_path, enabled=False)


@pytest.mark.asyncio
async def test_agent_loop_budget_exceeded_returns_string(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """BudgetExceededError is caught internally — run() returns a string, not raises."""
    settings = AgentSettings(max_steps=2)

    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="no_tool", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="2", name="no_tool", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="3", name="no_tool", arguments={})]),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))

    result, stats = await loop.run(test_user, "Loop forever")
    assert isinstance(result, str)
    assert "resource limit" in result.lower() or "budget" in result.lower()
    assert provider.call_count == 2
    assert stats.status == "budget"


@pytest.mark.asyncio
async def test_history_order(test_user: User, empty_registry: ToolRegistry) -> None:
    """History messages must appear BEFORE the current user message in context."""
    from unittest.mock import MagicMock

    from corpclaw_lite.memory.sqlite import SQLiteMemory

    # Mock memory returning 2 history entries
    memory = MagicMock(spec=SQLiteMemory)
    memory.get_history.return_value = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]

    captured_messages: list[list[dict]] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):  # type: ignore[override]
            captured_messages.append(list(messages))
            return LLMResponse(content="done")

    provider = CapturingProvider(responses=[])
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings(), memory=memory))
    await loop.run(test_user, "new question")

    msgs = captured_messages[0]
    # Find positions
    roles = [(m.get("role"), m.get("content")) for m in msgs]
    user_contents = [c for r, c in roles if r == "user"]
    # "old question" must come before "new question"
    assert user_contents.index("old question") < user_contents.index("new question")


@pytest.mark.asyncio
async def test_single_assistant_message_with_tool_calls(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """When LLM returns content + tool_calls, context must have ONE assistant message."""

    class FakeTool:
        name = "t"
        description = ""
        params = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "ok"

    empty_registry._tools["t"] = FakeTool()  # type: ignore

    captured: list[list[dict]] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):  # type: ignore[override]
            captured.append(list(messages))
            return await super().chat(messages, tools, system)

    provider = CapturingProvider(
        responses=[
            LLMResponse(
                content="Let me run the tool",
                tool_calls=[ToolCall(id="1", name="t", arguments={})],
            ),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    await loop.run(test_user, "go")

    # Check the second call's messages — should have exactly ONE assistant
    # message containing both content and tool_calls
    msgs = captured[1]
    assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
    # The assistant message with tool_calls must also carry the content
    tc_msg = [m for m in assistant_msgs if m.get("tool_calls")]
    assert len(tc_msg) == 1
    assert tc_msg[0]["content"] == "Let me run the tool"


@pytest.mark.asyncio
async def test_loop_stops_on_progress_guard(test_user: User, empty_registry: ToolRegistry) -> None:
    """Progress guard loop detection must break the outer while-True loop."""

    class FakeTool:
        name = "fail_tool"
        description = ""
        params = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "Error: something went wrong"

    empty_registry._tools["fail_tool"] = FakeTool()  # type: ignore

    # Return tool calls repeatedly — progress guard should detect the loop
    provider = MockProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id=str(i), name="fail_tool", arguments={})],
            )
            for i in range(20)
        ]
    )
    settings = AgentSettings(max_steps=20, max_tool_calls=100)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "do it")

    # Should have stopped due to loop detection, not budget
    assert "loop" in result.lower() or "stuck" in result.lower()
    # Should not have used all 20 responses
    assert provider.call_count < 20
    assert stats.status == "loop"


@pytest.mark.asyncio
async def test_approval_callback_per_call_takes_priority(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """approval_callback passed to run() must override the instance-level default."""
    from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuard

    # ToolGuard rule that always raises ApprovalRequest for exec_tool
    class AlwaysApprovalGuard(ToolGuard):
        async def check(  # type: ignore[override]
            self, tool_name: str, arguments: Any, risk_level: str | None = None
        ) -> None:
            raise ApprovalRequest(action="exec_tool", details="test")

    # A fake tool so it can execute after approval
    class FakeTool:
        name = "exec_tool"
        description = ""
        params = []
        terminal = False
        risk_level = None

        async def execute(self, **kwargs: Any) -> str:
            return "executed"

    empty_registry._tools["exec_tool"] = FakeTool()  # type: ignore

    instance_called: list[bool] = []
    per_call_called: list[bool] = []

    async def instance_cb(action: str, details: str) -> bool:
        instance_called.append(True)
        return False  # would deny

    async def per_call_cb(action: str, details: str) -> bool:
        per_call_called.append(True)
        return True  # approve

    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="exec_tool", arguments={})]),
            LLMResponse(content="done"),
        ]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            AgentSettings(),
            tool_guard=AlwaysApprovalGuard(),
            approval_callback=instance_cb,
        )
    )

    result, stats = await loop.run(test_user, "run it", approval_callback=per_call_cb)

    # Per-call callback must have been used (approved → executed)
    assert per_call_called, "per-call callback was not called"
    assert not instance_called, "instance-level callback must not be called when per-call is given"
    assert result == "done"
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_agent_loop_trace_approval_denied(
    tmp_path: Path, test_user: User, empty_registry: ToolRegistry
) -> None:
    import json

    from corpclaw_lite.logging.trace import setup_trace_logging
    from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuard

    class AlwaysApprovalGuard(ToolGuard):
        async def check(  # type: ignore[override]
            self, tool_name: str, arguments: Any, risk_level: str | None = None
        ) -> None:
            raise ApprovalRequest(action="NEEDS_APPROVAL", details="approval required")

    class FakeTool:
        name = "exec_tool"
        description = ""
        params = []
        terminal = False
        risk_level = None

        async def execute(self, **kwargs: Any) -> str:
            return "executed"

    async def deny_cb(action: str, details: str) -> bool:
        return False

    empty_registry._tools["exec_tool"] = FakeTool()  # type: ignore
    setup_trace_logging(tmp_path, enabled=True)
    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="exec_tool", arguments={})]),
            LLMResponse(content="done"),
        ]
    )
    loop = AgentLoop(
        AgentConfig(provider, empty_registry, AgentSettings(), tool_guard=AlwaysApprovalGuard())
    )

    _, stats = await loop.run(test_user, "run it", approval_callback=deny_cb)

    records = [
        json.loads(line)
        for line in (tmp_path / "agent_trace.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    approvals = [r for r in records if r["event"] == "approval_finished"]
    assert approvals[0]["run_id"] == stats.run_id
    assert approvals[0]["status"] == "denied"

    setup_trace_logging(tmp_path, enabled=False)


@pytest.mark.asyncio
async def test_on_tool_start_callback_called(test_user: User, empty_registry: ToolRegistry) -> None:
    """on_tool_start callback must fire for each tool call."""

    class FakeTool:
        name = "read_file"
        description = ""
        params = []  # type: ignore[var-annotated]
        parallel_safe = False

        async def execute(self, **kwargs: Any) -> str:
            return "content"

    empty_registry._tools["read_file"] = FakeTool()  # type: ignore

    provider = MockProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="1", name="read_file", arguments={}),
                    ToolCall(id="2", name="read_file", arguments={}),
                ],
            ),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    started_tools: list[str] = []
    result, stats = await loop.run(
        test_user, "read two files", on_tool_start=lambda name: started_tools.append(name)
    )

    assert result == "Done."
    assert started_tools == ["read_file", "read_file"]
    assert stats.tools_used == ["read_file", "read_file"]


@pytest.mark.asyncio
async def test_subagent_status_callbacks_passed_to_tool_runtime_context(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Subagent status callbacks should reach runtime-aware tools."""
    captured_kwargs: dict[str, Any] = {}

    class FakeDispatchTool:
        name = "dispatch_subagent"
        description = ""
        params = []  # type: ignore[var-annotated]
        parallel_safe = False

        async def execute(self, **kwargs: Any) -> str:
            captured_kwargs.update(kwargs)
            return "subagent result"

    empty_registry._tools["dispatch_subagent"] = FakeDispatchTool()  # type: ignore

    provider = MockProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="1",
                        name="dispatch_subagent",
                        arguments={"subagent_id": "worker", "task": "work"},
                    )
                ],
            ),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    started_tools: list[str] = []

    def on_subagent_tool_start(subagent_name: str, tool_name: str) -> None:
        _ = subagent_name, tool_name

    def on_subagent_tool_batch_start(subagent_name: str, tool_names: list[str]) -> None:
        _ = subagent_name, tool_names

    def on_subagent_llm_stage(subagent_name: str, stage: str) -> None:
        _ = subagent_name, stage

    result, stats = await loop.run(
        test_user,
        "delegate",
        on_tool_start=started_tools.append,
        on_subagent_tool_start=on_subagent_tool_start,
        on_subagent_tool_batch_start=on_subagent_tool_batch_start,
        on_subagent_llm_stage=on_subagent_llm_stage,
    )

    assert result == "Done."
    assert started_tools == ["dispatch_subagent"]
    assert stats.tools_used == ["dispatch_subagent"]
    assert captured_kwargs["on_subagent_tool_start"] is on_subagent_tool_start
    assert captured_kwargs["on_subagent_tool_batch_start"] is on_subagent_tool_batch_start
    assert captured_kwargs["on_subagent_llm_stage"] is on_subagent_llm_stage


@pytest.mark.asyncio
async def test_parallel_tool_batch_callback_suppresses_individual_starts(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Parallel tool batches should emit one aggregate status update."""

    class FakeTool:
        name = "read_file"
        description = ""
        params = []  # type: ignore[var-annotated]
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "content"

    empty_registry._tools["read_file"] = FakeTool()  # type: ignore

    provider = MockProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="1", name="read_file", arguments={}),
                    ToolCall(id="2", name="read_file", arguments={}),
                ],
            ),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    started_tools: list[str] = []
    started_batches: list[list[str]] = []

    result, stats = await loop.run(
        test_user,
        "read two files",
        on_tool_start=lambda name: started_tools.append(name),
        on_tool_batch_start=lambda names: started_batches.append(list(names)),
    )

    assert result == "Done."
    assert started_tools == []
    assert started_batches == [["read_file", "read_file"]]
    assert stats.tools_used == ["read_file", "read_file"]


class TestContextBuilderPruning:
    """Tests for ContextBuilder pruning methods."""

    def test_message_count(self, test_user: User) -> None:
        ctx = ContextBuilder("system")
        assert ctx.message_count == 0  # system prompt not in messages

        ctx.add_user_message("hello")
        assert ctx.message_count == 1

    def test_prune_old_tool_results_noop_when_few_messages(self) -> None:
        ctx = ContextBuilder("system")
        ctx.add_user_message("hi")
        assert ctx.prune_old_tool_results() == 0

    def test_prune_old_tool_results_protects_tail(self) -> None:
        ctx = ContextBuilder("system")
        ctx.add_user_message("hi")
        long_result = "x" * 500
        for i in range(10):
            ctx.messages.append(
                {"role": "tool", "tool_call_id": str(i), "name": "t", "content": long_result}
            )

        pruned = ctx.prune_old_tool_results(protect_tail=3)

        assert pruned == 7
        for i in range(7):
            assert (
                ctx.messages[1 + i]["content"] == "[Old tool output cleared to save context space]"
            )
        for i in range(7, 10):
            assert ctx.messages[1 + i]["content"] == long_result

    def test_prune_old_tool_results_skips_short_content(self) -> None:
        ctx = ContextBuilder("system")
        ctx.add_user_message("hi")
        short_result = "short"
        ctx.messages.append(
            {"role": "tool", "tool_call_id": "1", "name": "t", "content": short_result}
        )
        ctx.messages.append(
            {"role": "tool", "tool_call_id": "2", "name": "t", "content": "x" * 300}
        )

        pruned = ctx.prune_old_tool_results(protect_tail=0)

        assert pruned == 1
        assert ctx.messages[1]["content"] == short_result
        assert ctx.messages[2]["content"] == "[Old tool output cleared to save context space]"


@pytest.mark.asyncio
async def test_pruning_in_loop_reduces_context(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Loop should prune old tool results when context grows large."""

    class FakeTool:
        name = "big_tool"
        description = ""
        params = []

        async def execute(self, **kwargs: Any) -> str:
            return "x" * 500

    empty_registry._tools["big_tool"] = FakeTool()

    captured_contexts: list[list[dict]] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):
            captured_contexts.append(list(messages))
            return await super().chat(messages, tools, system)

    tool_calls = [ToolCall(id=str(i), name="big_tool", arguments={}) for i in range(12)]
    provider = CapturingProvider(
        responses=[
            LLMResponse(content="", tool_calls=tool_calls),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))
    await loop.run(test_user, "run tools")

    first_call = captured_contexts[0]
    second_call = captured_contexts[1]

    assert len(first_call) < 15
    tool_results_in_second = [m for m in second_call if m.get("role") == "tool"]
    pruned_results = [
        m for m in tool_results_in_second if "[Old tool output cleared" in m.get("content", "")
    ]
    assert len(pruned_results) > 0


@pytest.mark.asyncio
async def test_parallel_loop_all_results_added(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Parallel loop detection must NOT orphan tool results.

    When the progress guard detects a loop on the N-th tool in a parallel batch,
    all results (including N+1, N+2, …) must still be added to context so that
    every tool_call in the assistant message has a matching tool result.
    """
    call_count = 0

    class LoopingTool:
        name = "loop_tool"
        description = ""
        params: list[Any] = []
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            return "Error: same error every time"

    empty_registry._tools["loop_tool"] = LoopingTool()  # type: ignore

    # 3 parallel tool calls — loop will be detected on the 1st one
    tool_calls = [ToolCall(id=str(i), name="loop_tool", arguments={}) for i in range(3)]

    captured_contexts: list[list[dict[str, Any]]] = []

    class CapturingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            captured_contexts.append(list(messages))
            return await super().chat(messages, tools, system)

    provider = CapturingProvider(
        responses=[
            LLMResponse(content="", tool_calls=tool_calls),
            LLMResponse(content="Stopped."),
        ]
    )
    settings = AgentSettings(max_steps=10, max_tool_calls=50)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "run parallel")

    # First loop detection gives LLM another chance; LLM responds with text → "ok"
    assert "stopped" in result.lower()
    # All 3 tools were actually executed
    assert call_count == 3
    assert stats.status == "ok"

    # The context sent to the first LLM call had the user message
    assert any(m.get("role") == "user" for m in captured_contexts[0])


@pytest.mark.asyncio
async def test_parallel_tool_approval_serialized(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """_approval_lock must serialize concurrent approval callbacks."""

    from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuard

    class AlwaysApprovalGuard(ToolGuard):
        async def check(  # type: ignore[override]
            self, tool_name: str, arguments: Any, risk_level: str | None = None
        ) -> None:
            raise ApprovalRequest(action=tool_name, details="test")

    class FakeTool:
        name = "exec_tool"
        description = ""
        params = []
        terminal = False
        risk_level = None
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "executed"

    empty_registry._tools["exec_tool"] = FakeTool()  # type: ignore

    concurrent_count = 0
    max_concurrent = 0

    async def approval_cb(action: str, details: str) -> bool:
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        await asyncio.sleep(0.05)
        concurrent_count -= 1
        return True

    tool_calls = [ToolCall(id=str(i), name="exec_tool", arguments={}) for i in range(3)]
    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=tool_calls),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            AgentSettings(),
            tool_guard=AlwaysApprovalGuard(),
            approval_callback=approval_cb,
        )
    )
    result, stats = await loop.run(test_user, "run parallel approvals")

    assert result == "Done."
    assert max_concurrent <= 1


@pytest.mark.asyncio
async def test_parallel_tool_one_approved_one_denied(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """When one tool is approved and another denied, results reflect both."""

    from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuard

    call_index = 0

    class SelectiveGuard(ToolGuard):
        async def check(  # type: ignore[override]
            self, tool_name: str, arguments: Any, risk_level: str | None = None
        ) -> None:
            raise ApprovalRequest(action=tool_name, details="test")

    class FakeTool:
        name = "exec_tool"
        description = ""
        params = []
        terminal = False
        risk_level = None
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "executed"

    empty_registry._tools["exec_tool"] = FakeTool()  # type: ignore

    async def selective_cb(action: str, details: str) -> bool:
        nonlocal call_index
        call_index += 1
        return call_index == 1

    tool_calls = [ToolCall(id=str(i), name="exec_tool", arguments={}) for i in range(2)]
    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=tool_calls),
            LLMResponse(content="Done."),
        ]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            AgentSettings(),
            tool_guard=SelectiveGuard(),
            approval_callback=selective_cb,
        )
    )
    result, stats = await loop.run(test_user, "run selective")

    assert result == "Done."
    assert call_index == 2, f"Expected 2 approval callbacks, got {call_index}"
    assert len(stats.tools_used) == 2, (
        f"Expected 2 tool entries (1 approved, 1 denied), got {len(stats.tools_used)}"
    )


@pytest.mark.asyncio
async def test_loop_detection_gives_second_chance(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """First loop detection injects a warning and continues; second breaks.

    P0-6: Previously loop detection immediately broke on the first occurrence,
    meaning the LLM never saw the warning. Now the LLM gets one more turn.
    """

    class FailTool:
        name = "fail_tool"
        description = ""
        params: list[Any] = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "Error: same error every time"

    empty_registry._tools["fail_tool"] = FailTool()  # type: ignore

    call_count = 0
    saw_warning: list[bool] = []

    class TrackingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            nonlocal call_count
            # Check if any message contains the loop warning
            has_warning = any("System Guard" in str(m.get("content", "")) for m in messages)
            saw_warning.append(has_warning)
            return await super().chat(messages, tools, system)

    # Enough responses: 3 errors to trigger first detection, warning injected,
    # then LLM gets another chance (responds with tool call again),
    # 3 more errors trigger second detection → hard stop
    provider = TrackingProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id=str(i), name="fail_tool", arguments={})],
            )
            for i in range(10)
        ]
    )
    settings = AgentSettings(max_steps=20, max_tool_calls=100)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "do it")

    assert stats.status == "loop"
    # The LLM saw the warning at least once before the hard stop
    assert any(saw_warning), "LLM should have seen the System Guard warning"
    # More than 3 iterations (first detection doesn't stop immediately)
    assert stats.iterations > 3
