"""Tests for SubagentRegistry YAML loading."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
from corpclaw_lite.extensions.subagents.watcher import SubagentHotReloader


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


def test_load_directory_reads_per_subagent_fields(tmp_path: Path) -> None:
    """B-049/B-047: max_wall_time_ms, terminal_tool, required_before_terminal map to spec."""
    (tmp_path / "research.yaml").write_text(
        "id: research-agent\nname: Research Agent\ndescription: research\n"
        "allowed_tools: ['research_finalize']\nprompt_path: p.md\n"
        "direct_response: true\nmax_wall_time_ms: 600000\n"
        "terminal_tool: research_finalize\nrequired_before_terminal: [research_list_facts]\n"
    )
    registry = SubagentRegistry()
    registry.load_directory(tmp_path)

    spec = registry.get_spec("research-agent")
    assert spec is not None
    assert spec.direct_response is True
    assert spec.max_wall_time_ms == 600000
    assert spec.terminal_tool == "research_finalize"
    assert spec.required_before_terminal == ["research_list_facts"]


def test_load_directory_defaults_when_fields_absent(tmp_path: Path) -> None:
    """Fields are optional: unspecified subagents fall back to neutral defaults."""
    (tmp_path / "plain.yaml").write_text(
        "id: plain\nname: Plain\ndescription: d\nallowed_tools: ['read_file']\n"
    )
    registry = SubagentRegistry()
    registry.load_directory(tmp_path)

    spec = registry.get_spec("plain")
    assert spec is not None
    assert spec.direct_response is False
    assert spec.max_wall_time_ms is None
    assert spec.terminal_tool is None
    assert spec.required_before_terminal == []


def test_watcher_load_spec_matches_registry(tmp_path: Path) -> None:
    """Regression: the hot-reloader (_load_spec) must produce the same spec as
    SubagentRegistry.load_directory. Previously watcher omitted direct_response
    (and the new per-subagent fields), silently dropping them on hot reload.
    """
    yaml_path = tmp_path / "research.yaml"
    yaml_path.write_text(
        "id: research-agent\nname: Research Agent\ndescription: research\n"
        "allowed_tools: ['research_finalize']\nprompt_path: p.md\n"
        "direct_response: true\nmax_wall_time_ms: 600000\n"
        "terminal_tool: research_finalize\nrequired_before_terminal: [research_list_facts]\n"
    )

    registry = SubagentRegistry()
    registry.load_directory(tmp_path)
    from_registry = registry.get_spec("research-agent")
    from_watcher = SubagentHotReloader._load_spec(yaml_path)

    assert from_registry is not None
    assert from_watcher is not None
    # The two loaders must agree on every field.
    assert from_watcher == from_registry
    # Explicit checks for the previously-divergent field.
    assert from_watcher.direct_response is True
    assert from_watcher.max_wall_time_ms == 600000


def test_subagent_overlay_override(tmp_path: Path, caplog) -> None:
    """A subagent spec loaded later overrides an earlier one by id and logs a WARN."""
    import logging

    default_dir = tmp_path / "default"
    overlay_dir = tmp_path / "overlay"
    default_dir.mkdir()
    overlay_dir.mkdir()

    (default_dir / "research.yaml").write_text(
        "id: research-agent\nname: Default Research\ndescription: default\n"
        "allowed_tools: ['read_file']\nprompt_path: p1.md\n"
    )
    (overlay_dir / "research.yaml").write_text(
        "id: research-agent\nname: Overlay Research\ndescription: overlay\n"
        "allowed_tools: ['read_file', 'write_file']\nprompt_path: p2.md\n"
    )

    registry = SubagentRegistry()
    with caplog.at_level(logging.WARNING, logger="corpclaw_lite.extensions.subagents.registry"):
        registry.load_directory(default_dir)
        registry.load_directory(overlay_dir)

    spec = registry.get_spec("research-agent")
    assert spec is not None
    assert spec.name == "Overlay Research"  # overlay won
    assert spec.prompt_path == "p2.md"
    assert any("overridden by overlay" in r.message for r in caplog.records)
