from __future__ import annotations

from typing import Any

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.security.network_policy import NetworkPolicy

__all__ = [
    "build_docker_args",
    "ContainerPolicyError",
]


class ContainerPolicyError(Exception):
    """Raised when a container security policy cannot be satisfied (fail-fast)."""


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
            # H-1 (code review): persistent nonce-store path for the container
            # worker. Lives on the writable /tmp tmpfs so seen-nonces survive
            # across the short-lived docker-exec processes sharing one
            # container — without this, each worker restarts with an empty
            # nonce store and inbound replay protection is inert. Not a secret.
            "CORPCLAW_IPC_NONCE_STORE": "/tmp/corpclaw_nonces.db",
        },
        # Generation label lets ensure_running detect a container created under a
        # different policy/image and recreate it, so a pre-existing or stale
        # container with the same name cannot be silently reused as the sandbox.
        "labels": {
            "corpclaw.image": settings.image,
            "corpclaw.strict_capabilities": str(settings.strict_capabilities),
            "corpclaw.network": "none" if network_policy is not None else "default",
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
        elif settings.seccomp_missing_fatal:
            # Fail fast instead of silently running with Docker's wider default
            # seccomp profile. The deny-by-default profile is the load-bearing
            # syscall filter; dropping it weakens isolation without any signal.
            raise ContainerPolicyError(
                f"Seccomp profile not found at {seccomp_path} and "
                "container.strict_capabilities + container.seccomp_missing_fatal are both True. "
                "Either place the profile, set seccomp_missing_fatal=false to accept Docker's "
                "default, or set strict_capabilities=false for dev/debug."
            )
        else:
            import logging

            logging.getLogger(__name__).warning(
                "Seccomp profile %s missing; running with Docker default (reduced isolation)",
                seccomp_path,
            )

    # Do NOT inject CORPCLAW_IPC_SECRET into the long-lived container env.
    # Secret is passed on each docker exec via the worker's stdin (security-hardening
    # sprint 3), so idle PID 1 never holds it in /proc/*/environ or argv.

    if network_policy:
        net_args: dict[str, Any] = dict(network_policy.to_docker_args())
        args.update(net_args)

    return args
