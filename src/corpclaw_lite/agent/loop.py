from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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
from corpclaw_lite.llm.base import Provider
from corpclaw_lite.logging import health
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuardError
from corpclaw_lite.users.models import User

if TYPE_CHECKING:
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.memory.consolidation import MemoryConsolidator
    from corpclaw_lite.security.tool_guard import ToolGuard


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
    ):
        self._provider = provider
        self._registry = registry
        self._settings = settings
        self._permission_checker = permission_checker
        self._tool_guard = tool_guard
        self._memory = memory
        self._approval_callback = approval_callback
        self._consolidator = consolidator

    async def run(
        self,
        user: User,
        message: str,
        system_prompt: str | None = None,
        approval_callback: Callable[[str, str], Awaitable[bool]] | None = None,
        on_tool_start: Callable[[str], None] | None = None,
        tools_enabled: bool = True,
    ) -> str:
        """Run the ReAct loop until a final answer is given or limits are reached."""
        # Per-call callback takes priority over the instance-level default
        _approval_cb = (
            approval_callback if approval_callback is not None else self._approval_callback
        )

        # Load history BEFORE building context so it precedes the current message
        history: list[dict[str, Any]] = []
        if self._memory:
            history = self._memory.get_history(str(user.id), limit=self._settings.max_history)

        context = ContextBuilder.build_initial(
            user,
            message,
            history=history,
            system_prompt_override=system_prompt,
        )

        # Save new user message
        if self._memory:
            self._memory.add_message(str(user.id), "user", message)

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

                try:
                    response = await asyncio.wait_for(
                        self._provider.chat(
                            messages=context.messages,
                            tools=tools_schema,
                        ),
                        timeout=120,
                    )
                except TimeoutError:
                    msg = "I could not get a response from the language model (timed out)."
                    if self._memory:
                        self._memory.add_message(str(user.id), "assistant", msg)
                    return msg

                if not response.tool_calls:
                    # Agent provided text directly — save and return
                    final = response.content if response.content else "Agent provided no response."
                    if self._memory:
                        self._memory.add_message(str(user.id), "assistant", final)
                        if self._consolidator:
                            await self._consolidator.maybe_consolidate(self._memory, str(user.id))
                    return final

                # Agent requested tools — emit a single assistant message
                # containing both content (if any) and tool_calls.
                context.add_tool_calls(response.tool_calls, content=response.content or None)
                budget.consume_tool_calls(len(response.tool_calls))
                health.increment("tool_calls", len(response.tool_calls))

                should_stop = False
                for tc in response.tool_calls:
                    # 1. RBAC Tool check
                    if self._permission_checker and not self._permission_checker.can_use_tool(
                        user, tc.name
                    ):
                        result = (
                            f"Error: Permission denied. Your department ({user.department})"
                            f" cannot use tool '{tc.name}'."
                        )
                    else:
                        try:
                            # 2. Security Guard check
                            if self._tool_guard:
                                self._tool_guard.check(tc.name, tc.arguments)

                            # 3. Progress callback
                            if on_tool_start:
                                on_tool_start(tc.name)

                            # 4. Execution
                            result = await self._registry.execute(tc.name, tc.arguments, user=user)
                        except ApprovalRequest as e:
                            if _approval_cb:
                                approved = await _approval_cb(e.action, e.details)
                                if approved:
                                    result = await self._registry.execute(
                                        tc.name, tc.arguments, user=user
                                    )
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

                    context.add_tool_result(tc.id, tc.name, result)

                    # Check looping
                    if progress.detect_loop(tc.name, result):
                        loop_msg = (
                            "System Guard: You seem to be stuck in a loop repeating the same"
                            " error. Please change your strategy or stop using this tool."
                        )
                        context.add_assistant_message(loop_msg)
                        should_stop = True
                        break

                if should_stop:
                    break

        except BudgetExceededError as e:
            health.increment("errors")
            msg = f"I reached my resource limit and had to stop: {e}"
            if self._memory:
                self._memory.add_message(str(user.id), "assistant", msg)
            return msg

        # Reached when progress guard breaks the loop
        fallback = "I detected a loop and stopped to avoid repeating the same actions."
        if self._memory:
            self._memory.add_message(str(user.id), "assistant", fallback)
        return fallback
