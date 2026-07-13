"""Tests for DC-017 per-user workspace_root contextvar (B-098)."""

from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.agent.workspace_context import (
    get_workspace_root,
    reset_workspace_root,
    set_workspace_root,
)
from corpclaw_lite.security.path_validator import PermissionDenied, resolve_and_validate_path


def test_set_get_reset_workspace_root(tmp_path: Path) -> None:
    assert get_workspace_root() is None
    token = set_workspace_root(tmp_path)
    assert get_workspace_root() == tmp_path.resolve()
    reset_workspace_root(token)
    assert get_workspace_root() is None


def test_path_validator_uses_contextvar(tmp_path: Path) -> None:
    user_a = tmp_path / "user_a"
    user_b = tmp_path / "user_b"
    user_a.mkdir()
    user_b.mkdir()
    secret = user_b / "secret.txt"
    secret.write_text("nope", encoding="utf-8")

    token = set_workspace_root(user_a)
    try:
        # Relative path stays inside user_a
        ok = resolve_and_validate_path("notes.txt")
        assert ok == (user_a / "notes.txt").resolve()

        # Escape into sibling workspace must fail
        with pytest.raises(PermissionDenied, match="outside of workspace"):
            resolve_and_validate_path(str(secret))
        with pytest.raises(PermissionDenied, match="outside of workspace"):
            resolve_and_validate_path("../user_b/secret.txt")
    finally:
        reset_workspace_root(token)


def test_path_validator_falls_back_to_cwd_without_contextvar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert get_workspace_root() is None
    resolved = resolve_and_validate_path("local.txt")
    assert resolved == (tmp_path / "local.txt").resolve()


@pytest.mark.asyncio
async def test_exec_script_cwd_uses_workspace_root(tmp_path: Path) -> None:
    from corpclaw_lite.extensions.tools.builtin.exec_script import ExecScriptTool

    marker = tmp_path / "cwd_marker"
    token = set_workspace_root(tmp_path)
    try:
        tool = ExecScriptTool()
        # Portable: write cwd into a file inside the workspace
        result = await tool.execute(script="pwd > cwd_marker || cd > cwd_marker")
        assert not result.startswith("Error:"), result
        assert marker.exists() or (tmp_path / "cwd_marker").exists()
        content = (tmp_path / "cwd_marker").read_text(encoding="utf-8", errors="replace")
        assert str(tmp_path.resolve()) in content or content.strip()
    finally:
        reset_workspace_root(token)
