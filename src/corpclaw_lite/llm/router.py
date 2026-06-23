"""LLM Router — routes LLM requests to named providers based on task_kind or subagent_id.

Design:
    - Provider connections are registered via ``PROVIDER_*__*`` env vars (ProviderRegistry)
    - Routing rules in ``config/settings.yaml`` map tasks to provider + model + preset
    - Provider instances are cached per (provider_name, model, preset) combination
    - Falls back to the "default" task_kind rule if no specific rule matches
    - Implements both Provider and VisionProvider protocols, so the router can be
      used as a drop-in replacement everywhere a Provider is expected

Usage:
    registry = ProviderRegistry.from_env()
    router = LLMRouter.from_settings(settings.llm, registry, preset_registry)
    provider = router.for_task("vision")        # → vision-specific provider
    provider = router.for_subagent("exec-agent") # → subagent-specific provider
    response = await router.chat(messages)       # → uses default provider
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from corpclaw_lite.config.providers import ProviderConnection, ProviderRegistry, ProviderSettings
from corpclaw_lite.config.settings import LLMSettings
from corpclaw_lite.llm.base import (
    BackendRequestOptions,
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamChunk,
    StreamingProvider,
    VisionProvider,
    reset_backend_request_options,
    set_backend_request_options,
)
from corpclaw_lite.llm.cache import LLMCacheManager, config_from_settings
from corpclaw_lite.llm.presets import (
    ModelPreset,
    ModelProfile,
    PresetRegistry,
    SamplingProfile,
)
from corpclaw_lite.llm.queue import (
    LLMLoadClass,
    LLMQueueStatus,
    LLMRequestQueue,
    SlotAffinityConfig,
)
from corpclaw_lite.logging.trace import log_event

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = [
    "LLMRouter",
    "QueuedProvider",
    "build_provider",
]

logger = logging.getLogger(__name__)

ProviderMeta = tuple[str | None, str, str | None]
QueueStatusCallback = Callable[[LLMQueueStatus], None]


def _load_class_for_task(task_kind: str) -> LLMLoadClass:
    if task_kind == "vision":
        return "vision"
    if task_kind == "compress":
        return "compression"
    if task_kind == "consolidate":
        return "consolidation"
    if task_kind == "calibration":
        return "calibration"
    return "interactive"


def build_provider(
    conn: ProviderConnection,
    model: str,
    preset: ModelPreset | None = None,
    *,
    model_profile: ModelProfile | None = None,
    sampling: SamplingProfile | None = None,
) -> Provider | None:
    """Build a concrete Provider from a connection + model + (profiles | preset).

    Two equivalent input styles (D-056):

    - **New (preferred):** ``model_profile=`` + ``sampling=`` — the split
      ModelProfile/SamplingProfile pair.
    - **Legacy (back-compat):** ``preset=`` — a combined :class:`ModelPreset`.
      Internally split into a (ModelProfile, SamplingProfile) pair so the
      provider always sees the same internal shape.

    If both are given, ``model_profile``/``sampling`` win. Returns None if the
    provider cannot be built (e.g., missing required API key).
    """
    # Bridge legacy preset → profiles when the new-style args are absent.
    if model_profile is None and sampling is None and preset is not None:
        from corpclaw_lite.llm.presets import profile_from_legacy_preset

        model_profile, sampling = profile_from_legacy_preset(preset)

    if conn.type == "anthropic":
        if not conn.api_key:
            return None  # Anthropic requires a key; skip silently
        from corpclaw_lite.llm.anthropic import AnthropicProvider

        settings = ProviderSettings(
            type="anthropic", model=model, api_key=conn.api_key, base_url=conn.base_url
        )
        return AnthropicProvider(
            settings,
            preset=preset,
            model_profile=model_profile,
            sampling=sampling,
        )

    # Default: openai-compatible (Ollama, vLLM, LM Studio, OpenRouter, etc.)
    from corpclaw_lite.llm.openai import OpenAIProvider

    api_key = conn.api_key or "dummy"  # local models may not need a real key
    settings = ProviderSettings(type="openai", model=model, api_key=api_key, base_url=conn.base_url)
    return OpenAIProvider(
        settings,
        preset=preset,
        model_profile=model_profile,
        sampling=sampling,
    )


class LLMRouter:
    """Routes LLM calls to named providers based on task_kind or subagent_id.

    Implements both Provider and VisionProvider protocols so it can be used
    as a drop-in replacement anywhere a Provider is expected. Internally,
    ``chat()`` and ``stream()`` always use the default routing rule
    (``task_kind="default"``). To get a task-specific provider, call
    ``for_task()`` or ``for_subagent()``.

    When a :class:`~corpclaw_lite.llm.queue.LLMRequestQueue` is provided,
    all LLM calls go through the queue's semaphore, bounding concurrent
    inference requests to match GPU capacity.
    """

    def __init__(
        self,
        providers: dict[str, Provider],
        default_provider: Provider,
        default_provider_name: str | None,
        # (task_kind, subagent_id, provider, provider_name)
        routing: list[tuple[str | None, str | None, Provider, str]],
        queue: LLMRequestQueue | None = None,
        provider_meta: dict[int, ProviderMeta] | None = None,
        cache_manager: LLMCacheManager | None = None,
    ) -> None:
        self._providers = providers
        self._default_provider = default_provider
        self._default_provider_name = default_provider_name
        self._routing = routing
        self._queue = queue
        self._provider_meta = provider_meta or {}
        self._cache_manager = cache_manager
        logger.info(
            "LLMRouter ready: %d provider instances, %d routing rules, queue=%s",
            len(providers),
            len(routing),
            "enabled" if queue else "disabled",
        )

    @classmethod
    def from_settings(
        cls,
        llm: LLMSettings,
        provider_registry: ProviderRegistry,
        preset_registry: PresetRegistry | None = None,
    ) -> LLMRouter:
        """Build an LLMRouter from LLMSettings + ProviderRegistry + PresetRegistry."""
        # Cache: (provider_name, model, preset_name) → Provider instance
        cache: dict[tuple[str, str, str | None], Provider] = {}
        provider_meta: dict[int, ProviderMeta] = {}
        default_provider: Provider | None = None
        default_provider_name: str | None = None

        def _get_or_create(
            provider_name: str,
            model: str,
            preset_name: str | None,
        ) -> Provider | None:
            cache_key = (provider_name, model, preset_name)
            if cache_key in cache:
                return cache[cache_key]

            conn = provider_registry.get(provider_name)
            if conn is None:
                return None

            # Prefer the split-profile API (D-056): a name registered either as
            # a new-style model/sampling profile or as a legacy preset resolves
            # to the same name in both registries, so we look it up in both.
            model_profile: ModelProfile | None = None
            sampling_profile: SamplingProfile | None = None
            legacy_preset: ModelPreset | None = None
            if preset_name and preset_registry:
                model_profile = preset_registry.get_model_profile(preset_name)
                sampling_profile = preset_registry.get_sampling_profile(preset_name)
                legacy_preset = preset_registry.get(preset_name)
                if model_profile is None and sampling_profile is None and legacy_preset is None:
                    # Truly unknown name — no model profile, no sampling profile,
                    # no legacy combined preset. Fall back to provider defaults.
                    logger.warning("Unknown preset '%s', ignoring", preset_name)

            provider = build_provider(
                conn,
                model=model,
                # Pass the legacy preset alongside so provider._preset stays
                # populated for back-compat introspection when the name was a
                # legacy combined preset. New-style profiles take precedence
                # inside build_provider when both are non-None.
                preset=legacy_preset,
                model_profile=model_profile,
                sampling=sampling_profile,
            )
            if provider is not None:
                cache[cache_key] = provider
                provider_meta[id(provider)] = (provider_name, model, preset_name)
            return provider

        # Process routing rules
        routing: list[tuple[str | None, str | None, Provider, str]] = []

        for rule in llm.routing:
            rule_label = rule.task_kind or rule.subagent_id or "(unnamed)"

            # Validate provider exists
            if not provider_registry.get(rule.provider):
                logger.warning(
                    "Routing rule '%s': provider '%s' not found in registry, skipping. "
                    "Available: %s",
                    rule_label,
                    rule.provider,
                    provider_registry.list_all(),
                )
                continue

            # Validate model is specified
            if not rule.model:
                logger.warning("Routing rule '%s': no model specified, skipping", rule_label)
                continue

            provider = _get_or_create(rule.provider, rule.model, rule.preset)
            if provider is None:
                logger.warning(
                    "Routing rule '%s': failed to build provider '%s' with model '%s', skipping",
                    rule_label,
                    rule.provider,
                    rule.model,
                )
                continue

            logger.info(
                "  [route] %s → provider=%s model=%s%s",
                rule_label,
                rule.provider,
                rule.model,
                f" preset={rule.preset}" if rule.preset else "",
            )
            routing.append((rule.task_kind, rule.subagent_id, provider, rule.provider))

            # Track default provider
            if rule.task_kind == "default" and default_provider is None:
                default_provider = provider
                default_provider_name = rule.provider

        if default_provider is None:
            # Try first routing rule as fallback
            if routing:
                default_provider = routing[0][2]
                default_provider_name = routing[0][3]
                logger.warning(
                    "No routing rule with task_kind='default' found. "
                    "Using first rule's provider as default."
                )
            else:
                raise RuntimeError(
                    "No valid routing rules found. Ensure settings.yaml has at least one "
                    "routing rule with task_kind='default' and a valid provider + model."
                )

        # Build providers dict for lookup by cache key
        providers: dict[str, Provider] = {}
        for key, provider in cache.items():
            providers[f"{key[0]}:{key[1]}"] = provider

        # Build request queue if enabled
        queue: LLMRequestQueue | None = None
        cache_manager: LLMCacheManager | None = None
        if llm.queue.enabled:
            from corpclaw_lite.llm.queue import LLMRequestQueue

            slot_cfg = llm.queue.slot_affinity
            queue = LLMRequestQueue(
                max_concurrent=llm.max_concurrent_requests,
                strategy=llm.queue.strategy,
                slot_affinity=SlotAffinityConfig(
                    enabled=slot_cfg.enabled,
                    provider_names=tuple(slot_cfg.provider_names),
                    sticky_slot_ids=tuple(slot_cfg.sticky_slot_ids),
                    overflow_slot_ids=tuple(slot_cfg.overflow_slot_ids),
                    idle_ttl_seconds=slot_cfg.idle_ttl_seconds,
                    cache_prompt=slot_cfg.cache_prompt,
                    auxiliary_policy=slot_cfg.auxiliary_policy,
                ),
            )
            persistent_cfg = llm.queue.persistent_cache
            if persistent_cfg.enabled:
                provider_base_urls: dict[str, str] = {}
                provider_api_keys: dict[str, str | None] = {}
                for provider_name in provider_registry.list_all():
                    conn = provider_registry.get(provider_name)
                    if conn is None:
                        continue
                    if conn.base_url:
                        provider_base_urls[provider_name] = conn.base_url
                    provider_api_keys[provider_name] = conn.api_key
                cache_manager = LLMCacheManager(
                    config_from_settings(persistent_cfg),
                    provider_base_urls=provider_base_urls,
                    provider_api_keys=provider_api_keys,
                )

        return cls(
            providers,
            default_provider,
            default_provider_name,
            routing,
            queue=queue,
            provider_meta=provider_meta,
            cache_manager=cache_manager,
        )

    def _wrap_provider(
        self,
        provider: Provider,
        *,
        provider_name: str | None,
        user_id: str,
        task_kind: str,
        load_class: LLMLoadClass,
        run_id: str | None = None,
        agent_id: str = "main",
        on_queue_status: QueueStatusCallback | None = None,
        notify_position: bool = True,
        notify_interval_seconds: float = 30.0,
    ) -> Provider:
        if self._queue is None:
            return provider
        _meta_provider_name, model, preset_name = self._details_for_provider(
            provider,
            provider_name=provider_name,
        )
        return QueuedProvider(
            provider,
            self._queue,
            cache_manager=self._cache_manager,
            user_id=user_id,
            task_kind=task_kind,
            load_class=load_class,
            run_id=run_id,
            provider_name=provider_name,
            model=model,
            preset_name=preset_name,
            agent_id=agent_id,
            on_queue_status=on_queue_status,
            notify_position=notify_position,
            notify_interval_seconds=notify_interval_seconds,
        )

    def _details_for_provider(
        self,
        provider: Provider,
        *,
        provider_name: str | None,
    ) -> ProviderMeta:
        meta = self._provider_meta.get(id(provider))
        if meta is not None:
            return meta
        model = str(getattr(provider, "_model", "unknown"))
        return provider_name, model, None

    def has_task_route(self, task_kind: str) -> bool:
        """Return True if a task-specific routing rule exists."""
        return any(
            rule_task == task_kind
            for rule_task, _rule_subagent, _provider, _provider_name in self._routing
        )

    def for_task(
        self,
        task_kind: str,
        *,
        user_id: str = "",
        load_class: LLMLoadClass | None = None,
        run_id: str | None = None,
    ) -> Provider:
        """Return the provider configured for a given task_kind.

        Falls back to the default provider if no rule matches.
        """
        for rule_task, _rule_subagent, provider, provider_name in self._routing:
            if rule_task == task_kind:
                return self._wrap_provider(
                    provider,
                    provider_name=provider_name,
                    user_id=user_id,
                    task_kind=task_kind,
                    load_class=load_class or _load_class_for_task(task_kind),
                    run_id=run_id,
                    agent_id="main" if task_kind == "default" else task_kind,
                )
        return self._wrap_provider(
            self._default_provider,
            provider_name=self._default_provider_name,
            user_id=user_id,
            task_kind=task_kind,
            load_class=load_class or _load_class_for_task(task_kind),
            run_id=run_id,
            agent_id="main" if task_kind == "default" else task_kind,
        )

    def for_subagent(
        self,
        subagent_id: str,
        *,
        user_id: str = "",
        run_id: str | None = None,
        on_queue_status: QueueStatusCallback | None = None,
        notify_position: bool = True,
        notify_interval_seconds: float = 30.0,
    ) -> Provider:
        """Return the provider configured for a given subagent_id.

        Falls back to the default provider if no rule matches.
        """
        for _rule_task, rule_subagent, provider, provider_name in self._routing:
            if rule_subagent == subagent_id:
                return self._wrap_provider(
                    provider,
                    provider_name=provider_name,
                    user_id=user_id,
                    task_kind=f"subagent:{subagent_id}",
                    load_class="subagent",
                    run_id=run_id,
                    agent_id=subagent_id,
                    on_queue_status=on_queue_status,
                    notify_position=notify_position,
                    notify_interval_seconds=notify_interval_seconds,
                )
        return self._wrap_provider(
            self._default_provider,
            provider_name=self._default_provider_name,
            user_id=user_id,
            task_kind=f"subagent:{subagent_id}",
            load_class="subagent",
            run_id=run_id,
            agent_id=subagent_id,
            on_queue_status=on_queue_status,
            notify_position=notify_position,
            notify_interval_seconds=notify_interval_seconds,
        )

    @property
    def default(self) -> Provider:
        """Return the default provider."""
        return self._default_provider

    @property
    def has_queue(self) -> bool:
        """Return True if a request queue is configured."""
        return self._queue is not None

    @property
    def queue(self) -> LLMRequestQueue | None:
        """Return the request queue, or ``None`` if queuing is disabled."""
        return self._queue

    async def mark_user_cache_reset(self, user_id: str) -> None:
        """Invalidate persistent cache state for a user after conversation reset."""
        if self._cache_manager is None or not self._cache_manager.enabled:
            return
        await self._cache_manager.mark_user_reset(user_id)

    async def call_default_with_slot(
        self,
        *,
        user_id: str,
        run_id: str | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
        on_acquired: Callable[[], None] | None,
        call: Callable[[Provider], Awaitable[LLMResponse]],
        on_queue_status: QueueStatusCallback | None = None,
        notify_position: bool = True,
        notify_interval_seconds: float = 30.0,
    ) -> LLMResponse:
        """Call the default provider through queue/cache while preserving budget control."""
        if self._queue is None:
            if on_acquired is not None:
                on_acquired()
            return await call(self._default_provider)
        provider_name, model, preset_name = self._details_for_provider(
            self._default_provider,
            provider_name=self._default_provider_name,
        )
        return await _execute_with_queue(
            provider=self._default_provider,
            queue=self._queue,
            cache_manager=self._cache_manager,
            provider_name=provider_name,
            model=model,
            preset_name=preset_name,
            user_id=user_id,
            task_kind="default",
            load_class="interactive",
            run_id=run_id,
            agent_id="main",
            messages=messages,
            tools=tools,
            system=system,
            on_acquired=on_acquired,
            on_queue_status=on_queue_status,
            notify_position=notify_position,
            notify_interval_seconds=notify_interval_seconds,
            call=call,
        )

    @asynccontextmanager
    async def acquire_slot(
        self,
        user_id: str = "",
        *,
        task_kind: str = "default",
        load_class: LLMLoadClass = "interactive",
        run_id: str | None = None,
        provider_name: str | None = None,
    ) -> AsyncGenerator[None, None]:
        """Acquire an LLM inference slot via the request queue.

        Use as an async context manager around the raw provider call so the
        budget guard can pause while waiting for a slot::

            budget.pause()
            async with router.acquire_slot(user_id):
                budget.resume()
                response = await router.default.chat(...)
        """
        import time

        if self._queue is None:
            yield
            return
        entry = await self._queue.acquire(
            user_id,
            task_kind=task_kind,
            load_class=load_class,
            run_id=run_id,
            provider_name=provider_name or self._default_provider_name,
        )
        t0 = time.monotonic()
        token = set_backend_request_options(
            BackendRequestOptions(extra_body=entry.backend_extra_body)
            if entry.backend_extra_body
            else None
        )
        try:
            yield
        finally:
            reset_backend_request_options(token)
            elapsed = time.monotonic() - t0
            await self._queue.release(entry, elapsed)

    # ── Provider protocol implementation (delegates to default via queue) ────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        """Chat using the default provider (through the queue if enabled)."""
        if self._queue is not None:
            return await self.call_default_with_slot(
                user_id="_router_chat",
                run_id=None,
                messages=messages,
                tools=tools,
                system=system,
                on_acquired=None,
                call=lambda provider: provider.chat(messages=messages, tools=tools, system=system),
            )
        return await self.default.chat(messages=messages, tools=tools, system=system)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream using the default provider (through the queue if enabled)."""
        if self._queue is not None:
            async with self.acquire_slot(
                "_router_stream",
                task_kind="default",
                provider_name=self._default_provider_name,
            ):
                async for chunk in self.default.stream(
                    messages=messages, tools=tools, system=system
                ):
                    yield chunk
        else:
            async for chunk in self.default.stream(messages=messages, tools=tools, system=system):
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
        vision_provider = self.for_task("vision", load_class="vision")
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


