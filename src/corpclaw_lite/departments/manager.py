from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from corpclaw_lite.agent.guards import SimpleBudgetGuardConfig

__all__ = [
    "DepartmentConfig",
    "DepartmentManager",
    "resolve_department_files",
]

if TYPE_CHECKING:
    from corpclaw_lite.config.settings import Settings

logger = logging.getLogger(__name__)

# The five RBAC allowlist fields, in the order they appear on DepartmentConfig.
_ALLOWLIST_FIELDS: tuple[str, ...] = (
    "allowed_tools",
    "allowed_skills",
    "allowed_plugins",
    "allowed_subagents",
    "allowed_mcp",
)


def _union_allowlists(a: list[str], b: list[str]) -> list[str]:
    """Union two allowlists preserving order, de-duplicating, and collapsing
    to ``["*"]`` when the merged result contains a wildcard (since ``"*"``
    already means "everything permitted", listing anything alongside it is
    redundant)."""
    merged = list(dict.fromkeys([*a, *b]))
    if "*" in merged:
        return ["*"]
    return merged


class DepartmentConfig:
    """RBAC configuration for a specific department."""

    def __init__(self, data: dict[str, Any]):
        name_obj: object = data.get("description", "Unknown")
        profile_obj: object = data.get("profile", "default")
        if not isinstance(name_obj, str) or not isinstance(profile_obj, str):
            raise ValueError("Department description and profile must be strings")
        self.name = name_obj
        self.profile = profile_obj
        self.allowed_tools = self._allowlist(data, "allowed_tools", ["*"])
        self.allowed_skills = self._allowlist(data, "allowed_skills", ["*"])
        self.allowed_plugins = self._allowlist(data, "allowed_plugins", ["*"])
        self.allowed_subagents = self._allowlist(data, "allowed_subagents", [])
        self.allowed_mcp = self._allowlist(data, "allowed_mcp", ["*"])

        budget_obj: object = data.get("budget", {})
        if not isinstance(budget_obj, dict):
            raise ValueError("Department field 'budget' must be a mapping")
        budget_data = cast(dict[str, object], budget_obj)
        max_iterations = budget_data.get("max_iterations", 15)
        max_tool_calls = budget_data.get("max_tool_calls", 30)
        if (
            not isinstance(max_iterations, int)
            or isinstance(max_iterations, bool)
            or not isinstance(max_tool_calls, int)
            or isinstance(max_tool_calls, bool)
        ):
            raise ValueError("Department budget values must be integers")
        self.budget = SimpleBudgetGuardConfig(
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
            max_time_ms=300000,
        )

    @staticmethod
    def _allowlist(data: dict[str, Any], field: str, default: list[str]) -> list[str]:
        raw_obj: object = data.get(field, default)
        if not isinstance(raw_obj, list):
            raise ValueError(f"Department field '{field}' must be a list of strings")
        raw = cast(list[object], raw_obj)
        if any(not isinstance(item, str) for item in raw):
            raise ValueError(f"Department field '{field}' must be a list of strings")
        return [cast(str, item) for item in raw]

    def to_data(self) -> dict[str, Any]:
        return {
            "description": self.name,
            "profile": self.profile,
            "allowed_tools": list(self.allowed_tools),
            "allowed_skills": list(self.allowed_skills),
            "allowed_plugins": list(self.allowed_plugins),
            "allowed_subagents": list(self.allowed_subagents),
            "allowed_mcp": list(self.allowed_mcp),
            "budget": {
                "max_iterations": self.budget.max_iterations,
                "max_tool_calls": self.budget.max_tool_calls,
            },
        }


