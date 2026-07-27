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

import pytest

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
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=(),
            exc_info=None,
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
        # DC-020: ghp_ with 20+ alnum chars (synced with tool_guard_rules)
        pat = "ghp_" + "B" * 24
        record = self._make_record(f"Token: {pat}")
        scrubber.filter(record)
        assert pat not in record.getMessage()
        assert "***REDACTED***" in record.getMessage()

    @pytest.mark.parametrize(
        "token",
        [
            "sk-proj-" + "A" * 30,
            "sk-ant-" + "B" * 30,
            "hf_" + "C" * 30,
            "glpat-" + "D" * 30,
            "github_pat_" + "E" * 30,
            # H-7 (code review): previously-missed credential formats.
            "AIza" + "S" * 35,  # Google API key (39 chars total)
            # Minimal JWT: header.payload.signature, each a base64url segment.
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
            "x-api-key: " + "k" * 32,
            "authorization: Token " + "t" * 40,
            "Authorization: Bearer " + "b" * 40,  # scheme + token, title-case header
            "X-Api-Key: " + "K" * 32,  # case-insensitive header name
        ],
    )
    def test_scrubs_modern_token_formats(self, token: str) -> None:
        from corpclaw_lite.security.credential_scrubber import scrub_text

        assert token not in scrub_text(f"token={token}")

    def test_formatter_scrubs_exception_traceback(self) -> None:
        import io

        from corpclaw_lite.security.credential_scrubber import (
            CredentialScrubber,
            CredentialScrubbingFormatter,
        )

        token = "sk-proj-" + "Z" * 30
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(CredentialScrubber())
        handler.setFormatter(CredentialScrubbingFormatter("%(message)s"))
        logger = logging.getLogger("credential-traceback-test")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        try:
            raise RuntimeError(f"backend rejected {token}")
        except RuntimeError:
            logger.exception("request failed")

        rendered = stream.getvalue()
        assert token not in rendered
        assert "***REDACTED***" in rendered

    def test_scrubs_string_args(self) -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        scrubber = CredentialScrubber()
        key = "sk-" + "x" * 30
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
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
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=42,  # non-string msg  # type: ignore[arg-type]
            args=(),
            exc_info=None,
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

    def test_to_docker_args_returns_deny_all(self) -> None:
        from corpclaw_lite.security.network_policy import NetworkPolicy

        policy = NetworkPolicy()
        args = policy.to_docker_args()
        assert args["network_mode"] == "none"
        assert "environment" not in args


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
        assert stats["llm_calls"] == 0
        assert stats["llm_timeouts"] == 0
        assert stats["tool_errors"] == 0
        assert stats["guard_blocks"] == 0
        assert stats["approval_denied"] == 0
        assert stats["active_requests"] == 0

    def test_new_observability_counters(self) -> None:
        from corpclaw_lite.logging import health

        health.increment("llm_calls", 2)
        health.increment("tool_errors")
        health.increment("guard_blocks")
        stats = health.get_stats()

        assert stats["llm_calls"] == 2
        assert stats["tool_errors"] == 1
        assert stats["guard_blocks"] == 1


def test_health_server_defaults_to_loopback() -> None:
    """S3-11: run_health_server must default to 127.0.0.1, not 0.0.0.0."""
    import inspect

    from corpclaw_lite.logging import health

    sig = inspect.signature(health.run_health_server)
    assert sig.parameters["host"].default == "127.0.0.1"


def test_logging_settings_health_host_default_loopback() -> None:
    """S3-11: LoggingSettings.health_host defaults to loopback."""
    from corpclaw_lite.config.settings import LoggingSettings

    assert LoggingSettings().health_host == "127.0.0.1"
