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
from corpclaw_lite.llm.queue import LLMQueueStatus, LLMRequestQueue
from corpclaw_lite.llm.router import LLMRouter
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
    assert seen_stages == ["model_preparing", "model_waiting", "started", "answer", "finished"]
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_agent_loop_reports_queue_then_model_waiting_status(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    provider = MockProvider(responses=[LLMResponse(content="queued response")])
    queue = LLMRequestQueue(max_concurrent=1)
    router = LLMRouter(
        providers={},
        default_provider=provider,
        default_provider_name="llamacpp",
        routing=[("default", None, provider, "llamacpp")],
        queue=queue,
    )
    holder = await queue.acquire("holder")
    queue_statuses: list[LLMQueueStatus] = []
    stages: list[str] = []
    loop = AgentLoop(AgentConfig(router, empty_registry, AgentSettings()))

    task = asyncio.create_task(
        loop.run(
            test_user,
            "Hi",
            on_llm_queue_status=queue_statuses.append,
            on_llm_stage=stages.append,
        )
    )
    for _ in range(20):
        if queue_statuses:
            break
        await asyncio.sleep(0.01)

    assert queue_statuses
    assert queue_statuses[0].position == 0
    assert stages == []

    await queue.release(holder, 0.1)
    res, stats = await task

    assert res == "queued response"
    assert stats.status == "ok"
    assert stages == ["model_preparing", "model_waiting"]


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

    def on_subagent_llm_queue_status(
        subagent_name: str,
        status: LLMQueueStatus,
    ) -> None:
        _ = subagent_name, status

    result, stats = await loop.run(
        test_user,
        "delegate",
        on_tool_start=started_tools.append,
        on_subagent_tool_start=on_subagent_tool_start,
        on_subagent_tool_batch_start=on_subagent_tool_batch_start,
        on_subagent_llm_stage=on_subagent_llm_stage,
        on_subagent_llm_queue_status=on_subagent_llm_queue_status,
    )

    assert result == "Done."
    assert started_tools == ["dispatch_subagent"]
    assert stats.tools_used == ["dispatch_subagent"]
    assert captured_kwargs["on_subagent_tool_start"] is on_subagent_tool_start
    assert captured_kwargs["on_subagent_tool_batch_start"] is on_subagent_tool_batch_start
    assert captured_kwargs["on_subagent_llm_stage"] is on_subagent_llm_stage
    assert captured_kwargs["on_subagent_llm_queue_status"] is on_subagent_llm_queue_status


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
async def test_parallel_same_batch_errors_do_not_trigger_loop(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Repeated errors in one parallel action are one failed strategy, not a loop."""
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

    tool_calls = [ToolCall(id=str(i), name="loop_tool", arguments={}) for i in range(3)]

    captured_contexts: list[list[dict[str, Any]]] = []
    captured_systems: list[str | None] = []

    class CapturingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            captured_contexts.append(list(messages))
            captured_systems.append(system if isinstance(system, str) else None)
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

    assert "stopped" in result.lower()
    assert call_count == 3
    assert stats.status == "ok"
    assert all("Internal recovery instruction" not in (system or "") for system in captured_systems)

    # The context sent to the first LLM call had the user message
    assert any(m.get("role") == "user" for m in captured_contexts[0])


@pytest.mark.asyncio
async def test_sequential_same_batch_errors_do_not_trigger_loop(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Repeated errors in one non-parallel action are one failed strategy."""
    call_count = 0

    class LoopingTool:
        name = "loop_tool"
        description = ""
        params: list[Any] = []
        parallel_safe = False

        async def execute(self, **kwargs: Any) -> str:
            nonlocal call_count
            call_count += 1
            return "Error: same error every time"

    empty_registry._tools["loop_tool"] = LoopingTool()  # type: ignore

    tool_calls = [ToolCall(id=str(i), name="loop_tool", arguments={}) for i in range(3)]
    captured_systems: list[str | None] = []

    class CapturingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            captured_systems.append(system if isinstance(system, str) else None)
            return await super().chat(messages, tools, system)

    provider = CapturingProvider(
        responses=[
            LLMResponse(content="", tool_calls=tool_calls),
            LLMResponse(content="Stopped."),
        ]
    )
    settings = AgentSettings(max_steps=10, max_tool_calls=50)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))

    result, stats = await loop.run(test_user, "run sequential")

    assert result == "Stopped."
    assert call_count == 3
    assert stats.status == "ok"
    assert all("Internal recovery instruction" not in (system or "") for system in captured_systems)


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
async def test_approval_lock_is_per_user(
    empty_registry: ToolRegistry,
) -> None:
    """Different users must reach their approval callback concurrently.

    Regression guard for the per-user approval lock (previously a single instance-level
    lock serialized ALL users' approvals — one user waiting on Approve/Deny blocked
    every other user). With per-user locks, two distinct users hit approval at once.
    """

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

    tool = FakeTool()

    def make_loop(user: User) -> AgentLoop:
        # Each user gets its own registry + provider so the two runs are independent.
        registry = ToolRegistry()
        registry._tools["exec_tool"] = tool  # type: ignore
        provider = MockProvider(
            responses=[
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id="0", name="exec_tool", arguments={})],
                ),
                LLMResponse(content="Done."),
            ]
        )
        return AgentLoop(
            AgentConfig(
                provider,
                registry,
                AgentSettings(),
                tool_guard=AlwaysApprovalGuard(),
                approval_callback=approval_cb,
            )
        )

    concurrent_count = 0
    max_concurrent = 0

    async def approval_cb(action: str, details: str) -> bool:
        nonlocal concurrent_count, max_concurrent
        concurrent_count += 1
        max_concurrent = max(max_concurrent, concurrent_count)
        await asyncio.sleep(0.05)
        concurrent_count -= 1
        return True

    user_a = User(id=1, name="A", department="QA")
    user_b = User(id=2, name="B", department="QA")

    # Run both users concurrently. With a per-user lock their approval callbacks
    # overlap; with the old single instance lock they would serialize (max_concurrent=1).
    results = await asyncio.gather(
        make_loop(user_a).run(user_a, "run"),
        make_loop(user_b).run(user_b, "run"),
    )

    assert all(result == "Done." for result, _ in results)
    assert max_concurrent >= 2


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
    """First loop detection gives a recovery turn; repeated failure then breaks.

    The recovery hint is internal system context, not assistant text that can be
    returned verbatim to the user.
    """

    class FailTool:
        name = "fail_tool"
        description = ""
        params: list[Any] = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "Error: same error every time"

    empty_registry._tools["fail_tool"] = FailTool()  # type: ignore

    saw_recovery_instruction: list[bool] = []
    saw_assistant_guard_text: list[bool] = []

    class TrackingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            has_recovery = "Internal recovery instruction" in str(system or "")
            has_guard_text = any("System Guard" in str(m.get("content", "")) for m in messages)
            saw_recovery_instruction.append(has_recovery)
            saw_assistant_guard_text.append(has_guard_text)
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
    assert any(saw_recovery_instruction), "LLM should get one recovery turn"
    assert not any(saw_assistant_guard_text), "Guard text must not be assistant-visible"
    assert stats.iterations > 3


