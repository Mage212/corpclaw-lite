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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from corpclaw_lite.agent.adaptations import (
    apply_closing_mode,
    apply_tool_surface,
    apply_workflow_mandate,
    inject_tool_soft_hint,
)
from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.agent.context_target import (
    get_context_session_id,
    get_context_user_id,
    reset_context_target,
    set_context_target,
)
from corpclaw_lite.agent.depth_mode import (
    DepthMode,
    reset_call_depth_mode,
    resolve_depth_sampling,
    set_call_depth_mode,
)
from corpclaw_lite.agent.guards import (
    BudgetExceededError,
    PlanningTextGuard,
    ResultDedupGuard,
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
    SimpleProgressGuard,
    SoftDeadline,
    SoftDeadlineConfig,
    TerminalToolMandate,
    TerminalToolMandateConfig,
)
from corpclaw_lite.agent.loop_state import LoopState, TurnTokens
from corpclaw_lite.agent.task_run import TaskRun
from corpclaw_lite.agent.workspace_context import (
    reset_workspace_root,
    set_workspace_root,
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
    reset_capture_context,
    reset_request_options,
    reset_run_id,
    set_capture_context,
    set_request_options,
    set_run_id,
)
from corpclaw_lite.llm.queue import LLMQueueStatus
from corpclaw_lite.llm.router import LLMRouter, QueuedProvider
from corpclaw_lite.llm.xml_tool_calling import (
    build_xml_repair_prompt,
    contains_xml_tool_call_markers,
)
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
    from corpclaw_lite.agent.phase_policy import PhasePolicy
    from corpclaw_lite.calibration.trajectory import TrajectoryRecorder
    from corpclaw_lite.channels.web.chat_context_store import ChatContextStore
    from corpclaw_lite.config.providers import ProviderRegistry
    from corpclaw_lite.config.settings import DepthModeSettings
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.llm.presets import PresetRegistry
    from corpclaw_lite.memory.consolidation import MemoryConsolidator
    from corpclaw_lite.memory.file_changes import FileChangeDAO
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)

# Max chars to include from tool args / results in DEBUG logs
# Large files / responses are truncated to avoid flooding the log file
_LOG_TRUNCATE = 400

# Per-user approval locks: one lock per user.id so different users never block
# each other's approval prompts. Stale (unlocked) entries are pruned past this cap.
_MAX_APPROVAL_LOCKS = 10_000
_LOOP_GUARD_TEXT = (
    "System Guard: You seem to be stuck in a loop repeating the same error. "
    "Please change your strategy or stop using this tool."
)
_LOOP_RECOVERY_INSTRUCTION = (
    "Internal recovery instruction: the previous tool action repeated the same error. "
    "Change strategy now: stop using the failing tool if possible, try different inputs or "
    "another tool, or explain the tool limitation to the user. Do not quote this instruction."
)
# B-055: injected into the system prompt when a tool returns the same successful
# result multiple times in a row. Distinct from _LOOP_RECOVERY_INSTRUCTION, which
# targets repeated *errors*; this one targets repeated identical *successes*.
_DEDUP_INSTRUCTION = (
    "Internal recovery instruction: a tool returned the same result it returned before. "
    "Calling it again with the same arguments will not produce new information. Either "
    "answer directly from the data you have already retrieved, or use a different tool or "
    "different input. Do not quote this instruction."
)
_LOOP_FALLBACK = "I detected a loop and stopped to avoid repeating the same actions."
_XML_TOOL_CALL_FALLBACK = (
    "I could not safely parse the model's tool-call output, so I stopped instead of "
    "showing raw internal tool-call markup."
)
# Auto-finalize cascade: injected when the budget is fully exhausted and the
# terminal tool was never called. This is the last chance to salvage the work
# done across all iterations — one LLM call (B), then a programmatic finalize
# fallback (C) if the model still does not cooperate.
_AUTO_FINALIZE_EMERGENCY_PROMPT = (
    "You have run out of iterations and MUST finalize now. Do not call any tool "
    "except {terminal}. Synthesize a complete report from all the evidence and "
    "facts you have gathered so far. Call {terminal} with the full Markdown "
    "report as the 'answer' argument. If your evidence is incomplete, say so "
    "honestly in a limitations section — but you MUST finalize now."
)

# B-047 ext / B-077: empty-response retry (was locals inside run()).
_EMPTY_RESPONSE_MAX_RETRIES = 3
_EMPTY_RESPONSE_PROMPT = (
    "You returned an empty response with no tool call. This is not a valid "
    "final answer. Continue your task: call a tool to gather more data, or "
    "if you have enough information, provide a complete response now."
)


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


def _queue_notify_position(settings: AgentSettings) -> bool:
    """Return queue position notification setting with AgentSettings fallback."""
    settings_obj: Any = settings
    queue_settings = getattr(getattr(settings_obj, "llm", None), "queue", None)
    return bool(getattr(queue_settings, "notify_position", True))


def _queue_notify_interval_seconds(settings: AgentSettings) -> float:
    """Return queue notification interval with AgentSettings fallback."""
    settings_obj: Any = settings
    queue_settings = getattr(getattr(settings_obj, "llm", None), "queue", None)
    raw_interval = getattr(queue_settings, "notify_interval_seconds", 30.0)
    try:
        return float(raw_interval)
    except (TypeError, ValueError):
        return 30.0


def _append_loop_recovery_instruction(context: ContextBuilder) -> None:
    """Add a one-run recovery hint without creating assistant-visible content."""
    if _LOOP_RECOVERY_INSTRUCTION in context.system_prompt:
        return

    if context.system_prompt:
        context.system_prompt += f"\n\n---\n{_LOOP_RECOVERY_INSTRUCTION}"
    else:
        context.system_prompt = _LOOP_RECOVERY_INSTRUCTION


def _append_dedup_instruction(context: ContextBuilder) -> None:
    """Add the result-dedup recovery hint (B-055), idempotent per run."""
    if _DEDUP_INSTRUCTION in context.system_prompt:
        return

    if context.system_prompt:
        context.system_prompt += f"\n\n---\n{_DEDUP_INSTRUCTION}"
    else:
        context.system_prompt = _DEDUP_INSTRUCTION


def _detect_result_dedup(
    guard: ResultDedupGuard,
    action_results: list[tuple[str, str]],
) -> tuple[str | None, str]:
    """Run the result-dedup guard over one tool batch (B-055).

    Only non-error results are considered: error loops are the responsibility of
    :class:`SimpleProgressGuard`. Returns ``(tool_name, result)`` of the first
    result that triggered dedup, or ``(None, "")`` if no loop was detected.
    """
    for tool_name, result in action_results:
        if result.startswith(TOOL_ERROR_PREFIX):
            continue
        if guard.detect(tool_name, result):
            return tool_name, result
    return None, ""


