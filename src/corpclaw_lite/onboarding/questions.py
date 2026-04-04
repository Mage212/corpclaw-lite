"""Onboarding question definitions.

Each question is a frozen dataclass with a storage key, user-facing prompt,
optional hint, and optional department filter.  The list is iterated by
``OnboardingEngine`` in order; department-filtered questions are skipped
when they don't match the user's department.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ONBOARDING_QUESTIONS",
    "OnboardingQuestion",
]


@dataclass(frozen=True)
class OnboardingQuestion:
    """A single onboarding question."""

    key: str
    """Storage key for the raw answer (used in ``OnboardingState.answers``)."""

    prompt: str
    """Question text shown to the user."""

    hint: str = ""
    """Optional hint / examples shown below the question."""

    department_filter: str | None = None
    """If set, the question is only asked for users in this department."""

    skippable: bool = True
    """Whether the user can skip this question."""


ONBOARDING_QUESTIONS: list[OnboardingQuestion] = [
    OnboardingQuestion(
        key="preferred_name",
        prompt="👋 Как тебя называть?",
        hint="Имя или ник, как тебе удобнее",
        skippable=False,
    ),
    OnboardingQuestion(
        key="communication_style",
        prompt="🗣 В какой манере с тобой общаться?",
        hint="Формально / неформально / коротко и по делу / дружелюбно",
    ),
    OnboardingQuestion(
        key="preferred_language",
        prompt="🌐 На каком языке предпочитаешь общаться?",
        hint="Русский, English, или другой",
    ),
    OnboardingQuestion(
        key="work_context",
        prompt="📝 Расскажи коротко о себе: роль, проекты, чем занимаешься?",
        hint="Например: «Маркетолог, веду соцсети и email-рассылки»",
    ),
    OnboardingQuestion(
        key="typical_tasks",
        prompt="🔧 Какие задачи чаще всего хочешь решать со мной?",
        hint="Анализ файлов, переводы, генерация текста, поиск информации...",
    ),
    OnboardingQuestion(
        key="additional_notes",
        prompt="⚙️ Есть что-то ещё, что мне стоит знать?",
        hint="Любые предпочтения. Напиши «нет» чтобы пропустить",
    ),
]
