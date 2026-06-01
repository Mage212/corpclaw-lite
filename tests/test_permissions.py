"""Tests for PermissionChecker — RBAC checks per department."""

from __future__ import annotations

from typing import Any

from corpclaw_lite.departments.manager import DepartmentConfig, DepartmentManager
from corpclaw_lite.departments.permissions import PermissionChecker
from corpclaw_lite.extensions.tools.base import Tool
from corpclaw_lite.users.models import User


class DummyTool(Tool):
    name = "dummy_tool"
    description = "Dummy tool"
    params = []

    async def execute(self, **kwargs: object) -> str:
        return "ok"


def _make_checker_with_dept(
    slug: str = "engineering",
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    plugins: list[str] | None = None,
    subagents: list[str] | None = None,
    mcp: list[str] | None = None,
) -> PermissionChecker:
    mgr = DepartmentManager()
    data: dict[str, Any] = {
        "description": slug,
        "allowed_tools": tools if tools is not None else ["*"],
        "allowed_skills": skills if skills is not None else ["*"],
        "allowed_plugins": plugins if plugins is not None else ["*"],
        "allowed_subagents": subagents if subagents is not None else ["*"],
        "allowed_mcp": mcp if mcp is not None else ["*"],
    }
    mgr._departments[slug] = DepartmentConfig(data)
    return PermissionChecker(mgr)


def _make_user(department: str = "engineering") -> User:
    return User(id=1, name="Test", department=department)


def test_can_use_tool_wildcard() -> None:
    checker = _make_checker_with_dept(tools=["*"])
    assert checker.can_use_tool(_make_user(), "any_tool")


def test_can_use_tool_specific() -> None:
    checker = _make_checker_with_dept(tools=["read_file", "write_file"])
    user = _make_user()
    assert checker.can_use_tool(user, "read_file")
    assert not checker.can_use_tool(user, "exec_script")


def test_can_use_tool_unknown_department() -> None:
    checker = _make_checker_with_dept(slug="engineering")
    assert not checker.can_use_tool(_make_user("unknown"), "read_file")


def test_can_use_skill() -> None:
    checker = _make_checker_with_dept(skills=["normalize_data"])
    user = _make_user()
    assert checker.can_use_skill(user, "normalize_data")
    assert not checker.can_use_skill(user, "other_skill")


def test_can_use_plugin() -> None:
    checker = _make_checker_with_dept(plugins=["excel_plugin"])
    user = _make_user()
    assert checker.can_use_plugin(user, "excel_plugin")
    assert not checker.can_use_plugin(user, "other_plugin")


def test_can_dispatch_subagent() -> None:
    checker = _make_checker_with_dept(subagents=["data-agent"])
    user = _make_user()
    assert checker.can_dispatch_subagent(user, "data-agent")
    assert not checker.can_dispatch_subagent(user, "other")


def test_department_without_allowed_subagents_denies_dispatch() -> None:
    mgr = DepartmentManager()
    mgr._departments["default"] = DepartmentConfig(
        {
            "description": "Default",
            "allowed_tools": ["dispatch_subagent"],
        }
    )
    checker = PermissionChecker(mgr)

    assert not checker.can_dispatch_subagent(_make_user("default"), "research-agent")


def test_can_use_mcp() -> None:
    checker = _make_checker_with_dept(mcp=["server_a"])
    user = _make_user()
    assert checker.can_use_mcp(user, "server_a")
    assert not checker.can_use_mcp(user, "server_b")


def test_registered_plugin_tool_enforces_plugin_scope() -> None:
    checker = _make_checker_with_dept(
        tools=["*"],
        plugins=["hr_plugin"],
    )
    user = _make_user("engineering")
    tool = DummyTool()
    tool.source_kind = "plugin"
    tool.source_name = "hr_plugin"
    tool.allowed_departments = ["hr"]

    assert not checker.can_use_registered_tool(user, tool)


def test_registered_mcp_tool_enforces_server_scope() -> None:
    checker = _make_checker_with_dept(tools=["*"], mcp=["server_a"])
    user = _make_user()
    tool = DummyTool()
    tool.source_kind = "mcp"
    tool.source_name = "server_b"

    assert not checker.can_use_registered_tool(user, tool)


def test_registered_tool_can_skip_base_allowlist_for_subagents() -> None:
    checker = _make_checker_with_dept(tools=["read_file"])
    user = _make_user()
    tool = DummyTool()

    assert not checker.can_use_registered_tool(user, tool)
    assert checker.can_use_registered_tool(user, tool, enforce_tool_allowlist=False)


def test_get_budget_with_department() -> None:
    checker = _make_checker_with_dept()
    budget = checker.get_budget(_make_user())
    assert budget.max_iterations > 0


def test_get_budget_unknown_department() -> None:
    checker = _make_checker_with_dept(slug="engineering")
    budget = checker.get_budget(_make_user("unknown"))
    assert budget.max_iterations == 10
    assert budget.max_tool_calls == 20


def test_empty_allowed_list_denies() -> None:
    checker = _make_checker_with_dept(tools=[])
    assert not checker.can_use_tool(_make_user(), "read_file")
