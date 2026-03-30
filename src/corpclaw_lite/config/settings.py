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
    "ProviderSettings",
    "RoutingRule",
    "Settings",
    "TelegramSettings",
]


class ProviderSettings(BaseModel):
    """Settings for a specific LLM provider."""

    type: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    base_url: str | None = None


class RoutingRule(BaseModel):
    """Rule for routing tasks to specific providers."""

    task_kind: str | None = None
    subagent_id: str | None = None
    provider: str = "default"


class LLMSettings(BaseModel):
    """Settings for all LLM providers and routing."""

    default: str = "local"
    named: dict[str, ProviderSettings] = {}
    routing: list[RoutingRule] = []


class ContainerSettings(BaseModel):
    """Settings for Docker container sandboxes."""

    max_memory: str = "512m"
    cpus: float = 0.5
    idle_timeout_seconds: int = 600
    max_per_user: int = 1


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
    max_wall_time_ms: int = 120000
    max_history: int = 20
    consolidation_threshold: int = 30
    consolidation_enabled: bool = True
    approval_mode: Literal["manual", "smart", "off"] = "manual"
    compression: CompressionSettings = CompressionSettings()


class TelegramSettings(BaseModel):
    """Settings for Telegram channel."""

    workspace_base: Path = Path("workspaces")
    rate_limit_per_minute: int = 10
    whitelist: list[int] = []
    default_department: str = "default"
    admin_ids: list[int] = []


class Settings(BaseSettings):
    """Main application settings."""

    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    container: ContainerSettings = ContainerSettings()
    telegram: TelegramSettings = TelegramSettings()

    model_config = {"env_nested_delimiter": "__"}
