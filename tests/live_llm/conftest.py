from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers import LiveLlamaClient, LiveLlmConfig, write_report


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live_llm: requires a real llama-server backend")
    config.addinivalue_line("markers", "live_llm_slow: slower live LLM benchmark")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.environ.get("CORPCLAW_LIVE_LLM_TESTS") != "1":
        skip = pytest.mark.skip(reason="set CORPCLAW_LIVE_LLM_TESTS=1 to run live LLM tests")
        for item in items:
            item.add_marker(skip)
        return
    if os.environ.get("CORPCLAW_LIVE_LLM_RUN_SLOW") == "1":
        return
    skip_slow = pytest.mark.skip(reason="set CORPCLAW_LIVE_LLM_RUN_SLOW=1 to run slow tests")
    for item in items:
        if "live_llm_slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(scope="session")
def live_config() -> LiveLlmConfig:
    return LiveLlmConfig.from_env()


@pytest.fixture(scope="session")
def live_client(live_config: LiveLlmConfig) -> LiveLlamaClient:
    return LiveLlamaClient(live_config)


@pytest.fixture
def report_writer(live_config: LiveLlmConfig) -> Callable[[str, dict[str, Any]], None]:
    def write(test_name: str, payload: dict[str, Any]) -> None:
        write_report(live_config.report_dir, test_name, payload)

    return write
