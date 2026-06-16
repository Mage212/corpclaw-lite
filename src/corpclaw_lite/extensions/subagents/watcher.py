from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import anyio
import yaml

from corpclaw_lite.extensions.subagents.base import SubagentSpec
from corpclaw_lite.extensions.subagents.registry import SubagentRegistry

__all__ = [
    "SubagentHotReloader",
]

logger = logging.getLogger(__name__)


class SubagentHotReloader:
    """Polls config/subagents/ for YAML changes and hot-reloads specs.

    Uses the same polling pattern as SkillHotReloader — no OS inotify
    dependency, cross-platform, asyncio-native.

    Detects:
    - New YAML files → register spec
    - Modified YAML files → replace existing spec
    - Deleted YAML files → unregister spec
    """

    def __init__(
        self,
        config_dir: Path | str,
        registry: SubagentRegistry,
        poll_interval: float = 10.0,
    ) -> None:
        self._dir = Path(config_dir)
        self._registry = registry
        self._poll_interval = poll_interval
        self._mtimes: dict[Path, float] = {}
        self._known_files: set[Path] = set()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the background polling task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())
            logger.info("SubagentHotReloader started for: %s", self._dir)

    def stop(self) -> None:
        """Cancel the background polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            logger.info("SubagentHotReloader stopped.")

    async def _poll_loop(self) -> None:
        """Poll the directory for mtime changes on .yaml files."""
        await self._scan()
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._scan()
            except Exception as e:
                logger.error("SubagentHotReloader error during scan: %s", e)

    async def _scan(self) -> None:
        """Check all .yaml files, reload any that changed."""
        aio_dir = anyio.Path(self._dir)
        if not await aio_dir.exists():
            return

        current_files: dict[Path, float] = {}
        async for p in aio_dir.glob("*.yaml"):
            sync_p = Path(p)
            stat = await p.stat()
            current_files[sync_p] = stat.st_mtime

        current_paths = set(current_files.keys())

        # Detect deleted files
        deleted = self._known_files - current_paths
        for path in deleted:
            subagent_id = path.stem
            self._registry.unregister(subagent_id)
            self._mtimes.pop(path, None)
            logger.info("SubagentHotReload: '%s' removed (file deleted)", subagent_id)

        # Detect new or modified files
        for path, mtime in current_files.items():
            prev_mtime = self._mtimes.get(path)
            if prev_mtime is None or mtime > prev_mtime:
                spec = self._load_spec(path)
                if spec:
                    self._registry.register(spec)
                    logger.info(
                        "SubagentHotReload: '%s' %s",
                        spec.id,
                        "updated" if prev_mtime else "loaded",
                    )
                self._mtimes[path] = mtime

        self._known_files = current_paths

    @staticmethod
    def _load_spec(path: Path) -> SubagentSpec | None:
        """Parse a single subagent YAML file into a SubagentSpec.

        Field mapping is kept in sync with :meth:`SubagentRegistry.load_directory`
        (registry.py) — both loaders must produce identical specs from the same YAML.
        """
        try:
            data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return SubagentSpec(
                id=data.get("id", path.stem),
                name=data.get("name", path.stem),
                description=data.get("description", "No description"),
                capabilities=data.get("capabilities", []),
                allowed_tools=data.get("allowed_tools", ["*"]),
                allowed_departments=data.get("allowed_departments", ["*"]),
                prompt_path=data.get("prompt_path", ""),
                direct_response=bool(data.get("direct_response", False)),
                max_wall_time_ms=data.get("max_wall_time_ms"),
                terminal_tool=data.get("terminal_tool"),
                required_before_terminal=data.get("required_before_terminal", []),
            )
        except Exception as e:
            logger.error("SubagentHotReloader: failed to load %s: %s", path, e)
            return None
