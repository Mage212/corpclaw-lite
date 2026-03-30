"""Tests for logging, security, and health modules.

Covers:
- setup_logging() correctly attaches CredentialScrubber to both handlers
- CredentialScrubber masks API keys in log records
- NetworkPolicy.to_docker_args() returns proper Docker arguments
- Health server counters and stats
"""

from __future__ import annotations

import logging
from pathlib import Path


# ── agent_logger / CredentialScrubber integration ────────────────────────────────

class TestSetupLogging:
    """Tests that setup_logging() correctly configures the credential scrubber."""

    def test_setup_logging_attaches_scrubber(self, tmp_path: Path) -> None:
        """setup_logging() should add CredentialScrubber filter to both handlers."""
        from corpclaw_lite.logging.agent_logger import setup_logging
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        root = logging.getLogger()
        old_handlers = root.handlers[:]
        root.handlers.clear()

        try:
            setup_logging(log_dir=tmp_path)
            for handler in root.handlers:
                has_scrubber = any(isinstance(f, CredentialScrubber) for f in handler.filters)
                assert has_scrubber, f"Handler {handler} missing CredentialScrubber filter"
        finally:
            for handler in root.handlers:
                if handler not in old_handlers:
                    handler.close()
            root.handlers.clear()
            root.handlers.extend(old_handlers)

    def test_setup_logging_creates_log_file(self, tmp_path: Path) -> None:
        from corpclaw_lite.logging.agent_logger import setup_logging

        log_dir = tmp_path / "new_logs"
        root = logging.getLogger()
        old_handlers = root.handlers[:]
        root.handlers.clear()

        try:
            setup_logging(log_dir=log_dir)
            assert (log_dir / "corpclaw.log").exists()
        finally:
            for handler in root.handlers:
                if handler not in old_handlers:
                    handler.close()
            root.handlers.clear()
            root.handlers.extend(old_handlers)


class TestCredentialScrubber:
    """Tests for CredentialScrubber log filter."""

    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg=msg, args=(), exc_info=None,
        )

    def test_scrubs_openai_key_pattern(self) -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        scrubber = CredentialScrubber()
        # Construct a key matching the sk-... pattern (20+ chars)
        key = "sk-" + "a" * 25
        record = self._make_record(f"Using key {key}")
        result = scrubber.filter(record)
        assert result is True
        assert key not in record.getMessage()
        assert "***REDACTED***" in record.getMessage()

    def test_passes_safe_message(self) -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        scrubber = CredentialScrubber()
        msg = "Processing file: report.xlsx for user marketing_team"
        record = self._make_record(msg)
        result = scrubber.filter(record)
        assert result is True
        assert record.getMessage() == msg

    def test_scrubs_github_pat(self) -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        scrubber = CredentialScrubber()
        # ghp_ followed by exactly 36 chars
        pat = "ghp_" + "B" * 36
        record = self._make_record(f"Token: {pat}")
        scrubber.filter(record)
        assert pat not in record.getMessage()
        assert "***REDACTED***" in record.getMessage()

    def test_scrubs_string_args(self) -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        scrubber = CredentialScrubber()
        key = "sk-" + "x" * 30
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg="Using %s",
            args=(key,),
            exc_info=None,
        )
        scrubber.filter(record)
        assert isinstance(record.args, tuple)
        assert key not in record.args[0]

    def test_non_string_msg_passes_through(self) -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        scrubber = CredentialScrubber()
        record = logging.LogRecord(
            name="test", level=logging.INFO,
            pathname="", lineno=0,
            msg=42,  # non-string msg  # type: ignore[arg-type]
            args=(), exc_info=None,
        )
        result = scrubber.filter(record)
        assert result is True  # should not crash


# ── NetworkPolicy ─────────────────────────────────────────────────────────────────

class TestNetworkPolicy:
    """Tests for NetworkPolicy Docker arguments generation."""

    def test_to_docker_args_sets_network_none(self) -> None:
        from corpclaw_lite.security.network_policy import NetworkPolicy

        policy = NetworkPolicy()
        args = policy.to_docker_args()
        assert args.get("network_mode") == "none"

    def test_to_docker_args_includes_allowed_domains_env(self) -> None:
        from corpclaw_lite.security.network_policy import NetworkPolicy

        policy = NetworkPolicy()
        policy.allowlist = ["api.anthropic.com", "localhost"]
        args = policy.to_docker_args()
        env = args.get("environment", [])
        assert isinstance(env, list)
        allowed_env = [e for e in env if isinstance(e, str) and "ALLOWED_DOMAINS" in e]
        assert len(allowed_env) == 1
        assert "api.anthropic.com" in allowed_env[0]

    def test_load_file_populates_allowlist(self, tmp_path: Path) -> None:
        from corpclaw_lite.security.network_policy import NetworkPolicy

        yaml_content = "allowlist:\n  - api.anthropic.com\n  - localhost\n"
        policy_file = tmp_path / "network_policy.yaml"
        policy_file.write_text(yaml_content)

        policy = NetworkPolicy()
        policy.load_file(policy_file)
        assert "api.anthropic.com" in policy.allowlist
        assert "localhost" in policy.allowlist

    def test_load_missing_file_graceful(self, tmp_path: Path) -> None:
        from corpclaw_lite.security.network_policy import NetworkPolicy

        policy = NetworkPolicy()
        # Should not raise, just log warning
        policy.load_file(tmp_path / "nonexistent.yaml")
        assert policy.allowlist == []


# ── Health counters ───────────────────────────────────────────────────────────────

class TestHealthCounters:
    """Tests for in-memory health counters."""

    def setup_method(self) -> None:
        """Reset counters before each test."""
        from corpclaw_lite.logging import health
        health._counters.clear()  # type: ignore[attr-defined]

    def test_increment_and_get_stats(self) -> None:
        from corpclaw_lite.logging import health

        health.increment("requests")
        health.increment("requests")
        health.increment("errors")

        stats = health.get_stats()
        assert stats["requests"] == 2
        assert stats["errors"] == 1

    def test_increment_by_value(self) -> None:
        from corpclaw_lite.logging import health

        health.increment("tool_calls", 5)
        stats = health.get_stats()
        assert stats["tool_calls"] == 5

    def test_get_stats_has_status_ok(self) -> None:
        from corpclaw_lite.logging import health

        stats = health.get_stats()
        assert stats["status"] == "ok"
        assert "uptime_seconds" in stats

    def test_get_stats_zero_when_not_incremented(self) -> None:
        from corpclaw_lite.logging import health

        stats = health.get_stats()
        assert stats["requests"] == 0
        assert stats["tool_calls"] == 0
        assert stats["errors"] == 0