@pytest.mark.asyncio
async def test_dedup_triggers_on_repeated_identical_tool_result(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """B-055: a tool returning the same successful result repeatedly triggers
    the result-dedup guard, which injects a recovery hint and lets the model
    try again with that context."""

    class StaleTool:
        name = "stale_tool"
        description = ""
        params: list[Any] = []
        terminal = False

        async def execute(self, **kwargs: Any) -> str:
            return "rows: 42"  # identical successful result every call

    empty_registry._tools["stale_tool"] = StaleTool()  # type: ignore

    saw_dedup_instruction: list[bool] = []

    class TrackingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            # The B-055 dedup instruction must appear in the system prompt.
            saw_dedup_instruction.append(
                "returned the same result it returned before" in str(system or "")
            )
            return await super().chat(messages, tools, system)

    # Two identical tool calls → second triggers dedup → third response is a
    # real final answer (model "recovers" and answers directly).
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
            LLMResponse(content="The answer is 42."),
        ]
    )
    settings = AgentSettings(max_steps=20, max_tool_calls=100)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "do it")

    assert result == "The answer is 42."
    assert any(saw_dedup_instruction), "B-055 dedup instruction must be injected"
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_planning_text_guard_gives_second_chance(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """B-056: a planning-text final answer ("Let me now check the file") triggers
    a correction; the model gets another turn and produces a real answer."""

    saw_correction: list[bool] = []

    class TrackingProvider(MockProvider):
        async def chat(  # type: ignore[override]
            self,
            messages: list[dict[str, Any]],
            tools: Any = None,
            system: Any = None,
        ) -> LLMResponse:
            # The B-056 correction appears as a user message in the next turn.
            saw_correction.append(
                any("statement of intent" in str(m.get("content", "")) for m in messages)
            )
            return await super().chat(messages, tools, system)

    provider = TrackingProvider(
        responses=[
            LLMResponse(content="Let me now check the file for you."),
            LLMResponse(content="The file contains 42 records."),
        ]
    )
    settings = AgentSettings(max_steps=10, max_tool_calls=20)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "check it")

    assert result == "The file contains 42 records."
    assert any(saw_correction), "B-056 correction message must be injected"
    assert provider.call_count == 2
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_planning_text_guard_tool_artifact_blocked(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """B-056: a Qwen3/Gemma tool-artifact ([tool:<name>]) as a final answer is
    blocked and the model gets another turn."""

    provider = MockProvider(
        responses=[
            LLMResponse(content="[tool:query_specific_file]"),
            LLMResponse(content="Found 3 matching rows."),
        ]
    )
    settings = AgentSettings(max_steps=10, max_tool_calls=20)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "query it")

    assert result == "Found 3 matching rows."
    assert provider.call_count == 2
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_planning_text_guard_neutral_on_long_legitimate_answer(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """B-056: a long legitimate answer starting with a planning phrase is NOT
    blocked — only short planning-text answers are."""

    long_answer = "Let me explain. " + ("Detail. " * 200)
    assert len(long_answer) >= 500

    provider = MockProvider(responses=[LLMResponse(content=long_answer)])
    settings = AgentSettings(max_steps=10, max_tool_calls=20)
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "explain")

    assert result == long_answer
    assert provider.call_count == 1
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_loop_guard_echo_is_not_returned_to_user(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Old internal guard text must be converted to a user-facing fallback."""

    provider = MockProvider(
        responses=[
            LLMResponse(
                content=(
                    "System Guard: You seem to be stuck in a loop repeating the same error. "
                    "Please change your strategy or stop using this tool."
                )
            ),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    result, stats = await loop.run(test_user, "run it")

    assert not result.startswith("System Guard:")
    assert "detected a loop" in result
    assert stats.status == "loop"
    assert stats.error == "model_echoed_loop_guard"


@pytest.mark.asyncio
async def test_agent_loop_repairs_raw_xml_final_once(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    provider = MockProvider(
        responses=[
            LLMResponse(content="<tool_call>BROKEN XML</tool_call>"),
            LLMResponse(content="safe answer"),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    result, stats = await loop.run(test_user, "run it")

    assert result == "safe answer"
    assert "<tool_call>" not in result
    assert provider.call_count == 2
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_agent_loop_raw_xml_final_falls_back_after_repair_failure(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    provider = MockProvider(
        responses=[
            LLMResponse(content="<tool_call>BROKEN XML</tool_call>"),
            LLMResponse(content="<tool_call>STILL BROKEN</tool_call>"),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    result, stats = await loop.run(test_user, "run it")

    assert "<tool_call>" not in result
    assert "could not safely parse" in result
    assert provider.call_count == 2
    assert stats.status == "error"
    assert stats.error == "malformed_xml_tool_call"


# ── B-046: soft deadline granularity (check before each LLM call) ────────────


@pytest.mark.asyncio
async def test_closing_mode_reduces_schema_before_llm_call(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """A long iteration that straddles the wall-clock soft deadline must still trigger
    closing mode: by the next LLM call the schema is reduced to terminal tools only.

    B-046 fix: previously the check ran once per iteration, so an iteration that started
    just under the deadline and ran past it would only enter closing mode the iteration
    *after* next — by which point asyncio.wait_for might cancel the whole run.
    """

    # A non-terminal tool the model keeps calling, plus a terminal one it could call
    # to finalize. Closing mode must narrow the schema to the terminal tool.
    class GatherTool:
        name = "gather"
        description = "gather"
        params = []
        terminal = False
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            # Slow each iteration so the 20ms wall-clock deadline is crossed between
            # LLM calls (not after the whole run completes).
            await asyncio.sleep(0.015)
            return "gathered"

    class FinalizeTool:
        name = "finalize"
        description = "finalize"
        params = []
        terminal = True
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "done"

    empty_registry._tools["gather"] = GatherTool()  # type: ignore[attr-defined]
    empty_registry._tools["finalize"] = FinalizeTool()  # type: ignore[attr-defined]

    # Capture the tools list passed to each chat() call so we can assert narrowing.
    captured_tools: list[list[dict[str, Any]]] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):  # type: ignore[override]
            captured_tools.append(list(tools or []))
            return await super().chat(messages, tools=tools, system=system)

    # Tiny deadline: 0.1 * 200ms = 20ms. The model keeps calling `gather`; after 20ms
    # the next chat() call must see a schema containing only `finalize`.
    settings = AgentSettings(
        max_steps=10,
        max_tool_calls=30,
        max_wall_time_ms=200,
        soft_deadline_ratio=0.1,
    )
    provider = CapturingProvider(
        responses=[
            # Several iterations that keep gathering (slow path)...
            LLMResponse(content="", tool_calls=[ToolCall(id=f"t{i}", name="gather", arguments={})])
            for i in range(6)
        ]
        + [LLMResponse(content="final answer")]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))

    result, stats = await loop.run(test_user, "research", channel="test")
    assert isinstance(result, str)

    tool_names_per_call = [
        {str(t.get("function", {}).get("name", "")) for t in tools} for tools in captured_tools
    ]
    # Early calls see both tools.
    assert "gather" in tool_names_per_call[0]
    # After the deadline (within a few calls given the 20ms window), a later call sees
    # only the terminal tool — closing mode engaged before that LLM call.
    narrowed = [names for names in tool_names_per_call if names == {"finalize"}]
    assert narrowed, f"expected a schema narrowed to {{finalize}}, got {tool_names_per_call}"


# ── B-047: workflow-finalize guard (nudge + restrict) ────────────────────────


@pytest.mark.asyncio
async def test_workflow_mandate_nudges_then_restricts(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """A research-style run that keeps gathering without finalizing is nudged toward
    research_finalize (system note) and then has its schema restricted to the finalize
    tool set as the wall-clock budget runs low.
    """
    import asyncio

    class SearchTool:
        name = "research_search"
        description = "search"
        params = []
        terminal = False
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            # Slow enough that ~8 iterations cross the 0.75 restrict ratio (150ms of a
            # 200ms window) well before the run ends.
            await asyncio.sleep(0.03)
            return "results"

    class ListFactsTool:
        name = "research_list_facts"
        description = "list facts"
        params = []
        terminal = False
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "facts"

    class FinalizeTool:
        name = "research_finalize"
        description = "finalize"
        params = []
        terminal = True
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "## Report\nfinal"

    for t in (SearchTool(), ListFactsTool(), FinalizeTool()):
        empty_registry._tools[t.name] = t  # type: ignore[attr-defined]

    captured_tools: list[list[dict[str, Any]]] = []
    captured_system: list[str] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):  # type: ignore[override]
            captured_tools.append(list(tools or []))
            captured_system.append(system or "")
            return await super().chat(messages, tools=tools, system=system)

    settings = AgentSettings(
        max_steps=12,
        max_tool_calls=40,
        max_wall_time_ms=200,
    )
    provider = CapturingProvider(
        responses=[
            LLMResponse(
                content="", tool_calls=[ToolCall(id=f"t{i}", name="research_search", arguments={})]
            )
            for i in range(8)
        ]
        + [LLMResponse(content="## Report\nfinal answer")]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            settings,
            terminal_tool="research_finalize",
            required_before_terminal=["research_list_facts"],
        )
    )

    result, stats = await loop.run(test_user, "research", channel="test")
    assert isinstance(result, str)

    # Nudge: a system note mentioning research_finalize was injected at some point.
    nudge_seen = any("research_finalize" in s and "time budget" in s for s in captured_system)
    assert nudge_seen, "expected the workflow nudge note in the system prompt"

    # Restrict: at least one later LLM call saw a schema narrowed to the finalize
    # tool set (research_list_facts + research_finalize, or just research_finalize
    # when closing-mode also engaged). The exact set depends on timing — iteration-
    # aware mandate may restrict at a different iteration than wall-clock-only.
    names_per_call = [
        {str(t.get("function", {}).get("name", "")) for t in tools} for tools in captured_tools
    ]
    full_set = {"research_search", "research_list_facts", "research_finalize"}
    restricted = [names for names in names_per_call if names != full_set and names]
    assert restricted, f"expected at least one restricted schema, got {names_per_call}"
    # Every restricted schema must contain research_finalize (the terminal tool).
    assert all("research_finalize" in names for names in restricted), (
        f"restricted schemas must contain research_finalize, got {names_per_call}"
    )


@pytest.mark.asyncio
async def test_workflow_mandate_neutral_without_terminal_tool(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Regression: the main agent (no terminal_tool configured) is NOT nudged or
    restricted — the guard is neutral when AgentConfig.terminal_tool is None/empty.
    """
    import asyncio

    class SlowTool:
        name = "slow_tool"
        description = "slow"
        params = []
        terminal = False
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            await asyncio.sleep(0.05)
            return "ok"

    empty_registry._tools["slow_tool"] = SlowTool()  # type: ignore[attr-defined]

    captured_system: list[str] = []

    class CapturingProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):  # type: ignore[override]
            captured_system.append(system or "")
            return await super().chat(messages, tools=tools, system=system)

    settings = AgentSettings(max_steps=8, max_tool_calls=20, max_wall_time_ms=100)
    provider = CapturingProvider(
        responses=[
            LLMResponse(
                content="", tool_calls=[ToolCall(id=f"t{i}", name="slow_tool", arguments={})]
            )
            for i in range(5)
        ]
        + [LLMResponse(content="done")]
    )
    # No terminal_tool → guard disabled.
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))

    await loop.run(test_user, "task", channel="test")

    # No workflow nudge note should ever appear.
    assert not any("research_finalize" in s and "time budget" in s for s in captured_system)


