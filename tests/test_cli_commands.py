"""Tests for CLI admin commands — user management, containers, skills, plugins."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.cli import (
    cmd_user_allow,
    cmd_user_create,
    cmd_user_deny,
    cmd_user_list,
    cmd_user_revoke,
)


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route UserManager to tmp so tests don't touch the real DB."""
    from corpclaw_lite.users.manager import UserManager

    original_init = UserManager.__init__

    def patched_init(self: UserManager, db_path: str = "") -> None:  # type: ignore[misc]
        original_init(self, str(tmp_path / "users.db"))

    monkeypatch.setattr(UserManager, "__init__", patched_init)


def test_cmd_user_list_empty(capsys: pytest.CaptureFixture[str]) -> None:
    cmd_user_list()
    assert "No users registered" in capsys.readouterr().out


def test_cmd_user_create_and_list(capsys: pytest.CaptureFixture[str]) -> None:
    cmd_user_create(telegram_id=12345, department="engineering", name="TestUser")
    out = capsys.readouterr().out
    assert "Created user" in out
    assert "TestUser" in out

    cmd_user_list()
    out = capsys.readouterr().out
    assert "12345" in out
    assert "TestUser" in out


def test_cmd_user_allow(capsys: pytest.CaptureFixture[str]) -> None:
    cmd_user_allow(telegram_id=99999, department="marketing")
    out = capsys.readouterr().out
    assert "99999" in out
    assert "whitelist" in out


def test_cmd_user_deny_not_in_whitelist(capsys: pytest.CaptureFixture[str]) -> None:
    cmd_user_deny(telegram_id=11111)
    out = capsys.readouterr().out
    assert "was not in whitelist" in out


def test_cmd_user_deny_in_whitelist(capsys: pytest.CaptureFixture[str]) -> None:
    cmd_user_allow(telegram_id=22222, department="sales")
    capsys.readouterr()  # clear
    cmd_user_deny(telegram_id=22222)
    out = capsys.readouterr().out
    assert "removed from whitelist" in out


def test_cmd_user_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    cmd_user_revoke(telegram_id=33333)
    out = capsys.readouterr().out
    assert "revoked" in out
