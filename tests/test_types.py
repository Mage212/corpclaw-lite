from inspect import iscoroutinefunction

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam


def test_tool_definition() -> None:
    class DummyTool(Tool):
        name = "dummy"
        description = "A dummy tool"
        params = [ToolParam(name="x", type="string", description="x desc")]
        risk_level = RiskLevel.LOW
        
        async def execute(self, **kwargs) -> str:
            return "ok"

    t = DummyTool()
    assert t.name == "dummy"
    assert len(t.params) == 1
    assert t.risk_level == RiskLevel.LOW
    assert iscoroutinefunction(t.execute)
