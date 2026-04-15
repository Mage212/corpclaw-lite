"""Typed async helpers — wrappers that fix pyright strict-mode gaps in third-party libs."""

from __future__ import annotations

from collections.abc import Callable
from typing import ParamSpec, TypeVar

import anyio

P = ParamSpec("P")
T = TypeVar("T")


async def run_in_thread(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:  # noqa: UP047
    """Run *func* in a worker thread, returning its result.

    Typed wrapper around ``anyio.to_thread.run_sync`` that works cleanly
    with pyright strict mode (anyio 4.x stubs cause
    ``reportUnknownMemberType`` / ``BrokenWorkerInterpreter`` errors).
    """
    return await anyio.to_thread.run_sync(  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType,reportAttributeAccessIssue]
        lambda: func(*args, **kwargs)
    )
