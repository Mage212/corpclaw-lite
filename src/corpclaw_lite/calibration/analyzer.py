"""Calibration analyzer — send failures to a cloud model for config improvement proposals."""

from __future__ import annotations

import json
import logging
from typing import Any

from corpclaw_lite.calibration.scorer import ScenarioResult
from corpclaw_lite.llm.base import Provider

__all__ = [
    "CalibrationAnalyzer",
]

logger = logging.getLogger(__name__)

_ANALYSIS_SYSTEM = (
    "You are a precise AI agent engineering assistant. "
    "You analyse agent failures and propose configuration changes. "
    "Respond ONLY with valid JSON — no markdown, no commentary outside the JSON object."
)

_ANALYSIS_PROMPT = """You are analysing how a small local LLM ({model_id}) performs as a
tool-calling AI agent. Below are FAILED and PASSED scenarios.

Your job: propose configuration changes so the local model passes more scenarios.

## What you can change

1. **SYSTEM PROMPT** — rewrite SOUL.md / BEHAVIOR.md to be clearer for a small model:
   - Use short, direct sentences
   - Add explicit ALWAYS/NEVER rules
   - Remove abstract phrasing
2. **TOOL DESCRIPTIONS** — rewrite tool name descriptions and parameter descriptions
   to be simpler and more explicit for a small model.
3. **FEW-SHOT EXAMPLES** — add example user→tool_call pairs so the model learns
   the pattern by example. This is the most powerful lever for small models.
4. **AGENT SETTINGS** — adjust numeric parameters (max_steps, max_history, etc.)

## Current Configuration

### System Prompt
{system_prompt}

### Tool Schemas
{tool_schemas}

### Current Few-Shot Examples
{current_few_shots}

## Failed Scenarios
{failures_json}

## Passed Scenarios (DO NOT break these)
{passed_json}

## Response Format

Return a JSON object:
{{
  "reasoning": "2-3 sentence analysis of the failure patterns",
  "changes": {{
    "system_prompt": {{
      "SOUL.md": "full new content, or null to keep unchanged",
      "BEHAVIOR.md": "full new content, or null to keep unchanged"
    }},
    "tool_overrides": {{
      "tool_name": {{
        "description": "new simpler description",
        "params": {{
          "param_name": {{
            "description": "new simpler description"
          }}
        }}
      }}
    }},
    "few_shots": [
      {{
        "user": "example user message",
        "assistant": {{
          "tool_calls": [
            {{"name": "tool_name", "arguments": {{"param": "value"}}}}
          ]
        }}
      }},
      {{
        "user": "example where no tool is needed",
        "assistant": {{
          "content": "direct text answer"
        }}
      }}
    ],
    "settings": {{
      "max_steps": 20,
      "max_history": 10
    }}
  }}
}}

Only include sections you want to change. Omit sections you want to keep as-is.
"""


class CalibrationAnalyzer:
    """Send failure analysis to a cloud model and get proposed configuration changes."""

    def __init__(self, cloud_provider: Provider) -> None:
        self._provider = cloud_provider

    async def analyze(
        self,
        model_id: str,
        failed: list[ScenarioResult],
        passed: list[ScenarioResult],
        current_system_prompt: str,
        current_tool_schemas: list[dict[str, Any]],
        current_few_shots: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Ask cloud model to propose configuration changes.

        Args:
            model_id: Identifier of the local model being calibrated.
            failed: Scenarios that the local model failed.
            passed: Scenarios that the local model passed.
            current_system_prompt: Current system prompt text.
            current_tool_schemas: Current tool JSON schemas.
            current_few_shots: Currently configured few-shot examples.

        Returns:
            Dictionary with 'reasoning' and 'changes' keys.

        Raises:
            ValueError: If the cloud model returns unparseable JSON.
        """
        prompt = _ANALYSIS_PROMPT.format(
            model_id=model_id,
            system_prompt=current_system_prompt,
            tool_schemas=json.dumps(current_tool_schemas, indent=2, ensure_ascii=False),
            current_few_shots=json.dumps(current_few_shots or [], indent=2, ensure_ascii=False),
            failures_json=json.dumps([r.to_dict() for r in failed], indent=2, ensure_ascii=False),
            passed_json=json.dumps([r.to_dict() for r in passed], indent=2, ensure_ascii=False),
        )

        logger.info(
            "[calibration] Sending %d failures + %d passed to cloud model for analysis",
            len(failed),
            len(passed),
        )

        response = await self._provider.chat(
            messages=[{"role": "user", "content": prompt}],
            system=_ANALYSIS_SYSTEM,
        )

        content = (response.content or "").strip()

        # Strip markdown code fence if present
        if content.startswith("```"):
            lines = content.split("\n")
            # Remove first line (```json) and last line (```)
            lines = [line for line in lines if not line.strip().startswith("```")]
            content = "\n".join(lines)

        try:
            result: dict[str, Any] = json.loads(content)
        except json.JSONDecodeError as e:
            logger.error("[calibration] Cloud model returned invalid JSON: %s", content[:500])
            raise ValueError(f"Cloud model returned invalid JSON: {e}") from e

        if "changes" not in result:
            raise ValueError(f"Cloud model response missing 'changes' key: {list(result.keys())}")

        reasoning = result.get("reasoning", "No reasoning provided")
        logger.info("[calibration] Cloud analysis: %s", reasoning)

        return result
