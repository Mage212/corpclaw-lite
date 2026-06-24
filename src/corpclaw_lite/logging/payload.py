# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Raw LLM request/response payload capture (D-056 post-0.2.0).

A separate JSONL logger (``logs/llm_payloads.jsonl``) that records the full raw
request and response of each LLM call, filtered by an **allowlist of fields**.
This complements the metadata-only :mod:`trace` logger with the actual payloads
needed to (a) diagnose model behaviour under specific conditions and
(b) build a dataset of successful/unsuccessful agent trajectories for future
local-LLM fine-tuning.

Disabled by default (``capture_enabled: false``); opt-in via
``config/settings.yaml → logging``. Credential scrubbing is applied to every
string leaf via :func:`scrub_text` plus the :class:`CredentialScrubber` handler
filter (double-layer, same pattern as :mod:`trace`).

Field allowlist uses dot-notation paths::

    request.model
    request.messages        # full messages array (system + user + history)
    request.tools           # full tools schema
    request.params          # temperature/top_p/max_tokens/seed/...
    request.extra_body      # chat_template_kwargs, id_slot, cache_prompt, top_k...
    response.content        # raw content (pre-XML-parse)
    response.reasoning      # raw reasoning_content
    response.tool_calls     # parsed tool_calls
    response.usage          # token counts + backend timings
    response.finish_reason

Only allowlisted fields are written; unknown paths are ignored. The diagnostic
fields (``diagnostic.*``) are always captured on XML-parse failures regardless
of the allowlist, so the raw unparsed content is visible when the model returns
malformed output.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from corpclaw_lite.security.credential_scrubber import CredentialScrubber, scrub_text

__all__ = [
    "DEFAULT_CAPTURE_FIELDS",
    "PayloadCaptureLogger",
    "get_payload_logger",
    "setup_payload_logging",
]

# Module-level singleton (same pattern as trace.py).
_payload_logger: PayloadCaptureLogger | None = None

_logger = logging.getLogger(__name__)

# Default allowlist — full capture for diagnostics. Operators trim this list
# via config/settings.yaml → logging.capture_fields.
DEFAULT_CAPTURE_FIELDS: list[str] = [
    "request.model",
    "request.messages",
    "request.tools",
    "request.params",
    "request.extra_body",
    "response.content",
    "response.reasoning",
    "response.tool_calls",
    "response.usage",
    "response.finish_reason",
]

# Known request.* field paths (for validation/warning on unknown paths).
_REQUEST_FIELDS = frozenset(
    {"request.model", "request.messages", "request.tools", "request.params", "request.extra_body"}
)
# Known response.* field paths.
_RESPONSE_FIELDS = frozenset(
    {
        "response.content",
        "response.reasoning",
        "response.tool_calls",
        "response.usage",
        "response.finish_reason",
    }
)


