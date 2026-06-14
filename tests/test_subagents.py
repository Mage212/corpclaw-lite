from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from corpclaw_lite.agent.loop import RunStats
from corpclaw_lite.agent.subagent import SubagentDispatcher
from corpclaw_lite.config.settings import AgentSettings
from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.base import RiskLevel, Tool
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.llm.queue import LLMQueueStatus
from corpclaw_lite.users.models import User


class DummyProvider:
    pass


class DummyToolA(Tool):
    name = "tool_a"
    description = ""
    params = []
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return "A"


class DummyToolB(Tool):
    name = "tool_b"
    description = ""
    params = []
    risk_level = RiskLevel.LOW

    async def execute(self, **kwargs: Any) -> str:
        return "B"


@pytest.mark.asyncio
async def test_subagent_dispatcher():
    registry = ToolRegistry()
    registry.register(DummyToolA())
    registry.register(DummyToolB())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(), main_registry=registry, settings=AgentSettings()
    )  # type: ignore

    spec = SubagentSpec(
        id="test_agent", name="Test Agent", description="Testing", allowed_tools=["tool_a"]
    )
    user = User(id=1, name="User", department="dev")

    # Instead of running the real loop which requires a real provider, we patch AgentLoop.run
    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value=("Subagent result", RunStats()))

        res = await dispatcher.dispatch(spec, user, "Do something")

        assert res == "Subagent result"
        call_args = MockLoop.call_args
        agent_config = call_args.args[0] if call_args.args else call_args.kwargs["config"]
        isolated_registry = agent_config.registry
        assert "tool_a" in isolated_registry._tools
        assert "tool_b" not in isolated_registry._tools
        assert agent_config.enforce_tool_permissions is False


@pytest.mark.asyncio
async def test_subagent_loop_keeps_allowed_tools_even_if_department_lacks_direct_tool_access():
    """Subagent allowed_tools are the authority inside the isolated subagent loop."""
    registry = ToolRegistry()
    registry.register(DummyToolA())
    registry.register(DummyToolB())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(), main_registry=registry, settings=AgentSettings()
    )  # type: ignore

    spec = SubagentSpec(
        id="data-agent",
        name="Data Agent",
        description="Data work",
        allowed_tools=["tool_a"],
    )
    user = User(id=1, name="User", department="marketing")

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value=("Subagent result", RunStats()))

        await dispatcher.dispatch(spec, user, "Analyze data")

        call_args = MockLoop.call_args
        agent_config = call_args.args[0] if call_args.args else call_args.kwargs["config"]
        assert agent_config.enforce_tool_permissions is False
        assert set(agent_config.registry.items()) == {"tool_a"}


