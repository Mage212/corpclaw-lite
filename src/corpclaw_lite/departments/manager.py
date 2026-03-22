import logging
from pathlib import Path
from typing import Any, cast

import yaml

from corpclaw_lite.agent.guards import SimpleBudgetGuardConfig

logger = logging.getLogger(__name__)


class DepartmentConfig:
    """RBAC configuration for a specific department."""

    def __init__(self, data: dict[str, Any]):
        self.name: str = data.get("name", "Unknown")
        self.profile: str = data.get("profile", "default")
        self.allowed_tools: list[str] = data.get("allowed_tools", ["*"])
        self.allowed_skills: list[str] = data.get("allowed_skills", ["*"])
        self.allowed_plugins: list[str] = data.get("allowed_plugins", ["*"])
        self.allowed_subagents: list[str] = data.get("allowed_subagents", ["*"])
        self.allowed_mcp: list[str] = data.get("allowed_mcp", ["*"])

        budget_data = data.get("budget", {})
        self.budget = SimpleBudgetGuardConfig(
            max_iterations=budget_data.get("max_steps", 15),
            max_tool_calls=budget_data.get("max_tool_calls", 30),
            max_time_ms=budget_data.get("max_wall_time_ms", 120000),
        )


class DepartmentManager:
    """Loads and manages department configurations from departments.yaml."""

    def __init__(self) -> None:
        self._departments: dict[str, DepartmentConfig] = {}

    def load_file(self, path: Path | str) -> None:
        file_path = Path(path)
        if not file_path.exists():
            logger.warning(f"Departments config not found: {file_path}")
            return

        try:
            with open(file_path, encoding="utf-8") as f:
                data = cast(dict[str, Any], yaml.safe_load(f) or {})

            depts = cast(dict[str, Any], data.get("departments", {}))
            for slug, dept_data in depts.items():
                self._departments[str(slug)] = DepartmentConfig(cast(dict[str, Any], dept_data))

            logger.info(f"Loaded {len(self._departments)} departments")
        except Exception as e:
            logger.error(f"Failed to load departments from {file_path}: {e}")

    def get_department(self, slug: str) -> DepartmentConfig | None:
        return self._departments.get(slug)
