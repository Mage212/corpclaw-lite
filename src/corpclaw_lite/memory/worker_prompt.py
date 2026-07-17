"""B-109 / DC-027 Layer 2+3: prompt builders for the memory worker LLM call.

The worker makes a single ``provider.chat()`` (tools=None) with a strict system
prompt (merge-only curator, JSON-only output) and a user payload containing the
current ``.md`` body, current ``memory_entries``, and a transcript excerpt.

Response is parsed by :func:`corpclaw_lite.memory.worker_merge.parse_worker_response`.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "build_system_prompt",
    "build_user_payload",
]

_SYSTEM_PROMPT = """\
You are a memory curator for an AI assistant. Your job is to update the user's \
long-term profile based on their recent conversations.

## Rules

1. **Merge-only**: never delete information that is still relevant. Add new \
facts, update outdated ones, keep everything else.
2. **Non-authoritative**: the profile you produce is a hint for the assistant, \
not ground truth. Do not state guesses as facts.
3. **Preserve language**: write in the same language the user speaks (Russian \
or English). If unsure, match the language of the existing profile.
4. **Structured entries**: each entry has a short `abstraction` (6-8 words, \
a search hook), a `value` (full text), and optional `cues` (names, dates, \
projects, tools — anchor words for retrieval).
5. **Do not invent secrets**: never fabricate passwords, API keys, or \
sensitive data that does not appear in the transcript.
6. **Output format**: respond with ONLY a single JSON object, no markdown \
fences, no prose before or after:

```json
{
  "md": "Full markdown body for the user profile. Keep sections. Add a \
disclaimer comment at the top.",
  "entries": [
    {"abstraction": "prefers concise answers", "value": "User asked for \
short replies without preamble", "cues": ["concise", "short"]}
  ],
  "summary": "1-2 sentences describing what you changed."
}
```

7. **Entry limits**: at most 20 entries. Each abstraction ≤ 120 chars, \
value ≤ 8000 chars, 16 cues max, 64 chars each.
8. **md body**: must be non-empty. Include a top-level comment \
`<!-- auto-managed: memory worker -->` so the system knows it is managed.
"""


def build_system_prompt() -> str:
    """Return the fixed system prompt for the memory worker LLM call."""
    return _SYSTEM_PROMPT


def build_user_payload(
    *,
    current_md: str,
    current_entries: list[dict[str, Any]],
    transcript: str,
) -> str:
    """Build the user-role payload for the memory worker LLM call.

    Args:
        current_md: current ``users/{id}.md`` body (or empty string if none).
        current_entries: current ``memory_entries`` (list of dicts with
            ``abstraction``, ``value``, ``cues``).
        transcript: transcript excerpt from recent non-system sessions.

    Returns:
        A JSON string with the three sections for the LLM to process.
    """
    # Trim entries to avoid huge payloads — the LLM only needs a summary view.
    trimmed_entries = [
        {
            "abstraction": e.get("abstraction", e.get("key", "")),
            "value": str(e.get("value", ""))[:500],
            "cues": e.get("cues", []),
        }
        for e in current_entries[:50]
    ]
    payload = {
        "current_md": current_md[:8000] if current_md else "",
        "current_entries": trimmed_entries,
        "transcript_excerpt": transcript,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
