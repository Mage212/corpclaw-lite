"""LLM-powered onboarding finalization.

After all onboarding questions are answered, this module makes a single LLM
call to convert raw user answers into:

1. **Per-user bootstrap** ``config/bootstrap/users/{telegram_id}.md`` —
   actionable agent instructions written *by* the LLM *for* the LLM.
2. **Structured memory facts** — key-value pairs saved to ``memory_facts``
   for runtime recall via ``memory_recall`` tool.

If the LLM call fails, a deterministic fallback saves raw answers directly.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from corpclaw_lite.llm.base import Provider
from corpclaw_lite.memory.sqlite import SQLiteMemory

if TYPE_CHECKING:
    from corpclaw_lite.users.manager import UserManager

__all__ = [
    "FINALIZATION_PROMPT",
    "OnboardingFinalizer",
]

logger = logging.getLogger(__name__)

FINALIZATION_PROMPT = """\
You are configuring a corporate AI assistant for a new user.
Below are the user's raw answers to onboarding questions.

Your task is to generate TWO outputs:

## Output 1: AGENT_INSTRUCTIONS
Write a markdown document with instructions for yourself (the AI assistant) about \
HOW to interact with this user.
Write in second person imperative ("Общайся...", "Используй...", "Отвечай...").
This will become part of your system prompt for ALL future conversations with this user.

Sections to include (skip a section if the relevant answer is empty, "нет", or a clear skip):
- **About This User** — role, work context (1-2 concise sentences)
- **Communication Style** — concrete, actionable rules for tone and format
- **Language** — which language to use
- **Typical Tasks** — what to expect, proactive suggestions
- **Special Notes** — only if meaningful preferences were given

Be concise. Every sentence should be an actionable instruction, not a description.
Write the instructions in the same language as the user's answers.

## Output 2: USER_FACTS
Generate 4-8 structured key-value facts about this user.
Format: one fact per line, as "key: value"
Keys should be short lowercase English identifiers (e.g. name, role, style, language).
Values should be concise and informative, in the user's language.
Skip facts where the user gave no meaningful answer.

---

User's raw answers:
- Preferred name: {preferred_name}
- Communication style: {communication_style}
- Preferred language: {preferred_language}
- Work context: {work_context}
- Typical tasks: {typical_tasks}
- Additional notes: {additional_notes}
- Department: {department}

---

Respond in EXACTLY this format:

