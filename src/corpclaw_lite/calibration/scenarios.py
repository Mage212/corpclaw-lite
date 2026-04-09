"""Calibration scenario data models and YAML loader."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "CalibrationScenario",
    "ScenarioExpectation",
    "ScenarioSetup",
    "load_scenarios",
]


@dataclass
class ScenarioExpectation:
    """What we expect the agent to do."""

    tool_calls: list[str] = field(default_factory=lambda: list[str]())
    must_read: str | None = None
    contains: str | None = None
    has_content: bool = True


@dataclass
class ScenarioSetup:
    """Filesystem state to prepare before running."""

    files: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())


@dataclass
class CalibrationScenario:
    """Single test scenario for calibration."""

    id: str
    user_message: str
    expected: ScenarioExpectation
    setup: ScenarioSetup | None = None
    category: str = "general"


def _parse_setup(raw: dict[str, Any] | None) -> ScenarioSetup | None:
    """Parse setup section from YAML."""
    if raw is None:
        return None
    files = [(f["path"], f["content"]) for f in raw.get("files", [])]
    return ScenarioSetup(files=files)


def _parse_expectation(raw: dict[str, Any]) -> ScenarioExpectation:
    """Parse expected section from YAML."""
    return ScenarioExpectation(
        tool_calls=raw.get("tool_calls", []),
        must_read=raw.get("must_read"),
        contains=raw.get("contains"),
        has_content=raw.get("has_content", True),
    )


def load_scenarios(path: Path | str) -> list[CalibrationScenario]:
    """Load calibration scenarios from a YAML file.

    Args:
        path: Path to the YAML file with scenario definitions.

    Returns:
        List of parsed CalibrationScenario instances.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or missing required fields.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Scenarios file not found: {path}")

    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_scenarios: list[dict[str, Any]] = data.get("scenarios", [])

    if not raw_scenarios:
        raise ValueError(f"No scenarios found in {path}")

    scenarios: list[CalibrationScenario] = []
    for raw in raw_scenarios:
        if "id" not in raw or "user_message" not in raw:
            raise ValueError(f"Scenario missing required 'id' or 'user_message': {raw}")
        if "expected" not in raw:
            raise ValueError(f"Scenario '{raw['id']}' missing 'expected' section")

        scenarios.append(
            CalibrationScenario(
                id=raw["id"],
                user_message=raw["user_message"],
                expected=_parse_expectation(raw["expected"]),
                setup=_parse_setup(raw.get("setup")),
                category=raw.get("category", "general"),
            )
        )

    return scenarios
