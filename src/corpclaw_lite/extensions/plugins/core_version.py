"""Core-version constraint checking for plugin manifests.

Lets a plugin declare ``requires_core`` and have the running core validate it,
so an overlay plugin built for one core line fails loudly (warn-and-skip)
instead of silently breaking when the core moves on.

Supported constraint syntax:
- ``""`` (empty) — no constraint, always satisfied.
- ``"0.1.11"`` — exact match (major.minor.patch equal).
- ``"^0.1.11"`` — caret: for 0.x pins the minor (0.1.x compatible, 0.2.0 not);
  for 1.x+ pins the major.

Semver 0.x caret rule: before 1.0, a minor bump is a breaking change, so caret
fixes the minor for 0.x constraints. This is the case that matters for this
project today (0.1.11 -> 0.2.0 is the break we want to catch).
"""

from __future__ import annotations

__all__ = [
    "get_core_version",
    "satisfies_core_version",
]

# Sentinel for when the core version cannot be determined (editable/source
# checkout without installed distribution metadata).
_UNKNOWN = "0.0.0+unknown"


def get_core_version() -> str:
    """Return the running core version, or the unknown sentinel."""
    from corpclaw_lite import __version__

    return __version__


def _parse_version(v: str) -> tuple[int, int, int]:
    """Parse a dotted version into a (major, minor, patch) tuple of ints.

    Non-numeric components and pre-release suffixes (e.g. ``0.1.11-rc1``) are
    coerced: digits are kept, anything else treated as 0. Missing components
    default to 0 (``"1.2"`` -> ``(1, 2, 0)``).
    """
    parts = v.split(".")
    nums: list[int] = []
    for part in parts[:3]:
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def satisfies_core_version(constraint: str, core_version: str | None = None) -> bool:
    """Return True if ``core_version`` satisfies ``constraint``.

    Args:
        constraint: ``""``, a bare exact version, or a ``^`` caret constraint.
        core_version: Override the running core version (used in tests). When
            None or the unknown sentinel, the check is skipped (returns True)
            so editable checkouts are not blocked.

    See module docstring for the supported syntax.
    """
    if not constraint:
        return True

    core = core_version if core_version is not None else get_core_version()
    if core == _UNKNOWN:
        # Cannot determine the core version — do not block loading.
        return True

    constraint = constraint.strip()
    if constraint.startswith("^"):
        required = _parse_version(constraint[1:])
        actual = _parse_version(core)
        if required[0] == 0:
            # 0.x: caret pins the minor.
            return actual[0] == 0 and actual[1] == required[1] and actual >= required
        # 1.x+: caret pins the major.
        return actual[0] == required[0] and actual >= required

    # Bare version = exact match.
    return _parse_version(constraint) == _parse_version(core)
