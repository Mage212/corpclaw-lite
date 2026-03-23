"""
Bootstrap system prompt loader.

Modular prompt system: the final system prompt is assembled from independent
Markdown files in config/bootstrap/:
    SOUL.md     – Agent identity, values, hard constraints
    COMPANY.md  – Company-specific context, tone, brand voice
    SKILLS.md   – Injected list of available skills (auto-generated)

Hot-reload: BootstrapLoader caches file contents by modification time.
Calling get_system_prompt() again after a file changes returns the fresh content.
"""

from __future__ import annotations

from pathlib import Path


class BootstrapLoader:
    """
    Loads and assembles the agent system prompt from modular markdown files.

    Each file in the bootstrap directory is sorted alphabetically and concatenated
    (with a separator) into the final system prompt. File mtimes are tracked to
    support hot-reloading without process restart.
    """

    def __init__(self, bootstrap_dir: Path | str = "config/bootstrap") -> None:
        self._dir = Path(bootstrap_dir)
        self._cache: dict[Path, tuple[float, str]] = {}  # path -> (mtime, content)

    def get_system_prompt(self, extras: dict[str, str] | None = None) -> str:
        """
        Build and return the full system prompt by joining all .md files found
        in the bootstrap directory.

        Args:
            extras: Additional sections to inject, keyed by title (e.g. {"Skills": "..."}).
        """
        if not self._dir.exists():
            return ""

        parts: list[str] = []
        for path in sorted(self._dir.glob("*.md")):
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

        Looks for ``config/bootstrap/departments/<department>.md``.
        Returns cached content, or None if no file exists.
        """
        path = self._dir / "departments" / f"{department}.md"
        if path.exists():
            content = self._load_cached(path)
            return content.strip() if content.strip() else None
        return None
