# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Telegram communication channel for CorpClaw Lite."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from collections.abc import Callable
from datetime import datetime
from html import escape as html_escape
from pathlib import Path
from typing import Any

import anyio

from corpclaw_lite.channels.base import Channel
from corpclaw_lite.channels.telegram.formatting import build_response_parts, convert_markdown
from corpclaw_lite.channels.telegram.upload import (
    MAX_FILE_SIZE,
    build_agent_directive,
    is_safe_extension,
    sanitize_filename,
)
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.memory.sqlite import SQLiteMemory
from corpclaw_lite.users.models import User
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

_APPROVAL_TIMEOUT = 300.0  # seconds
_DEDUP_BUFFER_SIZE = 10_000
_USER_MODE_KEY = "user_interaction_mode"


class TelegramChannel(Channel):
    """Telegram communication channel for CorpClaw Lite."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        message_handler: Callable[..., Any],
        workspace_base: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        memory: SQLiteMemory | None = None,
    ) -> None:
        """
        Args:
            token: Telegram Bot token
            message_handler: Callback async function(telegram_id, message, mode) -> str
            workspace_base: Base directory for per-user workspaces
            tool_registry: For /help command — lists available tools
            memory: For /new command — clears user history
        """
        self.token = token
        self._app: Application | None = None  # type: ignore
        self._on_message = message_handler
        self._workspace_base = workspace_base or Path("workspaces")
        self._tool_registry = tool_registry
        self._memory = memory

        # Approval system: message_id → (Future[bool], expected_telegram_user_id)
        self._pending_approvals: dict[str, tuple[asyncio.Future[bool], int]] = {}

        # Deduplication
        self._processed_ids: set[int] = set()
        self._processed_order: deque[int] = deque()
        self._dedup_lock = asyncio.Lock()

    @property
    def bot(self) -> Any:
        """Return the bot instance, or None if not started."""
        return self._app.bot if self._app else None

    @property
    def app(self) -> Any:
        """Return the Application instance, or None if not started."""
        return self._app

    def get_user_workspace(self, user: User) -> Path:
        """Return per-user workspace directory, creating it if needed."""
        ws = self._workspace_base / f"user_{user.telegram_id}"
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    async def start(self) -> None:
        """Initialize the Telegram bot application."""
        self._app = Application.builder().token(self.token).concurrent_updates(True).build()

        # Commands
        self._app.add_handler(CommandHandler("start", self._handle_start))
        self._app.add_handler(CommandHandler("help", self._handle_help))
        self._app.add_handler(CommandHandler("new", self._handle_new))
        self._app.add_handler(CommandHandler("delete", self._handle_delete))
        self._app.add_handler(CommandHandler("chat", self._handle_chat))
        self._app.add_handler(CommandHandler("execute", self._handle_execute))

        # Messages
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.Document.ALL, self._handle_document))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))

        # Callbacks
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))

        # Error handler
        self._app.add_error_handler(self._on_error)

        logger.info("Initializing Telegram application...")
        await self._app.initialize()
        await self._register_bot_commands()
        await self._app.start()

        await self._app.updater.start_polling()  # type: ignore
        logger.info("Telegram channel started (concurrent_updates=True).")

    async def stop(self) -> None:
        """Stop polling and close the Telegram bot."""
        if self._app:
            await self._app.updater.stop()  # type: ignore
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram channel stopped.")

    async def _register_bot_commands(self) -> None:
        """Register Telegram menu commands."""
        if not self._app or not self._app.bot:
            return
        await self._app.bot.set_my_commands(
            [
                BotCommand("start", "Регистрация и приветствие"),
                BotCommand("help", "Справка и доступные инструменты"),
                BotCommand("new", "Сбросить текущую сессию"),
                BotCommand("delete", "Открыть удаление файлов"),
                BotCommand("chat", "Режим диалога (без инструментов)"),
                BotCommand("execute", "Режим исполнения (с инструментами)"),
            ]
        )

    # ── Deduplication ──────────────────────────────────────────────────────────

    async def _is_duplicate(self, update: Update) -> bool:
        """Return True if this update_id was already processed."""
        uid = update.update_id
        if uid == 0:
            return False
        async with self._dedup_lock:
            if uid in self._processed_ids:
                return True
            self._processed_ids.add(uid)
            self._processed_order.append(uid)
            while len(self._processed_order) > _DEDUP_BUFFER_SIZE:
                old = self._processed_order.popleft()
                self._processed_ids.discard(old)
        return False

    # ── Send helpers ──────────────────────────────────────────────────────────

    async def send_message(self, user: User, text: str, **opts: Any) -> None:
        """Send a message with MarkdownV2 formatting and auto-splitting."""
        if not self._app or not self._app.bot:
            return

        parts = build_response_parts(text)
        for part in parts:
            try:
                formatted = convert_markdown(part)
                await self._app.bot.send_message(
                    chat_id=user.telegram_id,
                    text=formatted,
                    parse_mode="MarkdownV2",
                    disable_web_page_preview=True,
                )
            except Exception:
                try:
                    await self._app.bot.send_message(
                        chat_id=user.telegram_id,
                        text=part,
                    )
                except Exception as e:
                    logger.error("Failed to send Telegram message to %s: %s", user.telegram_id, e)

    async def send_file(self, user: User, path: Path, caption: str = "") -> None:
        """Send a document wrapper."""
        if not self._app or not self._app.bot:
            return
        try:
            content = await anyio.Path(path).read_bytes()
            await self._app.bot.send_document(
                chat_id=user.telegram_id,
                document=content,
                caption=caption,
                filename=path.name,
            )
        except Exception as e:
            logger.error("Failed to send file to %s: %s", user.telegram_id, e)

    async def request_approval(self, user: User, action: str, details: str) -> bool:
        """Send Approve/Deny inline buttons and wait for the tap."""
        if not self._app or not self._app.bot or not user.telegram_id:
            return False

        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data="approve"),
                InlineKeyboardButton("❌ Deny", callback_data="deny"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        sent: Message = await self._app.bot.send_message(
            chat_id=user.telegram_id,
            text=(
                f"<b>⚠️ Approval Required</b>\n\n"
                f"<b>Action:</b> {html_escape(action)}\n\n"
                f"<i>{html_escape(details)}</i>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending_approvals[str(sent.message_id)] = (future, user.telegram_id or 0)

        try:
            return await asyncio.wait_for(future, timeout=_APPROVAL_TIMEOUT)
        except TimeoutError:
            logger.warning("Approval request timed out for action '%s'", action)
            self._pending_approvals.pop(str(sent.message_id), None)
            return False

    # ── Mode helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
        """Read interaction mode from context.user_data, defaulting to 'execute'."""
        if context.user_data is None:
            return "execute"
        return str(context.user_data.get(_USER_MODE_KEY, "execute"))

    @staticmethod
    def _set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
        """Store interaction mode in context.user_data."""
        if context.user_data is not None:
            context.user_data[_USER_MODE_KEY] = mode

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _handle_start(self, update: Update, context: Any) -> None:
        if update.effective_chat:
            await update.effective_chat.send_message(
                "👋 Добро пожаловать в CorpClaw Lite!\n\n"
                "/help — справка\n"
                "/new — сбросить сессию\n"
                "/delete — удаление файлов\n"
                "/chat — режим диалога\n"
                "/execute — режим исполнения",
            )

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show available tools."""
        if not update.effective_chat:
            return
        if self._tool_registry:
            tools = self._tool_registry.list_all()
            if tools:
                lines = [f"• {t.name} — {t.description.split('.')[0]}" for t in tools]
                text = "🔧 Доступные инструменты:\n\n" + "\n".join(lines)
            else:
                text = "Инструменты не зарегистрированы."
        else:
            text = "Инструменты недоступны."
        await update.effective_chat.send_message(text)

    async def _handle_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset conversation history."""
        if not update.effective_user or not update.effective_chat:
            return
        tid = update.effective_user.id
        if self._memory:
            await self._memory.clear(str(tid))
        await update.effective_chat.send_message("🔄 Сессия сброшена. Можете начать заново.")
        logger.info("User %d reset session", tid)

    async def _handle_delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Open interactive file manager."""
        if not update.effective_user:
            return

        from corpclaw_lite.channels.telegram.file_manager import DeleteBrowserHandler

        tid = update.effective_user.id
        temp_user = User(id=0, name=str(tid), telegram_id=tid, department="default")
        workspace = self.get_user_workspace(temp_user)

        handler = DeleteBrowserHandler(workspace=workspace)
        # Store in context.user_data for thread safety
        if context.user_data is not None:
            context.user_data["delete_handler"] = handler
        await handler.handle_delete_command(update, context)

    async def _handle_chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch to chat mode (no tools)."""
        if not update.effective_chat:
            return
        self._set_mode(context, "chat")
        await update.effective_chat.send_message(
            "💬 Режим диалога.\n\n"
            "В этом режиме я отвечаю на вопросы текстом, без инструментов.\n"
            "Для выполнения действий с файлами переключитесь командой /execute",
        )

    async def _handle_execute(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Switch to execute mode (with tools)."""
        if not update.effective_chat:
            return
        self._set_mode(context, "execute")
        await update.effective_chat.send_message(
            "🔧 Режим исполнения.\n\n"
            "В этом режиме я могу работать с файлами и инструментами.\n"
            "Для простых вопросов переключитесь командой /chat",
        )

    # ── Message handlers ──────────────────────────────────────────────────────

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text or not update.effective_user:
            return
        if await self._is_duplicate(update):
            return

        mode = self._get_mode(context)
        await self._on_message(str(update.effective_user.id), update.message.text, mode)

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle document upload: download → sanitize → save → agent run."""
        if not update.message or not update.message.document or not update.effective_user:
            return
        if await self._is_duplicate(update):
            return

        doc = update.message.document
        caption = update.message.caption
        tid = update.effective_user.id

        await self._save_and_process_file(
            update, tid, doc.file_id, doc.file_name or "document", doc.file_size, caption
        )

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle photo upload: take largest resolution → save → agent run."""
        if not update.message or not update.message.photo or not update.effective_user:
            return
        if await self._is_duplicate(update):
            return

        photo = update.message.photo[-1]
        caption = update.message.caption
        tid = update.effective_user.id
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name = f"image_{timestamp}.jpg"

        await self._save_and_process_file(
            update, tid, photo.file_id, file_name, photo.file_size, caption
        )

    async def _save_and_process_file(
        self,
        update: Update,
        telegram_id: int,
        file_id: str,
        file_name: str,
        file_size: int | None,
        caption: str | None,
    ) -> None:
        """Shared logic: download file, sanitize, save, notify agent."""
        if not update.message or not self._app:
            return

        # Size check
        if file_size and file_size > MAX_FILE_SIZE:
            size_mb = file_size / 1024 / 1024
            await update.message.reply_text(
                f"⚠️ Файл слишком большой ({size_mb:.1f} МБ). Максимум 20 МБ."
            )
            return

        # Sanitize filename
        safe_name = sanitize_filename(file_name)
        if safe_name is None:
            await update.message.reply_text("⚠️ Некорректное имя файла.")
            return
        if not is_safe_extension(safe_name):
            await update.message.reply_text(
                "⚠️ Загрузка файлов этого типа запрещена из соображений безопасности."
            )
            return

        # Resolve workspace
        temp_user = User(
            id=0, name=f"user_{telegram_id}", telegram_id=telegram_id, department="default"
        )
        workspace = self.get_user_workspace(temp_user)
        target_path = (workspace / safe_name).resolve()

        if not str(target_path).startswith(str(workspace.resolve())):
            await update.message.reply_text("⚠️ Недопустимый путь файла.")
            return

        # Auto-rename on collision
        base, ext = os.path.splitext(safe_name)
        counter = 1
        while target_path.exists():
            safe_name = f"{base}_{counter}{ext}"
            target_path = (workspace / safe_name).resolve()
            counter += 1

        # Download
        try:
            telegram_file = await self._app.bot.get_file(file_id)
            await telegram_file.download_to_drive(custom_path=target_path)  # type: ignore
            logger.info("Saved file %s for telegram_id=%d", safe_name, telegram_id)
        except Exception:
            logger.exception("Error downloading file for telegram_id=%d", telegram_id)
            await update.message.reply_text("❌ Не удалось сохранить файл. Попробуйте снова.")
            return

        # Notify agent in execute mode
        directive = build_agent_directive(safe_name, caption)
        await self._on_message(str(telegram_id), directive, "execute")

    # ── Callback handler ──────────────────────────────────────────────────────

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.message:
            return

        data = query.data or ""

        # Delete-flow callbacks → file manager handler
        if data.startswith("del:"):
            handler = (
                context.user_data.get("delete_handler") if context.user_data is not None else None
            )
            if handler is not None:
                await query.answer()
                await handler.handle_callback(update, context, data)
                return
            await query.answer("Вызовите /delete для начала.")
            return

        # Approval callbacks
        msg_id = str(query.message.message_id)
        entry = self._pending_approvals.get(msg_id)
        if not entry:
            await query.answer()
            return

        future, expected_uid = entry
        caller_uid = query.from_user.id if query.from_user else None
        if caller_uid != expected_uid:
            await query.answer("This approval request is not addressed to you.", show_alert=True)
            return

        self._pending_approvals.pop(msg_id)
        approved = data == "approve"

        if not future.done():
            future.set_result(approved)

        await query.answer()
        label = "✅ Approved" if approved else "❌ Denied"
        await query.edit_message_text(text=label)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Global error handler for unhandled exceptions."""
        logger.error("Unhandled error in Telegram handler: %s", context.error, exc_info=True)