@pytest.mark.asyncio
async def test_research_subagent_initializes_persisted_mode() -> None:
    registry = ToolRegistry()
    registry.register(DummyToolA())

    class RuntimeSpy:
        def __init__(self) -> None:
            self.calls: list[tuple[User, str | None, str, str]] = []

        def initialize_run_mode(
            self, user: User, run_id: str | None, mode: str, *, language: str = "en"
        ) -> None:
            self.calls.append((user, run_id, mode, language))

    runtime = RuntimeSpy()
    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore[arg-type]
        main_registry=registry,
        settings=AgentSettings(),
        research_runtime=runtime,  # type: ignore[arg-type]
    )
    spec = SubagentSpec(
        id="research-agent",
        name="Research Agent",
        description="Research",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")
    captured_messages: list[str] = []

    async def _capture_run(
        u: object,
        msg: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, RunStats]:
        captured_messages.append(msg)
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Сделай детальное исследование")

    assert runtime.calls
    assert runtime.calls[0][2] == "deep_research"
    assert runtime.calls[0][3] == "ru"  # Cyrillic task -> target language ru
    assert captured_messages[0].startswith("Research mode: deep_research")
    assert "Target language: ru" in captured_messages[0]


@pytest.mark.asyncio
async def test_subagent_dispatcher_uses_same_run_id_for_router_and_loop() -> None:
    """Subagent LLM queue/cache events must use the subagent's own run_id."""
    from types import SimpleNamespace

    from corpclaw_lite.config.providers import ProviderRegistry
    from corpclaw_lite.config.settings import LLMSettings, RoutingRule
    from corpclaw_lite.llm.router import LLMRouter

    registry = ToolRegistry()
    registry.register(DummyToolA())
    provider_registry = ProviderRegistry.from_env(
        {
            "PROVIDER_OLLAMA__TYPE": "openai",
            "PROVIDER_OLLAMA__BASE_URL": "http://localhost:11434/v1",
            "PROVIDER_OLLAMA__API_KEY": "ollama",
        }
    )
    router = LLMRouter.from_settings(
        LLMSettings(
            routing=[RoutingRule(task_kind="default", provider="ollama", model="qwen")],
            queue={"enabled": True},
        ),
        provider_registry,
    )
    router.for_subagent = MagicMock(return_value=DummyProvider())  # type: ignore[method-assign]

    dispatcher = SubagentDispatcher(
        provider=router,
        main_registry=registry,
        settings=AgentSettings(),
    )
    spec = SubagentSpec(
        id="data-agent",
        name="Data Agent",
        description="Data work",
        allowed_tools=["tool_a"],
    )
    user = User(id=1, name="User", department="dev")

    with (
        patch("corpclaw_lite.agent.subagent.uuid.uuid4") as mock_uuid4,
        patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop,
    ):
        mock_uuid4.return_value = SimpleNamespace(hex="sub-run-id")
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value=("Subagent result", RunStats()))

        await dispatcher.dispatch(spec, user, "Analyze data", parent_run_id="parent-run-id")

    router.for_subagent.assert_called_once_with(
        "data-agent",
        user_id="1",
        run_id="sub-run-id",
    )
    mock_loop_instance.run.assert_called_once()
    assert mock_loop_instance.run.call_args.kwargs["run_id"] == "sub-run-id"


