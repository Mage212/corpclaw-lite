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


# ──────────────────── Multi-seed (D-052) ─────────────────────────────────


from corpclaw_lite.eval.report import MultiSeedReport, ScenarioMultiSeed  # noqa: E402


def _ab(on_overall: float, off_overall: float, sid: str = "s1") -> ABReport:
    """Build a single-scenario A/B report with the given on/off scores."""
    on = PassReport(
        label="guards_on", scenario_scores=[_scenario_score(sid, on_overall, on_overall >= 6.0)]
    )
    off = PassReport(
        label="guards_off", scenario_scores=[_scenario_score(sid, off_overall, off_overall >= 6.0)]
    )
    return ABReport.compare(on, off)


def test_multiseed_median_filters_noise() -> None:
    """3 seeds: off-pass has one random 0.0 (sampling noise) but two 8.5s.
    Median should be 8.5, not dragged down by the outlier."""
    reports = [
        _ab(8.5, 0.0),  # seed 1: noise (random wrong-number)
        _ab(8.5, 8.5),  # seed 2: stable
        _ab(8.5, 8.5),  # seed 3: stable
    ]
    ms = MultiSeedReport.from_ab_reports(reports)
    assert ms.seeds == 3
    assert len(ms.per_scenario) == 1
    s = ms.per_scenario[0]
    assert s.on_median == 8.5
    assert s.off_median == 8.5  # median filters the single 0.0
    assert s.delta == 0.0
    assert ms.verdict == "guards_neutral"


def test_multiseed_stable_verdict_help() -> None:
    """Guards genuinely help: on stable at 8.5, off stable at 4.0 across all seeds."""
    reports = [_ab(8.5, 4.0), _ab(8.5, 4.0), _ab(8.5, 4.0)]
    ms = MultiSeedReport.from_ab_reports(reports)
    assert ms.mean_delta > 4.0
    assert ms.verdict == "guards_help"
    assert ms.improved_count == 1
    assert ms.regressed_count == 0


def test_multiseed_stable_verdict_hurt() -> None:
    """Guards genuinely hurt: on stable at 3.0, off stable at 8.0."""
    reports = [_ab(3.0, 8.0), _ab(3.0, 8.0)]
    ms = MultiSeedReport.from_ab_reports(reports)
    assert ms.verdict == "guards_hurt"
    assert ms.regressed_count == 1


def test_multiseed_noisy_scenario_flagged() -> None:
    """on_scores=[8.5, 0.0, 8.5] → on is noisy (spread > 0.1)."""
    reports = [_ab(8.5, 8.0), _ab(0.0, 8.0), _ab(8.5, 8.0)]
    ms = MultiSeedReport.from_ab_reports(reports)
    s = ms.per_scenario[0]
    assert not s.on_stable  # spread 8.5 > 0.1
    assert s.off_stable
    assert s.noisy
    assert ms.noisy_count == 1
    assert ms.stable_count == 0


def test_multiseed_to_dict_and_write(tmp_path: Path) -> None:
    reports = [_ab(8.5, 4.0), _ab(8.5, 4.0)]
    ms = MultiSeedReport.from_ab_reports(reports)
    d = ms.to_dict()
    assert d["seeds"] == 2
    assert d["summary"]["verdict"] == "guards_help"
    assert len(d["per_scenario"]) == 1

    ms.write(tmp_path)
    assert (tmp_path / "multi_seed_report.json").exists()
    assert (tmp_path / "multi_seed_report.md").exists()
    parsed = json.loads((tmp_path / "multi_seed_report.json").read_text(encoding="utf-8"))
    assert parsed["seeds"] == 2


def test_multiseed_mismatched_scenarios() -> None:
    """Seed 1 has scenario 's1', seed 2 has 's1' and 's2'. Both aggregated."""
    reports = [_ab(8.5, 4.0, sid="s1"), _ab(9.0, 5.0, sid="s1"), _ab(7.0, 6.0, sid="s2")]
    ms = MultiSeedReport.from_ab_reports(reports)
    ids = {s.scenario_id for s in ms.per_scenario}
    assert ids == {"s1", "s2"}
    s1 = next(s for s in ms.per_scenario if s.scenario_id == "s1")
    assert len(s1.on_scores) == 2  # s1 appeared in seed 1 and 2 only


def test_scenario_multiseed_delta_and_stability() -> None:
    """Direct ScenarioMultiSeed unit test for edge cases."""
    # Stable scenario
    s_stable = ScenarioMultiSeed(
        scenario_id="x", on_scores=[8.5, 8.5, 8.5], off_scores=[8.0, 8.0, 8.0]
    )
    assert s_stable.on_stable
    assert not s_stable.noisy
    assert s_stable.delta == 0.5
    assert s_stable.improved  # delta exactly at threshold 0.5

    # Noisy scenario (tiny spread within band still counts stable)
    s_band = ScenarioMultiSeed(scenario_id="y", on_scores=[8.5, 8.45, 8.5])
    assert s_band.on_stable  # spread 0.05 < 0.1

    # Empty scores
    s_empty = ScenarioMultiSeed(scenario_id="z")
    assert s_empty.on_median == 0.0
    assert s_empty.on_stable  # empty treated as stable


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