# ── Auto-finalize cascade (B + C) ────────────────────────────────────────────


def _finalize_registry(registry: ToolRegistry) -> None:
    """Register research_search + research_finalize stubs for auto-finalize tests."""

    class SearchTool:
        name = "research_search"
        description = "search"
        params = []
        terminal = False
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            return "results"

    class FinalizeTool:
        name = "research_finalize"
        description = "finalize"
        params = []
        terminal = True
        parallel_safe = True

        async def execute(self, **kwargs: Any) -> str:
            answer = kwargs.get("answer", "")
            return f"## Report\n{answer}" if answer else "## Report\nempty"

    for t in (SearchTool(), FinalizeTool()):
        registry._tools[t.name] = t  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_auto_finalize_stage_b_model_calls_terminal(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Budget exhausted → stage B emergency LLM call → model calls terminal → result returned.

    The model never finalized on its own (kept calling research_search), hit
    the iteration limit, then the cascade's emergency prompt made it call
    research_finalize.
    """
    _finalize_registry(empty_registry)
    settings = AgentSettings(max_steps=3, max_tool_calls=100, max_wall_time_ms=60000)

    provider = MockProvider(
        responses=[
            # 3 iterations: model loops on research_search, never finalizes.
            LLMResponse(
                content="", tool_calls=[ToolCall(id="1", name="research_search", arguments={})]
            ),
            LLMResponse(
                content="", tool_calls=[ToolCall(id="2", name="research_search", arguments={})]
            ),
            LLMResponse(
                content="", tool_calls=[ToolCall(id="3", name="research_search", arguments={})]
            ),
            # Stage B: emergency call → model cooperates, calls research_finalize.
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="4", name="research_finalize", arguments={"answer": "synthesized report"}
                    )
                ],
            ),
        ]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            settings,
            terminal_tool="research_finalize",
        )
    )
    result, stats = await loop.run(test_user, "research task", channel="test")
    assert "synthesized report" in result
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_auto_finalize_stage_c_programmatic(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Stage B returns plain text (no tool call) → stage C calls terminal
    programmatically with the model's text as the answer."""
    _finalize_registry(empty_registry)
    settings = AgentSettings(max_steps=3, max_tool_calls=100, max_wall_time_ms=60000)

    provider = MockProvider(
        responses=[
            LLMResponse(
                content="", tool_calls=[ToolCall(id="1", name="research_search", arguments={})]
            ),
            LLMResponse(
                content="", tool_calls=[ToolCall(id="2", name="research_search", arguments={})]
            ),
            LLMResponse(
                content="", tool_calls=[ToolCall(id="3", name="research_search", arguments={})]
            ),
            # Stage B: model returns text instead of calling terminal.
            LLMResponse(content="Here is what I found: the answer is 42."),
        ]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            settings,
            terminal_tool="research_finalize",
        )
    )
    result, stats = await loop.run(test_user, "research task", channel="test")
    # Stage C: the text was passed as answer to research_finalize programmatically.
    assert "the answer is 42" in result
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_auto_finalize_skipped_for_main_agent(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Main agent (no terminal_tool) → auto-finalize cascade does NOT trigger;
    returns the generic budget-exceeded message instead."""
    settings = AgentSettings(max_steps=2, max_tool_calls=100)

    provider = MockProvider(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="x", arguments={})]),
            LLMResponse(content="", tool_calls=[ToolCall(id="2", name="x", arguments={})]),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "loop", channel="test")
    assert "resource limit" in result.lower()
    assert stats.status == "budget"


@pytest.mark.asyncio
async def test_auto_finalize_skipped_if_terminal_already_called(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """If terminal was already called (terminal_called=True), no cascade — the
    run completed normally via the terminal tool before budget ran out."""
    _finalize_registry(empty_registry)
    settings = AgentSettings(max_steps=3, max_tool_calls=100, max_wall_time_ms=60000)

    provider = MockProvider(
        responses=[
            LLMResponse(
                content="", tool_calls=[ToolCall(id="1", name="research_search", arguments={})]
            ),
            # Model finalizes on iter 2 (before budget).
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="2", name="research_finalize", arguments={"answer": "done early"})
                ],
            ),
        ]
    )
    loop = AgentLoop(
        AgentConfig(
            provider,
            empty_registry,
            settings,
            terminal_tool="research_finalize",
        )
    )
    result, stats = await loop.run(test_user, "research", channel="test")
    assert "done early" in result
    assert stats.status == "ok"
    # Only 2 LLM calls (no cascade): no emergency call needed.
    assert provider.call_count == 2


# ── Degenerate empty-response retry ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_response_retry_then_recovers(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """Model returns empty content + no tool calls (degenerate stutter) →
    loop retries with a correction prompt instead of exiting immediately.
    On the second attempt the model produces a real answer."""
    settings = AgentSettings(max_steps=10, max_tool_calls=50)
    provider = MockProvider(
        responses=[
            # iter 1: normal tool call
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="x", arguments={})]),
            # iter 2: degenerate empty (no content, no tools) — triggers retry
            LLMResponse(content="", tool_calls=[]),
            # iter 3: model recovers with a real answer
            LLMResponse(content="Here is the answer."),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "task", channel="test")
    assert result == "Here is the answer."
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_empty_response_retry_exhausted(
    test_user: User, empty_registry: ToolRegistry
) -> None:
    """If the model keeps returning empty after max retries, the loop exits
    with 'Agent provided no response' (graceful degradation)."""
    settings = AgentSettings(max_steps=10, max_tool_calls=50)
    provider = MockProvider(
        responses=[
            # iter 1: tool call
            LLMResponse(content="", tool_calls=[ToolCall(id="1", name="x", arguments={})]),
            # All subsequent: empty degenerate (exceeds _EMPTY_RESPONSE_MAX_RETRIES=3)
            LLMResponse(content="", tool_calls=[]),
            LLMResponse(content="", tool_calls=[]),
            LLMResponse(content="", tool_calls=[]),
            LLMResponse(content="", tool_calls=[]),
        ]
    )
    loop = AgentLoop(AgentConfig(provider, empty_registry, settings))
    result, stats = await loop.run(test_user, "task", channel="test")
    assert result == "Agent provided no response."
    assert stats.status == "ok"


@pytest.mark.asyncio
async def test_depth_mode_contextvar_reset_on_context_build_error(
    test_user: User,
    empty_registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1 regression: if context building raises after set_call_depth_mode,
    the finally must still reset the contextvar so it never leaks into the
    next run() in the same task.

    Before the fix, set_call_depth_mode lived BEFORE the try/finally; a failure
    in build_initial (which runs between set and try) leaked the depth value.
    """
    from corpclaw_lite.agent.depth_mode import get_call_depth_mode, set_call_depth_mode

    # Start clean: no leaked depth from a prior test.
    set_call_depth_mode(None)

    def _raising_build_initial(*_args: Any, **_kwargs: Any) -> ContextBuilder:
        raise RuntimeError("simulated context-build failure")

    monkeypatch.setattr(ContextBuilder, "build_initial", _raising_build_initial)

    provider = MockProvider(responses=[LLMResponse(content="ok")])
    loop = AgentLoop(AgentConfig(provider, empty_registry, AgentSettings()))

    with pytest.raises(RuntimeError, match="simulated context-build failure"):
        await loop.run(test_user, "task", depth_mode="research", channel="test")

    # The depth contextvar MUST be reset — not still "research".
    assert get_call_depth_mode() is None


@pytest.mark.asyncio
async def test_compress_now_persists_compressed_context(
    test_user: User, empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """compress_now loads history, runs the compressor, and writes the compressed
    transcript back to memory (clear + re-add). Verified by reading history back
    and checking it matches the compressor's output."""
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    memory = SQLiteMemory(db_path=str(tmp_path / "compress.db"))
    # Seed 6 messages (> the 5-message floor).
    for i in range(6):
        await memory.add_message(test_user.memory_key(), "user", f"msg {i}")
        await memory.add_message(test_user.memory_key(), "assistant", f"reply {i}")

    class StubCompressor:
        """Returns a fixed 2-message summary regardless of input."""

        async def compress(self, messages: list[dict[str, Any]], **_: Any) -> list[dict[str, Any]]:
            return [
                {"role": "system", "content": "[Context Summary] compressed"},
                {"role": "user", "content": "latest"},
            ]

    loop = AgentLoop(
        AgentConfig(
            MockProvider(responses=[]),
            empty_registry,
            AgentSettings(),
            memory=memory,
            compressor=StubCompressor(),  # type: ignore[arg-type]
        )
    )

    ok, message = await loop.compress_now(test_user)

    assert ok is True
    assert "сжат" in message
    history = await memory.get_history(test_user.memory_key(), limit=20)
    # The compressed transcript (2 messages) replaced the seeded 12.
    assert len(history) == 2
    assert history[0]["content"] == "[Context Summary] compressed"
    assert history[1]["content"] == "latest"


@pytest.mark.asyncio
async def test_compress_now_too_few_messages_is_noop(
    test_user: User, empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """compress_now refuses when there are fewer than 5 messages (<compress floor)."""
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    memory = SQLiteMemory(db_path=str(tmp_path / "compress_few.db"))
    await memory.add_message(test_user.memory_key(), "user", "only one")

    class ExplodingCompressor:
        async def compress(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise AssertionError("compress should not be called for <5 messages")

    loop = AgentLoop(
        AgentConfig(
            MockProvider(responses=[]),
            empty_registry,
            AgentSettings(),
            memory=memory,
            compressor=ExplodingCompressor(),  # type: ignore[arg-type]
        )
    )

    ok, message = await loop.compress_now(test_user)

    assert ok is False
    assert "мало" in message
    # History untouched.
    history = await memory.get_history(test_user.memory_key(), limit=20)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_agent_loop_persists_full_context_with_session_id(
    test_user: User, empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """B-063 S1: when run() is given a session_id and a chat_context_store, the
    loop persists the full LLM-facing message schema (user / assistant+tool_calls
    / tool-result / final assistant) to the per-chat store, including tool_calls
    and reasoning — which SQLiteMemory does NOT capture."""
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    db = tmp_path / "ctx.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id=str(test_user.id), section="work")

    # Provider returns a tool_call, then a final answer.
    provider = MockProvider(
        responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})],
                reasoning="deciding to call echo",
            ),
            LLMResponse(content="Done!"),
        ]
    )
    # Register a trivial tool so the loop can execute the requested call.
    from corpclaw_lite.extensions.tools.base import Tool, ToolParam

    class EchoTool(Tool):
        name = "echo"
        description = "echo back"
        params = [ToolParam(name="text", type="string", required=True, description="t")]
        risk_level = "info"

        async def execute(self, **kwargs: Any) -> str:
            return f"echo:{kwargs.get('text')}"

    empty_registry.register(EchoTool())

    loop = AgentLoop(
        AgentConfig(provider, empty_registry, AgentSettings(), chat_context_store=store)
    )

    await loop.run(test_user, "call echo", session_id=session_id, channel="test")

    ctx = await store.list_context(session_id)
    # Expected order: user / assistant(tool_calls) / tool(result) / assistant(final)
    assert len(ctx) == 4
    assert ctx[0]["role"] == "user"
    assert ctx[0]["content"] == "call echo"
    assert ctx[1]["role"] == "assistant"
    assert ctx[1]["tool_calls"] is not None and len(ctx[1]["tool_calls"]) == 1
    assert ctx[1]["tool_calls"][0]["function"]["name"] == "echo"
    assert ctx[1]["reasoning"] == "deciding to call echo"
    assert ctx[2]["role"] == "tool"
    assert ctx[2]["tool_call_id"] == "call_1"
    assert ctx[2]["name"] == "echo"
    assert ctx[3]["role"] == "assistant"
    assert ctx[3]["content"] == "Done!"


