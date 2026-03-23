"""Tests for SubagentRegistry YAML loading."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.extensions.subagents.registry import SubagentRegistry


def test_load_directory_with_yaml(tmp_path: Path) -> None:
    (tmp_path / "helper.yaml").write_text(
        "id: helper\nname: Helper\ndescription: A helper subagent\n"
        "capabilities: [summarize]\nallowed_tools: ['read_file']\nprompt_path: prompts/helper.md\n"
    )
    registry = SubagentRegistry()
    registry.load_directory(tmp_path)

    specs = registry.list_all()
    assert len(specs) == 1
    assert specs[0].id == "helper"
    assert specs[0].name == "Helper"
    assert "summarize" in specs[0].capabilities


def test_load_directory_nonexistent() -> None:
    registry = SubagentRegistry()
    registry.load_directory("/nonexistent/path")
    assert registry.list_all() == []


def test_load_directory_invalid_yaml(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text(":::\ninvalid: [")
    registry = SubagentRegistry()
    registry.load_directory(tmp_path)
    assert registry.list_all() == []


def test_get_spec() -> None:
    registry = SubagentRegistry()
    assert registry.get_spec("nonexistent") is None
