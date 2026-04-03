from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from typing import Any

__all__ = [
    "IPCAuth",
    "IPCAuthError",
    "MAX_NONCES",
]

logger = logging.getLogger(__name__)


class IPCAuthError(Exception):
    """Raised when IPC authentication fails."""

    pass


MAX_NONCES = 100_000


class IPCAuth:
    """Provides HMAC-SHA256 authentication with nonce to prevent replay attacks."""

    def __init__(self, secret: str | None = None, nonce_ttl_seconds: int = 300) -> None:
        _raw = secret or os.environ.get("CORPCLAW_IPC_SECRET")
        if not _raw:
            raise IPCAuthError("CORPCLAW_IPC_SECRET is required to secure IPC channels")
        self._secret: str | bytes = _raw

        self.nonce_ttl = nonce_ttl_seconds
        self._seen_nonces: dict[str, float] = {}

    def _cleanup_nonces(self) -> None:
        """Remove expired nonces."""
        now = time.time()
        expired = [n for n, t in self._seen_nonces.items() if now - t > self.nonce_ttl]
        for n in expired:
            del self._seen_nonces[n]

    def sign(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Sign a payload dict and return the wrapped message."""
        nonce = str(uuid.uuid4())
        timestamp = time.time()

        # Consistent JSON stringification for hashing
        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))

        msg = f"{nonce}:{timestamp}:{payload_str}"
        secret_bytes = self._secret if isinstance(self._secret, bytes) else self._secret.encode()
        signature = hmac.new(secret_bytes, msg.encode(), hashlib.sha256).hexdigest()

        return {
            "signature": signature,
            "nonce": nonce,
            "timestamp": timestamp,
            "payload": payload,
        }

    def verify(self, message: dict[str, Any]) -> dict[str, Any]:
        """Verify the signature and nonce of a message. Returns the payload."""
        signature = message.get("signature")
        nonce = message.get("nonce")
        timestamp = message.get("timestamp")
        payload = message.get("payload")

        if not signature or not nonce or not timestamp or payload is None:
            raise IPCAuthError("Missing authentication fields in message")

        now = time.time()
        if abs(now - timestamp) > self.nonce_ttl:
            raise IPCAuthError("Message timestamp out of acceptable range (expired or future)")

        self._cleanup_nonces()
        if len(self._seen_nonces) >= MAX_NONCES:
            raise IPCAuthError("Nonce store capacity exceeded — possible abuse")
        if nonce in self._seen_nonces:
            raise IPCAuthError("Replay attack detected (nonce already seen)")

        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        msg = f"{nonce}:{timestamp}:{payload_str}"

        secret_bytes = self._secret if isinstance(self._secret, bytes) else self._secret.encode()
        expected_sig = hmac.new(secret_bytes, msg.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            raise IPCAuthError("Invalid signature")

        self._seen_nonces[nonce] = now
        return payload
