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
from typing import TYPE_CHECKING, Any, Literal

from corpclaw_lite.config.providers import ProviderConnection, ProviderRegistry, ProviderSettings
from corpclaw_lite.config.settings import LLMSettings
from corpclaw_lite.llm.base import (
    BackendRequestOptions,
    LLMResponse,
    LLMStreamEvent,
    Provider,
    StreamChunk,
    StreamingProvider,
    ThinkingOverride,
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


def _derive_override_sampling(
    *,
    base: SamplingProfile | None,
    model_name: str | None,
    thinking: ThinkingOverride | None,
    inference: dict[str, Any] | None,
) -> SamplingProfile:
    """Build an ad-hoc SamplingProfile for with_overrides.

    Starts from the route's existing profile (``base``) if available, then
    applies the per-call thinking/inference overrides. ``thinking.mode ==
    "default"`` is treated as no thinking override.
    """
    # Carry over base fields, then override.
    model_ref = model_name or (base.model if base else None)
    thinking_mode = base.thinking_mode if base else "default"
    thinking_budget = base.thinking_budget if base else None
    inference_overrides = dict(base.inference_overrides) if base else {}

    if thinking is not None and thinking.mode != "default":
        thinking_mode = thinking.mode
        thinking_budget = thinking.budget if thinking.mode == "budget" else None

    if inference:
        inference_overrides.update(inference)

    return SamplingProfile(
        model=model_ref,
        thinking_mode=thinking_mode,
        thinking_budget=thinking_budget,
        inference_overrides=inference_overrides,
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
            sampling_name: str | None,
            model_profile_name: str | None,
            preset_name: str | None,
        ) -> Provider | None:
            # Sampling profile name wins over legacy preset name (D-056). When a
            # sampling profile is given, it also carries the model reference;
            # an explicit model_profile_name overrides the referenced ModelProfile.
            cache_key = (provider_name, model, sampling_name or preset_name)
            if cache_key in cache:
                return cache[cache_key]

            conn = provider_registry.get(provider_name)
            if conn is None:
                return None

            model_profile: ModelProfile | None = None
            sampling_profile: SamplingProfile | None = None
            legacy_preset: ModelPreset | None = None

            if sampling_name and preset_registry:
                # New-style sampling reference (preferred).
                sampling_profile = preset_registry.get_sampling_profile(sampling_name)
                if sampling_profile is None:
                    logger.warning("Unknown sampling profile '%s', ignoring", sampling_name)
                # Resolve the ModelProfile: explicit override > sampling's
                # referenced model > same-name model profile (back-compat).
                ref_model = model_profile_name or (
                    sampling_profile.model if sampling_profile else None
                )
                if ref_model and preset_registry:
                    model_profile = preset_registry.get_model_profile(ref_model)
            elif preset_name and preset_registry:
                # Legacy combined preset — resolve via the back-compat bridge:
                # the name maps to both a model profile and a sampling profile
                # (sharing the preset name), plus the legacy ModelPreset object.
                model_profile = preset_registry.get_model_profile(preset_name)
                sampling_profile = preset_registry.get_sampling_profile(preset_name)
                legacy_preset = preset_registry.get(preset_name)
                if model_profile is None and sampling_profile is None and legacy_preset is None:
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
                provider_meta[id(provider)] = (provider_name, model, sampling_name or preset_name)
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

            provider = _get_or_create(
                rule.provider,
                rule.model,
                sampling_name=rule.sampling,
                model_profile_name=rule.model_profile,
                preset_name=rule.preset,
            )
            if provider is None:
                logger.warning(
                    "Routing rule '%s': failed to build provider '%s' with model '%s', skipping",
                    rule_label,
                    rule.provider,
                    rule.model,
                )
                continue

            profile_tag = (
                f" sampling={rule.sampling}"
                if rule.sampling
                else f" preset={rule.preset}"
                if rule.preset
                else ""
            )
            logger.info(
                "  [route] %s → provider=%s model=%s%s",
                rule_label,
                rule.provider,
                rule.model,
                profile_tag,
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

    # ── Programmatic override (D-056 PR3) ─────────────────────────────────────

    # Task kinds that count as "agent-facing" for with_overrides: the main
    # agent loop plus the auxiliary LLM calls it drives (vision, compress,
    # consolidate) and all subagent routes. Non-matching routes (e.g. a
    # cloud-only "eval" judge route) are left untouched.
    _AGENT_TASK_KINDS = frozenset({"default", "vision", "compress", "consolidate"})

    def with_overrides(
        self,
        *,
        provider_registry: ProviderRegistry,
        preset_registry: PresetRegistry | None = None,
        model: str | None = None,
        thinking: ThinkingOverride | None = None,
        sampling_name: str | None = None,
        inference: dict[str, Any] | None = None,
        apply_to: Literal["all_agent_routes", "default_only"] = "all_agent_routes",
    ) -> LLMRouter:
        """Return a new router with overridden sampling/thinking/model on routes.

        Rebuilds providers for the selected routes in-memory from
        ``provider_registry`` + an override SamplingProfile — no YAML mutation,
        no file rewrite. ``queue`` and ``cache_manager`` are shared with this
        router (same semaphore, same cache leases).

        Override resolution (first non-None wins for the SamplingProfile):
          - ``sampling_name``: a named SamplingProfile from ``preset_registry``.
          - ``thinking`` / ``inference``: an ad-hoc SamplingProfile derived from
            each route's existing profile (or a fresh one) with these fields
            overridden. ``thinking.mode == "default"`` is treated as no override.
          - neither: the route keeps its existing profile (only ``model`` may change).

        ``model`` (optional) swaps the model across overridden routes; the
        ModelProfile is looked up by the new model name in ``preset_registry``
        (when available), else carried over from the existing route.

        ``apply_to``:
          - ``"all_agent_routes"`` (default): override every route whose
            ``task_kind`` is in {default, vision, compress, consolidate} OR has
            a ``subagent_id``. Non-agent routes (e.g. an "eval" judge route) are
            preserved unchanged.
          - ``"default_only"``: override only the default route.

        Routes whose provider connection cannot be rebuilt (provider not in the
        registry, or build_provider returns None) fall back to the original
        provider — override is skipped for that route, not an error.
        """
        override_label_parts: list[str] = []
        if model:
            override_label_parts.append(f"model={model}")
        if sampling_name:
            override_label_parts.append(f"sampling={sampling_name}")
        if thinking and thinking.mode != "default":
            override_label_parts.append(f"thinking={thinking.mode}")
            if thinking.budget is not None:
                override_label_parts[-1] += f":{thinking.budget}"
        if inference:
            override_label_parts.append("inference=custom")
        override_tag = ",".join(override_label_parts) or "override"

        new_routing: list[tuple[str | None, str | None, Provider, str]] = []
        new_providers: dict[str, Provider] = {}
        new_meta: dict[int, ProviderMeta] = {}
        new_default: Provider | None = None
        new_default_name: str | None = None

        def _should_override(task_kind: str | None, subagent_id: str | None) -> bool:
            if apply_to == "default_only":
                return task_kind == "default"
            # all_agent_routes: agent task_kinds + all subagent routes.
            return task_kind in self._AGENT_TASK_KINDS or subagent_id is not None

        for task_kind, subagent_id, provider, provider_name in self._routing:
            if not _should_override(task_kind, subagent_id):
                # Preserve the route as-is (copy its meta entry too).
                new_routing.append((task_kind, subagent_id, provider, provider_name))
                new_providers[f"{provider_name}:{getattr(provider, '_model', '?')}"] = provider
                meta = self._provider_meta.get(id(provider))
                if meta is not None:
                    new_meta[id(provider)] = meta
                if task_kind == "default" and new_default is None:
                    new_default = provider
                    new_default_name = provider_name
                continue

            # Look up the connection by name.
            conn = provider_registry.get(provider_name)
            if conn is None:
                logger.warning(
                    "with_overrides: provider '%s' not in registry; keeping original for route %s",
                    provider_name,
                    task_kind or f"subagent:{subagent_id}",
                )
                new_routing.append((task_kind, subagent_id, provider, provider_name))
                meta = self._provider_meta.get(id(provider))
                if meta is not None:
                    new_meta[id(provider)] = meta
                if task_kind == "default" and new_default is None:
                    new_default = provider
                    new_default_name = provider_name
                continue

            # Resolve the effective model + profiles.
            parent_meta = self._provider_meta.get(id(provider))
            parent_model = (
                parent_meta[1] if parent_meta else str(getattr(provider, "_model", "unknown"))
            )
            effective_model = model or parent_model

            # SamplingProfile: sampling_name > thinking/inference-on-existing > none.
            override_sampling: SamplingProfile | None = None
            if sampling_name and preset_registry is not None:
                override_sampling = preset_registry.get_sampling_profile(sampling_name)
                if override_sampling is None:
                    logger.warning(
                        "with_overrides: sampling profile '%s' not found; "
                        "falling back to existing profile",
                        sampling_name,
                    )

            if override_sampling is None and (thinking or inference):
                # Derive from the route's existing sampling profile if available.
                base_profile: SamplingProfile | None = None
                if parent_meta and parent_meta[2] and preset_registry is not None:
                    base_profile = preset_registry.get_sampling_profile(parent_meta[2])
                override_sampling = _derive_override_sampling(
                    base=base_profile,
                    model_name=effective_model,
                    thinking=thinking,
                    inference=inference,
                )

            # ModelProfile: lookup by effective model, else carry over.
            model_profile: ModelProfile | None = None
            if preset_registry is not None:
                model_profile = preset_registry.get_model_profile(effective_model)
            if (
                model_profile is None
                and parent_meta
                and parent_meta[2]
                and preset_registry is not None
            ):
                # Carry over the parent's model profile by name.
                parent_mp_name = parent_meta[2]
                model_profile = preset_registry.get_model_profile(parent_mp_name)

            built = build_provider(
                conn,
                model=effective_model,
                model_profile=model_profile,
                sampling=override_sampling,
            )
            if built is None:
                logger.warning(
                    "with_overrides: build_provider failed for provider '%s' model "
                    "'%s'; keeping original for route %s",
                    provider_name,
                    effective_model,
                    task_kind or f"subagent:{subagent_id}",
                )
                new_routing.append((task_kind, subagent_id, provider, provider_name))
                meta = self._provider_meta.get(id(provider))
                if meta is not None:
                    new_meta[id(provider)] = meta
                if task_kind == "default" and new_default is None:
                    new_default = provider
                    new_default_name = provider_name
                continue

            # Build a distinct profile label so cache scopes don't collide.
            parent_profile = parent_meta[2] if parent_meta else None
            profile_label = sampling_name or (
                f"{parent_profile}+{override_tag}" if parent_profile else override_tag
            )
            new_routing.append((task_kind, subagent_id, built, provider_name))
            new_providers[f"{provider_name}:{effective_model}"] = built
            new_meta[id(built)] = (provider_name, effective_model, profile_label)
            if task_kind == "default" and new_default is None:
                new_default = built
                new_default_name = provider_name

        if new_default is None:
            # No default route was overridden/seen — fall back to this router's default.
            new_default = self._default_provider
            new_default_name = self._default_provider_name
            meta = self._provider_meta.get(id(self._default_provider))
            if meta is not None:
                new_meta[id(self._default_provider)] = meta

        return LLMRouter(
            providers=new_providers,
            default_provider=new_default,
            default_provider_name=new_default_name,
            routing=new_routing,
            queue=self._queue,
            provider_meta=new_meta,
            cache_manager=self._cache_manager,
        )

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
