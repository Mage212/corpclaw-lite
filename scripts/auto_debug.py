"""Standalone auto-debug runner — запускает EvalLoop и выводит детальный трейс.

Использование:
    uv run python scripts/auto_debug.py
    uv run python scripts/auto_debug.py --scenarios config/debug_scenarios.yaml
    uv run python scripts/auto_debug.py --output reports/debug

Переменные окружения:
    CORPCLAW_ALLOW_HOST_TOOLS=1   — обязательно (dev-режим без контейнера)
    (другие env из .env загружаются автоматически)

Отчёты пишутся в:
    reports/debug/eval_report.json
    reports/debug/eval_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# ── Загружаем .env ПЕРВЫМ — до любых project-imports ─────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent
_DOTENV = _PROJECT_ROOT / ".env"
if _DOTENV.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path=_DOTENV, override=False)
    except ImportError:
        pass  # dotenv опционален

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Auto-debug harness: run debug_scenarios.yaml through EvalLoop"
    )
    parser.add_argument(
        "--scenarios",
        default=str(_PROJECT_ROOT / "config" / "debug_scenarios.yaml"),
        help="Path to scenarios YAML (default: config/debug_scenarios.yaml)",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="Optional path to an external fixture corpus",
    )
    parser.add_argument(
        "--output",
        default=str(_PROJECT_ROOT / "reports" / "debug"),
        help="Directory for eval reports (default: reports/debug)",
    )
    parser.add_argument(
        "--ab",
        action="store_true",
        default=False,
        help="Run A/B comparison (guards on vs off). Default: single-pass (guards on)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Show full trajectories for ALL turns, not just failed",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="Override the evaluation workspace directory",
    )
    parser.add_argument(
        "--auto-approve-high-risk",
        action="store_true",
        help="Explicitly approve high-risk tools in this headless run",
    )
    parser.add_argument(
        "--min-pass-rate",
        type=float,
        default=None,
        help="Exit non-zero when pass rate is below this 0..1 threshold",
    )
    parser.add_argument(
        "--judge",
        nargs="?",
        const="cloud",
        default="cloud",
        metavar="PROVIDER",
        help="Enable the LLM judge using PROVIDER (default: cloud). "
        "Pass --judge none to disable (deterministic-only fallback).",
    )
    parser.add_argument(
        "--judge-ensemble",
        type=int,
        default=1,
        help="Number of judge calls per turn (median, default 1)",
    )
    return parser.parse_args()


def _build_judge(provider_name: str, ensemble: int):
    """Build an LLMJudge from a named provider in the env registry.

    Reads the ``task_kind: eval`` routing rule to get the model, then constructs
    a standalone provider via ProviderRegistry + build_provider. Returns None
    (deterministic-only) when the provider is not registered or lacks an API key.
    """
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.config.providers import ProviderRegistry
    from corpclaw_lite.llm.router import build_provider

    settings = load_settings(_PROJECT_ROOT / "config" / "settings.yaml")
    registry = ProviderRegistry.from_env()
    conn = registry.get(provider_name)
    if conn is None:
        print(
            f"⚠️  Judge provider '{provider_name}' not registered"
            " — using deterministic-only scoring."
        )
        return None

    # Resolve the model from the eval routing rule.
    model = "glm-5.2"
    for rule in settings.llm.routing:
        if getattr(rule, "task_kind", None) == "eval":
            model = rule.model
            break

    provider = build_provider(conn, model)
    if provider is None:
        print(
            f"⚠️  Judge provider '{provider_name}' could not be built"
            " (missing API key?) — deterministic-only."
        )
        return None

    from corpclaw_lite.eval.judge import LLMJudge

    print(f"⚖️  Judge enabled: provider={provider_name} model={model} ensemble={ensemble}")
    return LLMJudge(provider=provider, ensemble=ensemble)


def _load_scenarios_safe(scenarios_path: Path) -> list:
    """Load scenarios, returning [] if corpus is empty."""
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
    from corpclaw_lite.eval.scenarios import load_scenarios

    try:
        return load_scenarios(scenarios_path)
    except (FileNotFoundError, ValueError) as e:
        if "No scenarios found" in str(e) or not scenarios_path.exists():
            return []
        raise


def _print_report(report: object, verbose: bool = False) -> None:
    """Print a PassReport with full trajectory traces for failed turns."""
    passed = getattr(report, "passed", 0)
    total = getattr(report, "total", 0)
    pass_rate = getattr(report, "pass_rate", 0.0)
    mean_overall = getattr(report, "mean_overall", 0.0)
    mean_correctness = getattr(report, "mean_correctness", 0.0)
    scenario_scores = getattr(report, "scenario_scores", [])

    print(f"\n{'═' * 64}")
    print(f"РЕЗУЛЬТАТЫ: {passed}/{total} сценариев прошло ({pass_rate:.0%})")
    print(f"  mean overall:     {mean_overall:.2f}")
    print(f"  mean correctness: {mean_correctness:.2f}")
    print(f"{'═' * 64}")

    for sc in scenario_scores:
        status = "✅" if sc.passed else "❌"
        print(f"\n{status} [{sc.scenario_id}]  overall={sc.overall_score:.2f}")

        for t_idx, turn in enumerate(sc.turns):
            turn_status = "✅" if turn.passed else "❌"
            print(f"   Turn {t_idx + 1}: {turn_status} overall={turn.overall_score:.2f}")

            tools = getattr(turn, "tools_called", [])
            if tools:
                print(f"   Tools:    {tools}")

            answer = getattr(turn, "final_answer", "")
            if answer:
                preview = answer[:300].replace("\n", "\\n")
                print(f"   Answer:   {preview}{'...' if len(answer) > 300 else ''}")

            if not turn.passed:
                reasoning = getattr(turn, "reasoning", "")
                if reasoning:
                    print(f"   Reason:   {reasoning}")

            show_trajectory = verbose or not turn.passed
            transcript = getattr(turn, "transcript", "")
            if show_trajectory and transcript:
                print("\n   ── TRAJECTORY ───────────────────────────────────────")
                # Отступ для читаемости
                for line in transcript.splitlines():
                    print(f"   {line}")
                print("   ─────────────────────────────────────────────────────")

    print()


def _quality_exit_code(report: object, threshold: float | None) -> int:
    """Return a CI-style exit code when an explicit quality threshold is set."""
    if threshold is None:
        return 0
    if not 0.0 <= threshold <= 1.0:
        return 2
    measured = getattr(report, "pass_rate", None)
    if measured is None:
        measured = getattr(getattr(report, "guards_on", None), "pass_rate", 0.0)
    return 0 if float(measured) >= threshold else 1


async def _run(args: argparse.Namespace) -> int:
    """Main async entry point. Returns exit code (0 = harness OK, 1 = error)."""
    scenarios_path = Path(args.scenarios)
    # Resolve paths before EvalRunner chdir() into the per-pass workspace —
    # relative corpus/output paths would otherwise resolve against the wrong cwd.
    corpus_dir = Path(args.corpus).expanduser().resolve() if args.corpus else None
    output_dir = Path(args.output).expanduser().resolve()
    workspace_override = (
        args.workspace.expanduser().resolve() if args.workspace is not None else None
    )
    scenarios_path = scenarios_path.expanduser().resolve()

    scenarios = _load_scenarios_safe(scenarios_path)
    if not scenarios:
        print(
            f"ℹ️  Нет сценариев в {scenarios_path}\n   Добавьте сценарии в файл и запустите снова."
        )
        return 0

    print(f"🚀 Запуск {len(scenarios)} сценариев из {scenarios_path}")
    print(f"   Corpus:  {corpus_dir or 'generated fixtures / none'}")
    print(f"   Reports: {output_dir}")
    print(f"   Mode:    {'A/B (guards on/off)' if args.ab else 'single-pass (guards on)'}")

    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
    sys.path.insert(0, str(_PROJECT_ROOT / "tests"))
    from corpclaw_lite.eval.loop import EvalLoop

    # Build the LLM judge from the cloud provider (unless --judge none).
    judge_raw = args.judge
    judge_provider = judge_raw if judge_raw and judge_raw.lower() not in ("none", "off") else None
    judge = _build_judge(judge_provider, args.judge_ensemble) if judge_provider else None

    output_dir.mkdir(parents=True, exist_ok=True)

    ev = EvalLoop(
        scenarios_path=scenarios_path,
        corpus_dir=corpus_dir if corpus_dir and corpus_dir.exists() else None,
        output_dir=output_dir,
        ab_guards=args.ab,
        judge=judge,
        # Cross-topic calibrated few-shots (weather, etc.) contaminate debug runs.
        inject_few_shots=False,
        workspace_base=workspace_override,
        auto_approve_high_risk=args.auto_approve_high_risk,
    )

    report = await ev.run()

    # PassReport for single-pass; ABReport/MultiSeedReport for A/B.
    # _print_report works with any object that has scenario_scores + summary attrs.
    if args.ab:
        # ABReport: print both passes
        on_report = getattr(report, "guards_on", None)
        off_report = getattr(report, "guards_off", None)
        if on_report:
            print("\n── Pass: guards ON ─────────────────────────────────────")
            _print_report(on_report, verbose=args.verbose)
        if off_report:
            print("\n── Pass: guards OFF ────────────────────────────────────")
            _print_report(off_report, verbose=args.verbose)
        verdict = getattr(report, "verdict", "unknown")
        delta = getattr(report, "pass_rate_delta", 0.0)
        print(f"\n🏁 Verdict: {verdict}  (pass rate delta: {delta:+.0%})")
    else:
        _print_report(report, verbose=args.verbose)

    report_json = output_dir / "eval_report.json"
    if report_json.exists():
        print(f"📄 JSON:  {report_json}")
        print(f"📝 MD:    {output_dir / 'eval_report.md'}")

    quality_exit = _quality_exit_code(report, args.min_pass_rate)
    if quality_exit:
        if quality_exit == 2:
            print("Error: --min-pass-rate must be between 0 and 1")
            return 2
        measured = getattr(report, "pass_rate", None)
        if measured is None:
            measured = getattr(getattr(report, "guards_on", None), "pass_rate", 0.0)
        print(f"Pass rate {float(measured):.0%} is below {args.min_pass_rate:.0%}")
        return 1

    return 0


def main() -> None:
    args = _parse_args()

    from corpclaw_lite.config.loader import load_settings
    from corpclaw_lite.logging.payload import setup_payload_logging

    # Init payload logging for debugging
    settings = load_settings()
    setup_payload_logging(
        log_dir="logs",
        enabled=settings.logging.capture_enabled,
        fields=settings.logging.capture_fields,
    )

    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
