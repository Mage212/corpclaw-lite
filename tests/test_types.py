from inspect import iscoroutinefunction

from corpclaw_lite.extensions.tools.base import RiskLevel, Tool, ToolParam
from corpclaw_lite.users.models import User


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


def test_user_memory_key_with_telegram_id() -> None:
    user = User(id=42, name="Test", department="default", telegram_id=12345)
    assert user.memory_key() == "12345"


def test_user_memory_key_without_telegram_id() -> None:
    user = User(id=42, name="Test", department="default")
    assert user.memory_key() == "42"