async def _execute_with_queue(
    *,
    provider: Provider,
    queue: LLMRequestQueue,
    cache_manager: LLMCacheManager | None,
    provider_name: str | None,
    model: str,
    preset_name: str | None,
    user_id: str,
    task_kind: str,
    load_class: LLMLoadClass,
    run_id: str | None,
    agent_id: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    system: str | None,
    on_acquired: Callable[[], None] | None,
    on_queue_status: QueueStatusCallback | None,
    notify_position: bool,
    notify_interval_seconds: float,
    call: Callable[[Provider], Awaitable[LLMResponse]],
) -> LLMResponse:
    entry = await queue.acquire(
        user_id,
        task_kind=task_kind,
        load_class=load_class,
        run_id=run_id,
        provider_name=provider_name,
        on_status=on_queue_status,
        notify_position=notify_position,
        notify_interval_seconds=notify_interval_seconds,
    )
    t0 = time.monotonic()
    if on_acquired is not None:
        on_acquired()
    lease = None
    scope = None
    try:
        extra_body = dict(entry.backend_extra_body)
        if cache_manager is not None and cache_manager.enabled:
            scope = cache_manager.build_scope(
                user_id=user_id,
                conversation_id="default",
                agent_id=agent_id,
                provider_name=provider_name or "",
                model=model,
                preset=preset_name,
                system=system,
                tools=tools,
            )
            lease = await cache_manager.prepare(entry, scope)
            if lease.enabled:
                extra_body["timings_per_token"] = True
        token = set_backend_request_options(
            BackendRequestOptions(extra_body=extra_body) if extra_body else None
        )
        try:
            response = await call(provider)
        except Exception:
            if cache_manager is not None and lease is not None:
                await cache_manager.abort(lease)
            raise
        finally:
            reset_backend_request_options(token)
        if cache_manager is None or lease is None:
            return response
        result = await cache_manager.finalize(entry, lease, response)
        if not result.retry_without_cache or scope is None:
            return response

        retry_lease = await cache_manager.prepare_uncached_retry(entry, scope)
        retry_extra_body = dict(entry.backend_extra_body)
        retry_extra_body["cache_prompt"] = True
        retry_extra_body["timings_per_token"] = True
        retry_token = set_backend_request_options(
            BackendRequestOptions(extra_body=retry_extra_body)
        )
        try:
            retry_response = await call(provider)
        finally:
            reset_backend_request_options(retry_token)
        await cache_manager.finalize(entry, retry_lease, retry_response, allow_retry=False)
        log_event(
            "llm_cache_mismatch_fallback_finished",
            run_id or "unknown",
            user_id=user_id,
            agent_id=agent_id,
            slot_id=entry.slot_id,
            scope_key=scope.key,
            mismatch_reason=result.mismatch_reason,
        )
        return retry_response
    finally:
        await queue.release(entry, time.monotonic() - t0)


