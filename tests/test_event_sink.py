"""B-079: EventSink + CallbackEventSink dispatch."""

from __future__ import annotations

from corpclaw_lite.agent.events import (
    CallbackEventSink,
    LlmQueueStatusEvent,
    LlmStageEvent,
    NullEventSink,
    SubagentLlmStageEvent,
    SubagentToolBatchStartEvent,
    SubagentToolStartEvent,
    ToolBatchStartEvent,
    ToolStartEvent,
    callback_event_sink_from_kwargs,
    sink_to_registry_callbacks,
)
from corpclaw_lite.llm.queue import LLMQueueStatus


def test_null_event_sink_noop() -> None:
    NullEventSink().emit(ToolStartEvent(name="x"))


def test_callback_event_sink_dispatches_all() -> None:
    seen: list[str] = []

    sink = CallbackEventSink(
        on_tool_start=lambda n: seen.append(f"tool:{n}"),
        on_tool_batch_start=lambda names: seen.append(f"batch:{','.join(names)}"),
        on_llm_stage=lambda s: seen.append(f"stage:{s}"),
        on_llm_queue_status=lambda st: seen.append(f"q:{st.position}"),
        on_subagent_tool_start=lambda sid, t: seen.append(f"sub:{sid}:{t}"),
        on_subagent_tool_batch_start=lambda sid, tools: seen.append(
            f"subbatch:{sid}:{','.join(tools)}"
        ),
        on_subagent_llm_stage=lambda sid, s: seen.append(f"substage:{sid}:{s}"),
        on_subagent_llm_queue_status=lambda sid, st: seen.append(f"subq:{sid}:{st.position}"),
    )

    sink.emit(ToolStartEvent(name="read_file"))
    sink.emit(ToolBatchStartEvent(names=("a", "b")))
    sink.emit(LlmStageEvent(stage="model_waiting"))
    sink.emit(
        LlmQueueStatusEvent(
            status=LLMQueueStatus(
                user_id="u1",
                task_kind="default",
                load_class="interactive",
                position=2,
                estimated_wait_seconds=10.0,
                waiting_count=3,
                active_count=1,
                max_concurrent=4,
                wait_seconds=1.0,
            )
        )
    )
    sink.emit(SubagentToolStartEvent(subagent_id="doc", tool="write_file"))
    sink.emit(SubagentToolBatchStartEvent(subagent_id="doc", tools=("r", "w")))
    sink.emit(SubagentLlmStageEvent(subagent_id="doc", stage="thinking"))
    sink.emit(
        SubagentLlmStageEvent(subagent_id="x", stage="y")  # covered
    )

    assert "tool:read_file" in seen
    assert "batch:a,b" in seen
    assert "stage:model_waiting" in seen
    assert "q:2" in seen
    assert "sub:doc:write_file" in seen
    assert "subbatch:doc:r,w" in seen
    assert "substage:doc:thinking" in seen


def test_callback_event_sink_skips_none_handlers() -> None:
    sink = CallbackEventSink()  # all None
    sink.emit(ToolStartEvent(name="x"))  # no raise


def test_from_kwargs_and_registry_adapters() -> None:
    tools: list[tuple[str, str]] = []
    sink = callback_event_sink_from_kwargs(
        on_subagent_tool_start=lambda sid, t: tools.append((sid, t)),
    )
    cbs = sink_to_registry_callbacks(sink)
    cbs["on_subagent_tool_start"]("research-agent", "web_fetch")
    assert tools == [("research-agent", "web_fetch")]
