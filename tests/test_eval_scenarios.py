"""Tests for the eval scenario loader (B-060, step 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.eval.scenarios import (
    EvalScenario,
    ScenarioSetup,
    ScenarioTurn,
    load_scenarios,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "scenarios.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_single_turn_shorthand(tmp_path: Path) -> None:
    """A top-level user_message is accepted as a single-turn shorthand."""
    p = _write(
        tmp_path,
        """
scenarios:
  - id: simple
    category: general
    user_message: "hello"
    expected_answer: "hi"
""",
    )
    scenarios = load_scenarios(p)
    assert len(scenarios) == 1
    s = scenarios[0]
    assert s.id == "simple"
    assert len(s.turns) == 1
    assert s.turns[0].user_message == "hello"
    assert s.turns[0].expected_answer == "hi"
    assert not s.is_multi_turn


def test_load_multi_turn(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
scenarios:
  - id: multi
    category: multi_turn
    turns:
      - user_message: "q1"
        expected_answer: "a1"
      - user_message: "q2"
        expected_answer: "a2"
""",
    )
    scenarios = load_scenarios(p)
    s = scenarios[0]
    assert s.is_multi_turn
    assert len(s.turns) == 2
    assert s.turns[1].expected_answer == "a2"


def test_expected_answer_null_preserved(tmp_path: Path) -> None:
    """null expected_answer (adversarial 'not in document') must round-trip."""
    p = _write(
        tmp_path,
        """
scenarios:
  - id: null_case
    user_message: "what is the secret?"
    expected_answer: null
""",
    )
    s = load_scenarios(p)[0]
    assert s.turns[0].expected_answer is None


def test_setup_files_and_corpus(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
scenarios:
  - id: with_setup
    user_message: "x"
    setup:
      files:
        - path: "a.txt"
          content: "hello"
      copy_from_corpus:
        - dest: "b.xlsx"
          source: "fixture.xlsx"
""",
    )
    s = load_scenarios(p)[0]
    assert s.setup is not None
    assert s.setup.files == [("a.txt", "hello")]
    assert s.setup.copy_from_corpus == [("b.xlsx", "fixture.xlsx")]


def test_severity_and_description_parsed(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
scenarios:
  - id: sev
    severity: adversarial
    description: "a tricky one"
    user_message: "x"
""",
    )
    s = load_scenarios(p)[0]
    assert s.severity == "adversarial"
    assert s.description == "a tricky one"


def test_missing_id_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
scenarios:
  - user_message: "x"
""",
    )
    with pytest.raises(ValueError, match="id"):
        load_scenarios(p)


def test_missing_turns_and_message_raises(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        """
scenarios:
  - id: no_msg
""",
    )
    with pytest.raises(ValueError, match="turns"):
        load_scenarios(p)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_scenarios(tmp_path / "nope.yaml")


def test_empty_scenarios_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "scenarios: []\n")
    with pytest.raises(ValueError, match="No scenarios"):
        load_scenarios(p)


def test_shipped_corpus_loads() -> None:
    """The shipped config/eval_scenarios.yaml must load and contain the planned
    12 scenarios with at least the expected categories."""
    from corpclaw_lite.paths import PROJECT_ROOT

    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    assert len(scenarios) == 12
    ids = {s.id for s in scenarios}
    # A representative subset that pins the corpus shape.
    assert "csv_aggregate_total" in ids
    assert "null_answer_not_in_document" in ids
    assert "multi_turn_yoy_then_q4" in ids
    # Every scenario has at least one turn with a user_message.
    for s in scenarios:
        assert s.turns, f"scenario {s.id} has no turns"
        assert all(t.user_message for t in s.turns), f"empty message in {s.id}"
    # The null-answer scenario must preserve None.
    null_scn = next(s for s in scenarios if s.id == "null_answer_not_in_document")
    assert null_scn.turns[0].expected_answer is None
    assert null_scn.severity == "adversarial"


def test_turn_defaults() -> None:
    t = ScenarioTurn(user_message="x")
    assert t.expected_answer is None
    assert t.expected_tools == []
    assert t.success_criteria == ""
    assert t.must_contain is None


def test_setup_defaults() -> None:
    s = ScenarioSetup()
    assert s.files == []
    assert s.copy_from_corpus == []


def test_scenario_is_multi_turn_flag() -> None:
    single = EvalScenario(id="a", category="c", turns=[ScenarioTurn(user_message="x")])
    multi = EvalScenario(
        id="b", category="c", turns=[ScenarioTurn(user_message="x"), ScenarioTurn(user_message="y")]
    )
    assert not single.is_multi_turn
    assert multi.is_multi_turn
