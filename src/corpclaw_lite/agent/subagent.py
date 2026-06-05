from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

import anyio

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.logging.trace import log_event
from corpclaw_lite.users.models import User

__all__ = [
    "SubagentDispatcher",
]

if TYPE_CHECKING:
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.skills.matcher import SkillMatcher
    from corpclaw_lite.extensions.skills.registry import SkillRegistry
    from corpclaw_lite.extensions.tools.builtin.research import ResearchRuntime
    from corpclaw_lite.llm.base import Provider
    from corpclaw_lite.security.tool_guard import ToolGuard

logger = logging.getLogger(__name__)

# Subagent timeout derives from max_wall_time_ms so it scales automatically
# with the model/hardware speed configured in settings.yaml.

_DEEP_RESEARCH_MARKERS = (
    "deep research",
    "deep_research",
    "глубок",
    "детальн",
    "подробн",
    "сравн",
    "противореч",
    "опроверг",
    "уточн",
    "гипотез",
    "fact-check",
    "factcheck",
    "verify",
    "cross-check",
    "compare",
    "contradict",
    "hypothesis",
)
_URL_RE = re.compile(r"https?://\S+")


def _research_mode_for_task(spec: SubagentSpec, task_context: str) -> str | None:
    if spec.id != "research-agent":
        return None
    lowered = task_context.casefold()
    if any(marker in lowered for marker in _DEEP_RESEARCH_MARKERS):
        return "deep_research"
    return "research"


def _prepare_research_task_context(spec: SubagentSpec, task_context: str) -> str:
    mode = _research_mode_for_task(spec, task_context)
    if mode is None:
        return task_context
    return (
        f"Research mode: {mode}\n"
        "Use the research-specific tools and finish with research_finalize.\n\n"
        f"{task_context}"
    )


def _ensure_research_sources_section(result: str) -> str:
    lowered = result.casefold()
    has_sources_section = "использованные источники" in lowered or "sources" in lowered
    urls = sorted({url.rstrip(").,]") for url in _URL_RE.findall(result)})
    if has_sources_section and urls:
        return result
    if urls:
        sources = "\n".join(f"- {url}" for url in urls)
    else:
        sources = "- Источники не были явно указаны в ответе research-agent."
    return result.rstrip() + "\n\n## Использованные источники\n" + sources


