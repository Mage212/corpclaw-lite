from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings

from corpclaw_lite.agent.guards import (
    PlanningTextGuardConfig,
    ResultDedupGuardConfig,
)

__all__ = [
    "AgentSettings",
    "CompressionSettings",
    "ContainerSettings",
    "ExtensionsSettings",
    "LLMSettings",
    "LoggingSettings",
    "PersistentCacheSettings",
    "PhasePolicySettings",
    "QueueSettings",
    "ResearchSettings",
    "RoutingRule",
    "Settings",
    "SkillsSettings",
    "SlotAffinitySettings",
    "TelegramSettings",
    "WebSettings",
    "WebChannelSettings",
]


class RoutingRule(BaseModel):
    """Rule for routing tasks to specific providers with model selection.

    Each rule specifies a provider (registered via ``PROVIDER_*__*`` env vars),
    a model from that provider, and profile selection for inference/thinking.

    Profile selection (D-056) — two equivalent styles:

    - **New (preferred):** ``sampling`` references a SamplingProfile in
      ``config/model_presets.yaml`` (``sampling:`` block). ``model_profile``
      optionally overrides the ModelProfile; if absent it is inferred from the
      SamplingProfile's ``model`` field or looked up by the rule's ``model``.
    - **Legacy (back-compat):** ``preset`` references a combined ModelPreset
      (the old ``presets:`` block). Internally split into a
      (ModelProfile, SamplingProfile) pair sharing the preset name.

    If both ``sampling`` and ``preset`` are set, ``sampling`` wins.
    """

    task_kind: str | None = None
    subagent_id: str | None = None
    provider: str = "default"
    model: str | None = None
    # D-056 split-profile references (preferred):
    model_profile: str | None = None
    sampling: str | None = None
    # DEPRECATED: legacy combined preset (back-compat → split internally).
    preset: str | None = None


class PhasePolicySettings(BaseModel):
    """Phase-based per-call thinking overrides (D-056 PR2).

    Configures :class:`~corpclaw_lite.agent.phase_policy.DefaultPhasePolicy`,
    which switches model thinking on/off/budget per-call based on the current
    task phase, via the per-call ``RequestOptions`` contextvar.

    Enabled by default (``enabled: true``): the policy is a no-op for the main
    agent in its default phase (no override returned), so it only takes effect
    in closing mode (budget pressure) and for workflow subagents (research).
    """

    enabled: bool = True
    # Semantic primary signal: tool names whose presence in the previous turn
    # marks the aggregation/finalization phase (about to write the final report).
    aggregation_markers: list[str] = ["research_list_facts"]
    # Semantic signal: tool names whose presence in the previous turn marks the
    # gathering phase (still collecting raw material).
    gathering_tools: list[str] = [
        "research_search",
        "research_fetch_source",
        "research_read_source",
        "research_list_sources",
        "research_store_fact",
    ]
    # Per-phase thinking overrides (Literal matches ThinkingOverride.mode).
    closing_thinking: Literal["default", "off", "budget"] = "off"
    gathering_thinking: Literal["default", "off", "budget"] = "off"
    aggregation_thinking: Literal["default", "off", "budget"] = "default"

    @field_validator(
        "closing_thinking", "gathering_thinking", "aggregation_thinking", mode="before"
    )
    @classmethod
    def _coerce_thinking_yaml_bool(cls, v: Any) -> Any:
        """Coerce YAML-bool forms (``off``→False, ``on``→True) to string literals.

        YAML 1.1 parses unquoted ``off``/``on``/``yes``/``no`` as booleans; an
        operator writing ``closing_thinking: off`` gets ``False`` and the
        Literal rejects it. Maps: ``False``/``off``→``"off"``,
        ``True``/``on``/``yes``/``None``→``"default"``.
        """
        if v is False or v == "off":
            return "off"
        if v is True or v in ("on", "yes") or v is None:
            return "default"
        return v


class SlotAffinitySettings(BaseModel):
    """Settings for llama.cpp-compatible slot affinity."""

    enabled: bool = False
    backend: Literal["llama_cpp"] = "llama_cpp"
    provider_names: list[str] = ["llamacpp"]
    sticky_slot_ids: list[int] = [0, 1, 2]
    overflow_slot_ids: list[int] = [3]
    idle_ttl_seconds: float = 120.0
    cache_prompt: bool = True
    auxiliary_policy: Literal["overflow_only"] = "overflow_only"


