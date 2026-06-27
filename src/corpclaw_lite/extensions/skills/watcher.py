from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import anyio

from corpclaw_lite.extensions.skills.loader import SkillLoader
from corpclaw_lite.extensions.skills.registry import SkillRegistry

__all__ = [
    "SkillHotReloader",
]

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
        skills_dir: Path | str | list[str | Path],
        registry: SkillRegistry,
        poll_interval: float = 5.0,
    ) -> None:
        if isinstance(skills_dir, list):
            self._dirs: list[Path] = [Path(d) for d in skills_dir]
        else:
            self._dirs = [Path(skills_dir)]
        self._registry = registry
        self._poll_interval = poll_interval
        self._mtimes: dict[Path, float] = {}
        self._known_files: set[Path] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("SkillHotReloader started for: %s", self._dirs)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("SkillHotReloader stopped.")

    async def reload_now(self) -> None:
        """Trigger an immediate rescan (Etap 4: manual reload button)."""
        await self._scan()

    async def _poll_loop(self) -> None:
        """Poll the directories for mtime changes on .md files."""
        # Prime the mtime cache with the on-disk state WITHOUT re-registering.
        # The bootstrap loader already registered skills from these same files;
        # an unprimed first scan would treat every file as "new" and re-register
        # it, spurious-warning "overridden by overlay" against the bootstrap copy.
        await self._prime()
        # Now scan for genuine deltas (new/changed/deleted) since the prime.
        await self._scan()
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._scan()
            except Exception as e:
                logger.error("SkillHotReloader error during scan: %s", e)

    async def _discover_files(self) -> dict[Path, float]:
        """Return {path: mtime} for every .md file across the watched dirs."""
        current_files: dict[Path, float] = {}
        for directory in self._dirs:
            aio_dir = anyio.Path(directory)
            if not await aio_dir.exists():
                continue
            async for p in aio_dir.glob("*.md"):
                sync_p = Path(p)
                stat = await p.stat()
                current_files[sync_p] = stat.st_mtime
        return current_files

    async def _prime(self) -> None:
        """Seed the mtime/known-file caches for files already in the registry.

        Called once before the first scan so skills the bootstrap loader already
        registered are not re-registered (which would log a misleading
        "overridden by overlay" WARNING). Files NOT yet in the registry are left
        uncached, so the first scan still picks them up (covers the case where
        the watcher is the sole loader). Matches a file to its registry entry by
        the skill's ``.path`` to avoid re-parsing frontmatter for the id.
        """
        registered_paths = {
            (skill.path.resolve() if skill.path else None) for skill in self._registry.list_all()
        }
        current_files = await self._discover_files()
        for path, mtime in current_files.items():
            if path.resolve() in registered_paths:
                self._mtimes[path] = mtime
        self._known_files = set(current_files.keys())

    async def _scan(self) -> None:
        """Check all .md files across the directories, reload any that changed."""
        current_files = await self._discover_files()

        current_paths = set(current_files.keys())

        # Detect deleted files
        deleted = self._known_files - current_paths
        for path in deleted:
            skill_id = path.stem
            self._registry.unregister(skill_id)
            self._mtimes.pop(path, None)
            logger.info("HotReload: skill '%s' removed (file deleted)", skill_id)

        # Detect new or modified files
        for path, mtime in current_files.items():
            prev_mtime = self._mtimes.get(path)
            if prev_mtime is None or mtime > prev_mtime:
                skill = SkillLoader.load_from_file(path)
                if skill:
                    self._registry.register(skill, allow_replace=True)
                    logger.info(
                        "HotReload: skill '%s' %s",
                        skill.id,
                        "updated" if prev_mtime else "loaded",
                    )
                self._mtimes[path] = mtime

        self._known_files = current_paths
