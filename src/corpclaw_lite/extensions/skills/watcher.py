from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from corpclaw_lite.extensions.skills.loader import SkillLoader
from corpclaw_lite.extensions.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillHotReloader:
    """
    A lightweight file watcher that polls the skills directory for changes
    and hot-reloads new or modified skill files without restarting the process.

    Uses asyncio polling (interval-based) rather than OS inotify to stay
    dependency-free and cross-platform.
    """

    def __init__(
        self,
        skills_dir: Path | str,
        registry: SkillRegistry,
        poll_interval: float = 5.0,
    ) -> None:
        self._dir = Path(skills_dir)
        self._registry = registry
        self._poll_interval = poll_interval
        self._mtimes: dict[Path, float] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("SkillHotReloader started for: %s", self._dir)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("SkillHotReloader stopped.")

    async def _poll_loop(self) -> None:
        """Poll the directory for mtime changes on .md files."""
        # Do an initial scan on startup
        await self._scan()
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._scan()
            except Exception as e:
                logger.error("SkillHotReloader error during scan: %s", e)

    async def _scan(self) -> None:
        """Check all .md files in the directory, reload any that changed."""
        if not self._dir.exists():
            return

        current_files = {p: p.stat().st_mtime for p in self._dir.glob("*.md")}

        for path, mtime in current_files.items():
            prev_mtime = self._mtimes.get(path)
            if prev_mtime is None or mtime > prev_mtime:
                skill = SkillLoader.load_from_file(path)
                if skill:
                    self._registry.register(skill)
                    logger.info(
                        "HotReload: skill '%s' %s",
                        skill.id,
                        "updated" if prev_mtime else "loaded",
                    )
                self._mtimes[path] = mtime
