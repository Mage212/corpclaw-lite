from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.agent.guards import (
    BudgetExceededError,
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
    SimpleProgressGuard,
)
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.exceptions import ContainerIPCError, StorageError
from corpclaw_lite.extensions.tools.base import TOOL_ERROR_PREFIX
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import (
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamingProvider,
    ToolCall,
)
from corpclaw_lite.llm.router import LLMRouter
from corpclaw_lite.logging import health
from corpclaw_lite.logging.trace import get_trace_logger, log_event
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuardError
from corpclaw_lite.users.models import User

__all__ = [
    "AgentConfig",
    "AgentLoop",
    "RunStats",
]

if TYPE_CHECKING:
    from corpclaw_lite.agent.compressor import ContextCompressor
    from corpclaw_lite.calibration.trajectory import TrajectoryRecorder
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.memory.consolidation import MemoryConsolidator
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)

# Max chars to include from tool args / results in DEBUG logs
# Large files / responses are truncated to avoid flooding the log file
_LOG_TRUNCATE = 400


def _json_preview(value: Any, limit: int = _LOG_TRUNCATE) -> str:
    """Stable, scrubbed-by-trace preview input for structured trace fields."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:limit]
    except TypeError:
        return str(value)[:limit]


def _payload_hash(value: Any) -> str:
    """Short hash to correlate payloads without logging full contents."""
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _trace_payload_enabled() -> bool:
    """Return True when trace config allows payload previews beyond metadata."""
    trace_logger = get_trace_logger()
    return bool(trace_logger and trace_logger.trace_level in ("debug_preview", "full"))


def _format_tool_marker(tools_used: list[str]) -> str:
    """Compact marker for the reasoning column (audit only, not shown to model)."""
    if not tools_used:
        return "[Called tools: none]"
    seen: set[str] = set()
    unique: list[str] = []
    for t in tools_used:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return f"[Called tools: {', '.join(unique)}]"


@dataclass
class RunStats:
    """Metrics for a single AgentLoop.run() call.

    Returned alongside the final answer so callers (runner, CLI) can log
    or display execution details without reading internal state.
    """

    iterations: int = 0
    tools_used: list[str] = field(default_factory=list[str])
    duration_ms: float = 0.0
    status: Literal["ok", "budget", "loop", "timeout", "error"] = "ok"
    error: str | None = None
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latest_total_tokens: int = 0
    tool_durations_ms: dict[str, float] = field(default_factory=dict[str, float])
    llm_stream_calls: int = 0
    llm_stream_fallbacks: int = 0
    llm_stream_stalls: int = 0
    llm_stream_events: int = 0
    llm_first_event_ms: float | None = None
    llm_first_content_ms: float | None = None
    llm_first_tool_call_ms: float | None = None


@dataclass
class AgentConfig:
    """Configuration for AgentLoop — groups all constructor parameters."""

    provider: Provider
    registry: ToolRegistry
    settings: AgentSettings
    permission_checker: PermissionChecker | None = None
    enforce_tool_permissions: bool = True
    tool_guard: ToolGuard | None = None
    memory: SQLiteMemory | None = None
    approval_callback: Callable[[str, str], Awaitable[bool]] | None = None
    consolidator: MemoryConsolidator | None = None
    compressor: ContextCompressor | None = None
    default_system_prompt: str | None = None


class AgentLoop:
    """Core ReAct loop for agent execution."""

    def __init__(self, config: AgentConfig) -> None:
        self._provider = config.provider
        self._registry = config.registry
        self._settings = config.settings
        self._permission_checker = config.permission_checker
        self._enforce_tool_permissions = config.enforce_tool_permissions
        self._tool_guard = config.tool_guard
        self._memory = config.memory
        self._approval_callback = config.approval_callback
        self._consolidator = config.consolidator
        self._compressor = config.compressor
        self._default_system_prompt = config.default_system_prompt
        self._approval_lock = asyncio.Lock()

    @property
    def memory(self) -> SQLiteMemory | None:
        """Access the memory backend (if configured)."""
        return self._memory

    @property
    def provider(self) -> Provider:
        """Access the LLM provider."""
        return self._provider

    async def _call_llm_provider(
        self,
        provider: Provider,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
        run_id: str,
        iteration: int,
        on_llm_stage: Callable[[str], None] | None,
        stats: RunStats | None,
    ) -> LLMResponse:
        """Call an LLM provider, using backend streaming when available."""
        if not self._settings.llm_streaming_enabled or not isinstance(provider, StreamingProvider):
            return await provider.chat(messages=messages, tools=tools, system=system)

        health.increment("llm_stream_calls")
        if stats is not None:
            stats.llm_stream_calls += 1
        started_at = time.monotonic()
        last_activity_at = started_at
        last_stage: str | None = None
        reasoning_limit_logged = False
        event_count = 0
        content_delta_count = 0
        reasoning_delta_count = 0
        tool_call_delta_count = 0
        first_event_ms: float | None = None
        first_reasoning_ms: float | None = None
        first_content_ms: float | None = None
        first_tool_call_ms: float | None = None
        stage_counts: dict[str, int] = {}
        payload_trace_enabled = _trace_payload_enabled()

        def emit_status(stage: str) -> None:
            if self._settings.llm_stream_status_updates and on_llm_stage is not None:
                on_llm_stage(stage)

        def handle_event(event: LLMStreamEvent) -> None:
            nonlocal content_delta_count, event_count, first_content_ms, first_event_ms
            nonlocal first_reasoning_ms, first_tool_call_ms, last_activity_at, last_stage
            nonlocal reasoning_delta_count, reasoning_limit_logged, tool_call_delta_count
            now = time.monotonic()
            elapsed_ms = round((now - started_at) * 1000, 1)
            event_count += 1
            stage_counts[event.stage] = stage_counts.get(event.stage, 0) + 1
            if first_event_ms is None:
                first_event_ms = elapsed_ms
            if event.stage != "stalled":
                last_activity_at = now
            if event.reasoning_delta:
                reasoning_delta_count += 1
                if first_reasoning_ms is None:
                    first_reasoning_ms = elapsed_ms
            if event.content_delta:
                content_delta_count += 1
                if first_content_ms is None:
                    first_content_ms = elapsed_ms
            if event.tool_call_arguments_delta or event.tool_call_name:
                tool_call_delta_count += 1
                if first_tool_call_ms is None:
                    first_tool_call_ms = elapsed_ms
            if event.stage != last_stage:
                last_stage = event.stage
                emit_status(event.stage)
                log_event(
                    "llm_stream_stage",
                    run_id,
                    iteration=iteration,
                    stage=event.stage,
                    elapsed_ms=elapsed_ms,
                    content_chars=event.content_chars,
                    reasoning_chars=event.reasoning_chars,
                    tool_call_count=event.tool_call_count,
                    finish_reason=event.finish_reason,
                )
            if payload_trace_enabled and (
                event.content_delta or event.reasoning_delta or event.tool_call_arguments_delta
            ):
                log_event(
                    "llm_stream_delta",
                    run_id,
                    iteration=iteration,
                    stage=event.stage,
                    elapsed_ms=elapsed_ms,
                    content_delta_preview=event.content_delta,
                    content_delta_hash=(
                        _payload_hash(event.content_delta) if event.content_delta else ""
                    ),
                    reasoning_delta_preview=event.reasoning_delta,
                    reasoning_delta_hash=(
                        _payload_hash(event.reasoning_delta) if event.reasoning_delta else ""
                    ),
                    tool_call_id=event.tool_call_id,
                    tool_call_name=event.tool_call_name,
                    tool_call_arguments_delta_preview=event.tool_call_arguments_delta,
                    tool_call_arguments_delta_hash=(
                        _payload_hash(event.tool_call_arguments_delta)
                        if event.tool_call_arguments_delta
                        else ""
                    ),
                    content_chars=event.content_chars,
                    reasoning_chars=event.reasoning_chars,
                    tool_call_count=event.tool_call_count,
                )
            if (
                event.reasoning_chars > self._settings.llm_stream_max_reasoning_chars
                and not reasoning_limit_logged
            ):
                reasoning_limit_logged = True
                log_event(
                    "llm_stream_reasoning_over_limit",
                    run_id,
                    iteration=iteration,
                    reasoning_chars=event.reasoning_chars,
                    limit=self._settings.llm_stream_max_reasoning_chars,
                )

        async def stall_monitor() -> None:
            stall_seconds = max(1.0, self._settings.llm_stream_stall_seconds)
            last_logged_at = 0.0
            while True:
                await asyncio.sleep(stall_seconds)
                now = time.monotonic()
                if now - last_activity_at < stall_seconds:
                    continue
                if now - last_logged_at < stall_seconds:
                    continue
                last_logged_at = now
                health.increment("llm_stream_stalls")
                if stats is not None:
                    stats.llm_stream_stalls += 1
                emit_status("stalled")
                log_event(
                    "llm_stream_stalled",
                    run_id,
                    iteration=iteration,
                    current_stage=last_stage,
                    idle_seconds=round(now - last_activity_at, 1),
                    elapsed_ms=round((now - started_at) * 1000, 1),
                    event_count=event_count,
                    stage_counts=stage_counts,
                )

        monitor_task = asyncio.create_task(stall_monitor())
        log_event(
            "llm_stream_started",
            run_id,
            iteration=iteration,
            provider=type(provider).__name__,
            message_count=len(messages),
            tools_count=len(tools or []),
            system_prompt_chars=len(system or ""),
            stall_seconds=max(1.0, self._settings.llm_stream_stall_seconds),
            payload_trace_enabled=payload_trace_enabled,
        )
        try:
            response = await provider.chat_streamed(
                messages=messages,
                tools=tools,
                system=system,
                on_event=handle_event,
            )
        except Exception as e:
            health.increment("llm_stream_fallbacks")
            if stats is not None:
                stats.llm_stream_fallbacks += 1
            emit_status("fallback")
            log_event(
                "llm_stream_fallback",
                run_id,
                iteration=iteration,
                error=type(e).__name__,
                current_stage=last_stage,
                event_count=event_count,
                stage_counts=stage_counts,
                elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
            )
            logger.warning("LLM streaming failed; falling back to chat(): %s", e)
            return await provider.chat(messages=messages, tools=tools, system=system)
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

        health.increment("llm_reasoning_chars", len(response.reasoning or ""))
        health.increment("llm_content_chars", len(response.content or ""))
        if stats is not None:
            stats.llm_stream_events += event_count
            stats.llm_first_event_ms = first_event_ms
            stats.llm_first_content_ms = first_content_ms
            stats.llm_first_tool_call_ms = first_tool_call_ms
        log_event(
            "llm_stream_finished",
            run_id,
            iteration=iteration,
            duration_ms=round((time.monotonic() - started_at) * 1000, 1),
            event_count=event_count,
            content_delta_count=content_delta_count,
            reasoning_delta_count=reasoning_delta_count,
            tool_call_delta_count=tool_call_delta_count,
            stage_counts=stage_counts,
            first_event_ms=first_event_ms,
            first_reasoning_ms=first_reasoning_ms,
            first_content_ms=first_content_ms,
            first_tool_call_ms=first_tool_call_ms,
            content_chars=len(response.content or ""),
            reasoning_chars=len(response.reasoning or ""),
            tool_call_names=[tc.name for tc in response.tool_calls or []],
            content_hash=_payload_hash(response.content or ""),
            reasoning_hash=_payload_hash(response.reasoning or ""),
        )
        return response

    async def run(
        self,
        user: User,
        message: str,
        system_prompt: str | None = None,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
        on_llm_stage: Callable[[str], None] | None = None,
        tools_enabled: bool = True,
        trajectory_recorder: TrajectoryRecorder | None = None,
        few_shots: list[dict[str, Any]] | None = None,
        channel: str | None = None,
        run_id: str | None = None,
    ) -> tuple[str, RunStats]:
        """Run the ReAct loop until a final answer is given or limits are reached.

        Args:
            few_shots: Calibrated few-shot examples injected before history.
                Loaded from ``config/calibrated/few_shots.yaml`` by AgentStack
                and passed through here into ContextBuilder.

        Returns:
            (reply, stats) — the agent's final answer and execution metrics.
        """
        stats = RunStats(run_id=run_id) if run_id is not None else RunStats()
        loop_warning_count = 0
        last_actual_total_tokens: int | None = None
        t0 = time.monotonic()

        logger.debug(
            "[user=%s] run() start | msg=%r",
            user.id,
            message[:120],
        )
        log_event(
            "request_started",
            stats.run_id,
            user_id=user.id,
            department=user.department,
            channel=channel,
            message_len=len(message),
            message_preview=message,
        )

        # Per-call callback takes priority over the instance-level default
        _approval_cb = (
            approval_callback if approval_callback is not None else self._approval_callback
        )

        mem_key = user.memory_key()

        # Load history BEFORE building context so it precedes the current message
        history: list[dict[str, Any]] = []
        if self._memory:
            try:
                history = await self._memory.get_history(mem_key, limit=self._settings.max_history)
            except StorageError:
                logger.error("[user=%s] Failed to load history", user.id)

        # Prepend dynamic user context to the system prompt
        base_prompt = system_prompt or self._default_system_prompt or ""

        # Load user facts from memory (onboarding + manually stored via memory_store)
        user_facts_block = ""
        facts_count = 0
        if self._memory:
            facts: list[dict[str, str]] = []
            try:
                facts = await self._memory.recall_facts(
                    mem_key, limit=self._settings.max_facts_recall
                )
            except StorageError:
                logger.error("[user=%s] Failed to recall facts", user.id)
            if facts:
                facts_count = len(facts)
                lines = [f"- {f['key']}: {f['value']}" for f in facts]
                user_facts_block = "\n\n## Known Facts About This User\n" + "\n".join(lines)

        dynamic_prompt = (
            f"Current User Context:\n"
            f"- Name: {user.name}\n"
            f"- Department: {user.department}\n"
            f"{user_facts_block}\n\n"
            f"{base_prompt}"
        )

        context = ContextBuilder.build_initial(
            user,
            message,
            history=history,
            system_prompt_override=dynamic_prompt,
            few_shots=few_shots,
        )

        if self._memory:
            await self._save_memory(mem_key, "user", message)

        # Time budget is ALWAYS from settings (depends on hardware/model speed).
        # Department budget controls only iterations and tool calls (complexity limits).
        if self._permission_checker:
            dept_budget = self._permission_checker.get_budget(user)
            guard_config = SimpleBudgetGuardConfig(
                max_iterations=dept_budget.max_iterations,
                max_tool_calls=dept_budget.max_tool_calls,
                max_time_ms=self._settings.max_wall_time_ms,
            )
        else:
            guard_config = SimpleBudgetGuardConfig(
                max_iterations=self._settings.max_steps,
                max_tool_calls=self._settings.max_tool_calls,
                max_time_ms=self._settings.max_wall_time_ms,
            )
        budget = SimpleBudgetGuard(guard_config)
        progress = SimpleProgressGuard()
        tools_schema = None
        if tools_enabled:
            if self._enforce_tool_permissions:
                tools_schema = self._registry.to_schemas_for_user(self._permission_checker, user)
            else:
                tools_schema = self._registry.to_schemas()
        health.increment("requests")
        health.increment("active_requests")
        log_event(
            "context_built",
            stats.run_id,
            history_count=len(history),
            facts_count=facts_count,
            tools_available_count=len(tools_schema or []),
            system_prompt_chars=len(context.system_prompt or ""),
            message_count=context.message_count,
        )

        try:
            while True:
                budget.consume_iteration()
                stats.iterations += 1

                compression_cfg = self._settings.compression
                if compression_cfg.enabled and context.message_count > (
                    compression_cfg.prune_min_messages
                ):
                    context.prune_old_tool_results(protect_tail=6)

                if self._compressor and self._compressor.should_compress(
                    context.messages,
                    actual_tokens=last_actual_total_tokens,
                ):
                    context.messages = await self._compressor.compress(
                        context.messages,
                        mem_key,
                        actual_tokens=last_actual_total_tokens,
                    )
                    last_actual_total_tokens = None

                llm_t0 = time.monotonic()
                try:
                    log_event(
                        "llm_call_started",
                        stats.run_id,
                        iteration=stats.iterations,
                        tools_count=len(tools_schema or []),
                        message_count=context.message_count,
                        system_prompt_chars=len(context.system_prompt or ""),
                        streaming_enabled=self._settings.llm_streaming_enabled,
                    )
                    # When the provider is a queued router, separate queue wait
                    # from LLM inference so the budget only counts active time.
                    if isinstance(self._provider, LLMRouter) and self._provider.has_queue:
                        budget.pause()
                        response = await self._provider.call_default_with_slot(
                            user_id=str(user.id),
                            run_id=stats.run_id,
                            messages=context.messages,
                            tools=tools_schema,
                            system=context.system_prompt or None,
                            on_acquired=budget.resume,
                            call=lambda target_provider: asyncio.wait_for(
                                self._call_llm_provider(
                                    target_provider,
                                    messages=context.messages,
                                    tools=tools_schema,
                                    system=context.system_prompt or None,
                                    run_id=stats.run_id,
                                    iteration=stats.iterations,
                                    on_llm_stage=on_llm_stage,
                                    stats=stats,
                                ),
                                timeout=self._settings.llm_timeout_seconds,
                            ),
                        )
                    else:
                        target_provider: Provider = (
                            self._provider.default
                            if isinstance(self._provider, LLMRouter)
                            else self._provider
                        )
                        response = await asyncio.wait_for(
                            self._call_llm_provider(
                                target_provider,
                                messages=context.messages,
                                tools=tools_schema,
                                system=context.system_prompt or None,
                                run_id=stats.run_id,
                                iteration=stats.iterations,
                                on_llm_stage=on_llm_stage,
                                stats=stats,
                            ),
                            timeout=self._settings.llm_timeout_seconds,
                        )
                except TimeoutError:
                    msg = "I could not get a response from the language model (timed out)."
                    await self._save_memory(mem_key, "assistant", msg)
                    stats.status = "timeout"
                    stats.duration_ms = (time.monotonic() - t0) * 1000
                    health.increment("llm_timeouts")
                    log_event(
                        "llm_call_finished",
                        stats.run_id,
                        iteration=stats.iterations,
                        status="timeout",
                        duration_ms=round((time.monotonic() - llm_t0) * 1000, 1),
                    )
                    log_event(
                        "request_finished",
                        stats.run_id,
                        status=stats.status,
                        iterations=stats.iterations,
                        tools_used=stats.tools_used,
                        duration_ms=round(stats.duration_ms, 1),
                        final_answer_len=len(msg),
                    )
                    logger.warning(
                        "[user=%s] LLM timeout on iteration %d", user.id, stats.iterations
                    )
                    return msg, stats
                except Exception as e:
                    health.increment("errors")
                    stats.status = "error"
                    stats.error = str(e)
                    stats.duration_ms = (time.monotonic() - t0) * 1000
                    log_event(
                        "llm_call_finished",
                        stats.run_id,
                        iteration=stats.iterations,
                        status="error",
                        duration_ms=round((time.monotonic() - llm_t0) * 1000, 1),
                        error=type(e).__name__,
                    )
                    log_event(
                        "request_finished",
                        stats.run_id,
                        status=stats.status,
                        iterations=stats.iterations,
                        tools_used=stats.tools_used,
                        duration_ms=round(stats.duration_ms, 1),
                        final_answer_len=0,
                        error=stats.error,
                    )
                    raise

                stats.llm_calls += 1
                stats.input_tokens += response.usage.input_tokens
                stats.output_tokens += response.usage.output_tokens
                stats.total_tokens += response.usage.total_tokens
                stats.latest_total_tokens = response.usage.total_tokens
                last_actual_total_tokens = (
                    response.usage.total_tokens if response.usage.total_tokens > 0 else None
                )
                health.increment("llm_calls")
                log_event(
                    "llm_call_finished",
                    stats.run_id,
                    iteration=stats.iterations,
                    status="ok",
                    duration_ms=round((time.monotonic() - llm_t0) * 1000, 1),
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    tool_call_names=[tc.name for tc in response.tool_calls or []],
                    finish_has_content=bool(response.content),
                    content_chars=len(response.content or ""),
                    reasoning_chars=len(response.reasoning or ""),
                    content_hash=_payload_hash(response.content or ""),
                    reasoning_hash=_payload_hash(response.reasoning or ""),
                )

                logger.debug(
                    "[user=%s] llm_response iter=%d | content=%r | tool_calls=%d",
                    user.id,
                    stats.iterations,
                    (response.content or "")[:200],
                    len(response.tool_calls or []),
                )

                # Log reasoning (if present) — does NOT enter agent context
                if response.reasoning:
                    logger.debug(
                        "[user=%s] reasoning (%d chars): %s",
                        user.id,
                        len(response.reasoning),
                        response.reasoning[:200],
                    )

                if not response.tool_calls:
                    # Final answer — ALWAYS return, even if time budget exceeded.
                    # The model already completed its work; discarding it wastes the
                    # entire LLM call and frustrates users who waited for a response.
                    final = response.content if response.content else "Agent provided no response."
                    if self._memory:
                        await self._save_turn(mem_key, final, stats.tools_used, response.reasoning)
                        if self._consolidator:
                            await self._consolidator.maybe_consolidate(self._memory, mem_key)
                    stats.duration_ms = (time.monotonic() - t0) * 1000
                    logger.debug(
                        "[user=%s] final_answer | len=%d | iterations=%d | duration_ms=%.0f",
                        user.id,
                        len(final),
                        stats.iterations,
                        stats.duration_ms,
                    )
                    log_event(
                        "request_finished",
                        stats.run_id,
                        status=stats.status,
                        iterations=stats.iterations,
                        tools_used=stats.tools_used,
                        duration_ms=round(stats.duration_ms, 1),
                        final_answer_len=len(final),
                    )
                    return final, stats

                # Model wants more work — check ALL budget limits before continuing.
                budget.check()
                budget.consume_tool_calls(len(response.tool_calls))

                # Agent requested tools — emit a single assistant message
                # containing both content (if any) and tool_calls.
                context.add_tool_calls(response.tool_calls, content=response.content or None)
                health.increment("tool_calls", len(response.tool_calls))

                if self._can_parallelize(response.tool_calls):
                    results = await self._execute_parallel(
                        response.tool_calls,
                        user,
                        _approval_cb,
                        on_tool_start,
                        trajectory_recorder,
                        stats,
                    )
                    # Add ALL results first to keep context valid (no orphaned tool_calls)
                    loop_detected = False
                    for tc, result in zip(response.tool_calls, results, strict=True):
                        context.add_tool_result(tc.id, tc.name, result)
                        stats.tools_used.append(tc.name)
                        if not loop_detected and progress.detect_loop(tc.name, result):
                            context.add_assistant_message(
                                "System Guard: You seem to be stuck in a loop repeating the same"
                                " error. Please change your strategy or stop using this tool."
                            )
                            loop_detected = True
                    if loop_detected:
                        loop_warning_count += 1
                        if loop_warning_count >= 2:
                            break
                        continue
                else:
                    should_stop = False
                    for tc in response.tool_calls:
                        result = await self._execute_single_tool(
                            tc,
                            user,
                            _approval_cb,
                            on_tool_start,
                            trajectory_recorder,
                            stats,
                        )
                        context.add_tool_result(tc.id, tc.name, result)
                        stats.tools_used.append(tc.name)

                        # Terminal tool: return result directly (no LLM re-paraphrase).
                        # Used for tools like read_image where the vision model already
                        # produces a complete user-facing response.
                        tool_obj = self._registry.get(tc.name)
                        if (
                            tool_obj is not None
                            and (
                                tool_obj.should_return_direct(tc.arguments, result)
                                if hasattr(tool_obj, "should_return_direct")
                                else getattr(tool_obj, "terminal", False)
                            )
                            and len(response.tool_calls) == 1
                            and not result.startswith(TOOL_ERROR_PREFIX)
                        ):
                            if self._memory:
                                await self._save_turn(mem_key, result, stats.tools_used)
                                if self._consolidator:
                                    await self._consolidator.maybe_consolidate(
                                        self._memory, mem_key
                                    )
                            stats.duration_ms = (time.monotonic() - t0) * 1000
                            logger.debug(
                                "[user=%s] terminal_tool=%s | returning result directly",
                                user.id,
                                tc.name,
                            )
                            log_event(
                                "request_finished",
                                stats.run_id,
                                status=stats.status,
                                iterations=stats.iterations,
                                tools_used=stats.tools_used,
                                duration_ms=round(stats.duration_ms, 1),
                                final_answer_len=len(result),
                            )
                            return result, stats

                        if progress.detect_loop(tc.name, result):
                            context.add_assistant_message(
                                "System Guard: You seem to be stuck in a loop repeating the same"
                                " error. Please change your strategy or stop using this tool."
                            )
                            loop_warning_count += 1
                            if loop_warning_count >= 2:
                                should_stop = True
                            break

                    if should_stop:
                        break

        except BudgetExceededError as e:
            health.increment("errors")
            msg = f"I reached my resource limit and had to stop: {e}"
            if self._memory:
                await self._save_turn(mem_key, msg, stats.tools_used)
            stats.status = "budget"
            stats.error = str(e)
            stats.duration_ms = (time.monotonic() - t0) * 1000
            logger.warning("[user=%s] budget exceeded: %s", user.id, e)
            log_event(
                "request_finished",
                stats.run_id,
                status=stats.status,
                iterations=stats.iterations,
                tools_used=stats.tools_used,
                duration_ms=round(stats.duration_ms, 1),
                final_answer_len=len(msg),
                error=stats.error,
            )
            return msg, stats
        finally:
            health.increment("active_requests", -1)

        fallback = "I detected a loop and stopped to avoid repeating the same actions."
        if self._memory:
            await self._save_turn(mem_key, fallback, stats.tools_used)
        stats.status = "loop"
        stats.duration_ms = (time.monotonic() - t0) * 1000
        logger.warning("[user=%s] loop detected after %d iterations", user.id, stats.iterations)
        log_event(
            "request_finished",
            stats.run_id,
            status=stats.status,
            iterations=stats.iterations,
            tools_used=stats.tools_used,
            duration_ms=round(stats.duration_ms, 1),
            final_answer_len=len(fallback),
        )
        return fallback, stats

    async def _save_memory(self, mem_key: str, role: str, content: str, **kwargs: Any) -> None:
        """Persist a message to memory, swallowing StorageError."""
        if not self._memory:
            return
        try:
            await self._memory.add_message(mem_key, role, content, **kwargs)
        except StorageError:
            logger.error("[user=%s] Failed to save %s message", mem_key, role)

    async def _save_turn(
        self,
        mem_key: str,
        content: str,
        tools_used: list[str],
        response_reasoning: str | None = None,
    ) -> None:
        """Save assistant response + factual execution record as system message.

        The execution record is saved as a ``system`` role message right after
        the assistant message.  On next run it appears in the message list as a
        system message, giving the model hard evidence of what was *actually*
        executed — so it can detect its own false claims.
        """
        # 1. Save assistant text (reasoning column for audit)
        reasoning_marker = _format_tool_marker(tools_used)
        reasoning_parts: list[str] = []
        if response_reasoning:
            reasoning_parts.append(response_reasoning)
        reasoning_parts.append(reasoning_marker)
        await self._save_memory(mem_key, "assistant", content, reasoning="\n".join(reasoning_parts))

        # 2. Save factual execution record (visible to model as system message)
        if tools_used:
            seen: set[str] = set()
            unique: list[str] = []
            for t in tools_used:
                if t not in seen:
                    seen.add(t)
                    unique.append(t)
            record = f"Tools called in this turn: {', '.join(unique)}"
        else:
            record = "Tools called in this turn: none"
        await self._save_memory(mem_key, "system", record)

    def _can_parallelize(self, tool_calls: list[ToolCall]) -> bool:
        """Check if all tools in batch can be safely executed in parallel.

        ToolGuard checks are performed inside _execute_single_tool for each tool,
        so parallel execution is safe even with ToolGuard present.
        """
        if len(tool_calls) <= 1:
            return False

        for tc in tool_calls:
            tool = self._registry.get(tc.name)
            if tool is None or not getattr(tool, "parallel_safe", True):
                return False
        return True

    async def _execute_parallel(
        self,
        tool_calls: list[ToolCall],
        user: User,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None,
        on_tool_start: Callable[[str], None] | None,
        trajectory_recorder: TrajectoryRecorder | None = None,
        stats: RunStats | None = None,
    ) -> list[str]:
        """Execute multiple tools in parallel and return results."""

        async def execute_one(tc: ToolCall) -> str:
            return await self._execute_single_tool(
                tc,
                user,
                approval_callback,
                on_tool_start,
                trajectory_recorder,
                stats,
            )

        results = await asyncio.gather(*[execute_one(tc) for tc in tool_calls])
        return list(results)

    async def _execute_single_tool(
        self,
        tc: ToolCall,
        user: User,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None,
        on_tool_start: Callable[[str], None] | None,
        trajectory_recorder: TrajectoryRecorder | None = None,
        stats: RunStats | None = None,
    ) -> str:
        """Execute a single tool with all checks."""
        run_id = stats.run_id if stats else "unknown"
        tool_t0 = time.monotonic()
        log_event(
            "tool_call_started",
            run_id,
            tool=tc.name,
            tool_call_id=tc.id,
            args_preview=_json_preview(tc.arguments),
            args_hash=_payload_hash(tc.arguments),
        )
        if (
            self._enforce_tool_permissions
            and self._permission_checker
            and not self._permission_checker.can_use_tool(user, tc.name)
        ):
            result = (
                f"Error: Permission denied. Your department ({user.department})"
                f" cannot use tool '{tc.name}'."
            )
            log_event(
                "tool_call_finished",
                run_id,
                tool=tc.name,
                tool_call_id=tc.id,
                status="permission_denied",
                duration_ms=round((time.monotonic() - tool_t0) * 1000, 1),
                result_preview=result,
                result_hash=_payload_hash(result),
            )
            return (
                f"Error: Permission denied. Your department ({user.department})"
                f" cannot use tool '{tc.name}'."
            )

        logger.debug(
            "[user=%s] tool_call | tool=%s | args=%s",
            user.id,
            tc.name,
            json.dumps(tc.arguments, ensure_ascii=False)[:_LOG_TRUNCATE],
        )

        # Calibration trajectory recording
        if trajectory_recorder is not None:
            trajectory_recorder.record_tool_call(tc.name, tc.arguments)

        try:
            if self._tool_guard:
                tool = self._registry.get(tc.name)
                risk_level = tool.risk_level if tool else None
                risk = risk_level.value if risk_level else None
                guard_check = self._tool_guard.check
                if "run_id" in inspect.signature(guard_check).parameters:
                    await guard_check(
                        tc.name,
                        tc.arguments,
                        risk_level=risk,
                        run_id=run_id,
                    )
                else:
                    await guard_check(tc.name, tc.arguments, risk_level=risk)
                log_event(
                    "tool_guard_decision",
                    run_id,
                    tool=tc.name,
                    tool_call_id=tc.id,
                    decision="allow",
                    risk_level=risk,
                )

            if on_tool_start:
                on_tool_start(tc.name)

            result = await self._registry.execute(
                tc.name,
                tc.arguments,
                user=user,
                run_id=run_id,
            )
            status = "error" if result.startswith("Error") else "ok"
            if status == "error":
                health.increment("tool_errors")

        except ApprovalRequest as e:
            log_event(
                "tool_guard_decision",
                run_id,
                tool=tc.name,
                tool_call_id=tc.id,
                decision="approval_required",
                rule_id=e.action,
                details=e.details,
            )
            if approval_callback:
                # Lock prevents concurrent approval prompts when tools run in parallel
                async with self._approval_lock:
                    approved = await approval_callback(e.action, e.details)
                log_event(
                    "approval_finished",
                    run_id,
                    tool=tc.name,
                    tool_call_id=tc.id,
                    action=e.action,
                    approved=approved,
                    status="approved" if approved else "denied",
                )
                if approved:
                    result = await self._registry.execute(
                        tc.name,
                        tc.arguments,
                        user=user,
                        run_id=run_id,
                    )
                    status = "ok"
                else:
                    health.increment("approval_denied")
                    result = f"Action '{e.action}' was denied by user."
                    status = "approval_denied"
            else:
                result = (
                    f"Action Paused: approval required for '{e.action}' "
                    f"but no approval channel is configured."
                )
                status = "approval_no_channel"
                log_event(
                    "approval_finished",
                    run_id,
                    tool=tc.name,
                    tool_call_id=tc.id,
                    action=e.action,
                    approved=False,
                    status="no_channel",
                )
        except ToolGuardError as e:
            health.increment("guard_blocks")
            log_event(
                "tool_guard_decision",
                run_id,
                tool=tc.name,
                tool_call_id=tc.id,
                decision="block",
                details=str(e),
            )
            result = str(e)
            status = "guard_blocked"
        except ContainerIPCError as e:
            logger.error("[user=%s] Container IPC error for tool %s: %s", user.id, tc.name, e)
            result = str(e)
            status = "container_error"
            health.increment("tool_errors")
        except Exception:
            logger.exception("[user=%s] Unexpected error executing tool %s", user.id, tc.name)
            result = f"Error executing tool {tc.name}: see logs for details"
            status = "error"
            health.increment("tool_errors")

        logger.debug(
            "[user=%s] tool_result | tool=%s | result=%r",
            user.id,
            tc.name,
            result[:_LOG_TRUNCATE],
        )

        # Calibration trajectory recording
        if trajectory_recorder is not None:
            trajectory_recorder.record_tool_result(tc.name, result)

        duration_ms = round((time.monotonic() - tool_t0) * 1000, 1)
        if stats is not None:
            stats.tool_durations_ms[tc.name] = (
                stats.tool_durations_ms.get(tc.name, 0.0) + duration_ms
            )
        log_event(
            "tool_call_finished",
            run_id,
            tool=tc.name,
            tool_call_id=tc.id,
            status=status,
            duration_ms=duration_ms,
            result_preview=result,
            result_hash=_payload_hash(result),
        )

        return result
