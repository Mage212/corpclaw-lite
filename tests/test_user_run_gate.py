"""Sprint 2 / C3: process-local UserRunGate shared across channels."""

from __future__ import annotations

import pytest

from corpclaw_lite.runtime.user_run_gate import (
    get_user_run_gate,
    reset_user_run_gate_for_tests,
)


@pytest.fixture(autouse=True)
def _fresh_gate() -> None:
    reset_user_run_gate_for_tests()
    yield
    reset_user_run_gate_for_tests()


@pytest.mark.asyncio
async def test_try_start_finish_roundtrip() -> None:
    gate = get_user_run_gate()
    assert await gate.try_start(1, session_id=10, title="a") is True
    assert await gate.try_start(1) is False
    assert await gate.active_count() == 1
    info = await gate.finish(1)
    assert info is not None
    assert info.session_id == 10
    assert await gate.try_start(1) is True
    await gate.finish(1)


@pytest.mark.asyncio
async def test_same_process_gate_is_singleton() -> None:
    """Two callers (service + telegram style) share one gate in-process."""
    a = get_user_run_gate()
    b = get_user_run_gate()
    assert a is b
    assert await a.try_start(42) is True
    assert await b.try_start(42) is False
    await a.finish(42)


@pytest.mark.asyncio
async def test_update_metadata() -> None:
    gate = get_user_run_gate()
    assert await gate.try_start(7, title="pending") is True
    assert await gate.update(7, session_id=99, title="system") is True
    info = await gate.get(7)
    assert info is not None
    assert info.session_id == 99
    assert info.title == "system"
    assert await gate.update(8, session_id=1) is False
    await gate.finish(7)
