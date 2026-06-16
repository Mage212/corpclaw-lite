"""Tests for B-037: research-agent finalization (language, source grounding, recovery)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.tools.builtin.research import (
    ResearchListFactsTool,
    ResearchRuntime,
    detect_language,
)
from corpclaw_lite.logging import trace as trace_mod
from corpclaw_lite.logging.trace import setup_trace_logging
from corpclaw_lite.users.models import User


def _user() -> User:
    return User(id=7, telegram_id=7, name="Tester", department="engineering")


def _runtime(tmp_path: Path, *, strict: bool = True) -> ResearchRuntime:
    from corpclaw_lite.config.settings import ResearchSettings

    return ResearchRuntime(
        settings=ResearchSettings(finalize_strict=strict, deep_max_sources=5),
        workspace_base=tmp_path,
    )


def _add_source(
    runtime: ResearchRuntime,
    user: User,
    run_id: str,
    url: str,
    body: str,
    title: str,
) -> dict[str, object]:
    return runtime.store_source(
        user,
        run_id,
        url,
        f"url: {url}\nstatus: 200\nsize: 10\n---\n{title}\n{body}",
    )


def _read_trace(tmp_path: Path) -> list[dict[str, object]]:
    trace = tmp_path / "agent_trace.jsonl"
    if not trace.exists():
        return []
    return [
        json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


@pytest.fixture(autouse=True)
def _reset_trace_logger() -> None:
    yield
    trace_mod._trace_logger = None


# ---------------------------------------------------------------- language


def test_detect_language_cyrillic_threshold() -> None:
    assert detect_language("Проведи глубокое исследование") == "ru"
    assert detect_language("Conduct a deep research on quantization") == "en"
    assert detect_language("") == "en"
    # mixed but Cyrillic-dominant stays ru
    assert detect_language("Сравни GPTQ и AWQ квантование моделей") == "ru"


def test_ru_skeleton_uses_russian_headings(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="ru")
    report = runtime.finalize_report(user, "r1", "deep_research", "")
    assert "## Краткий вывод" in report
    assert "## Методология исследования" in report


def test_en_skeleton_uses_english_headings(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    report = runtime.finalize_report(user, "r1", "deep_research", "")
    assert "## Executive summary" in report
    assert "## Methodology" in report


# ---------------------------------------------------- strict validation


def test_strict_rejects_english_answer_for_ru_task(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="ru")
    answer = "## Executive summary\nQuantization reduces inference cost."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "Russian" in result


def test_strict_rejects_invented_source_ids(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    # 12-hex source id that does not exist in the manifest
    answer = "## Summary\nResult [abcdef123456] shows cost reduction."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "abcdef123456" in result


def test_strict_rejects_invented_urls(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    answer = "## Summary\nSee https://never-fetched.example.com/x for details."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "URL" in result


def test_strict_requires_list_facts_in_deep_research(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    answer = "## Executive summary\nFindings about KV cache."
    result = runtime.finalize_report(user, "r1", "deep_research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "research_list_facts" in result


def test_strict_rejects_inflated_source_count(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    _add_source(runtime, user, "r1", "https://ex.com/a", "Evidence A.", "Title A")
    # only 1 source actually fetched
    answer = "## Summary\nIdentified 10 sources for this analysis."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert result.startswith("Error: research_finalize_validation_failed")
    assert "10" in result


def test_strict_accepts_well_grounded_answer(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    src = _add_source(runtime, user, "r1", "https://ex.com/a", "Evidence A.", "Title A")
    sid = str(src["source_id"])
    answer = f"## Summary\nCost reduced [{sid}] per the cited source."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert not result.startswith("Error")
    assert "## Summary" in result


# --------------------------------------------------------- hybrid recovery


def test_skeleton_fallback_after_max_attempts(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    # deep_research without list_facts -> always fails validation
    bad = "## Executive summary\nNo list_facts call."
    r1 = runtime.finalize_report(user, "r1", "deep_research", bad)
    assert r1.startswith("Error: research_finalize_validation_failed")
    r2 = runtime.finalize_report(user, "r1", "deep_research", bad)
    assert r2.startswith("Error: research_finalize_validation_failed")
    # 3rd attempt: backstop -> deterministic skeleton instead of another Error
    r3 = runtime.finalize_report(user, "r1", "deep_research", bad)
    assert not r3.startswith("Error")
    assert "## Executive summary" in r3  # en skeleton


def test_soft_mode_warns_but_returns_report(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "research", language="ru")
    answer = "## Executive summary\nEnglish answer despite ru task."
    result = runtime.finalize_report(user, "r1", "research", answer)
    assert not result.startswith("Error")
    assert "## Executive summary" in result  # original answer preserved


# ------------------------------------------------------------- list_facts


@pytest.mark.asyncio()
async def test_list_facts_records_flag(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "research", language="ru")
    tool = ResearchListFactsTool(runtime)
    await tool.execute(user=user, run_id="r1")
    state = runtime._read_state(runtime.run_dir(user, "r1"))
    assert state.get("list_facts_called") is True


# --------------------------------------------------------- task_context


def test_target_language_injected_for_ru_task() -> None:
    from corpclaw_lite.agent.subagent import _prepare_research_task_context

    spec = SubagentSpec(id="research-agent", name="Research", description="d")
    ctx = _prepare_research_task_context(spec, "Проведи глубокое исследование квантования")
    assert "Target language: ru" in ctx


def test_target_language_injected_for_en_task() -> None:
    from corpclaw_lite.agent.subagent import _prepare_research_task_context

    spec = SubagentSpec(id="research-agent", name="Research", description="d")
    ctx = _prepare_research_task_context(spec, "Conduct deep research on quantization")
    assert "Target language: en" in ctx


def test_non_research_spec_keeps_context_unchanged() -> None:
    from corpclaw_lite.agent.subagent import _prepare_research_task_context

    spec = SubagentSpec(id="filesystem-agent", name="FS", description="d")
    ctx = _prepare_research_task_context(spec, "List files in workspace")
    assert "Target language" not in ctx


# ------------------------------------------------------------- trace


def test_strict_emits_validation_failed_trace(tmp_path: Path) -> None:
    setup_trace_logging(tmp_path, enabled=True)
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="ru")
    answer = "## Executive summary\nEnglish answer."
    runtime.finalize_report(user, "r1", "research", answer)
    events = [str(e.get("event")) for e in _read_trace(tmp_path)]
    assert "research_finalize_validation_failed" in events


def test_passed_emits_validation_passed_trace(tmp_path: Path) -> None:
    setup_trace_logging(tmp_path, enabled=True)
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    answer = "## Summary\nGrounded answer with no claims."
    runtime.finalize_report(user, "r1", "research", answer)
    events = [str(e.get("event")) for e in _read_trace(tmp_path)]
    assert "research_finalize_validation_passed" in events


# ----------------------------------------------------- B-048: facts dedup


def test_skeleton_deep_research_does_not_duplicate_facts(tmp_path: Path) -> None:
    """The deep_research skeleton's 'Key findings' and 'Facts and evidence' sections
    previously rendered the same {facts} placeholder twice. They must now differ: the
    brief section omits the Evidence excerpt, the full section includes it.
    """
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    _add_source(runtime, user, "r1", "https://ex.test/a", "Body A", "Title A")
    runtime.store_fact(
        user,
        "r1",
        {
            "source_id": runtime.get_source(
                user, "r1", _source_id_of(runtime, "https://ex.test/a")
            ),
            "fact": "Quantum fact one",
            "evidence": "Evidence excerpt one",
            "confidence": "high",
            "relation": "supports",
        },
    )
    report = runtime.finalize_report(user, "r1", "deep_research", "")
    # Both section headings present...
    assert "## Key findings" in report
    assert "## Facts and evidence" in report
    # ...but the fact body is not duplicated verbatim under the two headings: the brief
    # line (no 'Evidence:') must differ from the full line.
    assert "Quantum fact one" in report
    assert "Evidence excerpt one" in report  # only in the full section
    # The brief 'Key findings' section must not carry the evidence excerpt.
    brief_section = report.split("## Facts and evidence")[0]
    assert "Evidence excerpt one" not in brief_section


def test_facts_markdown_brief_omits_evidence(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, strict=False)
    facts = [
        {
            "fact": "F1",
            "source_id": "abc",
            "evidence": "EV1",
            "confidence": "high",
            "relation": "supports",
        }
    ]
    brief = runtime._facts_markdown(facts, "en", with_evidence=False)
    full = runtime._facts_markdown(facts, "en", with_evidence=True)
    assert "Evidence: EV1" not in brief
    assert "Evidence: EV1" in full
    assert "F1" in brief and "F1" in full


# --------------------------------------------------------- B-045: interrupted


def test_interrupted_report_carries_banner_and_no_fake_analysis(tmp_path: Path) -> None:
    """A timeout partial-handoff (interrupted=True) must be honestly marked and must
    NOT emit the analysis sections (Contradictions / Hypotheses / Recommendations) that
    a finished deep_research report promises, because no synthesis was performed.
    """
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="ru")
    src = _add_source(runtime, user, "r1", "https://ex.test/a", "Body A", "Title A")
    runtime.store_fact(
        user,
        "r1",
        {
            "source_id": src["source_id"],
            "fact": "Факт раз",
            "evidence": "Доказательство",
            "confidence": "high",
            "relation": "supports",
        },
    )
    report = runtime.finalize_report(user, "r1", "deep_research", "", interrupted=True)
    # Honest banner present (ru).
    assert "прервано" in report.casefold()
    # Honest limitation line present.
    assert "Синтез не выполнен" in report
    # Gathered fact still surfaced.
    assert "Факт раз" in report
    # The fake analysis sections are NOT present.
    assert "Противоречия" not in report
    assert "Гипотезы" not in report
    assert "Практические рекомендации" not in report
    # Source still cited.
    assert "https://ex.test/a" in report


def test_interrupted_report_english(tmp_path: Path) -> None:
    user = _user()
    runtime = _runtime(tmp_path, strict=False)
    runtime.initialize_run_mode(user, "r1", "research", language="en")
    _add_source(runtime, user, "r1", "https://ex.test/a", "Body A", "Title A")
    report = runtime.finalize_report(user, "r1", "research", "", interrupted=True)
    assert "interrupted" in report.casefold()
    assert "No synthesis" in report


def test_interrupted_skips_validation(tmp_path: Path) -> None:
    """interrupted=True short-circuits before grounding validation — it builds the
    skeleton directly, so a strict runtime does NOT emit validation_failed traces."""
    setup_trace_logging(tmp_path, enabled=True)
    user = _user()
    runtime = _runtime(tmp_path, strict=True)
    runtime.initialize_run_mode(user, "r1", "deep_research", language="en")
    runtime.finalize_report(user, "r1", "deep_research", "", interrupted=True)
    events = [str(e.get("event")) for e in _read_trace(tmp_path)]
    assert "research_finalize_validation_failed" not in events
    assert "research_finalize_validation_passed" not in events


def _source_id_of(runtime: ResearchRuntime, url: str) -> str:
    """Look up the source_id for a URL via the runtime (helper for fact seeding)."""
    src = runtime.find_source_by_url(_user(), "r1", url)
    assert src is not None
    return str(src.get("source_id") or "")
