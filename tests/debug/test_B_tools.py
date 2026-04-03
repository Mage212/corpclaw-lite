"""Group B — Individual tool integration tests.

Each test verifies exactly one builtin tool end-to-end:
the tool executes, produces a meaningful result, and the agent
incorporates it correctly into the reply.

Tools covered (13 total):
  B01  read_file          B02  write_file         B03  edit_file
  B04  list_files         B05  search_files        B06  memory_store
  B07  memory_recall      B08  web_fetch           B09  exec_script
  B10  normalize_excel    B11  read_image (block)  B12  send_file (stub)
  B13  dispatch_subagent  → covered in Group E

Run:
    uv run pytest tests/debug/test_B_tools.py -v -s
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from corpclaw_lite.agent.loop import AgentLoop
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.users.models import User

from .helpers import DebugAssertions, summarise_run

pytestmark = [pytest.mark.integration, pytest.mark.llm_required]


# ---------------------------------------------------------------------------
# B01 — write_file: create a file with known content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B01_write_file(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent creates a text file via write_file."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Создай файл hello.txt со следующим содержимым: Hello World",
    )

    DebugAssertions.assert_tool_used(stats, "write_file")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_file_exists(tmp_workspace / "hello.txt", contains="Hello World")
    print(f"\n[B01] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B02 — read_file: agent reads an existing file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B02_read_file(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent reads an existing text file and includes its content in the reply."""
    loop, _ = agent_stack_no_container

    # Pre-create the file so the agent can find it immediately
    secret_file = tmp_workspace / "secret.txt"
    secret_file.write_text("SECRET_TOKEN_B02", encoding="utf-8")

    reply, stats = await loop.run(
        test_user,
        "Прочитай содержимое файла secret.txt и скажи мне что там написано.",
    )

    DebugAssertions.assert_tool_used(stats, "read_file")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_reply_contains(reply, "SECRET_TOKEN_B02")
    print(f"\n[B02] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B03 — edit_file: replace text in an existing file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B03_edit_file(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent replaces text in an existing file via edit_file."""
    loop, _ = agent_stack_no_container

    target = tmp_workspace / "config.txt"
    target.write_text("version=1\nenv=dev", encoding="utf-8")

    reply, stats = await loop.run(
        test_user,
        "В файле config.txt замени текст 'version=1' на 'version=2'.",
    )

    DebugAssertions.assert_tool_used(stats, "edit_file")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_file_exists(tmp_workspace / "config.txt", contains="version=2")
    DebugAssertions.assert_file_not_contains(tmp_workspace / "config.txt", "version=1")
    print(f"\n[B03] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B04 — list_files: agent lists directory contents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B04_list_files(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent lists files in a directory via list_files."""
    loop, _ = agent_stack_no_container

    # Create known files
    for name in ("alpha.txt", "beta.txt", "gamma.txt"):
        (tmp_workspace / name).write_text(name, encoding="utf-8")

    reply, stats = await loop.run(
        test_user,
        "Перечисли все файлы в текущей директории.",
    )

    DebugAssertions.assert_tool_used(stats, "list_files")
    DebugAssertions.assert_status_ok(stats)
    # At least one known file name must appear in the reply
    has_any = any(name in reply for name in ("alpha.txt", "beta.txt", "gamma.txt"))
    assert has_any, f"None of the expected file names found in reply:\n{reply[:400]}"
    print(f"\n[B04] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B05 — search_files: agent finds a unique token across files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B05_search_files(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent searches for a unique string across files via search_files."""
    loop, _ = agent_stack_no_container

    (tmp_workspace / "notes.txt").write_text("FIND_ME_B05 is here", encoding="utf-8")
    (tmp_workspace / "other.txt").write_text("Nothing special", encoding="utf-8")

    reply, stats = await loop.run(
        test_user,
        "Найди в текущей директории файлы содержащие строку FIND_ME_B05.",
    )

    DebugAssertions.assert_tool_used(stats, "search_files")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_reply_contains(reply, "notes.txt")
    print(f"\n[B05] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B06+B07 — memory_store + memory_recall across two runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B06_B07_memory_store_and_recall(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Agent stores a fact then retrieves it in a subsequent run() call."""
    loop, _ = agent_stack_no_container

    # First turn: store
    _, stats1 = await loop.run(
        test_user,
        "Запомни: мой любимый фрукт — MANGO_B07. Используй инструмент memory_store.",
    )
    DebugAssertions.assert_tool_used(stats1, "memory_store")

    # Second turn: recall — new message, same user → history continues
    reply2, stats2 = await loop.run(
        test_user,
        "Какой мой любимый фрукт? Используй memory_recall чтобы вспомнить.",
    )
    DebugAssertions.assert_tool_used(stats2, "memory_recall")
    DebugAssertions.assert_reply_contains(reply2, "MANGO_B07")
    print(f"\n[B06/B07] store_stats={stats1.tools_used} recall_reply={reply2[:200]}")


# ---------------------------------------------------------------------------
# B08 — web_fetch: load a public test URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B08_web_fetch(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
) -> None:
    """Agent fetches content from a public URL via web_fetch."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Загрузи страницу https://httpbin.org/get и скажи мне какой URL был запрошен.",
    )

    DebugAssertions.assert_tool_used(stats, "web_fetch")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_no_tool_error(reply)
    print(f"\n[B08] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B09 — exec_script: run a trivial Python expression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B09_exec_script_python(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent executes print(6*7) via exec_script and reports the result (42)."""
    loop, _ = agent_stack_no_container

    reply, stats = await loop.run(
        test_user,
        "Выполни Python код через exec_script: print(6 * 7)",
    )

    DebugAssertions.assert_tool_used(stats, "exec_script")
    DebugAssertions.assert_status_ok(stats)
    DebugAssertions.assert_reply_contains(reply, "42")
    print(f"\n[B09] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B10 — normalize_excel: create .xlsx, normalise it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B10_normalize_excel(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """Agent normalises an Excel file via normalize_excel."""
    loop, _ = agent_stack_no_container

    # Create a minimal valid .xlsx with openpyxl
    import openpyxl  # already a project dependency

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["Имя сотрудника", "ИНН", "Сумма"])
    ws.append(["Алиса", "770123456789", 1000.0])
    ws.append(["Боб", "770123456789", 2000.0])
    ws.append(["Алиса", "770123456789", 1000.0])  # duplicate
    ws.append([None, None, None])               # empty row
    wb.save(str(tmp_workspace / "data.xlsx"))

    reply, stats = await loop.run(
        test_user,
        "Нормализуй файл data.xlsx: удали дубликаты и пустые строки.",
    )

    DebugAssertions.assert_tool_used(stats, "normalize_excel")
    DebugAssertions.assert_status_ok(stats)

    output = tmp_workspace / "data_normalized.xlsx"
    assert output.exists(), (
        f"normalize_excel should have created data_normalized.xlsx.\n"
        f"Reply: {reply[:400]}"
    )
    print(f"\n[B10] {summarise_run(reply, stats)}")


# ---------------------------------------------------------------------------
# B11 — read_file refuses image files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B11_read_file_refuses_image(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """read_file tool must reject .png files and suggest read_image."""
    from corpclaw_lite.extensions.tools.builtin.files import ReadFileTool

    # Write a minimal PNG header
    png_header = struct.pack(">8B", 0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
    (tmp_workspace / "photo.png").write_bytes(png_header)

    tool = ReadFileTool()
    result = await tool.execute(path="photo.png")

    assert "read_image" in result.lower(), (
        f"read_file should have suggested read_image for .png.\nResult: {result}"
    )
    print(f"\n[B11] Image refusal result: {result}")


# ---------------------------------------------------------------------------
# B12 — send_file: graceful error when channel callback raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_B12_send_file_channel_error(
    agent_stack_no_container: tuple[AgentLoop, ToolRegistry],
    test_user: User,
    tmp_workspace: Path,
) -> None:
    """SendFileTool propagates a graceful error when the channel callback fails."""
    from corpclaw_lite.extensions.tools.builtin.send_file import SendFileTool

    # Create a file to attempt to send
    (tmp_workspace / "report.txt").write_text("dummy content", encoding="utf-8")

    async def _failing_callback(path: Path, user: object, caption: str) -> str:
        raise RuntimeError("No active channel configured")

    tool = SendFileTool(send_callback=_failing_callback)  # type: ignore[arg-type]
    result = await tool.execute(path="report.txt", user=test_user)

    assert "error" in result.lower(), (
        f"Expected graceful error from failing channel callback.\nResult: {result}"
    )
    print(f"\n[B12] Channel error result: {result}")
