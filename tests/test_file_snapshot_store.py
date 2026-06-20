"""Tests for B-040: FileSnapshotStore — on-disk backup copies."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.agent.file_snapshots import FileSnapshotStore
from corpclaw_lite.users.models import User


@pytest.fixture
def user() -> User:
    return User(id=1, name="Test", department="qa")


@pytest.fixture
def store(tmp_path: Path) -> FileSnapshotStore:
    return FileSnapshotStore(workspace_base=tmp_path)


def _workspace_root(tmp_path: Path, user: User) -> Path:
    return (tmp_path / f"user_{user.workspace_key()}").resolve()


# ─── backup ──────────────────────────────────────────────────────────────────


def test_backup_creates_file_in_snapshots_dir(
    tmp_path: Path, store: FileSnapshotStore, user: User
) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    source = ws / "report.xlsx"
    source.write_bytes(b"xlsx bytes")

    rel = store.backup(user, "run-1", source)
    assert rel == "report.xlsx"

    backup = tmp_path / f"user_{user.workspace_key()}" / ".snapshots" / "run-1" / "report.xlsx"
    assert backup.exists()
    assert backup.read_bytes() == b"xlsx bytes"


def test_backup_preserves_nested_relative_path(
    tmp_path: Path, store: FileSnapshotStore, user: User
) -> None:
    ws = _workspace_root(tmp_path, user)
    nested = ws / "data" / "2024" / "q3.csv"
    nested.parent.mkdir(parents=True)
    nested.write_text("a,b,c")

    rel = store.backup(user, "run-42", nested)
    assert rel == "data/2024/q3.csv"

    backup = (
        tmp_path
        / f"user_{user.workspace_key()}"
        / ".snapshots"
        / "run-42"
        / "data"
        / "2024"
        / "q3.csv"
    )
    assert backup.exists()
    assert backup.read_text() == "a,b,c"


def test_backup_rejects_source_outside_workspace(
    tmp_path: Path, store: FileSnapshotStore, user: User
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")
    with pytest.raises(ValueError, match="outside user workspace"):
        store.backup(user, "run-1", outside)


def test_backup_overwrites_previous(tmp_path: Path, store: FileSnapshotStore, user: User) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    source = ws / "f.txt"
    source.write_text("v1")
    store.backup(user, "run-1", source)
    source.write_text("v2")
    store.backup(user, "run-1", source)

    backup = tmp_path / f"user_{user.workspace_key()}" / ".snapshots" / "run-1" / "f.txt"
    assert backup.read_text() == "v2"


# ─── restore ─────────────────────────────────────────────────────────────────


def test_restore_overwrites_target_atomically(
    tmp_path: Path, store: FileSnapshotStore, user: User
) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    source = ws / "report.xlsx"
    source.write_bytes(b"original")

    rel = store.backup(user, "run-1", source)
    # Mutate the source.
    source.write_bytes(b"corrupted")

    store.restore(user, "run-1", rel, source)
    assert source.read_bytes() == b"original"


def test_restore_missing_backup_raises(
    tmp_path: Path, store: FileSnapshotStore, user: User
) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    target = ws / "f.txt"
    target.write_text("data")
    with pytest.raises(FileNotFoundError):
        store.restore(user, "run-1", "nonexistent.txt", target)


def test_restore_rejects_target_outside_workspace(
    tmp_path: Path, store: FileSnapshotStore, user: User
) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    source = ws / "f.txt"
    source.write_text("data")
    rel = store.backup(user, "run-1", source)

    outside = tmp_path / "outside.txt"
    with pytest.raises(ValueError, match="outside user workspace"):
        store.restore(user, "run-1", rel, outside)


# ─── prune ───────────────────────────────────────────────────────────────────


def test_prune_run_removes_dir(tmp_path: Path, store: FileSnapshotStore, user: User) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    source = ws / "f.txt"
    source.write_text("data")
    store.backup(user, "run-1", source)

    run_dir = tmp_path / f"user_{user.workspace_key()}" / ".snapshots" / "run-1"
    assert run_dir.exists()
    store.prune_run(user, "run-1")
    assert not run_dir.exists()


def test_prune_run_idempotent(tmp_path: Path, store: FileSnapshotStore, user: User) -> None:
    # Pruning a non-existent run must not raise.
    store.prune_run(user, "never-existed")


# ─── safe_run_id ─────────────────────────────────────────────────────────────


def test_safe_run_id_sanitizes(tmp_path: Path, store: FileSnapshotStore, user: User) -> None:
    ws = _workspace_root(tmp_path, user)
    ws.mkdir(parents=True)
    source = ws / "f.txt"
    source.write_text("data")
    # Slashes, spaces, dots → underscores, truncated.
    nasty = "../../etc/passwd with spaces"
    store.backup(user, nasty, source)
    run_dir = tmp_path / f"user_{user.workspace_key()}" / ".snapshots"
    children = [p.name for p in run_dir.iterdir()]
    assert len(children) == 1
    assert "/" not in children[0]
    assert " " not in children[0]
