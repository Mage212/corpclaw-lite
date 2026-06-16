import type { AgentMode, FileEntry } from "../types";

export const ROOT_LABEL = "Корень";
export const NO_DATA_LABEL = "нет данных";
export const REQUEST_FAILED_LABEL = "Не удалось выполнить запрос.";
export const UPLOAD_FAILED_LABEL = "Не удалось загрузить файл.";

export function displayPath(path: string | null | undefined): string {
  return path?.trim() ? path : ROOT_LABEL;
}

export function agentModeLabel(mode: AgentMode): string {
  return mode === "chat" ? "Диалог" : "Выполнение";
}

export function statusPhaseLabel(phase: string): string {
  switch (phase) {
    case "idle":
      return "Ожидание";
    case "request":
      return "Запрос";
    case "queue":
      return "Очередь";
    case "llm":
      return "LLM";
    case "tool":
      return "Инструмент";
    case "done":
      return "Готово";
    case "warning":
      return "Внимание";
    case "error":
      return "Ошибка";
    case "ws":
      return "Канал связи";
    case "legacy":
    case "status":
      return "Статус";
    case "reset":
      return "Сброс";
    default:
      return phase || "Статус";
  }
}

export function fileKindLabel(kind: string, isDir = false): string {
  if (isDir) return "Папка";

  switch (kind.toLowerCase()) {
    case "folder":
      return "Папка";
    case "file":
      return "Файл";
    case "image":
      return "Изображение";
    case "text":
      return "Текст";
    case "spreadsheet":
      return "Таблица";
    case "document":
      return "Документ";
    case "archive":
      return "Архив";
    case "code":
      return "Код";
    case "unknown":
      return "Файл";
    default:
      return kind || "Файл";
  }
}

export function fileEntryKindLabel(entry: FileEntry): string {
  return fileKindLabel(entry.kind, entry.is_dir);
}
