"""Config editor — apply and rollback calibration changes atomically."""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

__all__ = [
    "ConfigEditor",
]

logger = logging.getLogger(__name__)

# Filenames accepted by apply() when a cloud model supplies a key that becomes
# the written basename. Restrict to alphanumerics, underscore, hyphen and dot,
# reject any path separator or ``..`` so a crafted key cannot escape the target
# directory (e.g. a bootstrap file that is later loaded as the system prompt).
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ALLOWED_EXTENSIONS = {".md", ".yaml", ".yml"}


def _safe_filename(name: str, *, default_ext: str = ".md") -> str:
    """Validate and normalise a model-supplied filename.

    Rejects path separators, ``..`` and non-allowlisted extensions. Raises
    ValueError on any attempt to escape the target directory.
    """
    if not name or not _SAFE_FILENAME_RE.match(name):
        raise ValueError(f"Unsafe calibration filename rejected: {name!r}")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"Unsafe calibration filename rejected: {name!r}")
    if not any(name.endswith(ext) for ext in _ALLOWED_EXTENSIONS):
        # No recognised extension → append the default. Keep it inside the
        # allowlist by construction.
        return f"{name}{default_ext}"
    return name


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

    def apply(self, changes: dict[str, Any] | None) -> None:
        """Apply proposed changes from CalibrationAnalyzer.

        Args:
            changes: Dictionary with optional keys: system_prompt, tool_overrides,
                     few_shots, settings, skills, subagent_prompts, department_prompts.

        Any write failure rolls back to the pre-apply state, so a partially-applied
        change set can never leave the calibrated tree inconsistent.
        """
        if changes is None:
            changes = {}
        self._backup_current()
        self._calibrated_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._apply_sections(changes)
        except Exception:
            # S3-13: a partial apply (e.g. disk full, permission error, or a
            # rejected filename mid-way) must not leave the calibrated tree in a
            # half-written state. Roll back to the backed-up state and re-raise.
            logger.exception("[calibration] apply() failed; rolling back")
            self.rollback()
            raise

    def _apply_sections(self, changes: dict[str, Any]) -> None:
        # 1. System prompt overrides
        raw_sp: Any = changes.get("system_prompt")
        if raw_sp is not None and isinstance(raw_sp, dict):
            bootstrap_dir = self._calibrated_dir / "bootstrap"
            bootstrap_dir.mkdir(parents=True, exist_ok=True)
            sp_items = cast(dict[str, str], raw_sp)
            for filename, content in sp_items.items():
                safe = _safe_filename(filename, default_ext=".md")
                target = bootstrap_dir / safe
                target.write_text(content, encoding="utf-8")
                logger.info("[calibration] Updated bootstrap: %s", safe)

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

        # 5. Skill instruction overrides
        raw_sk: Any = changes.get("skills")
        if raw_sk is not None and isinstance(raw_sk, dict):
            skills_dir = self._calibrated_dir / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            sk_items = cast(dict[str, str], raw_sk)
            for skill_id, instructions in sk_items.items():
                safe = _safe_filename(f"{skill_id}.md", default_ext=".md")
                target = skills_dir / safe
                target.write_text(instructions, encoding="utf-8")
                logger.info("[calibration] Updated skill instructions: %s", safe)

        # 6. Subagent prompt overrides
        raw_sa: Any = changes.get("subagent_prompts")
        if raw_sa is not None and isinstance(raw_sa, dict):
            sa_dir = self._calibrated_dir / "bootstrap" / "subagents"
            sa_dir.mkdir(parents=True, exist_ok=True)
            sa_items = cast(dict[str, str], raw_sa)
            for filename, content in sa_items.items():
                safe = _safe_filename(filename, default_ext=".md")
                target = sa_dir / safe
                target.write_text(content, encoding="utf-8")
                logger.info("[calibration] Updated subagent prompt: %s", safe)

        # 7. Department prompt overrides
        raw_dp: Any = changes.get("department_prompts")
        if raw_dp is not None and isinstance(raw_dp, dict):
            dp_dir = self._calibrated_dir / "bootstrap" / "departments"
            dp_dir.mkdir(parents=True, exist_ok=True)
            dp_items = cast(dict[str, str], raw_dp)
            for dept_name, content in dp_items.items():
                safe = _safe_filename(
                    dept_name if str(dept_name).endswith(".md") else f"{dept_name}.md",
                    default_ext=".md",
                )
                target = dp_dir / safe
                target.write_text(content, encoding="utf-8")
                logger.info("[calibration] Updated department prompt: %s", safe)

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

    def load_skill_override(self, skill_id: str) -> str | None:
        """Load calibrated instruction override for a specific skill.

        Returns the override content, or None if no override exists.
        """
        path = self._calibrated_dir / "skills" / f"{skill_id}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

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
