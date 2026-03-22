from pathlib import Path

import pytest

from corpclaw_lite.security.tool_guard import (
    ApprovalRequest,
    ToolGuard,
    ToolGuardError,
)


def test_tool_guard_load_and_evaluate(tmp_path: Path):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: DANGEROUS_RM\n"
        "    severity: CRITICAL\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: rm\\s+-rf\\s+/\n"
        "  - id: PATH_TRAVERSAL\n"
        "    severity: HIGH\n"
        "    tool: read_file\n"
        "    match_param: path\n"
        "    match_pattern: \\.\\./\n"
        "    require_approval: true\n"
        "  - id: SECRET_IN_ARGS\n"
        "    severity: HIGH\n"
        "    match_param: script\n"
        "    match_pattern: (sk-[a-zA-Z0-9]{20,})\n"
        "    require_approval: true\n"
    )

    guard = ToolGuard()
    guard.load_file(rules_file)

    # 1. Block CRITICAL without approval
    with pytest.raises(ToolGuardError, match="Blocked by ToolGuard: Security Rule 'DANGEROUS_RM' triggered"):
        guard.check("exec_script", {"script": "rm -rf / etc"})

    # 2. Block HIGH with approval required
    with pytest.raises(ApprovalRequest, match="Approval required for PATH_TRAVERSAL"):
        guard.check("read_file", {"path": "../../etc/passwd"})

    # 3. Block SECRET globally regardless of tool (tool=*)
    with pytest.raises(ApprovalRequest, match="Approval required for SECRET_IN_ARGS"):
        guard.check("any_tool", {"script": "curl -H 'Auth: sk-12345678901234567890'"})

    # 4. Success for benign calls
    guard.check("exec_script", {"script": "ls -la"})
    guard.check("read_file", {"path": "/var/log/syslog"})
