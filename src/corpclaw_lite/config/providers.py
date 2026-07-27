"""Provider registry — discovers LLM providers from PROVIDER_*__* env vars.

Environment variable format::

    PROVIDER_{NAME}__{FIELD}=value

Fields:
    TYPE            — "openai" (default) or "anthropic"
    BASE_URL        — endpoint URL (e.g. http://localhost:11434/v1)
    API_KEY         — authentication key (optional for local providers)
    CONNECT_TIMEOUT — HTTP connect timeout in seconds (default 5.0)
    READ_TIMEOUT    — HTTP read timeout in seconds (default 600.0; see note)
    WRITE_TIMEOUT   — HTTP write (request body) timeout in seconds (default 600.0)
    POOL_TIMEOUT    — connection-pool acquisition timeout in seconds (default 600.0)
    MAX_RETRIES     — SDK automatic retries on transport errors (default 2)

Example ``.env``::

    PROVIDER_OLLAMA__TYPE=openai
    PROVIDER_OLLAMA__BASE_URL=http://localhost:11434/v1
    PROVIDER_OLLAMA__API_KEY=ollama
    # Local LLM: prompt processing of large contexts can take minutes.
    PROVIDER_OLLAMA__READ_TIMEOUT=1200

    PROVIDER_OPENROUTER__TYPE=openai
    PROVIDER_OPENROUTER__BASE_URL=https://openrouter.ai/api/v1
    PROVIDER_OPENROUTER__API_KEY=sk-or-...
    PROVIDER_OPENROUTER__READ_TIMEOUT=120
    PROVIDER_OPENROUTER__MAX_RETRIES=2

The registry stores **connection details only** (no model). Model selection
happens in routing rules (``config/settings.yaml``).

Timeout/retry defaults mirror the OpenAI/Anthropic SDK defaults exactly
(connect 5s, read/write/pool 600s, max_retries 2) so existing deployments are
unaffected. Local-LLM stacks with large contexts should raise READ_TIMEOUT
explicitly. Lower MAX_RETRIES to 0 if SDK retries mask failures or double-bill
cloud providers — the agent-level ``asyncio.wait_for`` around ``provider.chat()``
remains the primary timeout guard regardless.
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
_VALID_FIELDS = {
    "TYPE",
    "BASE_URL",
    "API_KEY",
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "WRITE_TIMEOUT",
    "POOL_TIMEOUT",
    "MAX_RETRIES",
}

# Defaults mirror the OpenAI/Anthropic SDK defaults exactly so existing
# deployments keep their prior behaviour. Local-LLM stacks with large contexts
# should raise READ_TIMEOUT explicitly; see the module docstring.
_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_TIMEOUT = 600.0
_DEFAULT_WRITE_TIMEOUT = 600.0
_DEFAULT_POOL_TIMEOUT = 600.0
_DEFAULT_MAX_RETRIES = 2


def _parse_float(value: str | None, default: float) -> float:
    """Parse an env value to float, falling back to ``default`` on any error."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("Non-numeric timeout value %r, using default %s", value, default)
        return default
    return parsed if parsed > 0 else default


def _parse_int(value: str | None, default: int) -> int:
    """Parse an env value to int, falling back to ``default`` on any error."""
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        logger.warning("Non-numeric retry value %r, using default %s", value, default)
        return default
    return parsed if parsed >= 0 else default


class ProviderConnection(BaseModel):
    """Connection details for a single LLM provider (no model)."""

    type: str = "openai"
    api_key: str | None = None
    base_url: str | None = None
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = _DEFAULT_READ_TIMEOUT
    write_timeout: float = _DEFAULT_WRITE_TIMEOUT
    pool_timeout: float = _DEFAULT_POOL_TIMEOUT
    max_retries: int = _DEFAULT_MAX_RETRIES


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
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = _DEFAULT_READ_TIMEOUT
    write_timeout: float = _DEFAULT_WRITE_TIMEOUT
    pool_timeout: float = _DEFAULT_POOL_TIMEOUT
    max_retries: int = _DEFAULT_MAX_RETRIES


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
                connect_timeout=_parse_float(
                    fields.get("connect_timeout"), _DEFAULT_CONNECT_TIMEOUT
                ),
                read_timeout=_parse_float(fields.get("read_timeout"), _DEFAULT_READ_TIMEOUT),
                write_timeout=_parse_float(fields.get("write_timeout"), _DEFAULT_WRITE_TIMEOUT),
                pool_timeout=_parse_float(fields.get("pool_timeout"), _DEFAULT_POOL_TIMEOUT),
                max_retries=_parse_int(fields.get("max_retries"), _DEFAULT_MAX_RETRIES),
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
