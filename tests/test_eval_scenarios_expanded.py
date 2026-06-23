"""Schema validation for the 10 expanded-corpus scenarios (B-060 GAIA gaps).

Pins the router+executor contract (D-028): every expected_tools entry must be a
tool the MAIN agent actually has (read_file, list_files, search_files,
excel_inspect, dispatch_subagent, web_fetch, read_image, memory_store,
memory_recall). Execution tools (table_query, write_file, convert_format, ...)
live inside subagents and must NOT appear directly in expected_tools — scenarios
that need them use dispatch_subagent + expected_subagent instead.
"""

from __future__ import annotations

from corpclaw_lite.eval.scenarios import load_scenarios
from corpclaw_lite.paths import PROJECT_ROOT

# Tools the MAIN agent has (D-028). Any expected_tools entry outside this set
# is a contract violation — the tool is subagent-only.
_MAIN_AGENT_TOOLS = frozenset(
    {
        "read_file",
        "list_files",
        "search_files",
        "excel_inspect",
        "dispatch_subagent",
        "web_fetch",
        "read_image",
        "memory_store",
        "memory_recall",
    }
)

_EXPANDED_IDS = {
    "read_file_missing_use_list_files",
    "read_image_switch_from_read_file",
    "dispatch_bad_subagent_recovers",
    "memory_store_then_confirm",
    "memory_multi_fact_accumulation",
    "memory_recall_persistence",
    "concise_direct_numeric_answer",
    "no_unnecessary_explanation",
    "image_chart_value_extraction",
    "image_table_cell_extraction",
}


def test_all_10_expanded_scenarios_present() -> None:
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    ids = {s.id for s in scenarios}
    missing = _EXPANDED_IDS - ids
    assert not missing, f"missing expanded scenarios: {missing}"


def test_expanded_expected_tools_are_main_agent_only() -> None:
    """No expanded scenario asks for a subagent-only execution tool directly."""
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    expanded = [s for s in scenarios if s.id in _EXPANDED_IDS]
    assert len(expanded) == 10
    for sc in expanded:
        for turn in sc.turns:
            for tool in turn.expected_tools:
                assert tool in _MAIN_AGENT_TOOLS, (
                    f"scenario '{sc.id}' expects '{tool}' which is not a "
                    f"main-agent tool (D-028). Use dispatch_subagent + "
                    f"expected_subagent instead."
                )


def test_vision_scenarios_have_generated_images() -> None:
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    vision = [s for s in scenarios if s.category == "vision"]
    assert len(vision) == 2
    for sc in vision:
        assert sc.setup is not None, f"vision scenario {sc.id} has no setup"
        assert sc.setup.generated_images, f"vision scenario {sc.id} must declare generated_images"
        for _dest, generator_id in sc.setup.generated_images:
            assert generator_id in {"bar_chart_42", "table_2x2"}, (
                f"unknown generator id '{generator_id}' in {sc.id}"
            )


def test_memory_scenarios_use_memory_tools() -> None:
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    memory = [s for s in scenarios if s.category == "memory"]
    assert len(memory) == 3
    for sc in memory:
        # At least one turn must reference memory_store or memory_recall.
        has_memory_tool = any(
            "memory_store" in t.expected_tools or "memory_recall" in t.expected_tools
            for t in sc.turns
        )
        assert has_memory_tool, f"memory scenario {sc.id} uses no memory tool"


def test_error_recovery_scenarios_have_success_criteria() -> None:
    """Error recovery scenarios must encode the expected recovery in
    success_criteria so the judge can score the error_recovery dimension."""
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    er = [s for s in scenarios if s.category == "error_recovery"]
    assert len(er) == 3
    for sc in er:
        for turn in sc.turns:
            assert turn.success_criteria, f"error_recovery turn in {sc.id} has no success_criteria"


def test_all_seven_rubric_dimensions_covered() -> None:
    """The expanded corpus must exercise all 7 judge rubric dimensions via at
    least one scenario category that targets it."""
    scenarios = load_scenarios(PROJECT_ROOT / "config" / "eval_scenarios.yaml")
    categories = {s.category for s in scenarios}
    # Each category maps to at least one rubric dimension it primarily exercises.
    required = {
        "office_aggregation",  # correctness, tool_selection (via delegation)
        "office_convert",
        "error_recovery",  # error_recovery
        "memory",  # context_retention
        "personality",  # personality
        "vision",  # tool_selection (read_image), correctness
        "loop_pressure",  # efficiency (B-055 dedup)
        "planning_text_pressure",  # efficiency (B-056)
        "adversarial_null_answer",  # correctness (no hallucination)
    }
    missing = required - categories
    assert not missing, f"categories missing coverage for rubric dims: {missing}"
