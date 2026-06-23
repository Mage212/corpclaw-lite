"""Tests for B-059: workspace path validation and security checks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from corpclaw_lite.security.path_validator import (
    PermissionDenied,
    resolve_and_validate_path,
)

# ─── workspace boundary ──────────────────────────────────────────────────────


def test_relative_path_resolves_inside_workspace(tmp_path: Path) -> None:
    result = resolve_and_validate_path("sub/file.txt", workspace_root=tmp_path)
    assert result == (tmp_path / "sub" / "file.txt").resolve()


def test_absolute_path_inside_workspace_ok(tmp_path: Path) -> None:
    target = tmp_path / "data.xlsx"
    result = resolve_and_validate_path(str(target), workspace_root=tmp_path)
    assert result == target.resolve()


def test_path_outside_workspace_rejected(tmp_path: Path) -> None:
    evil = tmp_path.parent / "outside.txt"
    with pytest.raises(PermissionDenied, match="outside of workspace"):
        resolve_and_validate_path(str(evil), workspace_root=tmp_path)


def test_dotdot_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermissionDenied):
        resolve_and_validate_path("../escape.txt", workspace_root=tmp_path)


def test_workspace_root_itself_is_valid(tmp_path: Path) -> None:
    result = resolve_and_validate_path(str(tmp_path), workspace_root=tmp_path)
    assert result == tmp_path.resolve()


# ─── null bytes ──────────────────────────────────────────────────────────────


def test_null_byte_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermissionDenied, match="null byte"):
        resolve_and_validate_path("evil\x00.txt", workspace_root=tmp_path)


# ─── symlink ancestor walk ───────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated perms on Windows")
def test_symlink_ancestor_inside_workspace_allowed(tmp_path: Path) -> None:
    """A symlink whose target is inside the workspace is OK."""
    real = tmp_path / "real_dir"
    real.mkdir()
    (real / "data.txt").write_text("ok")
    link = tmp_path / "link_dir"
    link.symlink_to(real)
    result = resolve_and_validate_path("link_dir/data.txt", workspace_root=tmp_path)
    assert result.name == "data.txt"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need elevated perms on Windows")
def test_symlink_escaping_workspace_rejected(tmp_path: Path) -> None:
    """A symlink pointing outside the workspace is rejected. The boundary
    check catches it first (resolve() already dereferences the symlink to a
    path outside the workspace), which is correct — the ancestor-walk is
    defense-in-depth for symlinks that resolve *inside* the workspace but
    were swapped after the resolve."""
    outside = tmp_path.parent / "outside_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "evil_link"
    link.symlink_to(outside)
    with pytest.raises(PermissionDenied):
        resolve_and_validate_path("evil_link/secret.txt", workspace_root=tmp_path)


# ─── hardlink rejection ──────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="hardlinks behave differently on Windows")
def test_hardlink_rejected(tmp_path: Path) -> None:
    original = tmp_path / "original.txt"
    original.write_text("data")
    hardlink = tmp_path / "hardlink.txt"
    os.link(original, hardlink)  # st_nlink == 2
    with pytest.raises(PermissionDenied, match="hardlink"):
        resolve_and_validate_path("hardlink.txt", workspace_root=tmp_path)


def test_regular_file_in_workspace_ok(tmp_path: Path) -> None:
    f = tmp_path / "normal.txt"
    f.write_text("ok")
    result = resolve_and_validate_path("normal.txt", workspace_root=tmp_path)
    assert result == f.resolve()


# ─── sensitive path patterns ─────────────────────────────────────────────────


def test_ssh_path_rejected(tmp_path: Path) -> None:
    # Simulate a path that resolves to a .ssh directory inside workspace.
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("key")
    with pytest.raises(PermissionDenied, match="sensitive"):
        resolve_and_validate_path(".ssh/id_rsa", workspace_root=tmp_path)


def test_aws_credentials_rejected(tmp_path: Path) -> None:
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("key")
    with pytest.raises(PermissionDenied, match="sensitive"):
        resolve_and_validate_path(".aws/credentials", workspace_root=tmp_path)


def test_kube_config_rejected(tmp_path: Path) -> None:
    kube = tmp_path / ".kube"
    kube.mkdir()
    (kube / "config").write_text("cfg")
    with pytest.raises(PermissionDenied, match="sensitive"):
        resolve_and_validate_path(".kube/config", workspace_root=tmp_path)


def test_env_file_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=x")
    with pytest.raises(PermissionDenied, match="sensitive"):
        resolve_and_validate_path(".env", workspace_root=tmp_path)


def test_env_local_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env.local").write_text("SECRET=x")
    with pytest.raises(PermissionDenied, match="sensitive"):
        resolve_and_validate_path(".env.local", workspace_root=tmp_path)


def test_normal_data_file_ok(tmp_path: Path) -> None:
    f = tmp_path / "report.xlsx"
    f.write_bytes(b"fake xlsx")
    result = resolve_and_validate_path("report.xlsx", workspace_root=tmp_path)
    assert result == f.resolve()


# ─── Windows device names ────────────────────────────────────────────────────


def test_windows_device_name_con_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermissionDenied, match="reserved Windows device"):
        resolve_and_validate_path("CON", workspace_root=tmp_path)


def test_windows_device_name_nul_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermissionDenied, match="reserved Windows device"):
        resolve_and_validate_path("NUL", workspace_root=tmp_path)


def test_windows_device_name_com1_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermissionDenied, match="reserved Windows device"):
        resolve_and_validate_path("COM1", workspace_root=tmp_path)


# ─── backward compatibility re-export ────────────────────────────────────────


def test_reexport_from_files_module() -> None:
    """Existing `from ...files import resolve_and_validate_path` still works."""
    from corpclaw_lite.extensions.tools.builtin.files import (
        resolve_and_validate_path as reexported,
    )

    assert reexported is resolve_and_validate_path


def test_permission_denied_subclasses_permission_error() -> None:
    """Existing `except PermissionError` handlers keep working."""
    assert issubclass(PermissionDenied, PermissionError)
