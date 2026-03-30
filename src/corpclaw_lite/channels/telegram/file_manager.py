"""Interactive file manager for Telegram — browse and delete files via inline buttons.

Combines telegram_file_manager.py and delete_browser.py from v1 into a single module.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from corpclaw_lite.channels.telegram.callback_data import (
    CB_DELETE_BACK,
    CB_DELETE_CANCEL,
    CB_DELETE_CONFIRM,
    CB_DELETE_DIR,
    CB_DELETE_EXEC,
    CB_DELETE_FILE,
    CB_DELETE_NOOP,
    CB_DELETE_PAGE,
    CB_DELETE_REFRESH,
    CB_DELETE_ROOT,
    CB_DELETE_UP,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

__all__ = [
    "DELETE_PAGE_SIZE",
    "DELETE_STATE_KEY",
    "DeleteBrowserHandler",
    "DeleteEntry",
    "PROTECTED_DELETE_FILES",
    "PROTECTED_DELETE_ROOTS",
    "build_delete_browser",
    "build_delete_confirmation",
    "is_protected_delete_target",
    "safe_edit_message",
]

logger = logging.getLogger(__name__)

DELETE_STATE_KEY = "delete_file_manager"
DELETE_PAGE_SIZE = 8

PROTECTED_DELETE_ROOTS = frozenset(
    {
        ".agents",
        ".claude",
        ".git",
        ".github",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "config",
        "docker",
        "docs",
        "plans",
        "plugins",
        "references",
        "scripts",
        "skills",
        "src",
        "tests",
    }
)

PROTECTED_DELETE_FILES = frozenset(
    {
        ".env",
        ".env.example",
        "AGENTS.md",
        "Dockerfile",
        "README.md",
        "pyproject.toml",
        "uv.lock",
    }
)


@dataclass(slots=True)
class DeleteEntry:
    """Single browseable item in the file manager."""

    name: str
    relative_path: str
    is_dir: bool
    size_bytes: int
    modified_at: str


# ── Helper functions ──────────────────────────────────────────────────────────


def _escape_md(text: str) -> str:
    """Escape text for Telegram MarkdownV2."""
    return escape_markdown(text, version=2)


def is_protected_delete_target(target_path: Path, workspace: Path) -> bool:
    """Return True when target must not be deletable from Telegram UI."""
    try:
        rel = target_path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True

    parts = rel.parts
    if not parts:
        return True
    if parts[0] in PROTECTED_DELETE_ROOTS:
        return True
    return bool(len(parts) == 1 and parts[0] in PROTECTED_DELETE_FILES)


def _relative_display(path: Path, workspace: Path) -> str:
    """Return a stable workspace-relative display path."""
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return str(path.resolve())
    rel_str = str(rel).replace("\\", "/")
    return rel_str if rel_str else "."


def _is_within_workspace(target_path: Path, workspace: Path) -> bool:
    """Check that target path is inside workspace directory."""
    try:
        target_path.resolve().relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


def _format_size(size_bytes: int) -> str:
    """Return a compact human-readable file size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _list_entries(path: Path, workspace: Path) -> tuple[list[DeleteEntry], int]:
    entries: list[DeleteEntry] = []
    hidden_count = 0
    try:
        children = sorted(
            path.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError:
        return entries, hidden_count

    for child in children:
        try:
            child.resolve().relative_to(workspace.resolve())
        except ValueError:
            continue
        if is_protected_delete_target(child, workspace):
            hidden_count += 1
            continue
        try:
            stat = child.stat()
            modified_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            size_bytes = 0 if child.is_dir() else stat.st_size
        except OSError:
            modified_at = "n/a"
            size_bytes = 0
        entries.append(
            DeleteEntry(
                name=child.name,
                relative_path=str(child.resolve().relative_to(workspace.resolve())).replace(
                    "\\", "/"
                ),
                is_dir=child.is_dir(),
                size_bytes=size_bytes,
                modified_at=modified_at,
            )
        )
    return entries, hidden_count


def _format_entry_listing(entries: list[DeleteEntry], start_index: int) -> str:
    lines: list[str] = []
    for offset, entry in enumerate(entries, start=1):
        absolute_index = start_index + offset
        kind = "папка" if entry.is_dir else "файл"
        display = entry.name[:40] + "…" if len(entry.name) > 41 else entry.name
        size_display = "dir" if entry.is_dir else _format_size(entry.size_bytes)
        name_escaped = _escape_md(display)
        size_escaped = _escape_md(size_display)
        date_escaped = _escape_md(entry.modified_at)
        lines.append(
            f"{absolute_index}\\. {'📁' if entry.is_dir else '📄'} "
            f"`{name_escaped}` "
            f"\\({_escape_md(kind)}, {size_escaped}, {date_escaped}\\)"
        )
    return "\n".join(lines)


# ── Browser & confirmation builders ───────────────────────────────────────────


def build_delete_browser(
    current_path: Path,
    workspace: Path,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup, list[DeleteEntry], int]:
    """Render the file manager browser screen."""
    path = current_path.resolve()
    workspace = workspace.resolve()
    if not path.exists() or not path.is_dir():
        path = workspace

    entries, hidden_count = _list_entries(path, workspace)
    total_pages = max(1, (len(entries) + DELETE_PAGE_SIZE - 1) // DELETE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * DELETE_PAGE_SIZE
    page_entries = entries[start : start + DELETE_PAGE_SIZE]

    buttons: list[list[InlineKeyboardButton]] = []
    for idx, entry in enumerate(page_entries, start=start):
        icon = "📁" if entry.is_dir else "📄"
        display = entry.name[:22] + "…" if len(entry.name) > 23 else entry.name
        prefix = CB_DELETE_DIR if entry.is_dir else CB_DELETE_FILE
        buttons.append([InlineKeyboardButton(f"{icon} {display}", callback_data=f"{prefix}{idx}")])

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"{CB_DELETE_PAGE}{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=CB_DELETE_NOOP))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"{CB_DELETE_PAGE}{page + 1}"))
        buttons.append(nav)

    actions: list[InlineKeyboardButton] = []
    if path != workspace:
        actions.append(InlineKeyboardButton("..", callback_data=CB_DELETE_UP))
        actions.append(InlineKeyboardButton("⌂", callback_data=CB_DELETE_ROOT))
    actions.append(InlineKeyboardButton("↻ Обновить", callback_data=CB_DELETE_REFRESH))
    actions.append(InlineKeyboardButton("✖ Отмена", callback_data=CB_DELETE_CANCEL))
    buttons.append(actions)

    if path != workspace and not entries and not is_protected_delete_target(path, workspace):
        buttons.append(
            [InlineKeyboardButton("🗑 Удалить пустую папку", callback_data=CB_DELETE_CONFIRM)]
        )

    current_display = _relative_display(path, workspace)
    escaped_display = _escape_md(current_display)
    summary = f"Видимые элементы: {len(entries)}" + (
        f", скрытые служебные: {hidden_count}" if hidden_count > 0 else ""
    )
    escaped_summary = _escape_md(summary)
    listing = _format_entry_listing(page_entries, start)
    if not entries:
        text = (
            "Удаление файлов\n\n"
            f"Текущая папка: `{escaped_display}`\n\n"
            f"{escaped_summary}\n\n"
            "В этой папке нет файлов или подпапок\\."
        )
    else:
        text = (
            "Удаление файлов\n\n"
            f"Текущая папка: `{escaped_display}`\n\n"
            f"{escaped_summary}\n\n"
            "Нажмите на папку, чтобы открыть ее, или на файл, "
            "чтобы перейти к подтверждению удаления\\.\n\n"
            f"{listing}"
        )

    return text, InlineKeyboardMarkup(buttons), entries, page


def build_delete_confirmation(
    target_path: Path, workspace: Path
) -> tuple[str, InlineKeyboardMarkup]:
    """Render confirmation screen for a selected target."""
    stat = target_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    display = _relative_display(target_path, workspace)
    escaped_display = _escape_md(display)
    is_dir = target_path.is_dir()
    kind = "Папка" if is_dir else "Файл"
    button_label = "✅ Удалить папку" if is_dir else "✅ Удалить файл"
    size_line = ""
    warning = "Действие необратимо\\. Подтвердите удаление\\."
    if is_dir:
        warning = "Будет удалена только пустая папка\\. Действие необратимо\\."
    else:
        size_str = f"{stat.st_size / 1024:.1f}"
        size_line = f"Размер: {_escape_md(size_str)} KB\n"

    text = (
        "Подтверждение удаления\n\n"
        f"{kind}: `{escaped_display}`\n"
        f"{size_line}"
        f"Изменен: {_escape_md(modified)}\n\n"
        f"{warning}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(button_label, callback_data=CB_DELETE_EXEC)],
            [
                InlineKeyboardButton("↩ Назад", callback_data=CB_DELETE_BACK),
                InlineKeyboardButton("✖ Отмена", callback_data=CB_DELETE_CANCEL),
            ],
        ]
    )
    return text, keyboard


# ── Async wrappers ────────────────────────────────────────────────────────────


async def _build_browser_async(
    current_path: Path, workspace: Path, page: int = 0
) -> tuple[str, InlineKeyboardMarkup, list[DeleteEntry], int]:
    return await asyncio.to_thread(build_delete_browser, current_path, workspace, page)


async def _build_confirmation_async(
    target_path: Path, workspace: Path
) -> tuple[str, InlineKeyboardMarkup]:
    return await asyncio.to_thread(build_delete_confirmation, target_path, workspace)


async def _path_delete_async(path: Path) -> None:
    def _delete() -> None:
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()

    await asyncio.to_thread(_delete)


async def _is_empty_dir_async(path: Path) -> bool:
    def _check() -> bool:
        return path.is_dir() and not any(path.iterdir())

    return await asyncio.to_thread(_check)


# ── DeleteBrowserHandler ──────────────────────────────────────────────────────


async def safe_edit_message(update: Update, text: str, **kwargs: Any) -> None:
    """Edit callback message, tolerating Telegram no-op edit errors."""
    query = update.callback_query
    if query is None:
        return
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as exc:
        error_str = str(exc).lower()
        if "message is not modified" in error_str:
            await query.answer("Список уже актуален.")
            return
        if "can't parse entities" in error_str:
            logger.warning("MarkdownV2 parse error, falling back to plain text: %s", exc)
            fallback_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
            try:
                await query.edit_message_text(text, parse_mode=None, **fallback_kwargs)
                return
            except Exception as fallback_exc:
                logger.error("Fallback edit also failed: %s", fallback_exc)
                await query.answer("Ошибка отображения")
                return
        logger.error("Error editing callback message: %s", exc)


class DeleteBrowserHandler:
    """Handles file deletion UI and operations for Telegram channel.

    Args:
        workspace: Root workspace directory for browsing.
    """

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    async def handle_delete_command(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle /delete command by opening the file manager."""
        await self._render_browser(update, context)

    async def handle_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        data: str,
    ) -> None:
        """Handle file manager browsing and deletion callbacks."""
        query = update.callback_query
        if query is None:
            return

        if context.user_data is None:
            await safe_edit_message(
                update, "Состояние файлового менеджера устарело. Вызовите /delete снова."
            )
            return

        state_raw = context.user_data.get(DELETE_STATE_KEY)
        if not isinstance(state_raw, dict):
            await safe_edit_message(
                update, "Состояние файлового менеджера устарело. Вызовите /delete снова."
            )
            return

        state: dict[str, Any] = cast("dict[str, Any]", state_raw)
        workspace = self._workspace
        current_path = Path(str(state.get("current_path", str(workspace))))
        current_page = int(state.get("page", 0))

        if data == CB_DELETE_CANCEL:
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Удаление отменено.")
            return

        if data == CB_DELETE_BACK:
            await self._render_browser(
                update, context, current_path=current_path, page=current_page, edit=True
            )
            return

        if data == CB_DELETE_UP:
            next_path = (
                current_path.parent if current_path.resolve() != workspace.resolve() else workspace
            )
            await self._render_browser(update, context, current_path=next_path, page=0, edit=True)
            return

        if data == CB_DELETE_ROOT:
            await self._render_browser(update, context, current_path=workspace, page=0, edit=True)
            return

        if data == CB_DELETE_REFRESH:
            await self._render_browser(
                update, context, current_path=current_path, page=current_page, edit=True
            )
            return

        if data.startswith(CB_DELETE_PAGE):
            try:
                page = int(data[len(CB_DELETE_PAGE) :])
            except ValueError:
                self._clear_state(context.user_data)
                await safe_edit_message(
                    update, "Состояние файлового менеджера повреждено. Вызовите /delete снова."
                )
                return
            await self._render_browser(
                update, context, current_path=current_path, page=page, edit=True
            )
            return

        if data == CB_DELETE_CONFIRM:
            await self._handle_confirm(update, context, current_path, workspace)
            return

        if data.startswith(CB_DELETE_DIR):
            entry = self._resolve_entry(state, data, CB_DELETE_DIR)
            if entry is None or not entry.is_dir:
                self._clear_state(context.user_data)
                await safe_edit_message(update, "Папка больше недоступна. Вызовите /delete снова.")
                return
            target = (workspace / entry.relative_path).resolve()
            await self._render_browser(update, context, current_path=target, page=0, edit=True)
            return

        if data.startswith(CB_DELETE_FILE):
            entry = self._resolve_entry(state, data, CB_DELETE_FILE)
            if entry is None or entry.is_dir:
                self._clear_state(context.user_data)
                await safe_edit_message(update, "Файл больше недоступен. Вызовите /delete снова.")
                return
            await self._render_confirmation(update, context, entry)
            return

        if data == CB_DELETE_EXEC:
            await self._execute_delete(update, context)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _clear_state(self, user_data: dict[str, Any] | None) -> None:
        if user_data is not None:
            user_data.pop(DELETE_STATE_KEY, None)

    def _resolve_entry(self, state: dict[str, Any], data: str, prefix: str) -> DeleteEntry | None:
        try:
            index = int(data[len(prefix) :])
        except ValueError:
            return None
        entries_raw = state.get("entries")
        entries_typed = cast("list[Any]", entries_raw)
        if not isinstance(entries_raw, list) or index < 0 or index >= len(entries_typed):
            return None
        entry = entries_typed[index]
        if not isinstance(entry, DeleteEntry):
            return None
        return entry

    async def _render_browser(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        current_path: Path | None = None,
        page: int = 0,
        edit: bool = False,
    ) -> None:
        workspace = self._workspace
        path = current_path.resolve() if current_path is not None else workspace.resolve()
        if not _is_within_workspace(path, workspace):
            path = workspace.resolve()

        text, keyboard, entries, page = await _build_browser_async(path, workspace, page)
        if context.user_data is None:
            context.user_data = {}  # type: ignore[assignment]
        context.user_data[DELETE_STATE_KEY] = {  # type: ignore[index]
            "mode": "browse",
            "current_path": str(path),
            "page": page,
            "entries": entries,
            "selected_target": None,
            "selected_kind": None,
        }
        if edit and update.callback_query is not None:
            await safe_edit_message(update, text, reply_markup=keyboard, parse_mode="MarkdownV2")
            return

        if update.message is None:
            return

        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="MarkdownV2")

    async def _render_confirmation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        entry: DeleteEntry,
    ) -> None:
        workspace = self._workspace
        target_path = (workspace / entry.relative_path).resolve()

        if not _is_within_workspace(target_path, workspace) or not target_path.exists():
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Файл больше недоступен. Вызовите /delete снова.")
            return
        if is_protected_delete_target(target_path, workspace):
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Этот путь защищен и не может быть удален.")
            return

        if context.user_data is not None:
            state = context.user_data.get(DELETE_STATE_KEY)
            if isinstance(state, dict):
                state["mode"] = "confirm"
                state["selected_target"] = entry.relative_path
                state["selected_kind"] = "directory" if target_path.is_dir() else "file"

        text, keyboard = await _build_confirmation_async(target_path, workspace)
        await safe_edit_message(update, text, reply_markup=keyboard, parse_mode="MarkdownV2")

    async def _handle_confirm(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        current_path: Path,
        workspace: Path,
    ) -> None:
        if current_path.resolve() == workspace.resolve():
            await safe_edit_message(update, "Корневую папку workspace удалять нельзя.")
            return
        if is_protected_delete_target(current_path, workspace):
            await safe_edit_message(update, "Эта папка защищена и не может быть удалена.")
            return
        try:
            is_empty = await _is_empty_dir_async(current_path)
        except OSError:
            is_empty = False
        if not is_empty:
            await safe_edit_message(update, "Удалять можно только пустую папку.")
            return
        # Render confirmation for the empty directory
        entry = DeleteEntry(
            name=current_path.name,
            relative_path=str(current_path.resolve().relative_to(workspace.resolve())).replace(
                "\\", "/"
            ),
            is_dir=True,
            size_bytes=0,
            modified_at="",
        )
        await self._render_confirmation(update, context, entry)

    async def _execute_delete(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if context.user_data is None:
            await safe_edit_message(
                update, "Состояние файлового менеджера устарело. Вызовите /delete снова."
            )
            return
        state_raw = context.user_data.get(DELETE_STATE_KEY)
        if not isinstance(state_raw, dict):
            await safe_edit_message(
                update, "Состояние файлового менеджера устарело. Вызовите /delete снова."
            )
            return

        state: dict[str, Any] = cast("dict[str, Any]", state_raw)
        selected_target: str | None = state.get("selected_target")
        if not isinstance(selected_target, str) or not selected_target:
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Файл для удаления не выбран. Вызовите /delete снова.")
            return

        workspace = self._workspace
        target_path = (workspace / selected_target).resolve()
        if not _is_within_workspace(target_path, workspace):
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Недопустимый путь файла.")
            return
        if is_protected_delete_target(target_path, workspace):
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Этот путь защищен и не может быть удален.")
            return
        if not target_path.exists():
            self._clear_state(context.user_data)
            await safe_edit_message(update, "Файл уже удален или недоступен.")
            return

        selected_kind: str | None = state.get("selected_kind")
        if target_path.is_dir():
            if selected_kind != "directory":
                self._clear_state(context.user_data)
                await safe_edit_message(update, "Для папок нужен отдельный сценарий подтверждения.")
                return
            try:
                await _path_delete_async(target_path)
            except OSError:
                self._clear_state(context.user_data)
                await safe_edit_message(update, "Удалить можно только пустую папку.")
                return
            next_path = target_path.parent
            next_page = 0
        else:
            await _path_delete_async(target_path)
            next_path = Path(str(state.get("current_path", str(workspace))))
            next_page = int(state.get("page", 0))

        await self._render_browser(
            update, context, current_path=next_path, page=next_page, edit=True
        )
