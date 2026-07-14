"""B-073 / B-047 auto-finalize cascade (adaptation layer).

Moved from :class:`~corpclaw_lite.agent.loop.AgentLoop` (B-078 / PR 2A.2).

When a workflow subagent exhausts its budget without calling the mandatory
terminal tool, salvage work via:

* **Stage B** — one emergency LLM call with schema restricted to the terminal tool.
* **Stage C** — programmatic ``terminal_tool.execute(answer=...)`` if B fails.

**Never re-raises** (B-073): any exception/timeout falls through to the next stage
or returns ``None`` so the loop can emit a generic budget message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from corpclaw_lite.llm.base import LLMResponse, Provider, ToolCall
from corpclaw_lite.llm.router import LLMRouter, QueuedProvider
from corpclaw_lite.logging import health
from corpclaw_lite.logging.trace import log_event

if TYPE_CHECKING:
    from corpclaw_lite.agent.context import ContextBuilder
    from corpclaw_lite.agent.loop import RunStats
    from corpclaw_lite.extensions.tools.registry import ToolRegistry
    from corpclaw_lite.users.models import User

logger = logging.getLogger(__name__)

__all__ = [
    "AUTO_FINALIZE_EMERGENCY_PROMPT",
    "auto_finalize_cascade",
    "resolve_target_provider",
]

# Injected when the budget is fully exhausted and the terminal tool was never
# called. Last chance to salvage work — one LLM call (B), then programmatic
# finalize (C) if the model still does not cooperate.
AUTO_FINALIZE_EMERGENCY_PROMPT = (
    "You have run out of iterations and MUST finalize now. Do not call any tool "
    "except {terminal}. Synthesize a complete report from all the evidence and "
    "facts you have gathered so far. Call {terminal} with the full Markdown "
    "report as the 'answer' argument. If your evidence is incomplete, say so "
    "honestly in a limitations section — but you MUST finalize now."
)

# Bound to AgentLoop._call_llm_provider shape (provider-first).
CallLlmFn = Callable[..., Awaitable[LLMResponse]]
# (tool_call, user, stats) → tool result string
ExecuteToolCallFn = Callable[[ToolCall, "User", "RunStats"], Awaitable[str]]


def resolve_target_provider(provider: Provider) -> Provider:
    """Resolve the raw provider for a direct (non-queued) LLM call.

    Used by auto-finalize where budget is already exhausted — no queue
    accounting needed, just the inner provider.
    """
    if isinstance(provider, LLMRouter):
        return provider.default or provider
    if isinstance(provider, QueuedProvider):
        return provider._provider  # type: ignore[attr-defined]
    return provider


async def auto_finalize_cascade(
    context: ContextBuilder,
    stats: RunStats,
    user: User,
    terminal_tool: str,
    error: BaseException,
    *,
    registry: ToolRegistry,
    provider: Provider,
    llm_timeout_seconds: float,
    notify_position: bool,
    notify_interval_seconds: float,
    call_llm: CallLlmFn,
    execute_tool_call: ExecuteToolCallFn,
) -> str | None:
    """Salvage accumulated work when a workflow subagent exhausts its budget.

    Two-stage cascade (each stage is a safety net for the previous):
      B — one LLM "synthesize now" call with schema=[terminal_tool] only and
          an emergency prompt. If the model calls the terminal tool, execute
          it and return its result.
      C — if B returns text (no tool call) or fails, programmatically call
          the terminal tool with the model's text (or empty) as the answer.

    Returns the finalized result string, or None if both stages fail (caller
    falls back to the generic budget-exceeded message). Only for subagents
    with a configured terminal_tool; the main agent never enters this path.

    Never re-raises (B-073).
    """
    emergency = AUTO_FINALIZE_EMERGENCY_PROMPT.format(terminal=terminal_tool)
    context.add_user_message(emergency)

    # Build a schema containing ONLY the terminal tool — the model has no
    # other choice but to finalize (or return plain text → C handles it).
    terminal_schema = [
        s
        for s in registry.to_schemas()
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
    response: LLMResponse | None
    try:
        log_event(
            "auto_finalize_llm_call",
            stats.run_id,
            terminal_tool=terminal_tool,
            budget_error=str(error),
        )
        if isinstance(provider, LLMRouter) and provider.has_queue:
            # Route through the queue so this LLM call is slot-bounded and
            # accounted for (previously it bypassed the queue via
            # resolve_target_provider, adding un-bounded load when many
            # subagents exhaust their budget at once). The budget is already
            # exhausted here, so there is no pause/resume; on_acquired=None.
            # task_kind="default"/load_class="interactive" are hardcoded in
            # call_default_with_slot → sticky-eligible (user slot).
            response = await provider.call_default_with_slot(
                user_id=str(user.id),
                run_id=stats.run_id,
                messages=context.messages,
                tools=terminal_schema,
                system=context.system_prompt or None,
                on_acquired=None,
                call=lambda target_provider: asyncio.wait_for(
                    call_llm(
                        target_provider,
                        messages=context.messages,
                        tools=terminal_schema,
                        system=context.system_prompt or None,
                        run_id=stats.run_id,
                        iteration=stats.iterations + 1,
                        on_llm_stage=None,
                        stats=None,
                    ),
                    timeout=llm_timeout_seconds,
                ),
                on_queue_status=None,
                notify_position=notify_position,
                notify_interval_seconds=notify_interval_seconds,
            )
        else:
            # Fallback: no queue (bare QueuedProvider or non-router) — raw call.
            response = await asyncio.wait_for(
                call_llm(
                    resolve_target_provider(provider),
                    messages=context.messages,
                    tools=terminal_schema,
                    system=context.system_prompt or None,
                    run_id=stats.run_id,
                    iteration=stats.iterations + 1,
                    on_llm_stage=None,
                    stats=None,
                ),
                timeout=llm_timeout_seconds,
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
            llm_timeout_seconds,
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
                result = await execute_tool_call(tc, user, stats)
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
    tool = registry.get(terminal_tool)
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
