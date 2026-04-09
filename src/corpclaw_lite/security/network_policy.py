from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import yaml

__all__ = [
    "NetworkPolicy",
]

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
        # KNOWN LIMITATION: network_mode='none' blocks ALL outbound traffic including
        # the allowlist. The ALLOWED_DOMAINS env var is set but not consumed by any
        # component — it is informational only.
        #
        # To implement true allowlist-based networking:
        #   1. Create a custom Docker network:
        #         docker network create --driver bridge corpclaw_agent
        #   2. Remove network_mode='none' and connect container to corpclaw_agent
        #   3. In the container entrypoint, apply iptables rules:
        #         iptables -P OUTPUT DROP
        #         for domain in $ALLOWED_DOMAINS; do
        #             iptables -A OUTPUT -d $(dig +short $domain) -j ACCEPT
        #         done
        #   4. Requires --cap-add NET_ADMIN on the container
        #
        # See docs/network_policy.md for the full setup guide.
        logger.debug(
            "NetworkPolicy: network_mode='none' blocks ALL traffic (deny-by-default). "
            "The ALLOWED_DOMAINS env var is set but not enforced — "
            "allowlist-based networking requires Docker custom network + iptables. "
            "See inline comments in network_policy.py for setup instructions."
        )
        return {
            "network_mode": "none",
            "environment": [f"ALLOWED_DOMAINS={','.join(self.allowlist)}"],
        }
