import logging
from pathlib import Path
from typing import Any

import yaml

from corpclaw_lite.extensions.subagents.base import SubagentSpec

logger = logging.getLogger(__name__)


class SubagentRegistry:
    """Loads and manages Subagent specifications from YAML files."""

    def __init__(self) -> None:
        self._subagents: dict[str, SubagentSpec] = {}

    def load_directory(self, config_dir: Path | str) -> None:
        """Load all subagent YAML definitions from a directory."""
        dir_path = Path(config_dir)
        if not dir_path.exists() or not dir_path.is_dir():
            logger.warning("Subagents config directory not found: %s", dir_path)
            return

        loaded_count = 0
        for yaml_file in dir_path.glob("*.yaml"):
            try:
                with open(yaml_file, encoding="utf-8") as f:
                    data: dict[str, Any] = yaml.safe_load(f) or {}

                spec = SubagentSpec(
                    id=data.get("id", yaml_file.stem),
                    name=data.get("name", yaml_file.stem),
                    description=data.get("description", "No description"),
                    capabilities=data.get("capabilities", []),
                    allowed_tools=data.get("allowed_tools", ["*"]),
                    prompt_path=data.get("prompt_path", ""),
                )
                self.register(spec)
                loaded_count += 1
            except Exception as e:
                logger.error("Failed to load subagent spec %s: %s", yaml_file, e)

        logger.info("Loaded %d subagents from %s", loaded_count, dir_path)

    def register(self, spec: SubagentSpec) -> None:
        self._subagents[spec.id] = spec

    def get_spec(self, subagent_id: str) -> SubagentSpec | None:
        return self._subagents.get(subagent_id)

    def list_all(self) -> list[SubagentSpec]:
        return list(self._subagents.values())
