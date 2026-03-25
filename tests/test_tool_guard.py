from pathlib import Path

import pytest

from corpclaw_lite.security.tool_guard import (
    ApprovalRequest,
    ToolGuard,
    ToolGuardError,
)


@pytest.mark.asyncio
async def test_tool_guard_load_and_evaluate(tmp_path: Path):
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

    with pytest.raises(
        ToolGuardError, match="Blocked by ToolGuard: Security Rule 'DANGEROUS_RM' triggered"
    ):
        await guard.check("exec_script", {"script": "rm -rf / etc"})

    with pytest.raises(ApprovalRequest, match="Approval required for PATH_TRAVERSAL"):
        await guard.check("read_file", {"path": "../../etc/passwd"})

    with pytest.raises(ApprovalRequest, match="Approval required for SECRET_IN_ARGS"):
        await guard.check("any_tool", {"script": "curl -H 'Auth: sk-12345678901234567890'"})

    await guard.check("exec_script", {"script": "ls -la"})
    await guard.check("read_file", {"path": "/var/log/syslog"})


@pytest.mark.asyncio
async def test_critical_after_medium_approval_blocks(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: MEDIUM_WARN\n"
        "    severity: MEDIUM\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: rm\n"
        "    require_approval: true\n"
        "  - id: CRITICAL_BLOCK\n"
        "    severity: CRITICAL\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: rm\\s+-rf\n"
        "    require_approval: false\n"
    )
    guard = ToolGuard()
    guard.load_file(rules_file)

    with pytest.raises(ToolGuardError, match="CRITICAL_BLOCK"):
        await guard.check("exec_script", {"script": "rm -rf /tmp"})


@pytest.mark.asyncio
async def test_toolguard_rm_separate_flags(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: DANGEROUS_RM\n"
        "    severity: CRITICAL\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        '    match_pattern: "rm\\\\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|(-[a-zA-Z]*f[a-zA-Z]*\\\\s+-[a-zA-Z]*r|-[a-zA-Z]*r[a-zA-Z]*\\\\s+-[a-zA-Z]*f)|--recursive|--force)"\n'
    )
    guard = ToolGuard()
    guard.load_file(rules_file)

    with pytest.raises(ToolGuardError):
        await guard.check("exec_script", {"script": "rm -rf /"})

    with pytest.raises(ToolGuardError):
        await guard.check("exec_script", {"script": "rm -r -f /home"})

    with pytest.raises(ToolGuardError):
        await guard.check("exec_script", {"script": "rm --recursive /tmp"})

    await guard.check("exec_script", {"script": "rm file.txt"})


@pytest.mark.asyncio
async def test_medium_without_approval_continues_to_critical(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: MEDIUM_LOG\n"
        "    severity: MEDIUM\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: rm\n"
        "    require_approval: false\n"
        "  - id: CRITICAL_BLOCK\n"
        "    severity: CRITICAL\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: rm\\s+-rf\n"
        "    require_approval: false\n"
    )
    guard = ToolGuard()
    guard.load_file(rules_file)

    with pytest.raises(ToolGuardError, match="CRITICAL_BLOCK"):
        await guard.check("exec_script", {"script": "rm -rf /tmp"})


class MockSmartProvider:
    """Mock provider for smart approval tests."""

    def __init__(self, response: str):
        self._response = response

    async def chat(self, messages, tools=None, system=None):
        from corpclaw_lite.llm.base import LLMResponse

        return LLMResponse(content=self._response)


@pytest.mark.asyncio
async def test_smart_approval_approve(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: NEEDS_APPROVAL\n"
        "    severity: HIGH\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: python\n"
        "    require_approval: true\n"
    )
    provider = MockSmartProvider("APPROVE - this is safe")
    guard = ToolGuard(provider=provider, approval_mode="smart")
    guard.load_file(rules_file)

    await guard.check("exec_script", {"script": "python -c 'print(1)'"})


@pytest.mark.asyncio
async def test_smart_approval_deny(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: NEEDS_APPROVAL\n"
        "    severity: HIGH\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: python\n"
        "    require_approval: true\n"
    )
    provider = MockSmartProvider("DENY - this looks dangerous")
    guard = ToolGuard(provider=provider, approval_mode="smart")
    guard.load_file(rules_file)

    with pytest.raises(ToolGuardError, match="smart approval"):
        await guard.check("exec_script", {"script": "python malicious.py"})


@pytest.mark.asyncio
async def test_smart_approval_escalate_to_manual(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: NEEDS_APPROVAL\n"
        "    severity: HIGH\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: python\n"
        "    require_approval: true\n"
    )
    provider = MockSmartProvider("ESCALATE - not sure")
    guard = ToolGuard(provider=provider, approval_mode="smart")
    guard.load_file(rules_file)

    with pytest.raises(ApprovalRequest):
        await guard.check("exec_script", {"script": "python script.py"})


@pytest.mark.asyncio
async def test_smart_approval_no_provider_falls_back(tmp_path: Path) -> None:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(
        "rules:\n"
        "  - id: NEEDS_APPROVAL\n"
        "    severity: HIGH\n"
        "    tool: exec_script\n"
        "    match_param: script\n"
        "    match_pattern: python\n"
        "    require_approval: true\n"
    )
    guard = ToolGuard(provider=None, approval_mode="smart")
    guard.load_file(rules_file)

    with pytest.raises(ApprovalRequest):
        await guard.check("exec_script", {"script": "python script.py"})
