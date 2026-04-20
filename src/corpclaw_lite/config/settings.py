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
    "RoutingRule",
    "Settings",
    "SkillsSettings",
    "TelegramSettings",
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


class LLMSettings(BaseModel):
    """Settings for LLM provider routing.

    Providers are registered via ``PROVIDER_*__*`` env vars in ``.env``.
    This model only contains routing rules that map tasks to providers + models.
    """

    routing: list[RoutingRule] = []


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
    max_facts_recall: int = 20


class TelegramSettings(BaseModel):
    """Settings for Telegram channel."""

    workspace_base: Path = Path("workspaces")
    rate_limit_per_minute: int = 10
    whitelist: list[int] = []
    default_department: str = "default"
    admin_ids: list[int] = []


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


class Settings(BaseSettings):
    """Main application settings."""

    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    container: ContainerSettings = ContainerSettings()
    telegram: TelegramSettings = TelegramSettings()
    skills: SkillsSettings = SkillsSettings()
    logging: LoggingSettings = LoggingSettings()

    model_config = {"env_nested_delimiter": "__"}
