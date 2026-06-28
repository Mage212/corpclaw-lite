"""Per-run context-persistence target (B-063 S1 audit fix).

Stores the active chat's ``(session_id, user_id)`` for the context-persistence
layer. These are held in ``contextvars`` (NOT instance attributes) so that
concurrent ``AgentLoop.run()`` calls on the shared singleton loop are isolated:
each run executes in its own ``asyncio.create_task`` (orchestrator), and
``contextvars`` are task-scoped, so one run's session_id never leaks into
another's persist calls.

Mirrors the ``depth_mode`` contextvar pattern.
"""

from __future__ import annotations

import contextvars

__all__ = [
    "get_context_session_id",
    "get_context_user_id",
    "reset_context_target",
    "set_context_target",
]

_ctx_session_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "corpclaw_ctx_session_id", default=None
)
_ctx_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "corpclaw_ctx_user_id", default=None
)

# Type alias for the token pair returned by set_context_target.
ContextTargetTokens = tuple[contextvars.Token[int | None], contextvars.Token[str | None]]


def set_context_target(session_id: int | None, user_id: str | None) -> ContextTargetTokens:
    """Bind the persist target for this run.

    Returns the token pair; pass to :func:`reset_context_target` in a ``finally``
    block to restore the prior values (so the contextvar does not outlive the
    run). See ``AgentLoop.run`` for usage.
    """
    return (_ctx_session_id.set(session_id), _ctx_user_id.set(user_id))


def reset_context_target(tokens: ContextTargetTokens) -> None:
    """Restore the contextvars to their pre-run values."""
    _ctx_session_id.reset(tokens[0])
    _ctx_user_id.reset(tokens[1])


def get_context_session_id() -> int | None:
    """Return the session_id bound for the current run, or None."""
    return _ctx_session_id.get()


def get_context_user_id() -> str | None:
    """Return the user_id bound for the current run, or None."""
    return _ctx_user_id.get()
