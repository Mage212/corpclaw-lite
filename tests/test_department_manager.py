"""Tests for DepartmentManager multi-source merge (PR-3).

Covers the merge channel for departments: loading multiple files unions
allowlists (with wildcard normalization), overrides budget when present,
and inherits budget when absent. The default load (merge=False) keeps the
pre-existing replace behavior.
"""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.config.settings import ExtensionsSettings, Settings
from corpclaw_lite.departments.manager import (
    DepartmentManager,
    resolve_department_files,
)


def _write_depts(path: Path, departments: dict[str, dict[str, object]]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"departments": departments}), encoding="utf-8")


def test_load_file_merge_unions_tools(tmp_path: Path) -> None:
    """Overlay adds a tool to an existing department's allowlist."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"allowed_tools": ["read_file"]}})
    _write_depts(overlay, {"eng": {"allowed_tools": ["write_file"]}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay, merge=True)

    dept = mgr.get_department("eng")
    assert dept is not None
    assert dept.allowed_tools == ["read_file", "write_file"]


def test_load_file_merge_wildcard_normalization(tmp_path: Path) -> None:
    """Union with '*' collapses to ['*']."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"allowed_tools": ["*"]}})
    _write_depts(overlay, {"eng": {"allowed_tools": ["corp_tool"]}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay, merge=True)

    dept = mgr.get_department("eng")
    assert dept is not None
    assert dept.allowed_tools == ["*"]


def test_load_file_merge_new_department(tmp_path: Path) -> None:
    """A department only present in the overlay is added."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"allowed_tools": ["read_file"]}})
    _write_depts(overlay, {"sales": {"allowed_tools": ["crm_lookup"]}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay, merge=True)

    assert set(mgr._departments) == {"eng", "sales"}
    sales = mgr.get_department("sales")
    assert sales is not None
    assert sales.allowed_tools == ["crm_lookup"]


def test_load_file_merge_budget_override(tmp_path: Path) -> None:
    """Overlay budget overrides max_iterations / max_tool_calls."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"budget": {"max_iterations": 15, "max_tool_calls": 30}}})
    _write_depts(overlay, {"eng": {"budget": {"max_iterations": 25, "max_tool_calls": 60}}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay, merge=True)

    dept = mgr.get_department("eng")
    assert dept is not None
    assert dept.budget.max_iterations == 25
    assert dept.budget.max_tool_calls == 60


def test_load_file_merge_budget_inherited_when_absent(tmp_path: Path) -> None:
    """When the overlay has no budget block, the default budget is inherited."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"budget": {"max_iterations": 20, "max_tool_calls": 50}}})
    _write_depts(overlay, {"eng": {"allowed_tools": ["extra_tool"]}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay, merge=True)

    dept = mgr.get_department("eng")
    assert dept is not None
    assert dept.budget.max_iterations == 20
    assert dept.budget.max_tool_calls == 50


def test_load_file_no_merge_replaces(tmp_path: Path) -> None:
    """merge=False (default): a second load replaces the department entirely."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"allowed_tools": ["read_file", "write_file"]}})
    _write_depts(overlay, {"eng": {"allowed_tools": ["read_file"]}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay)  # merge=False

    dept = mgr.get_department("eng")
    assert dept is not None
    # Replaced, not unioned: write_file is gone.
    assert dept.allowed_tools == ["read_file"]


def test_load_file_merge_subagents_union_preserves_empty_default(tmp_path: Path) -> None:
    """allowed_subagents (default []) unions with overlay without collapsing."""
    default = tmp_path / "config" / "departments.yaml"
    overlay = tmp_path / "overlay" / "config" / "departments.yaml"
    _write_depts(default, {"eng": {"allowed_subagents": []}})
    _write_depts(overlay, {"eng": {"allowed_subagents": ["data-agent"]}})

    mgr = DepartmentManager()
    mgr.load_file(default)
    mgr.load_file(overlay, merge=True)

    dept = mgr.get_department("eng")
    assert dept is not None
    assert dept.allowed_subagents == ["data-agent"]


def test_resolve_department_files_default_and_overlays(tmp_path: Path) -> None:
    """resolve_department_files returns [default, ...existing overlays], skipping
    empty/missing entries (guards against unresolved ${VAR})."""
    overlay_root = tmp_path / "overlay"
    _write_depts(overlay_root / "config" / "departments.yaml", {"sales": {}})

    settings = Settings(extensions=ExtensionsSettings(extra_paths=[str(overlay_root), ""]))
    paths = resolve_department_files(settings, tmp_path)

    assert len(paths) == 2
    assert paths[0] == (tmp_path / "config" / "departments.yaml").resolve()
    assert paths[1] == (overlay_root / "config" / "departments.yaml").resolve()
