"""Conftest for live eval tests (B-060).

These tests spin up a full AgentLoop against a real llama-server, so they are
excluded from the normal test pool (pyproject.toml addopts ignores this dir)
and gated behind CORPCLAW_EVAL_LIVE=1 — mirroring the tests/live_llm convention.

Run manually::

    CORPCLAW_EVAL_LIVE=1 \
    CORPCLAW_LIVE_LLM_BASE_URL=http://192.168.193.178:8080 \
    CORPCLAW_LIVE_LLM_MODEL=gpt-oss-20b-UD-Q4_K_XL \
    uv run pytest tests/eval_live/ -v -s -o addopts=''
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "eval_live: end-to-end eval run against a real llama-server")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("CORPCLAW_EVAL_LIVE") != "1":
        skip = pytest.mark.skip(reason="set CORPCLAW_EVAL_LIVE=1 to run live eval tests")
        for item in items:
            item.add_marker(skip)