class DepartmentManager:
    """Loads and manages department configurations from departments.yaml.

    Supports loading multiple files (default + overlays). When ``merge=True``,
    a department that already exists is merged instead of replaced: allowlists
    are unioned (with wildcard normalization) and ``max_iterations`` /
    ``max_tool_calls`` are overridden by the overlay when present (``max_time_ms``
    is never merged — it is always sourced from settings, see D-037).
    """

    def __init__(self) -> None:
        self._departments: dict[str, DepartmentConfig] = {}

    def load_file(self, path: Path | str, *, merge: bool = False) -> None:
        """Load departments from a YAML file.

        Args:
            path: Path to a departments.yaml file.
            merge: When False (default), an existing department slug is replaced
                by the new definition (backward-compatible). When True, an
                existing department is merged — allowlists unioned, budget
                overridden by the overlay where present.
        """
        file_path = Path(path)
        if not file_path.exists():
            logger.warning("Departments config not found: %s", file_path)
            return

        try:
            with open(file_path, encoding="utf-8") as f:
                data = cast(dict[str, Any], yaml.safe_load(f) or {})

            depts = cast(dict[str, Any], data.get("departments", {}))
            pending = dict(self._departments)
            for slug, dept_data in depts.items():
                slug_str = str(slug)
                if not isinstance(dept_data, dict):
                    raise ValueError(f"Department '{slug_str}' must be a mapping")
                dept_dict = cast(dict[str, Any], dept_data)
                existing = pending.get(slug_str)
                if merge and existing is not None:
                    pending[slug_str] = self._merge_department(existing, dept_dict)
                else:
                    parsed = DepartmentConfig(dept_dict)
                    pending[slug_str] = parsed
                    for field in _ALLOWLIST_FIELDS:
                        implicit_wildcard = "*" in cast(list[str], getattr(parsed, field))
                        if field not in dept_dict and implicit_wildcard:
                            logger.warning(
                                "Department '%s' omits %s; compatibility default ['*'] applies",
                                slug_str,
                                field,
                            )

            # Replace only after every department in the file validated.
            self._departments = pending

            logger.info("Loaded %d departments from %s", len(depts), file_path)
        except Exception as e:
            logger.error("Failed to load departments from %s: %s", file_path, e)

    def _merge_department(
        self, existing: DepartmentConfig, overlay_data: dict[str, Any]
    ) -> DepartmentConfig:
        """Merge an overlay department dict into an existing DepartmentConfig."""
        # Start from the complete base. An omitted overlay field means inherit;
        # it must never materialise DepartmentConfig's compatibility wildcard.
        merged_data = existing.to_data()
        if "description" in overlay_data:
            merged_data["description"] = overlay_data["description"]
        if "profile" in overlay_data:
            merged_data["profile"] = overlay_data["profile"]
        for field in _ALLOWLIST_FIELDS:
            if field in overlay_data:
                raw_obj: object = overlay_data[field]
                if not isinstance(raw_obj, list):
                    raise ValueError(f"Department field '{field}' must be a list of strings")
                raw = cast(list[object], raw_obj)
                if any(not isinstance(item, str) for item in raw):
                    raise ValueError(f"Department field '{field}' must be a list of strings")
                merged_data[field] = _union_allowlists(
                    cast(list[str], getattr(existing, field)), [cast(str, item) for item in raw]
                )

        # Budget: overlay overrides max_iterations / max_tool_calls only when
        # the overlay file actually specified a budget block; otherwise inherit.
        # max_time_ms is never merged (D-037: always from settings).
        overlay_budget: object = overlay_data.get("budget")
        if "budget" in overlay_data and not isinstance(overlay_budget, dict):
            raise ValueError("Department field 'budget' must be a mapping")
        if isinstance(overlay_budget, dict):
            budget_dict = cast(dict[str, Any], overlay_budget)
            merged_data["budget"] = {
                "max_iterations": budget_dict.get("max_iterations", existing.budget.max_iterations),
                "max_tool_calls": budget_dict.get("max_tool_calls", existing.budget.max_tool_calls),
            }
        return DepartmentConfig(merged_data)

    def get_department(self, slug: str) -> DepartmentConfig | None:
        return self._departments.get(slug)


def resolve_department_files(settings: Settings, project_root: Path) -> list[Path]:
    """Ordered department config paths: default first, overlays later.

    Mirrors the mirror-layout of the other extension kinds, but kept separate
    from ``resolve_dirs`` because departments use a union-merge channel rather
    than an override channel (see spec §4/§5). Returns ``[default, ...overlays]``
    where later = higher priority; non-existent and empty paths are skipped.
    """
    result: list[Path] = [(project_root / "config" / "departments.yaml").resolve()]

    for raw in settings.extensions.extra_paths:
        stripped = str(raw).strip()
        if not stripped:
            continue
        candidate = (Path(stripped) / "config" / "departments.yaml").resolve()
        if not candidate.exists():
            logger.debug("departments: skip missing overlay path: %s", candidate)
            continue
        result.append(candidate)

    return result
