"""Safe file upload handling for Telegram channel.

Ported from CorpClaw v1 ``telegram.py`` with simplifications.
"""

from __future__ import annotations

import ntpath
import os

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".py",
    ".js",
    ".html",
    ".css",
    ".csv",
    ".tsv",
    ".xlsx",
    ".xls",
    ".doc",
    ".docx",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}

FORBIDDEN_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".sh",
    ".vbs",
    ".ps1",
    ".msi",
    ".scr",
    ".com",
    ".pif",
}

DANGEROUS_DOUBLE_EXTENSIONS = {
    ".exe",
    ".bat",
    ".cmd",
    ".sh",
    ".vbs",
    ".ps1",
    ".msi",
    ".scr",
    ".com",
    ".pif",
    ".js",
    ".jar",
    ".php",
    ".pl",
    ".py",
    ".rb",
    ".dll",
    ".so",
    ".dylib",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


def is_safe_extension(filename: str) -> bool:
    """Check if a file extension is in the safe whitelist."""
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        return False
    return ext in ALLOWED_EXTENSIONS and ext not in FORBIDDEN_EXTENSIONS


def is_image(filename: str) -> bool:
    """Return True if filename has an image extension."""
    return os.path.splitext(filename)[1].lower() in IMAGE_EXTENSIONS


def sanitize_filename(filename: str) -> str | None:
    """Return a safe filename or None if the name is unsafe.

    Protects against:
    1. Null bytes that could truncate filenames
    2. Double extensions hiding executables (e.g. .jpg.exe)
    3. Path traversal via separators and parent references
    """
    if "\x00" in filename:
        return None

    stripped = filename.strip()
    if not stripped or stripped in {".", ".."}:
        return None

    # Remove drive letters (Windows paths)
    if ":" in stripped:
        _, stripped = ntpath.splitdrive(stripped)

    # Flatten path separators
    safe = stripped.replace("/", "_").replace("\\", "_")
    safe = os.path.basename(safe)

    if not safe or safe in {".", ".."}:
        return None
    if safe.startswith("..") or safe.endswith(".."):
        return None
    if "_.._" in safe:
        return None

    # Check double extensions (e.g. image.jpg.exe)
    parts = safe.rsplit(".", 2)
    if len(parts) == 3:
        final_ext = f".{parts[2].lower()}"
        if final_ext in DANGEROUS_DOUBLE_EXTENSIONS:
            return None

    return safe


def build_agent_directive(relative_path: str, caption: str | None) -> str:
    """Build a message for the agent about an uploaded file.

    Uses explicit directives so the agent acts immediately
    instead of asking follow-up questions.
    """
    if is_image(relative_path):
        if caption:
            return (
                f"Немедленно проанализируй только что загруженное изображение "
                f"'{relative_path}' с помощью read_image и сразу верни результат. "
                f"Используй подпись пользователя как запрос к анализу: {caption}"
            )
        return (
            f"Немедленно проанализируй только что загруженное изображение "
            f"'{relative_path}' с помощью read_image и сразу верни краткий результат. "
            "Не задавай уточняющих вопросов и не выполняй других действий."
        )

    if caption:
        return (
            f"Пользователь загрузил файл '{relative_path}'. Выполни только явно указанное "
            f"в подписи действие над этим файлом и не делай ничего сверх этого: {caption}"
        )

    return (
        f"Пользователь загрузил файл '{relative_path}'. Сообщи кратко, что файл сохранен, "
        "и что для дальнейшей обработки нужно явно указать действие. "
        "Не выполняй других действий."
    )
