"""Shared extension initialization for CLI and Telegram channels.

Eliminates DRY violation: skill loading, plugin loading, plugin tool
registration, and SkillMatcher creation were duplicated between
``cli.py`` and ``runner.py``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from corpclaw_lite.config.settings import SkillsSettings
from corpclaw_lite.extensions.plugins.registry import PluginRegistry
from corpclaw_lite.extensions.skills.matcher import SkillMatcher, SkillMatcherConfig
from corpclaw_lite.extensions.skills.registry import SkillRegistry
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.extensions.tools.scoped import ScopedTool

__all__ = [
    "load_extensions",
]

logger = logging.getLogger(__name__)


def load_extensions(
    project_root: Path,
    tool_registry: ToolRegistry,
    skills_settings: SkillsSettings,
    full_tool_registry: ToolRegistry | None = None,
) -> tuple[SkillRegistry, PluginRegistry, SkillMatcher | None]:
    """Load skills, plugins, register plugin tools, and create SkillMatcher.

    Shared between CLI ``cmd_chat`` and Telegram ``run_telegram_bot``
    to ensure identical behaviour across channels.

    Returns:
        (skill_registry, plugin_registry, skill_matcher_or_None)
    """
    # ── Skills ──────────────────────────────────────────────────────────
    skill_registry = SkillRegistry()
    skills_dir = project_root / "skills"
    if skills_dir.exists():
        skill_registry.load_directory(skills_dir)

    # ── Plugins ─────────────────────────────────────────────────────────
    plugin_registry = PluginRegistry()
    plugins_dir = project_root / "plugins"
    if plugins_dir.exists():
        plugin_registry.load_directory(plugins_dir)
        for plugin in plugin_registry.list_all():
            for tool in plugin.tools:
                scoped_tool = ScopedTool(
                    tool,
                    source_kind="plugin",
                    source_name=plugin.manifest.name,
                    allowed_departments=plugin.manifest.allowed_departments,
                )
                registered_main = False
                try:
                    tool_registry.register(scoped_tool)
                    registered_main = True
                    logger.info(
                        "Plugin '%s': registered tool '%s'",
                        plugin.manifest.name,
                        scoped_tool.name,
                    )
                except ValueError:
                    logger.warning(
                        "Plugin '%s': tool '%s' conflicts with an existing tool, skipping.",
                        plugin.manifest.name,
                        scoped_tool.name,
                    )
                if full_tool_registry is not None:
                    try:
                        full_tool_registry.register(scoped_tool)
                        logger.info(
                            "Plugin '%s': registered full-registry tool '%s'",
                            plugin.manifest.name,
                            scoped_tool.name,
                        )
                    except ValueError:
                        if registered_main:
                            logger.warning(
                                "Plugin '%s': tool '%s' conflicts in full registry, skipping.",
                                plugin.manifest.name,
                                scoped_tool.name,
                            )

    # ── Skill Matcher (semantic selection) ──────────────────────────────
    skill_matcher: SkillMatcher | None = None
    if skills_settings.selection_mode == "semantic":
        matcher_config = SkillMatcherConfig(
            enabled=True,
            top_k=skills_settings.top_k,
            tfidf_threshold=skills_settings.tfidf_threshold,
            keyword_boost=skills_settings.keyword_boost,
        )
        skill_matcher = SkillMatcher(matcher_config)
        logger.info(
            "Skill semantic selection enabled (top_k=%d, threshold=%.2f)",
            skills_settings.top_k,
            skills_settings.tfidf_threshold,
        )
    else:
        logger.info("Skill selection mode: all (injecting every allowed skill)")

    return skill_registry, plugin_registry, skill_matcher
