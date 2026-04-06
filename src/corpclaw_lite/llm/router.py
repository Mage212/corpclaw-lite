"""
LLM Router — routes LLM requests to named providers based on task_kind or subagent_id.

Design:
    - Reads named providers from LLMSettings (populated from config/settings.yaml)
    - Routing rules: task_kind and/or subagent_id → provider name
    - Falls back to the 'default' provider if no rule matches
    - Implements both Provider and VisionProvider protocols, so the router can be
      used as a drop-in replacement everywhere a Provider is expected

Usage:
    router = LLMRouter.from_settings(settings.llm, providers)
    provider = router.for_task("vision")        # → vision-specific provider
    provider = router.for_subagent("exec-agent") # → subagent-specific provider
    response = await router.chat(messages)       # → uses default provider
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from corpclaw_lite.config.settings import LLMSettings, ProviderSettings
from corpclaw_lite.llm.base import LLMResponse, Provider, StreamChunk, VisionProvider
from corpclaw_lite.llm.presets import PresetRegistry

__all__ = [
    "LLMRouter",
    "build_provider",
]

logger = logging.getLogger(__name__)


def build_provider(
    settings: ProviderSettings,
    preset_registry: PresetRegistry | None = None,
) -> Provider | None:
    """Build a concrete Provider from a ProviderSettings spec.

    Returns None if the provider cannot be built (e.g., missing required API key).
    The caller should decide whether this is a fatal error or just skip the provider.
    """
    # Resolve preset by name
    preset = None
    if settings.preset and preset_registry:
        preset = preset_registry.get(settings.preset)
        if preset is None:
            logger.warning("Unknown preset '%s' for provider, ignoring", settings.preset)

    if settings.type == "anthropic":
        if not settings.api_key:
            return None  # Anthropic requires a key; skip silently
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        return AnthropicProvider(settings, preset=preset)

    # Default: openai-compatible (Ollama, vLLM, LM Studio, OpenRouter, etc.)
    from corpclaw_lite.llm.openai import OpenAIProvider

    # openai-compatible providers work without a real key (local models)
    if not settings.api_key:
        settings = ProviderSettings(**{**settings.model_dump(), "api_key": "dummy"})
    return OpenAIProvider(settings, preset=preset)


class LLMRouter:
    """Routes LLM calls to named providers based on task_kind or subagent_id.

    Implements both Provider and VisionProvider protocols so it can be used
    as a drop-in replacement anywhere a Provider is expected. Internally,
    `chat()` and `stream()` always use the default provider. To get a
    task-specific provider, call `for_task()` or `for_subagent()`.
    """

    def __init__(
        self,
        providers: dict[str, Provider],
        default_name: str,
        # (task_kind, subagent_id, provider_name)
        routing: list[tuple[str | None, str | None, str]],
    ) -> None:
        if default_name not in providers:
            raise ValueError(
                f"Default provider '{default_name}' not in providers: {list(providers)}"
            )
        self._providers = providers
        self._default_name = default_name
        self._routing = routing
        logger.info(
            "LLMRouter ready: providers=%s default=%s rules=%d",
            list(providers),
            default_name,
            len(routing),
        )

    @classmethod
    def from_settings(
        cls, llm: LLMSettings, preset_registry: PresetRegistry | None = None
    ) -> LLMRouter:
        """Build an LLMRouter from LLMSettings (populated from settings.yaml)."""
        providers: dict[str, Provider] = {}
        for name, spec in llm.named.items():
            built = build_provider(spec, preset_registry=preset_registry)
            if built is None:
                logger.warning(
                    "  [provider] %s skipped (type=%s, missing required credentials)",
                    name,
                    spec.type,
                )
                continue
            providers[name] = built
            preset_info = f" preset={spec.preset}" if spec.preset else ""
            logger.info(
                "  [provider] %s: type=%s model=%s%s", name, spec.type, spec.model, preset_info
            )

        # Build routing table: list of (task_kind, subagent_id, provider_name)
        routing: list[tuple[str | None, str | None, str]] = []
        for rule in llm.routing:
            provider_name = rule.provider
            if provider_name not in providers:
                logger.warning(
                    "Routing rule references unknown provider '%s', skipping", provider_name
                )
                continue
            routing.append((rule.task_kind, rule.subagent_id, provider_name))

        return cls(providers, llm.default, routing)

    def for_task(self, task_kind: str) -> Provider:
        """Return the provider configured for a given task_kind.

        Falls back to the default provider if no rule matches.
        """
        for rule_task, _rule_subagent, provider_name in self._routing:
            if rule_task == task_kind:
                return self._providers[provider_name]
        return self._providers[self._default_name]

    def for_subagent(self, subagent_id: str) -> Provider:
        """Return the provider configured for a given subagent_id.

        Falls back to the default provider if no rule matches.
        """
        for _rule_task, rule_subagent, provider_name in self._routing:
            if rule_subagent == subagent_id:
                return self._providers[provider_name]
        return self._providers[self._default_name]

    @property
    def default(self) -> Provider:
        """Return the default provider."""
        return self._providers[self._default_name]

    # ── Provider protocol implementation (delegates to default) ───────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Chat using the default provider."""
        return await self.default.chat(messages=messages, tools=tools, system=system)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream using the default provider."""
        async for chunk in await self.default.stream(messages=messages, tools=tools, system=system):  # type: ignore[misc]
            yield chunk

    # ── VisionProvider protocol implementation (delegates to vision task) ─────

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        """Image chat using the provider routed for 'vision' task_kind."""
        vision_provider = self.for_task("vision")
        if isinstance(vision_provider, VisionProvider):
            return await vision_provider.chat_with_image(
                image_data=image_data,
                image_media_type=image_media_type,
                prompt=prompt,
                system=system,
            )
        # Vision provider doesn't support images — fall back to text
        logger.warning(
            "Vision provider '%s' does not support images; falling back to text",
            type(vision_provider).__name__,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": f"{prompt}"}]
        return await vision_provider.chat(messages=messages, system=system)
