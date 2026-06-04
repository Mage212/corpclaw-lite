from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal, cast

from corpclaw_lite.security.credential_scrubber import CredentialScrubber, scrub_text

__all__ = [
    "TraceLogger",
    "get_trace_logger",
    "log_event",
    "setup_trace_logging",
]

TraceLevel = Literal["metadata", "debug_preview", "full"]

_trace_logger: TraceLogger | None = None


class TraceLogger:
    """Structured JSONL trace logger for reconstructing one agent run."""

    def __init__(
        self,
        log_dir: Path | str = "logs",
        *,
        enabled: bool = True,
        trace_level: TraceLevel = "metadata",
        preview_chars: int = 200,
    ) -> None:
        self.enabled = enabled
        self.trace_level = trace_level
        self.preview_chars = preview_chars
        self._path = Path(log_dir) / "agent_trace.jsonl"

        self._logger = logging.getLogger("agent_trace")
        self._logger.handlers.clear()
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        if not enabled:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            self._path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.addFilter(CredentialScrubber())
        self._logger.addHandler(handler)

    def log_event(self, event: str, run_id: str, **fields: Any) -> None:
        """Write one trace event as JSONL.

        The first implementation is intentionally metadata-first: string fields
        are scrubbed and truncated unless the caller has already reduced them to
        a count, status, id, or preview.
        """
        if not self.enabled:
            return

        record: dict[str, Any] = {
            "ts": time.time(),
            "event": event,
            "run_id": run_id,
        }
        record.update({k: self._sanitize(v) for k, v in fields.items()})
        self._logger.info(json.dumps(record, ensure_ascii=False, default=str))

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            scrubbed = scrub_text(value)
            if self.trace_level in ("metadata", "debug_preview"):
                return scrubbed[: self.preview_chars]
            return scrubbed
        if isinstance(value, dict):
            dict_value = cast(dict[object, object], value)
            return {str(k): self._sanitize(v) for k, v in dict_value.items()}
        if isinstance(value, list):
            list_value = cast(list[object], value)
            return [self._sanitize(v) for v in list_value]
        if isinstance(value, tuple):
            tuple_value = cast(tuple[object, ...], value)
            return [self._sanitize(v) for v in tuple_value]
        return value


def setup_trace_logging(
    log_dir: Path | str = "logs",
    *,
    enabled: bool = True,
    trace_level: TraceLevel = "metadata",
    preview_chars: int = 200,
) -> TraceLogger:
    """Configure the global trace logger used by runtime components."""
    global _trace_logger
    _trace_logger = TraceLogger(
        log_dir=log_dir,
        enabled=enabled,
        trace_level=trace_level,
        preview_chars=preview_chars,
    )
    return _trace_logger


def get_trace_logger() -> TraceLogger | None:
    """Return the configured global trace logger, if any."""
    return _trace_logger


def log_event(event: str, run_id: str, **fields: Any) -> None:
    """Convenience wrapper that no-ops until trace logging is configured."""
    logger = get_trace_logger()
    if logger is not None:
        logger.log_event(event, run_id, **fields)
