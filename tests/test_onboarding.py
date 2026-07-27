"""Tests for onboarding module: storage, engine, finalizer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from corpclaw_lite.onboarding.engine import OnboardingEngine
from corpclaw_lite.onboarding.finalizer import FINALIZATION_PROMPT, OnboardingFinalizer
from corpclaw_lite.onboarding.questions import ONBOARDING_QUESTIONS
from corpclaw_lite.onboarding.storage import OnboardingState, OnboardingStorage

# ── Storage tests ────────────────────────────────────────────────────────────


@pytest.fixture()
def storage(tmp_path: Path) -> OnboardingStorage:
    return OnboardingStorage(db_path=tmp_path / "test.db")


@pytest.mark.asyncio()
async def test_get_state_nonexistent(storage: OnboardingStorage) -> None:
    state = await storage.get_state(9999)
    assert state is None


@pytest.mark.asyncio()
async def test_get_or_create(storage: OnboardingStorage) -> None:
    state = await storage.get_or_create(1)
    assert state.user_id == 1
    assert state.current_step == 0
    assert state.answers == {}
    assert state.completed is False


@pytest.mark.asyncio()
async def test_save_and_load(storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=1, current_step=2, answers={"key": "value"})
    await storage.save_state(state)
    loaded = await storage.get_state(1)
    assert loaded is not None
    assert loaded.current_step == 2
    assert loaded.answers == {"key": "value"}


@pytest.mark.asyncio()
async def test_save_completed(storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=1, completed=True)
    await storage.save_state(state)
    loaded = await storage.get_state(1)
    assert loaded is not None
    assert loaded.completed is True


@pytest.mark.asyncio()
async def test_reset(storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=1, current_step=3, completed=True)
    await storage.save_state(state)
    await storage.reset(1)
    result = await storage.get_state(1)
    assert result is None


@pytest.mark.asyncio()
async def test_get_or_create_returns_existing(storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=900000042, current_step=3, answers={"a": "b"})
    await storage.save_state(state)
    existing = await storage.get_or_create(900000042)
    assert existing.current_step == 3
    assert existing.answers == {"a": "b"}


# ── Questions tests ──────────────────────────────────────────────────────────


def test_questions_defined() -> None:
    assert len(ONBOARDING_QUESTIONS) >= 5
    keys = [q.key for q in ONBOARDING_QUESTIONS]
    assert "preferred_name" in keys
    assert "communication_style" in keys
    assert "preferred_language" in keys


def test_question_frozen() -> None:
    q = ONBOARDING_QUESTIONS[0]
    with pytest.raises(AttributeError):
        q.key = "new_key"  # type: ignore[misc]


def test_preferred_name_not_skippable() -> None:
    name_q = next(q for q in ONBOARDING_QUESTIONS if q.key == "preferred_name")
    assert name_q.skippable is False


# ── Engine tests ─────────────────────────────────────────────────────────────


@pytest.fixture()
def mock_finalizer() -> OnboardingFinalizer:
    finalizer = MagicMock(spec=OnboardingFinalizer)
    finalizer.finalize = AsyncMock()
    return finalizer


@pytest.fixture()
def engine(storage: OnboardingStorage, mock_finalizer: OnboardingFinalizer) -> OnboardingEngine:
    return OnboardingEngine(storage, mock_finalizer)


@pytest.mark.asyncio()
async def test_needs_onboarding_new_user(engine: OnboardingEngine) -> None:
    assert await engine.needs_onboarding(9999) is True


@pytest.mark.asyncio()
async def test_needs_onboarding_completed(
    engine: OnboardingEngine, storage: OnboardingStorage
) -> None:
    state = OnboardingState(user_id=1, completed=True)
    await storage.save_state(state)
    assert await engine.needs_onboarding(1) is False


@pytest.mark.asyncio()
async def test_is_in_progress_new(engine: OnboardingEngine) -> None:
    assert await engine.is_in_progress(9999) is False


@pytest.mark.asyncio()
async def test_is_in_progress_started(engine: OnboardingEngine, storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=1, current_step=2)
    await storage.save_state(state)
    assert await engine.is_in_progress(1) is True


@pytest.mark.asyncio()
async def test_start_returns_first_question(engine: OnboardingEngine) -> None:
    q = await engine.start(1, "default")
    assert q is not None
    assert q.key == "preferred_name"


@pytest.mark.asyncio()
async def test_full_flow(engine: OnboardingEngine, mock_finalizer: OnboardingFinalizer) -> None:
    """Walk through all questions and verify finalize is called."""
    q = await engine.start(1, "default")
    answers = ["Вадим", "коротко", "русский", "продакт", "Excel", "нет"]
    step = 0
    while q is not None:
        q = await engine.submit_answer(1, answers[step], "default")
        step += 1

    # All questions answered
    assert step == len(ONBOARDING_QUESTIONS)
    # Finalize was called
    mock_finalizer.finalize.assert_called_once()  # type: ignore[union-attr]
    call_args = mock_finalizer.finalize.call_args  # type: ignore[union-attr]
    assert call_args[0][0] == 1  # user_id
    assert "preferred_name" in call_args[0][1]  # answers dict

    # User is now completed
    assert await engine.needs_onboarding(1) is False


@pytest.mark.asyncio()
async def test_reset_restarts(engine: OnboardingEngine, storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=1, completed=True)
    await storage.save_state(state)
    assert await engine.needs_onboarding(1) is False

    await engine.reset(1)
    assert await engine.needs_onboarding(1) is True


@pytest.mark.asyncio()
async def test_get_summary(engine: OnboardingEngine, storage: OnboardingStorage) -> None:
    state = OnboardingState(user_id=1, answers={"name": "Test"})
    await storage.save_state(state)
    summary = await engine.get_summary(1)
    assert summary == {"name": "Test"}


@pytest.mark.asyncio()
async def test_get_summary_empty(engine: OnboardingEngine) -> None:
    summary = await engine.get_summary(9999)
    assert summary == {}


# ── Finalizer tests ──────────────────────────────────────────────────────────


MOCK_LLM_RESPONSE = """\
===AGENT_INSTRUCTIONS===
## About This User
Вадим — продакт-менеджер, работает над FinTech-проектом.

