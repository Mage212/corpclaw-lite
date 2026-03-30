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
    root.setLevel(logging.DEBUG)  # root must be lowest — handlers filter upward
    root.addHandler(text_handler)

    # Console handler — cleaner output for operators watching stdout
    console = logging.StreamHandler()
    console.setLevel(con_level)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.addFilter(CredentialScrubber())
    root.addHandler(console)


class AgentLogger:
    """
    Structured JSON event logger that writes to agent_activity.jsonl.

    Each call to log_request() appends one JSON record with standard fields:
    user_id, duration_ms, tools_used, tool_count, tokens, status, error.
    """

    def __init__(self, log_dir: Path | str = "logs") -> None:
        self._path = Path(log_dir) / "agent_activity.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            self._path,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
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
    ) -> None:
        """Write a single structured JSON record to agent_activity.jsonl."""
        record: dict[str, Any] = {
            "ts": time.time(),
            "user_id": user_id,
            "department": department,
            "message_preview": message_preview[:100],
            "duration_ms": round(duration_ms, 1),
            "tool_count": len(tools_used),
            "tools_used": tools_used,
            "tokens": tokens or {},
            "status": status,
        }
        if error:
            record["error"] = error

        self._logger.info(json.dumps(record, ensure_ascii=False))
