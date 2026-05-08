from __future__ import annotations

import json
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_trace_logger_writes_jsonl_and_scrubs(tmp_path: Path) -> None:
    from corpclaw_lite.logging.trace import setup_trace_logging

    logger = setup_trace_logging(tmp_path, enabled=True, preview_chars=80)
    secret = "sk-" + "a" * 25
    logger.log_event("request_started", "run1", message_preview=f"use {secret}")

    records = _read_jsonl(tmp_path / "agent_trace.jsonl")
    assert records[0]["event"] == "request_started"
    assert records[0]["run_id"] == "run1"
    assert secret not in json.dumps(records[0])
    assert "***REDACTED***" in records[0]["message_preview"]

    setup_trace_logging(tmp_path, enabled=False)


def test_trace_logger_metadata_truncates_text(tmp_path: Path) -> None:
    from corpclaw_lite.logging.trace import setup_trace_logging

    logger = setup_trace_logging(tmp_path, enabled=True, preview_chars=20)
    logger.log_event("tool_call_finished", "run2", result_preview="x" * 200)

    records = _read_jsonl(tmp_path / "agent_trace.jsonl")
    assert records[0]["result_preview"] == "x" * 20

    setup_trace_logging(tmp_path, enabled=False)
