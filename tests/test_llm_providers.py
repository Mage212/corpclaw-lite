
from corpclaw_lite.config.settings import LLMSettings, ProviderSettings, RoutingRule
from corpclaw_lite.llm.routing import ProviderRouter


def test_provider_router() -> None:
    settings = LLMSettings(
        default="local",
        named={
            "local": ProviderSettings(type="openai", model="qwen"),
            "vision": ProviderSettings(type="openai", model="qwen-vl"),
            "cloud": ProviderSettings(type="anthropic", model="claude-3"),
        },
        routing=[
            RoutingRule(task_kind="vision", provider="vision"),
            RoutingRule(subagent_id="exec", provider="cloud"),
        ],
    )

    router = ProviderRouter(settings)

    # Test default
    assert router.get_provider_settings().model == "qwen"

    # Test routing by task kind
    assert router.get_provider_settings(task_kind="vision").model == "qwen-vl"

    # Test routing by subagent_id
    assert router.get_provider_settings(subagent_id="exec").model == "claude-3"


def test_xml_fallback_parsing() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

    content = """
Here is my answer.
<tool_call>
<name>read_file</name>
<arguments>{"path": "/tmp/test"}</arguments>
</tool_call>
"""
    result = parse_xml_tool_call(content)
    assert result.status == "valid"
    assert result.tool_call is not None
    assert result.tool_call.name == "read_file"
    assert result.tool_call.arguments["path"] == "/tmp/test"
