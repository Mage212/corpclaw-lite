"""Tests for the central extension-path resolver (``extensions/paths.py``).

These cover the contract for PR-1: ``resolve_dirs`` returns the ordered list of
extension paths per kind, with mirror-layout overlays applied after the default,
and guards against empty strings (cwd-leak from an unresolved ``${VAR}``) and
non-existent paths.
"""

from __future__ import annotations

from pathlib import Path

from corpclaw_lite.config.settings import ExtensionsSettings, Settings
from corpclaw_lite.extensions.paths import ExtensionKind, resolve_dirs

_ALL_KINDS: tuple[ExtensionKind, ...] = ("skills", "plugins", "subagents", "mcp", "bootstrap")


def _settings(extra_paths: list[str]) -> Settings:
    return Settings(extensions=ExtensionsSettings(extra_paths=extra_paths))


def test_resolve_dirs_default_only(tmp_path: Path) -> None:
    """Empty extra_paths → only the default path is returned for every kind."""
    settings = _settings([])
    for kind in _ALL_KINDS:
        result = resolve_dirs(kind, settings, tmp_path)
        assert result == [tmp_path / _expected_subpath(kind)]


def test_resolve_dirs_with_overlay(tmp_path: Path) -> None:
    """An existing overlay dir is appended after the default."""
    overlay = tmp_path / "overlay"
    (overlay / "skills").mkdir(parents=True)
    settings = _settings([str(overlay)])
    result = resolve_dirs("skills", settings, tmp_path)
    assert result == [
        (tmp_path / "skills").resolve(),
        (overlay / "skills").resolve(),
    ]


def test_resolve_dirs_multiple_overlays_preserve_order(tmp_path: Path) -> None:
    """Overlays are appended in list order; later = higher priority (PR-2 semantics)."""
    overlay_a = tmp_path / "a"
    overlay_b = tmp_path / "b"
    (overlay_a / "plugins").mkdir(parents=True)
    (overlay_b / "plugins").mkdir(parents=True)
    settings = _settings([str(overlay_a), str(overlay_b)])
    result = resolve_dirs("plugins", settings, tmp_path)
    assert result == [
        (tmp_path / "plugins").resolve(),
        (overlay_a / "plugins").resolve(),
        (overlay_b / "plugins").resolve(),
    ]


def test_resolve_dirs_skips_missing(tmp_path: Path) -> None:
    """A non-existent overlay subpath is filtered out, default remains."""
    settings = _settings([str(tmp_path / "does-not-exist")])
    result = resolve_dirs("skills", settings, tmp_path)
    assert result == [(tmp_path / "skills").resolve()]


def test_resolve_dirs_skips_empty_string(tmp_path: Path) -> None:
    """An empty extra_path entry (from an unresolved ${VAR}) is skipped, not cwd-leaked.

    Without the guard, ``Path("") / "skills" == "skills"`` resolves against the
    process cwd; if ``skills/`` exists there it would be silently injected as an
    overlay. Empty/whitespace entries must be dropped.
    """
    settings = _settings(["", "   ", "\t"])
    result = resolve_dirs("skills", settings, tmp_path)
    assert result == [(tmp_path / "skills").resolve()]


def test_resolve_dirs_kind_subpath_mapping(tmp_path: Path) -> None:
    """Each kind maps to the correct mirror-layout subpath."""
    overlay = tmp_path / "overlay"
    for sub in ("skills", "plugins", "config/subagents", "config/bootstrap"):
        (overlay / sub).mkdir(parents=True, exist_ok=True)
    settings = _settings([str(overlay)])

    assert resolve_dirs("skills", settings, tmp_path)[1] == (overlay / "skills").resolve()
    assert resolve_dirs("plugins", settings, tmp_path)[1] == (overlay / "plugins").resolve()
    assert (
        resolve_dirs("subagents", settings, tmp_path)[1]
        == (overlay / "config" / "subagents").resolve()
    )
    assert (
        resolve_dirs("bootstrap", settings, tmp_path)[1]
        == (overlay / "config" / "bootstrap").resolve()
    )


def test_resolve_dirs_mcp_is_file(tmp_path: Path) -> None:
    """For mcp the overlay path points at a file, not a directory."""
    overlay = tmp_path / "overlay"
    (overlay / "config").mkdir(parents=True)
    mcp_file = overlay / "config" / "mcp_servers.yaml"
    mcp_file.write_text("servers: []\n")
    settings = _settings([str(overlay)])

    result = resolve_dirs("mcp", settings, tmp_path)
    assert result == [
        (tmp_path / "config" / "mcp_servers.yaml").resolve(),
        mcp_file.resolve(),
    ]


def test_resolve_dirs_default_returned_even_when_missing(tmp_path: Path) -> None:
    """The default path is always present regardless of existence on disk.

    Call-sites already handle a missing default (they check ``.exists()`` before
    loading); the resolver must not silently drop it.
    """
    settings = _settings([])
    # tmp_path has no skills/ dir; default still returned.
    result = resolve_dirs("skills", settings, tmp_path)
    assert result == [(tmp_path / "skills").resolve()]


def _expected_subpath(kind: ExtensionKind) -> Path:
    subpaths = {
        "skills": "skills",
        "plugins": "plugins",
        "subagents": "config/subagents",
        "mcp": "config/mcp_servers.yaml",
        "bootstrap": "config/bootstrap",
    }
    return Path(subpaths[kind])
