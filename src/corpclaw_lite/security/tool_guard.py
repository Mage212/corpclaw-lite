from __future__ import annotations

import logging
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import yaml

logger = logging.getLogger(__name__)


class RuleSeverity(StrEnum):
    INFO = "INFO"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ToolGuardError(Exception):
    """Raised when ToolGuard blocks a tool call."""


class ApprovalRequest(Exception):
    """Raised when an action requires explicit user approval via a channel."""

    def __init__(self, action: str, details: str):
        self.action = action
        self.details = details
        super().__init__(f"Approval required for {action}: {details}")


class GuardRule:
    """A single security rule for ToolGuard."""

    def __init__(self, data: dict[str, Any]):
        self.id: str = data.get("id", "UNKNOWN")
        self.description: str = data.get("description", "")
        self.severity: str = data.get("severity", RuleSeverity.INFO)
        self.tool: str = data.get("tool", "*")

        # Conditions (at least one must match if present)
        self.match_param: str | None = data.get("match_param")
        self.match_pattern: str | None = data.get("match_pattern")

        self.require_approval: bool = data.get("require_approval", False)

        self._regex = re.compile(self.match_pattern) if self.match_pattern else None

    def evaluate(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """Return True if this rule matches the tool call."""
        if self.tool != "*" and self.tool != tool_name:
            return False

        if self.match_param and self._regex:
            val = arguments.get(self.match_param)
            if isinstance(val, str) and self._regex.search(val):
                return True

        # If there are no specific matchers but the tool matched
        return not self.match_param and not self.match_pattern


_SEVERITY_RANK: dict[str, int] = {
    RuleSeverity.INFO: 0,
    RuleSeverity.MEDIUM: 1,
    RuleSeverity.HIGH: 2,
    RuleSeverity.CRITICAL: 3,
}


class ToolGuard:
    """Security guard that intercepts tool calls and applies YAML policies (CoPaw pattern)."""

    def __init__(self) -> None:
        self._rules: list[GuardRule] = []

    def load_file(self, path: Path | str) -> None:
        file_path = Path(path)
        if not file_path.exists():
            logger.warning("ToolGuard rules file not found: %s", file_path)
            return

        try:
            with open(file_path, encoding="utf-8") as f:
                data = cast(dict[str, Any], yaml.safe_load(f) or {})

            rules_data = cast(list[dict[str, Any]], data.get("rules", []))
            for r in rules_data:
                self._rules.append(GuardRule(r))

            logger.info("Loaded %d ToolGuard rules", len(self._rules))
        except Exception as e:
            logger.error("Failed to load ToolGuard rules from %s: %s", file_path, e)

    def check(self, tool_name: str, arguments: dict[str, Any]) -> None:
        """
        Evaluate ALL rules against the tool call, then apply the strictest matching action.

        Priority order:
        1. CRITICAL/HIGH without require_approval → unconditional ToolGuardError
        2. Any require_approval rule (highest severity of those) → ApprovalRequest
        3. MEDIUM/INFO without require_approval → log only, execution continues
        """
        matches = [r for r in self._rules if r.evaluate(tool_name, arguments)]
        if not matches:
            return

        # Log every match
        for rule in matches:
            msg = f"Security Rule '{rule.id}' triggered ({rule.severity}): {rule.description}"
            logger.warning("ToolGuard: %s for tool %s", msg, tool_name)

        # 1. Hard blocks — CRITICAL or HIGH without require_approval
        hard_blocks = [
            r
            for r in matches
            if r.severity in (RuleSeverity.CRITICAL, RuleSeverity.HIGH) and not r.require_approval
        ]
        if hard_blocks:
            worst = max(hard_blocks, key=lambda r: _SEVERITY_RANK.get(r.severity, 0))
            msg = f"Security Rule '{worst.id}' triggered ({worst.severity}): {worst.description}"
            raise ToolGuardError(f"Blocked by ToolGuard: {msg}")

        # 2. Approval requests — pick the highest-severity one
        approval_rules = [r for r in matches if r.require_approval]
        if approval_rules:
            worst = max(approval_rules, key=lambda r: _SEVERITY_RANK.get(r.severity, 0))
            msg = f"Security Rule '{worst.id}' triggered ({worst.severity}): {worst.description}"
            raise ApprovalRequest(action=worst.id, details=msg)

        # 3. MEDIUM/INFO without require_approval — already logged above, allow execution
