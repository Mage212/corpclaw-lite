"""Shared extension initialization for the agent stack.

Skill loading, plugin loading, plugin tool registration, and SkillMatcher
creation are centralised here and called from ``factory.build_agent_stack()``
(``agent/factory.py``), so every channel (CLI, Telegram, Web) shares identical
extension-loading behaviour.

Extensions are loaded from the default project directories plus any overlay
paths configured in ``settings.extensions.extra_paths`` (mirror-layout). Overlay
entries override defaults by id/name (later paths win).
"""

from __future__ import annotations

import logging
from pathlib import Path

from corpclaw_lite.config.settings import Settings, SkillsSettings
from corpclaw_lite.extensions.paths import resolve_dirs
from corpclaw_lite.extensions.plugins.base import Plugin
from corpclaw_lite.extensions.plugins.registry import PluginRegistry
from corpclaw_lite.extensions.skills.matcher import SkillMatcher, SkillMatcherConfig
from corpclaw_lite.extensions.skills.registry import SkillRegistry
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.extensions.tools.scoped import ScopedTool

__all__ = [
    "load_extensions",
]

logger = logging.getLogger(__name__)


def _register_plugin_tools(
    plugin: Plugin,
    tool_registry: ToolRegistry,
    full_tool_registry: ToolRegistry | None,
    allow_replace: bool,
) -> None:
    """Register a plugin's tools as ScopedTools into one or both registries.

    When ``allow_replace=True`` (overlay), tool-name collisions silently replace
    the existing tool so the overlay plugin's implementation wins.
    """
    for tool in plugin.tools:
        scoped_tool = ScopedTool(
            tool,
            source_kind="plugin",
            source_name=plugin.manifest.name,
            allowed_departments=plugin.manifest.allowed_departments,
        )
        tool_registry.register(scoped_tool, allow_replace=allow_replace)
        logger.info(
            "Plugin '%s': registered tool '%s'",
            plugin.manifest.name,
            scoped_tool.name,
        )
        if full_tool_registry is not None:
            full_tool_registry.register(scoped_tool, allow_replace=allow_replace)
            logger.info(
                "Plugin '%s': registered full-registry tool '%s'",
                plugin.manifest.name,
                scoped_tool.name,
            )


def _unregister_plugin_tools(
    plugin: Plugin,
    tool_registry: ToolRegistry,
    full_tool_registry: ToolRegistry | None,
) -> None:
    """Remove a plugin's tools from one or both registries (used before overlay
    replacement, so stale tools from the superseded plugin don't linger)."""
    for tool in plugin.tools:
        tool_registry.unregister(tool.name)
        if full_tool_registry is not None:
            full_tool_registry.unregister(tool.name)


def load_extensions(
    settings: Settings,
    project_root: Path,
    tool_registry: ToolRegistry,
    skills_settings: SkillsSettings,
    full_tool_registry: ToolRegistry | None = None,
) -> tuple[SkillRegistry, PluginRegistry, SkillMatcher | None]:
    """Load skills, plugins, register plugin tools, and create SkillMatcher.

    Loads from the default directories plus overlay paths
    (``settings.extensions.extra_paths``, mirror-layout). Overlay entries override
    defaults: a skill/plugin with the same id/name as a default one wins and its
    tools replace the default's tools.

    Called from ``factory.build_agent_stack()`` (the single assembly point for
    the agent stack), ensuring identical extension-loading behaviour across all
    channels (CLI, Telegram, Web).

    Returns:
        (skill_registry, plugin_registry, skill_matcher_or_None)
    """
    # ── Skills ──────────────────────────────────────────────────────────
    skill_registry = SkillRegistry()
    skill_dirs = resolve_dirs("skills", settings, project_root)
    for index, skills_dir in enumerate(skill_dirs):
        if not skills_dir.exists():
            continue
        # First dir (index 0) is the default; later dirs are overlays.
        skill_registry.load_directory(skills_dir, allow_replace=index > 0)

    # ── Plugins ─────────────────────────────────────────────────────────
    plugin_registry = PluginRegistry()
    plugin_dirs = resolve_dirs("plugins", settings, project_root)
    for index, plugins_dir in enumerate(plugin_dirs):
        if not plugins_dir.exists():
            continue
        is_overlay = index > 0
        # Snapshot plugins present before loading this dir, so overridden ones
        # can have their old tools removed first (prevents stale orphan tools
        # when an overlay plugin drops or renames a tool vs the default).
        before = {p.manifest.name: p for p in plugin_registry.list_all()}
        plugin_registry.load_directory(plugins_dir, allow_replace=is_overlay)

        for plugin in plugin_registry.list_all():
            name = plugin.manifest.name
            is_overridden = is_overlay and name in before
            # On the default dir pass everything is new; on an overlay pass only
            # plugins that this dir added or overrode should have tools registered.
            if is_overlay and name in before and before[name] is plugin:
                continue  # not actually changed by this dir
            if is_overridden:
                _unregister_plugin_tools(before[name], tool_registry, full_tool_registry)
            _register_plugin_tools(
                plugin, tool_registry, full_tool_registry, allow_replace=is_overlay
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
