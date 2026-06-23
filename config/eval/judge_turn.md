# Per-Turn Judge Instructions

After each agent response, evaluate using the rules below.

You are judging an office AI agent built around typed tools (excel_workbook,
normalize_excel, convert_format, table_query, read_file, list_files, write_file)
driven by a local LLM. The agent is given a scenario with an expected answer or
behaviour and a transcript of what it actually did (tool calls + final answer).

## STEP 1 — PRE-CHECK (mandatory, apply before scoring)

If ANY check fails: set correctness=0, completeness=0, tool_selection=0,
failure_category as noted.

1. **Garbled output**: Response is mostly `}`, `{`, brackets, or <3 readable
   words → failure_category=garbled_output
   - NOTE: a bare numeric answer (e.g. "3650") to a numeric question is VALID,
     not garbled.
2. **Raw JSON leak**: Response is a JSON blob with keys like `"chunks"`,
   `"scores"`, `"tool"`, `"result"` → failure_category=garbled_output
3. **Non-answer**: Response is completely off-topic or empty →
   failure_category=wrong_answer
4. **Tool-call artifact**: Response is ONLY a `[tool:X]` label, not prose →
   failure_category=garbled_output

## STEP 2 — AUTOMATIC ZERO RULES (apply only when `expected_answer` is non-null)

If the turn uses `expected_behavior` instead (i.e. expected_tools only, no
expected_answer), skip these rules and score correctness behaviorally
(10=optimal tool use, 7=correct with extra calls, 4=wrong tool but recovered,
0=wrong/missing).

If `expected_answer` is `null`, the ground truth asserts that NO specific answer
exists in the source. The agent should indicate the information is not
available. Saying "I don't know" or "the document doesn't mention this" =
correctness up to 10. Inventing a specific answer = correctness=0. The
lazy-refusal auto-zero rule does NOT apply for null expected_answer turns.

When `expected_answer` IS non-null, these force **correctness=0**:
- **Wrong number**: ground_truth has a number and response is **>5% off**
- **Wrong name**: ground_truth names a person/entity and response names a
  different one
- **Lazy refusal**: agent says "can't find" / "не могу найти" WITHOUT calling a
  query tool (read_file / table_query / list_files / excel_workbook) first
- **Hallucinated source**: agent claims a fact "from the document" that
  contradicts ground_truth

## STEP 3 — SCORE EACH DIMENSION (0-10)

- **correctness** (25%): Factual accuracy vs ground_truth.
  10=exact, 7=minor omissions, 4=partial, 0=wrong/hallucinated.
- **tool_selection** (20%): Right tools in right order.
  10=optimal, 7=correct+extra calls, 4=wrong tool but recovered, 0=wrong/missing.
- **context_retention** (20%): Used prior-turn info.
  10=perfect, 7=mostly, 4=missed key info, 0=ignored.
  - Cap at 4 if agent re-asks established info.
  - If prior turn failed, judge against ground_truth, not the failed response.
- **completeness** (15%): Fully answered all parts.
  10=complete, 7=mostly, 4=partial, 0=didn't answer.
- **efficiency** (10%): Steps vs optimal.
  10=optimal, 7=1-2 extra, 4=many redundant, **0=tool loop (3+ identical calls)**.
  - A run where the agent called the same tool with the same arguments ≥3 times
    (a detected loop) must score efficiency ≤ 4.
- **personality** (5%): Direct and confident, no sycophancy.
  10=concise+direct, 7=neutral/functional, 4=generic AI hedging, 0=sycophantic.
- **error_recovery** (5%): Handles tool failures gracefully.
  10=graceful, 7=recovered after retry, 4=partial, 0=gave up.

### Border-case determinations (apply exactly, no discretion)

These recurring situations have a fixed scoring floor so the judge does not
introduce variance across runs that behave identically:

- **File genuinely absent**: if the transcript shows the agent looked for a
  requested file (via read_file / list_files / search_files) and the file does
  NOT exist in the workspace, then reporting "file not found" is the correct
  outcome — NOT a failure. Score **correctness 9-10** (10 if it confirmed via
  list_files/search_files, 9 if it only tried read_file). Do NOT penalise for
  "not dispatching the subagent" when the input it would need is missing.
- **Null answer with context number**: if `expected_answer` is null and the
  agent stated the information is unavailable while quoting a number from the
  source as context (e.g. "no extra leave for tenure, only the standard 28
  days"), score **correctness 9-10**. The context number is not a
  hallucination.
- **Correct refusal of an impossible task**: if the scenario asks for something
  the agent genuinely cannot do (live data, real-time info, no matching
  subagent capability) and the agent honestly says so, score **correctness
  9-10** and **error_recovery 9-10**.

## STEP 4 — OVERALL SCORE AND PASS/FAIL

```
overall = correctness*0.25 + tool_selection*0.20 + context_retention*0.20
        + completeness*0.15 + efficiency*0.10 + personality*0.05
        + error_recovery*0.05
```

Pass/fail decision (apply in order):
1. FAIL if correctness=0
2. FAIL if correctness < 4
3. FAIL if overall_score < 6.0
4. PASS otherwise

## OUTPUT FORMAT

Return ONLY a JSON object (no markdown fences, no prose before/after):

```json
{
  "scores": {
    "correctness": N,
    "tool_selection": N,
    "context_retention": N,
    "completeness": N,
    "efficiency": N,
    "personality": N,
    "error_recovery": N
  },
  "overall_score": N.N,
  "pass": true,
  "failure_category": null,
  "reasoning": "1-2 sentence explanation"
}
```

Notes:
- Each score N is an integer 0-10.
- `failure_category` is one of: `garbled_output`, `wrong_answer`, `wrong_number`,
  `wrong_name`, `lazy_refusal`, `hallucinated_source`, `null`, or a custom
  short slug.
- `overall_score` is your computed weighted average to one decimal place. The
  harness recomputes it deterministically from your per-dimension scores, so be
  precise about the dimensions.
