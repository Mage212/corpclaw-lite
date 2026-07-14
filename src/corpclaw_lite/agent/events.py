"""Agent run status events + EventSink (B-079 / Sprint 2A.3).

Channels historically passed 5–8 ``on_*`` callbacks into :meth:`AgentLoop.run`.
They are now funneled through a single :class:`EventSink`. Existing kwargs are
wrapped by :class:`CallbackEventSink` for back-compat (web/telegram unchanged).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from corpclaw_lite.llm.queue import LLMQueueStatus

__all__ = [
    "AgentEvent",
    "CallbackEventSink",
    "EventSink",
    "LlmQueueStatusEvent",
    "LlmStageEvent",
    "NullEventSink",
    "SubagentLlmQueueStatusEvent",
    "SubagentLlmStageEvent",
    "SubagentToolBatchStartEvent",
    "SubagentToolStartEvent",
    "ToolBatchStartEvent",
    "ToolStartEvent",
    "callback_event_sink_from_kwargs",
]


@dataclass(frozen=True, slots=True)
class ToolStartEvent:
    """Main-agent tool about to execute."""

    name: str


@dataclass(frozen=True, slots=True)
class ToolBatchStartEvent:
    """Parallel tool batch about to execute."""

    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LlmStageEvent:
    """LLM stream/status stage (model_waiting, model_preparing, stalled, …)."""

    stage: str


@dataclass(frozen=True, slots=True)
class LlmQueueStatusEvent:
    """Main-agent LLM queue position update."""

    status: LLMQueueStatus


@dataclass(frozen=True, slots=True)
class SubagentToolStartEvent:
    subagent_id: str
    tool: str


@dataclass(frozen=True, slots=True)
class SubagentToolBatchStartEvent:
    subagent_id: str
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubagentLlmStageEvent:
    subagent_id: str
    stage: str


@dataclass(frozen=True, slots=True)
class SubagentLlmQueueStatusEvent:
    subagent_id: str
    status: LLMQueueStatus


AgentEvent = (
    ToolStartEvent
    | ToolBatchStartEvent
    | LlmStageEvent
    | LlmQueueStatusEvent
    | SubagentToolStartEvent
    | SubagentToolBatchStartEvent
    | SubagentLlmStageEvent
    | SubagentLlmQueueStatusEvent
)


@runtime_checkable
class EventSink(Protocol):
    """Receives status events during an agent run (B-079)."""

    def emit(self, event: AgentEvent) -> None:
        """Handle one status event. Must not raise into the agent loop."""
        ...


class NullEventSink:
    """No-op sink for headless / tests."""

    def emit(self, event: AgentEvent) -> None:
        return


class CallbackEventSink:
    """Back-compat: dispatches events to legacy ``on_*`` callables."""

    def __init__(
        self,
        *,
        on_tool_start: Callable[[str], None] | None = None,
        on_tool_batch_start: Callable[[list[str]], None] | None = None,
        on_llm_stage: Callable[[str], None] | None = None,
        on_llm_queue_status: Callable[[LLMQueueStatus], None] | None = None,
        on_subagent_tool_start: Callable[[str, str], None] | None = None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
        on_subagent_llm_stage: Callable[[str, str], None] | None = None,
        on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None,
    ) -> None:
        self._on_tool_start = on_tool_start
        self._on_tool_batch_start = on_tool_batch_start
        self._on_llm_stage = on_llm_stage
        self._on_llm_queue_status = on_llm_queue_status
        self._on_subagent_tool_start = on_subagent_tool_start
        self._on_subagent_tool_batch_start = on_subagent_tool_batch_start
        self._on_subagent_llm_stage = on_subagent_llm_stage
        self._on_subagent_llm_queue_status = on_subagent_llm_queue_status

    def emit(self, event: AgentEvent) -> None:
        if isinstance(event, ToolStartEvent):
            if self._on_tool_start is not None:
                self._on_tool_start(event.name)
            return
        if isinstance(event, ToolBatchStartEvent):
            if self._on_tool_batch_start is not None:
                self._on_tool_batch_start(list(event.names))
            return
        if isinstance(event, LlmStageEvent):
            if self._on_llm_stage is not None:
                self._on_llm_stage(event.stage)
            return
        if isinstance(event, LlmQueueStatusEvent):
            if self._on_llm_queue_status is not None:
                self._on_llm_queue_status(event.status)
            return
        if isinstance(event, SubagentToolStartEvent):
            if self._on_subagent_tool_start is not None:
                self._on_subagent_tool_start(event.subagent_id, event.tool)
            return
        if isinstance(event, SubagentToolBatchStartEvent):
            if self._on_subagent_tool_batch_start is not None:
                self._on_subagent_tool_batch_start(event.subagent_id, list(event.tools))
            return
        if isinstance(event, SubagentLlmStageEvent):
            if self._on_subagent_llm_stage is not None:
                self._on_subagent_llm_stage(event.subagent_id, event.stage)
            return
        # SubagentLlmQueueStatusEvent (exhaustion of AgentEvent union)
        if self._on_subagent_llm_queue_status is not None:
            self._on_subagent_llm_queue_status(event.subagent_id, event.status)


def callback_event_sink_from_kwargs(
    *,
    on_tool_start: Callable[[str], None] | None = None,
    on_tool_batch_start: Callable[[list[str]], None] | None = None,
    on_llm_stage: Callable[[str], None] | None = None,
    on_llm_queue_status: Callable[[LLMQueueStatus], None] | None = None,
    on_subagent_tool_start: Callable[[str, str], None] | None = None,
    on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
    on_subagent_llm_stage: Callable[[str, str], None] | None = None,
    on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None,
) -> CallbackEventSink:
    """Build a :class:`CallbackEventSink` from legacy ``run()`` kwargs."""
    return CallbackEventSink(
        on_tool_start=on_tool_start,
        on_tool_batch_start=on_tool_batch_start,
        on_llm_stage=on_llm_stage,
        on_llm_queue_status=on_llm_queue_status,
        on_subagent_tool_start=on_subagent_tool_start,
        on_subagent_tool_batch_start=on_subagent_tool_batch_start,
        on_subagent_llm_stage=on_subagent_llm_stage,
        on_subagent_llm_queue_status=on_subagent_llm_queue_status,
    )


def sink_to_registry_callbacks(
    sink: EventSink,
) -> dict[str, Callable[..., None]]:
    """Adapters for :meth:`ToolRegistry.execute` subagent status kwargs."""

    def on_subagent_tool_start(subagent_id: str, tool: str) -> None:
        sink.emit(SubagentToolStartEvent(subagent_id=subagent_id, tool=tool))

    def on_subagent_tool_batch_start(subagent_id: str, tools: list[str]) -> None:
        sink.emit(SubagentToolBatchStartEvent(subagent_id=subagent_id, tools=tuple(tools)))

    def on_subagent_llm_stage(subagent_id: str, stage: str) -> None:
        sink.emit(SubagentLlmStageEvent(subagent_id=subagent_id, stage=stage))

    def on_subagent_llm_queue_status(subagent_id: str, status: LLMQueueStatus) -> None:
        sink.emit(SubagentLlmQueueStatusEvent(subagent_id=subagent_id, status=status))

    return {
        "on_subagent_tool_start": on_subagent_tool_start,
        "on_subagent_tool_batch_start": on_subagent_tool_batch_start,
        "on_subagent_llm_stage": on_subagent_llm_stage,
        "on_subagent_llm_queue_status": on_subagent_llm_queue_status,
    }
