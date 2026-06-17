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
        self.name: str = data.get("description", "Unknown")
        self.profile: str = data.get("profile", "default")
        self.allowed_tools: list[str] = data.get("allowed_tools", ["*"])
        self.allowed_skills: list[str] = data.get("allowed_skills", ["*"])
        self.allowed_plugins: list[str] = data.get("allowed_plugins", ["*"])
        self.allowed_subagents: list[str] = data.get("allowed_subagents", [])
        self.allowed_mcp: list[str] = data.get("allowed_mcp", ["*"])

        budget_data = data.get("budget", {})
        self.budget = SimpleBudgetGuardConfig(
            max_iterations=budget_data.get("max_iterations", 15),
            max_tool_calls=budget_data.get("max_tool_calls", 30),
            max_time_ms=300000,
        )


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
            for slug, dept_data in depts.items():
                slug_str = str(slug)
                dept_dict = cast(dict[str, Any], dept_data)
                existing = self._departments.get(slug_str)
                if merge and existing is not None:
                    self._departments[slug_str] = self._merge_department(existing, dept_dict)
                else:
                    self._departments[slug_str] = DepartmentConfig(dept_dict)

            logger.info("Loaded %d departments from %s", len(depts), file_path)
        except Exception as e:
            logger.error("Failed to load departments from %s: %s", file_path, e)

    def _merge_department(
        self, existing: DepartmentConfig, overlay_data: dict[str, Any]
    ) -> DepartmentConfig:
        """Merge an overlay department dict into an existing DepartmentConfig."""
        overlay = DepartmentConfig(overlay_data)

        merged = DepartmentConfig(overlay_data)
        # description / profile: overlay wins (already set via DepartmentConfig).
        for field in _ALLOWLIST_FIELDS:
            setattr(
                merged,
                field,
                _union_allowlists(
                    cast(list[str], getattr(existing, field)),
                    cast(list[str], getattr(overlay, field)),
                ),
            )

        # Budget: overlay overrides max_iterations / max_tool_calls only when
        # the overlay file actually specified a budget block; otherwise inherit.
        # max_time_ms is never merged (D-037: always from settings).
        overlay_budget = overlay_data.get("budget")
        if isinstance(overlay_budget, dict):
            merged.budget = SimpleBudgetGuardConfig(
                max_iterations=overlay_budget.get("max_iterations", existing.budget.max_iterations),
                max_tool_calls=overlay_budget.get("max_tool_calls", existing.budget.max_tool_calls),
                max_time_ms=300000,
            )
        else:
            merged.budget = existing.budget
        return merged

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
