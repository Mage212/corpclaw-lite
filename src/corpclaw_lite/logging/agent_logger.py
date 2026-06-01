from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

__all__ = [
    "AgentLogger",
    "setup_logging",
]


def setup_logging(
    log_dir: Path | str = "logs",
    level: str = "DEBUG",
    console_level: str = "INFO",
    trace_enabled: bool = True,
    trace_level: str = "metadata",
    trace_preview_chars: int = 200,
) -> None:
    """Configure root logging with two handlers:

    - corpclaw.log: text logs with rotation (5MB x 3 files), level=level
    - Console (stderr): level=console_level (INFO by default — less noise)

    Both handlers strip credentials via CredentialScrubber.
    Call this once at application startup before any loggers are used.
    """
    from corpclaw_lite.security.credential_scrubber import CredentialScrubber

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    file_level: int = getattr(logging, level.upper(), logging.DEBUG)
    con_level: int = getattr(logging, console_level.upper(), logging.INFO)

    # Text log — human-readable, full detail for debugging
    text_handler = RotatingFileHandler(
        log_path / "corpclaw.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=3,
        encoding="utf-8",
    )
    text_handler.setLevel(file_level)
    text_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    text_handler.addFilter(CredentialScrubber())

    root = logging.getLogger()
    root.handlers.clear()  # Prevent duplicate logs if setup_logging is called twice
    root.setLevel(logging.DEBUG)  # root must be lowest — handlers filter upward
    root.addHandler(text_handler)

    # Console handler — cleaner output for operators watching stdout
    console = logging.StreamHandler()
    console.setLevel(con_level)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.addFilter(CredentialScrubber())
    root.addHandler(console)

    # Reduce spam from underlying HTTP libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
    logging.getLogger("telegram.ext").setLevel(logging.INFO)

    from corpclaw_lite.logging.trace import setup_trace_logging

    setup_trace_logging(
        log_dir=log_path,
        enabled=trace_enabled,
        trace_level=trace_level,  # type: ignore[arg-type]
        preview_chars=trace_preview_chars,
    )


class AgentLogger:
    """
    Structured JSON event logger that writes to agent_activity.jsonl.

    Each call to log_request() appends one JSON record with standard fields:
    user_id, duration_ms, tools_used, tool_count, tokens, status, error.
    """

    def __init__(self, log_dir: Path | str = "logs") -> None:
        from corpclaw_lite.security.credential_scrubber import CredentialScrubber

        self._path = Path(log_dir) / "agent_activity.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            self._path,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        self._handler.addFilter(CredentialScrubber())
        self._logger = logging.getLogger("agent_activity")
        self._logger.addHandler(self._handler)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False  # Don't bubble up to root

    def log_request(
        self,
        *,
        user_id: str,
        department: str,
        message_preview: str,
        duration_ms: float,
        tools_used: list[str],
        tokens: dict[str, int] | None = None,
        status: str = "ok",
        error: str | None = None,
        run_id: str | None = None,
        channel: str | None = None,
        iterations: int | None = None,
        llm_calls: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        latest_total_tokens: int | None = None,
        stream_stats: dict[str, Any] | None = None,
    ) -> None:
        """Write a single structured JSON record to agent_activity.jsonl."""
        from corpclaw_lite.security.credential_scrubber import scrub_text

        record: dict[str, Any] = {
            "ts": time.time(),
            "user_id": user_id,
            "department": department,
            "message_preview": scrub_text(message_preview)[:100],
            "duration_ms": round(duration_ms, 1),
            "tool_count": len(tools_used),
            "tools_used": tools_used,
            "tokens": tokens or {},
            "status": status,
        }
        if run_id is not None:
            record["run_id"] = run_id
        if channel is not None:
            record["channel"] = channel
        if iterations is not None:
            record["iterations"] = iterations
        if llm_calls is not None:
            record["llm_calls"] = llm_calls
        if input_tokens is not None:
            record["input_tokens"] = input_tokens
        if output_tokens is not None:
            record["output_tokens"] = output_tokens
        if total_tokens is not None:
            record["total_tokens"] = total_tokens
        if latest_total_tokens is not None:
            record["latest_total_tokens"] = latest_total_tokens
        if stream_stats is not None:
            record["stream"] = stream_stats
        if error:
            record["error"] = scrub_text(error)

        self._logger.info(json.dumps(record, ensure_ascii=False))
