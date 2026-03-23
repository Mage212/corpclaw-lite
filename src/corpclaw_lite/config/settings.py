from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings


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


class AgentSettings(BaseModel):
    """Settings for the AgentLoop."""

    max_steps: int = 15
    max_tool_calls: int = 30
    max_wall_time_ms: int = 120000
    max_history: int = 20
    consolidation_threshold: int = 30
    consolidation_enabled: bool = True


class ContainerSettings(BaseModel):
    """Settings for Docker container sandboxes."""

    max_memory: str = "512m"
    cpus: float = 0.5
    idle_timeout_seconds: int = 600
    max_per_user: int = 1


class Settings(BaseSettings):
    """Main application settings."""

    llm: LLMSettings = LLMSettings()
    agent: AgentSettings = AgentSettings()
    container: ContainerSettings = ContainerSettings()

    model_config = {"env_nested_delimiter": "__"}
