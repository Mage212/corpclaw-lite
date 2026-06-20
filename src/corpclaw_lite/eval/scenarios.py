"""Eval scenario data models and YAML loader (B-060).

Extends the calibration scenario shape with GAIA-style ground-truth fields:
multi-turn support, ``expected_answer`` (string or ``null`` for the adversarial
"not in document" case), ``success_criteria``, and ``severity``. Setup can
materialise inline text files into the workspace (for deterministic fixtures)
and copy binary fixtures (xlsx/csv) from a corpus directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "EvalScenario",
    "ScenarioSetup",
    "ScenarioTurn",
    "load_scenarios",
]


@dataclass
class ScenarioTurn:
    """A single user turn in a (possibly multi-turn) scenario.

    ``expected_answer`` semantics follow judge_turn.md:

    - A non-null string is the ground truth the answer is matched against
      (subject to zero-rules: wrong number, wrong name, lazy refusal,
      hallucinated source).
    - ``None`` asserts that NO specific answer exists in the source material;
      the agent should indicate the information is unavailable. Saying
      "I don't know" scores up to 10; inventing an answer scores 0.
    """

    user_message: str
    expected_answer: str | None = None
    # Behavioural expectation (used instead of expected_answer when set): which
    # tools the agent should call, in order. Extras are tolerated. Used by the
    # tool_selection dimension and the deterministic scorer.
    expected_tools: list[str] = field(default_factory=lambda: list[str]())
    # For router+executor scenarios (D-028): the subagent the main agent should
    # delegate to. When set, the deterministic scorer checks the trajectory for a
    # dispatch_subagent call with this subagent_id. Office execution tools
    # (table_query, write_file, ...) live inside subagents, so expected_tools for
    # such scenarios is typically ["dispatch_subagent"] and expected_subagent
    # names the specialist.
    expected_subagent: str | None = None
    # Free-form clause the judge evaluates (e.g. "FAIL if agent re-reads the
    # file after it was already read in turn 1"). Not parsed mechanically.
    success_criteria: str = ""
    # Optional substring that must appear in the answer (deterministic check,
    # case-insensitive). Useful for exact numeric answers.
    must_contain: str | None = None


@dataclass
class ScenarioSetup:
    """Workspace state to materialise before running the scenario.

    Inline text files are written verbatim; binary fixtures are copied from a
    corpus directory (so xlsx/csv blobs are not embedded in YAML); images are
    generated programmatically (deterministic, no external files).
    """

    files: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())
    # (relative_dest_path, source_path_within_corpus_dir)
    copy_from_corpus: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())
    # (relative_dest_path, generator_id) — deterministic PNG generation for
    # vision scenarios. Supported generator ids live in
    # corpclaw_lite.eval.vision_fixtures.generate_image.
    generated_images: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())


@dataclass
class EvalScenario:
    """A single eval scenario (one or more turns)."""

    id: str
    category: str
    turns: list[ScenarioTurn]
    setup: ScenarioSetup | None = None
    severity: str = "normal"  # normal | adversarial | critical
    description: str = ""

    @property
    def is_multi_turn(self) -> bool:
        return len(self.turns) > 1


def _parse_turn(raw: dict[str, Any]) -> ScenarioTurn:
    return ScenarioTurn(
        user_message=raw["user_message"],
        expected_answer=raw.get("expected_answer"),
        expected_tools=raw.get("expected_tools", []),
        expected_subagent=raw.get("expected_subagent"),
        success_criteria=raw.get("success_criteria", ""),
        must_contain=raw.get("must_contain"),
    )


def _parse_setup(raw: dict[str, Any] | None) -> ScenarioSetup | None:
    if raw is None:
        return None
    files = [(f["path"], f["content"]) for f in raw.get("files", [])]
    copy_from_corpus = [(c["dest"], c["source"]) for c in raw.get("copy_from_corpus", [])]
    generated_images = [(g["dest"], g["generator"]) for g in raw.get("generated_images", [])]
    return ScenarioSetup(
        files=files, copy_from_corpus=copy_from_corpus, generated_images=generated_images
    )


def load_scenarios(path: Path | str) -> list[EvalScenario]:
    """Load eval scenarios from a YAML file.

    Each scenario has::

        id, category, severity?, description?, setup?, turns: [{user_message,
        expected_answer?, expected_tools?, success_criteria?, must_contain?}]

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the YAML is malformed or a scenario is missing required
            fields (``id``, ``turns`` with at least one ``user_message``).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Eval scenarios file not found: {path}")

    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_scenarios: list[dict[str, Any]] = data.get("scenarios", [])

    if not raw_scenarios:
        raise ValueError(f"No scenarios found in {path}")

    scenarios: list[EvalScenario] = []
    for raw in raw_scenarios:
        if "id" not in raw:
            raise ValueError(f"Scenario missing required 'id': {raw}")
        raw_turns = raw.get("turns")
        if not raw_turns:
            # Allow single-turn shorthand: a top-level user_message.
            if "user_message" not in raw:
                raise ValueError(
                    f"Scenario '{raw['id']}' missing 'turns' or top-level 'user_message'"
                )
            raw_turns = [
                {
                    "user_message": raw["user_message"],
                    **{
                        k: raw[k]
                        for k in (
                            "expected_answer",
                            "expected_tools",
                            "expected_subagent",
                            "success_criteria",
                            "must_contain",
                        )
                        if k in raw
                    },
                }
            ]
        turns = [_parse_turn(t) for t in raw_turns]
        scenarios.append(
            EvalScenario(
                id=raw["id"],
                category=raw.get("category", "general"),
                turns=turns,
                setup=_parse_setup(raw.get("setup")),
                severity=raw.get("severity", "normal"),
                description=raw.get("description", ""),
            )
        )

    return scenarios
