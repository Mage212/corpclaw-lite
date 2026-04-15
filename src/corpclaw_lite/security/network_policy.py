from __future__ import annotations

__all__ = [
    "NetworkPolicy",
]


class NetworkPolicy:
    """Deny-all network policy for containers.

    Containers run with ``network_mode: "none"`` — zero outbound access.
    Tools that need network connectivity (e.g. ``web_fetch``) execute on
    the host, not inside the container.
    """

    def to_docker_args(self) -> dict[str, str]:
        """Return Docker kwargs for deny-all networking."""
        return {"network_mode": "none"}
