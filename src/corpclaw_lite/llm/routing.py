from __future__ import annotations

from corpclaw_lite.config.settings import LLMSettings, ProviderSettings


class ProviderRouter:
    """Routes LLM requests to specific providers based on task or context."""

    def __init__(self, settings: LLMSettings):
        self._settings = settings

    def get_provider_settings(
        self, task_kind: str | None = None, subagent_id: str | None = None
    ) -> ProviderSettings:
        """Get the provider settings based on routing rules."""
        # 1. Check rules
        for rule in self._settings.routing:
            if task_kind and rule.task_kind == task_kind:
                return self._get_named(rule.provider)
            if subagent_id and rule.subagent_id == subagent_id:
                return self._get_named(rule.provider)

        # 2. Fallback to default
        return self._get_named(self._settings.default)

    def _get_named(self, name: str) -> ProviderSettings:
        if name not in self._settings.named:
            raise ValueError(f"Provider '{name}' not found in named configurations.")
        return self._settings.named[name]
