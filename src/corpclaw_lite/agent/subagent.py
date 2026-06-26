from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from corpclaw_lite.agent.loop import AgentConfig, AgentLoop
from corpclaw_lite.agent.task_run import TaskRun
from corpclaw_lite.calibration.trajectory import TrajectoryRecorder
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.builtin.research import detect_language
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
    from corpclaw_lite.llm.queue import LLMQueueStatus
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


def _prepare_research_task_context(
    spec: SubagentSpec, task_context: str, *, research_mode: str | None = None
) -> str:
    mode = (
        research_mode if research_mode is not None else _research_mode_for_task(spec, task_context)
    )
    if mode is None:
        return task_context
    language = detect_language(task_context)
    return (
        f"Research mode: {mode}\n"
        f"Target language: {language}\n"
        "Write the final report ONLY in this language. Do not switch to English.\n"
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
        workspace_base: Path | None = None,
    ) -> None:
        self._provider = provider
        self._main_registry = main_registry
        self._settings = settings
        self._tool_guard = tool_guard
        self._permission_checker = permission_checker
        self._skill_matcher = skill_matcher
        self._skill_registry = skill_registry
        self._research_runtime = research_runtime
        self._workspace_base = workspace_base

    async def dispatch(
        self,
        spec: SubagentSpec,
        user: User,
        task_context: str,
        *,
        parent_run_id: str | None = None,
        parent_trajectory_recorder: TrajectoryRecorder | None = None,
        on_subagent_tool_start: Callable[[str, str], None] | None = None,
        on_subagent_tool_batch_start: Callable[[str, list[str]], None] | None = None,
        on_subagent_llm_stage: Callable[[str, str], None] | None = None,
        on_subagent_llm_queue_status: Callable[[str, LLMQueueStatus], None] | None = None,
        forced_research_mode: str | None = None,
    ) -> str:
        """Run the subagent on a specific task.

        When ``parent_trajectory_recorder`` is set (eval harness), the subagent's
        inner tool calls are recorded and merged into the parent trajectory via
        ``record_nested`` — so the eval harness can see ``table_query`` and
        friends that ran inside the dispatched subagent, not just the
        ``dispatch_subagent`` call itself.
        """
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
        # B-057: submit_report is always available to every subagent as an
        # explicit completion signal, regardless of allowed_tools filtering.
        # Without it, local LLMs often loop or fall silent after finishing.
        submit_tool = self._main_registry.get("submit_report")
        if submit_tool is not None and "submit_report" not in isolated_registry.items():
            isolated_registry.register(submit_tool)

        registered_names = list(isolated_registry.items().keys())
        logger.debug(
            "Subagent %s: filtered to %d tools: %s",
            spec.id,
            len(registered_names),
            ", ".join(registered_names),
        )

        # Load system prompt: calibrated override > prompt_path > description fallback
        system_prompt = f"You are a specialized subagent: {spec.name}.\n{spec.description}\n"
        # B-057: universal explicit-terminator instruction for all subagents.
        # Guarantees coverage of every subagent without per-spec prompt edits;
        # research-agent additionally prefers research_finalize (its own
        # terminal tool with source-grounding validation).
        system_prompt += (
            "\n\nWhen your work is complete, call `submit_report(result_text)` with "
            "your final result; this terminates your run and returns the result to "
            "the parent agent. Do not just stop or repeat tool calls.\n"
        )
        if spec.terminal_tool == "research_finalize":
            system_prompt += (
                "For research tasks prefer `research_finalize`; use `submit_report` "
                "only if research_finalize is not applicable.\n"
            )
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

        # B-049: per-subagent wall-clock budget. When the spec overrides max_wall_time_ms
        # (e.g. research-agent at 600000ms), clone the parent AgentSettings with the
        # override so the inner AgentLoop's SimpleBudgetGuard AND SoftDeadline scale to
        # the same window, and the outer asyncio.wait_for uses it as its hard limit.
        # pydantic model_copy produces an independent instance — the parent agent's
        # budget is untouched.
        effective_settings = self._settings
        if spec.max_wall_time_ms is not None:
            effective_settings = self._settings.model_copy(
                update={"max_wall_time_ms": spec.max_wall_time_ms}
            )

        # Setup isolated loop — pass security guards through from parent
        loop = AgentLoop(
            AgentConfig(
                provider=effective_provider,
                registry=isolated_registry,
                settings=effective_settings,
                enforce_tool_permissions=False,
                tool_guard=self._tool_guard,
                permission_checker=self._permission_checker,
                workspace_base=self._workspace_base,
                # B-047: workflow-finalize guard wiring. When the spec declares a
                # terminal tool (research-agent → research_finalize), the inner loop
                # nudges/restricts toward it as the budget runs out.
                terminal_tool=spec.terminal_tool,
                required_before_terminal=list(spec.required_before_terminal),
            )
        )

        timeout_seconds = effective_settings.max_wall_time_ms / 1000
        subagent_name = spec.name

        try:
            t0 = time.monotonic()
            # Etap 3B: explicit depth-mode override ("research" from the UI)
            # forces deep_research for the research-agent, bypassing keyword
            # detection. When None, keyword detection is the fallback (3A behavior).
            if forced_research_mode == "research" and spec.id == "research-agent":
                resolved_research_mode: str | None = "deep_research"
            else:
                resolved_research_mode = forced_research_mode or _research_mode_for_task(
                    spec, task_context
                )
            effective_task_context = _prepare_research_task_context(
                spec, task_context, research_mode=resolved_research_mode
            )
            if self._research_runtime is not None and resolved_research_mode is not None:
                mode = "deep_research" if resolved_research_mode == "deep_research" else "research"
                language = detect_language(task_context)
                self._research_runtime.initialize_run_mode(
                    user, subagent_run_id, mode, language=language
                )
                log_event(
                    "research_run_mode_initialized",
                    subagent_run_id,
                    parent_run_id=parent_run_id,
                    subagent_id=spec.id,
                    mode=mode,
                    language=language,
                    forced=bool(forced_research_mode),
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

            def forward_queue_status(status: LLMQueueStatus) -> None:
                if on_subagent_llm_queue_status is not None:
                    on_subagent_llm_queue_status(subagent_name, status)

            # B-060: when the parent wants visibility into the subagent's tool
            # calls, record them in an inner recorder and merge into the parent
            # trajectory after the run completes. This is how the eval harness
            # sees table_query/excel_workbook/etc. that ran inside the dispatch.
            inner_recorder = (
                TrajectoryRecorder(f"{spec.id}#inner")
                if parent_trajectory_recorder is not None
                else None
            )

            result, stats_inner = await asyncio.wait_for(
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
                    on_llm_queue_status=(
                        forward_queue_status if on_subagent_llm_queue_status is not None else None
                    ),
                    trajectory_recorder=inner_recorder,
                    run_id=subagent_run_id,
                ),
                timeout=timeout_seconds,
            )
            if inner_recorder is not None and parent_trajectory_recorder is not None:
                inner_traj = inner_recorder.finalize(
                    result,
                    iterations=stats_inner.iterations,
                    tools_used=list(stats_inner.tools_used),
                    duration_ms=stats_inner.duration_ms,
                    status=stats_inner.status,
                )
                parent_trajectory_recorder.record_nested(spec.id, inner_traj.steps)
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
            # Research-agent: recover stored facts/sources as a partial report instead
            # of a bare error (B-036). B-045: interrupted=True renders an honest skeleton
            # — banner + gathered facts + sources + a limitation noting synthesis did not
            # happen — instead of pretending the facts dump is a finished deep report.
            if spec.id == "research-agent" and self._research_runtime is not None:
                # Etap 3B: honour the explicit override on timeout recovery too.
                if forced_research_mode == "research":
                    recovery_research_mode: str | None = "deep_research"
                else:
                    recovery_research_mode = forced_research_mode or _research_mode_for_task(
                        spec, task_context
                    )
                mode = "deep_research" if recovery_research_mode == "deep_research" else "research"
                try:
                    partial = self._research_runtime.finalize_report(
                        user, subagent_run_id, mode, answer="", interrupted=True
                    )
                except Exception as partial_err:  # pragma: no cover - defensive
                    logger.warning("Research partial-handoff failed: %s", partial_err)
                    return f"Subagent error: execution timed out after {int(timeout_seconds)}s"
                await TaskRun(self._workspace_base).generate_handoff(
                    user,
                    subagent_run_id,
                    partial_result=partial,
                    reason=f"research-agent timed out after {int(timeout_seconds)}s",
                )
                log_event(
                    "subagent_partial_handoff",
                    subagent_run_id,
                    parent_run_id=parent_run_id,
                    subagent_id=spec.id,
                    timeout_seconds=timeout_seconds,
                    partial_len=len(partial),
                )
                return partial
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