===AGENT_INSTRUCTIONS===
(markdown content for per-user system prompt)
===USER_FACTS===
(key: value lines)
===END===\
"""


class OnboardingFinalizer:
    """Uses a single LLM call to convert raw onboarding answers into
    actionable agent instructions and structured memory facts.
    """

    def __init__(
        self,
        provider: Provider,
        memory: SQLiteMemory,
        bootstrap_users_dir: Path,
        user_manager: UserManager,
    ) -> None:
        self._provider = provider
        self._memory = memory
        self._users_dir = bootstrap_users_dir
        self._user_manager = user_manager

    async def finalize(
        self,
        user_id: int,
        answers: dict[str, str],
        department: str,
    ) -> None:
        """Run LLM finalization and persist all results.

        Steps:
        1. Call LLM with finalization prompt
        2. Parse structured response
        3. Save per-user bootstrap .md
        4. Save structured facts to memory_facts
        5. Update user.name in DB
        """
        # Always update user name, regardless of LLM success
        preferred_name = answers.get("preferred_name", "").strip()
        if preferred_name:
            try:
                await self._user_manager.async_update_name(user_id, preferred_name)
                logger.info("Updated user name for %d: %s", user_id, preferred_name)
            except Exception as e:
                logger.warning("Failed to update user name for %d: %s", user_id, e)

        prompt = FINALIZATION_PROMPT.format(
            preferred_name=answers.get("preferred_name", ""),
            communication_style=answers.get("communication_style", ""),
            preferred_language=answers.get("preferred_language", ""),
            work_context=answers.get("work_context", ""),
            typical_tasks=answers.get("typical_tasks", ""),
            additional_notes=answers.get("additional_notes", ""),
            department=department,
        )

        try:
            response = await self._provider.chat(
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content

            instructions, facts = self._parse_response(raw)

            if instructions:
                self._save_bootstrap(user_id, instructions)
            else:
                logger.warning(
                    "LLM returned no AGENT_INSTRUCTIONS for user %d, using fallback", user_id
                )
                self._save_fallback_bootstrap(user_id, answers, department)

            # Save LLM-structured facts
            await self._save_facts(user_id, facts)

            # Always save preferred_name as explicit fact (user's exact choice)
            if preferred_name:
                await self._memory.store_fact(str(user_id), "name", preferred_name)

            logger.info(
                "Onboarding finalized for user %d: %d facts, bootstrap saved",
                user_id,
                len(facts),
            )
        except Exception as e:
            logger.error("Onboarding LLM finalization failed for user %d: %s", user_id, e)
            await self._fallback_save(user_id, answers, department)

    def _parse_response(self, raw: str) -> tuple[str, list[tuple[str, str]]]:
        """Parse the LLM response into instructions and facts."""
        instructions = ""
        facts: list[tuple[str, str]] = []

        # Extract AGENT_INSTRUCTIONS block
        instr_match = re.search(
            r"===AGENT_INSTRUCTIONS===\s*\n(.*?)===USER_FACTS===",
            raw,
            re.DOTALL,
        )
        if instr_match:
            instructions = instr_match.group(1).strip()

        # Extract USER_FACTS block
        facts_match = re.search(
            r"===USER_FACTS===\s*\n(.*?)===END===",
            raw,
            re.DOTALL,
        )
        if facts_match:
            for line in facts_match.group(1).strip().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower().replace(" ", "_")
                    value = value.strip()
                    if key and value:
                        facts.append((key, value))

        return instructions, facts

    def _save_bootstrap(self, user_id: int, instructions: str) -> None:
        """Write per-user bootstrap file (LLM-generated)."""
        self._users_dir.mkdir(parents=True, exist_ok=True)
        path = self._users_dir / f"{user_id}.md"
        content = f"---\nUser Preferences\n\n{instructions}\n"
        path.write_text(content, encoding="utf-8")
        logger.info("Saved user bootstrap: %s (%d chars)", path, len(content))

    def _save_fallback_bootstrap(
        self,
        user_id: int,
        answers: dict[str, str],
        department: str,
    ) -> None:
        """Generate a minimal bootstrap from raw answers (no LLM)."""
        name = answers.get("preferred_name", f"user_{user_id}")
        style = answers.get("communication_style", "")
        language = answers.get("preferred_language", "")
        context = answers.get("work_context", "")

        lines = ["---", "User Preferences", ""]
        lines.append("## About This User")
        lines.append(f"- Name: {name}")
        lines.append(f"- Department: {department}")
        if context and context.lower() != "нет":
            lines.append(f"- Context: {context}")
        if style and style.lower() != "нет":
            lines.append(f"\n## Communication Style\n{style}")
        if language and language.lower() != "нет":
            lines.append(f"\n## Language\n{language}")
        lines.append("")

        self._users_dir.mkdir(parents=True, exist_ok=True)
        path = self._users_dir / f"{user_id}.md"
        path.write_text("\n".join(lines), encoding="utf-8")

    async def _save_facts(self, user_id: int, facts: list[tuple[str, str]]) -> None:
        """Write LLM-structured facts to memory_facts."""
        for key, value in facts:
            await self._memory.store_fact(str(user_id), key, value)

    async def _fallback_save(
        self,
        user_id: int,
        answers: dict[str, str],
        department: str,
    ) -> None:
        """If LLM fails, save raw answers directly (better than nothing)."""
        logger.warning("Using fallback raw save for user %d", user_id)
        for key, value in answers.items():
            stripped = value.strip()
            if stripped and stripped.lower() != "нет":
                await self._memory.store_fact(str(user_id), key, stripped)

        self._save_fallback_bootstrap(user_id, answers, department)
