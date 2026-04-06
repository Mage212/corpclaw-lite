from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.agent.guards import (
    BudgetExceededError,
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
    SimpleProgressGuard,
)
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import Provider, ToolCall
from corpclaw_lite.logging import health
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuardError
from corpclaw_lite.users.models import User

__all__ = [
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


@dataclass
class RunStats:
    """Metrics for a single AgentLoop.run() call.

    Returned alongside the final answer so callers (runner, CLI) can log
    or display execution details without reading internal state.
    """

    iterations: int = 0
    tools_used: list[str] = field(default_factory=list[str])
    duration_ms: float = 0.0
    # "ok" | "budget" | "loop" | "timeout" | "error"
    status: str = "ok"
    error: str | None = None


class AgentLoop:
    """Core ReAct loop for agent execution."""

    def __init__(
        self,
        provider: Provider,
        registry: ToolRegistry,
        settings: AgentSettings,
        permission_checker: PermissionChecker | None = None,
        tool_guard: ToolGuard | None = None,
        memory: SQLiteMemory | None = None,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None = None,
        consolidator: MemoryConsolidator | None = None,
        compressor: ContextCompressor | None = None,
        default_system_prompt: str | None = None,
    ):
        self._provider = provider
        self._registry = registry
        self._settings = settings
        self._permission_checker = permission_checker
        self._tool_guard = tool_guard
        self._memory = memory
        self._approval_callback = approval_callback
        self._consolidator = consolidator
        self._compressor = compressor
        self._default_system_prompt = default_system_prompt
        self._approval_lock = asyncio.Lock()

    @property
    def memory(self) -> SQLiteMemory | None:
        """Access the memory backend (if configured)."""
        return self._memory

    @property
    def provider(self) -> Provider:
        """Access the LLM provider."""
        return self._provider

    async def run(
        self,
        user: User,
        message: str,
        system_prompt: str | None = None,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
        tools_enabled: bool = True,
        trajectory_recorder: TrajectoryRecorder | None = None,
    ) -> tuple[str, RunStats]:
        """Run the ReAct loop until a final answer is given or limits are reached.

        Returns:
            (reply, stats) — the agent's final answer and execution metrics.
        """
        stats = RunStats()
        t0 = time.monotonic()

        logger.debug(
            "[user=%s] run() start | msg=%r",
            user.id,
            message[:120],
        )

        # Per-call callback takes priority over the instance-level default
        _approval_cb = (
            approval_callback if approval_callback is not None else self._approval_callback
        )

        # Memory key: use telegram_id where available for consistency with the
        # onboarding finalizer which stores facts under telegram_id, not the
        # internal DB id. Falling back to internal id keeps CLI mode working.
        mem_key = str(user.telegram_id) if user.telegram_id else str(user.id)

        # Load history BEFORE building context so it precedes the current message
        history: list[dict[str, Any]] = []
        if self._memory:
            history = await self._memory.get_history(mem_key, limit=self._settings.max_history)

        # Prepend dynamic user context to the system prompt
        base_prompt = system_prompt or self._default_system_prompt or ""

        # Load user facts from memory (onboarding + manually stored via memory_store)
        user_facts_block = ""
        if self._memory:
            facts = await self._memory.recall_facts(mem_key, limit=20)
            if facts:
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
        )

        # Save new user message
        if self._memory:
            await self._memory.add_message(mem_key, "user", message)

        # Get budget from department if permission checker is available
        guard_config = (
            self._permission_checker.get_budget(user)
            if self._permission_checker
            else SimpleBudgetGuardConfig(
                max_iterations=self._settings.max_steps,
                max_tool_calls=self._settings.max_tool_calls,
                max_time_ms=self._settings.max_wall_time_ms,
            )
        )
        budget = SimpleBudgetGuard(guard_config)
        progress = SimpleProgressGuard()
        tools_schema = self._registry.to_schemas() if tools_enabled else None
        health.increment("requests")

        try:
            while True:
                budget.check()
                budget.consume_iteration()
                stats.iterations += 1

                compression_cfg = self._settings.compression
                if compression_cfg.enabled and context.message_count > (
                    compression_cfg.prune_min_messages
                ):
                    context.prune_old_tool_results(protect_tail=6)

                if self._compressor and self._compressor.should_compress(context.messages):
                    context.messages = await self._compressor.compress(context.messages)

                try:
                    response = await asyncio.wait_for(
                        self._provider.chat(
                            messages=context.messages,
                            tools=tools_schema,
                            system=context.system_prompt or None,
                        ),
                        timeout=120,
                    )
                except TimeoutError:
                    msg = "I could not get a response from the language model (timed out)."
                    if self._memory:
                        await self._memory.add_message(str(user.id), "assistant", msg)
                    stats.status = "timeout"
                    stats.duration_ms = (time.monotonic() - t0) * 1000
                    logger.warning(
                        "[user=%s] LLM timeout on iteration %d", user.id, stats.iterations
                    )
                    return msg, stats

                logger.debug(
                    "[user=%s] llm_response iter=%d | content=%r | tool_calls=%d",
                    user.id,
                    stats.iterations,
                    (response.content or "")[:200],
                    len(response.tool_calls or []),
                )

                if not response.tool_calls:
                    # Agent provided text directly — save and return
                    final = response.content if response.content else "Agent provided no response."
                    if self._memory:
                        await self._memory.add_message(mem_key, "assistant", final)
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
                    return final, stats

                # Agent requested tools — emit a single assistant message
                # containing both content (if any) and tool_calls.
                context.add_tool_calls(response.tool_calls, content=response.content or None)
                budget.consume_tool_calls(len(response.tool_calls))
                health.increment("tool_calls", len(response.tool_calls))

                if self._can_parallelize(response.tool_calls):
                    results = await self._execute_parallel(
                        response.tool_calls,
                        user,
                        _approval_cb,
                        on_tool_start,
                        trajectory_recorder,
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
                        break
                else:
                    should_stop = False
                    for tc in response.tool_calls:
                        result = await self._execute_single_tool(
                            tc,
                            user,
                            _approval_cb,
                            on_tool_start,
                            trajectory_recorder,
                        )
                        context.add_tool_result(tc.id, tc.name, result)
                        stats.tools_used.append(tc.name)
                        if progress.detect_loop(tc.name, result):
                            context.add_assistant_message(
                                "System Guard: You seem to be stuck in a loop repeating the same"
                                " error. Please change your strategy or stop using this tool."
                            )
                            should_stop = True
                            break

                    if should_stop:
                        break

        except BudgetExceededError as e:
            health.increment("errors")
            msg = f"I reached my resource limit and had to stop: {e}"
            if self._memory:
                await self._memory.add_message(mem_key, "assistant", msg)
            stats.status = "budget"
            stats.error = str(e)
            stats.duration_ms = (time.monotonic() - t0) * 1000
            logger.warning("[user=%s] budget exceeded: %s", user.id, e)
            return msg, stats

        fallback = "I detected a loop and stopped to avoid repeating the same actions."
        if self._memory:
            await self._memory.add_message(mem_key, "assistant", fallback)
        stats.status = "loop"
        stats.duration_ms = (time.monotonic() - t0) * 1000
        logger.warning("[user=%s] loop detected after %d iterations", user.id, stats.iterations)
        return fallback, stats

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
    ) -> list[str]:
        """Execute multiple tools in parallel and return results."""

        async def execute_one(tc: ToolCall) -> str:
            return await self._execute_single_tool(
                tc,
                user,
                approval_callback,
                on_tool_start,
                trajectory_recorder,
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
    ) -> str:
        """Execute a single tool with all checks."""
        if self._permission_checker and not self._permission_checker.can_use_tool(user, tc.name):
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
                risk_level = getattr(tool, "risk_level", None)
                risk = risk_level.value if risk_level else None
                await self._tool_guard.check(tc.name, tc.arguments, risk_level=risk)

            if on_tool_start:
                on_tool_start(tc.name)

            result = await self._registry.execute(tc.name, tc.arguments, user=user)

        except ApprovalRequest as e:
            if approval_callback:
                # Lock prevents concurrent approval prompts when tools run in parallel
                async with self._approval_lock:
                    approved = await approval_callback(e.action, e.details)
                if approved:
                    result = await self._registry.execute(tc.name, tc.arguments, user=user)
                else:
                    result = f"Action '{e.action}' was denied by user."
            else:
                result = (
                    f"Action Paused: approval required for '{e.action}' "
                    f"but no approval channel is configured."
                )
        except ToolGuardError as e:
            result = str(e)
        except Exception as e:
            result = f"Error executing tool {tc.name}: {e}"

        logger.debug(
            "[user=%s] tool_result | tool=%s | result=%r",
            user.id,
            tc.name,
            result[:_LOG_TRUNCATE],
        )

        # Calibration trajectory recording
        if trajectory_recorder is not None:
            trajectory_recorder.record_tool_result(tc.name, result)

        return result
