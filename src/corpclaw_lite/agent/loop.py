from __future__ import annotations

from corpclaw_lite.agent.context import ContextBuilder
from corpclaw_lite.agent.guards import (
    SimpleBudgetGuard,
    SimpleBudgetGuardConfig,
    SimpleProgressGuard,
)
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.base import Provider
from corpclaw_lite.users.models import User


class AgentLoop:
    """Core ReAct loop for agent execution."""

    def __init__(self, provider: Provider, registry: ToolRegistry, settings: AgentSettings):
        self._provider = provider
        self._registry = registry
        self._settings = settings

    async def run(self, user: User, message: str) -> str:
        """Run the ReAct loop until a final answer is given or limits are reached."""
        context = ContextBuilder.build_initial(user, message, self._registry)

        budget = SimpleBudgetGuard(
            SimpleBudgetGuardConfig(
                max_iterations=self._settings.max_steps,
                max_tool_calls=self._settings.max_tool_calls,
                max_time_ms=self._settings.max_wall_time_ms,
            )
        )
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
                # Agent provided text directly
                return response.content if response.content else "Agent provided no response."

            # Agent requested tools
            if response.content:
                context.add_assistant_message(response.content)

            # Note down tool calls it made
            context.add_tool_calls(response.tool_calls)
            budget.consume_tool_calls(len(response.tool_calls))

            # Execute tools in parallel typically, or seq for simplicity in Phase 1
            for tc in response.tool_calls:
                result = await self._registry.execute(tc.name, tc.arguments)
                context.add_tool_result(tc.id, tc.name, result)

                # Check looping
                if progress.detect_loop(tc.name, result):
                    context.add_assistant_message(
                        "System Guard: You seem to be stuck in a loop repeating the same error. "
                        "Please change your strategy or stop using this tool."
                    )
                    break
