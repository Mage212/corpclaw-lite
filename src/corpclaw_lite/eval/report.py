"""Eval report aggregation and A/B comparison (B-060, step 6).

A :class:`PassReport` aggregates one run over the full corpus (guards on OR
off). :class:`ABReport` compares two passes (guards on vs off) and answers the
central B-060 question: did the Phase 0 guards improve outcomes? Both render to
JSON and Markdown for human review.

:class:`MultiSeedReport` (D-052) aggregates N A/B runs to filter sampling noise
on local LLMs — median per scenario, stability flags, and a verdict that does
not flip between runs.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corpclaw_lite.eval.scores import ScenarioScore

__all__ = [
    "ABReport",
    "MultiSeedReport",
    "PassReport",
    "ScenarioDelta",
    "ScenarioMultiSeed",
]

# D-052 verdict thresholds: per-scenario swing < 0.5 and mean delta < 0.25 are
# treated as sampling noise (within judge variance on local LLMs).
_VERDICT_DELTA_THRESHOLD = 0.25
_VERDICT_SCENARIO_THRESHOLD = 0.5
# A scenario is "stable" across seeds when the spread of its scores is within
# this band (two seeds differing by < 0.1 are effectively identical).
_STABILITY_SPREAD = 0.1


@dataclass
class PassReport:
    """Aggregate results of one eval pass (guards on or off)."""

    label: str  # "guards_on" | "guards_off"
    scenario_scores: list[ScenarioScore] = field(default_factory=lambda: list[ScenarioScore]())

    @property
    def total(self) -> int:
        return len(self.scenario_scores)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scenario_scores if s.passed)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def mean_overall(self) -> float:
        if not self.scenario_scores:
            return 0.0
        return round(sum(s.overall_score for s in self.scenario_scores) / self.total, 4)

    @property
    def mean_correctness(self) -> float:
        if not self.scenario_scores:
            return 0.0
        totals = [
            sum(t.scores.get("correctness", 0.0) for t in s.turns) / len(s.turns)
            for s in self.scenario_scores
            if s.turns
        ]
        return round(sum(totals) / len(totals), 4) if totals else 0.0

    def by_id(self) -> dict[str, ScenarioScore]:
        return {s.scenario_id: s for s in self.scenario_scores}

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 4),
            "mean_overall": self.mean_overall,
            "mean_correctness": self.mean_correctness,
            "scenarios": [s.to_dict() for s in self.scenario_scores],
        }


@dataclass
class ScenarioDelta:
    """Per-scenario difference between the guards-on and guards-off passes."""

    scenario_id: str
    overall_on: float
    overall_off: float
    passed_on: bool
    passed_off: bool

    @property
    def overall_delta(self) -> float:
        return round(self.overall_on - self.overall_off, 4)

    @property
    def improved(self) -> bool:
        """True if guards ON scored strictly higher than OFF for this scenario."""
        return self.overall_delta > 0

    @property
    def regressed(self) -> bool:
        return self.overall_delta < 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "overall_on": self.overall_on,
            "overall_off": self.overall_off,
            "overall_delta": self.overall_delta,
            "passed_on": self.passed_on,
            "passed_off": self.passed_off,
        }


@dataclass
class ABReport:
    """Comparison of guards-on vs guards-off passes."""

    guards_on: PassReport
    guards_off: PassReport
    deltas: list[ScenarioDelta] = field(default_factory=lambda: list[ScenarioDelta]())

    @classmethod
    def compare(cls, on: PassReport, off: PassReport) -> ABReport:
        on_by_id = on.by_id()
        off_by_id = off.by_id()
        deltas: list[ScenarioDelta] = []
        for sid, on_score in on_by_id.items():
            off_score = off_by_id.get(sid)
            if off_score is None:
                continue
            deltas.append(
                ScenarioDelta(
                    scenario_id=sid,
                    overall_on=on_score.overall_score,
                    overall_off=off_score.overall_score,
                    passed_on=on_score.passed,
                    passed_off=off_score.passed,
                )
            )
        return cls(guards_on=on, guards_off=off, deltas=deltas)

    @property
    def pass_rate_delta(self) -> float:
        return round(self.guards_on.pass_rate - self.guards_off.pass_rate, 4)

    @property
    def mean_overall_delta(self) -> float:
        return round(self.guards_on.mean_overall - self.guards_off.mean_overall, 4)

    @property
    def improved_count(self) -> int:
        return sum(1 for d in self.deltas if d.improved)

    @property
    def regressed_count(self) -> int:
        return sum(1 for d in self.deltas if d.regressed)

    @property
    def verdict(self) -> str:
        """Plain-language summary: did guards help, hurt, or make no difference?"""
        if self.mean_overall_delta > 0.1 or self.pass_rate_delta > 0.0:
            return "guards_help"
        if self.mean_overall_delta < -0.1 or self.pass_rate_delta < 0.0:
            return "guards_hurt"
        return "guards_neutral"

    def to_dict(self) -> dict[str, Any]:
        return {
            "guards_on": self.guards_on.to_dict(),
            "guards_off": self.guards_off.to_dict(),
            "pass_rate_delta": self.pass_rate_delta,
            "mean_overall_delta": self.mean_overall_delta,
            "improved_count": self.improved_count,
            "regressed_count": self.regressed_count,
            "verdict": self.verdict,
            "deltas": [d.to_dict() for d in self.deltas],
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Eval Report — Phase 0 Guards A/B (B-060)\n")
        verdict_label = {
            "guards_help": "✅ Guards helped",
            "guards_hurt": "⚠️ Guards hurt",
            "guards_neutral": "➖ Guards made no significant difference",
        }[self.verdict]
        lines.append(f"**Verdict:** {verdict_label}\n")
        lines.append("## Summary\n")
        lines.append("| Metric | Guards ON | Guards OFF | Delta |")
        lines.append("|---|---|---|---|")
        lines.append(
            f"| Pass rate | {self.guards_on.pass_rate:.1%} "
            f"({self.guards_on.passed}/{self.guards_on.total}) | "
            f"{self.guards_off.pass_rate:.1%} "
            f"({self.guards_off.passed}/{self.guards_off.total}) | "
            f"{self.pass_rate_delta:+.1%} |"
        )
        lines.append(
            f"| Mean overall | {self.guards_on.mean_overall:.2f} | "
            f"{self.guards_off.mean_overall:.2f} | "
            f"{self.mean_overall_delta:+.2f} |"
        )
        lines.append(
            f"| Mean correctness | {self.guards_on.mean_correctness:.2f} | "
            f"{self.guards_off.mean_correctness:.2f} | "
            f"{self.guards_on.mean_correctness - self.guards_off.mean_correctness:+.2f} |"
        )
        lines.append(
            f"\nImproved scenarios: **{self.improved_count}**, "
            f"regressed: **{self.regressed_count}**.\n"
        )
        lines.append("## Per-scenario deltas\n")
        lines.append("| Scenario | Overall ON | Overall OFF | Delta | ON pass | OFF pass |")
        lines.append("|---|---|---|---|---|---|")
        lines.extend(
            f"| {d.scenario_id} | {d.overall_on:.2f} | {d.overall_off:.2f} | "
            f"{d.overall_delta:+.2f} | {'✅' if d.passed_on else '❌'} | "
            f"{'✅' if d.passed_off else '❌'} |"
            for d in sorted(self.deltas, key=lambda x: x.overall_delta, reverse=True)
        )
        return "\n".join(lines) + "\n"

    def write(self, out_dir: Path | str) -> None:
        """Write JSON and Markdown reports to ``out_dir``."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "eval_report.json").write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out / "eval_report.md").write_text(self.to_markdown(), encoding="utf-8")


