"""Onboarding flow controller.

Drives the deterministic question-by-question onboarding flow.
No LLM calls happen here — the LLM is only used at finalization
(see :class:`OnboardingFinalizer`).
"""

from __future__ import annotations

import logging

from corpclaw_lite.onboarding.finalizer import OnboardingFinalizer
from corpclaw_lite.onboarding.questions import ONBOARDING_QUESTIONS, OnboardingQuestion
from corpclaw_lite.onboarding.storage import OnboardingStorage

__all__ = [
    "OnboardingEngine",
]

logger = logging.getLogger(__name__)


class OnboardingEngine:
    """Drives the onboarding questionnaire flow.

    Deterministic question-by-question flow.  No LLM calls during collection.
    LLM is only used once at finalization (see :class:`OnboardingFinalizer`).
    """

    def __init__(
        self,
        storage: OnboardingStorage,
        finalizer: OnboardingFinalizer,
    ) -> None:
        self._storage = storage
        self._finalizer = finalizer

    async def needs_onboarding(self, user_id: int) -> bool:
        """Return True if user hasn't completed onboarding yet."""
        state = await self._storage.get_state(user_id)
        if state is None:
            return True
        return not state.completed

    async def is_in_progress(self, user_id: int) -> bool:
        """Return True if onboarding has been started but not completed.

        A state record is created by :meth:`start` (via ``get_or_create``) the
        moment the first question is sent to the user.  At that point
        ``current_step`` is still 0 because the user hasn't answered yet —
        the step is incremented only after :meth:`submit_answer` is called.
        Therefore we must NOT check ``current_step > 0`` here: any existing
        non-completed record means the flow is in progress and the next
        message should be treated as an answer, not a fresh start.
        """
        state = await self._storage.get_state(user_id)
        if state is None:
            return False
        return not state.completed

    async def start(self, user_id: int, department: str) -> OnboardingQuestion | None:
        """Start onboarding and return the first question.

        If the user already has state, reuses it (idempotent).
        """
        state = await self._storage.get_or_create(user_id)
        questions = self._get_questions(department)
        if state.current_step < len(questions):
            return questions[state.current_step]
        return None

    async def submit_answer(
        self,
        user_id: int,
        answer: str,
        department: str,
    ) -> OnboardingQuestion | None:
        """Process answer, save, advance to next question.

        Returns the next question, or None if onboarding is complete
        (finalization has been triggered).
        """
        state = await self._storage.get_or_create(user_id)
        questions = self._get_questions(department)

        if state.current_step >= len(questions):
            return None

        current = questions[state.current_step]
        state.answers[current.key] = answer.strip()
        state.current_step += 1

        if state.current_step >= len(questions):
            # All questions answered — finalize with LLM
            state.completed = True
            await self._storage.save_state(state)
            logger.info("Onboarding complete for user %d, starting finalization", user_id)
            await self._finalizer.finalize(user_id, state.answers, department)
            return None

        await self._storage.save_state(state)
        return questions[state.current_step]

    async def reset(self, user_id: int) -> None:
        """Reset onboarding for re-setup via /setup."""
        await self._storage.reset(user_id)
        logger.info("Onboarding reset for user %d", user_id)

    async def get_summary(self, user_id: int) -> dict[str, str]:
        """Return raw answers collected so far (for display after completion)."""
        state = await self._storage.get_state(user_id)
        return state.answers if state else {}

    @staticmethod
    def _get_questions(department: str) -> list[OnboardingQuestion]:
        """Filter questions relevant to the user's department."""
        return [
            q
            for q in ONBOARDING_QUESTIONS
            if q.department_filter is None or q.department_filter == department
        ]
