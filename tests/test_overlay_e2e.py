"""End-to-end verification of the private-extensions overlay model.

Activates the sibling ``corpclaw-corp`` overlay (the private extensions repo)
through ``settings.extensions.extra_paths`` and asserts the four guarantees the
overlay must provide:

  (a) every extension kind loads from the overlay and is usable through it;
  (b) overlay entries override defaults by id/name/filename (and departments
      union-merge rather than replace);
  (c) activating the overlay leaks no private files into the public repository;
  (d) no traces are left in the public repository after the overlay is used.

The overlay is loaded the same way ``agent.factory._build_extensions_stack`` /
``load_extensions`` / ``BootstrapLoader`` / ``MCPManager`` / ``DepartmentManager``
load it in production — by calling ``resolve_dirs(kind, settings, project_root)``
for each kind. No LLM provider or Docker is required.

These tests skip automatically when the sibling ``corpclaw-corp`` repository is
absent, so CI without the private overlay still passes. They are intended to run
on a developer machine where both ``corpclaw-lite`` and ``corpclaw-corp`` are
checked out side by side.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from corpclaw_lite.config.bootstrap import BootstrapLoader
from corpclaw_lite.config.settings import ExtensionsSettings, Settings, SkillsSettings
from corpclaw_lite.departments.manager import DepartmentManager, resolve_department_files
from corpclaw_lite.extensions.bootstrap import load_extensions
from corpclaw_lite.extensions.mcp.manager import MCPManager
from corpclaw_lite.extensions.paths import resolve_dirs
from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
from corpclaw_lite.extensions.tools.registry import ToolRegistry
from corpclaw_lite.paths import PROJECT_ROOT

if TYPE_CHECKING:
    from corpclaw_lite.extensions.plugins.base import Plugin
    from corpclaw_lite.extensions.skills.base import Skill
    from corpclaw_lite.extensions.subagents.base import SubagentSpec

# ─── Fixtures ────────────────────────────────────────────────────────────────

#: Path to the sibling private overlay repository.
CORPCLAW_CORP = PROJECT_ROOT.parent / "corpclaw-corp"

#: Subdirectories of the public repo whose listing must be invariant across an
#: overlay activation cycle (proves loaders are read-only w.r.t. source dirs).
_PUBLIC_TRACKED_DIRS = ("skills", "plugins", "config")


def _require_overlay() -> Path:
    """Skip the test unless the sibling corpclaw-corp overlay is present."""
    if not CORPCLAW_CORP.exists():
        pytest.skip(
            f"corpclaw-corp overlay not present at {CORPCLAW_CORP}; "
            "W7 overlay verification skipped",
        )
    return CORPCLAW_CORP


def _overlay_settings() -> Settings:
    """Settings that activate the sibling overlay via extra_paths."""
    return Settings(extensions=ExtensionsSettings(extra_paths=[str(CORPCLAW_CORP)]))


@pytest.fixture()
def _git_clean_before() -> str:  # pyright: ignore[reportUnusedFunction]
    """Snapshot ``git status --porcelain`` of the public repo before a test.

    The overlay must never dirty the public working tree, so we capture the
    pre-state and compare against it afterwards. We snapshot rather than assume
    "empty" so the test also passes when the developer has unrelated local
    changes already staged/modified — only NEW changes caused by the overlay
    count as a leak.
    """
    return _git_porcelain()


def _git_porcelain() -> str:
    """Return the public repo's ``git status --porcelain`` output (empty string
    on a clean tree)."""
    result = subprocess.run(  # noqa: S603 — known command, no untrusted input
        ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _public_dir_listing() -> set[str]:
    """Return the set of relative file paths under the tracked public subdirs.

    Used to prove overlay loading does not add/remove files in the public
    source tree.
    """
    listing: set[str] = set()
    for sub in _PUBLIC_TRACKED_DIRS:
        root = PROJECT_ROOT / sub
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                listing.add(str(path.relative_to(PROJECT_ROOT)))
    return listing


def _load_subagents(settings: Settings) -> SubagentRegistry:
    """Load subagents from default + overlay dirs, mirroring factory behaviour."""
    registry = SubagentRegistry()
    for config_dir in resolve_dirs("subagents", settings, PROJECT_ROOT):
        if config_dir.exists():
            registry.load_directory(config_dir)
    return registry


def _load_bootstrap(settings: Settings) -> BootstrapLoader:
    dirs: list[str | Path] = list(resolve_dirs("bootstrap", settings, PROJECT_ROOT))
    return BootstrapLoader(dirs)


def _load_mcp(settings: Settings) -> MCPManager:
    paths: list[str | Path] = [p for p in resolve_dirs("mcp", settings, PROJECT_ROOT) if p.exists()]
    return MCPManager(config_path=paths)


def _load_departments(settings: Settings) -> DepartmentManager:
    """Load default departments (replace) then overlays (union-merge)."""
    manager = DepartmentManager()
    for index, path in enumerate(resolve_department_files(settings, PROJECT_ROOT)):
        if not path.exists():
            continue
        manager.load_file(path, merge=index > 0)
    return manager


# ─── (a) Load + usable ───────────────────────────────────────────────────────


def test_overlay_loads_all_extension_types() -> None:
    """Every kind of mock extension present in corpclaw-corp is discoverable."""
    _require_overlay()
    settings = _overlay_settings()

    tool_registry = ToolRegistry()
    full_registry = ToolRegistry()
    skills, plugins, _ = load_extensions(
        settings,
        PROJECT_ROOT,
        tool_registry,
        SkillsSettings(),
        full_tool_registry=full_registry,
    )
    subagents = _load_subagents(settings)
    bootstrap = _load_bootstrap(settings)
    mcp = _load_mcp(settings)
    departments = _load_departments(settings)

    # Skill ADD (new id only present in overlay).
    corp_greeting: Skill | None = skills.get("corp-greeting")
    assert corp_greeting is not None, "overlay skill 'corp-greeting' not loaded"

    # Plugin ADD + its tool registered in both registries.
    corp_echo_plugin: Plugin | None = plugins.get("corp-echo")
    assert corp_echo_plugin is not None, "overlay plugin 'corp-echo' not loaded"
    assert "corp_echo" in tool_registry.items(), "plugin tool not in main registry"
    assert "corp_echo" in full_registry.items(), "plugin tool not in full registry"
    # Plugin-bundled skill is present.
    assert corp_echo_plugin.skill is not None
    assert corp_echo_plugin.skill.id == "corp-echo-skill"

    # Subagent ADD (new id only present in overlay).
    corp_agent: SubagentSpec | None = subagents.get("corp-agent")
    assert corp_agent is not None, "overlay subagent 'corp-agent' not loaded"

    # Bootstrap ADD: uniquely-named file appears in the assembled prompt.
    system_prompt = bootstrap.get_system_prompt()
    assert "CORP POLICIES" in system_prompt, "overlay bootstrap file not appended"

    # MCP ADD: overlay server appears in the merged config.
    server_names = [s.get("name") for s in mcp.load_config_raw()]
    assert "corp-mock-echo" in server_names, "overlay MCP server not merged"

    # Departments: union-merge adds corp_echo to marketing + new corp-internal.
    marketing = departments.get_department("marketing")
    assert marketing is not None
    assert "corp_echo" in marketing.allowed_tools, "overlay tool not unioned into marketing"
    # Default marketing tools must survive the union (not replaced).
    assert "read_file" in marketing.allowed_tools, "default tool lost in union-merge"
    assert departments.get_department("corp-internal") is not None, "new overlay dept not added"


# ─── (b) Override semantics ──────────────────────────────────────────────────


def test_overlay_skill_replaces_default(caplog: pytest.LogCaptureFixture) -> None:
    """Overlay skill with the same id as a default wins (replace-by-id)."""
    _require_overlay()
    settings = _overlay_settings()
    tool_registry = ToolRegistry()
    skills, _, _ = load_extensions(settings, PROJECT_ROOT, tool_registry, SkillsSettings())

    translator = skills.get("translator")
    assert translator is not None
    assert "[CORP OVERLAY]" in translator.instructions
    # The default translator body contains this example phrase; the overlay body
    # does not — its absence proves the default was replaced, not merged.
    assert "Welcome to our company." not in translator.instructions
    assert caplog.text  # at least the default skill-load INFO logs
    assert any(
        "overridden by overlay" in record.getMessage() and "translator" in record.getMessage()
        for record in caplog.records
    ), "no WARN log for skill override"


def test_overlay_subagent_replaces_default(caplog: pytest.LogCaptureFixture) -> None:
    """Overlay subagent with the same id as a default wins; WARN is logged."""
    _require_overlay()
    settings = _overlay_settings()
    caplog.set_level(logging.WARNING, logger="corpclaw_lite.extensions.subagents.registry")

    subagents = _load_subagents(settings)
    research = subagents.get("research-agent")
    assert research is not None
    assert research.name == "[CORP OVERLAY] Corp Research"
    assert any(
        "overridden by overlay" in record.getMessage() and "research-agent" in record.getMessage()
        for record in caplog.records
    ), "no WARN log for subagent override"


def test_overlay_bootstrap_replaces_default_soul(caplog: pytest.LogCaptureFixture) -> None:
    """Overlay SOUL.md replaces the default SOUL.md by filename; new files are added."""
    _require_overlay()
    settings = _overlay_settings()
    caplog.set_level(logging.WARNING, logger="corpclaw_lite.config.bootstrap")

    bootstrap = _load_bootstrap(settings)
    prompt = bootstrap.get_system_prompt()

    assert "corp soul content" in prompt, "overlay SOUL.md content missing"
    # The default SOUL.md contains these phrases; the overlay does not — their
    # absence proves per-filename override.
    assert "Agent Identity" not in prompt, "default SOUL.md not overridden"
    assert "Docker sandbox" not in prompt, "default SOUL.md not overridden"
    assert "CORP POLICIES" in prompt, "uniquely-named overlay file not added"
    assert any(
        "overridden by overlay" in record.getMessage() and "SOUL.md" in record.getMessage()
        for record in caplog.records
    ), "no WARN log for bootstrap override"


def test_overlay_bootstrap_department_prompt_replaces_default() -> None:
    """Overlay department prompt wins over the default (first match high→low)."""
    _require_overlay()
    settings = _overlay_settings()
    bootstrap = _load_bootstrap(settings)

    prompt = bootstrap.get_department_prompt("marketing")
    assert prompt is not None
    assert "corp marketing prompt" in prompt
    assert "Excel normalization" not in prompt, "default marketing prompt not overridden"


def test_overlay_departments_union_not_replace() -> None:
    """Departments use union-merge: overlay EXTENDS allowlists, does not replace."""
    _require_overlay()
    settings = _overlay_settings()

    # Load default-only first to capture the baseline marketing allowlist.
    default_manager = DepartmentManager()
    for path in resolve_department_files(Settings(), PROJECT_ROOT):
        if path.exists():
            default_manager.load_file(path)
    default_marketing = default_manager.get_department("marketing")
    assert default_marketing is not None
    default_tools = set(default_marketing.allowed_tools)
    assert "corp_echo" not in default_tools, "precondition: corp_echo is overlay-only"

    # Now load with the overlay and confirm union semantics.
    overlay_manager = _load_departments(settings)
    marketing = overlay_manager.get_department("marketing")
    assert marketing is not None
    merged_tools = set(marketing.allowed_tools)
    assert "corp_echo" in merged_tools, "overlay tool not unioned in"
    assert default_tools.issubset(merged_tools), "default tools lost in union-merge"


# ─── (c) Plugin tool usability via subprocess ────────────────────────────────


@pytest.mark.asyncio
async def test_overlay_plugin_tool_executes_via_subprocess() -> None:
    """The overlay plugin's tool.py is loaded and executed through the
    PluginToolProxy → sandbox_worker subprocess path (full e2e usability)."""
    _require_overlay()
    settings = _overlay_settings()
    tool_registry = ToolRegistry()
    load_extensions(settings, PROJECT_ROOT, tool_registry, SkillsSettings())

    tool = tool_registry.get("corp_echo")
    assert tool is not None
    # ScopedTool wraps the PluginToolProxy; execute proxies to the subprocess.
    result = await tool.execute(text="hello overlay")
    assert result == "[CORP-ECHO] hello overlay"


# ─── (d1) Privacy: no git changes in public repo ────────────────────────────


def test_overlay_leaves_no_git_changes_in_public_repo(_git_clean_before: str) -> None:
    """After a full overlay load cycle, the public repo's git status is unchanged."""
    _require_overlay()
    settings = _overlay_settings()

    tool_registry = ToolRegistry()
    full_registry = ToolRegistry()
    load_extensions(
        settings,
        PROJECT_ROOT,
        tool_registry,
        SkillsSettings(),
        full_tool_registry=full_registry,
    )
    _load_subagents(settings)
    _load_bootstrap(settings)
    _load_mcp(settings)
    _load_departments(settings)

    after = _git_porcelain()
    assert after == _git_clean_before, (
        "overlay activation changed the public repo's git status — private "
        f"content may have leaked.\nBefore:\n{_git_clean_before}\nAfter:\n{after}"
    )


