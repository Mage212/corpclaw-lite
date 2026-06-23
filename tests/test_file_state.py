"""Tests for B-058: FileStateRegistry — cross-agent stale-write detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.agent.file_state import FileStateRegistry


@pytest.fixture(autouse=True)
def _enable_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the env-disable flag is not set during tests."""
    monkeypatch.delenv("CORPCLAW_FILE_STATE_GUARD", raising=False)


# ─── record_read ─────────────────────────────────────────────────────────────


def test_record_read_stores_stamp() -> None:
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=100, size=42)
    assert reg.has_read(path="/ws/a.txt", task_id="r1") is True


def test_record_read_separated_per_task() -> None:
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=100, size=42)
    assert reg.has_read(path="/ws/a.txt", task_id="r2") is False


# ─── note_write ──────────────────────────────────────────────────────────────


def test_note_write_updates_last_writer() -> None:
    reg = FileStateRegistry()
    reg.note_write(path="/ws/a.txt", task_id="r1")
    assert reg.last_writer("/ws/a.txt") == "r1"


# ─── check_stale ─────────────────────────────────────────────────────────────


def test_check_stale_write_without_read_warns() -> None:
    """Class 1: writing a file this run has not read → warning."""
    reg = FileStateRegistry()
    reg.note_write(path="/ws/a.txt", task_id="other")  # someone else wrote
    warning = reg.check_stale(path="/ws/a.txt", task_id="r1")
    assert warning is not None
    assert "have not read" in warning


def test_check_stale_sibling_wrote_after_my_read_warns() -> None:
    """Class 2: sibling subagent wrote after my read → stale overwrite."""
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=100, size=42)
    reg.note_write(path="/ws/a.txt", task_id="r2")  # sibling wrote
    warning = reg.check_stale(path="/ws/a.txt", task_id="r1")
    assert warning is not None
    assert "another agent" in warning
    assert "r2" in warning


def test_check_stale_i_am_last_writer_no_warning() -> None:
    """I read it and I am the last writer → safe, no warning."""
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=100, size=42)
    reg.note_write(path="/ws/a.txt", task_id="r1")  # I wrote after reading
    assert reg.check_stale(path="/ws/a.txt", task_id="r1") is None


def test_check_stale_read_but_no_writer_no_warning() -> None:
    """I read, nobody wrote → safe."""
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=100, size=42)
    assert reg.check_stale(path="/ws/a.txt", task_id="r1") is None


def test_check_stale_other_read_does_not_count() -> None:
    """Only my own read counts; another agent's read doesn't clear my warning."""
    reg = FileStateRegistry()
    # r2 read, r1 did not.
    reg.record_read(path="/ws/a.txt", task_id="r2", mtime_ns=100, size=42)
    warning = reg.check_stale(path="/ws/a.txt", task_id="r1")
    assert warning is not None
    assert "have not read" in warning


# ─── writes_since ────────────────────────────────────────────────────────────


def test_writes_since_excludes_self() -> None:
    reg = FileStateRegistry()
    reg.note_write(path="/ws/a.txt", task_id="r1")
    reg.note_write(path="/ws/b.txt", task_id="r2")
    reg.note_write(path="/ws/c.txt", task_id="r1")
    paths = ["/ws/a.txt", "/ws/b.txt", "/ws/c.txt", "/ws/d.txt"]
    result = reg.writes_since(exclude_task_id="r1", paths=paths)
    assert result == ["/ws/b.txt"]


# ─── FIFO eviction ───────────────────────────────────────────────────────────


def test_fifo_eviction_at_max_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-agent path cache evicts FIFO beyond _MAX_PATHS_PER_AGENT."""
    import corpclaw_lite.agent.file_state as mod

    monkeypatch.setattr(mod, "_MAX_PATHS_PER_AGENT", 3)
    reg = mod.FileStateRegistry()
    for i in range(4):
        reg.record_read(path=f"/ws/f{i}.txt", task_id="r1", mtime_ns=i, size=1)
    # First path evicted, the rest remain.
    assert reg.has_read(path="/ws/f0.txt", task_id="r1") is False
    assert reg.has_read(path="/ws/f1.txt", task_id="r1") is True
    assert reg.has_read(path="/ws/f2.txt", task_id="r1") is True
    assert reg.has_read(path="/ws/f3.txt", task_id="r1") is True


# ─── reset ───────────────────────────────────────────────────────────────────


def test_reset_clears_state() -> None:
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=1, size=1)
    reg.note_write(path="/ws/a.txt", task_id="r1")
    reg.reset()
    assert reg.has_read(path="/ws/a.txt", task_id="r1") is False
    assert reg.last_writer("/ws/a.txt") is None


# ─── env disable ─────────────────────────────────────────────────────────────


def test_env_disable_makes_guard_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORPCLAW_FILE_STATE_GUARD", "0")
    reg = FileStateRegistry()
    reg.record_read(path="/ws/a.txt", task_id="r1", mtime_ns=1, size=1)
    # record_read was a no-op.
    assert reg.has_read(path="/ws/a.txt", task_id="r1") is False
    reg.note_write(path="/ws/a.txt", task_id="r1")
    assert reg.last_writer("/ws/a.txt") is None
    # check_stale returns None (no tracking).
    assert reg.check_stale(path="/ws/a.txt", task_id="r1") is None


# ─── record_read_path helper ─────────────────────────────────────────────────


def test_record_read_path_from_real_file(tmp_path: Path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("hello")
    reg = FileStateRegistry()
    reg.record_read_path(f, task_id="r1")
    assert reg.has_read(path=str(f), task_id="r1") is True


def test_record_read_path_missing_file_silent(tmp_path: Path) -> None:
    reg = FileStateRegistry()
    reg.record_read_path(tmp_path / "nope.txt", task_id="r1")
    # No exception, no stamp.
    assert reg.has_read(path=str(tmp_path / "nope.txt"), task_id="r1") is False


# ─── thread-safety smoke (concurrent reads/writes) ───────────────────────────


def test_concurrent_reads_and_writes_are_safe() -> None:
    """Smoke: no exception under concurrent access."""
    import threading

    reg = FileStateRegistry()
    errors: list[Exception] = []

    def reader() -> None:
        try:
            for i in range(50):
                reg.record_read(path=f"/ws/r{i}.txt", task_id="r1", mtime_ns=i, size=1)
                reg.check_stale(path=f"/ws/r{i}.txt", task_id="r1")
        except Exception as e:
            errors.append(e)

    def writer() -> None:
        try:
            for i in range(50):
                reg.note_write(path=f"/ws/r{i}.txt", task_id="r2")
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
