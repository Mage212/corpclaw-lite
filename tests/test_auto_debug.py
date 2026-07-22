from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_SCRIPT = Path(__file__).parents[1] / "scripts" / "auto_debug.py"
_SPEC = importlib.util.spec_from_file_location("corpclaw_auto_debug", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_parse_args = _MODULE._parse_args
_quality_exit_code = _MODULE._quality_exit_code


def test_auto_debug_defaults_do_not_enable_corpus_or_approvals(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["auto_debug.py"])

    args = _parse_args()

    assert args.corpus is None
    assert args.auto_approve_high_risk is False
    assert args.min_pass_rate is None


def test_quality_threshold_is_optional_and_enforced() -> None:
    report = SimpleNamespace(pass_rate=0.75)

    assert _quality_exit_code(report, None) == 0
    assert _quality_exit_code(report, 0.75) == 0
    assert _quality_exit_code(report, 0.8) == 1
    assert _quality_exit_code(report, 1.1) == 2


def test_quality_threshold_reads_guards_on_for_ab_report() -> None:
    report = SimpleNamespace(guards_on=SimpleNamespace(pass_rate=0.9))

    assert _quality_exit_code(report, 0.85) == 0