class PersistentCacheSettings(BaseModel):
    """Settings for llama.cpp persistent slot KV-cache files."""

    enabled: bool = False
    root_dir: str = "data/llm_cache/slot-cache"
    index_path: str = "data/llm_cache/index.sqlite"
    slot_api_base_url: str | None = None
    max_total_bytes: int = 100 * 1024 * 1024 * 1024
    max_age_days: int = 30
    save_policy: Literal["hybrid", "every_response", "eviction_only"] = "hybrid"
    save_min_tokens: int = 1024
    save_dirty_seconds: float = 60.0
    validation_min_reuse_ratio: float = 0.70
    validation_large_context_tokens: int = 16000
    validation_large_reuse_ratio: float = 0.90
    strict_mismatch_retry: bool = True
    prune_interval_seconds: float = 600.0
    http_timeout_seconds: float = 30.0


class QueueSettings(BaseModel):
    """Settings for the LLM request queue."""

    enabled: bool = True
    strategy: Literal["simple", "slot_affinity"] = "simple"
    notify_position: bool = True
    notify_interval_seconds: int = 30
    slot_affinity: SlotAffinitySettings = SlotAffinitySettings()
    persistent_cache: PersistentCacheSettings = PersistentCacheSettings()


class LLMSettings(BaseModel):
    """Settings for LLM provider routing.

    Providers are registered via ``PROVIDER_*__*`` env vars in ``.env``.
    This model only contains routing rules that map tasks to providers + models.
    """

    routing: list[RoutingRule] = []
    max_concurrent_requests: int = 4
    queue: QueueSettings = QueueSettings()


class ContainerSettings(BaseModel):
    """Settings for Docker container sandboxes."""

    # Set to false to disable container isolation (dev/test mode — runs on host)
    enabled: bool = True
    # Docker image used as the per-user sandbox
    image: str = "corpclaw-agent-base:latest"
    # Base directory for per-user workspaces (bind-mounted into /workspace)
    workspace_base: str = "workspaces"
    max_memory: str = "512m"
    cpus: float = 0.5
    idle_timeout_seconds: int = 600
    # Global cap on simultaneous container *creations* (Path B of ensure_running).
    # Idempotent "already-running" checks (Path A) bypass this, so the per-message
    # hot path is never serialized. Per-user 1-container invariant is guaranteed by
    # ContainerManager._get_lock, not by this setting.
    max_concurrent_containers: int = 20
    strict_capabilities: bool = False  # Set to True on Linux production for cap_drop ALL + seccomp
    # Timeout for the outer docker exec call (host-side IPC envelope)
    ipc_timeout_seconds: float = 120.0


class CompressionSettings(BaseModel):
    """Settings for context compression (Hermes pattern)."""

    enabled: bool = True
    max_context_tokens: int = 8000
    threshold_ratio: float = 0.5
    protect_tail_tokens: int = 3000
    summary_ratio: float = 0.20
    prune_min_messages: int = 10


class AgentSettings(BaseModel):
    """Settings for the AgentLoop."""

    max_steps: int = 15
    max_tool_calls: int = 30
    max_wall_time_ms: int = 300000
    soft_deadline_ratio: float = 0.85
    max_history: int = 20
    consolidation_threshold: int = 30
    consolidation_enabled: bool = True
    approval_mode: Literal["manual", "smart", "off"] = "manual"
    compression: CompressionSettings = CompressionSettings()
    llm_timeout_seconds: int = 120
    llm_streaming_enabled: bool = True
    llm_stream_stall_seconds: float = 20.0
    llm_stream_max_reasoning_chars: int = 12000
    llm_stream_status_updates: bool = True
    max_facts_recall: int = 20
    vision_max_image_bytes: int = 10 * 1024 * 1024
    # B-055/B-056: Phase 0 guard configuration. Exposed on AgentSettings so the
    # eval harness (B-060) can run A/B passes with guards enabled/disabled, and
    # operators can tune thresholds without code changes. Defaults preserve the
    # pre-B-060 behaviour (guards on with original thresholds).
    result_dedup_guard: ResultDedupGuardConfig = ResultDedupGuardConfig()
    planning_text_guard: PlanningTextGuardConfig = PlanningTextGuardConfig()
    # D-056 PR2: per-call thinking overrides based on task phase
    # (closing mode / research gathering / research aggregation). The policy is
    # a no-op for the main agent in its default phase, so enabling it by default
    # does not change main-agent behaviour unless the budget runs out.
    phase_policy: PhasePolicySettings = PhasePolicySettings()


class WebSettings(BaseModel):
    """Settings for host-side web tools."""

    search_backend: Literal["auto", "duckduckgo"] = "auto"
    search_retry_attempts: int = 3
    search_retry_backoff_seconds: float = 1.5
    search_max_concurrent: int = 1
    fetch_max_concurrent: int = 4
    timeout_seconds: int = 20
    user_agent: str = "CorpClawLite/0.1 web tools"


