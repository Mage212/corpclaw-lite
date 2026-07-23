"""Tests for raw LLM request/response payload capture (D-056 post-0.2.0).

Covers:
  - Allowlist field filtering (request.*/response.* paths)
  - Credential scrubbing (sk-*, Bearer tokens) on captured strings
  - No truncation (full payloads preserved — unlike trace._sanitize)
  - Disabled-by-default (no file written, capture() is no-op)
  - Diagnostic capture on XML-parse failures (always captured regardless of allowlist)
  - Unknown allowlist fields warn-once + ignored
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from corpclaw_lite.logging.payload import (
    DEFAULT_CAPTURE_FIELDS,
    PayloadCaptureLogger,
    setup_payload_logging,
)


@pytest.fixture
def capture_logger(tmp_path: Path) -> PayloadCaptureLogger:
    """A payload logger writing to tmp_path, enabled, full default allowlist."""
    return setup_payload_logging(
        log_dir=tmp_path,
        enabled=True,
        fields=list(DEFAULT_CAPTURE_FIELDS),
    )


def _read_records(tmp_path: Path) -> list[dict[str, Any]]:
    """Read all JSONL records from llm_payloads.jsonl in tmp_path."""
    payload_file = tmp_path / "llm_payloads.jsonl"
    if not payload_file.exists():
        return []
    return [
        json.loads(line)
        for line in payload_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── Disabled-by-default ───────────────────────────────────────────────────────


def test_disabled_logger_writes_nothing(tmp_path: Path) -> None:
    """When enabled=False, capture() is a no-op and no file is created."""
    logger = setup_payload_logging(log_dir=tmp_path, enabled=False)
    assert logger.enabled is False
    logger.capture(
        run_id="r1",
        phase="chat",
        request={"model": "test"},
        response={"content": "hello"},
        finish_reason="stop",
    )
    assert not (tmp_path / "llm_payloads.jsonl").exists()


# ── Allowlist field filtering ─────────────────────────────────────────────────


def test_allowlist_filters_request_fields(tmp_path: Path) -> None:
    """Only allowlisted request.* fields are captured."""
    logger = setup_payload_logging(
        log_dir=tmp_path,
        enabled=True,
        fields=["request.model", "request.extra_body"],
    )
    logger.capture(
        run_id="r1",
        phase="chat",
        request={
            "model": "gemma4",
            "messages": [{"role": "user", "content": "secret question"}],
            "tools": [{"type": "function"}],
            "params": {"temperature": 0.5},
            "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
        },
        response={"content": "answer", "finish_reason": "stop"},
        finish_reason="stop",
    )
    records = _read_records(tmp_path)
    assert len(records) == 1
    req = records[0]["request"]
    assert req["model"] == "gemma4"
    assert req["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    # messages/tools/params NOT in allowlist → absent.
    assert "messages" not in req
    assert "tools" not in req
    assert "params" not in req
    # No response fields allowlisted → empty response dict.
    assert records[0]["response"] == {}


def test_allowlist_filters_response_fields(tmp_path: Path) -> None:
    """Only allowlisted response.* fields are captured."""
    logger = setup_payload_logging(
        log_dir=tmp_path,
        enabled=True,
        fields=["response.content", "response.finish_reason"],
    )
    logger.capture(
        run_id="r1",
        phase="chat",
        request={"model": "test"},
        response={
            "content": "the answer",
            "reasoning": "hidden reasoning",
            "tool_calls": [{"name": "foo"}],
            "usage": {"input_tokens": 10},
            "finish_reason": "stop",
        },
        finish_reason="stop",
    )
    records = _read_records(tmp_path)
    resp = records[0]["response"]
    assert resp["content"] == "the answer"
    assert resp["finish_reason"] == "stop"
    # reasoning/tool_calls/usage NOT in allowlist.
    assert "reasoning" not in resp
    assert "tool_calls" not in resp
    assert "usage" not in resp


# ── No truncation ─────────────────────────────────────────────────────────────


def test_no_truncation_full_payload_preserved(tmp_path: Path) -> None:
    """Unlike trace._sanitize, payloads are NOT truncated — full content kept."""
    logger = setup_payload_logging(log_dir=tmp_path, enabled=True, fields=["response.content"])
    long_content = "x" * 10000  # would be truncated to 200 by trace
    logger.capture(
        run_id="r1",
        phase="chat",
        request={},
        response={"content": long_content},
        finish_reason="stop",
    )
    records = _read_records(tmp_path)
    assert records[0]["response"]["content"] == long_content
    assert len(records[0]["response"]["content"]) == 10000


# ── Credential scrubbing ──────────────────────────────────────────────────────


def test_credential_scrubbing_on_captured_strings(tmp_path: Path) -> None:
    """sk-* API keys (20+ chars) and Bearer tokens are redacted in captured strings."""
    logger = setup_payload_logging(
        log_dir=tmp_path, enabled=True, fields=["request.messages", "response.content"]
    )
    # sk- requires 20+ alphanumeric chars to match the scrubber pattern.
    long_key = "sk-" + "a" * 24
    logger.capture(
        run_id="r1",
        phase="chat",
        request={
            "messages": [{"role": "user", "content": f"my key is {long_key}"}],
        },
        response={"content": "Bearer eyJhbGciOiJIUzI1 token here"},
        finish_reason="stop",
    )
    records = _read_records(tmp_path)
    msg_content = records[0]["request"]["messages"][0]["content"]
    assert long_key not in msg_content
    resp_content = records[0]["response"]["content"]
    assert "Bearer eyJhbGciOiJIUzI1" not in resp_content


def test_scrubbing_nested_in_dicts_and_lists(tmp_path: Path) -> None:
    """Scrubbing recurses into nested dict/list structures."""
    logger = setup_payload_logging(log_dir=tmp_path, enabled=True, fields=["request.extra_body"])
    long_key = "sk-" + "b" * 24
    logger.capture(
        run_id="r1",
        phase="chat",
        request={
            "extra_body": {
                "nested": {"secret": long_key},
                "items": ["Bearer token123", "safe text"],
            },
        },
        response=None,
        finish_reason="stop",
    )
    records = _read_records(tmp_path)
    eb = records[0]["request"]["extra_body"]
    assert long_key not in str(eb["nested"])
    assert "Bearer token123" not in str(eb["items"])


# ── Diagnostic capture (XML failures) ─────────────────────────────────────────


def test_diagnostic_always_captured_regardless_of_allowlist(tmp_path: Path) -> None:
    """Diagnostic fields are captured even when not in the allowlist."""
    logger = setup_payload_logging(
        log_dir=tmp_path,
        enabled=True,
        fields=[],  # empty allowlist
    )
    logger.capture(
        run_id="r1",
        phase="xml_parse_failure",
        request={"model": "test"},
        response={"content": "parsed"},
        finish_reason="stop",
        error="malformed_xml",
        diagnostic={
            "raw_unparsed_content": "<tool_call><name>foo</name><arguments>bad",
            "parse_error_message": "No valid block found",
        },
    )
    records = _read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["error"] == "malformed_xml"
    diag = records[0]["diagnostic"]
    assert "raw_unparsed_content" in diag
    assert "tool_call" in diag["raw_unparsed_content"]
    # request/response empty (allowlist is empty), but diagnostic present.
    assert records[0]["request"] == {}
    assert records[0]["response"] == {}


# ── Metadata fields ───────────────────────────────────────────────────────────


def test_record_metadata_fields(tmp_path: Path) -> None:
    """Each record carries ts, run_id, phase, finish_reason, error."""
    logger = setup_payload_logging(log_dir=tmp_path, enabled=True, fields=[])
    logger.capture(
        run_id="run-42",
        phase="chat_streamed",
        request={},
        response=None,
        finish_reason="tool_calls",
        error=None,
    )
    records = _read_records(tmp_path)
    assert records[0]["run_id"] == "run-42"
    assert records[0]["phase"] == "chat_streamed"
    assert records[0]["finish_reason"] == "tool_calls"
    assert "ts" in records[0]


def test_run_id_can_be_none(tmp_path: Path) -> None:
    """run_id is optional (None for diagnostic captures outside a run)."""
    logger = setup_payload_logging(log_dir=tmp_path, enabled=True, fields=[])
    logger.capture(
        run_id=None,
        phase="chat",
        request={},
        response=None,
        finish_reason="stop",
    )
    records = _read_records(tmp_path)
    assert records[0]["run_id"] is None


# ── Unknown allowlist fields ──────────────────────────────────────────────────


def test_unknown_allowlist_field_warned_and_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unknown allowlist paths (not in known request/response fields) warn once."""
    logger = setup_payload_logging(
        log_dir=tmp_path,
        enabled=True,
        fields=["request.model", "request.nonexistent_field"],
    )
    with caplog.at_level("WARNING"):
        logger.capture(
            run_id="r1",
            phase="chat",
            request={"model": "test"},
            response=None,
            finish_reason="stop",
        )
    records = _read_records(tmp_path)
    # model captured, nonexistent_field absent.
    assert records[0]["request"]["model"] == "test"
    # Warning emitted.
    assert any("nonexistent_field" in rec.message for rec in caplog.records)
