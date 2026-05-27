from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.channels.web.files import (
    delete_path,
    list_directory,
    make_directory,
    resolve_workspace_path,
    save_upload,
)


def test_resolve_workspace_path_blocks_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    assert resolve_workspace_path(workspace, "docs").is_relative_to(workspace)

    with pytest.raises(PermissionError):
        resolve_workspace_path(workspace, "../outside.txt")


@pytest.mark.asyncio
async def test_web_file_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    folder = await make_directory(workspace, "", "reports")
    assert folder == "reports"

    rel = await save_upload(
        workspace=workspace,
        filename="report.txt",
        data=b"hello",
        max_bytes=100,
        target_dir="reports",
    )
    assert rel == "reports/report.txt"

    listing = await list_directory(workspace, "reports")
    entries = listing["entries"]
    assert isinstance(entries, list)
    assert entries[0]["name"] == "report.txt"

    await delete_path(workspace, "reports/report.txt")
    listing = await list_directory(workspace, "reports")
    assert listing["entries"] == []


@pytest.mark.asyncio
async def test_web_upload_rejects_bad_extension(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(ValueError, match="File type"):
        await save_upload(
            workspace=workspace,
            filename="payload.exe",
            data=b"x",
            max_bytes=100,
        )
