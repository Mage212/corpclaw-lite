from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from corpclaw_lite.agent.guards import SimpleBudgetGuardConfig
from corpclaw_lite.departments.manager import DepartmentManager
from corpclaw_lite.users.models import User

__all__ = [
    "PermissionChecker",
]

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from corpclaw_lite.extensions.tools.base import Tool


class PermissionChecker:
    """Centralized RBAC logic defining what a user can do based on their department."""

    def __init__(self, manager: DepartmentManager) -> None:
        self._manager = manager

    def _is_allowed(self, allowed_list: list[str], item: str) -> bool:
        if not allowed_list:
            return False
        if "*" in allowed_list:
            return True
        return item in allowed_list

    def can_use_tool(self, user: User, tool_name: str) -> bool:
        dept = self._manager.get_department(user.department)
        if not dept:
            logger.debug(
                "Permission check: can_use_tool dept=%s not found, denying tool=%s",
                user.department,
                tool_name,
            )
            return False
        result = self._is_allowed(dept.allowed_tools, tool_name)
        logger.debug(
            "Permission check: can_use_tool dept=%s tool=%s result=%s",
            user.department,
            tool_name,
            result,
        )
        return result

    def can_use_registered_tool(
        self,
        user: User,
        tool: Tool,
        *,
        enforce_tool_allowlist: bool = True,
    ) -> bool:
        """Return True when the user can invoke this registered tool.

        ``enforce_tool_allowlist=False`` is used by subagents: the subagent
        spec's allowed_tools list selects the tool set, but source-level
        plugin/MCP restrictions still apply.
        """
        if enforce_tool_allowlist and not self.can_use_tool(user, tool.name):
            return False

        source_kind = getattr(tool, "source_kind", None)
        source_name = getattr(tool, "source_name", None)
        raw_allowed_departments: object = getattr(tool, "allowed_departments", None)

        if source_kind == "plugin":
            if not source_name or not self.can_use_plugin(user, source_name):
                return False
            if not isinstance(raw_allowed_departments, list):
                return False
            raw_department_list = cast(list[object], raw_allowed_departments)
            allowed_departments: list[str] = []
            for raw_department in raw_department_list:
                if not isinstance(raw_department, str):
                    return False
                allowed_departments.append(raw_department)
            return self._is_allowed(allowed_departments, user.department)

        if source_kind == "mcp":
            if not source_name:
                return False
            return self.can_use_mcp(user, source_name)

        return True

    def can_use_skill(self, user: User, skill_id: str) -> bool:
        dept = self._manager.get_department(user.department)
        if not dept:
            return False
        return self._is_allowed(dept.allowed_skills, skill_id)

    def can_use_plugin(self, user: User, plugin_name: str) -> bool:
        dept = self._manager.get_department(user.department)
        if not dept:
            return False
        return self._is_allowed(dept.allowed_plugins, plugin_name)

    def can_dispatch_subagent(self, user: User, subagent_id: str) -> bool:
        dept = self._manager.get_department(user.department)
        if not dept:
            logger.debug(
                "Permission check: can_dispatch_subagent dept=%s not found, denying subagent=%s",
                user.department,
                subagent_id,
            )
            return False
        result = self._is_allowed(dept.allowed_subagents, subagent_id)
        logger.debug(
            "Permission check: can_dispatch_subagent dept=%s subagent=%s result=%s",
            user.department,
            subagent_id,
            result,
        )
        return result

    def can_use_mcp(self, user: User, server_name: str) -> bool:
        dept = self._manager.get_department(user.department)
        if not dept:
            return False
        return self._is_allowed(dept.allowed_mcp, server_name)

    def get_budget(self, user: User) -> SimpleBudgetGuardConfig:
        """Returns the specific budget config for the user's department.
        Fallback to safe defaults if department not found.
        """
        dept = self._manager.get_department(user.department)
        if not dept:
            return SimpleBudgetGuardConfig(max_iterations=10, max_tool_calls=20, max_time_ms=300000)
        return dept.budget
