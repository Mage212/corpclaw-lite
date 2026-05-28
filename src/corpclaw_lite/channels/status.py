"""Shared user-facing status labels for long-running channel requests."""

from __future__ import annotations

__all__ = [
    "INITIAL_STATUS_TEXT",
    "LLM_STAGE_STATUS_MAP",
    "READY_STATUS_TEXT",
    "TOOL_STATUS_MAP",
    "format_llm_stage_status",
    "format_tool_status",
]

INITIAL_STATUS_TEXT = "⏳ В обработке..."
READY_STATUS_TEXT = "✅ Готово..."

TOOL_STATUS_MAP: dict[str, str] = {
    "list_files": "📂 Читаю файл...",
    "read_file": "📂 Читаю файл...",
    "read_image": "🖼️ Просматриваю изображение...",
    "normalize_excel": "📊 Обрабатываю таблицу...",
    "web_fetch": "🌐 Ищу информацию...",
    "web_search": "🌐 Ищу информацию...",
    "exec_script": "💻 Запускаю команду...",
    "exec_command": "💻 Запускаю команду...",
    "send_file_to_user": "📎 Готовлю файл...",
    "write_file": "✏️ Записываю файл...",
    "edit_file": "✏️ Редактирую файл...",
    "search_files": "🔍 Ищу в файлах...",
    "memory_store": "💾 Запоминаю...",
    "memory_recall": "💾 Вспоминаю...",
    "dispatch_subagent": "🤖 Делегирую субагенту...",
}

LLM_STAGE_STATUS_MAP: dict[str, str] = {
    "started": "🤔 Думаю...",
    "reasoning": "🤔 Думаю...",
    "answer": "📝 Собираю ответ...",
    "tool_call": "⚙️ Готовлю действие...",
    "fallback": "🤔 Думаю...",
    "stalled": "🤔 Думаю...",
}


def format_tool_status(tool_name: str) -> str:
    """Return a friendly status label for a tool execution start."""
    return TOOL_STATUS_MAP.get(tool_name, "⚙️ Выполняю действие...")


def format_llm_stage_status(stage: str) -> str | None:
    """Return a friendly status label for backend LLM streaming telemetry."""
    return LLM_STAGE_STATUS_MAP.get(stage)