# ─── (d2) Privacy: plugin bytecode stays in the overlay ─────────────────────


def test_overlay_plugin_pycache_stays_in_overlay_only() -> None:
    """Importing the overlay plugin's tool.py must not write bytecode anywhere
    under the public repository. The sandbox_worker writes .pyc into the
    plugin's own ``__pycache__`` (inside the overlay), never into the public
    tree."""
    _require_overlay()
    settings = _overlay_settings()
    tool_registry = ToolRegistry()
    load_extensions(settings, PROJECT_ROOT, tool_registry, SkillsSettings())

    leaks: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("_plugin_tool") and name.endswith((".pyc", ".pyo")):
            leaks.append(path)
        if path.parent.name == "__pycache__" and "_plugin_tool" in name:
            leaks.append(path)
    assert not leaks, f"plugin tool bytecode leaked into public repo: {[str(p) for p in leaks]}"


# ─── (d3) Privacy: resolve_dirs without overlay returns default only ─────────


def test_resolve_dirs_without_overlay_returns_default_only() -> None:
    """With no overlay configured, every kind resolves to exactly the default
    path. Proves overlay content is never copied into the default directories —
    the registries hold in-memory objects, not written files."""
    _require_overlay()  # only asserts the sibling exists; this test uses empty Settings
    settings = Settings()  # empty extra_paths

    for kind in ("skills", "plugins", "subagents", "bootstrap", "mcp"):
        resolved = resolve_dirs(kind, settings, PROJECT_ROOT)
        assert len(resolved) == 1, f"{kind}: expected 1 default dir, got {len(resolved)}"
        default_subpath = {
            "skills": "skills",
            "plugins": "plugins",
            "subagents": "config/subagents",
            "bootstrap": "config/bootstrap",
            "mcp": "config/mcp_servers.yaml",
        }[kind]
        assert resolved[0] == (PROJECT_ROOT / default_subpath).resolve(), (
            f"{kind}: resolved default path mismatch"
        )


# ─── (d4) Privacy: public dir listing is invariant ──────────────────────────


def test_overlay_does_not_modify_public_dirs(_git_clean_before: str) -> None:
    """The set of files under skills/, plugins/, config/ in the public repo is
    identical before and after a full overlay load cycle."""
    _require_overlay()
    settings = _overlay_settings()
    before = _public_dir_listing()

    tool_registry = ToolRegistry()
    full_registry = ToolRegistry()
    load_extensions(
        settings,
        PROJECT_ROOT,
        tool_registry,
        SkillsSettings(),
        full_tool_registry=full_registry,
    )
    _load_subagents(settings)
    _load_bootstrap(settings)
    _load_mcp(settings)
    _load_departments(settings)

    after = _public_dir_listing()
    assert before == after, (
        "public source tree changed across overlay activation.\n"
        f"Added: {sorted(after - before)}\nRemoved: {sorted(before - after)}"
    )
    # Re-check git cleanliness too (cheap, and the strongest single guarantee).
    assert _git_porcelain() == _git_clean_before
