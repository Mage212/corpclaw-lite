import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


class NetworkPolicy:
    """Defines and applies network allowlist policies for containers."""

    def __init__(self) -> None:
        self.allowlist: list[str] = []

    def load_file(self, path: Path | str) -> None:
        file_path = Path(path)
        if not file_path.exists():
            logger.warning("NetworkPolicy file not found: %s", file_path)
            return

        try:
            with open(file_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            self.allowlist = data.get("allowlist", [])
            logger.info("Loaded NetworkPolicy with %d allowed domains", len(self.allowlist))
        except Exception as e:
            logger.error("Failed to load NetworkPolicy from %s: %s", file_path, e)

    def to_docker_args(self) -> dict[str, str | list[str]]:
        """
        Translates the policy into Docker container keyword arguments.
        In Docker, to enact a true zero-trust deny-by-default with specific allowlist
        for outbound traffic requires custom network configurations or iptables.
        
        For CorpClaw Lite, connecting to a restricted predefined network is the pattern.
        """
        # A simple approach is passing environment variables or using specific add-host entries
        # but true lockdown is achieved via container orchestration networks.
        # This returns a simplified representation for ContainerManager.
        
        # NemoClaw pattern: "none" network + specific proxies, or restricted bridge
        return {
            "network_mode": "bridge",
            # We would add iptables rules here via startup script or specific DNS mappings
            # For phase 2 mock, we pass it as an environment variable to the container wrapper.
            "environment": [f"ALLOWED_DOMAINS={','.join(self.allowlist)}"]
        }
