from __future__ import annotations

import os
from typing import Any

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.security.network_policy import NetworkPolicy

__all__ = [
    "build_docker_args",
]


def build_docker_args(
    user_id: int,
    settings: ContainerSettings,
    network_policy: NetworkPolicy | None = None,
    workspace_dir: str = "workspaces",
    seccomp_profile_path: str = "docker/seccomp_default.json",
) -> dict[str, Any]:
    """Generate kwargs for docker.containers.run()

    Args:
        user_id: Telegram user ID — used to name the container.
        settings: ContainerSettings with image, limits, etc.
        network_policy: Optional network deny-all policy to apply.
        workspace_dir: Absolute host path to bind-mount at /workspace.
    """
    args: dict[str, Any] = {
        "image": settings.image,
        "name": f"corpclaw_agent_{user_id}",
        "detach": True,
        "stdin_open": True,
        "tty": False,
        "mem_limit": settings.max_memory,
        "nano_cpus": int(settings.cpus * 1e9),
        "pids_limit": 100,
        "security_opt": ["no-new-privileges:true"],
        "read_only": True,
        "tmpfs": {"/tmp": "size=64m"},
        "volumes": {
            workspace_dir: {"bind": "/workspace", "mode": "rw"},
        },
        "working_dir": "/workspace",
        "environment": {
            "CORPCLAW_USER_ID": str(user_id),
            "PYTHONUNBUFFERED": "1",
        },
    }

    # Hardening is ON by default (strict_capabilities defaults True). It drops ALL
    # Linux capabilities, applies a deny-by-default seccomp allow-list, and pins an
    # explicit non-root user. The corpclaw-agent-base image already declares
    # ``USER agent`` (UID 1001) with ``/workspace`` chowned inside it, so the
    # explicit ``user`` kwarg is defense-in-depth — it makes the non-root contract
    # independent of image metadata. Setting ``strict_capabilities = False`` is an
    # opt-out for dev/debug: cap_drop/seccomp/explicit-user are skipped, but the
    # image's own ``USER agent`` still applies, so the container never runs as root.
    if settings.strict_capabilities:
        args["user"] = "agent"
        args["cap_drop"] = ["ALL"]
        seccomp_path = PROJECT_ROOT / seccomp_profile_path
        if seccomp_path.exists():
            args["security_opt"].append(f"seccomp={seccomp_path}")

    ipc_secret = os.environ.get("CORPCLAW_IPC_SECRET")
    if ipc_secret:
        args["environment"]["CORPCLAW_IPC_SECRET"] = ipc_secret

    if network_policy:
        net_args: dict[str, Any] = dict(network_policy.to_docker_args())
        args.update(net_args)

    return args
