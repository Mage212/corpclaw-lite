"""Tests for settings models touched by D-056 PR2 (PhasePolicySettings, RoutingRule)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from corpclaw_lite.config.settings import (
    AgentSettings,
    PhasePolicySettings,
    RoutingRule,
)

# ── PhasePolicySettings ───────────────────────────────────────────────────────


def test_phase_policy_settings_defaults() -> None:
    """Default PhasePolicySettings: enabled, research markers, off/off/default."""
    s = PhasePolicySettings()
    assert s.enabled is True
    assert s.aggregation_markers == ["research_list_facts"]
    assert "research_search" in s.gathering_tools
    assert "research_store_fact" in s.gathering_tools
    assert s.closing_thinking == "off"
    assert s.gathering_thinking == "off"
    assert s.aggregation_thinking == "default"


def test_phase_policy_settings_rejects_invalid_thinking_mode() -> None:
    """Thinking-mode literals are validated against default|off|budget."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PhasePolicySettings(closing_thinking="bogus")  # type: ignore[arg-type]


def test_agent_settings_has_phase_policy_default() -> None:
    """AgentSettings carries a PhasePolicySettings field by default."""
    a = AgentSettings()
    assert isinstance(a.phase_policy, PhasePolicySettings)
    assert a.phase_policy.enabled is True


# ── RoutingRule (D-056 split fields) ──────────────────────────────────────────


def test_routing_rule_legacy_preset_field() -> None:
    """Legacy RoutingRule.preset still accepted (back-compat)."""
    r = RoutingRule(task_kind="default", provider="ollama", model="m", preset="legacy")
    assert r.preset == "legacy"
    assert r.sampling is None
    assert r.model_profile is None


def test_routing_rule_split_fields() -> None:
    """New RoutingRule.model_profile / sampling fields accepted."""
    r = RoutingRule(
        task_kind="default",
        provider="ollama",
        model="m",
        sampling="fast-off",
        model_profile="qwen-test",
    )
    assert r.sampling == "fast-off"
    assert r.model_profile == "qwen-test"
    assert r.preset is None


def test_routing_rule_both_sampling_and_preset_allowed() -> None:
    """Both sampling and preset may be set (sampling wins at resolution time)."""
    r = RoutingRule(
        task_kind="default",
        provider="ollama",
        model="m",
        sampling="new-rule",
        preset="legacy-rule",
    )
    assert r.sampling == "new-rule"
    assert r.preset == "legacy-rule"


# ── End-to-end: settings.yaml loads phase_policy ─────────────────────────────


def test_settings_yaml_phase_policy_loads(tmp_path: Path) -> None:
    """A settings.yaml with agent.phase_policy parses into PhasePolicySettings."""
    from corpclaw_lite.config.loader import load_settings

    yaml = textwrap.dedent("""\
        agent:
          phase_policy:
            enabled: true
            aggregation_markers: ["finalize"]
            gathering_tools: ["search", "fetch"]
            closing_thinking: "off"
            gathering_thinking: "off"
            aggregation_thinking: "default"
    """)
    p = tmp_path / "settings.yaml"
    p.write_text(yaml, encoding="utf-8")
    s = load_settings(p)
    assert s.agent.phase_policy.enabled is True
    assert s.agent.phase_policy.aggregation_markers == ["finalize"]
    assert s.agent.phase_policy.gathering_tools == ["search", "fetch"]


def test_settings_yaml_phase_policy_thinking_off_unquoted(tmp_path: Path) -> None:
    """thinking_mode: off (unquoted) in YAML → coerced to 'off' string (D-056 YAML fix)."""
    from corpclaw_lite.config.loader import load_settings

    yaml = textwrap.dedent("""\
        agent:
          phase_policy:
            closing_thinking: off
    """)
    p = tmp_path / "settings.yaml"
    p.write_text(yaml, encoding="utf-8")
    s = load_settings(p)
    assert s.agent.phase_policy.closing_thinking == "off"
