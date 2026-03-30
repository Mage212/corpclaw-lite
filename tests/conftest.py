"""Global pytest configuration and fixtures for CorpClaw Lite tests."""

from __future__ import annotations

import gc

import pytest


@pytest.fixture(autouse=True)
def _force_gc_after_test() -> None:  # pyright: ignore[reportReturnType]
    """Force garbage collection after every test.

    Python's sqlite3.connect() context manager commits/rolls back transactions
    but does NOT close the underlying connection. On CPython 3.12+ the garbage
    collector emits ResourceWarning for unclosed database connections when the
    objects are eventually collected.

    Running gc.collect() explicitly after each test ensures connections owned
    by test-local SQLiteMemory instances are closed before the next test starts,
    eliminating ResourceWarning noise in the test output.
    """
    yield  # run the test
    gc.collect()  # collect garbage → closes unreferenced sqlite3.Connection objects
