"""Tests for UserManager CRUD operations."""

from __future__ import annotations

from corpclaw_lite.users.manager import UserManager


def test_create_and_get_user(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)

    user = mgr.create_user(telegram_id=12345, department="engineering", name="Alice")
    assert user.name == "Alice"
    assert user.department == "engineering"
    assert user.telegram_id == 12345

    found = mgr.get_by_telegram_id(12345)
    assert found is not None
    assert found.name == "Alice"


def test_create_user_duplicate(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)

    mgr.create_user(telegram_id=111, department="dev", name="Bob")
    user2 = mgr.create_user(telegram_id=111, department="marketing", name="Bob2")
    # Should return existing user, not create duplicate
    assert user2.telegram_id == 111


def test_get_nonexistent_user(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    assert mgr.get_by_telegram_id(99999) is None


def test_list_users(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)

    mgr.create_user(telegram_id=1, department="dev")
    mgr.create_user(telegram_id=2, department="hr")

    users = mgr.list_users()
    assert len(users) == 2
    tids = {u.telegram_id for u in users}
    assert tids == {1, 2}
