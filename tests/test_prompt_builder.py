"""Tests for agent/prompt.py — build_skill_block() utility."""

from __future__ import annotations

from corpclaw_lite.agent.prompt import build_skill_block
from corpclaw_lite.extensions.skills.base import Skill


def _skill(id_: str, description: str = "desc", instructions: str = "do it") -> Skill:
    return Skill(id=id_, description=description, allowed_for=["*"], instructions=instructions)


def test_empty_returns_none() -> None:
    assert build_skill_block([], []) is None


def test_standalone_only() -> None:
    result = build_skill_block([_skill("s1"), _skill("s2")], [])
    assert result is not None
    assert "s1" in result
    assert "s2" in result
    assert "## Available Skills" in result


def test_plugin_skills_appended() -> None:
    result = build_skill_block([], [_skill("ps1")])
    assert result is not None
    assert "ps1" in result


def test_merge_both() -> None:
    result = build_skill_block([_skill("s1")], [_skill("ps1")])
    assert result is not None
    assert "s1" in result
    assert "ps1" in result


def test_deduplication_standalone_wins() -> None:
    """If standalone and plugin share an id, standalone skill takes priority."""
    standalone = _skill("shared", instructions="standalone version")
    plugin = _skill("shared", instructions="plugin version")
    result = build_skill_block([standalone], [plugin])
    assert result is not None
    assert "standalone version" in result
    assert "plugin version" not in result


def test_deduplication_no_duplicate_block() -> None:
    """The shared id should only appear once in the output."""
    s = _skill("shared")
    result = build_skill_block([s], [s])
    assert result is not None
    assert result.count("shared") == 1


def test_plugin_only_with_empty_standalone() -> None:
    """Plugin skills work when there are no standalone skills."""
    result = build_skill_block([], [_skill("only_plugin")])
    assert result is not None
    assert "only_plugin" in result


def test_order_standalone_first() -> None:
    """Standalone skills appear before plugin skills in the output."""
    s = _skill("aaa", instructions="standalone")
    p = _skill("zzz", instructions="plugin")
    result = build_skill_block([s], [p])
    assert result is not None
    assert result.index("aaa") < result.index("zzz")
