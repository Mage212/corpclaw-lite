from typing import Any

from corpclaw_lite.config.settings import ContainerSettings
from corpclaw_lite.security.network_policy import NetworkPolicy


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
            net_args = network_policy.to_docker_args()
            args.update(net_args)
            if "environment" in net_args and "environment" in args:  # noqa: SIM102
                # Merge environments (handling list vs dict safely depending on how it's passed)
                if isinstance(net_args["environment"], list):
                    for env_var in net_args["environment"]:
                        k, v = env_var.split("=", 1)
                        args["environment"][k] = v

        return args
