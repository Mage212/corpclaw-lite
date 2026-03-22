"""Tests for SendFileTool."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool
from corpclaw_lite.users.models import User


@pytest.fixture
def user() -> User:
    return User(id=1, name="Test", department="dev")


@pytest.fixture
def callback() -> AsyncMock:
    return AsyncMock(return_value="File sent.")


@pytest.mark.asyncio
async def test_send_file_requires_user(callback: AsyncMock) -> None:
    tool = SendFileTool(callback)
    res = await tool.execute(path="test.txt")
    assert "Error" in res
    assert "User context" in res


@pytest.mark.asyncio
async def test_send_file_validates_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback: AsyncMock, user: User
) -> None:
    monkeypatch.chdir(tmp_path)
    res = await tool_with_callback(callback).execute(path="../secret.txt", user=user)
    assert "Error" in res


@pytest.mark.asyncio
async def test_send_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback: AsyncMock, user: User
) -> None:
    monkeypatch.chdir(tmp_path)
    res = await tool_with_callback(callback).execute(path="nonexistent.txt", user=user)
    assert "Error" in res
    assert "does not exist" in res


@pytest.mark.asyncio
async def test_send_file_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, callback: AsyncMock, user: User
) -> None:
    monkeypatch.chdir(tmp_path)
    test_file = tmp_path / "report.pdf"
    test_file.write_bytes(b"fake pdf content")

    tool = SendFileTool(callback)
    res = await tool.execute(path="report.pdf", caption="Q4 Report", user=user)

    assert "File sent" in res
    callback.assert_called_once()
    call_args = callback.call_args
    assert call_args[0][0] == test_file.resolve()
    assert call_args[0][1] == user
    assert call_args[0][2] == "Q4 Report"


def tool_with_callback(cb: AsyncMock) -> SendFileTool:
    return SendFileTool(cb)
