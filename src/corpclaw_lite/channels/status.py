"""Shared user-facing status labels for long-running channel requests."""

from __future__ import annotations

from corpclaw_lite.llm.queue import LLMQueueStatus

__all__ = [
    "INITIAL_STATUS_TEXT",
    "LLM_STAGE_STATUS_MAP",
    "READY_STATUS_TEXT",
    "TOOL_STATUS_MAP",
    "format_llm_stage_status",
    "format_llm_queue_status",
    "format_subagent_llm_stage_status",
    "format_subagent_llm_queue_status",
    "format_subagent_tool_batch_status",
    "format_subagent_tool_status",
    "format_tool_batch_status",
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
    "model_preparing": "⚙️ Готовлю запрос модели...",
    "model_waiting": "⏳ Жду начало ответа модели...",
    "started": "⏳ Жду начало ответа модели...",
    "reasoning": "🤔 Думаю...",
    "answer": "📝 Собираю ответ...",
    "tool_call": "⚙️ Готовлю действие...",
    "fallback": "🤔 Думаю...",
    "stalled": "🤔 Думаю...",
}

FILE_TOOL_NAMES = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "send_file_to_user",
    }
)
SEARCH_TOOL_NAMES = frozenset({"search_files", "web_fetch", "web_search"})
MEMORY_TOOL_NAMES = frozenset({"memory_store", "memory_recall"})


def format_tool_status(tool_name: str) -> str:
    """Return a friendly status label for a tool execution start."""
    return TOOL_STATUS_MAP.get(tool_name, "⚙️ Выполняю действие...")


def format_tool_batch_status(tool_names: list[str]) -> str:
    """Return a friendly status label for a parallel tool batch."""
    names = set(tool_names)
    if names and names <= FILE_TOOL_NAMES:
        return "📂 Работаю с файлами..."
    if names and names <= SEARCH_TOOL_NAMES:
        return "🔍 Ищу данные..."
    if names and names <= MEMORY_TOOL_NAMES:
        return "💾 Работаю с памятью..."

    count = len(tool_names)
    if count <= 0:
        return "⚙️ Выполняю действия..."
    if count % 10 == 1 and count % 100 != 11:
        word = "действие"
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = "действия"
    else:
        word = "действий"
    return f"⚙️ Выполняю {count} {word}..."


def format_llm_stage_status(stage: str) -> str | None:
    """Return a friendly status label for backend LLM streaming telemetry."""
    return LLM_STAGE_STATUS_MAP.get(stage)


def format_llm_queue_status(status: LLMQueueStatus) -> str:
    """Return a friendly status label for waiting in the LLM request queue."""
    if status.position is None:
        return "⏳ Ожидаю свободный LLM-слот..."
    position = status.position + 1
    wait_text = ""
    if status.estimated_wait_seconds is not None:
        wait_text = f", примерно {int(status.estimated_wait_seconds)}с"
    return f"⏳ Ожидаю LLM-слот. В очереди: {position}{wait_text}..."


def format_subagent_tool_status(subagent_name: str, tool_name: str) -> str:
    """Return a friendly status label for a subagent tool execution start."""
    return f"{subagent_name}: {format_tool_status(tool_name)}"


def format_subagent_tool_batch_status(subagent_name: str, tool_names: list[str]) -> str:
    """Return a friendly status label for a subagent parallel tool batch."""
    return f"{subagent_name}: {format_tool_batch_status(tool_names)}"


def format_subagent_llm_stage_status(subagent_name: str, stage: str) -> str | None:
    """Return a friendly status label for subagent backend LLM streaming telemetry."""
    friendly = format_llm_stage_status(stage)
    if friendly is None:
        return None
    return f"{subagent_name}: {friendly}"


def format_subagent_llm_queue_status(subagent_name: str, status: LLMQueueStatus) -> str:
    """Return a friendly status label for subagent LLM queue waiting."""
    return f"{subagent_name}: {format_llm_queue_status(status)}"
