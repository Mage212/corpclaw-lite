"""Tests for DC-016 host-tools / container.enabled gate (B-097)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from corpclaw_lite.exceptions import StartupConfigurationError
from corpclaw_lite.security.host_tools_gate import (
    assert_host_tools_allowed,
    host_tools_allowed_by_env,
    prod_container_enforced,
)


def test_container_enabled_is_noop() -> None:
    assert_host_tools_allowed(container_enabled=True, surface="multiuser")


def test_level1_raises_without_allow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORPCLAW_ALLOW_HOST_TOOLS", raising=False)
    with pytest.raises(StartupConfigurationError, match="not explicitly allowed"):
        assert_host_tools_allowed(container_enabled=False, surface="dev")


def test_level1_ok_with_allow_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPCLAW_ALLOW_HOST_TOOLS", "1")
    monkeypatch.delenv("CORPCLAW_ENFORCE_PROD_CONTAINER", raising=False)
    assert_host_tools_allowed(container_enabled=False, surface="dev")
    assert host_tools_allowed_by_env() is True


def test_level2_raises_on_multiuser_when_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPCLAW_ALLOW_HOST_TOOLS", "1")
    monkeypatch.delenv("CORPCLAW_ENFORCE_PROD_CONTAINER", raising=False)
    with pytest.raises(StartupConfigurationError, match="multi-user"):
        assert_host_tools_allowed(container_enabled=False, surface="multiuser")
    assert prod_container_enforced() is True


def test_level2_ok_when_enforce_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPCLAW_ALLOW_HOST_TOOLS", "1")
    monkeypatch.setenv("CORPCLAW_ENFORCE_PROD_CONTAINER", "false")
    assert_host_tools_allowed(container_enabled=False, surface="multiuser")
    assert prod_container_enforced() is False


def test_build_agent_stack_refuses_host_tools_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: factory Level 1 gate fires when containers off and no ALLOW."""
    from corpclaw_lite.agent.factory import build_agent_stack
    from corpclaw_lite.config import loader as config_loader
    from corpclaw_lite.config.settings import (
        ContainerSettings,
        LLMSettings,
        RoutingRule,
        Settings,
    )

    _original = config_loader.load_settings

    def _mock_load(path: object = None) -> Settings:  # type: ignore[misc]
        settings = _original(path)  # type: ignore[arg-type]
        settings.container = ContainerSettings(enabled=False)
        settings.llm = LLMSettings(
            routing=[RoutingRule(task_kind="default", provider="ollama", model="t")]
        )
        return settings

    monkeypatch.delenv("CORPCLAW_ALLOW_HOST_TOOLS", raising=False)
    env = {
        "PROVIDER_OLLAMA__TYPE": "openai",
        "PROVIDER_OLLAMA__BASE_URL": "http://test:11434/v1",
        "PROVIDER_OLLAMA__API_KEY": "ollama",
    }
    with (
        patch.object(config_loader, "load_settings", side_effect=_mock_load),
        patch.dict(os.environ, env, clear=False),
    ):
        # Clear ALLOW if fixture or outer env set it
        os.environ.pop("CORPCLAW_ALLOW_HOST_TOOLS", None)
        with pytest.raises(StartupConfigurationError, match="not explicitly allowed"):
            build_agent_stack()
