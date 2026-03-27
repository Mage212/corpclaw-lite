"""Tests for upload module: filename sanitization and extension checks."""

from __future__ import annotations

from corpclaw_lite.channels.telegram.upload import (
    build_agent_directive,
    is_image,
    is_safe_extension,
    sanitize_filename,
)


class TestSanitizeFilename:
    def test_normal_filename(self) -> None:
        assert sanitize_filename("report.xlsx") == "report.xlsx"

    def test_null_byte(self) -> None:
        assert sanitize_filename("report\x00.xlsx") is None

    def test_path_traversal_blocked(self) -> None:
        # ../../ patterns are blocked
        assert sanitize_filename("../../etc/passwd") is None

    def test_safe_path_cleaned(self) -> None:
        # Normal paths with separators are flattened, not blocked
        result = sanitize_filename("uploads/report.txt")
        assert result is not None
        assert "/" not in result

    def test_double_extension(self) -> None:
        assert sanitize_filename("image.jpg.exe") is None

    def test_empty_name(self) -> None:
        assert sanitize_filename("") is None
        assert sanitize_filename("  ") is None

    def test_single_dot(self) -> None:
        assert sanitize_filename(".") is None

    def test_double_dot(self) -> None:
        assert sanitize_filename("..") is None

    def test_windows_path_separators(self) -> None:
        result = sanitize_filename("C:\\Users\\foo\\bar.txt")
        assert result is not None
        assert "\\" not in result

    def test_slash_replaced_with_underscore(self) -> None:
        result = sanitize_filename("some/path/file.txt")
        assert result is not None
        assert "/" not in result


class TestIsSafeExtension:
    def test_allowed(self) -> None:
        assert is_safe_extension("report.xlsx") is True
        assert is_safe_extension("data.csv") is True
        assert is_safe_extension("readme.md") is True

    def test_forbidden(self) -> None:
        assert is_safe_extension("virus.exe") is False
        assert is_safe_extension("script.bat") is False
        assert is_safe_extension("run.sh") is False

    def test_no_extension(self) -> None:
        assert is_safe_extension("Makefile") is False

    def test_unknown_extension(self) -> None:
        assert is_safe_extension("data.parquet") is False


class TestIsImage:
    def test_images(self) -> None:
        assert is_image("photo.jpg") is True
        assert is_image("screenshot.png") is True
        assert is_image("banner.webp") is True

    def test_non_images(self) -> None:
        assert is_image("report.xlsx") is False
        assert is_image("document.pdf") is False


class TestBuildAgentDirective:
    def test_image_with_caption(self) -> None:
        result = build_agent_directive("photo.jpg", "Что на фото?")
        assert "read_image" in result
        assert "Что на фото?" in result

    def test_image_without_caption(self) -> None:
        result = build_agent_directive("photo.png", None)
        assert "read_image" in result

    def test_file_with_caption(self) -> None:
        result = build_agent_directive("report.xlsx", "Нормализуй")
        assert "Нормализуй" in result

    def test_file_without_caption(self) -> None:
        result = build_agent_directive("data.csv", None)
        assert "сохранен" in result.lower() or "файл" in result.lower()
