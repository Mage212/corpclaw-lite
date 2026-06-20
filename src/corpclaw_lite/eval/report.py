"""Eval report aggregation and A/B comparison (B-060, step 6).

A :class:`PassReport` aggregates one run over the full corpus (guards on OR
off). :class:`ABReport` compares two passes (guards on vs off) and answers the
central B-060 question: did the Phase 0 guards improve outcomes? Both render to
JSON and Markdown for human review.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corpclaw_lite.eval.scores import ScenarioScore

__all__ = [
    "ABReport",
    "PassReport",
    "ScenarioDelta",
]


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
