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
async def test_write_and_read_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry
) -> None:
    # Change CWD to tmp_path to test the _resolve_and_validate_path constraint
    monkeypatch.chdir(tmp_path)

    test_file = "test.txt"
    # Write
    res_w = await registry.execute(
        "write_file", {"path": test_file, "content": "Hello World\nLine 2"}
    )
    assert "Successfully" in res_w

    # Read
    res_r = await registry.execute("read_file", {"path": test_file})
    assert res_r == "Hello World\nLine 2"

    # Edit
    res_e = await registry.execute(
        "edit_file", {"path": test_file, "old_text": "World", "new_text": "Lite"}
    )
    assert "Successfully" in res_e

    # Check edit result
    assert await registry.execute("read_file", {"path": test_file}) == "Hello Lite\nLine 2"


@pytest.mark.asyncio
async def test_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry
) -> None:
    monkeypatch.chdir(tmp_path)
    # Trying to read something outside the workspace (like parent dir)
    res = await registry.execute("read_file", {"path": "../secret.txt"})
    assert "Error" in res
    assert "PermissionError" in res or "outside of workspace" in res


@pytest.mark.asyncio
async def test_path_traversal_same_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry
) -> None:
    """A directory whose name starts with workspace_root string must be blocked."""
    monkeypatch.chdir(tmp_path)
    # Create a sibling directory with a name that is a prefix extension of tmp_path
    sibling = tmp_path.parent / (tmp_path.name + "_evil")
    sibling.mkdir(exist_ok=True)
    evil_file = sibling / "secret.txt"
    evil_file.write_text("evil content")
    res = await registry.execute("read_file", {"path": str(evil_file)})
    assert "Error" in res
    assert "outside of workspace" in res or "PermissionError" in res


@pytest.mark.asyncio
async def test_list_and_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry
) -> None:
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


@pytest.mark.asyncio
async def test_registry_execute_passes_user_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """ToolRegistry.execute() must forward the user kwarg into tool.execute()."""
    from typing import Any

    from corpclaw_lite.extensions.tools.base import RiskLevel, Tool
    from corpclaw_lite.users.models import User

    received_user: list[Any] = []

    class UserCapturingTool(Tool):
        name = "capture_user"
        description = "Captures the user kwarg"
        params = []
        risk_level = RiskLevel.LOW

        async def execute(self, *, user: Any = None, **kwargs: Any) -> str:
            received_user.append(user)
            return "ok"

    r = ToolRegistry()
    r.register(UserCapturingTool())

    user = User(id=42, name="Test", department="qa")
    result = await r.execute("capture_user", {}, user=user)

    assert result == "ok"
    assert received_user == [user], "user kwarg was not forwarded to tool.execute()"