@pytest.mark.asyncio
async def test_subagent_dispatcher_wraps_status_callbacks_with_subagent_name() -> None:
    """Internal subagent loop callbacks should include the human-readable subagent name."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )
    spec = SubagentSpec(
        id="document-agent",
        name="Document Agent",
        description="Document work",
        allowed_tools=["tool_a"],
    )
    user = User(id=1, name="User", department="dev")
    status_events: list[tuple[str, str, object]] = []
    queue_status = LLMQueueStatus(
        user_id="1",
        task_kind="subagent:document-agent",
        load_class="subagent",
        position=0,
        estimated_wait_seconds=15.0,
        waiting_count=1,
        active_count=1,
        max_concurrent=1,
        wait_seconds=1.0,
    )

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(return_value=("Subagent result", RunStats()))

        await dispatcher.dispatch(
            spec,
            user,
            "Prepare document",
            on_subagent_tool_start=lambda subagent, tool: status_events.append(
                ("tool", subagent, tool)
            ),
            on_subagent_tool_batch_start=lambda subagent, tools: status_events.append(
                ("batch", subagent, list(tools))
            ),
            on_subagent_llm_stage=lambda subagent, stage: status_events.append(
                ("llm", subagent, stage)
            ),
            on_subagent_llm_queue_status=lambda subagent, status: status_events.append(
                ("queue", subagent, status)
            ),
        )

    run_kwargs = mock_loop_instance.run.call_args.kwargs
    run_kwargs["on_tool_start"]("read_file")
    run_kwargs["on_tool_batch_start"](["read_file", "list_files"])
    run_kwargs["on_llm_stage"]("reasoning")
    run_kwargs["on_llm_queue_status"](queue_status)

    assert status_events == [
        ("tool", "Document Agent", "read_file"),
        ("batch", "Document Agent", ["read_file", "list_files"]),
        ("llm", "Document Agent", "reasoning"),
        ("queue", "Document Agent", queue_status),
    ]


@pytest.mark.asyncio
async def test_subagent_prompt_loading(tmp_path: Path) -> None:
    """Subagent loads system prompt from prompt_path when the file exists."""
    prompt_file = tmp_path / "test_agent.md"
    prompt_file.write_text("# Test Agent\nYou are a test agent.", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )

    spec = SubagentSpec(
        id="prompt_agent",
        name="Prompt Agent",
        description="Fallback description",
        allowed_tools=["*"],
        prompt_path=str(prompt_file),
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object,
        msg: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    # system_prompt kwarg should contain the loaded file content, not the fallback
    assert captured_system
    assert "# Test Agent" in captured_system[0]
    assert "Fallback description" not in captured_system[0]


@pytest.mark.asyncio
async def test_subagent_prompt_fallback_when_missing() -> None:
    """Subagent falls back to description when prompt_path file does not exist."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )

    spec = SubagentSpec(
        id="missing_prompt_agent",
        name="Missing Prompt Agent",
        description="Fallback description here",
        allowed_tools=["*"],
        prompt_path="/nonexistent/path/prompt.md",
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object,
        msg: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    assert captured_system
    assert "Fallback description here" in captured_system[0]


@pytest.mark.asyncio
async def test_subagent_system_prompt_passed_as_kwarg() -> None:
    """system_prompt is passed as a named kwarg to loop.run(), not concatenated into msg."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
    )

    spec = SubagentSpec(
        id="kwarg_agent",
        name="Kwarg Agent",
        description="Specialized instructions",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")

    captured_args: list[tuple[str, str | None]] = []

    async def _capture(
        u: object,
        msg: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, RunStats]:
        captured_args.append((msg, system_prompt))
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture)

        await dispatcher.dispatch(spec, user, "Actual task text")

    assert captured_args
    task_msg, sys_prompt = captured_args[0]
    # task context goes into msg, system prompt goes into system_prompt kwarg
    assert task_msg == "Actual task text"
    assert sys_prompt is not None
    assert "Kwarg Agent" in sys_prompt or "Specialized instructions" in sys_prompt


# ── DispatchSubagentTool tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_unknown_id() -> None:
    """Unknown subagent_id returns Error string listing available IDs."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    registry = SubagentRegistry()
    registry.register(
        SubagentSpec(id="known", name="Known", description="desc", allowed_tools=["*"])
    )

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="ok")

    tool = DispatchSubagentTool(dispatcher, registry)
    user = User(id=1, name="U", department="dev")

    result = await tool.execute(subagent_id="unknown_id", task="do it", user=user)

    assert "Error" in result
    assert "unknown_id" in result
    assert "known" in result


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_no_user() -> None:
    """user=None returns Error string."""
    from unittest.mock import MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    registry = SubagentRegistry()
    registry.register(
        SubagentSpec(id="agent", name="Agent", description="desc", allowed_tools=["*"])
    )

    tool = DispatchSubagentTool(MagicMock(), registry)

    result = await tool.execute(subagent_id="agent", task="do it", user=None)

    assert "Error" in result
    assert "User context" in result or "user" in result.lower()


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_dispatches() -> None:
    """Valid call delegates to SubagentDispatcher.dispatch() and returns its result."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    spec = SubagentSpec(id="worker", name="Worker", description="desc", allowed_tools=["*"])
    registry = SubagentRegistry()
    registry.register(spec)

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="subagent result")

    tool = DispatchSubagentTool(dispatcher, registry)
    user = User(id=1, name="U", department="dev")

    result = await tool.execute(
        subagent_id="worker",
        task="do the work",
        user=user,
        run_id="parent-run",
    )

    assert result == "subagent result"
    dispatcher.dispatch.assert_called_once_with(
        spec,
        user,
        "do the work",
        parent_run_id="parent-run",
        on_subagent_tool_start=None,
        on_subagent_tool_batch_start=None,
        on_subagent_llm_stage=None,
        on_subagent_llm_queue_status=None,
    )


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_passes_status_callbacks() -> None:
    """DispatchSubagentTool should forward runtime status callbacks to the dispatcher."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    spec = SubagentSpec(id="worker", name="Worker", description="desc", allowed_tools=["*"])
    registry = SubagentRegistry()
    registry.register(spec)

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="subagent result")
    tool = DispatchSubagentTool(dispatcher, registry)
    user = User(id=1, name="U", department="dev")

    def on_tool_start(subagent_name: str, tool_name: str) -> None:
        _ = subagent_name, tool_name

    def on_tool_batch_start(subagent_name: str, tool_names: list[str]) -> None:
        _ = subagent_name, tool_names

    def on_llm_stage(subagent_name: str, stage: str) -> None:
        _ = subagent_name, stage

    def on_llm_queue_status(subagent_name: str, status: LLMQueueStatus) -> None:
        _ = subagent_name, status

    result = await tool.execute(
        subagent_id="worker",
        task="do the work",
        user=user,
        run_id="parent-run",
        on_subagent_tool_start=on_tool_start,
        on_subagent_tool_batch_start=on_tool_batch_start,
        on_subagent_llm_stage=on_llm_stage,
        on_subagent_llm_queue_status=on_llm_queue_status,
    )

    assert result == "subagent result"
    dispatcher.dispatch.assert_called_once_with(
        spec,
        user,
        "do the work",
        parent_run_id="parent-run",
        on_subagent_tool_start=on_tool_start,
        on_subagent_tool_batch_start=on_tool_batch_start,
        on_subagent_llm_stage=on_llm_stage,
        on_subagent_llm_queue_status=on_llm_queue_status,
    )


