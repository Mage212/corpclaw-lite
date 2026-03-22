from __future__ import annotations

from datetime import datetime

from pydantic import ValidationError

from corpclaw_lite.extensions.tools.base import RiskLevel, ToolParam
from corpclaw_lite.llm.base import LLMResponse, ToolCall
from corpclaw_lite.users.models import User


def test_user_creation() -> None:
    u = User(id=1, telegram_id=12345, department="marketing", name="Vadim")
    assert u.id == 1
    assert u.telegram_id == 12345
    assert u.department == "marketing"
    assert isinstance(u.created_at, datetime)


def test_tool_call() -> None:
    tc = ToolCall(id="tc-1", name="read_file", arguments={"path": "/tmp"})
    assert tc.name == "read_file"
    assert "path" in tc.arguments


def test_llm_response() -> None:
    r = LLMResponse(content="Tested")
    assert r.content == "Tested"
    assert len(r.tool_calls) == 0


def test_tool_param() -> None:
    tp = ToolParam(name="path", type="string", description="File path", required=True)
    assert tp.name == "path"

    # Minimal example without description should raise validation error
    try:
        ToolParam(name="path", type="string")  # type: ignore
        assert False, "Should have raised validation error"
    except ValidationError:
        pass


def test_risk_level() -> None:
    assert RiskLevel.LOW.value == "low"
    assert RiskLevel.CRITICAL.value == "critical"
