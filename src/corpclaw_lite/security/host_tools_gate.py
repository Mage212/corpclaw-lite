"""Host-tools / container.enabled gate (DC-016 / D-070 / B-097).

Prevents accidental production deploys with ``container.enabled=false`` (tools
run on the host without Docker isolation).

Two levels:

1. **Env gate (always):** ``container.enabled=false`` requires
   ``CORPCLAW_ALLOW_HOST_TOOLS=1`` (or ``true`` / ``yes``).
2. **Multi-user surface gate:** telegram/web additionally require
   ``CORPCLAW_ENFORCE_PROD_CONTAINER=false`` to proceed without containers.
   Default is enforced (refuse).

Level 1 is applied inside ``build_agent_stack`` for every caller. Level 2 is
applied when ``surface="multiuser"`` (channel entry points).
"""

from __future__ import annotations

import os
from typing import Literal

from corpclaw_lite.exceptions import StartupConfigurationError

__all__ = [
    "HostToolsSurface",
    "assert_host_tools_allowed",
    "host_tools_allowed_by_env",
    "prod_container_enforced",
]

HostToolsSurface = Literal["dev", "multiuser"]

_ENV_ALLOW = "CORPCLAW_ALLOW_HOST_TOOLS"
_ENV_ENFORCE = "CORPCLAW_ENFORCE_PROD_CONTAINER"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def _env_truthy(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY


def _env_falsy(name: str) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in _FALSY


def host_tools_allowed_by_env() -> bool:
    """Return True when CORPCLAW_ALLOW_HOST_TOOLS opts in to host-side tools."""
    return _env_truthy(_ENV_ALLOW)


def prod_container_enforced() -> bool:
    """Return True when multi-user surfaces must refuse container.enabled=false.

    Default is enforced. Escape hatch: CORPCLAW_ENFORCE_PROD_CONTAINER=false.
    Unset or any non-falsy value → enforce.
    """
    return not _env_falsy(_ENV_ENFORCE)


def assert_host_tools_allowed(
    *,
    container_enabled: bool,
    surface: HostToolsSurface = "dev",
) -> None:
    """Raise StartupConfigurationError when host tools are not explicitly allowed.

    No-op when ``container_enabled`` is True (isolation path).
    """
    if container_enabled:
        return

    if not host_tools_allowed_by_env():
        raise StartupConfigurationError(
            "Container isolation is disabled (container.enabled=false), "
            "but host tools are not explicitly allowed.",
            hint=(
                f"Set {_ENV_ALLOW}=1 for local development without Docker, "
                "or set container.enabled=true and start Docker."
            ),
        )

    if surface == "multiuser" and prod_container_enforced():
        raise StartupConfigurationError(
            "Container isolation is disabled on a multi-user surface "
            "(telegram/web) while production container enforcement is active.",
            hint=(
                "Set container.enabled=true and start Docker for multi-user "
                f"deployments, or set {_ENV_ENFORCE}=false to override "
                f"(requires {_ENV_ALLOW}=1)."
            ),
        )
