from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings

__all__ = [
    "AgentSettings",
    "CompressionSettings",
    "ContainerSettings",
    "LLMSettings",
    "LoggingSettings",
    "PersistentCacheSettings",
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
    a model from that provider, and an optional preset.
    """

    task_kind: str | None = None
    subagent_id: str | None = None
    provider: str = "default"
    model: str | None = None
    preset: str | None = None


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
    max_per_user: int = 1
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


class WebSettings(BaseModel):
    """Settings for host-side web tools."""

    search_backend: Literal["duckduckgo"] = "duckduckgo"
    search_max_concurrent: int = 3
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
    session_ttl_hours: int = 12
    cookie_name: str = "corpclaw_lite_session"


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


class LoggingSettings(BaseModel):
    """Settings for the logging pipeline."""

    level: str = "DEBUG"
    console_level: str = "INFO"
    log_dir: str = "logs"
    health_port: int = 8080
    trace_enabled: bool = True
    trace_level: Literal["metadata", "debug_preview", "full"] = "metadata"
    trace_preview_chars: int = 200


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
    logging: LoggingSettings = LoggingSettings()

    model_config = {"env_nested_delimiter": "__"}
