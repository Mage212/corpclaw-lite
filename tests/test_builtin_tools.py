import os
from pathlib import Path

import pytest

from corpclaw_lite.extensions.tools.builtin.files import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
)
from corpclaw_lite.extensions.tools.registry import ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(ReadFileTool())
    r.register(WriteFileTool())
    r.register(EditFileTool())
    r.register(ListFilesTool())
    r.register(SearchFilesTool())
    return r


@pytest.mark.asyncio
async def test_tool_registry(registry: ToolRegistry) -> None:
    schemas = registry.to_schemas()
    assert len(schemas) == 5

    schema = next(s for s in schemas if s["function"]["name"] == "read_file")
    assert "path" in schema["function"]["parameters"]["properties"]
    assert "path" in schema["function"]["parameters"]["required"]


@pytest.mark.asyncio
async def test_write_and_read_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry) -> None:
    # Change CWD to tmp_path to test the _resolve_and_validate_path constraint
    monkeypatch.chdir(tmp_path)

    test_file = "test.txt"
    # Write
    res_w = await registry.execute("write_file", {"path": test_file, "content": "Hello World\nLine 2"})
    assert "Successfully" in res_w

    # Read
    res_r = await registry.execute("read_file", {"path": test_file})
    assert res_r == "Hello World\nLine 2"

    # Edit
    res_e = await registry.execute("edit_file", {"path": test_file, "old_text": "World", "new_text": "Lite"})
    assert "Successfully" in res_e

    # Check edit result
    assert await registry.execute("read_file", {"path": test_file}) == "Hello Lite\nLine 2"


@pytest.mark.asyncio
async def test_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry) -> None:
    monkeypatch.chdir(tmp_path)
    # Trying to read something outside the workspace (like parent dir)
    res = await registry.execute("read_file", {"path": "../secret.txt"})
    assert "Error" in res
    assert "PermissionError" in res or "outside of workspace" in res


@pytest.mark.asyncio
async def test_list_and_search(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry) -> None:
    monkeypatch.chdir(tmp_path)

    await registry.execute("write_file", {"path": "dir1/f1.txt", "content": "keyword_test"})
    await registry.execute("write_file", {"path": "f2.txt", "content": "nope"})

    # List
    res_l = await registry.execute("list_files", {"path": "."})
    assert "dir1" in res_l
    assert "f2.txt" in res_l

    # Search
    res_s = await registry.execute("search_files", {"path": ".", "pattern": "keyword_\\w+"})
    assert "keyword_test" in res_s
    assert "dir1/f1.txt" in res_s