@pytest.mark.asyncio
async def test_agent_loop_skips_context_persist_without_session_id(
    test_user: User, empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """B-063 S1: without session_id (telegram/CLI/subagent path), the loop does
    NOT write to the context store — even when a store is configured."""
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    db = tmp_path / "ctx2.db"
    WebChatStore(db)
    store = ChatContextStore(db)

    provider = MockProvider(responses=[LLMResponse(content="ok")])
    loop = AgentLoop(
        AgentConfig(provider, empty_registry, AgentSettings(), chat_context_store=store)
    )

    await loop.run(test_user, "hi", channel="test")  # no session_id

    # Nothing persisted for any session.
    assert store.has_context(1) is False


@pytest.mark.asyncio
async def test_context_target_isolates_concurrent_runs(
    empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """B-063 S1 audit: two concurrent loop.run() calls for different users on
    the SAME shared AgentLoop must not cross-write each other's session context.

    Regression for the instance-attribute bug: previously the loop stored
    session_id/user_id as instance attrs, so a concurrent run clobbered them.
    Now they live in contextvars (task-scoped), so each task sees its own.
    """
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    db = tmp_path / "iso.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    user_a = User(id=10, name="Alice", department="QA")
    user_b = User(id=20, name="Bob", department="QA")
    session_a = await ws.create_session(user_id=str(user_a.id), section="chat")
    session_b = await ws.create_session(user_id=str(user_b.id), section="chat")

    class YieldingProvider(MockProvider):
        """Forces a yield point after binding the context target, so the two
        runs interleave — exercising whether each sees its own session_id."""

        async def chat(self, messages, tools=None, system=None):
            await asyncio.sleep(0)  # yield to the other task
            return await super().chat(messages, tools, system)

    provider = YieldingProvider(responses=[LLMResponse(content="ok"), LLMResponse(content="ok")])
    loop = AgentLoop(
        AgentConfig(provider, empty_registry, AgentSettings(), chat_context_store=store)
    )

    # Run both concurrently on the SAME loop instance (as the web channel does).
    await asyncio.gather(
        loop.run(user_a, "hello from A", session_id=session_a, channel="web"),
        loop.run(user_b, "hello from B", session_id=session_b, channel="web"),
    )

    ctx_a = await store.list_context(session_a)
    ctx_b = await store.list_context(session_b)
    # Each session got exactly its own user message — no cross-contamination.
    assert any(m["content"] == "hello from A" for m in ctx_a)
    assert not any(m["content"] == "hello from B" for m in ctx_a)
    assert any(m["content"] == "hello from B" for m in ctx_b)
    assert not any(m["content"] == "hello from A" for m in ctx_b)


# --- B-063 S2: build_from_full_history + restore-on-activate ---


def test_build_from_full_history_preserves_tool_calls_and_tool_role(
    test_user: User,
) -> None:
    """build_from_full_history reconstructs tool_calls + tool-role messages
    faithfully (unlike build_initial, which drops them)."""
    full_history = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "content": "a.txt\nb.txt", "tool_call_id": "call_1", "name": "list_files"},
        {"role": "assistant", "content": "Found 2 files."},
    ]
    builder = ContextBuilder.build_from_full_history(
        test_user, "thanks", full_history, system_prompt_override="SYS"
    )
    # [user, assistant+tool_calls, tool, assistant, user(current)]
    assert len(builder.messages) == 5
    assert builder.messages[0] == {"role": "user", "content": "list files"}
    # assistant with tool_calls
    asst = builder.messages[1]
    assert asst["role"] == "assistant"
    assert "tool_calls" in asst
    assert asst["tool_calls"][0]["function"]["name"] == "list_files"
    # tool-role preserved
    tool = builder.messages[2]
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "call_1"
    assert tool["name"] == "list_files"
    assert builder.messages[3] == {"role": "assistant", "content": "Found 2 files."}
    assert builder.messages[4] == {"role": "user", "content": "thanks"}


