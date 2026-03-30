from typing import Any, cast

from corpclaw_lite.config.settings import ContainerSettings
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
        workspace_dir: str = "/tmp",
        seccomp_profile_path: str = "docker/seccomp_default.json",
    ) -> dict[str, Any]:
        """Generate kwargs for docker.containers.run()"""

        args: dict[str, Any] = {
            "image": "corpclaw-agent-base:latest",
            "name": f"corpclaw_agent_{user_id}",
            "detach": True,
            "stdin_open": True,  # Keep stdin open for IPC
            "tty": False,
            "mem_limit": settings.max_memory,
            "nano_cpus": int(settings.cpus * 1e9),
            "pids_limit": 100,  # Prevent fork bombs
            "security_opt": [f"seccomp={seccomp_profile_path}"],
            "cap_drop": ["ALL"],  # Drop all capabilities
            "volumes": {
                workspace_dir: {"bind": "/workspace", "mode": "rw"},
                # Future: Mount tools or read-only configs here
            },
            "working_dir": "/workspace",
            "environment": {"CORPCLAW_USER_ID": str(user_id)},
        }

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
