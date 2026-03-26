import logging
from pathlib import Path
from typing import Any, cast

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
                data = cast(dict[str, Any], yaml.safe_load(f) or {})

            self.allowlist = cast(list[str], data.get("allowlist", []))
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

        # NemoClaw pattern: deny-by-default via network_mode="none".
        # True allowlist-based networking requires Docker custom network + iptables.
        logger.warning(
            "NetworkPolicy: using network_mode='none' (deny-by-default). "
            "Allowlist-based networking requires Docker custom network + iptables setup."
        )
        return {
            "network_mode": "none",
            "environment": [f"ALLOWED_DOMAINS={','.join(self.allowlist)}"],
        }
