from __future__ import annotations

from pathlib import Path

import pytest

from corpclaw_lite.extensions.subagents.registry import SubagentRegistry
from corpclaw_lite.extensions.subagents.watcher import SubagentHotReloader


def _write_subagent(path: Path, *, description: str = "Initial") -> None:
    path.write_text(
        "id: research-agent\n"
        "name: Research\n"
        f"description: {description}\n"
        "capabilities: [web]\n"
        "allowed_tools: [research_search]\n"
        "allowed_departments: [engineering]\n"
        "prompt_path: config/bootstrap/subagents/research.md\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio()
async def test_subagent_hot_reloader_loads_updates_and_unregisters(tmp_path: Path) -> None:
    registry = SubagentRegistry()
    spec_path = tmp_path / "research-agent.yaml"
    _write_subagent(spec_path)

    reloader = SubagentHotReloader(tmp_path, registry)
    await reloader._scan()

    spec = registry.get("research-agent")
    assert spec is not None
    assert spec.description == "Initial"
    assert spec.allowed_tools == ["research_search"]

    reloader._mtimes[spec_path] = 0.0
    _write_subagent(spec_path, description="Updated")
    await reloader._scan()

    updated = registry.get("research-agent")
    assert updated is not None
    assert updated.description == "Updated"

    spec_path.unlink()
    await reloader._scan()

    assert registry.get("research-agent") is None


@pytest.mark.asyncio()
async def test_subagent_hot_reloader_ignores_missing_dir_and_invalid_yaml(tmp_path: Path) -> None:
    registry = SubagentRegistry()
    missing = tmp_path / "missing"

    missing_reloader = SubagentHotReloader(missing, registry)
    await missing_reloader._scan()
    assert registry.list_all() == []

    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text("id: [broken", encoding="utf-8")

    reloader = SubagentHotReloader(tmp_path, registry)
    await reloader._scan()

    assert registry.list_all() == []


@pytest.mark.asyncio()
async def test_subagent_hot_reloader_prime_does_not_re_register_bootstrap_loaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: when the bootstrap loader already registered a subagent, the
    watcher's startup prime + first scan must NOT re-register it and emit a
    spurious 'overridden by overlay' WARNING.

    Before the fix, the first scan treated every on-disk file as "new" (mtime
    unprimed) and re-registered, hitting the overlay branch.
    """
    registry = SubagentRegistry()
    spec_path = tmp_path / "research-agent.yaml"
    _write_subagent(spec_path)

    # Simulate the bootstrap loader registering the subagent from the same file.
    registry.load_directory(tmp_path)

    reloader = SubagentHotReloader(tmp_path, registry)
    with caplog.at_level("WARNING"):
        await reloader._prime()
        await reloader._scan()

    overlay_warnings = [r for r in caplog.records if "overridden by overlay" in r.getMessage()]
    assert overlay_warnings == [], "startup prime should not re-register bootstrap-loaded subagents"

    spec = registry.get("research-agent")
    assert spec is not None
    assert spec.description == "Initial"