def _is_loop_guard_echo(content: str) -> bool:
    """Detect old internal guard text if the model echoes it as a final answer."""
    return content.strip() == _LOOP_GUARD_TEXT


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
    workspace_base: Path | None = None
    # B-047: workflow-finalize guard config. When terminal_tool is set, the loop nudges
    # the model toward the mandatory terminal tool and restricts the schema as the
    # wall-clock budget runs out. None/empty for the main agent and non-research
    # subagents — the guard is neutral then.
    terminal_tool: str | None = None
    required_before_terminal: list[str] = field(default_factory=list[str])
    # B-040: file-change journal DAO. When set, the loop injects a
    # <recent_files> block into the system prompt at run start.
    file_change_dao: FileChangeDAO | None = None
    # D-056 PR2: per-call thinking overrides based on task phase. When None,
    # AgentLoop constructs a DefaultPhasePolicy from settings.agent.phase_policy.
    phase_policy: PhasePolicy | None = None
    # Etap 3: depth-mode override (Fast/Think). When the loop is given a
    # ``depth_mode`` in ``run()``, it resolves a per-model SamplingProfile and
    # applies it via ``LLMRouter.with_overrides``. Requires these registries +
    # mapping to be set; when None, depth override is a no-op.
    preset_registry: PresetRegistry | None = None
    provider_registry: ProviderRegistry | None = None
    depth_modes: DepthModeSettings | None = None
    # B-063 S1: full LLM-context persistence per chat. When set, the loop writes
    # the full message schema (tool_calls/reasoning/tool-role) to the store on
    # every turn, keyed by session_id. Restore (S2) and compress-any-chat (S3)
    # are separate sprints; S1 only accumulates data.
    chat_context_store: ChatContextStore | None = None
    # B-107: tool-surface profile — "main" | "office" | "execution" | "none".
    # Hard phase-filter applies to "office" (and optionally main soft-hint only).
    tool_surface_profile: str = "main"


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
        self._workspace_base = config.workspace_base
        self._terminal_tool = config.terminal_tool
        self._required_before_terminal = config.required_before_terminal
        self._file_change_dao = config.file_change_dao
        # D-056 PR2: phase-based per-call thinking overrides. Default policy
        # comes from settings; tests/callers may inject a custom PhasePolicy.
        from corpclaw_lite.agent.phase_policy import DefaultPhasePolicy

        self._phase_policy = config.phase_policy or DefaultPhasePolicy(self._settings.phase_policy)
        # Etap 3: registries + depth mapping for Fast/Think override.
        self._preset_registry = config.preset_registry
        self._provider_registry = config.provider_registry
        self._depth_modes = config.depth_modes
        # B-063 S1: full LLM-context persistence per chat.
        self._chat_context_store = config.chat_context_store
        self._tool_surface_profile = config.tool_surface_profile
        # Cache marker sets as frozensets for the hot path.
        self._phase_aggregation_markers = frozenset(self._settings.phase_policy.aggregation_markers)
        self._phase_gathering_tools = frozenset(self._settings.phase_policy.gathering_tools)
        # Per-user approval locks: serializes parallel approval prompts for ONE user
        # (avoids confusing multiple Approve/Deny buttons), but different users are
        # independent and never block each other. See _get_approval_lock.
        self._approval_locks: dict[int, asyncio.Lock] = {}

    def _get_approval_lock(self, user_id: int) -> asyncio.Lock:
        """Return the per-user approval lock, pruning stale entries past the cap.

        Mirrors ``ContainerManager._get_lock``: lazy creation plus cleanup of unlocked
        entries when the pool exceeds ``_MAX_APPROVAL_LOCKS`` (keeps memory bounded for
        long-running multi-user deployments).
        """
        lock = self._approval_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._approval_locks[user_id] = lock
        if len(self._approval_locks) > _MAX_APPROVAL_LOCKS:
            stale = [k for k, v in self._approval_locks.items() if not v.locked()]
            for k in stale[: len(stale) // 2]:
                del self._approval_locks[k]
        return lock

    @property
    def memory(self) -> SQLiteMemory | None:
        """Access the memory backend (if configured)."""
        return self._memory

    @property
    def provider(self) -> Provider:
        """Access the LLM provider."""
        return self._provider

    @property
    def compressor(self) -> ContextCompressor | None:
        """Access the context compressor (if configured)."""
        return self._compressor

    async def compress_now(self, user: User, session_id: int | None = None) -> tuple[bool, str]:
        """On-demand compression of a chat's LLM context (B-063 S3).

        When ``session_id`` is provided AND a chat-context-store is configured,
        compresses the FULL LLM context (tool_calls + reasoning) from the
        per-chat store — works for ANY chat, not just the active one.

        When ``session_id`` is None (telegram/CLI/legacy, or no store),
        compresses the active chat's in-memory history (role+content only) and
        writes back to SQLiteMemory.

        The caller holds the single-in-flight lock so this never races a run().

        Returns ``(ok, message)``. On any failure the context is left untouched.
        """
        if self._compressor is None:
            return False, "Компрессия контекста недоступна."

        # B-063 S3: prefer the full-context path from the per-chat store.
        if session_id is not None and self._chat_context_store is not None:
            return await self._compress_from_context_store(user, session_id)
        return await self._compress_from_memory(user)

    async def _compress_from_context_store(self, user: User, session_id: int) -> tuple[bool, str]:
        """Compress a chat's full LLM context from the per-chat store (S3).

        Loads the full message schema (tool_calls/tool-role/reasoning), compresses,
        and writes back to the store via ``replace_context``. Does NOT touch
        SQLiteMemory — the store is the source of truth for restored chats, and
        the chat may not be the active one (its memory shadow is irrelevant).
        """
        store = self._chat_context_store
        compressor = self._compressor
        if store is None or compressor is None:  # caller guards, but satisfy type checker
            return False, "Компрессия контекста недоступна."
        try:
            messages = await store.list_context(session_id)
        except Exception:
            logger.warning("[session=%s] compress: context-store load failed", session_id)
            return False, "Не удалось загрузить историю чата."
        if len(messages) < 5:
            return False, "Слишком мало сообщений для сжатия."

        try:
            compressed = await compressor.compress(messages, mem_key=f"{user.id}:{session_id}")
        except Exception:
            logger.exception("[session=%s] compress: compression failed", session_id)
            return False, "Ошибка при сжатии контекста."

        if len(compressed) >= len(messages):
            return True, "Контекст уже достаточно компактный — сжатие не требуется."

        try:
            await store.replace_context(
                session_id=session_id, user_id=str(user.id), messages=compressed
            )
        except Exception:
            logger.warning("[session=%s] compress: context-store write-back failed", session_id)
            return False, "Не удалось сохранить сжатый контекст."

        logger.info(
            "[user=%s session=%s] compress: %d → %d messages (context-store)",
            user.id,
            session_id,
            len(messages),
            len(compressed),
        )
        return True, f"Контекст сжат: {len(messages)} → {len(compressed)} сообщений."

    async def _compress_from_memory(self, user: User) -> tuple[bool, str]:
        """Compress the active chat's in-memory history (legacy/telegram path).

        Loads from SQLiteMemory (role+content only), compresses, writes back to
        memory (clear + re-add), and invalidates the KV-cache.
        """
        if self._memory is None:
            return False, "Память недоступна."
        compressor = self._compressor
        if compressor is None:
            return False, "Компрессия контекста недоступна."
        mem_key = user.memory_key()
        try:
            history = await self._memory.get_history(mem_key, limit=self._settings.max_history)
        except StorageError:
            logger.error("[user=%s] compress: failed to load history", user.id)
            return False, "Не удалось загрузить историю чата."
        if len(history) < 5:
            return False, "Слишком мало сообщений для сжатия."

        try:
            compressed = await compressor.compress(history, mem_key=mem_key)
        except Exception:
            logger.exception("[user=%s] compress: compression failed", user.id)
            return False, "Ошибка при сжатии контекста."

        if len(compressed) >= len(history):
            return True, "Контекст уже достаточно компактный — сжатие не требуется."

        try:
            await self._memory.clear(mem_key)
            for msg in compressed:
                role = str(msg.get("role", "user"))
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = json.dumps(content, ensure_ascii=False)
                await self._memory.add_message(mem_key, role, str(content))
        except StorageError:
            logger.error("[user=%s] compress: failed to persist", user.id)
            return False, "Не удалось сохранить сжатый контекст."

        if isinstance(self._provider, LLMRouter):
            try:
                await self._provider.mark_user_cache_reset(mem_key)
            except Exception:
                logger.debug("[user=%s] compress: cache reset skipped", user.id)

        logger.info(
            "[user=%s] compress: %d → %d messages (memory)",
            user.id,
            len(history),
            len(compressed),
        )
        return True, f"Контекст сжат: {len(history)} → {len(compressed)} сообщений."

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

        def emit_status(stage: str) -> None:
            if self._settings.llm_stream_status_updates and on_llm_stage is not None:
                on_llm_stage(stage)

        emit_status("model_waiting")

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
        on_llm_queue_status: Callable[[LLMQueueStatus], None] | None = None,
        on_tool_batch_start: Callable[[list[str]], None] | None = None,
        on_subagent_tool_start: Callable[[str, str], None] | None = None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
        on_subagent_llm_stage: Callable[[str, str], None] | None = None,
        on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None,
        tools_enabled: bool = True,
        trajectory_recorder: TrajectoryRecorder | None = None,
        few_shots: list[dict[str, Any]] | None = None,
        channel: str | None = None,
        run_id: str | None = None,
        depth_mode: DepthMode | None = None,
        session_id: int | None = None,
    ) -> tuple[str, RunStats]:
        """Run the ReAct loop until a final answer is given or limits are reached.

        Args:
            few_shots: Calibrated few-shot examples injected before history.
                Loaded from ``config/calibrated/few_shots.yaml`` by AgentStack
                and passed through here into ContextBuilder.

        Returns:
            (reply, stats) — the agent's final answer and execution metrics.

        B-077: prologue (``_build_turn_context``) packs ``LoopState`` + contextvars;
        epilogue (``_finalize_turn``) resets tokens. ReAct body is behavior-neutral.
        """
        tokens = TurnTokens()
        # Prologue outside the ReAct try so ``state`` is always bound for except/
        # fallback; epilogue still runs if prologue fails mid-bind.
        try:
            (
                state,
                effective_provider,
                _approval_cb,
                emit_llm_status,
            ) = await self._build_turn_context(
                user=user,
                message=message,
                system_prompt=system_prompt,
                approval_callback=approval_callback,
                on_llm_stage=on_llm_stage,
                tools_enabled=tools_enabled,
                few_shots=few_shots,
                channel=channel,
                run_id=run_id,
                depth_mode=depth_mode,
                session_id=session_id,
                tokens=tokens,
            )
        except BaseException:
            self._finalize_turn(tokens)
            raise

        try:
            while True:
                state.budget.consume_iteration()
                # B-066: check ALL state.budget limits at the top of every iteration so the
                # retry ``continue`` paths below (empty-response / XML-repair /
                # planning-text) cannot burn extra LLM calls past the state.budget. The
                # ``except BudgetExceededError`` handler gracefully finalizes even
                # when this fires before the iteration's LLM call.
                state.budget.check()
                state.stats.iterations += 1
                # Promote the previous turn's collected tools, then reset for
                # this turn. PhasePolicy reads state.prev_turn_tools below.
                state.prev_turn_tools = state.current_turn_tools
                state.current_turn_tools = []

                # Soft deadline (wall-clock) -> closing mode: reduce tool schema to
                # finalize-only terminal tools so the model wraps up instead of being
                # hard-cancelled by asyncio.wait_for. Fixes the subagent-timeout race
                # where wait_for (wall-clock) always beat the active-time state.budget guard.
                # B-046: the same check is also applied right before each LLM call (see
                # apply_closing_mode) so a single long iteration that straddles the
                # deadline still triggers it before the model is asked to produce more
                # tool calls.
                # B-107: recompute tools from base by phase (before closing narrows).
                apply_tool_surface(
                    state,
                    user_message=message,
                    settings=self._settings.tool_surface,
                    profile=self._tool_surface_profile,
                )

                state.tools_schema = await apply_closing_mode(
                    state.soft_deadline,
                    state.tools_schema,
                    state.task_run,
                    user,
                    state.stats,
                    terminal_tool_names=frozenset(
                        t.name for t in self._registry.list_all() if getattr(t, "terminal", False)
                    ),
                    max_wall_time_ms=self._settings.max_wall_time_ms,
                    soft_deadline_ratio=self._settings.soft_deadline_ratio,
                )

                compression_cfg = self._settings.compression
                if compression_cfg.enabled and state.context.message_count > (
                    compression_cfg.prune_min_messages
                ):
                    state.context.prune_old_tool_results(protect_tail=6)

                if self._compressor and self._compressor.should_compress(
                    state.context.messages,
                    actual_tokens=state.last_actual_total_tokens,
                ):
                    state.context.messages = await self._compressor.compress(
                        state.context.messages,
                        state.mem_key,
                        actual_tokens=state.last_actual_total_tokens,
                    )
                    state.last_actual_total_tokens = None

                llm_t0 = time.monotonic()
                try:
                    log_event(
                        "llm_call_started",
                        state.stats.run_id,
                        iteration=state.stats.iterations,
                        tools_count=len(state.tools_schema or []),
                        message_count=state.context.message_count,
                        system_prompt_chars=len(state.context.system_prompt or ""),
                        streaming_enabled=self._settings.llm_streaming_enabled,
                    )
                    # B-107 again then B-046: phase from base, mandate re-restrict,
                    # soft deadline, then single soft-hint (not at top-of-iter).
                    apply_tool_surface(
                        state,
                        user_message=message,
                        settings=self._settings.tool_surface,
                        profile=self._tool_surface_profile,
                    )
                    # B-046: re-check the soft deadline immediately before the LLM call.
                    # A long previous iteration may have crossed the wall-clock deadline
                    # mid-iteration; without this check the model is asked for another
                    # round of tool calls and closing mode only engages next iteration
                    # (by which point asyncio.wait_for may already cancel the run).
                    state.tools_schema = await apply_closing_mode(
                        state.soft_deadline,
                        state.tools_schema,
                        state.task_run,
                        user,
                        state.stats,
                        terminal_tool_names=frozenset(
                            t.name
                            for t in self._registry.list_all()
                            if getattr(t, "terminal", False)
                        ),
                        max_wall_time_ms=self._settings.max_wall_time_ms,
                        soft_deadline_ratio=self._settings.soft_deadline_ratio,
                    )
                    inject_tool_soft_hint(
                        state,
                        user_message=message,
                        settings=self._settings.tool_surface,
                        profile=self._tool_surface_profile,
                    )
                    # D-056 PR2: phase-based per-call thinking override. The
                    # policy returns RequestOptions (or None) based on the task
                    # phase (closing mode / research gathering / aggregation),
                    # which we set on the per-call contextvar for the duration
                    # of this LLM call. The provider merges it with model/
                    # sampling profiles. Independent of the queue/cache
                    # extra_body contextvar.
                    from corpclaw_lite.agent.phase_policy import PhaseContext

                    phase_ctx = PhaseContext(
                        is_workflow_subagent=state.mandate.enabled,
                        iteration=state.stats.iterations,
                        elapsed_ratio=state.mandate.elapsed_ratio(state.stats.iterations)
                        if state.mandate.enabled
                        else None,
                        closing_mode=state.soft_deadline.closing_mode,
                        nudge_injected=state.mandate.nudge_injected,
                        restricted=state.mandate.restricted,
                        prev_tool_calls=state.prev_turn_tools,
                        tools_used=state.stats.tools_used,
                        aggregation_markers=self._phase_aggregation_markers,
                        gathering_tools=self._phase_gathering_tools,
                    )
                    req_opts = self._phase_policy.options_for_phase(phase_ctx)
                    _phase_call_token = (
                        set_request_options(req_opts) if req_opts is not None else None
                    )
                    if req_opts is not None:
                        log_event(
                            "phase_changed",
                            state.stats.run_id,
                            iteration=state.stats.iterations,
                            prev_tools=state.prev_turn_tools,
                            thinking=(
                                req_opts.thinking.mode if req_opts.thinking is not None else None
                            ),
                            closing_mode=state.soft_deadline.closing_mode,
                            is_workflow_subagent=state.mandate.enabled,
                        )
                    try:
                        # When the provider is a queued router, separate queue wait
                        # from LLM inference so the state.budget only counts active time.
                        # Etap 3: uses effective_provider (depth override) instead of
                        # self._provider so Fast/Think applies to this run only.
                        is_router_queue = isinstance(effective_provider, LLMRouter)
                        if is_router_queue and effective_provider.has_queue:
                            state.budget.pause()

                            def on_router_acquired() -> None:
                                state.budget.resume()
                                emit_llm_status("model_preparing")

                            response = await effective_provider.call_default_with_slot(
                                user_id=str(user.id),
                                run_id=state.stats.run_id,
                                messages=state.context.messages,
                                tools=state.tools_schema,
                                system=state.context.system_prompt or None,
                                on_acquired=on_router_acquired,
                                call=lambda target_provider, _tools=state.tools_schema: (
                                    asyncio.wait_for(
                                        self._call_llm_provider(
                                            target_provider,
                                            messages=state.context.messages,
                                            tools=_tools,
                                            system=state.context.system_prompt or None,
                                            run_id=state.stats.run_id,
                                            iteration=state.stats.iterations,
                                            on_llm_stage=on_llm_stage,
                                            stats=state.stats,
                                        ),
                                        timeout=self._settings.llm_timeout_seconds,
                                    )
                                ),
                                on_queue_status=on_llm_queue_status,
                                notify_position=_queue_notify_position(self._settings),
                                notify_interval_seconds=_queue_notify_interval_seconds(
                                    self._settings
                                ),
                            )
                        elif isinstance(effective_provider, QueuedProvider):
                            state.budget.pause()

                            def on_queued_provider_acquired() -> None:
                                state.budget.resume()
                                emit_llm_status("model_preparing")

                            response = await effective_provider.call_with_slot(
                                messages=state.context.messages,
                                tools=state.tools_schema,
                                system=state.context.system_prompt or None,
                                on_acquired=on_queued_provider_acquired,
                                on_queue_status=on_llm_queue_status,
                                notify_position=_queue_notify_position(self._settings),
                                notify_interval_seconds=_queue_notify_interval_seconds(
                                    self._settings
                                ),
                                call=lambda target_provider, _tools=state.tools_schema: (
                                    asyncio.wait_for(
                                        self._call_llm_provider(
                                            target_provider,
                                            messages=state.context.messages,
                                            tools=_tools,
                                            system=state.context.system_prompt or None,
                                            run_id=state.stats.run_id,
                                            iteration=state.stats.iterations,
                                            on_llm_stage=on_llm_stage,
                                            stats=state.stats,
                                        ),
                                        timeout=self._settings.llm_timeout_seconds,
                                    )
                                ),
                            )
                        else:
                            target_provider: Provider = (
                                effective_provider.default
                                if isinstance(effective_provider, LLMRouter)
                                else effective_provider
                            )
                            emit_llm_status("model_preparing")
                            response = await asyncio.wait_for(
                                self._call_llm_provider(
                                    target_provider,
                                    messages=state.context.messages,
                                    tools=state.tools_schema,
                                    system=state.context.system_prompt or None,
                                    run_id=state.stats.run_id,
                                    iteration=state.stats.iterations,
                                    on_llm_stage=on_llm_stage,
                                    stats=state.stats,
                                ),
                                timeout=self._settings.llm_timeout_seconds,
                            )
                    finally:
                        if _phase_call_token is not None:
                            reset_request_options(_phase_call_token)
                except TimeoutError:
                    msg = "I could not get a response from the language model (timed out)."
                    await self._save_turn(state.mem_key, msg, state.stats.tools_used)
                    state.stats.status = "timeout"
                    state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
                    health.increment("llm_timeouts")
                    log_event(
                        "llm_call_finished",
                        state.stats.run_id,
                        iteration=state.stats.iterations,
                        status="timeout",
                        duration_ms=round((time.monotonic() - llm_t0) * 1000, 1),
                    )
                    log_event(
                        "request_finished",
                        state.stats.run_id,
                        status=state.stats.status,
                        iterations=state.stats.iterations,
                        tools_used=state.stats.tools_used,
                        duration_ms=round(state.stats.duration_ms, 1),
                        final_answer_len=len(msg),
                    )
                    logger.warning(
                        "[user=%s] LLM timeout on iteration %d", user.id, state.stats.iterations
                    )
                    return msg, state.stats
                except Exception as e:
                    health.increment("errors")
                    state.stats.status = "error"
                    state.stats.error = str(e)
                    state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
                    log_event(
                        "llm_call_finished",
                        state.stats.run_id,
                        iteration=state.stats.iterations,
                        status="error",
                        duration_ms=round((time.monotonic() - llm_t0) * 1000, 1),
                        error=type(e).__name__,
                    )
                    log_event(
                        "request_finished",
                        state.stats.run_id,
                        status=state.stats.status,
                        iterations=state.stats.iterations,
                        tools_used=state.stats.tools_used,
                        duration_ms=round(state.stats.duration_ms, 1),
                        final_answer_len=0,
                        error=state.stats.error,
                    )
                    raise

                state.stats.llm_calls += 1
                state.stats.input_tokens += response.usage.input_tokens
                state.stats.output_tokens += response.usage.output_tokens
                state.stats.total_tokens += response.usage.total_tokens
                state.stats.latest_total_tokens = response.usage.total_tokens
                state.last_actual_total_tokens = (
                    response.usage.total_tokens if response.usage.total_tokens > 0 else None
                )
                health.increment("llm_calls")
                log_event(
                    "llm_call_finished",
                    state.stats.run_id,
                    iteration=state.stats.iterations,
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
                    state.stats.iterations,
                    (response.content or "")[:200],
                    len(response.tool_calls or []),
                )

                # Log reasoning (if present) — does NOT enter agent state.context
                if response.reasoning:
                    logger.debug(
                        "[user=%s] reasoning (%d chars): %s",
                        user.id,
                        len(response.reasoning),
                        response.reasoning[:200],
                    )

                if not response.tool_calls:
                    # Degenerate-empty-response guard: if the model returned empty
                    # (or near-empty) content with no tool calls, it likely stuttered
                    # (gemma4 thinking-OFF after a tool result). Give it a bounded
                    # retry instead of exiting with "Agent provided no response".
                    if (
                        not response.content.strip()
                        and state.empty_response_retries < _EMPTY_RESPONSE_MAX_RETRIES
                    ):
                        state.empty_response_retries += 1
                        state.context.add_user_message(_EMPTY_RESPONSE_PROMPT)
                        log_event(
                            "empty_response_retry",
                            state.stats.run_id,
                            iteration=state.stats.iterations,
                            retries=state.empty_response_retries,
                        )
                        continue
                    # Final answer — ALWAYS return, even if time state.budget exceeded.
                    # The model already completed its work; discarding it wastes the
                    # entire LLM call and frustrates users who waited for a response.
                    final = response.content if response.content else "Agent provided no response."
                    if contains_xml_tool_call_markers(final):
                        if not state.xml_repair_attempted:
                            state.xml_repair_attempted = True
                            state.context.add_user_message(
                                build_xml_repair_prompt(
                                    "Raw XML tool-call markup was returned as assistant text "
                                    "instead of parsed tool calls."
                                )
                            )
                            log_event(
                                "xml_tool_call_repair_requested",
                                state.stats.run_id,
                                iteration=state.stats.iterations,
                                content_hash=_payload_hash(final),
                            )
                            continue
                        final = _XML_TOOL_CALL_FALLBACK
                        state.stats.status = "error"
                        state.stats.error = "malformed_xml_tool_call"
                    # B-056: planning-text / tool-artifact guard. If the final
                    # answer is a statement of intent ("Let me now...") or a
                    # Qwen3/Gemma tool-artifact ([tool:<name>]) instead of an
                    # action or real answer, inject a correction and give the
                    # model another turn — bounded by max_corrections.
                    if state.planning_guard.detect(final):
                        state.context.add_user_message(state.planning_guard.correction_message())
                        log_event(
                            "planning_text_blocked",
                            state.stats.run_id,
                            iteration=state.stats.iterations,
                            content_hash=_payload_hash(final),
                            corrections_used=state.planning_guard.corrections_used,
                        )
                        state.planning_guard.note_correction()
                        continue
                    if _is_loop_guard_echo(final):
                        final = _LOOP_FALLBACK
                        state.stats.status = "loop"
                        state.stats.error = "model_echoed_loop_guard"
                    # _save_turn writes to both SQLiteMemory (skipped if no memory)
                    # and the per-chat state.context store (B-063 S1). Called regardless
                    # of self._memory so state.context-store persistence always runs.
                    await self._save_turn(
                        state.mem_key,
                        final,
                        state.stats.tools_used,
                        response.reasoning,
                    )
                    if self._memory and self._consolidator:
                        await self._consolidator.maybe_consolidate(self._memory, state.mem_key)
                    state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
                    logger.debug(
                        "[user=%s] final_answer | len=%d | iterations=%d | duration_ms=%.0f",
                        user.id,
                        len(final),
                        state.stats.iterations,
                        state.stats.duration_ms,
                    )
                    log_event(
                        "request_finished",
                        state.stats.run_id,
                        status=state.stats.status,
                        iterations=state.stats.iterations,
                        tools_used=state.stats.tools_used,
                        duration_ms=round(state.stats.duration_ms, 1),
                        final_answer_len=len(final),
                        error=state.stats.error,
                    )
                    return final, state.stats

                # Model wants more work — check ALL state.budget limits before continuing.
                state.budget.check()
                state.budget.consume_tool_calls(len(response.tool_calls))

                # Agent requested tools — emit a single assistant message
                # containing both content (if any) and tool_calls.
                state.context.add_tool_calls(response.tool_calls, content=response.content or None)
                # B-063 S1: persist the assistant tool-call message (same schema as
                # ContextBuilder.add_tool_calls) to the per-chat state.context store.
                await self._persist_context_msg(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=[
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                    reasoning=response.reasoning,
                )
                health.increment("tool_calls", len(response.tool_calls))

                if self._can_parallelize(response.tool_calls):
                    results = await self._execute_parallel(
                        response.tool_calls,
                        user,
                        _approval_cb,
                        on_tool_start,
                        on_tool_batch_start,
                        on_subagent_tool_start,
                        on_subagent_tool_batch_start,
                        on_subagent_llm_stage,
                        on_subagent_llm_queue_status,
                        trajectory_recorder,
                        state.stats,
                        state.task_run,
                    )
                    # Add ALL results first to keep state.context valid (no orphaned tool_calls)
                    action_results: list[tuple[str, str]] = []
                    for tc, result in zip(response.tool_calls, results, strict=True):
                        state.context.add_tool_result(tc.id, tc.name, result)
                        await self._persist_context_msg(
                            role="tool", content=result, tool_call_id=tc.id, name=tc.name
                        )
                        state.stats.tools_used.append(tc.name)
                        state.current_turn_tools.append(tc.name)
                        action_results.append((tc.name, result))
                    # B-047 FIRST: the wall-clock deadline is time-critical and must
                    # always get a chance to nudge/restrict, even if the same tools
                    # keep returning identical results (B-055) or errors
                    # (SimpleProgressGuard). Without this ordering, a dedup/error
                    # loop would burn the whole state.budget before the state.mandate fires.
                    state.tools_schema = apply_workflow_mandate(
                        state.mandate, state.tools_schema, state.context, state.stats
                    )
                    # B-055: result-based dedup. Catches repeated identical
                    # successful results (the common loop mode for local LLMs).
                    # Only considers non-error results; error loops are handled
                    # below by SimpleProgressGuard.detect_loop_for_results.
                    dedup_tool, dedup_result = _detect_result_dedup(
                        state.result_dedup, action_results
                    )
                    if dedup_tool is not None:
                        _append_dedup_instruction(state.context)
                        log_event(
                            "dedup_result_triggered",
                            state.stats.run_id,
                            iteration=state.stats.iterations,
                            tool_name=dedup_tool,
                            result_hash=_payload_hash(dedup_result),
                            repeat_count=state.result_dedup.last_count(dedup_result),
                        )
                        continue
                    loop_detected = state.progress.detect_loop_for_results(action_results)
                    if loop_detected:
                        _append_loop_recovery_instruction(state.context)
                        state.loop_warning_count += 1
                        if state.loop_warning_count >= 2:
                            break
                        continue
                else:
                    action_results: list[tuple[str, str]] = []
                    for tc in response.tool_calls:
                        result = await self._execute_single_tool(
                            tc,
                            user,
                            _approval_cb,
                            on_tool_start,
                            on_subagent_tool_start,
                            on_subagent_tool_batch_start,
                            on_subagent_llm_stage,
                            on_subagent_llm_queue_status,
                            trajectory_recorder,
                            state.stats,
                            state.task_run,
                        )
                        state.context.add_tool_result(tc.id, tc.name, result)
                        # Terminal tool: return result directly (no LLM re-paraphrase).
                        # Used for tools like read_image where the vision model already
                        # produces a complete user-facing response.
                        tool_obj = self._registry.get(tc.name)
                        is_terminal = (
                            tool_obj is not None
                            and (
                                tool_obj.should_return_direct(tc.arguments, result)
                                if hasattr(tool_obj, "should_return_direct")
                                else getattr(tool_obj, "terminal", False)
                            )
                            and len(response.tool_calls) == 1
                            and not result.startswith(TOOL_ERROR_PREFIX)
                        )
                        # B-063 final-fix B1: for terminal tools, skip the tool-role
                        # persist — _save_turn below persists the result as the
                        # assistant's final answer, and duplicating it as tool-role
                        # inflates the restored state.context.
                        if not is_terminal:
                            await self._persist_context_msg(
                                role="tool",
                                content=result,
                                tool_call_id=tc.id,
                                name=tc.name,
                            )
                        state.stats.tools_used.append(tc.name)
                        state.current_turn_tools.append(tc.name)
                        action_results.append((tc.name, result))

                        if is_terminal:
                            await self._save_turn(state.mem_key, result, state.stats.tools_used)
                            if self._memory and self._consolidator:
                                await self._consolidator.maybe_consolidate(
                                    self._memory, state.mem_key
                                )
                            state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
                            logger.debug(
                                "[user=%s] terminal_tool=%s | returning result directly",
                                user.id,
                                tc.name,
                            )
                            log_event(
                                "request_finished",
                                state.stats.run_id,
                                status=state.stats.status,
                                iterations=state.stats.iterations,
                                tools_used=state.stats.tools_used,
                                duration_ms=round(state.stats.duration_ms, 1),
                                final_answer_len=len(result),
                            )
                            return result, state.stats

                    # B-047 FIRST (see parallel branch): wall-clock deadline wins
                    # over dedup/error-loop detection.
                    state.tools_schema = apply_workflow_mandate(
                        state.mandate, state.tools_schema, state.context, state.stats
                    )
                    # B-055: result-based dedup (sequential branch).
                    dedup_tool, dedup_result = _detect_result_dedup(
                        state.result_dedup, action_results
                    )
                    if dedup_tool is not None:
                        _append_dedup_instruction(state.context)
                        log_event(
                            "dedup_result_triggered",
                            state.stats.run_id,
                            iteration=state.stats.iterations,
                            tool_name=dedup_tool,
                            result_hash=_payload_hash(dedup_result),
                            repeat_count=state.result_dedup.last_count(dedup_result),
                        )
                        continue
                    loop_detected = state.progress.detect_loop_for_results(action_results)
                    if loop_detected:
                        _append_loop_recovery_instruction(state.context)
                        state.loop_warning_count += 1
                        if state.loop_warning_count >= 2:
                            break
                        continue

        except BudgetExceededError as e:
            health.increment("errors")
            # Auto-finalize cascade: if this is a workflow subagent with a
            # terminal tool that was never called, try to salvage the work
            # instead of returning a generic "state.budget exceeded" message.
            # B = one emergency LLM call; C = programmatic finalize fallback.
            if self._terminal_tool and not state.mandate.terminal_called(state.stats.tools_used):
                salvage = await self._auto_finalize_cascade(
                    state.context, state.stats, user, self._terminal_tool, e
                )
                if salvage is not None:
                    state.stats.status = "ok"
                    state.stats.error = None
                    await self._save_turn(state.mem_key, salvage, state.stats.tools_used)
                    state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
                    log_event(
                        "request_finished",
                        state.stats.run_id,
                        status="auto_finalized",
                        iterations=state.stats.iterations,
                        tools_used=state.stats.tools_used,
                        duration_ms=round(state.stats.duration_ms, 1),
                        final_answer_len=len(salvage),
                        budget_exceeded=str(e),
                    )
                    return salvage, state.stats
            # Fallback: generic state.budget message (non-salvageable).
            msg = f"I reached my resource limit and had to stop: {e}"
            await self._save_turn(state.mem_key, msg, state.stats.tools_used)
            state.stats.status = "budget"
            state.stats.error = str(e)
            state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
            logger.warning("[user=%s] budget exceeded: %s", user.id, e)
            log_event(
                "request_finished",
                state.stats.run_id,
                status=state.stats.status,
                iterations=state.stats.iterations,
                tools_used=state.stats.tools_used,
                duration_ms=round(state.stats.duration_ms, 1),
                final_answer_len=len(msg),
                error=state.stats.error,
            )
            return msg, state.stats
        finally:
            self._finalize_turn(tokens)

        fallback = _LOOP_FALLBACK
        await self._save_turn(state.mem_key, fallback, state.stats.tools_used)
        state.stats.status = "loop"
        state.stats.duration_ms = (time.monotonic() - state.t0) * 1000
        logger.warning(
            "[user=%s] loop detected after %d iterations",
            user.id,
            state.stats.iterations,
        )
        log_event(
            "request_finished",
            state.stats.run_id,
            status=state.stats.status,
            iterations=state.stats.iterations,
            tools_used=state.stats.tools_used,
            duration_ms=round(state.stats.duration_ms, 1),
            final_answer_len=len(fallback),
        )
        return fallback, state.stats

    async def _build_turn_context(
        self,
        *,
        user: User,
        message: str,
        system_prompt: str | None,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None,
        on_llm_stage: Callable[[str], None] | None,
        tools_enabled: bool,
        few_shots: list[dict[str, Any]] | None,
        channel: str | None,
        run_id: str | None,
        depth_mode: DepthMode | None,
        session_id: int | None,
        tokens: TurnTokens,
    ) -> tuple[
        LoopState,
        Provider,
        Callable[[str, str], Awaitable[bool]] | None,
        Callable[[str], None],
    ]:
        """B-077 prologue: history, prompt, LoopState, contextvars, user persist.

        Populates ``tokens`` in place so ``_finalize_turn`` can reset even if a
        later step fails. Pure move-and-name from the pre-B-077 ``run()`` setup.
        """
        stats = RunStats(run_id=run_id) if run_id is not None else RunStats()
        t0 = time.monotonic()

        # Etap 3 (Sprint 3A): resolve a depth-mode override provider for this run.
        # Fast/Think map to a per-model SamplingProfile name; with_overrides
        # rebuilds the default-route provider with that profile (thinking_mode +
        # inference_overrides). The override is a RUN-SCOPE local — it never
        # mutates self._provider, so concurrent runs on this shared loop are
        # isolated. When depth_mode is None or resolution fails, the route's
        # default provider is used unchanged.
        effective_provider: Provider = self._provider
        if depth_mode is not None and isinstance(self._provider, LLMRouter):
            effective_provider = self._apply_depth_override(self._provider, depth_mode)

        def emit_llm_status(stage: str) -> None:
            if self._settings.llm_stream_status_updates and on_llm_stage is not None:
                on_llm_stage(stage)

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
        approval_cb = (
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

        # B-063 S2: prefer the FULL LLM context (tool_calls/tool-role) from the
        # per-chat store when a session_id is bound and the store has data. Falls
        # back to the SQLiteMemory history above (role+content only) for old chats
        # or non-web channels. The full-history path reconstructs tool_calls and
        # tool-role messages that get_history/build_initial would drop.
        full_history: list[dict[str, Any]] | None = None
        if self._chat_context_store is not None and session_id is not None:
            try:
                full_history = await self._chat_context_store.list_context(session_id)
                if not full_history:
                    full_history = None
            except Exception:
                logger.warning("[session=%s] context-store load failed", session_id, exc_info=True)
                full_history = None

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

        # B-040: inject recently-touched files so the agent has cross-session
        # memory of what the user worked on.
        recent_files_block = ""
        recent_files_count = 0
        if self._file_change_dao is not None:
            try:
                recent_changes = await self._file_change_dao.list_recent_for_user(mem_key, limit=3)
            except StorageError:
                logger.error("[user=%s] Failed to load recent files", user.id)
                recent_changes = []
            if recent_changes:
                recent_files_count = len(recent_changes)
                lines = [f"- {c.file_path} ({c.tool_name})" for c in recent_changes]
                recent_files_block = "\n\n## Recently Touched Files\n" + "\n".join(lines)

        dynamic_prompt = (
            f"Current User Context:\n"
            f"- Name: {user.name}\n"
            f"- Department: {user.department}\n"
            f"{user_facts_block}{recent_files_block}\n\n"
            f"{base_prompt}"
        )

        if full_history is not None:
            # B-063 S2: full-context path — reconstructs tool_calls + tool-role.
            context = ContextBuilder.build_from_full_history(
                user,
                message,
                full_history,
                system_prompt_override=dynamic_prompt,
                few_shots=few_shots,
            )
        else:
            context = ContextBuilder.build_initial(
                user,
                message,
                history=history,
                system_prompt_override=dynamic_prompt,
                few_shots=few_shots,
            )

        if self._memory:
            await self._save_memory(mem_key, "user", message)

        # Budget is ALWAYS from settings. Department-specific iteration/tool-call
        # limits were removed — they silently overrode settings.max_steps, causing
        # "config change has no effect" bugs (the operator changes settings.yaml
        # but the department budget wins). RBAC (tools, subagents, skills) remains
        # department-scoped; only resource limits are now global.
        guard_config = SimpleBudgetGuardConfig(
            max_iterations=self._settings.max_steps,
            max_tool_calls=self._settings.max_tool_calls,
            max_time_ms=self._settings.max_wall_time_ms,
        )
        # B-047: workflow-finalize guard. Neutral when no terminal tool is configured
        # (main agent, non-research subagents); active for research-agent.
        mandate = TerminalToolMandate(
            TerminalToolMandateConfig(
                terminal_tool=self._terminal_tool or "",
                required_before=tuple(self._required_before_terminal),
            ),
            max_time_ms=self._settings.max_wall_time_ms,
            max_iterations=guard_config.max_iterations,
        )
        task_run = TaskRun(self._workspace_base)
        await task_run.initialize(user, stats.run_id)
        tools_schema: list[dict[str, Any]] | None = None
        if tools_enabled:
            if self._permission_checker:
                tools_schema = self._registry.to_schemas_for_user(
                    self._permission_checker,
                    user,
                    enforce_tool_allowlist=self._enforce_tool_permissions,
                )
            else:
                tools_schema = self._registry.to_schemas()
        # B-076/B-077: pack run-scoped mutable state into an explicit bag.
        # base_tools_schema is the immutable source of truth for schema refilters.
        state = LoopState(
            stats=stats,
            budget=SimpleBudgetGuard(guard_config),
            progress=SimpleProgressGuard(),
            # B-055: result-based dedup (success loops); config from AgentSettings.
            result_dedup=ResultDedupGuard(self._settings.result_dedup_guard),
            # B-056: planning-text guard.
            planning_guard=PlanningTextGuard(self._settings.planning_text_guard),
            soft_deadline=SoftDeadline(
                SoftDeadlineConfig(ratio=self._settings.soft_deadline_ratio),
                max_time_ms=self._settings.max_wall_time_ms,
            ),
            mandate=mandate,
            context=context,
            base_tools_schema=list(tools_schema) if tools_schema is not None else None,
            tools_schema=tools_schema,
            task_run=task_run,
            mem_key=mem_key,
            t0=t0,
        )
        health.increment("requests")
        health.increment("active_requests")
        tokens.active_request_counted = True
        log_event(
            "context_built",
            state.stats.run_id,
            history_count=len(history),
            facts_count=facts_count,
            recent_files_count=recent_files_count,
            tools_available_count=len(state.tools_schema or []),
            system_prompt_chars=len(state.context.system_prompt or ""),
            message_count=state.context.message_count,
        )

        # Etap 3B: set the depth-mode contextvar FIRST, so the epilogue
        # resets it no matter what. Tokens live on ``tokens`` (B-077).
        tokens.depth = set_call_depth_mode(depth_mode) if depth_mode is not None else None
        # B-063 S1 audit: bind the state.context-persist target (session_id, user_id)
        # via contextvars so concurrent runs are isolated. Reset in epilogue.
        tokens.context_target = set_context_target(session_id, str(user.id))
        # B-063 S4: populate capture-correlation contextvars so payload
        # captures carry user_id + session_id + run_id.
        tokens.capture = set_capture_context(str(user.id), session_id)
        tokens.run_id = set_run_id(state.stats.run_id)
        # DC-017 / B-098: bind per-user workspace so file tools do not share
        # process cwd across users in host mode. Path-validated tools only;
        # shell absolute paths still need container isolation (DC-016).
        from corpclaw_lite.extensions.tools.builtin._path_utils import (
            user_workspace_path,
        )
        from corpclaw_lite.paths import PROJECT_ROOT

        _ws_base = self._workspace_base or (PROJECT_ROOT / "workspaces")
        _user_ws = user_workspace_path(_ws_base, user)
        _user_ws.mkdir(parents=True, exist_ok=True)
        tokens.workspace = set_workspace_root(_user_ws)
        # Persist the user message now that the state.context-target is bound.
        await self._persist_context_msg(role="user", content=message)

        return state, effective_provider, approval_cb, emit_llm_status

    def _finalize_turn(self, tokens: TurnTokens) -> None:
        """B-077 epilogue: health counter + contextvar resets for one run."""
        if tokens.active_request_counted:
            health.increment("active_requests", -1)
        if tokens.depth is not None:
            reset_call_depth_mode(tokens.depth)
        if tokens.context_target is not None:
            reset_context_target(tokens.context_target)
        if tokens.capture is not None:
            reset_capture_context(tokens.capture)
        if tokens.run_id is not None:
            reset_run_id(tokens.run_id)
        if tokens.workspace is not None:
            reset_workspace_root(tokens.workspace)

    async def _save_memory(self, mem_key: str, role: str, content: str, **kwargs: Any) -> None:
        """Persist a message to memory, swallowing StorageError."""
        if not self._memory:
            return
        try:
            await self._memory.add_message(mem_key, role, content, **kwargs)
        except StorageError:
            logger.error("[user=%s] Failed to save %s message", mem_key, role)

    async def _persist_context_msg(
        self,
        *,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        reasoning: str | None = None,
    ) -> None:
        """Append one LLM-facing message to the per-chat context store (B-063 S1).

        Non-fatal: a persist failure is logged at DEBUG but does NOT abort the run.
        No-op when no store is configured or when ``session_id`` is None
        (telegram/CLI/subagents — they have no web chat session concept).
        """
        session_id = get_context_session_id()
        user_id = get_context_user_id()
        if self._chat_context_store is None or session_id is None or user_id is None:
            return
        try:
            await self._chat_context_store.append_context(
                session_id=session_id,
                user_id=user_id,
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                name=name,
                reasoning=reasoning,
            )
        except Exception:
            logger.debug(
                "[session=%s] chat_context persist failed (non-fatal)",
                session_id,
                exc_info=True,
            )

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
        # B-063 S1: persist the final assistant answer to the per-chat context
        # store. Only the raw model reasoning (no synthetic tool-marker) — the
        # context store mirrors what the model actually produced, not the audit
        # annotation SQLiteMemory adds.
        await self._persist_context_msg(
            role="assistant", content=content, reasoning=response_reasoning
        )

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

    async def _auto_finalize_cascade(
        self,
        context: ContextBuilder,
        stats: RunStats,
        user: User,
        terminal_tool: str,
        error: BudgetExceededError,
    ) -> str | None:
        """Salvage accumulated work when a workflow subagent exhausts its budget
        without calling the mandatory terminal tool.

        Two-stage cascade (each stage is a safety net for the previous):
          B — one LLM "synthesize now" call with schema=[terminal_tool] only and
              an emergency prompt. If the model calls the terminal tool, execute
              it and return its result.
          C — if B returns text (no tool call) or fails, programmatically call
              the terminal tool with the model's text (or empty) as the answer.

        Returns the finalized result string, or None if both stages fail (caller
        falls back to the generic budget-exceeded message). Only for subagents
        with a configured terminal_tool; the main agent never enters this path.
        """
        emergency = _AUTO_FINALIZE_EMERGENCY_PROMPT.format(terminal=terminal_tool)
        context.add_user_message(emergency)

        # Build a schema containing ONLY the terminal tool — the model has no
        # other choice but to finalize (or return plain text → C handles it).
        terminal_schema = [
            s
            for s in self._registry.to_schemas()
            if str(s.get("function", {}).get("name", "")) == terminal_tool
        ]
        if not terminal_schema:
            logger.warning(
                "[user=%s] auto-finalize: terminal tool '%s' not in registry",
                user.id,
                terminal_tool,
            )
            return None

        # ── Stage B: one emergency LLM call ─────────────────────────────────
        try:
            log_event(
                "auto_finalize_llm_call",
                stats.run_id,
                terminal_tool=terminal_tool,
                budget_error=str(error),
            )
            if isinstance(self._provider, LLMRouter) and self._provider.has_queue:
                # Route through the queue so this LLM call is slot-bounded and
                # accounted for (previously it bypassed the queue via
                # _resolve_target_provider, adding un-bounded load when many
                # subagents exhaust their budget at once). The budget is already
                # exhausted here, so there is no pause/resume; on_acquired=None.
                # task_kind="default"/load_class="interactive" are hardcoded in
                # call_default_with_slot → sticky-eligible (user slot).
                response = await self._provider.call_default_with_slot(
                    user_id=str(user.id),
                    run_id=stats.run_id,
                    messages=context.messages,
                    tools=terminal_schema,
                    system=context.system_prompt or None,
                    on_acquired=None,
                    call=lambda target_provider: asyncio.wait_for(
                        self._call_llm_provider(
                            target_provider,
                            messages=context.messages,
                            tools=terminal_schema,
                            system=context.system_prompt or None,
                            run_id=stats.run_id,
                            iteration=stats.iterations + 1,
                            on_llm_stage=None,
                            stats=None,
                        ),
                        timeout=self._settings.llm_timeout_seconds,
                    ),
                    on_queue_status=None,
                    notify_position=_queue_notify_position(self._settings),
                    notify_interval_seconds=_queue_notify_interval_seconds(self._settings),
                )
            else:
                # Fallback: no queue (bare QueuedProvider or non-router) — raw call.
                response = await asyncio.wait_for(
                    self._call_llm_provider(
                        self._resolve_target_provider(),
                        messages=context.messages,
                        tools=terminal_schema,
                        system=context.system_prompt or None,
                        run_id=stats.run_id,
                        iteration=stats.iterations + 1,
                        on_llm_stage=None,
                        stats=None,
                    ),
                    timeout=self._settings.llm_timeout_seconds,
                )
        except TimeoutError:
            # B-073: stage-B emergency call timed out (local LLM hang). Fall
            # through to programmatic finalize (stage C) instead of blocking
            # indefinitely past the budget. Parity with the main-loop timeout
            # telemetry (llm_timeouts health counter).
            health.increment("llm_timeouts")
            logger.warning(
                "[user=%s] auto-finalize stage B LLM call timed out"
                " (%.1fs); falling back to programmatic finalize",
                user.id,
                self._settings.llm_timeout_seconds,
            )
            response = None
        except Exception:
            logger.warning(
                "[user=%s] auto-finalize stage B (LLM call) failed; "
                "falling back to programmatic finalize",
                user.id,
                exc_info=True,
            )
            response = None

        # If B produced a terminal tool call, execute it directly.
        if response and response.tool_calls:
            tc = response.tool_calls[0]
            if tc.name == terminal_tool:
                try:
                    result = await self._execute_single_tool(
                        tc, user, None, None, None, None, None, None, None, stats, None
                    )
                    logger.info(
                        "[user=%s] auto-finalize stage B: model called %s, result_len=%d",
                        user.id,
                        terminal_tool,
                        len(result),
                    )
                    return result
                except Exception:
                    logger.warning(
                        "[user=%s] auto-finalize stage B: terminal tool execute "
                        "failed; falling back to programmatic",
                        user.id,
                        exc_info=True,
                    )

        # ── Stage C: programmatic finalize ──────────────────────────────────
        emergency_answer = (response.content if response and response.content else "") or ""
        tool = self._registry.get(terminal_tool)
        if tool is None:
            logger.warning(
                "[user=%s] auto-finalize: terminal tool '%s' not found in registry",
                user.id,
                terminal_tool,
            )
            return None
        try:
            log_event(
                "auto_finalize_programmatic",
                stats.run_id,
                terminal_tool=terminal_tool,
                emergency_answer_len=len(emergency_answer),
            )
            result = await tool.execute(user=user, answer=emergency_answer)
            logger.info(
                "[user=%s] auto-finalize stage C: programmatic %s, result_len=%d",
                user.id,
                terminal_tool,
                len(result),
            )
            return result
        except Exception:
            logger.warning(
                "[user=%s] auto-finalize stage C: programmatic finalize failed",
                user.id,
                exc_info=True,
            )
            return None

    def _resolve_target_provider(self) -> Provider:
        """Resolve the raw provider for a direct (non-queued) LLM call.

        Used by auto-finalize where budget is already exhausted — no queue
        accounting needed, just the inner provider.
        """
        provider = self._provider
        if isinstance(provider, LLMRouter):
            return provider.default or provider
        if isinstance(provider, QueuedProvider):
            return provider._provider  # type: ignore[attr-defined]
        return provider

    def _apply_depth_override(self, router: LLMRouter, depth: DepthMode) -> Provider:
        """Build a depth-mode override router (Etap 3).

        Resolves the default route's model, looks up the depth→sampling-profile
        mapping for that model, and rebuilds the default route with the profile
        via ``router.with_overrides``. On any failure (missing mapping/profile/
        registries) returns the original router unchanged so the run is never
        broken by a depth override.
        """
        if (
            self._depth_modes is None
            or self._provider_registry is None
            or self._preset_registry is None
        ):
            return router
        # Recover the default route's model from the router's provider meta, or
        # fall back to the provider's _model attribute.
        default_provider = router.default
        route_model = getattr(default_provider, "_model", None)
        if not route_model:
            logger.warning(
                "Cannot apply depth override '%s': default provider has no model attribute.",
                depth,
            )
            return router
        sampling_name = resolve_depth_sampling(
            depth, str(route_model), self._depth_modes, self._preset_registry
        )
        if sampling_name is None:
            return router
        try:
            overridden = router.with_overrides(
                provider_registry=self._provider_registry,
                preset_registry=self._preset_registry,
                sampling_name=sampling_name,
                apply_to="default_only",
            )
        except Exception:
            logger.exception("Failed to apply depth override '%s'; using default route", depth)
            return router
        logger.info(
            "[depth=%s] override sampling='%s' for model='%s'",
            depth,
            sampling_name,
            route_model,
        )
        return overridden

    async def _execute_parallel(
        self,
        tool_calls: list[ToolCall],
        user: User,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None,
        on_tool_start: Callable[[str], None] | None,
        on_tool_batch_start: Callable[[list[str]], None] | None,
        on_subagent_tool_start: Callable[[str, str], None] | None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None,
        on_subagent_llm_stage: Callable[[str, str], None] | None,
        on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None,
        trajectory_recorder: TrajectoryRecorder | None = None,
        stats: RunStats | None = None,
        task_run: TaskRun | None = None,
    ) -> list[str]:
        """Execute multiple tools in parallel and return results."""
        if on_tool_batch_start is not None:
            on_tool_batch_start([tc.name for tc in tool_calls])

        async def execute_one(tc: ToolCall) -> str:
            return await self._execute_single_tool(
                tc,
                user,
                approval_callback,
                None,
                on_subagent_tool_start,
                on_subagent_tool_batch_start,
                on_subagent_llm_stage,
                on_subagent_llm_queue_status,
                trajectory_recorder,
                stats,
                task_run,
            )

        results = await asyncio.gather(*[execute_one(tc) for tc in tool_calls])
        return list(results)

    async def _execute_single_tool(
        self,
        tc: ToolCall,
        user: User,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None,
        on_tool_start: Callable[[str], None] | None,
        on_subagent_tool_start: Callable[[str, str], None] | None = None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
        on_subagent_llm_stage: Callable[[str, str], None] | None = None,
        on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None,
        trajectory_recorder: TrajectoryRecorder | None = None,
        stats: RunStats | None = None,
        task_run: TaskRun | None = None,
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
        permission_tool = self._registry.get(tc.name)
        permission_denied = False
        if self._permission_checker:
            if permission_tool is not None:
                permission_denied = not self._permission_checker.can_use_registered_tool(
                    user,
                    permission_tool,
                    enforce_tool_allowlist=self._enforce_tool_permissions,
                )
            elif self._enforce_tool_permissions:
                permission_denied = not self._permission_checker.can_use_tool(user, tc.name)

        if permission_denied:
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
                check_params = inspect.signature(guard_check).parameters
                if "user_id" in check_params:
                    await guard_check(
                        tc.name,
                        tc.arguments,
                        risk_level=risk,
                        run_id=run_id,
                        user_id=str(user.id),
                    )
                elif "run_id" in check_params:
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
                permission_checker=self._permission_checker,
                enforce_tool_allowlist=self._enforce_tool_permissions,
                on_subagent_tool_start=on_subagent_tool_start,
                on_subagent_tool_batch_start=on_subagent_tool_batch_start,
                on_subagent_llm_stage=on_subagent_llm_stage,
                on_subagent_llm_queue_status=on_subagent_llm_queue_status,
                parent_trajectory_recorder=trajectory_recorder,
            )
            status = "error" if result.startswith("Error") else "ok"
            if status == "error":
                health.increment("tool_errors")
            if task_run is not None:
                await task_run.record_tool_call(
                    user,
                    run_id,
                    name=tc.name,
                    args_hash=_payload_hash(tc.arguments),
                    status=status,
                    duration_ms=(time.monotonic() - tool_t0) * 1000,
                    error=result if status == "error" else None,
                )

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
                # Per-user lock: serializes parallel approval prompts for ONE user (avoids
                # confusing multiple Approve/Deny buttons), but different users are independent.
                async with self._get_approval_lock(user.id):
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
                        permission_checker=self._permission_checker,
                        enforce_tool_allowlist=self._enforce_tool_permissions,
                        on_subagent_tool_start=on_subagent_tool_start,
                        on_subagent_tool_batch_start=on_subagent_tool_batch_start,
                        on_subagent_llm_stage=on_subagent_llm_stage,
                        on_subagent_llm_queue_status=on_subagent_llm_queue_status,
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
