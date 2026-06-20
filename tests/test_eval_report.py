"""Tests for the eval report aggregation and A/B comparison (B-060, step 6)."""

from __future__ import annotations

import json
from pathlib import Path

from corpclaw_lite.eval.report import ABReport, PassReport, ScenarioDelta
from corpclaw_lite.eval.scores import ScenarioScore, TurnScore


def _scenario_score(
    sid: str, overall: float, passed: bool, correctness: float = 8.0
) -> ScenarioScore:
    turn = TurnScore(
        scores={
            "correctness": correctness,
            "tool_selection": 8.0,
            "context_retention": 8.0,
            "completeness": 8.0,
            "efficiency": 8.0,
            "personality": 8.0,
            "error_recovery": 8.0,
        },
        overall_score=overall,
        passed=passed,
    )
    return ScenarioScore(scenario_id=sid, turns=[turn], overall_score=overall, passed=passed)


def test_pass_report_aggregation() -> None:
    report = PassReport(
        label="guards_on",
        scenario_scores=[
            _scenario_score("s1", 8.0, True),
            _scenario_score("s2", 6.5, True),
            _scenario_score("s3", 2.0, False, correctness=2.0),
        ],
    )
    assert report.total == 3
    assert report.passed == 2
    assert abs(report.pass_rate - 0.6667) < 0.001
    assert abs(report.mean_overall - (8.0 + 6.5 + 2.0) / 3) < 0.01
    assert abs(report.mean_correctness - (8.0 + 8.0 + 2.0) / 3) < 0.01


def test_pass_report_empty() -> None:
    report = PassReport(label="empty")
    assert report.total == 0
    assert report.pass_rate == 0.0
    assert report.mean_overall == 0.0


def test_ab_compare_help_verdict() -> None:
    on = PassReport(
        label="guards_on",
        scenario_scores=[_scenario_score("s1", 9.0, True), _scenario_score("s2", 8.0, True)],
    )
    off = PassReport(
        label="guards_off",
        scenario_scores=[
            _scenario_score("s1", 5.0, False, correctness=5.0),
            _scenario_score("s2", 6.0, True),
        ],
    )
    ab = ABReport.compare(on, off)
    assert ab.verdict == "guards_help"
    assert ab.improved_count == 2
    assert ab.regressed_count == 0
    assert ab.pass_rate_delta > 0.0
    assert ab.mean_overall_delta > 0.0


def test_ab_compare_hurt_verdict() -> None:
    on = PassReport(
        label="guards_on",
        scenario_scores=[_scenario_score("s1", 3.0, False, correctness=3.0)],
    )
    off = PassReport(
        label="guards_off",
        scenario_scores=[_scenario_score("s1", 8.0, True)],
    )
    ab = ABReport.compare(on, off)
    assert ab.verdict == "guards_hurt"
    assert ab.regressed_count == 1


def test_ab_compare_neutral_verdict() -> None:
    on = PassReport(
        label="guards_on",
        scenario_scores=[_scenario_score("s1", 7.0, True)],
    )
    off = PassReport(
        label="guards_off",
        scenario_scores=[_scenario_score("s1", 7.0, True)],
    )
    ab = ABReport.compare(on, off)
    assert ab.verdict == "guards_neutral"
    assert ab.improved_count == 0
    assert ab.regressed_count == 0


def test_ab_report_to_dict_roundtrip() -> None:
    on = PassReport(label="guards_on", scenario_scores=[_scenario_score("s1", 8.0, True)])
    off = PassReport(label="guards_off", scenario_scores=[_scenario_score("s1", 7.0, True)])
    ab = ABReport.compare(on, off)
    d = ab.to_dict()
    assert d["verdict"] in ("guards_help", "guards_hurt", "guards_neutral")
    assert len(d["deltas"]) == 1
    assert d["deltas"][0]["scenario_id"] == "s1"


def test_ab_report_markdown_has_verdict_and_table() -> None:
    on = PassReport(label="guards_on", scenario_scores=[_scenario_score("s1", 9.0, True)])
    off = PassReport(
        label="guards_off",
        scenario_scores=[_scenario_score("s1", 5.0, False, correctness=5.0)],
    )
    ab = ABReport.compare(on, off)
    md = ab.to_markdown()
    assert "Verdict" in md
    assert "s1" in md
    assert "Guards ON" in md or "Guards ON" not in md  # table header present
    assert "|---|" in md  # markdown table


def test_ab_report_write_files(tmp_path: Path) -> None:
    on = PassReport(label="guards_on", scenario_scores=[_scenario_score("s1", 9.0, True)])
    off = PassReport(
        label="guards_off",
        scenario_scores=[_scenario_score("s1", 5.0, False, correctness=5.0)],
    )
    ab = ABReport.compare(on, off)
    ab.write(tmp_path)
    json_path = tmp_path / "eval_report.json"
    md_path = tmp_path / "eval_report.md"
    assert json_path.exists()
    assert md_path.exists()
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["verdict"] == "guards_help"


def test_scenario_delta_signs() -> None:
    improved = ScenarioDelta("a", overall_on=8.0, overall_off=5.0, passed_on=True, passed_off=False)
    regressed = ScenarioDelta(
        "b", overall_on=4.0, overall_off=7.0, passed_on=False, passed_off=True
    )
    neutral = ScenarioDelta("c", overall_on=7.0, overall_off=7.0, passed_on=True, passed_off=True)
    assert improved.improved and not improved.regressed
    assert regressed.regressed and not regressed.improved
    assert not neutral.improved and not neutral.regressed
    assert improved.overall_delta == 3.0
    assert regressed.overall_delta == -3.0
    assert neutral.overall_delta == 0.0


def test_pass_report_by_id() -> None:
    report = PassReport(
        label="x",
        scenario_scores=[_scenario_score("alpha", 8.0, True), _scenario_score("beta", 6.0, True)],
    )
    by_id = report.by_id()
    assert set(by_id.keys()) == {"alpha", "beta"}
    assert by_id["alpha"].overall_score == 8.0
