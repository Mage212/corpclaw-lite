"""Tests for B-056: PlanningTextGuard — planning-text / tool-artifact detection.

Local LLMs sometimes emit a statement of intent ("Let me now search the
document...") as a *final* answer instead of taking action, or emit a
Qwen3/Gemma-specific tool-call artifact like ``[tool:query_specific_file]`` as
plain text. The guard detects both and lets the caller inject a bounded number
of corrections. Reference: GAIA base/agent.py:3506-3671.
"""

from __future__ import annotations

from corpclaw_lite.agent.guards import (
    PlanningTextGuard,
    PlanningTextGuardConfig,
)


def _guard(
    *,
    enabled: bool = True,
    max_length: int = 500,
    max_corrections: int = 2,
) -> PlanningTextGuard:
    return PlanningTextGuard(
        PlanningTextGuardConfig(
            enabled=enabled,
            max_length=max_length,
            max_corrections=max_corrections,
        )
    )


# ── planning-text phrases (EN) ────────────────────────────────────────────────


def test_detect_planning_phrase_en() -> None:
    guard = _guard()
    assert guard.detect("Let me now search the document for the answer.") is True


def test_detect_planning_phrase_en_lowercase() -> None:
    guard = _guard()
    assert guard.detect("i'll check that for you") is True


def test_detect_planning_phrase_en_nested() -> None:
    """The phrase can appear anywhere in a short answer."""
    guard = _guard()
    assert guard.detect("Great question! Now I will look into this.") is True


# ── planning-text phrases (RU) ────────────────────────────────────────────────


def test_detect_planning_phrase_ru() -> None:
    guard = _guard()
    assert guard.detect("Сейчас я проверю документ и вернусь к вам.") is True


def test_detect_planning_phrase_ru_lowercase() -> None:
    guard = _guard()
    assert guard.detect("я посмотрю этот файл сейчас") is True


def test_detect_planning_phrase_ru_formal() -> None:
    guard = _guard()
    assert guard.detect("Позвольте мне изучить этот вопрос подробнее.") is True


# ── tool-artifact (Qwen3/Gemma bug) ───────────────────────────────────────────


def test_detect_tool_artifact_basic() -> None:
    guard = _guard()
    assert guard.detect("[tool:query_specific_file]") is True


def test_detect_tool_artifact_with_whitespace() -> None:
    guard = _guard()
    assert guard.detect("  [tool:excel_inspect]  ") is True


def test_detect_tool_artifact_not_triggered_by_inline_brackets() -> None:
    """A legitimate answer containing [tool:...] inline (not as the whole text)
    is not a tool-artifact."""
    guard = _guard()
    assert guard.detect("The result of [tool:search] was 42 items.") is False


# ── neutrality ────────────────────────────────────────────────────────────────


def test_detect_neutral_when_disabled() -> None:
    guard = _guard(enabled=False)
    assert guard.detect("Let me now check the file.") is False
    assert guard.detect("[tool:excel_inspect]") is False


def test_detect_neutral_long_legitimate_answer() -> None:
    """A long answer (>max_length) that starts with 'let me' is not blocked."""
    guard = _guard(max_length=500)
    long_answer = "Let me explain the full process. " + ("detail. " * 200)
    assert len(long_answer) >= 500
    assert guard.detect(long_answer) is False


def test_detect_neutral_normal_answer() -> None:
    guard = _guard()
    assert guard.detect("The answer is 42.") is False
    assert guard.detect("Report complete. See the attached file.") is False


def test_detect_neutral_after_max_corrections() -> None:
    """Once max_corrections is reached, the guard goes neutral."""
    guard = _guard(max_corrections=2)
    assert guard.detect("Let me look at that.") is True
    guard.note_correction()
    assert guard.detect("Let me look at that.") is True
    guard.note_correction()
    # Two corrections used → neutral even on a fresh planning-text answer.
    assert guard.detect("I'll now look into it.") is False
    assert guard.detect("[tool:search]") is False


def test_tool_artifact_still_blocked_after_max_corrections() -> None:
    """The max_corrections cap applies to tool-artifacts too — once reached,
    the guard is fully neutral (does not loop the model forever)."""
    guard = _guard(max_corrections=1)
    assert guard.detect("[tool:x]") is True
    guard.note_correction()
    assert guard.detect("[tool:x]") is False


# ── correction message ────────────────────────────────────────────────────────


def test_correction_message_non_empty() -> None:
    guard = _guard()
    msg = guard.correction_message()
    assert isinstance(msg, str)
    assert len(msg) > 20
    assert "final answer" in msg.lower() or "do it" in msg.lower()


# ── counter / reset ───────────────────────────────────────────────────────────


def test_corrections_used_tracks_count() -> None:
    guard = _guard()
    assert guard.corrections_used == 0
    guard.note_correction()
    assert guard.corrections_used == 1
    guard.note_correction()
    assert guard.corrections_used == 2


def test_reset_clears_counter() -> None:
    guard = _guard()
    guard.note_correction()
    guard.note_correction()
    assert guard.corrections_used == 2
    guard.reset()
    assert guard.corrections_used == 0
    # After reset the guard is active again.
    assert guard.detect("Let me look at that.") is True


def test_default_config_used_when_none() -> None:
    guard = PlanningTextGuard()
    assert guard.config.enabled is True
    assert guard.config.max_length == 500
    assert guard.config.max_corrections == 2
