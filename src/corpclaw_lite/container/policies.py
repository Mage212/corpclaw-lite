from __future__ import annotations

from typing import Any, cast

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.paths import PROJECT_ROOT
from corpclaw_lite.security.network_policy import NetworkPolicy

__all__ = [
    "ContainerPolicies",
]


class ContainerPolicies:
    """Builder for Docker SDK container args applying resource limits and network isolation."""

    @staticmethod
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
            network_policy: Optional network allowlist to apply.
            workspace_dir: Absolute host path to bind-mount at /workspace.
        """
        args: dict[str, Any] = {
            "image": settings.image,
            "name": f"corpclaw_agent_{user_id}",
            "detach": True,
            "stdin_open": True,  # Keep stdin open for docker exec IPC
            "tty": False,
            "mem_limit": settings.max_memory,
            "nano_cpus": int(settings.cpus * 1e9),
            "pids_limit": 100,  # Prevent fork bombs
            "security_opt": ["no-new-privileges:true"],
            "read_only": True,  # Read-only root FS; /workspace and /tmp are rw
            "tmpfs": {"/tmp": "size=64m"},
            "volumes": {
                # User workspace: the ONLY writable persistent path for the user
                workspace_dir: {"bind": "/workspace", "mode": "rw"},
            },
            "working_dir": "/workspace",
            "environment": {
                "CORPCLAW_USER_ID": str(user_id),
                "PYTHONUNBUFFERED": "1",
            },
        }

        # Apply strict Linux isolation if enabled (breaks Docker Desktop for Mac's runc)
        if settings.strict_capabilities:
            args["cap_drop"] = ["ALL"]
            seccomp_path = PROJECT_ROOT / seccomp_profile_path
            if seccomp_path.exists():
                args["security_opt"].append(f"seccomp={seccomp_path}")

        # Pass IPC secret into container so agent_worker can verify requests
        import os

        ipc_secret = os.environ.get("CORPCLAW_IPC_SECRET")
        if ipc_secret:
            args["environment"]["CORPCLAW_IPC_SECRET"] = ipc_secret

        if network_policy:
            net_args: dict[str, Any] = dict(network_policy.to_docker_args())
            # Preserve our environment dict before update() overwrites it
            saved_env: dict[str, str] = dict(args.get("environment", {}))
            net_env = net_args.pop("environment", None)
            args.update(net_args)
            # Merge network policy environment entries into the original dict
            args["environment"] = saved_env
            if isinstance(net_env, list):
                env_list = cast(list[str], net_env)
                for env_var in env_list:
                    k, v = env_var.split("=", 1)
                    args["environment"][k] = v
            elif isinstance(net_env, dict):
                env_dict = cast(dict[str, str], net_env)
                args["environment"].update(env_dict)

        return args
