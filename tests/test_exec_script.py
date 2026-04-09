"""Tests for ExecScriptTool."""

from __future__ import annotations

import pytest

from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool


@pytest.fixture
def tool() -> ExecScriptTool:
    return ExecScriptTool()


@pytest.mark.asyncio
async def test_exec_simple_command(tool: ExecScriptTool) -> None:
    result = await tool.execute(script="echo hello")
    assert "Exit code: 0" in result
    assert "hello" in result


@pytest.mark.asyncio
async def test_exec_command_failure(tool: ExecScriptTool) -> None:
    result = await tool.execute(script="exit 1")
    assert "Exit code: 1" in result


@pytest.mark.asyncio
async def test_exec_timeout(tool: ExecScriptTool) -> None:
    result = await tool.execute(script='python -c "import time; time.sleep(10)"', timeout=1)
    assert "timed out" in result


@pytest.mark.asyncio
async def test_exec_script_required(tool: ExecScriptTool) -> None:
    result = await tool.execute()
    assert "Error" in result
    assert "script" in result


@pytest.mark.asyncio
async def test_exec_stderr_capture(tool: ExecScriptTool) -> None:
    result = await tool.execute(script="echo error_msg >&2")
    assert "error_msg" in result
    assert "Exit code: 0" in result


@pytest.mark.asyncio
async def test_exec_huge_output_truncated(tool: ExecScriptTool) -> None:
    result = await tool.execute(script="python3 -c \"print('x' * 100000)\"")
    assert "Exit code: 0" in result
    assert "truncated" in result or len(result) < 100100