class PayloadCaptureLogger:
    """Raw request/response payload logger, filtered by an allowlist of fields.

    Writes one JSONL record per ``capture()`` call to ``<log_dir>/llm_payloads.jsonl``.
    Each record carries: ``ts``, ``run_id``, ``phase``, ``finish_reason``,
    ``error``, the allowlisted ``request.*`` / ``response.*`` fields, and any
    ``diagnostic.*`` fields (always captured on errors).
    """

    def __init__(
        self,
        log_dir: Path | str = "logs",
        *,
        enabled: bool = False,
        fields: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        # Note: an explicitly empty list means "capture nothing" (allowlist is
        # empty); None means "use the defaults". Don't conflate [] with None.
        self._fields: set[str] = set(fields) if fields is not None else set(DEFAULT_CAPTURE_FIELDS)
        self._warned_unknown: set[str] = set()
        self._path = Path(log_dir) / "llm_payloads.jsonl"

        self._logger = logging.getLogger("llm_payloads")
        self._logger.handlers.clear()
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        if not enabled:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Payloads are heavier than trace events — larger rotation threshold.
        from logging.handlers import RotatingFileHandler

        handler = RotatingFileHandler(
            self._path,
            maxBytes=20 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.addFilter(CredentialScrubber())
        self._logger.addHandler(handler)

    def capture(
        self,
        run_id: str | None,
        *,
        phase: str,
        request: dict[str, Any] | None,
        response: dict[str, Any] | None,
        finish_reason: str | None = None,
        error: str | None = None,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        """Write one request+response record, filtered by the allowlist.

        Args:
            run_id: Agent run id (may be None for diagnostic-only captures).
            phase: Which provider method ("chat" / "chat_streamed" / "chat_with_image").
            request: The raw request summary dict (model, messages, tools, params,
                extra_body keys expected).
            response: The raw response summary dict (content, reasoning,
                tool_calls, usage, finish_reason keys expected).
            finish_reason: Top-level finish reason (also inside response if captured).
            error: Error message if the call failed (e.g. XML-parse failure code).
            diagnostic: Diagnostic fields (e.g. raw unparsed content on XML
                failures). **Always captured regardless of allowlist.**
        """
        if not self.enabled:
            return

        record: dict[str, Any] = {
            "ts": time.time(),
            "run_id": run_id,
            "phase": phase,
            "finish_reason": finish_reason,
            "error": error,
            "request": self._filter(request or {}, _REQUEST_FIELDS, prefix="request"),
            "response": self._filter(response or {}, _RESPONSE_FIELDS, prefix="response"),
        }
        if diagnostic:
            record["diagnostic"] = self._scrub_dict(diagnostic)

        self._logger.info(json.dumps(record, ensure_ascii=False, default=str))

    def _filter(
        self,
        data: dict[str, Any],
        known: frozenset[str],
        *,
        prefix: str,
    ) -> dict[str, Any]:
        """Copy only allowlisted fields from ``data``.

        ``data`` keys are bare (e.g. ``"messages"``); allowlist paths are
        prefixed (e.g. ``"request.messages"``). Only keys whose
        ``"{prefix}.{key}"`` is in the allowlist survive. Unknown allowlist
        paths (not in ``known``) are warned once and ignored.
        """
        result: dict[str, Any] = {}
        for key, value in data.items():
            path = f"{prefix}.{key}"
            if path in self._fields:
                result[key] = self._scrub_value(value)
            # else: field not in allowlist → skip.
        # Warn about allowlisted paths that don't map to a known field.
        for path in self._fields:
            if path.startswith(f"{prefix}.") and path not in known:
                field_name = path[len(prefix) + 1 :]
                if field_name not in data and path not in self._warned_unknown:
                    self._warned_unknown.add(path)
                    _logger.warning(
                        "capture_fields: unknown field '%s' — no such field in %s payload; ignored",
                        path,
                        prefix,
                    )
        return result

    def _scrub_value(self, value: Any) -> Any:
        """Recursively scrub credential patterns from strings (no truncation).

        Unlike trace._sanitize, payload capture does NOT truncate — the whole
        point is to see the full payload. Truncation would defeat the
        fine-tuning dataset use case.
        """
        if isinstance(value, str):
            return scrub_text(value)
        if isinstance(value, dict):
            return {str(k): self._scrub_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_value(v) for v in value]
        return value

    def _scrub_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        return {str(k): self._scrub_value(v) for k, v in data.items()}


def setup_payload_logging(
    log_dir: Path | str = "logs",
    *,
    enabled: bool = False,
    fields: list[str] | None = None,
) -> PayloadCaptureLogger:
    """Initialize the module-level payload logger singleton."""
    global _payload_logger
    _payload_logger = PayloadCaptureLogger(log_dir, enabled=enabled, fields=fields)
    return _payload_logger


def get_payload_logger() -> PayloadCaptureLogger | None:
    """Return the payload logger singleton, or None if not initialized."""
    return _payload_logger
