"""Assertion helpers and utilities for debug integration tests."""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.agent.loop import RunStats


class DebugAssertions:
    """Collection of assertion helpers for agent integration tests."""

    @staticmethod
    def assert_tool_used(stats: RunStats, *tool_names: str) -> None:
        """Assert that all specified tools appear in stats.tools_used."""
        for tool_name in tool_names:
            assert tool_name in stats.tools_used, (
                f"Expected tool '{tool_name}' to be used, but tools_used = {stats.tools_used}"
            )

    @staticmethod
    def assert_tool_not_used(stats: RunStats, tool_name: str) -> None:
        """Assert that a tool was NOT called."""
        assert tool_name not in stats.tools_used, (
            f"Tool '{tool_name}' was called but should not have been. "
            f"tools_used = {stats.tools_used}"
        )

    @staticmethod
    def assert_min_iterations(stats: RunStats, min_iterations: int) -> None:
        """Assert that the agent ran at least min_iterations ReAct cycles."""
        assert stats.iterations >= min_iterations, (
            f"Expected >= {min_iterations} iterations, "
            f"got {stats.iterations} (tools_used={stats.tools_used})"
        )

    @staticmethod
    def assert_status_ok(stats: RunStats) -> None:
        """Assert that the agent finished cleanly (no budget/loop/timeout)."""
        assert stats.status == "ok", (
            f"Expected status='ok', got '{stats.status}' "
            f"(error={stats.error!r}, iterations={stats.iterations})"
        )

    @staticmethod
    def assert_status(stats: RunStats, expected: str) -> None:
        """Assert a specific terminal status (ok/budget/loop/timeout/error)."""
        assert stats.status == expected, (
            f"Expected status='{expected}', got '{stats.status}' "
            f"(iterations={stats.iterations}, error={stats.error!r})"
        )

    @staticmethod
    def assert_reply_contains(reply: str, *fragments: str) -> None:
        """Assert that all fragments appear somewhere in the reply."""
        for fragment in fragments:
            assert fragment in reply, (
                f"Expected reply to contain {fragment!r}.\nReply (first 600 chars):\n{reply[:600]}"
            )

    @staticmethod
    def assert_reply_not_contains(reply: str, *fragments: str) -> None:
        """Assert that none of the fragments appear in the reply."""
        for fragment in fragments:
            assert fragment not in reply, (
                f"Reply should NOT contain {fragment!r}.\nReply (first 600 chars):\n{reply[:600]}"
            )

    @staticmethod
    def assert_no_tool_error(reply: str) -> None:
        """Assert the agent's reply contains no hard tool-execution errors.

        Checks for patterns that indicate a tool crashed unexpectedly
        (not blocked-by-design).
        """
        hard_errors = [
            "Traceback (most recent call last)",
            "AttributeError:",
            "TypeError:",
            "KeyError:",
            "ImportError:",
            "ModuleNotFoundError:",
        ]
        for marker in hard_errors:
            assert marker not in reply, (
                f"Reply contains unexpected stack-trace marker {marker!r}.\n"
                f"Reply (first 800 chars):\n{reply[:800]}"
            )

    @staticmethod
    def assert_file_exists(path: Path, contains: str | None = None) -> None:
        """Assert that a file exists and optionally contains a substring."""
        assert path.exists(), f"Expected file '{path}' to exist, but it does not."
        assert path.is_file(), f"Path '{path}' exists but is not a file."
        if contains is not None:
            content = path.read_text(encoding="utf-8")
            assert contains in content, (
                f"File '{path}' does not contain {contains!r}.\n"
                f"File content (first 500 chars):\n{content[:500]}"
            )

    @staticmethod
    def assert_file_not_contains(path: Path, substring: str) -> None:
        """Assert that a file does NOT contain a specific substring."""
        assert path.exists(), f"File '{path}' does not exist."
        content = path.read_text(encoding="utf-8")
        assert substring not in content, f"File '{path}' should NOT contain {substring!r} but does."


def summarise_run(reply: str, stats: RunStats) -> str:
    """Return a concise human-readable summary of a test run for failure messages."""
    tools = ", ".join(stats.tools_used) or "(none)"
    return (
        f"status={stats.status} | iterations={stats.iterations} | "
        f"tools=[{tools}] | duration={stats.duration_ms:.0f}ms\n"
        f"reply (first 400 chars): {reply[:400]}"
    )
