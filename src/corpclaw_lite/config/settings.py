from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class ProviderSettings(BaseModel):
    """Settings for a specific LLM provider."""

    type: str = Field(..., description="Provider type (e.g., openai, anthropic)")
    model: str = Field(..., description="Model name to use")
    api_key: str | None = Field(None, description="API key (if required)")
    base_url: str | None = Field(None, description="Base URL (e.g., for local Ollama/vLLM)")


class RoutingRule(BaseModel):
    """Rule for routing tasks to specific providers."""

    task_kind: str | None = Field(None, description="Type of task (e.g., vision, subagent)")
    subagent_id: str | None = Field(None, description="Specific subagent ID")
    provider: str = Field(..., description="Name of the provider in settings to use")


class LLMSettings(BaseModel):
    """Settings for all LLM providers and routing."""

    default: str = Field("local", description="Default provider name")
    named: dict[str, ProviderSettings] = Field(default_factory=dict, description="Named providers")
    routing: list[RoutingRule] = Field(default_factory=list, description="Routing rules")


class AgentSettings(BaseModel):
    """Settings for the AgentLoop."""

    max_steps: int = Field(15, description="Maximum iterations in the ReAct loop")
    max_tool_calls: int = Field(30, description="Maximum total tool calls per request")
    max_wall_time_ms: int = Field(120000, description="Maximum execution time in ms")


class Settings(BaseSettings):
    """Main application settings."""

    llm: LLMSettings = Field(default_factory=LLMSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)

    model_config = {"env_nested_delimiter": "__"}