# ───────────────────────── Multi-seed (D-052) ─────────────────────────────


def _is_stable(scores: list[float]) -> bool:
    """True when the spread of ``scores`` is within the stability band."""
    if not scores:
        return True
    if len(scores) == 1:
        return True
    return max(scores) - min(scores) <= _STABILITY_SPREAD


@dataclass
class ScenarioMultiSeed:
    """Per-scenario median across N seeds, for the on/off passes.

    A "noisy" scenario (where the on or off scores vary across seeds) is a
    candidate for corpus revision — either the scenario is ill-defined or the
    model is genuinely unstable on it.
    """

    scenario_id: str
    on_scores: list[float] = field(default_factory=lambda: list[float]())
    off_scores: list[float] = field(default_factory=lambda: list[float]())

    @property
    def on_median(self) -> float:
        return round(statistics.median(self.on_scores), 4) if self.on_scores else 0.0

    @property
    def off_median(self) -> float:
        return round(statistics.median(self.off_scores), 4) if self.off_scores else 0.0

    @property
    def delta(self) -> float:
        """on_median − off_median (positive = guards helped this scenario)."""
        return round(self.on_median - self.off_median, 4)

    @property
    def on_stable(self) -> bool:
        return _is_stable(self.on_scores)

    @property
    def off_stable(self) -> bool:
        return _is_stable(self.off_scores)

    @property
    def noisy(self) -> bool:
        """True if either pass varies across seeds beyond the stability band."""
        return not self.on_stable or not self.off_stable

    @property
    def improved(self) -> bool:
        return self.delta >= _VERDICT_SCENARIO_THRESHOLD

    @property
    def regressed(self) -> bool:
        return self.delta <= -_VERDICT_SCENARIO_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "on_scores": self.on_scores,
            "off_scores": self.off_scores,
            "on_median": self.on_median,
            "off_median": self.off_median,
            "delta": self.delta,
            "on_stable": self.on_stable,
            "off_stable": self.off_stable,
            "noisy": self.noisy,
        }


