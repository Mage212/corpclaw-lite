"""B-091 / DC-012: per-run web access flag (main-agent web_fetch).

Cache-safe (D-087): model-facing text is a **tail message** with a stable
marker (same idea as tool-surface soft-hint), not a system-prompt rewrite.
Execute deny uses a contextvar so nested subagent loops can leave web tools on.
"""

from __future__ import annotations

import contextvars
from typing import Any

__all__ = [
    "WEB_ACCESS_MARKER",
    "WEB_FETCH_DENIED_MESSAGE",
    "get_web_access",
    "inject_web_access_hint",
    "reset_web_access",
    "set_web_access",
]

WEB_ACCESS_MARKER = "[Web access]"

WEB_FETCH_DENIED_MESSAGE = (
    "Error: web_fetch is disabled for this turn (web access OFF). "
    "Do not call web_fetch or invent internet results. "
    "Deep research may still be available via dispatch_subagent if that tool is listed."
)

_OFF_HINT = (
    f"{WEB_ACCESS_MARKER} OFF\n"
    "Сетевой доступ (web_fetch) выключен. Не вызывай web_fetch и не имитируй "
    "поиск в интернете. Глубокий research — только через dispatch_subagent, "
    "если он доступен."
)

# Default True so subagents / CLI without explicit flag keep prior behaviour.
_web_access: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "corpclaw_web_access", default=True
)


def set_web_access(enabled: bool) -> contextvars.Token[bool]:
    """Set per-run web access. Returns a token for :func:`reset_web_access`."""
    return _web_access.set(enabled)


def reset_web_access(token: contextvars.Token[bool]) -> None:
    """Reset per-run web access to the prior value."""
    _web_access.reset(token)


def get_web_access() -> bool:
    """Return whether main-agent web_fetch is allowed this run (default True)."""
    return _web_access.get()


def inject_web_access_hint(
    messages: list[dict[str, Any]],
    *,
    enabled: bool,
) -> list[dict[str, Any]]:
    """Return messages with at most one ``[Web access]`` tail hint (cache-safe).

    - Strips any prior message containing :data:`WEB_ACCESS_MARKER`.
    - When *enabled* is False, appends the OFF instruction.
    - When *enabled* is True, only strips (no ON noise in the default path).

    Does not touch system_prompt or tools_schema.
    """
    cleaned = [
        m
        for m in messages
        if not (isinstance(m.get("content"), str) and WEB_ACCESS_MARKER in str(m.get("content")))
    ]
    if enabled:
        return cleaned
    cleaned.append({"role": "user", "content": _OFF_HINT})
    return cleaned
