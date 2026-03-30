"""Tests for container agent_worker real tool execution."""

from __future__ import annotations

from corpclaw_lite.container.agent_worker import _build_container_registry


def test_build_container_registry_has_expected_tools() -> None:
    """Verify the container registry contains the right subset of tools."""
    registry = _build_container_registry()
    expected = {
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "search_files",
        "exec_script",
        "normalize_excel",
    }
    actual = {t.name for t in registry.list_all()}
    assert actual == expected


def test_build_container_registry_excludes_host_tools() -> None:
    """Host-only tools should not be in the container registry."""
    registry = _build_container_registry()
    names = {t.name for t in registry.list_all()}
    for excluded in ["send_file", "memory_store", "memory_recall", "web_fetch", "read_image"]:
        assert excluded not in names
