"""Tests for ProviderRegistry — env var parsing and provider lookup."""

from __future__ import annotations

from corpclaw_lite.config.providers import ProviderConnection, ProviderRegistry

# ── Parsing ────────────────────────────────────────────────────────────────────


def test_parse_single_provider() -> None:
    """Single PROVIDER_OLLAMA__* set → one connection."""
    env = {
        "PROVIDER_OLLAMA__TYPE": "openai",
        "PROVIDER_OLLAMA__BASE_URL": "http://localhost:11434/v1",
        "PROVIDER_OLLAMA__API_KEY": "ollama",
    }
    registry = ProviderRegistry.from_env(env)
    assert registry.list_all() == ["ollama"]

    conn = registry.get("ollama")
    assert conn is not None
    assert conn.type == "openai"
    assert conn.base_url == "http://localhost:11434/v1"
    assert conn.api_key == "ollama"


def test_parse_multiple_providers() -> None:
    """Multiple PROVIDER_* groups → multiple connections."""
    env = {
        "PROVIDER_OLLAMA__TYPE": "openai",
        "PROVIDER_OLLAMA__BASE_URL": "http://localhost:11434/v1",
        "PROVIDER_OLLAMA__API_KEY": "ollama",
        "PROVIDER_ANTHROPIC__TYPE": "anthropic",
        "PROVIDER_ANTHROPIC__API_KEY": "sk-ant-test",
        "PROVIDER_OPENROUTER__TYPE": "openai",
        "PROVIDER_OPENROUTER__BASE_URL": "https://openrouter.ai/api/v1",
        "PROVIDER_OPENROUTER__API_KEY": "sk-or-123",
    }
    registry = ProviderRegistry.from_env(env)
    names = registry.list_all()
    assert len(names) == 3
    assert "ollama" in names
    assert "anthropic" in names
    assert "openrouter" in names


def test_ignore_unknown_fields() -> None:
    """Unknown fields (e.g., PROVIDER_FOO__UNKNOWN) are silently ignored."""
    env = {
        "PROVIDER_OLLAMA__TYPE": "openai",
        "PROVIDER_OLLAMA__BASE_URL": "http://localhost:11434/v1",
        "PROVIDER_OLLAMA__UNKNOWN_FIELD": "value",
    }
    registry = ProviderRegistry.from_env(env)
    assert registry.list_all() == ["ollama"]
    conn = registry.get("ollama")
    assert conn is not None
    assert conn.api_key is None


def test_case_insensitive_names() -> None:
    """PROVIDER_MyProvider → name is lowercased to 'myprovider'."""
    env = {
        "PROVIDER_MyProvider__TYPE": "openai",
        "PROVIDER_MyProvider__API_KEY": "key",
    }
    registry = ProviderRegistry.from_env(env)
    assert registry.list_all() == ["myprovider"]
    assert registry.get("myprovider") is not None
    assert registry.get("MyProvider") is None  # lookup is case-sensitive


def test_default_type_is_openai() -> None:
    """If only BASE_URL is set, type defaults to 'openai'."""
    env = {
        "PROVIDER_LOCAL__BASE_URL": "http://localhost:1234/v1",
    }
    registry = ProviderRegistry.from_env(env)
    conn = registry.get("local")
    assert conn is not None
    assert conn.type == "openai"
    assert conn.api_key is None


def test_empty_registry() -> None:
    """No PROVIDER_* vars → empty registry."""
    env: dict[str, str] = {"OTHER_VAR": "value", "SOMETHING": "else"}
    registry = ProviderRegistry.from_env(env)
    assert registry.list_all() == []
    assert len(registry) == 0


def test_no_separator_ignored() -> None:
    """PROVIDER_FOO (no __) is ignored."""
    env = {"PROVIDER_FOO": "value"}
    registry = ProviderRegistry.from_env(env)
    assert registry.list_all() == []


def test_partial_fields() -> None:
    """Only API_KEY set, no BASE_URL → connection with None base_url."""
    env = {
        "PROVIDER_ANTHROPIC__TYPE": "anthropic",
        "PROVIDER_ANTHROPIC__API_KEY": "sk-ant-key",
    }
    registry = ProviderRegistry.from_env(env)
    conn = registry.get("anthropic")
    assert conn is not None
    assert conn.type == "anthropic"
    assert conn.api_key == "sk-ant-key"
    assert conn.base_url is None


# ── Access ─────────────────────────────────────────────────────────────────────


def test_get_unknown_returns_none() -> None:
    """get() returns None for unknown provider."""
    registry = ProviderRegistry()
    assert registry.get("nonexistent") is None


def test_len() -> None:
    """len() returns correct count."""
    env = {
        "PROVIDER_A__TYPE": "openai",
        "PROVIDER_B__TYPE": "anthropic",
    }
    registry = ProviderRegistry.from_env(env)
    assert len(registry) == 2


def test_manual_construction() -> None:
    """Registry can be constructed with explicit connections dict."""
    connections = {
        "test": ProviderConnection(type="openai", base_url="http://test:1234/v1"),
    }
    registry = ProviderRegistry(connections)
    assert registry.list_all() == ["test"]
    conn = registry.get("test")
    assert conn is not None
    assert conn.type == "openai"