def test_build_from_full_history_converts_arguments_json_string(test_user: User) -> None:
    """arguments arrives as a JSON string from the store; build converts to dict."""
    full_history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": '{"path":"x.txt","content":"hi"}',
                    },
                }
            ],
        },
    ]
    builder = ContextBuilder.build_from_full_history(
        test_user, "go", full_history, system_prompt_override="SYS"
    )
    asst = builder.messages[0]
    args = asst["tool_calls"][0]["function"]["arguments"]
    assert args == '{"path": "x.txt", "content": "hi"}'  # json.dumps of the parsed dict


def test_build_from_full_history_merges_system_messages(test_user: User) -> None:
    """system messages from history merge into the system prompt (not mid-context)."""
    full_history = [
        {"role": "system", "content": "Tools called in this turn: list_files"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    builder = ContextBuilder.build_from_full_history(
        test_user, "next", full_history, system_prompt_override="BASE"
    )
    assert "Execution history:" in builder.system_prompt
    assert "Tools called in this turn: list_files" in builder.system_prompt
    # No system role in messages (merged away)
    assert all(m["role"] != "system" for m in builder.messages)


@pytest.mark.asyncio
async def test_run_restores_full_context_from_store(
    test_user: User, empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """B-063 S2: run(session_id) with a populated context-store builds the context
    from the store (full tool_calls/tool-role), NOT from SQLiteMemory history."""
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore

    db = tmp_path / "restore.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id=str(test_user.id), section="work")

    # Seed the context-store with a prior turn that included a tool call.
    await store.append_context(
        session_id=session_id, user_id=str(test_user.id), role="user", content="what files?"
    )
    await store.append_context(
        session_id=session_id,
        user_id=str(test_user.id),
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "list_files", "arguments": "{}"},
            }
        ],
    )
    await store.append_context(
        session_id=session_id,
        user_id=str(test_user.id),
        role="tool",
        content="a.txt",
        tool_call_id="c1",
        name="list_files",
    )
    await store.append_context(
        session_id=session_id, user_id=str(test_user.id), role="assistant", content="Found a.txt."
    )

    # Spy provider: capture the messages the loop actually sent to the model.
    captured: list[dict[str, Any]] = []

    class SpyProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):
            captured.extend(messages)
            return await super().chat(messages, tools, system)

    provider = SpyProvider(responses=[LLMResponse(content="ok")])
    loop = AgentLoop(
        AgentConfig(provider, empty_registry, AgentSettings(), chat_context_store=store)
    )

    await loop.run(test_user, "thanks", session_id=session_id, channel="web")

    # The model should have seen the restored tool_calls + tool-role message,
    # proving the full-context path was used (not the text-only get_history path).
    roles = [m["role"] for m in captured]
    assert "tool" in roles, "tool-role message missing — full-context restore did not fire"
    asst_with_calls = [m for m in captured if m.get("tool_calls")]
    assert asst_with_calls, "assistant tool_calls missing — full-context restore did not fire"


