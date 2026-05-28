"""Tests for UserManager CRUD operations."""

from __future__ import annotations

import sqlite3

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


def test_web_user_auth_and_session(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)

    user = mgr.create_web_user(
        username="Alice",
        password="secret",
        department="engineering",
        name="Alice Web",
        is_admin=True,
    )

    assert user.username == "alice"
    assert user.telegram_id is None
    assert user.workspace_key() == str(user.id)
    assert mgr.authenticate_web_user("alice", "wrong") is None

    authenticated = mgr.authenticate_web_user("alice", "secret")
    assert authenticated is not None
    assert authenticated.is_admin is True

    token, csrf = mgr.create_web_session(user.id, ttl_hours=1)
    session = mgr.get_user_by_session(token)
    assert session is not None
    session_user, session_csrf = session
    assert session_user.id == user.id
    assert session_csrf == csrf

    mgr.delete_web_session(token)
    assert mgr.get_user_by_session(token) is None


def test_link_web_user_to_existing_telegram_profile(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    telegram_user = mgr.create_user(telegram_id=278278319, department="engineering", name="Vadim")

    linked = mgr.link_web_user(
        telegram_id=278278319,
        username="Vadim",
        password="secret",
        is_admin=True,
    )

    assert linked.id == telegram_user.id
    assert linked.telegram_id == 278278319
    assert linked.username == "vadim"
    assert linked.workspace_key() == str(telegram_user.id)
    assert linked.memory_key() == str(telegram_user.id)

    authenticated = mgr.authenticate_web_user("vadim", "secret")
    assert authenticated is not None
    assert authenticated.id == telegram_user.id
    assert authenticated.telegram_id == 278278319
    assert authenticated.workspace_key() == str(telegram_user.id)
    assert authenticated.is_admin is True


def test_create_web_user_with_telegram_id_links_existing_profile(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    telegram_user = mgr.create_user(telegram_id=123, department="engineering", name="Alice")

    linked = mgr.create_web_user(
        username="alice",
        password="secret",
        department="ignored",
        telegram_id=123,
    )

    assert linked.id == telegram_user.id
    assert linked.department == "engineering"
    assert linked.workspace_key() == str(telegram_user.id)
    assert len(mgr.list_users()) == 1


def test_link_web_user_rejects_duplicate_username(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    mgr.create_user(telegram_id=1, department="engineering")
    mgr.create_web_user(username="vadim", password="secret", department="engineering")

    try:
        mgr.link_web_user(telegram_id=1, username="vadim", password="secret")
    except ValueError as e:
        assert "already belongs" in str(e)
    else:
        raise AssertionError("duplicate username was accepted")


def test_merge_web_user_moves_credentials_workspace_and_memory(tmp_path) -> None:
    db = tmp_path / "users.db"
    memory_db = tmp_path / "memory.db"
    workspace_base = tmp_path / "workspaces"
    mgr = UserManager(db_path=str(db))
    target = mgr.create_user(telegram_id=278278319, department="engineering", name="Vadim")
    source = mgr.create_web_user(username="vadim", password="secret", department="engineering")

    source_ws = workspace_base / f"user_{source.id}"
    target_ws = workspace_base / f"user_{target.id}"
    source_ws.mkdir(parents=True)
    target_ws.mkdir(parents=True)
    (source_ws / "note.txt").write_text("from source", encoding="utf-8")
    (target_ws / "note.txt").write_text("from target", encoding="utf-8")

    with sqlite3.connect(memory_db) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT,
                content TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE memory_facts (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE(user_id, key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE web_chat_sessions (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                ended_at DATETIME
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE web_chat_messages (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (str(source.id), "user", "hello"),
        )
        conn.execute(
            "INSERT INTO memory_facts (user_id, key, value) VALUES (?, ?, ?)",
            (str(source.id), "source_fact", "yes"),
        )
        conn.execute(
            "INSERT INTO web_chat_sessions (id, user_id) VALUES (?, ?)",
            (1, str(source.id)),
        )
        conn.execute(
            """
            INSERT INTO web_chat_messages (session_id, user_id, role, content)
            VALUES (?, ?, ?, ?)
            """,
            (1, str(source.id), "user", "web hello"),
        )

    result = mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
    )

    assert result["moved_workspace_items"] == 1
    assert result["moved_messages"] == 1
    assert result["moved_facts"] == 1

    merged = mgr.authenticate_web_user("vadim", "secret")
    assert merged is not None
    assert merged.id == target.id
    assert merged.telegram_id == 278278319
    assert merged.workspace_key() == str(target.id)

    disabled_source = mgr.get_by_id(source.id)
    assert disabled_source is not None
    assert disabled_source.disabled is True
    assert disabled_source.username is None

    assert (target_ws / "note.txt").read_text(encoding="utf-8") == "from target"
    assert (target_ws / f"note.txt.from_user_{source.id}").read_text(
        encoding="utf-8"
    ) == "from source"

    with sqlite3.connect(memory_db) as conn:
        message_user_ids = conn.execute("SELECT user_id FROM messages").fetchall()
        fact_user_ids = conn.execute("SELECT user_id FROM memory_facts").fetchall()
        web_session_user_ids = conn.execute("SELECT user_id FROM web_chat_sessions").fetchall()
        web_message_user_ids = conn.execute("SELECT user_id FROM web_chat_messages").fetchall()
    assert message_user_ids == [(str(target.id),)]
    assert fact_user_ids == [(str(target.id),)]
    assert web_session_user_ids == [(str(target.id),)]
    assert web_message_user_ids == [(str(target.id),)]


def test_migrate_canonical_ids_moves_legacy_telegram_data(tmp_path) -> None:
    db = tmp_path / "users.db"
    memory_db = tmp_path / "memory.db"
    workspace_base = tmp_path / "workspaces"
    bootstrap_dir = tmp_path / "bootstrap_users"
    mgr = UserManager(db_path=str(db))
    user = mgr.create_user(telegram_id=278278319, department="engineering", name="Vadim")

    legacy_ws = workspace_base / "user_278278319"
    target_ws = workspace_base / f"user_{user.id}"
    legacy_ws.mkdir(parents=True)
    target_ws.mkdir(parents=True)
    (legacy_ws / "legacy.txt").write_text("legacy", encoding="utf-8")

    bootstrap_dir.mkdir()
    (bootstrap_dir / "278278319.md").write_text("legacy prompt", encoding="utf-8")

    with sqlite3.connect(memory_db) as conn:
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT,
                content TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE memory_facts (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                UNIQUE(user_id, key)
            )
            """
        )
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            ("278278319", "user", "hello"),
        )
        conn.execute(
            "INSERT INTO memory_facts (user_id, key, value) VALUES (?, ?, ?)",
            ("278278319", "role", "architect"),
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE onboarding_state (
                user_id INTEGER PRIMARY KEY,
                current_step INTEGER NOT NULL DEFAULT 0,
                answers_json TEXT NOT NULL DEFAULT '{}',
                completed BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        conn.execute(
            "INSERT INTO onboarding_state (user_id, completed) VALUES (?, ?)",
            (278278319, 1),
        )

    result = mgr.migrate_canonical_ids(
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        bootstrap_users_dir=bootstrap_dir,
    )

    assert result["users"] == 1
    assert result["workspace_items"] == 1
    assert result["messages"] == 1
    assert result["facts"] == 1
    assert result["onboarding_states"] == 1
    assert result["bootstrap_files"] == 1
    assert not legacy_ws.exists()
    assert (target_ws / "legacy.txt").read_text(encoding="utf-8") == "legacy"
    assert (bootstrap_dir / f"{user.id}.md").read_text(encoding="utf-8") == "legacy prompt"

    with sqlite3.connect(memory_db) as conn:
        message_user_ids = conn.execute("SELECT user_id FROM messages").fetchall()
        fact_user_ids = conn.execute("SELECT user_id FROM memory_facts").fetchall()
    with sqlite3.connect(db) as conn:
        onboarding_ids = conn.execute("SELECT user_id FROM onboarding_state").fetchall()
    assert message_user_ids == [(str(user.id),)]
    assert fact_user_ids == [(str(user.id),)]
    assert onboarding_ids == [(user.id,)]

    second_result = mgr.migrate_canonical_ids(
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        bootstrap_users_dir=bootstrap_dir,
    )
    assert second_result["workspace_items"] == 0
    assert second_result["messages"] == 0
    assert second_result["facts"] == 0
    assert second_result["onboarding_states"] == 0
    assert second_result["bootstrap_files"] == 0


def test_set_web_password(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    mgr.create_web_user(username="bob", password="old", department="it")

    assert mgr.set_web_password("bob", "new") is True
    assert mgr.authenticate_web_user("bob", "old") is None
    assert mgr.authenticate_web_user("bob", "new") is not None
    assert mgr.set_web_password("missing", "new") is False