class QueuedProvider:
    """Provider wrapper that routes every LLM call through ``LLMRequestQueue``."""

    def __init__(
        self,
        provider: Provider,
        queue: LLMRequestQueue,
        *,
        cache_manager: LLMCacheManager | None,
        provider_name: str | None,
        model: str,
        preset_name: str | None,
        user_id: str,
        task_kind: str,
        load_class: LLMLoadClass,
        agent_id: str,
        run_id: str | None = None,
        on_queue_status: QueueStatusCallback | None = None,
        notify_position: bool = True,
        notify_interval_seconds: float = 30.0,
    ) -> None:
        self._provider = provider
        self._queue = queue
        self._cache_manager = cache_manager
        self._provider_name = provider_name
        self._model = model
        self._preset_name = preset_name
        self._user_id = user_id or f"_{task_kind}"
        self._task_kind = task_kind
        self._load_class: LLMLoadClass = load_class
        self._agent_id = agent_id
        self._run_id = run_id
        self._on_queue_status = on_queue_status
        self._notify_position = notify_position
        self._notify_interval_seconds = notify_interval_seconds

    async def call_with_slot(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
        on_acquired: Callable[[], None] | None,
        on_queue_status: QueueStatusCallback | None,
        notify_position: bool,
        notify_interval_seconds: float,
        call: Callable[[Provider], Awaitable[LLMResponse]],
    ) -> LLMResponse:
        """Call the wrapped provider through this queued provider's slot."""
        return await _execute_with_queue(
            provider=self._provider,
            queue=self._queue,
            cache_manager=self._cache_manager,
            provider_name=self._provider_name,
            model=self._model,
            preset_name=self._preset_name,
            user_id=self._user_id,
            task_kind=self._task_kind,
            load_class=self._load_class,
            run_id=self._run_id,
            agent_id=self._agent_id,
            messages=messages,
            tools=tools,
            system=system,
            on_acquired=on_acquired,
            on_queue_status=on_queue_status,
            notify_position=notify_position,
            notify_interval_seconds=notify_interval_seconds,
            call=call,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMResponse:
        return await _execute_with_queue(
            provider=self._provider,
            queue=self._queue,
            cache_manager=self._cache_manager,
            provider_name=self._provider_name,
            model=self._model,
            preset_name=self._preset_name,
            user_id=self._user_id,
            task_kind=self._task_kind,
            load_class=self._load_class,
            run_id=self._run_id,
            agent_id=self._agent_id,
            messages=messages,
            tools=tools,
            system=system,
            on_acquired=None,
            on_queue_status=self._on_queue_status,
            notify_position=self._notify_position,
            notify_interval_seconds=self._notify_interval_seconds,
            call=lambda provider: provider.chat(messages=messages, tools=tools, system=system),
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        entry = await self._queue.acquire(
            self._user_id,
            task_kind=self._task_kind,
            load_class=self._load_class,
            run_id=self._run_id,
            provider_name=self._provider_name,
            on_status=self._on_queue_status,
            notify_position=self._notify_position,
            notify_interval_seconds=self._notify_interval_seconds,
        )
        t0 = time.monotonic()
        token = set_backend_request_options(
            BackendRequestOptions(extra_body=entry.backend_extra_body)
            if entry.backend_extra_body
            else None
        )
        try:
            async for chunk in self._provider.stream(messages=messages, tools=tools, system=system):
                yield chunk
        finally:
            reset_backend_request_options(token)
            await self._queue.release(entry, time.monotonic() - t0)

    async def chat_streamed(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        on_event: Callable[[LLMStreamEvent], None] | None = None,
    ) -> LLMResponse:
        async def call(provider: Provider) -> LLMResponse:
            if isinstance(provider, StreamingProvider):
                return await provider.chat_streamed(
                    messages=messages,
                    tools=tools,
                    system=system,
                    on_event=on_event,
                )
            return await provider.chat(messages=messages, tools=tools, system=system)

        return await _execute_with_queue(
            provider=self._provider,
            queue=self._queue,
            cache_manager=self._cache_manager,
            provider_name=self._provider_name,
            model=self._model,
            preset_name=self._preset_name,
            user_id=self._user_id,
            task_kind=self._task_kind,
            load_class=self._load_class,
            run_id=self._run_id,
            agent_id=self._agent_id,
            messages=messages,
            tools=tools,
            system=system,
            on_acquired=None,
            on_queue_status=self._on_queue_status,
            notify_position=self._notify_position,
            notify_interval_seconds=self._notify_interval_seconds,
            call=call,
        )

    async def chat_with_image(
        self,
        image_data: str,
        image_media_type: str,
        prompt: str,
        system: str | None = None,
    ) -> LLMResponse:
        entry = await self._queue.acquire(
            self._user_id,
            task_kind=self._task_kind,
            load_class=self._load_class,
            run_id=self._run_id,
            provider_name=self._provider_name,
            on_status=self._on_queue_status,
            notify_position=self._notify_position,
            notify_interval_seconds=self._notify_interval_seconds,
        )
        t0 = time.monotonic()
        token = set_backend_request_options(
            BackendRequestOptions(extra_body=entry.backend_extra_body)
            if entry.backend_extra_body
            else None
        )
        try:
            if isinstance(self._provider, VisionProvider):
                return await self._provider.chat_with_image(
                    image_data=image_data,
                    image_media_type=image_media_type,
                    prompt=prompt,
                    system=system,
                )
            messages: list[dict[str, Any]] = [{"role": "user", "content": f"{prompt}"}]
            return await self._provider.chat(messages=messages, system=system)
        finally:
            reset_backend_request_options(token)
            await self._queue.release(entry, time.monotonic() - t0)
