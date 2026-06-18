"""Tests for B-055: ResultDedupGuard — result-based loop detection.

Complementary to ``SimpleProgressGuard`` (which detects repeated *errors*),
this guard detects repeated identical *successful results* — the common loop
mode for local LLMs. Reference: GAIA base/agent.py:3184-3206.
"""

from __future__ import annotations

from corpclaw_lite.agent.guards import (
    ResultDedupGuard,
    ResultDedupGuardConfig,
)

TOOL_ERROR_PREFIX = "Error:"


def _guard(*, enabled: bool = True, max_repeats: int = 2) -> ResultDedupGuard:
    return ResultDedupGuard(ResultDedupGuardConfig(enabled=enabled, max_repeats=max_repeats))


# ── detection ─────────────────────────────────────────────────────────────────


def test_detect_fires_on_second_identical_result() -> None:
    """Default max_repeats=2: second identical result triggers dedup."""
    guard = _guard()
    assert guard.detect("table_query", "rows: 42") is False
    assert guard.detect("table_query", "rows: 42") is True


def test_detect_does_not_fire_on_first_result() -> None:
    guard = _guard()
    assert guard.detect("read_file", "hello") is False


def test_detect_different_results_no_loop() -> None:
    """Distinct results must not trigger dedup."""
    guard = _guard()
    assert guard.detect("read_file", "content v1") is False
    assert guard.detect("read_file", "content v2") is False
    assert guard.detect("read_file", "content v3") is False


def test_detect_same_tool_different_results_not_loop() -> None:
    """Key GAIA insight: same tool name + different results = NOT a loop.

    Simulates a file changing between reads. Argument-based dedup would wrongly
    flag this; result-based dedup correctly sees different outputs.
    """
    guard = _guard()
    assert guard.detect("read_file", "state A") is False
    assert guard.detect("read_file", "state B") is False
    assert guard.detect("read_file", "state C") is False
    # A *second* appearance of an already-seen result (even interleaved with
    # different ones) is still a repeat — that is correct dedup behaviour.
    assert guard.detect("read_file", "state A") is True


def test_detect_different_tools_same_result_still_loops() -> None:
    """Dedup is per-result-hash, not per-tool — identical output across tools
    still indicates no new information regardless of which tool produced it."""
    guard = _guard()
    assert guard.detect("table_query", "empty") is False
    assert guard.detect("exec_script", "empty") is True


def test_max_repeats_three() -> None:
    guard = _guard(max_repeats=3)
    assert guard.detect("read_file", "x") is False
    assert guard.detect("read_file", "x") is False
    assert guard.detect("read_file", "x") is True


def test_last_count_tracks_repeats() -> None:
    guard = _guard()
    guard.detect("table_query", "rows: 1")
    assert guard.last_count("rows: 1") == 1
    guard.detect("table_query", "rows: 1")
    assert guard.last_count("rows: 1") == 2
    assert guard.last_count("different") == 0


# ── neutrality ────────────────────────────────────────────────────────────────


def test_detect_neutral_when_disabled() -> None:
    guard = _guard(enabled=False)
    # Even identical repeated results must not trigger when disabled.
    assert guard.detect("read_file", "same") is False
    assert guard.detect("read_file", "same") is False


def test_error_results_still_tracked() -> None:
    """The guard itself does not special-case error strings; the caller
    (_detect_result_dedup helper in loop.py) skips error-prefixed results so
    they fall through to SimpleProgressGuard. Verify the guard stays simple."""
    guard = _guard()
    err = f"{TOOL_ERROR_PREFIX} boom"
    assert guard.detect("exec_script", err) is False
    assert guard.detect("exec_script", err) is True


# ── reset ─────────────────────────────────────────────────────────────────────


def test_reset_clears_state() -> None:
    guard = _guard()
    guard.detect("read_file", "x")
    guard.detect("read_file", "x")
    assert guard.last_count("x") == 2
    guard.reset()
    assert guard.last_count("x") == 0
    # After reset the same result is fresh again.
    assert guard.detect("read_file", "x") is False


def test_default_config_used_when_none() -> None:
    guard = ResultDedupGuard()
    assert guard.config.enabled is True
    assert guard.config.max_repeats == 2
