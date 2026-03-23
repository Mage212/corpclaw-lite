"""Tests for whitelist and access control in UserManager."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from corpclaw_lite.users.manager import UserManager


@pytest.fixture
def manager(tmp_path: Path) -> UserManager:
    """UserManager backed by a temp dir."""
    db_path = str(tmp_path / "data" / "users.db")
    return UserManager(db_path=db_path)


class TestWhitelist:
    def test_empty_whitelist_denies_all(self, manager: UserManager) -> None:
        assert manager.is_allowed(12345) is False

    def test_add_and_check(self, manager: UserManager) -> None:
        manager.add_to_whitelist(111, "marketing")
        assert manager.is_allowed(111) is True
        assert manager.is_allowed(222) is False

    def test_remove(self, manager: UserManager) -> None:
        manager.add_to_whitelist(111, "default")
        assert manager.remove_from_whitelist(111) is True
        assert manager.is_allowed(111) is False

    def test_remove_nonexistent(self, manager: UserManager) -> None:
        assert manager.remove_from_whitelist(999) is False

    def test_get_whitelist(self, manager: UserManager) -> None:
        manager.add_to_whitelist(111, "hr")
        manager.add_to_whitelist(222, "dev")
        wl = manager.get_whitelist()
        assert len(wl) == 2

    def test_duplicate_add_ignored(self, manager: UserManager) -> None:
        manager.add_to_whitelist(111, "default")
        manager.add_to_whitelist(111, "default")
        assert len(manager.get_whitelist()) == 1

    def test_get_department(self, manager: UserManager) -> None:
        manager.add_to_whitelist(111, "marketing")
        assert manager.get_whitelist_department(111) == "marketing"
        assert manager.get_whitelist_department(999) == "default"

    def test_seed_whitelist(self, manager: UserManager) -> None:
        manager.add_to_whitelist(111, "hr")
        manager.seed_whitelist([111, 222, 333], "default")
        wl = manager.get_whitelist()
        ids = {e["telegram_id"] for e in wl}
        assert ids == {111, 222, 333}
        # Existing entry keeps its department
        for e in wl:
            if e["telegram_id"] == 111:
                assert e["department"] == "hr"

    def test_persistence(self, tmp_path: Path) -> None:
        db = str(tmp_path / "data" / "users.db")
        m1 = UserManager(db_path=db)
        m1.add_to_whitelist(111, "default")

        m2 = UserManager(db_path=db)
        assert m2.is_allowed(111) is True


class TestRevokedSessions:
    def test_revoke_and_check(self, manager: UserManager) -> None:
        assert manager.is_session_revoked(111) is False
        manager.revoke_session(111)
        assert manager.is_session_revoked(111) is True

    def test_unrevoke(self, manager: UserManager) -> None:
        manager.revoke_session(111)
        manager.unrevoke_session(111)
        assert manager.is_session_revoked(111) is False

    def test_persistence(self, tmp_path: Path) -> None:
        db = str(tmp_path / "data" / "users.db")
        m1 = UserManager(db_path=db)
        m1.revoke_session(111)

        m2 = UserManager(db_path=db)
        assert m2.is_session_revoked(111) is True