@dataclass
class MultiSeedReport:
    """Aggregated A/B report across N seeds (D-052).

    Each seed is a full :class:`ABReport` (guards on vs off). The aggregation
    takes the per-scenario median for on and off passes, then derives a stable
    verdict that does not flip on re-run.
    """

    seeds: int
    ab_reports: list[ABReport] = field(default_factory=lambda: list[ABReport]())
    per_scenario: list[ScenarioMultiSeed] = field(default_factory=lambda: list[ScenarioMultiSeed]())

    @classmethod
    def from_ab_reports(cls, reports: list[ABReport]) -> MultiSeedReport:
        """Aggregate N single-seed A/B reports into a multi-seed report.

        Scenarios that appear in only some seeds are still aggregated from the
        seeds that produced them (a crash in one seed does not drop the
        scenario).
        """
        if not reports:
            return cls(seeds=0)
        # Collect per-scenario on/off score lists across seeds.
        on_by_id: dict[str, list[float]] = {}
        off_by_id: dict[str, list[float]] = {}
        for ab in reports:
            for d in ab.deltas:
                on_by_id.setdefault(d.scenario_id, []).append(d.overall_on)
                off_by_id.setdefault(d.scenario_id, []).append(d.overall_off)
        per_scenario = [
            ScenarioMultiSeed(
                scenario_id=sid,
                on_scores=on_by_id[sid],
                off_scores=off_by_id.get(sid, []),
            )
            for sid in sorted(on_by_id)
        ]
        return cls(seeds=len(reports), ab_reports=reports, per_scenario=per_scenario)

    @property
    def mean_on_median(self) -> float:
        if not self.per_scenario:
            return 0.0
        return round(sum(s.on_median for s in self.per_scenario) / len(self.per_scenario), 4)

    @property
    def mean_off_median(self) -> float:
        if not self.per_scenario:
            return 0.0
        return round(sum(s.off_median for s in self.per_scenario) / len(self.per_scenario), 4)

    @property
    def mean_delta(self) -> float:
        return round(self.mean_on_median - self.mean_off_median, 4)

    @property
    def improved_count(self) -> int:
        return sum(1 for s in self.per_scenario if s.improved)

    @property
    def regressed_count(self) -> int:
        return sum(1 for s in self.per_scenario if s.regressed)

    @property
    def stable_count(self) -> int:
        return sum(1 for s in self.per_scenario if not s.noisy)

    @property
    def noisy_count(self) -> int:
        return sum(1 for s in self.per_scenario if s.noisy)

    @property
    def verdict(self) -> str:
        """Stable verdict across seeds (D-052 thresholds).

        - ``guards_help``: mean delta > +0.25 AND improved > regressed
        - ``guards_hurt``: mean delta < −0.25 AND regressed > improved
        - ``guards_neutral``: otherwise
        """
        if (
            self.mean_delta > _VERDICT_DELTA_THRESHOLD
            and self.improved_count > self.regressed_count
        ):
            return "guards_help"
        if (
            self.mean_delta < -_VERDICT_DELTA_THRESHOLD
            and self.regressed_count > self.improved_count
        ):
            return "guards_hurt"
        return "guards_neutral"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seeds": self.seeds,
            "summary": {
                "mean_on_median": self.mean_on_median,
                "mean_off_median": self.mean_off_median,
                "mean_delta": self.mean_delta,
                "verdict": self.verdict,
                "improved_count": self.improved_count,
                "regressed_count": self.regressed_count,
            },
            "stability": {
                "stable_count": self.stable_count,
                "noisy_count": self.noisy_count,
            },
            "per_scenario": [s.to_dict() for s in self.per_scenario],
        }

    def to_markdown(self) -> str:
        labels = {
            "guards_help": "✅ Guards HELPED (stable across seeds)",
            "guards_hurt": "⚠️ Guards HURT (stable across seeds)",
            "guards_neutral": "➖ Guards made no significant difference (stable)",
        }
        lines: list[str] = [
            f"# Eval Multi-Seed Report ({self.seeds} seeds, D-052)\n",
            f"**Verdict:** {labels[self.verdict]}\n",
            "## Summary (per-scenario medians)\n",
            "| Metric | Guards ON | Guards OFF | Delta |",
            "|---|---|---|---|",
            f"| Mean overall (median) | {self.mean_on_median:.2f} | "
            f"{self.mean_off_median:.2f} | {self.mean_delta:+.2f} |",
            f"| Improved / Regressed | | | {self.improved_count} / {self.regressed_count} |",
            f"| Stable / Noisy scenarios | | | {self.stable_count} / {self.noisy_count} |",
            "\n## Per-scenario (sorted by delta)\n",
            "| Scenario | ON median | OFF median | Delta | Noisy |",
            "|---|---|---|---|---|",
        ]
        for s in sorted(self.per_scenario, key=lambda x: x.delta, reverse=True):
            flag = "🔊" if s.noisy else "—"
            lines.append(
                f"| {s.scenario_id} | {s.on_median:.2f} | {s.off_median:.2f} | "
                f"{s.delta:+.2f} | {flag} |"
            )
        return "\n".join(lines) + "\n"

    def write(self, out_dir: Path | str) -> None:
        """Write multi_seed_report.json + .md to ``out_dir`` (top level)."""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "multi_seed_report.json").write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (out / "multi_seed_report.md").write_text(self.to_markdown(), encoding="utf-8")
