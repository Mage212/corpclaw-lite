"""
Bootstrap system prompt loader.

Modular prompt system: the final system prompt is assembled from independent
Markdown files in config/bootstrap/:
    SOUL.md     – Agent identity, values, hard constraints
    COMPANY.md  – Company-specific context, tone, brand voice
    SKILLS.md   – Injected list of available skills (auto-generated)

Overlay support: multiple bootstrap directories can be supplied (default first,
overlays later). Files are merged by filename — an overlay file with the same
name as a default file overrides it (later directory wins). Files unique to an
overlay are added. Department and per-user prompts resolve via the first
matching directory scanned from highest priority (overlay) to lowest.

Hot-reload: BootstrapLoader caches file contents by modification time.
Calling get_system_prompt() again after a file changes returns the fresh content.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

__all__ = [
    "BootstrapLoader",
]

logger = logging.getLogger(__name__)


class BootstrapLoader:
    """
    Loads and assembles the agent system prompt from modular markdown files.

    Each file across the configured bootstrap directories is merged by filename:
    later directories (overlays) override earlier ones for the same filename,
    and uniquely-named files are appended. Selected files are sorted
    alphabetically by filename and concatenated (with a separator) into the
    final system prompt. File mtimes are tracked to support hot-reloading
    without process restart.

    Calibrated overrides (``config/calibrated/bootstrap/``, sibling of the
    default directory) still apply per-file on top of the default directory —
    this is an orthogonal axis to overlays.
    """

    def __init__(self, bootstrap_dir: Path | str | list[str | Path] = "config/bootstrap") -> None:
        if isinstance(bootstrap_dir, list):
            self._dirs: list[Path] = [Path(d) for d in bootstrap_dir]
        else:
            self._dirs = [Path(bootstrap_dir)]
        self._cache: dict[Path, tuple[float, str]] = {}  # path -> (mtime, content)

    @property
    def dirs(self) -> list[Path]:
        """All bootstrap directories, lowest priority first."""
        return list(self._dirs)

    def get_system_prompt(self, extras: dict[str, str] | None = None) -> str:
        """
        Build and return the full system prompt by joining all .md files found
        across the bootstrap directories (merged by filename, overlays win).

        For the default (first) directory, calibrated versions in
        ``config/calibrated/bootstrap/`` take priority over the originals
        (per-file override) — this is the pre-existing calibration axis.

        Args:
            extras: Additional sections to inject, keyed by title (e.g. {"Skills": "..."}).
        """
        # filename -> source path. Iterate dirs low→high priority so overlays
        # overwrite earlier entries; warn on override.
        sources: dict[str, Path] = {}
        for directory in self._dirs:
            if not directory.exists():
                continue
            for path in directory.glob("*.md"):
                filename = path.name
                if filename in sources:
                    logger.warning("Bootstrap file '%s' overridden by overlay: %s", filename, path)
                sources[filename] = path

        # Calibrated override applies to the default directory's files only.
        calibrated_dir = self._dirs[0].parent / "calibrated" / "bootstrap"

        parts: list[str] = []
        for filename in sorted(sources):
            path = sources[filename]
            # Calibration wins only when the file came from the default dir.
            if path.parent == self._dirs[0].resolve() or path.parent == self._dirs[0]:
                calibrated_path = calibrated_dir / filename
                if calibrated_path.exists():
                    path = calibrated_path
            content = self._load_cached(path)
            if content.strip():
                parts.append(content.strip())

        if extras:
            for title, body in extras.items():
                parts.append(f"## {title}\n\n{body}")

        return "\n\n---\n\n".join(parts)

    def _load_cached(self, path: Path) -> str:
        """Return cached content if unchanged, otherwise reload from disk."""
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]

        content = path.read_text(encoding="utf-8")
        self._cache[path] = (mtime, content)
        return content

    def render_skills_section(self, skills: list[tuple[str, str]]) -> str:
        """
        Format a list of (skill_id, description) pairs as a markdown section
        suitable for injection into the system prompt.
        """
        if not skills:
            return ""
        lines = ["## Available Skills\n"]
        for skill_id, desc in skills:
            lines.append(f"- **{skill_id}**: {desc}")
        return "\n".join(lines)

    def get_department_prompt(self, department: str) -> str | None:
        """Load department-specific instructions if available.

        Scans bootstrap directories from highest priority (overlay) to lowest.
        For the default (first) directory, a calibrated override at
        ``config/calibrated/bootstrap/departments/<department>.md`` takes priority.
        Returns cached content, or None if no file exists.
        """
        if not department or not re.match(r"^[a-zA-Z0-9_-]+$", department):
            return None

        for directory in reversed(self._dirs):
            path = directory / "departments" / f"{department}.md"
            if path.exists():
                content = self._load_cached(path)
                return content.strip() if content.strip() else None
        return None

    def get_user_prompt(self, user_id: int, legacy_telegram_id: int | None = None) -> str | None:
        """Load per-user prompt generated by onboarding finalization.

        Looks for ``<dir>/users/<user_id>.md`` scanning directories from highest
        priority (overlay) to lowest. Uses the same mtime-based caching as other
        bootstrap files, so changes are picked up automatically on next call.
        """
        for directory in reversed(self._dirs):
            path = directory / "users" / f"{user_id}.md"
            if path.exists():
                content = self._load_cached(path)
                return content.strip() if content.strip() else None
        if legacy_telegram_id is not None:
            for directory in reversed(self._dirs):
                legacy_path = directory / "users" / f"{legacy_telegram_id}.md"
                if legacy_path.exists():
                    content = self._load_cached(legacy_path)
                    return content.strip() if content.strip() else None
        return None