class WebChannelSettings(BaseModel):
    """Settings for the browser-based user channel."""

    host: str = "127.0.0.1"
    port: int = 8090
    workspace_base: Path = Path("workspaces")
    upload_max_bytes: int = 20 * 1024 * 1024
    rate_limit_per_minute: int = 10
    login_rate_limit_per_minute: int = 5
    login_lockout_threshold: int = 5
    login_lockout_seconds: int = 300
    password_min_length: int = 12
    password_max_length: int = 256
    session_ttl_hours: int = 12
    cookie_name: str = "corpclaw_lite_session"
    cookie_secure: Literal["auto"] | bool = "auto"
    ws_ticket_ttl_seconds: int = 30
    chat_active_max_messages: int = 2000
    chat_archived_session_ttl_days: int = 30
    chat_max_archived_sessions_per_user: int = 20


class ResearchSettings(BaseModel):
    """Settings for research-agent runtime artifacts and budgets."""

    cache_ttl_hours: int = 24
    normal_max_sources: int = 5
    deep_max_sources: int = 10
    normal_search_waves: int = 1
    deep_search_waves: int = 3
    normal_max_rereads: int = 0
    deep_max_rereads: int = 10
    source_excerpt_chars: int = 6000
    finalize_strict: bool = False
    # B-054: dynamic source budget. The agent keeps fetching until this many
    # USABLE (HTTP 2xx) sources are collected, or until the cap (base limit
    # multiplied by ``dynamic_budget_max_multiplier``) is reached. This keeps a
    # run with many 404/403 responses from under-citing while bounding the
    # worst case. See ``effective_max_sources`` / ``effective_search_waves``.
    target_usable_sources: int = 5
    dynamic_budget_max_multiplier: float = 2.5


class TelegramSettings(BaseModel):
    """Settings for Telegram channel."""

    workspace_base: Path = Path("workspaces")
    rate_limit_per_minute: int = 10
    whitelist: list[int] = []
    default_department: str = "default"
    admin_ids: list[int] = []

    # Fallback transport — manual IP overrides (empty = DoH auto-discovery)
    fallback_ips: list[str] = []

    # HTTP timeouts (seconds) — passed to python-telegram-bot's HTTPXRequest
    connect_timeout: float = 10.0
    read_timeout: float = 20.0
    pool_timeout: float = 8.0

    # Connection resilience
    init_max_retries: int = 8
    network_max_retries: int = 10
    conflict_max_retries: int = 3


class SkillsSettings(BaseModel):
    """Settings for semantic skill selection."""

    # "all" = inject every allowed skill (legacy), "semantic" = match by message
    selection_mode: Literal["all", "semantic"] = "semantic"
    # Max skills injected into the prompt per request
    top_k: int = 3
    # Minimum TF-IDF + keyword combined score to include a skill
    tfidf_threshold: float = 0.08
    # Weight multiplier for keyword hits (on top of TF-IDF score)
    keyword_boost: float = 0.5


class ExtensionsSettings(BaseModel):
    """Extra overlay paths for private extensions (mirror-layout).

    Each path mirrors the project structure: <path>/skills/, <path>/plugins/,
    <path>/config/subagents/, <path>/config/bootstrap/, <path>/config/mcp_servers.yaml.
    Used for private/corporate extensions that must not enter the public repo.
    Empty/unset entries (e.g. from an unresolved ``${VAR}``) and non-existent
    paths are skipped by ``resolve_dirs``.
    """

    extra_paths: list[str] = []


class LoggingSettings(BaseModel):
    """Settings for the logging pipeline."""

    level: str = "DEBUG"
    console_level: str = "INFO"
    log_dir: str = "logs"
    health_port: int = 8080
    trace_enabled: bool = True
    trace_level: Literal["metadata", "debug_preview", "full"] = "metadata"
    trace_preview_chars: int = 200
    # D-056 post-0.2.0: raw LLM request/response capture (opt-in, disabled by
    # default). Writes logs/llm_payloads.jsonl with allowlisted fields. Used for
    # diagnostics (what the model actually receives/returns) and future
    # fine-tuning dataset collection.
    capture_enabled: bool = False
    capture_fields: list[str] = Field(
        default_factory=lambda: [
            "request.model",
            "request.messages",
            "request.tools",
            "request.params",
            "request.extra_body",
            "response.content",
            "response.reasoning",
            "response.tool_calls",
            "response.usage",
            "response.finish_reason",
        ]
    )
    capture_dir: str = "logs"


class Settings(BaseSettings):
    """Main application settings."""

    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    web: WebSettings = WebSettings()
    web_channel: WebChannelSettings = WebChannelSettings()
    research: ResearchSettings = ResearchSettings()
    container: ContainerSettings = ContainerSettings()
    telegram: TelegramSettings = TelegramSettings()
    skills: SkillsSettings = SkillsSettings()
    extensions: ExtensionsSettings = ExtensionsSettings()
    logging: LoggingSettings = LoggingSettings()

    model_config = {"env_nested_delimiter": "__"}
