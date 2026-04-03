"""Config editor — apply and rollback calibration changes atomically."""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

__all__ = [
    "ConfigEditor",
]

logger = logging.getLogger(__name__)


class ConfigEditor:
    """Apply and rollback calibration changes to configuration files.

    All calibrated configs are stored in ``config/calibrated/``.
    Before each apply, the current state is backed up to allow rollback.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root
        self._calibrated_dir = project_root / "config" / "calibrated"
        self._backup_dir = self._calibrated_dir / ".backup"

    @property
    def calibrated_dir(self) -> Path:
        """Return calibrated config directory."""
        return self._calibrated_dir

    def apply(self, changes: dict[str, Any]) -> None:
        """Apply proposed changes from CalibrationAnalyzer.

        Args:
            changes: Dictionary with optional keys: system_prompt, tool_overrides,
                     few_shots, settings, skills.
        """
        self._backup_current()
        self._calibrated_dir.mkdir(parents=True, exist_ok=True)

        # 1. System prompt overrides
        raw_sp: Any = changes.get("system_prompt")
        if raw_sp is not None and isinstance(raw_sp, dict):
            bootstrap_dir = self._calibrated_dir / "bootstrap"
            bootstrap_dir.mkdir(parents=True, exist_ok=True)
            sp_items = cast(dict[str, str], raw_sp)
            for filename, content in sp_items.items():
                target = bootstrap_dir / filename
                target.write_text(content, encoding="utf-8")
                logger.info("[calibration] Updated bootstrap: %s", filename)

        # 2. Tool description overrides
        raw_to: Any = changes.get("tool_overrides")
        if raw_to is not None and isinstance(raw_to, dict):
            path = self._calibrated_dir / "tool_overrides.yaml"
            typed_overrides = cast(dict[str, Any], raw_to)
            self._write_yaml(path, {"overrides": typed_overrides})
            logger.info(
                "[calibration] Updated tool overrides: %d tools",
                len(typed_overrides),
            )

        # 3. Few-shot examples
        raw_fs: Any = changes.get("few_shots")
        if raw_fs is not None and isinstance(raw_fs, list):
            path = self._calibrated_dir / "few_shots.yaml"
            typed_fs = cast(list[dict[str, Any]], raw_fs)
            self._write_yaml(path, {"examples": typed_fs})
            logger.info(
                "[calibration] Updated few-shot examples: %d examples",
                len(typed_fs),
            )

        # 4. Settings overrides
        raw_st: Any = changes.get("settings")
        if raw_st is not None and isinstance(raw_st, dict):
            path = self._calibrated_dir / "settings_override.yaml"
            typed_settings = cast(dict[str, Any], raw_st)
            self._write_yaml(path, {"agent": typed_settings})
            logger.info(
                "[calibration] Updated settings override: %s",
                list(typed_settings.keys()),
            )

    def rollback(self) -> None:
        """Restore previous calibration state from backup."""
        if not self._backup_dir.exists():
            logger.warning("[calibration] No backup found, nothing to rollback")
            return

        # Remove current calibrated (except .backup)
        for item in self._calibrated_dir.iterdir():
            if item.name == ".backup":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        # Restore from backup
        for item in self._backup_dir.iterdir():
            dest = self._calibrated_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

        # Remove backup
        shutil.rmtree(self._backup_dir)
        logger.info("[calibration] Rolled back to previous calibration state")

    def reset(self) -> None:
        """Clear all calibrated configs."""
        if self._calibrated_dir.exists():
            shutil.rmtree(self._calibrated_dir)
            logger.info("[calibration] Cleared all calibrated configs")

    def save_metadata(
        self,
        model_id: str,
        score: float,
        passed: int,
        total: int,
        iterations: int,
    ) -> None:
        """Save calibration metadata for later validation.

        When loading calibrated configs, the system can check if the current
        model matches the calibrated model_id.
        """
        self._calibrated_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "model_id": model_id,
            "score_pct": round(score, 1),
            "passed": passed,
            "total": total,
            "iterations": iterations,
            "calibrated_at": datetime.now(UTC).isoformat(),
        }
        path = self._calibrated_dir / "metadata.yaml"
        self._write_yaml(path, metadata)
        logger.info("[calibration] Saved metadata: model=%s score=%.1f%%", model_id, score)

    def load_metadata(self) -> dict[str, Any] | None:
        """Load calibration metadata if available."""
        path = self._calibrated_dir / "metadata.yaml"
        if not path.exists():
            return None
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data

    def load_few_shots(self) -> list[dict[str, Any]]:
        """Load calibrated few-shot examples."""
        path = self._calibrated_dir / "few_shots.yaml"
        if not path.exists():
            return []
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        examples: list[dict[str, Any]] = data.get("examples", [])
        return examples

    def load_tool_overrides(self) -> dict[str, Any]:
        """Load tool description overrides."""
        path = self._calibrated_dir / "tool_overrides.yaml"
        if not path.exists():
            return {}
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        overrides: dict[str, Any] = data.get("overrides", {})
        return overrides

    def _backup_current(self) -> None:
        """Backup current calibrated state before applying new changes."""
        if self._backup_dir.exists():
            shutil.rmtree(self._backup_dir)

        if not self._calibrated_dir.exists():
            return

        self._backup_dir.mkdir(parents=True, exist_ok=True)
        for item in self._calibrated_dir.iterdir():
            if item.name == ".backup":
                continue
            dest = self._backup_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)

        logger.debug("[calibration] Backed up current state to %s", self._backup_dir)

    @staticmethod
    def _write_yaml(path: Path, data: Any) -> None:
        """Write YAML file with consistent formatting."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