@pytest.mark.asyncio
async def test_run_falls_back_to_memory_when_store_empty(
    test_user: User, empty_registry: ToolRegistry, tmp_path: Path
) -> None:
    """B-063 S2: when session_id is set but the context-store is empty, run()
    falls back to the SQLiteMemory history path (build_initial)."""
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.channels.web.chat_store import WebChatStore
    from corpclaw_lite.memory.sqlite import SQLiteMemory

    db = tmp_path / "fallback.db"
    ws = WebChatStore(db)
    store = ChatContextStore(db)
    session_id = await ws.create_session(user_id=str(test_user.id), section="chat")
    # Seed MEMORY (not the context store) — the fallback source.
    memory = SQLiteMemory(db_path=str(db))
    await memory.add_message(test_user.memory_key(), "user", "old question")
    await memory.add_message(test_user.memory_key(), "assistant", "old answer")

    captured: list[dict[str, Any]] = []

    class SpyProvider(MockProvider):
        async def chat(self, messages, tools=None, system=None):
            captured.extend(messages)
            return await super().chat(messages, tools, system)

    provider = SpyProvider(responses=[LLMResponse(content="ok")])
    loop = AgentLoop(
        AgentConfig(
            provider, empty_registry, AgentSettings(), memory=memory, chat_context_store=store
        )
    )

    await loop.run(test_user, "follow up", session_id=session_id, channel="web")

    # Fallback path: text history present, but NO tool-role messages (those come
    # only from the full-context path).
    roles = [m["role"] for m in captured]
    assert "tool" not in roles
    assert any(m.get("content") == "old answer" for m in captured)
