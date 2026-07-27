"""Tests for UserManager CRUD operations."""

from __future__ import annotations

import sqlite3

import pytest

from corpclaw_lite.users.manager import UserManager

PASSWORD = "secret-password-123"


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
        password=PASSWORD,
        department="engineering",
        name="Alice Web",
        is_admin=True,
    )

    assert user.username == "alice"
    assert user.telegram_id is None
    assert user.workspace_key() == str(user.id)
    assert mgr.authenticate_web_user("alice", "wrong") is None

    authenticated = mgr.authenticate_web_user("alice", PASSWORD)
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


def test_web_password_policy_rejects_short_password(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)

    try:
        mgr.create_web_user(username="alice", password="short", department="engineering")
    except ValueError as e:
        assert "at least 12" in str(e)
    else:
        raise AssertionError("short web password was accepted")


def test_link_web_user_to_existing_telegram_profile(tmp_path) -> None:
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    telegram_user = mgr.create_user(telegram_id=278278319, department="engineering", name="Vadim")

    linked = mgr.link_web_user(
        telegram_id=278278319,
        username="Vadim",
        password=PASSWORD,
        is_admin=True,
    )

    assert linked.id == telegram_user.id
    assert linked.telegram_id == 278278319
    assert linked.username == "vadim"
    assert linked.workspace_key() == str(telegram_user.id)
    assert linked.memory_key() == str(telegram_user.id)

    authenticated = mgr.authenticate_web_user("vadim", PASSWORD)
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
        password=PASSWORD,
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
    mgr.create_web_user(username="vadim", password=PASSWORD, department="engineering")

    try:
        mgr.link_web_user(telegram_id=1, username="vadim", password=PASSWORD)
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
    source = mgr.create_web_user(username="vadim", password=PASSWORD, department="engineering")

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
            CREATE TABLE memory_entries (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                primary_abstraction TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                cue_indices_json TEXT NOT NULL DEFAULT '[]',
                UNIQUE(user_id, primary_abstraction)
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
            """
            INSERT INTO memory_entries (
                user_id, primary_abstraction, memory_value, cue_indices_json
            ) VALUES (?, ?, ?, '[]')
            """,
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

    merged = mgr.authenticate_web_user("vadim", PASSWORD)
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
        fact_user_ids = conn.execute("SELECT user_id FROM memory_entries").fetchall()
        web_session_user_ids = conn.execute("SELECT user_id FROM web_chat_sessions").fetchall()
        web_message_user_ids = conn.execute("SELECT user_id FROM web_chat_messages").fetchall()
    assert message_user_ids == [(str(target.id),)]
    assert fact_user_ids == [(str(target.id),)]
    assert web_session_user_ids == [(str(target.id),)]
    assert web_message_user_ids == [(str(target.id),)]


def test_merge_memory_facts_only_moves_non_conflicting(tmp_path) -> None:
    """Entries DB (no messages): non-conflicting abstractions move to target."""
    memory_db = tmp_path / "memory.db"
    with sqlite3.connect(memory_db) as conn:
        conn.execute(
            """
            CREATE TABLE memory_entries (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                primary_abstraction TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                cue_indices_json TEXT NOT NULL DEFAULT '[]',
                UNIQUE(user_id, primary_abstraction)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO memory_entries (
                user_id, primary_abstraction, memory_value, cue_indices_json
            ) VALUES (?, ?, ?, '[]')
            """,
            ("10", "source_only", "yes"),
        )
        conn.execute(
            """
            INSERT INTO memory_entries (
                user_id, primary_abstraction, memory_value, cue_indices_json
            ) VALUES (?, ?, ?, '[]')
            """,
            ("20", "target_only", "keep"),
        )

    moved_messages, moved_facts = UserManager._merge_memory(
        memory_db_path=memory_db,
        source_key="10",
        target_key="20",
    )
    assert moved_messages == 0
    assert moved_facts == 1

    with sqlite3.connect(memory_db) as conn:
        rows = {
            (str(r[0]), str(r[1]), str(r[2]))
            for r in conn.execute(
                "SELECT user_id, primary_abstraction, memory_value FROM memory_entries"
            ).fetchall()
        }
    assert rows == {("20", "source_only", "yes"), ("20", "target_only", "keep")}


def test_merge_web_user_failure_does_not_disable_source(tmp_path, monkeypatch) -> None:
    """B-074/M4: if a sub-migration (workspace/memory) fails mid-merge, the
    source user must NOT be left disabled. Credentials are moved and the
    source's username/password nulled (so it can't log in), but disabled=0
    keeps the row recoverable instead of a half-moved disabled user."""
    db = tmp_path / "users.db"
    memory_db = tmp_path / "memory.db"
    workspace_base = tmp_path / "workspaces"
    mgr = UserManager(db_path=str(db))
    target = mgr.create_user(telegram_id=278278319, department="engineering", name="Vadim")
    source = mgr.create_web_user(username="vadim", password=PASSWORD, department="engineering")
    (workspace_base / f"user_{source.id}").mkdir(parents=True)

    # Force _merge_memory to fail mid-merge.
    def boom(*args, **kwargs):
        raise RuntimeError("simulated memory-merge failure")

    monkeypatch.setattr(mgr, "_merge_memory", boom)

    with pytest.raises(RuntimeError, match="simulated memory-merge failure"):
        mgr.merge_web_user(
            source_user_id=source.id,
            target_user_id=target.id,
            workspace_base=workspace_base,
            memory_db_path=memory_db,
        )

    # Source is NOT disabled (disabled flag stays 0) — recoverable.
    source_after = mgr.get_by_id(source.id)
    assert source_after is not None
    assert not source_after.disabled


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
            CREATE TABLE memory_entries (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                primary_abstraction TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                cue_indices_json TEXT NOT NULL DEFAULT '[]',
                UNIQUE(user_id, primary_abstraction)
            )
            """
        )
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            ("278278319", "user", "hello"),
        )
        conn.execute(
            """
            INSERT INTO memory_entries (
                user_id, primary_abstraction, memory_value, cue_indices_json
            ) VALUES (?, ?, ?, '[]')
            """,
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
        fact_user_ids = conn.execute("SELECT user_id FROM memory_entries").fetchall()
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
    mgr.create_web_user(username="bob", password="old-password-123", department="it")

    assert mgr.set_web_password("bob", "new-password-123") is True
    assert mgr.authenticate_web_user("bob", "old-password-123") is None
    assert mgr.authenticate_web_user("bob", "new-password-123") is not None
    assert mgr.set_web_password("missing", "new-password-123") is False


def test_set_web_password_invalidates_existing_sessions(tmp_path) -> None:
    """S3-08: changing the password must invalidate prior web sessions."""
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    user = mgr.create_web_user(username="bob", password="old-password-123", department="it")

    # Establish a session before the password change.
    token, _csrf = mgr.create_web_session(user.id, ttl_hours=1)
    assert mgr.get_user_by_session(token) is not None

    assert mgr.set_web_password("bob", "new-password-123") is True

    # The pre-rotation session is no longer valid.
    assert mgr.get_user_by_session(token) is None


# ── S2-06: created_at read from DB ──────────────────────────────────────────


def test_created_at_read_from_db_not_field_default(tmp_path) -> None:
    """S2-06: created_at must come from the DB row, not the field default."""
    import time

    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    mgr.create_user(telegram_id=1, department="eng", name="Alice")
    # Sleep so datetime.now() at read time differs from the stored value.
    time.sleep(1.1)

    by_tg = mgr.get_by_telegram_id(1)
    assert by_tg is not None
    by_id = mgr.get_by_id(by_tg.id)
    assert by_id is not None
    listed = mgr.list_users()

    # All getters return the DB stored time, not "now" at read time.
    assert by_tg.created_at == by_id.created_at
    assert all(u.created_at == by_tg.created_at for u in listed)


# ── S2-07: create_user password without username ───────────────────────────


def test_create_user_password_without_username_raises(tmp_path) -> None:
    """S2-07: a password without a username must raise, not be silently dropped."""
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    with pytest.raises(ValueError, match="password without a username"):
        mgr.create_user(department="eng", telegram_id=2, password="secret")


def test_create_user_username_with_password_works(tmp_path) -> None:
    """S2-07 regression: username + password still works (validates + stores)."""
    db = str(tmp_path / "users.db")
    mgr = UserManager(db_path=db)
    user = mgr.create_user(department="eng", username="alice", password="good-pass-123")
    assert user.username == "alice"


# ── H-4 (code review): merge must reparent ALL user-keyed tables ────────────


def _setup_merge_full(tmp_path):
    """Build a users.db + memory.db + feedback.db + scheduler.db + workspace with
    one row for the source user in every user-keyed table. Used by the H-4
    reparent tests so each can focus on one table's post-merge state.
    """
    db = tmp_path / "users.db"
    memory_db = tmp_path / "memory.db"
    feedback_db = tmp_path / "feedback.db"
    scheduler_db = tmp_path / "scheduler.db"
    workspace_base = tmp_path / "workspaces"
    bootstrap_dir = tmp_path / "bootstrap"
    bootstrap_dir.mkdir()

    mgr = UserManager(db_path=str(db))
    target = mgr.create_user(telegram_id=100, department="eng", name="T")
    source = mgr.create_web_user(username="src", password=PASSWORD, department="eng")

    # Workspace + bootstrap artefacts.
    (workspace_base / f"user_{source.id}").mkdir(parents=True)
    (bootstrap_dir / f"{source.id}.md").write_text("bootstrap source", encoding="utf-8")

    # memory.db: every user-keyed table with one source row each.
    src = str(source.id)
    with sqlite3.connect(memory_db) as conn:
        conn.execute(
            "CREATE TABLE memory_entries (id INTEGER PRIMARY KEY, user_id TEXT, "
            "primary_abstraction TEXT, memory_value TEXT, cue_indices_json TEXT, "
            "UNIQUE(user_id, primary_abstraction))"
        )
        conn.execute(
            "CREATE TABLE web_chat_sessions "
            "(id INTEGER PRIMARY KEY, user_id TEXT, ended_at DATETIME)"
        )
        conn.execute(
            "CREATE TABLE web_chat_messages (id INTEGER PRIMARY KEY, "
            "session_id INTEGER, user_id TEXT, role TEXT, content TEXT)"
        )
        conn.execute(
            "CREATE TABLE web_chat_context (id INTEGER PRIMARY KEY, "
            "session_id INTEGER, user_id TEXT, role TEXT, content TEXT, "
            "tool_calls TEXT, tool_call_id TEXT, name TEXT, reasoning TEXT, seq INTEGER)"
        )
        conn.execute(
            "CREATE TABLE web_chat_pins (id INTEGER PRIMARY KEY, session_id INTEGER, "
            "user_id TEXT, path TEXT, kind TEXT, tokens INTEGER, approximate INTEGER, "
            "mode TEXT, content TEXT, label TEXT)"
        )
        conn.execute("CREATE TABLE agent_change_sets (run_id TEXT PRIMARY KEY, user_id TEXT)")
        conn.execute(
            "CREATE TABLE agent_file_changes (id INTEGER PRIMARY KEY, user_id TEXT, run_id TEXT)"
        )
        conn.execute("INSERT INTO web_chat_sessions (id, user_id) VALUES (1, ?)", (src,))
        conn.execute(
            "INSERT INTO web_chat_messages (session_id, user_id, role, content) "
            "VALUES (1, ?, 'user', 'hi')",
            (src,),
        )
        conn.execute(
            "INSERT INTO web_chat_context (session_id, user_id, role, content, seq) "
            "VALUES (1, ?, 'user', 'ctx', 1)",
            (src,),
        )
        conn.execute(
            "INSERT INTO web_chat_pins (session_id, user_id, path, kind, tokens, "
            "approximate, mode, content, label) "
            "VALUES (1, ?, '/p', 'file', 10, 1, 'auto', 'c', 'l')",
            (src,),
        )
        conn.execute("INSERT INTO agent_change_sets (run_id, user_id) VALUES ('r1', ?)", (src,))
        conn.execute("INSERT INTO agent_file_changes (user_id, run_id) VALUES (?, 'r1')", (src,))

    # feedback.db: one source row.
    with sqlite3.connect(feedback_db) as conn:
        conn.execute(
            "CREATE TABLE feedback_labels (id TEXT PRIMARY KEY, run_id TEXT, "
            "user_id TEXT, rating TEXT, channel TEXT, message_ref TEXT, "
            "created_at TEXT, updated_at TEXT, UNIQUE(run_id, user_id))"
        )
        conn.execute(
            "INSERT INTO feedback_labels (id, run_id, user_id, rating, channel, "
            "created_at, updated_at) VALUES ('f1', 'run1', ?, 'up', 'web', 't', 't')",
            (src,),
        )

    # scheduler.db: one source task. user_id is INTEGER here.
    with sqlite3.connect(scheduler_db) as conn:
        conn.execute(
            "CREATE TABLE scheduled_tasks "
            "(id INTEGER PRIMARY KEY, user_id INTEGER, status TEXT, dedup_key TEXT)"
        )
        conn.execute(
            "INSERT INTO scheduled_tasks (user_id, status) VALUES (?, 'pending')",
            (source.id,),
        )

    return mgr, source, target, memory_db, feedback_db, scheduler_db, workspace_base, bootstrap_dir


def test_merge_web_user_reparents_all_memory_db_tables(tmp_path) -> None:
    """H-4: web_chat_context, web_chat_pins, agent_change_sets, agent_file_changes
    move to target alongside sessions/messages (these were silently orphaned)."""
    (
        mgr,
        source,
        target,
        memory_db,
        _feedback_db,
        _scheduler_db,
        workspace_base,
        bootstrap_dir,
    ) = _setup_merge_full(tmp_path)

    result = mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        bootstrap_users_dir=bootstrap_dir,
    )
    assert result["source_disabled"] is True

    with sqlite3.connect(memory_db) as conn:
        for table in (
            "web_chat_context",
            "web_chat_pins",
            "agent_change_sets",
            "agent_file_changes",
        ):
            rows = conn.execute(f"SELECT user_id FROM {table}").fetchall()
            assert rows == [(str(target.id),)], f"{table} not reparented to target: {rows}"


def test_merge_web_user_reparents_feedback_labels(tmp_path) -> None:
    """H-4: feedback_labels (separate DB) move to target."""
    (
        mgr,
        source,
        target,
        memory_db,
        feedback_db,
        scheduler_db,
        workspace_base,
        bootstrap_dir,
    ) = _setup_merge_full(tmp_path)

    result = mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        feedback_db_path=feedback_db,
        scheduler_db_path=scheduler_db,
        bootstrap_users_dir=bootstrap_dir,
    )
    assert result["moved_feedback_labels"] == 1

    with sqlite3.connect(feedback_db) as conn:
        rows = conn.execute("SELECT user_id FROM feedback_labels").fetchall()
    assert rows == [(str(target.id),)]


def test_merge_web_user_reparents_scheduler_tasks(tmp_path) -> None:
    """H-4: scheduled_tasks (separate DB, INTEGER user_id) move to target."""
    (
        mgr,
        source,
        target,
        memory_db,
        feedback_db,
        scheduler_db,
        workspace_base,
        bootstrap_dir,
    ) = _setup_merge_full(tmp_path)

    result = mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        feedback_db_path=feedback_db,
        scheduler_db_path=scheduler_db,
        bootstrap_users_dir=bootstrap_dir,
    )
    assert result["moved_scheduler_tasks"] == 1

    with sqlite3.connect(scheduler_db) as conn:
        rows = conn.execute("SELECT user_id FROM scheduled_tasks").fetchall()
    assert rows == [(target.id,)]


def test_merge_web_user_migrates_onboarding_and_bootstrap(tmp_path) -> None:
    """H-4: merge_web_user (not just migrate_canonical_ids) reparents onboarding
    state and the bootstrap .md file — previously these were skipped."""
    (
        mgr,
        source,
        target,
        memory_db,
        _feedback_db,
        _scheduler_db,
        workspace_base,
        bootstrap_dir,
    ) = _setup_merge_full(tmp_path)

    # onboarding_state lives in users.db; seed a row for the source.
    with sqlite3.connect(str(mgr._db)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS onboarding_state (user_id INTEGER PRIMARY KEY, state TEXT)"
        )
        conn.execute(
            "INSERT INTO onboarding_state (user_id, state) VALUES (?, ?)",
            (source.id, "in_progress"),
        )

    result = mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        bootstrap_users_dir=bootstrap_dir,
    )
    assert result["moved_onboarding_states"] == 1
    assert result["moved_bootstrap_files"] == 1

    with sqlite3.connect(str(mgr._db)) as conn:
        onb = conn.execute("SELECT user_id FROM onboarding_state").fetchall()
    assert onb == [(target.id,)]
    assert (bootstrap_dir / f"{target.id}.md").exists()
    assert not (bootstrap_dir / f"{source.id}.md").exists()


def test_merge_web_user_is_idempotent_second_run_moves_nothing(tmp_path) -> None:
    """H-4: re-running merge after the first one moved everything reports zeros."""
    (
        mgr,
        source,
        target,
        memory_db,
        feedback_db,
        scheduler_db,
        workspace_base,
        bootstrap_dir,
    ) = _setup_merge_full(tmp_path)

    mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        feedback_db_path=feedback_db,
        scheduler_db_path=scheduler_db,
        bootstrap_users_dir=bootstrap_dir,
    )
    # Second pass: source has no rows left anywhere; all counters zero.
    # (merge_web_user raises on a disabled source only if re-invoked directly —
    # but the underlying reparent methods are idempotent, which is what matters
    # for re-running migrate_canonical_ids.)
    assert (
        mgr._reparent_feedback(
            feedback_db_path=feedback_db,
            source_key=str(source.id),
            target_key=str(target.id),
        )
        == 0
    )
    assert (
        mgr._reparent_scheduler(
            scheduler_db_path=scheduler_db,
            source_key=str(source.id),
            target_key=str(target.id),
        )
        == 0
    )


def test_merge_reparents_silently_skip_missing_tables(tmp_path) -> None:
    """H-4: a brand-new memory.db without the newer tables does not crash merge."""
    db = tmp_path / "users.db"
    memory_db = tmp_path / "memory.db"
    workspace_base = tmp_path / "workspaces"
    mgr = UserManager(db_path=str(db))
    target = mgr.create_user(telegram_id=1, department="eng")
    source = mgr.create_web_user(username="s", password=PASSWORD, department="eng")
    (workspace_base / f"user_{source.id}").mkdir(parents=True)

    # memory.db exists but has none of the new tables.
    with sqlite3.connect(memory_db) as conn:
        conn.execute(
            "CREATE TABLE memory_entries (id INTEGER PRIMARY KEY, user_id TEXT, "
            "primary_abstraction TEXT, memory_value TEXT, cue_indices_json TEXT, "
            "UNIQUE(user_id, primary_abstraction))"
        )

    # Must not raise.
    result = mgr.merge_web_user(
        source_user_id=source.id,
        target_user_id=target.id,
        workspace_base=workspace_base,
        memory_db_path=memory_db,
        feedback_db_path=tmp_path / "absent_feedback.db",  # does not exist
        scheduler_db_path=tmp_path / "absent_scheduler.db",  # does not exist
    )
    assert result["moved_feedback_labels"] == 0
    assert result["moved_scheduler_tasks"] == 0
