"""Tests for SimpleProgressGuard — cross-turn error-loop detection (B-069).

The guard detects when the same tool action produces the same normalized error
across multiple model turns. Since B-069 it skips (rather than resets on)
successful results in a mixed batch, so a sibling tool looping on an error
while another tool succeeds is still detected.
"""

from __future__ import annotations

from corpclaw_lite.agent.guards import (
    SimpleProgressGuard,
    SimpleProgressGuardConfig,
)

_ERROR = "Error: file not found"
_SUCCESS = "rows: 42"


class TestDetectLoopForResults:
    def test_mixed_batch_accumulates_error_across_turns(self) -> None:
        """B-069 core: [error_X, success, error_X] every turn trips the guard.

        Before B-069 the success at index 1 reset the counter every turn, so the
        looping sibling tool (X) was never detected.
        """
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=3))
        batch = [("read_file", _ERROR), ("list_files", _SUCCESS), ("read_file", _ERROR)]
        # turns 1 and 2: signature stable, count grows but not yet at limit
        assert guard.detect_loop_for_results(batch) is False
        assert guard.state.same_error_count == 1
        assert guard.detect_loop_for_results(batch) is False
        assert guard.state.same_error_count == 2
        # turn 3: count reaches max → loop detected
        assert guard.detect_loop_for_results(batch) is True
        assert guard.state.same_error_count == 3

    def test_all_success_batch_resets_count(self) -> None:
        """A fully-successful batch is genuine progress — reset, no loop."""
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=2))
        # seed an error signature first
        guard.detect_loop_for_results([("read_file", _ERROR)])
        assert guard.state.same_error_count == 1
        # now a fully-successful batch
        assert guard.detect_loop_for_results([("list_files", _SUCCESS)]) is False
        assert guard.state.same_error_count == 0
        assert guard.state.last_tool_error_signature is None

    def test_same_error_same_tool_one_turn_no_loop(self) -> None:
        """[error, error, error] for the SAME tool in ONE turn must NOT loop.

        Signature dedup collapses identical (tool, error) pairs to one entry, so
        the count only reaches 1. Regression for the parallel/sequential
        same-batch integration tests.
        """
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=3))
        batch = [("read_file", _ERROR), ("read_file", _ERROR), ("read_file", _ERROR)]
        assert guard.detect_loop_for_results(batch) is False
        assert guard.state.same_error_count == 1

    def test_changing_error_signature_resets_count(self) -> None:
        """If the error pair set changes turn to turn, the count resets — no
        false positive on legitimately-shifting failures."""
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=3))
        assert guard.detect_loop_for_results([("read_file", "Error: not found")]) is False
        assert guard.state.same_error_count == 1
        # different error → new signature → count resets to 1
        assert guard.detect_loop_for_results([("read_file", "Error: permission denied")]) is False
        assert guard.state.same_error_count == 1

    def test_different_tools_same_error_still_loops(self) -> None:
        """Two different tools producing the same normalized error in one turn
        form a 2-element signature; repeating it across turns loops."""
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=2))
        batch = [("read_file", _ERROR), ("search_files", _ERROR)]
        assert guard.detect_loop_for_results(batch) is False
        assert guard.state.same_error_count == 1
        assert guard.detect_loop_for_results(batch) is True
        assert guard.state.same_error_count == 2

    def test_error_normalized_digits_stripped(self) -> None:
        """Variable numeric parts (timestamps, file paths) are stripped before
        comparison, so the same logical error at different offsets matches."""
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=2))
        # turn 1: error mentions line 10
        assert guard.detect_loop_for_results([("read_file", "Error at line 10")]) is False
        # turn 2: same logical error at line 42 → normalized identically → loops
        assert guard.detect_loop_for_results([("read_file", "Error at line 42")]) is True

    def test_detect_loop_single_result_delegates(self) -> None:
        """detect_loop wraps detect_loop_for_results with a single-element list."""
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=2))
        assert guard.detect_loop("read_file", _ERROR) is False
        assert guard.detect_loop("read_file", _ERROR) is True

    def test_disabled_guard_never_loops(self) -> None:
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(enabled=False, max_same_tool_error=1))
        assert guard.detect_loop_for_results([("read_file", _ERROR)]) is False
        assert guard.detect_loop_for_results([("read_file", _ERROR)]) is False


class TestReset:
    def test_reset_clears_state(self) -> None:
        guard = SimpleProgressGuard(SimpleProgressGuardConfig(max_same_tool_error=2))
        guard.detect_loop_for_results([("read_file", _ERROR)])
        assert guard.state.same_error_count == 1
        guard.reset()
        assert guard.state.same_error_count == 0
        assert guard.state.last_tool_error_signature is None