@pytest.mark.asyncio
async def test_dispatch_subagent_tool_enforces_department_subagent_rbac() -> None:
    """Department allowed_subagents must be enforced before dispatch."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.departments.manager import DepartmentConfig, DepartmentManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    spec = SubagentSpec(
        id="data-agent",
        name="Data",
        description="desc",
        allowed_tools=["*"],
        allowed_departments=["analytics"],
    )
    registry = SubagentRegistry()
    registry.register(spec)

    manager = DepartmentManager()
    manager._departments["analytics"] = DepartmentConfig(
        {
            "description": "Analytics",
            "allowed_tools": ["*"],
            "allowed_subagents": ["research-agent"],
        }
    )
    checker = PermissionChecker(manager)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="subagent result")
    tool = DispatchSubagentTool(dispatcher, registry, permission_checker=checker)
    user = User(id=1, name="U", department="analytics")

    result = await tool.execute(subagent_id="data-agent", task="analyze", user=user)

    assert "Permission denied" in result
    dispatcher.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_subagent_available_list_respects_department_rbac() -> None:
    """Unknown-subagent errors must not list subagents the department cannot dispatch."""
    from unittest.mock import AsyncMock, MagicMock

    from corpclaw_lite.departments.manager import DepartmentConfig, DepartmentManager
    from corpclaw_lite.departments.permissions import PermissionChecker
    from corpclaw_lite.extensions.subagents.base import SubagentSpec
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
    from corpclaw_lite.extensions.tools.builtin.dispatch import DispatchSubagentTool

    registry = SubagentRegistry()
    registry.register(SubagentSpec(id="research-agent", name="Research", description="desc"))
    registry.register(SubagentSpec(id="execution-agent", name="Execution", description="desc"))
    manager = DepartmentManager()
    manager._departments["default"] = DepartmentConfig(
        {
            "description": "Default",
            "allowed_tools": ["dispatch_subagent"],
            "allowed_subagents": ["research-agent"],
        }
    )
    checker = PermissionChecker(manager)
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock(return_value="subagent result")
    tool = DispatchSubagentTool(dispatcher, registry, permission_checker=checker)
    user = User(id=1, name="U", department="default")

    result = await tool.execute(subagent_id="missing-agent", task="work", user=user)

    assert "research-agent" in result
    assert "execution-agent" not in result
    dispatcher.dispatch.assert_not_called()


# ── Department filtering tests ───────────────────────────────────────────────


def test_subagent_registry_department_filtering() -> None:
    """P1-6: get_allowed_subagents filters by user department."""
    from corpclaw_lite.extensions.subagents.registry import SubagentRegistry

    registry = SubagentRegistry()
    registry.register(
        SubagentSpec(
            id="marketing_agent",
            name="Marketing",
            description="Marketing tasks",
            allowed_departments=["marketing"],
        )
    )
    registry.register(
        SubagentSpec(
            id="global_agent",
            name="Global",
            description="Available to all",
            allowed_departments=["*"],
        )
    )
    registry.register(
        SubagentSpec(
            id="dev_agent",
            name="Dev",
            description="Dev only",
            allowed_departments=["dev", "engineering"],
        )
    )

    marketing_user = User(id=1, name="M", department="marketing")
    dev_user = User(id=2, name="D", department="dev")
    hr_user = User(id=3, name="H", department="hr")

    marketing_agents = registry.get_allowed_subagents(marketing_user)
    dev_agents = registry.get_allowed_subagents(dev_user)
    hr_agents = registry.get_allowed_subagents(hr_user)

    # Marketing user sees marketing_agent + global_agent
    assert {s.id for s in marketing_agents} == {"marketing_agent", "global_agent"}
    # Dev user sees dev_agent + global_agent
    assert {s.id for s in dev_agents} == {"dev_agent", "global_agent"}
    # HR user only sees global_agent (wildcard)
    assert {s.id for s in hr_agents} == {"global_agent"}


# ── Skill injection in subagents ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_subagent_receives_matched_skills() -> None:
    """When skill_matcher and skill_registry are provided, matched skills are injected."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    # Create mock skill matcher that returns a skill
    from corpclaw_lite.extensions.skills.base import Skill

    matched_skill = Skill(
        id="test_skill",
        description="Test skill for subagent",
        allowed_for=["*"],
        instructions="Do the test thing carefully.",
    )

    mock_matcher = MagicMock()
    mock_matcher.match.return_value = [matched_skill]

    mock_skill_registry = MagicMock()
    mock_skill_registry.get_allowed_skills.return_value = [matched_skill]

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
        skill_matcher=mock_matcher,
        skill_registry=mock_skill_registry,
    )

    spec = SubagentSpec(
        id="skill_agent",
        name="Skill Agent",
        description="Agent with skills",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object,
        msg: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something with test skill")

    assert captured_system
    # Skill block should be present in system prompt
    assert "## Available Skills" in captured_system[0]
    assert "test_skill" in captured_system[0]
    assert "Do the test thing carefully." in captured_system[0]


@pytest.mark.asyncio
async def test_subagent_no_skills_when_matcher_none() -> None:
    """Without skill_matcher, no skills are injected into subagent prompt."""
    registry = ToolRegistry()
    registry.register(DummyToolA())

    dispatcher = SubagentDispatcher(
        provider=DummyProvider(),  # type: ignore
        main_registry=registry,
        settings=AgentSettings(),
        skill_matcher=None,
        skill_registry=None,
    )

    spec = SubagentSpec(
        id="noskill_agent",
        name="NoSkill Agent",
        description="Agent without skills",
        allowed_tools=["*"],
    )
    user = User(id=1, name="User", department="dev")

    captured_system: list[str] = []

    async def _capture_run(
        u: object,
        msg: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, RunStats]:
        captured_system.append(system_prompt or "")
        return "done", RunStats()

    with patch("corpclaw_lite.agent.subagent.AgentLoop") as MockLoop:
        mock_loop_instance = MockLoop.return_value
        mock_loop_instance.run = AsyncMock(side_effect=_capture_run)

        await dispatcher.dispatch(spec, user, "Do something")

    assert captured_system
    # No skill block should be present
    assert "## Available Skills" not in captured_system[0]
