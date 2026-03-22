from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


def setup_logging(log_dir: Path | str = "logs") -> None:
    """
    Configure root logging with two handlers:
    - corpclaw.log: DEBUG text logs with rotation (5MB x 3 files)
    - agent_activity.jsonl: structured JSON events for analytics (10MB x 5)
    """
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Text log – human-readable DEBUG output
    text_handler = RotatingFileHandler(
        log_path / "corpclaw.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding="utf-8",
    )
    text_handler.setLevel(logging.DEBUG)
    text_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(text_handler)

    # Console handler (INFO only)
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
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
