"""B-143 PR3: one-shot LLM map of free-form schedule_text → parseable formula.

Never called from poll/tick — only on human request (REST parse-assist).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from corpclaw_lite.scheduler.models import ScheduleSpec
from corpclaw_lite.scheduler.parse import (
    ScheduleParseError,
    compute_next_run,
    parse_schedule,
)

if TYPE_CHECKING:
    from corpclaw_lite.llm.base import Provider

__all__ = [
    "ParseAssistError",
    "ParseAssistResult",
    "llm_parse_schedule",
]

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


class ParseAssistError(Exception):
    """LLM assist failed or returned unusable formula."""


@dataclass(frozen=True, slots=True)
class ParseAssistResult:
    """Validated interpretation ready for human confirm + accept."""

    formula: str
    explanation: str
    schedule: ScheduleSpec
    next_run_at: str | None
    original_text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "formula": self.formula,
            "explanation": self.explanation,
            "schedule": self.schedule.to_json(),
            "next_run_at": self.next_run_at,
            "original_text": self.original_text,
        }


_SYSTEM = (
    "You convert natural-language schedule phrases into a machine formula.\n"
    "Reply with ONLY a single JSON object (no markdown fences):\n"
    '{"formula":"<parseable formula>","explanation":"<short human explanation>"}\n\n'
    "Allowed formula formats:\n"
    '- Interval: "every 30m", "every 2h", "every 1d"\n'
    '- Once after delay: "30m", "2h", "1d"\n'
    '- Cron (5 fields): "0 9 * * 1-5"\n'
    '- ISO datetime: "2026-07-17T09:00:00"\n'
    "Do not invent other formats. Prefer interval/cron over vague language."
)


async def llm_parse_schedule(
    provider: Provider,
    *,
    schedule_text: str,
    timezone: str,
    now: datetime | None = None,
) -> ParseAssistResult:
    """Call LLM once, validate formula with deterministic parse_schedule."""
    raw = (schedule_text or "").strip()
    if not raw:
        raise ParseAssistError("schedule_text is empty")

    now_utc = (now or datetime.now(UTC)).replace(microsecond=0)
    user_msg = (
        f"Timezone: {timezone}\nNow (UTC): {now_utc.isoformat()}\nPhrase to convert:\n{raw}\n"
    )

    target = _resolve_provider(provider)
    try:
        response = await target.chat(
            messages=[{"role": "user", "content": user_msg}],
            tools=None,
            system=_SYSTEM,
        )
    except Exception as exc:
        logger.warning("parse_assist LLM call failed: %s", exc)
        raise ParseAssistError(f"LLM call failed: {exc}") from exc

    content = (response.content or "").strip()
    formula, explanation = _extract_json(content)

    try:
        spec = parse_schedule(formula, now=now_utc, tz=timezone)
    except ScheduleParseError as exc:
        raise ParseAssistError(f"LLM formula not parseable: {formula!r} ({exc})") from exc

    if spec.kind == "unset":
        raise ParseAssistError(f"LLM formula is unset: {formula!r}")

    next_run = compute_next_run(spec, last_run_at=None, now=now_utc, tz=timezone)
    next_iso = next_run.astimezone(UTC).replace(microsecond=0).isoformat() if next_run else None
    return ParseAssistResult(
        formula=formula.strip(),
        explanation=explanation.strip() or formula.strip(),
        schedule=spec,
        next_run_at=next_iso,
        original_text=raw,
    )


def _resolve_provider(provider: Provider) -> Provider:
    """Prefer a non-sticky route when router is available (overflow-friendly)."""
    try:
        from corpclaw_lite.llm.router import LLMRouter

        if isinstance(provider, LLMRouter):
            if provider.has_task_route("compress"):
                return provider.for_task("compress", load_class="compression")
            return provider.for_task("default", load_class="subagent")
    except Exception:
        pass
    return provider


def _extract_json(content: str) -> tuple[str, str]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    candidates: list[str] = [text]
    match = _JSON_BLOCK_RE.search(text)
    if match:
        candidates.insert(0, match.group(0))

    last_err: Exception | None = None
    for cand in candidates:
        try:
            data: Any = json.loads(cand)
        except json.JSONDecodeError as exc:
            last_err = exc
            continue
        if not isinstance(data, dict):
            continue
        body = cast("dict[str, Any]", data)
        formula_raw = body.get("formula")
        explanation_raw = body.get("explanation", "")
        if isinstance(formula_raw, str) and formula_raw.strip():
            expl = explanation_raw if isinstance(explanation_raw, str) else ""
            return formula_raw.strip(), expl.strip()
        last_err = ParseAssistError("JSON missing string 'formula'")

    raise ParseAssistError(
        f"Could not parse LLM JSON response: {content[:200]!r}"
        + (f" ({last_err})" if last_err else "")
    )