class SubagentDispatcher:
    """Dispatches a subagent with its own isolated AgentLoop."""

    def __init__(
        self,
        provider: Provider,
        main_registry: ToolRegistry,
        settings: AgentSettings,
        tool_guard: ToolGuard | None = None,
        permission_checker: PermissionChecker | None = None,
        skill_matcher: SkillMatcher | None = None,
        skill_registry: SkillRegistry | None = None,
        research_runtime: ResearchRuntime | None = None,
    ) -> None:
        self._provider = provider
        self._main_registry = main_registry
        self._settings = settings
        self._tool_guard = tool_guard
        self._permission_checker = permission_checker
        self._skill_matcher = skill_matcher
        self._skill_registry = skill_registry
        self._research_runtime = research_runtime

    async def dispatch(
        self,
        spec: SubagentSpec,
        user: User,
        task_context: str,
        *,
        parent_run_id: str | None = None,
        on_subagent_tool_start: Callable[[str, str], None] | None = None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
        on_subagent_llm_stage: Callable[[str, str], None] | None = None,
    ) -> str:
        """Run the subagent on a specific task."""
        subagent_run_id = uuid.uuid4().hex
        logger.info("Dispatching subagent %s for user %s", spec.id, user.id)
        log_event(
            "subagent_dispatch_started",
            subagent_run_id,
            parent_run_id=parent_run_id,
            subagent_id=spec.id,
            user_id=user.id,
            task_len=len(task_context),
        )

        # Resolve provider: if we have a router, use subagent-specific routing
        from corpclaw_lite.llm.router import LLMRouter

        effective_provider: Provider
        if isinstance(self._provider, LLMRouter):
            effective_provider = self._provider.for_subagent(
                spec.id,
                user_id=str(user.id),
                run_id=subagent_run_id,
            )
        else:
            effective_provider = self._provider

        # Create an isolated tool registry with ONLY the allowed tools
        isolated_registry = ToolRegistry()
        for tool_name, tool in self._main_registry.items().items():
            if "*" in spec.allowed_tools or tool_name in spec.allowed_tools:
                isolated_registry.register(tool)

        registered_names = list(isolated_registry.items().keys())
        logger.debug(
            "Subagent %s: filtered to %d tools: %s",
            spec.id,
            len(registered_names),
            ", ".join(registered_names),
        )

        # Load system prompt: calibrated override > prompt_path > description fallback
        system_prompt = f"You are a specialized subagent: {spec.name}.\n{spec.description}\n"
        if spec.prompt_path:
            from pathlib import Path

            from corpclaw_lite.paths import PROJECT_ROOT

            prompt_file = Path(spec.prompt_path)

            # Check for calibrated override first
            calibrated_prompt = (
                PROJECT_ROOT
                / "config"
                / "calibrated"
                / "bootstrap"
                / "subagents"
                / prompt_file.name
            )
            aio_calibrated = anyio.Path(calibrated_prompt)
            if await aio_calibrated.exists() and await aio_calibrated.is_file():
                system_prompt = await aio_calibrated.read_text(encoding="utf-8")
                logger.debug(
                    "Subagent %s: using calibrated prompt from %s",
                    spec.id,
                    calibrated_prompt,
                )
            else:
                aio_prompt = anyio.Path(prompt_file)
                if await aio_prompt.exists() and await aio_prompt.is_file():
                    system_prompt = await aio_prompt.read_text(encoding="utf-8")
                    logger.debug("Loaded prompt for subagent %s from %s", spec.id, prompt_file)
                else:
                    logger.warning(
                        "Subagent %s prompt_path '%s' not found, using description fallback",
                        spec.id,
                        spec.prompt_path,
                    )

        # Inject matched skills into subagent prompt — only skills scoped to this subagent
        if self._skill_matcher is not None and self._skill_registry is not None:
            allowed_skills = self._skill_registry.get_allowed_skills(user)
            scoped_skills = [s for s in allowed_skills if "*" in s.scope or spec.id in s.scope]
            matched = self._skill_matcher.match(task_context, scoped_skills)
            from corpclaw_lite.agent.prompt import build_skill_block

            skill_block = build_skill_block(matched, [])
            if skill_block:
                system_prompt += skill_block
                logger.debug("Subagent %s: injected %d skills for task", spec.id, len(matched))

        # Setup isolated loop — pass security guards through from parent
        loop = AgentLoop(
            AgentConfig(
                provider=effective_provider,
                registry=isolated_registry,
                settings=self._settings,
                enforce_tool_permissions=False,
                tool_guard=self._tool_guard,
                permission_checker=self._permission_checker,
            )
        )

        timeout_seconds = self._settings.max_wall_time_ms / 1000
        subagent_name = spec.name

        try:
            t0 = time.monotonic()
            effective_task_context = _prepare_research_task_context(spec, task_context)
            research_mode = _research_mode_for_task(spec, task_context)
            if self._research_runtime is not None and research_mode is not None:
                mode = "deep_research" if research_mode == "deep_research" else "research"
                self._research_runtime.initialize_run_mode(user, subagent_run_id, mode)
                log_event(
                    "research_run_mode_initialized",
                    subagent_run_id,
                    parent_run_id=parent_run_id,
                    subagent_id=spec.id,
                    mode=mode,
                )

            def forward_tool_start(tool_name: str) -> None:
                if on_subagent_tool_start is not None:
                    on_subagent_tool_start(subagent_name, tool_name)

            def forward_tool_batch_start(tool_names: list[str]) -> None:
                if on_subagent_tool_batch_start is not None:
                    on_subagent_tool_batch_start(subagent_name, tool_names)

            def forward_llm_stage(stage: str) -> None:
                if on_subagent_llm_stage is not None:
                    on_subagent_llm_stage(subagent_name, stage)

            result, _ = await asyncio.wait_for(
                loop.run(
                    user,
                    effective_task_context,
                    system_prompt=system_prompt,
                    on_tool_start=(
                        forward_tool_start if on_subagent_tool_start is not None else None
                    ),
                    on_tool_batch_start=(
                        forward_tool_batch_start
                        if on_subagent_tool_batch_start is not None
                        else None
                    ),
                    on_llm_stage=forward_llm_stage if on_subagent_llm_stage is not None else None,
                    run_id=subagent_run_id,
                ),
                timeout=timeout_seconds,
            )
            if spec.id == "research-agent":
                result = _ensure_research_sources_section(result)
            elapsed = time.monotonic() - t0
            log_event(
                "subagent_dispatch_finished",
                subagent_run_id,
                parent_run_id=parent_run_id,
                subagent_id=spec.id,
                user_id=user.id,
                status="ok",
                duration_ms=round(elapsed * 1000, 1),
                result_len=len(result),
            )
            logger.info(
                "Subagent %s completed: duration=%.1fs result_len=%d",
                spec.id,
                elapsed,
                len(result),
            )
            return result
        except TimeoutError:
            logger.error("Subagent %s timed out after %.0fs", spec.id, timeout_seconds)
            log_event(
                "subagent_dispatch_finished",
                subagent_run_id,
                parent_run_id=parent_run_id,
                subagent_id=spec.id,
                user_id=user.id,
                status="timeout",
                timeout_seconds=timeout_seconds,
            )
            return f"Subagent error: execution timed out after {int(timeout_seconds)}s"
        except Exception as e:
            logger.error("Subagent %s failed: %s", spec.id, e)
            log_event(
                "subagent_dispatch_finished",
                subagent_run_id,
                parent_run_id=parent_run_id,
                subagent_id=spec.id,
                user_id=user.id,
                status="error",
                error=str(e),
            )
            return f"Subagent error: {e}"
