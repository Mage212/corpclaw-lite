"""Provider registry — discovers LLM providers from PROVIDER_*__* env vars.

Environment variable format::

    PROVIDER_{NAME}__{FIELD}=value

Fields:
    TYPE     — "openai" (default) or "anthropic"
    BASE_URL — endpoint URL (e.g. http://localhost:11434/v1)
    API_KEY  — authentication key (optional for local providers)

Example ``.env``::

    PROVIDER_OLLAMA__TYPE=openai
    PROVIDER_OLLAMA__BASE_URL=http://localhost:11434/v1
    PROVIDER_OLLAMA__API_KEY=ollama

    PROVIDER_OPENROUTER__TYPE=openai
    PROVIDER_OPENROUTER__BASE_URL=https://openrouter.ai/api/v1
    PROVIDER_OPENROUTER__API_KEY=sk-or-...

The registry stores **connection details only** (no model). Model selection
happens in routing rules (``config/settings.yaml``).
"""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel

__all__ = [
    "ProviderConnection",
    "ProviderRegistry",
    "ProviderSettings",
]

logger = logging.getLogger(__name__)

_PROVIDER_PREFIX = "PROVIDER_"
_FIELD_SEPARATOR = "__"
_VALID_FIELDS = {"TYPE", "BASE_URL", "API_KEY"}


class ProviderConnection(BaseModel):
    """Connection details for a single LLM provider (no model)."""

    type: str = "openai"
    api_key: str | None = None
    base_url: str | None = None


class ProviderSettings(BaseModel):
    """Settings for building a concrete provider instance.

    Combines connection details with model selection. Used internally by
    ``build_provider()`` and by provider constructors.
    """

    type: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    base_url: str | None = None
    preset: str | None = None


class ProviderRegistry:
    """Registry of provider connections parsed from ``PROVIDER_*__*`` env vars."""

    def __init__(self, connections: dict[str, ProviderConnection] | None = None) -> None:
        self._connections: dict[str, ProviderConnection] = connections or {}

    # ── Construction ────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ProviderRegistry:
        """Parse ``PROVIDER_*__*`` env vars into a registry.

        Args:
            environ: dict to read from. Defaults to ``os.environ``.
        """
        env = environ if environ is not None else dict(os.environ)
        raw: dict[str, dict[str, str]] = {}

        for key, value in env.items():
            if not key.startswith(_PROVIDER_PREFIX):
                continue
            rest = key[len(_PROVIDER_PREFIX) :]
            if _FIELD_SEPARATOR not in rest:
                continue
            name_part, field = rest.split(_FIELD_SEPARATOR, 1)
            if field not in _VALID_FIELDS:
                logger.warning("Unknown provider field '%s' in %s, ignoring", field, key)
                continue
            provider_name = name_part.lower()
            raw.setdefault(provider_name, {})[field.lower()] = value

        connections: dict[str, ProviderConnection] = {}
        for name, fields in raw.items():
            connections[name] = ProviderConnection(
                type=fields.get("type", "openai"),
                api_key=fields.get("api_key"),
                base_url=fields.get("base_url"),
            )
            logger.info(
                "Provider '%s': type=%s base_url=%s",
                name,
                connections[name].type,
                connections[name].base_url or "(default)",
            )

        logger.info("ProviderRegistry: %d providers loaded", len(connections))
        return cls(connections)

    # ── Access ──────────────────────────────────────────────────────────────

    def get(self, name: str) -> ProviderConnection | None:
        """Return a provider connection by name, or ``None``."""
        return self._connections.get(name)

    def list_all(self) -> list[str]:
        """Return all registered provider names."""
        return list(self._connections.keys())

    def __len__(self) -> int:
        return len(self._connections)
