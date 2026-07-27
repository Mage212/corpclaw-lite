from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

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
_MIN_SECRET_LENGTH = 32  # S2-20: aligned with .env.example (was 16)


class _NonceStore(Protocol):
    """Abstraction over the seen-nonce tracking used by :class:`IPCAuth.verify`.

    Two implementations: an in-memory dict (default; host-side long-lived
    instance) and a sqlite-backed file (container worker; survives across the
    short-lived ``docker exec`` processes that share one running container —
    see H-1 in the code review).
    """

    def cleanup_expired(self, ttl_seconds: int) -> None: ...
    def at_capacity(self) -> bool: ...
    def seen(self, nonce: str) -> bool: ...
    def mark(self, nonce: str, timestamp: float) -> None: ...


class _InMemoryNonceStore:
    """Default nonce store: a plain dict on the IPCAuth instance.

    Backward-compatible with the pre-H-1 behaviour — sufficient for the
    long-lived host-side IPCAuth (one process, replay protection within its
    lifetime). NOT sufficient for the container worker, which is recreated per
    ``docker exec`` and would start each call with an empty dict.
    """

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def cleanup_expired(self, ttl_seconds: int) -> None:
        now = time.time()
        expired = [n for n, t in self._seen.items() if now - t > ttl_seconds]
        for n in expired:
            del self._seen[n]

    def at_capacity(self) -> bool:
        return len(self._seen) >= MAX_NONCES

    def seen(self, nonce: str) -> bool:
        return nonce in self._seen

    def mark(self, nonce: str, timestamp: float) -> None:
        self._seen[nonce] = timestamp


class _PersistentNonceStore:
    """SQLite-backed nonce store sharing replay state across short-lived processes.

    The container worker is launched fresh for every ``docker exec`` (the
    stateless IPC design in ``container/ipc.py``), so an in-memory dict would
    start empty on each call and never detect a replayed request. This store
    persists seen-nonces to a small sqlite file inside the container's writable
    tmpfs (``/tmp/corpclaw_nonces.db`` by default), which survives across the
    worker processes that share one running container.

    Single-writer assumption holds: tool dispatch into one user's container is
    serialised by ``ContainerIPC`` (one ``docker exec`` at a time per
    container), so no write concurrency. ``busy_timeout`` is set defensively
    anyway.
    """

    def __init__(self, path: Path, ttl_seconds: int) -> None:
        self._path = path
        self._conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        # Raise a clear error instead of waiting on a locked DB; the
        # serialised-dispatch contract means a lock indicates a real problem.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_nonces (nonce TEXT PRIMARY KEY, ts REAL NOT NULL)"
        )
        # Run one cleanup on open so a restarted container does not inherit a
        # full store from a prior incarnation (the tmpfs is wiped on container
        # recreate, but cleanup is cheap and keeps the size bounded mid-life).
        self.cleanup_expired(ttl_seconds)

    def cleanup_expired(self, ttl_seconds: int) -> None:
        cutoff = time.time() - ttl_seconds
        self._conn.execute("DELETE FROM seen_nonces WHERE ts < ?", (cutoff,))

    def at_capacity(self) -> bool:
        cur = self._conn.execute("SELECT COUNT(*) FROM seen_nonces")
        return int(cur.fetchone()[0]) >= MAX_NONCES

    def seen(self, nonce: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM seen_nonces WHERE nonce = ?", (nonce,))
        return cur.fetchone() is not None

    def mark(self, nonce: str, timestamp: float) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_nonces (nonce, ts) VALUES (?, ?)",
            (nonce, timestamp),
        )


class IPCAuth:
    """Provides HMAC-SHA256 authentication with nonce to prevent replay attacks.

    Two nonce-store backends:

    * default (``nonce_store_path=None``) — in-memory dict; fine for a long-lived
      process (the host-side ``IPCAuth`` constructed once in ``agent/factory.py``).
    * ``nonce_store_path=<file>`` — sqlite-backed; required for the container
      worker (``container/agent_worker.py``), where each ``docker exec`` is a
      fresh process and an in-memory store would never detect a replayed
      request (H-1, code review). On any error opening the file the auth
      degrades to in-memory and logs a WARNING — verification still works, just
      without cross-process replay protection.
    """

    def __init__(
        self,
        secret: str | None = None,
        nonce_ttl_seconds: int = 300,
        *,
        nonce_store_path: Path | None = None,
    ) -> None:
        _raw = secret or os.environ.get("CORPCLAW_IPC_SECRET")
        if not _raw:
            raise IPCAuthError("CORPCLAW_IPC_SECRET is required to secure IPC channels")
        if len(_raw) < _MIN_SECRET_LENGTH:
            raise IPCAuthError(
                f"CORPCLAW_IPC_SECRET must be at least {_MIN_SECRET_LENGTH} characters "
                f'(got {len(_raw)}). Generate one with: python -c "import secrets; '
                f'print(secrets.token_hex(32))"'
            )
        self._secret: str | bytes = _raw

        self.nonce_ttl = nonce_ttl_seconds
        self._nonce_store: _NonceStore
        if nonce_store_path is not None:
            try:
                self._nonce_store = _PersistentNonceStore(nonce_store_path, nonce_ttl_seconds)
            except Exception as e:  # noqa: BLE001 — degrade, do not crash verification
                logger.warning(
                    "IPCAuth: could not open persistent nonce store at %s "
                    "(%s); falling back to in-memory store. Cross-process "
                    "replay protection is disabled until the path is writable.",
                    nonce_store_path,
                    e,
                )
                self._nonce_store = _InMemoryNonceStore()
        else:
            self._nonce_store = _InMemoryNonceStore()

    def secret_for_stdin(self) -> str:
        """Return the secret as a string to feed the worker over stdin.

        The worker reads the secret as the first stdin line (followed by the
        signed JSON payload). Keeping it on stdin — not in the docker exec argv
        — prevents local observers from harvesting it via ``ps`` / ``/proc``.
        """
        if isinstance(self._secret, bytes):
            return self._secret.decode("utf-8")
        return self._secret

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

        self._nonce_store.cleanup_expired(self.nonce_ttl)
        if self._nonce_store.at_capacity():
            raise IPCAuthError("Nonce store capacity exceeded — possible abuse")
        if self._nonce_store.seen(nonce):
            raise IPCAuthError("Replay attack detected (nonce already seen)")

        payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        msg = f"{nonce}:{timestamp}:{payload_str}"

        secret_bytes = self._secret if isinstance(self._secret, bytes) else self._secret.encode()
        expected_sig = hmac.new(secret_bytes, msg.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, expected_sig):
            raise IPCAuthError("Invalid signature")

        self._nonce_store.mark(nonce, now)
        return payload
