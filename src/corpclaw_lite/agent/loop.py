from __future__ import annotations

from typing import TYPE_CHECKING

from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.agent.guards import (
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
    SimpleProgressGuard,
)
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import Provider
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.security.tool_guard import ApprovalRequest, ToolGuardError
from corpclaw_lite.users.models import User

if TYPE_CHECKING:
    from corpclaw_lite.departments.permissions import PermissionChecker
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
    ):
        self._provider = provider
        self._registry = registry
        self._settings = settings
        self._permission_checker = permission_checker
        self._tool_guard = tool_guard
        self._memory = memory

    async def run(self, user: User, message: str) -> str:
        """Run the ReAct loop until a final answer is given or limits are reached."""
        context = ContextBuilder.build_initial(user, message, self._registry)

        # Load history
        if self._memory:
            history = self._memory.get_history(str(user.id), limit=self._settings.max_steps)
            for item in history:
                if item["role"] == "user":
                    context.add_user_message(item["content"])
                else:
                    context.add_assistant_message(item["content"])
                    
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

        tools_schema = self._registry.to_schemas()

        while True:
            budget.check()
            budget.consume_iteration()

            response = await self._provider.chat(
                messages=context.messages,
                tools=tools_schema,
            )

            if not response.tool_calls:
                # Agent provided text directly — save and return
                final = response.content if response.content else "Agent provided no response."
                if self._memory:
                    self._memory.add_message(str(user.id), "assistant", final)
                return final

            # Agent requested tools
            if response.content:
                context.add_assistant_message(response.content)

            # Note down tool calls it made
            context.add_tool_calls(response.tool_calls)
            budget.consume_tool_calls(len(response.tool_calls))

            for tc in response.tool_calls:
                # 1. RBAC Tool check
                if self._permission_checker and not self._permission_checker.can_use_tool(user, tc.name):
                    result = f"Error: Permission denied. Your department ({user.department}) cannot use tool '{tc.name}'."
                else:
                    try:
                        # 2. Security Guard check
                        if self._tool_guard:
                            self._tool_guard.check(tc.name, tc.arguments)
                        
                        # 3. Execution
                        result = await self._registry.execute(tc.name, tc.arguments)
                    except ApprovalRequest as e:
                        result = f"Action Paused: {e.details}\nWaiting for user approval..."
                        # Handling inline approvals requires channel integration, returning a paused state for now.
                    except ToolGuardError as e:
                        result = str(e)
                    except Exception as e:
                        result = f"Error executing tool {tc.name}: {e}"

                context.add_tool_result(tc.id, tc.name, result)

                # Check looping
                if progress.detect_loop(tc.name, result):
                    loop_msg = "System Guard: You seem to be stuck in a loop repeating the same error. Please change your strategy or stop using this tool."
                    context.add_assistant_message(loop_msg)
                    break

        final_answer = context.messages[-1].content
        if self._memory and isinstance(final_answer, str):
            self._memory.add_message(str(user.id), "assistant", final_answer)
            
        return final_answer if isinstance(final_answer, str) else "Completed."