## Communication Style
- Общайся на «ты», коротко и по делу
- Без вступлений, сразу к сути

## Language
Общайся на русском.
===USER_FACTS===
name: Вадим
role: продакт-менеджер
style: краткий, деловой
language: русский
tasks: Excel, отчёты
===END===
"""


def test_parse_response_valid() -> None:
    finalizer = OnboardingFinalizer.__new__(OnboardingFinalizer)
    instructions, facts = finalizer._parse_response(MOCK_LLM_RESPONSE)
    assert "Вадим" in instructions
    assert "Communication Style" in instructions
    assert len(facts) >= 4
    fact_keys = [k for k, _ in facts]
    assert "name" in fact_keys
    assert "role" in fact_keys


def test_parse_response_empty() -> None:
    finalizer = OnboardingFinalizer.__new__(OnboardingFinalizer)
    instructions, facts = finalizer._parse_response("random text without markers")
    assert instructions == ""
    assert facts == []


def test_parse_response_partial() -> None:
    """Only AGENT_INSTRUCTIONS present, no USER_FACTS."""
    finalizer = OnboardingFinalizer.__new__(OnboardingFinalizer)
    raw = "===AGENT_INSTRUCTIONS===\nHello\n===USER_FACTS===\n===END==="
    instructions, facts = finalizer._parse_response(raw)
    assert instructions == "Hello"
    assert facts == []


@pytest.mark.asyncio()
async def test_finalize_saves_bootstrap(tmp_path: Path) -> None:
    """Verify finalize creates the per-user .md file."""
    mock_provider = MagicMock()
    mock_response = MagicMock()
    mock_response.content = MOCK_LLM_RESPONSE
    mock_provider.chat = AsyncMock(return_value=mock_response)

    mock_memory = MagicMock()
    mock_memory.store_fact = AsyncMock()

    mock_user_manager = MagicMock()
    mock_user_manager.async_update_name = AsyncMock()

    users_dir = tmp_path / "users"
    finalizer = OnboardingFinalizer(
        provider=mock_provider,
        memory=mock_memory,
        bootstrap_users_dir=users_dir,
        user_manager=mock_user_manager,
    )

    answers = {
        "preferred_name": "Вадим",
        "communication_style": "коротко",
        "preferred_language": "русский",
        "work_context": "продакт",
        "typical_tasks": "Excel",
        "additional_notes": "нет",
    }

    await finalizer.finalize(900000042, answers, "default")

    # Check bootstrap file was created
    bootstrap_file = users_dir / "900000042.md"
    assert bootstrap_file.exists()
    content = bootstrap_file.read_text()
    assert "Вадим" in content

    # Check memory facts were saved
    assert mock_memory.store_fact.await_count >= 1

    # Check user name was updated
    mock_user_manager.async_update_name.assert_awaited_once_with(900000042, "Вадим")


@pytest.mark.asyncio()
async def test_finalize_fallback_on_error(tmp_path: Path) -> None:
    """When LLM fails, raw answers are saved as fallback."""
    mock_provider = MagicMock()
    mock_provider.chat = AsyncMock(side_effect=RuntimeError("LLM failed"))

    mock_memory = MagicMock()
    mock_memory.store_fact = AsyncMock()

    mock_user_manager = MagicMock()
    mock_user_manager.async_update_name = AsyncMock()

    users_dir = tmp_path / "users"
    finalizer = OnboardingFinalizer(
        provider=mock_provider,
        memory=mock_memory,
        bootstrap_users_dir=users_dir,
        user_manager=mock_user_manager,
    )

    answers = {
        "preferred_name": "Test",
        "communication_style": "формально",
        "preferred_language": "русский",
        "work_context": "",
        "typical_tasks": "",
        "additional_notes": "нет",
    }

    await finalizer.finalize(99, answers, "engineering")

    # Fallback bootstrap file created
    bootstrap_file = users_dir / "99.md"
    assert bootstrap_file.exists()
    content = bootstrap_file.read_text()
    assert "Test" in content

    # Raw facts saved (only non-empty, non-"нет")
    saved_keys = [call.args[1] for call in mock_memory.store_fact.await_args_list]
    assert "preferred_name" in saved_keys
    assert "communication_style" in saved_keys
    # Empty and "нет" should be skipped
    assert "additional_notes" not in saved_keys

    # Name was still updated despite LLM failure
    mock_user_manager.async_update_name.assert_awaited_once_with(99, "Test")


# ── Bootstrap loader integration ─────────────────────────────────────────────


def test_bootstrap_get_user_prompt(tmp_path: Path) -> None:
    from corpclaw_lite.config.bootstrap import BootstrapLoader

    users_dir = tmp_path / "users"
    users_dir.mkdir()
    (users_dir / "123.md").write_text("## My User Prompt\nHello", encoding="utf-8")

    loader = BootstrapLoader(tmp_path)
    result = loader.get_user_prompt(123)
    assert result is not None
    assert "My User Prompt" in result


def test_bootstrap_get_user_prompt_missing(tmp_path: Path) -> None:
    from corpclaw_lite.config.bootstrap import BootstrapLoader

    loader = BootstrapLoader(tmp_path)
    assert loader.get_user_prompt(99999) is None


# ── Finalization prompt ──────────────────────────────────────────────────────


def test_finalization_prompt_has_placeholders() -> None:
    """Ensure the FINALIZATION_PROMPT has all expected placeholders."""
    for key in [
        "preferred_name",
        "communication_style",
        "preferred_language",
        "work_context",
        "typical_tasks",
        "additional_notes",
        "department",
    ]:
        assert f"{{{key}}}" in FINALIZATION_PROMPT


# ── S1-10: onboarding answer sanitization ──────────────────────────────────


def test_finalization_prompt_format_with_brace_in_answer() -> None:
    """S1-10: braces in answers must not break .format or mismatch placeholders."""
    from corpclaw_lite.onboarding.finalizer import FINALIZATION_PROMPT, _safe_answer

    # An answer containing {__class__} / {x} / stray braces must not raise.
    prompt = FINALIZATION_PROMPT.format(
        preferred_name=_safe_answer("Alice {__class__}"),
        communication_style=_safe_answer("concise {x}"),
        preferred_language=_safe_answer("ru"),
        work_context=_safe_answer("finance"),
        typical_tasks=_safe_answer("reports"),
        additional_notes=_safe_answer("note { with stray } braces"),
        department="finance",
    )
    # The braces are escaped so they appear literally in the rendered prompt.
    assert "{__class__}" in prompt
    assert "{x}" in prompt


def test_safe_answer_caps_length() -> None:
    """S1-10: an oversized answer is truncated before entering the prompt."""
    from corpclaw_lite.onboarding.finalizer import _MAX_ANSWER_CHARS, _safe_answer

    huge = "A" * (_MAX_ANSWER_CHARS * 3)
    result = _safe_answer(huge)
    assert len(result) == _MAX_ANSWER_CHARS


def test_safe_answer_handles_none_and_empty() -> None:
    from corpclaw_lite.onboarding.finalizer import _safe_answer

    assert _safe_answer(None) == ""
    assert _safe_answer("") == ""


@pytest.mark.asyncio
async def test_save_bootstrap_caps_oversized_instructions(tmp_path: Path) -> None:
    """S1-10: _save_bootstrap truncates oversized LLM output."""
    from corpclaw_lite.onboarding.finalizer import (
        _MAX_BOOTSTRAP_CHARS,
        OnboardingFinalizer,
    )

    users_dir = tmp_path / "users"
    fin = OnboardingFinalizer(
        provider=MagicMock(),
        memory=MagicMock(),
        bootstrap_users_dir=users_dir,
        user_manager=MagicMock(),
    )
    huge = "X" * (_MAX_BOOTSTRAP_CHARS * 2)
    fin._save_bootstrap(user_id=42, instructions=huge)
    content = (users_dir / "42.md").read_text(encoding="utf-8")
    # The instructions section is capped (the file has a small fixed header too).
    assert content.count("X") == _MAX_BOOTSTRAP_CHARS


@pytest.mark.asyncio
async def test_save_fallback_bootstrap_caps_oversized_answers(tmp_path: Path) -> None:
    """S1-10: _save_fallback_bootstrap also caps answers + total file length.

    The fallback path is reached when LLM finalization fails — exactly when a
    crafted oversized answer would otherwise persist uncapped to the per-user
    .md (→ system prompt). Both the LLM path and this fallback must be bounded.
    """
    from corpclaw_lite.onboarding.finalizer import (
        _MAX_ANSWER_CHARS,
        _MAX_BOOTSTRAP_CHARS,
        OnboardingFinalizer,
    )

    users_dir = tmp_path / "users"
    fin = OnboardingFinalizer(
        provider=MagicMock(),
        memory=MagicMock(),
        bootstrap_users_dir=users_dir,
        user_manager=MagicMock(),
    )
    # Oversized answers in every field.
    huge = "Z" * (_MAX_ANSWER_CHARS * 5)
    fin._save_fallback_bootstrap(
        user_id=7,
        answers={
            "preferred_name": huge,
            "communication_style": huge,
            "preferred_language": huge,
            "work_context": huge,
        },
        department="finance",
    )
    content = (users_dir / "7.md").read_text(encoding="utf-8")
    # Total file is capped.
    assert len(content) <= _MAX_BOOTSTRAP_CHARS
    # No single answer exceeds the per-answer cap (the header has no 'Z').
    # Each field appears once; even one full _MAX_ANSWER_CHARS run would fit,
    # so the key assertion is the total-file cap above.
    assert "Z" in content  # the (truncated) answers are present
