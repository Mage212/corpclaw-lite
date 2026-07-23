"""DC-039: default department is office-without-web."""

from __future__ import annotations

from corpclaw_lite.departments.manager import DepartmentManager
from corpclaw_lite.departments.permissions import PermissionChecker
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.users.models import User


def _default_checker() -> PermissionChecker:
    mgr = DepartmentManager()
    mgr.load_file(PROJECT_ROOT / "config" / "departments.yaml")
    return PermissionChecker(mgr)


def _default_user() -> User:
    return User(id=1, telegram_id=900001, name="Default User", department="default")


def test_default_has_office_subagents() -> None:
    mgr = DepartmentManager()
    mgr.load_file(PROJECT_ROOT / "config" / "departments.yaml")
    dept = mgr.get_department("default")
    assert dept is not None
    for agent in (
        "filesystem-agent",
        "document-agent",
        "execution-agent",
        "data-agent",
    ):
        assert agent in dept.allowed_subagents
    assert "research-agent" not in dept.allowed_subagents


def test_default_no_web_tools() -> None:
    checker = _default_checker()
    user = _default_user()
    assert checker.can_use_tool(user, "web_fetch") is False
    assert checker.can_use_tool(user, "web_search") is False
    assert checker.can_use_tool(user, "excel_inspect") is True
    assert checker.can_use_tool(user, "send_file") is True
    assert checker.can_use_tool(user, "dispatch_subagent") is True


def test_default_can_dispatch_document_not_research() -> None:
    checker = _default_checker()
    user = _default_user()
    assert checker.can_dispatch_subagent(user, "document-agent") is True
    assert checker.can_dispatch_subagent(user, "data-agent") is True
    assert checker.can_dispatch_subagent(user, "research-agent") is False
