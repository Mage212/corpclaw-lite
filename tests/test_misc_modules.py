"""Additional coverage tests for targeted uncovered modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.channels.cli import CLIChannel
from corpclaw_lite.departments.manager import DepartmentConfig, DepartmentManager
from corpclaw_lite.users.models import User

# ── DepartmentManager ─────────────────────────────────────────────────────────


def test_department_manager_load_file(tmp_path: Path) -> None:
    """load_file() parses departments.yaml and creates DepartmentConfig."""
    config_file = tmp_path / "departments.yaml"
    config_file.write_text(
        "departments:\n"
        "  engineering:\n"
        "    description: Engineering\n"
        "    allowed_tools: ['*']\n"
        "    budget:\n"
        "      max_iterations: 20\n"
        "  marketing:\n"
        "    description: Marketing\n"
        "    allowed_tools: [read_file]\n",
        encoding="utf-8",
    )
    mgr = DepartmentManager()
    mgr.load_file(config_file)

    eng = mgr.get_department("engineering")
    assert eng is not None
    assert eng.allowed_tools == ["*"]
    assert eng.budget.max_iterations == 20

    mkt = mgr.get_department("marketing")
    assert mkt is not None
    assert mkt.allowed_tools == ["read_file"]


def test_department_manager_load_missing_file(tmp_path: Path) -> None:
    """load_file() handles missing file gracefully."""
    mgr = DepartmentManager()
    mgr.load_file(tmp_path / "nonexistent.yaml")
    assert mgr.get_department("anything") is None


def test_department_config_defaults() -> None:
    """DepartmentConfig with empty data uses sensible defaults."""
    cfg = DepartmentConfig({})
    assert cfg.name == "Unknown"
    assert cfg.allowed_tools == ["*"]
    assert cfg.budget.max_iterations == 15


# ── CLIChannel ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_channel_start_stop() -> None:
    """CLIChannel start/stop lifecycle."""
    ch = CLIChannel()
    await ch.start()
    await ch.stop()


@pytest.mark.asyncio
async def test_cli_channel_send_message() -> None:
    """send_message prints to console."""
    ch = CLIChannel()
    user = User(id=0, name="test", department="default")
    await ch.send_message(user, "Hello!")
    # No assertion needed — confirms no exceptions


@pytest.mark.asyncio
async def test_cli_channel_send_file(tmp_path: Path) -> None:
    """send_file prints the file path."""
    ch = CLIChannel()
    user = User(id=0, name="test", department="default")
    f = tmp_path / "test.txt"
    f.write_text("content", encoding="utf-8")
    await ch.send_file(user, f, caption="test file")


# ── Container Policies ────────────────────────────────────────────────────────


def test_container_policies_build_docker_args() -> None:
    from corpclaw_lite.config.settings import ContainerSettings
    from corpclaw_lite.container.policies import build_docker_args

    settings = ContainerSettings()
    args = build_docker_args(user_id=123, settings=settings)
    assert args["name"] == "corpclaw_agent_123"
    assert args["detach"] is True
    assert "mem_limit" in args
    assert args["environment"]["CORPCLAW_USER_ID"] == "123"


def test_container_policies_with_network_policy() -> None:
    from corpclaw_lite.config.settings import ContainerSettings
    from corpclaw_lite.container.policies import build_docker_args
    from corpclaw_lite.security.network_policy import NetworkPolicy

    settings = ContainerSettings()
    policy = NetworkPolicy()
    args = build_docker_args(user_id=456, settings=settings, network_policy=policy)
    assert args["name"] == "corpclaw_agent_456"


# ── XML Tool Calling ──────────────────────────────────────────────────────────


def test_xml_tool_calling_no_tool_call() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

    result = parse_xml_tool_call("")
    assert result.status == "no_tool_call"


def test_xml_tool_calling_valid() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

    xml = (
        "<tool_call>\n"
        "<name>read_file</name>\n"
        '<arguments>{"path": "/tmp/test.txt"}</arguments>\n'
        "</tool_call>"
    )
    result = parse_xml_tool_call(xml)
    assert result.status == "valid"
    assert result.tool_call is not None
    assert result.tool_call.name == "read_file"
    assert result.tool_call.arguments == {"path": "/tmp/test.txt"}


def test_xml_tool_calling_multiple_standard_calls() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_calls

    xml = (
        '<tool_call><name>read_file</name><arguments>{"path": "a.txt"}</arguments></tool_call>'
        '<tool_call><name>search_files</name><arguments>{"query": "needle"}</arguments></tool_call>'
    )

    result = parse_xml_tool_calls(xml, allowed_tool_names={"read_file", "search_files"})

    assert result.status == "valid"
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].id == "xml-tool-call-1"
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[1].id == "xml-tool-call-2"
    assert result.tool_calls[1].arguments == {"query": "needle"}
    assert result.tool_call is None


def test_xml_tool_calling_multiple_qwen3_calls() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_calls

    xml = (
        "<tool_call><function=research_fetch_source>"
        "<parameter=url>https://example.com/a</parameter>"
        "<parameter=max_chars>30000</parameter>"
        "</function></tool_call>"
        "<tool_call><function=research_fetch_source>"
        "<parameter=url>https://example.com/b</parameter>"
        "<parameter=max_chars>30000</parameter>"
        "</function></tool_call>"
    )

    result = parse_xml_tool_calls(xml, allowed_tool_names={"research_fetch_source"})

    assert result.status == "valid"
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].name == "research_fetch_source"
    assert result.tool_calls[0].arguments["max_chars"] == 30000
    assert result.tool_calls[1].arguments["url"] == "https://example.com/b"


def test_xml_tool_calling_multiple_calls_are_all_or_nothing() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_calls

    xml = (
        '<tool_call><name>read_file</name><arguments>{"path": "a.txt"}</arguments></tool_call>'
        '<tool_call><name>unknown_tool</name><arguments>{"path": "b.txt"}</arguments></tool_call>'
    )

    result = parse_xml_tool_calls(xml, allowed_tool_names={"read_file"})

    assert result.status == "invalid_tool_name"
    assert result.tool_calls == ()


def test_xml_tool_calling_malformed() -> None:
    from corpclaw_lite.llm.xml_tool_calling import parse_xml_tool_call

    result = parse_xml_tool_call("<tool_call>broken")
    assert result.status == "malformed_xml"


def test_xml_build_fallback_system() -> None:
    from corpclaw_lite.llm.xml_tool_calling import build_xml_fallback_system

    result = build_xml_fallback_system(["read_file", "write_file"])
    assert "read_file" in result
    assert "write_file" in result


def test_xml_build_repair_prompt() -> None:
    from corpclaw_lite.llm.xml_tool_calling import build_xml_repair_prompt

    result = build_xml_repair_prompt("bad xml")
    assert "bad xml" in result


# ── Path Utils ──────────────────────────────────────────────────────────────


def test_resolve_container_path_workspace_prefix(tmp_path: Path) -> None:
    from corpclaw_lite.extensions.tools.builtin._path_utils import resolve_container_path

    user = User(id=1, name="test", department="hr", telegram_id=12345)
    result = resolve_container_path("/workspace/file.txt", tmp_path, user)
    assert "user_1" in str(result)
    assert "file.txt" in str(result)


def test_resolve_container_path_relative(tmp_path: Path) -> None:
    from corpclaw_lite.extensions.tools.builtin._path_utils import resolve_container_path

    user = User(id=1, name="test", department="hr", telegram_id=12345)
    result = resolve_container_path("report.xlsx", tmp_path, user)
    assert "user_1" in str(result)
    assert "report.xlsx" in str(result)


def test_resolve_container_path_traversal_relative(tmp_path: Path) -> None:
    from corpclaw_lite.extensions.tools.builtin._path_utils import resolve_container_path

    user = User(id=1, name="test", department="hr", telegram_id=12345)
    with pytest.raises(PermissionError, match="escapes user workspace"):
        resolve_container_path("../../etc/passwd", tmp_path, user)


def test_resolve_container_path_traversal_prefix(tmp_path: Path) -> None:
    from corpclaw_lite.extensions.tools.builtin._path_utils import resolve_container_path

    user = User(id=1, name="test", department="hr", telegram_id=12345)
    with pytest.raises(PermissionError, match="escapes user workspace"):
        resolve_container_path("/workspace/../../etc/shadow", tmp_path, user)


# ── Runtime Shutdown ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_signal_handlers_success() -> None:
    import asyncio
    from unittest.mock import MagicMock

    from corpclaw_lite.runtime.shutdown import install_signal_handlers

    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_add = loop.add_signal_handler
    calls: list[tuple[int, object]] = []
    mock_handler = MagicMock(side_effect=lambda sig, cb: calls.append((sig, cb)))
    loop.add_signal_handler = mock_handler  # type: ignore[assignment]

    try:
        install_signal_handlers(event)
        assert len(calls) == 2
    finally:
        loop.add_signal_handler = original_add  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_install_signal_handlers_windows_fallback() -> None:
    import asyncio
    from unittest.mock import patch

    from corpclaw_lite.runtime.shutdown import install_signal_handlers

    event = asyncio.Event()
    with patch("corpclaw_lite.runtime.shutdown.asyncio") as mock_asyncio:
        mock_asyncio.get_running_loop.side_effect = NotImplementedError
        install_signal_handlers(event)


# ── Container Policies — hardening (B-064) ──────────────────────────────────
# Hardening (cap_drop ALL + seccomp + explicit non-root user) is ON by default;
# strict_capabilities=False is the dev/debug opt-out.


def test_build_docker_args_hardened_by_default() -> None:
    """Default ContainerSettings() produces a hardened container (B-064)."""
    from corpclaw_lite.config.settings import ContainerSettings
    from corpclaw_lite.container.policies import build_docker_args

    settings = ContainerSettings()
    assert settings.strict_capabilities is True  # default flipped in B-064
    args = build_docker_args(user_id=99, settings=settings)
    assert args["name"] == "corpclaw_agent_99"
    assert args["user"] == "agent"
    assert args["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in args["security_opt"]
    # seccomp profile is appended when the file exists on disk
    assert any(s.startswith("seccomp=") for s in args["security_opt"])


def test_build_docker_args_strict_capabilities_opt_out() -> None:
    """strict_capabilities=False skips cap_drop/seccomp/explicit-user (dev/debug)."""
    from corpclaw_lite.config.settings import ContainerSettings
    from corpclaw_lite.container.policies import build_docker_args

    settings = ContainerSettings(strict_capabilities=False)
    args = build_docker_args(user_id=99, settings=settings)
    assert args["name"] == "corpclaw_agent_99"
    # Hardening kwargs absent in opt-out mode (image still runs non-root via USER agent)
    assert "user" not in args
    assert "cap_drop" not in args
    assert not any(s.startswith("seccomp=") for s in args["security_opt"])
    # no-new-privileges is always on regardless of the flag
    assert "no-new-privileges:true" in args["security_opt"]
