import time

import pytest

from corpclaw_lite.security.ipc_auth import IPCAuth, IPCAuthError

# Must meet the _MIN_SECRET_LENGTH=32 requirement (S2-20)
_TEST_SECRET = "test_secret_long_enough_for_tests_32chars"


def test_ipc_auth_verify_success():
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=10)
    payload = {"command": "do_something", "args": {"x": 1}}

    signed = auth.sign(payload)

    assert "signature" in signed
    assert "nonce" in signed
    assert "timestamp" in signed

    verified_payload = auth.verify(signed)
    assert verified_payload == payload


def test_ipc_auth_detects_tampering():
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=10)
    payload = {"command": "do_something"}
    signed = auth.sign(payload)

    # Tamper payload
    signed["payload"] = {"command": "do_evil_things"}

    with pytest.raises(IPCAuthError, match="Invalid signature"):
        auth.verify(signed)


def test_ipc_auth_detects_replay():
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=10)
    payload = {"command": "test"}
    signed = auth.sign(payload)

    auth.verify(signed)  # first time okay

    with pytest.raises(IPCAuthError, match="Replay attack detected"):
        auth.verify(signed)  # second time fails


def test_ipc_auth_ttl_expiration():
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=0)  # Expires immediately
    payload = {"command": "test"}
    signed = auth.sign(payload)

    # ensure it's "old"
    time.sleep(0.01)

    with pytest.raises(IPCAuthError, match="expired"):
        auth.verify(signed)


def test_ipc_auth_rejects_future_timestamp() -> None:
    """A message with a timestamp far in the future must be rejected."""
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=10)
    payload = {"command": "test"}
    signed = auth.sign(payload)

    # Tamper the timestamp to be 1 hour in the future
    signed["timestamp"] = time.time() + 3600

    # Re-sign with the tampered timestamp to make the signature valid
    # (but the timestamp check should still reject it)
    import hashlib
    import hmac
    import json

    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    msg = f"{signed['nonce']}:{signed['timestamp']}:{payload_str}"
    signed["signature"] = hmac.new(_TEST_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

    with pytest.raises(IPCAuthError, match="future|range|expired"):
        auth.verify(signed)


def test_ipc_auth_rejects_short_secret() -> None:
    """Secrets shorter than _MIN_SECRET_LENGTH must raise IPCAuthError."""
    with pytest.raises(IPCAuthError, match="at least"):
        IPCAuth(secret="tooshort")


def test_ipc_auth_missing_secret_raises_ipc_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing CORPCLAW_IPC_SECRET must raise IPCAuthError, not ValueError."""
    monkeypatch.delenv("CORPCLAW_IPC_SECRET", raising=False)
    with pytest.raises(IPCAuthError):
        IPCAuth(secret=None)


# ── H-1 (code review): persistent nonce store for cross-process replay protection ──


def test_ipc_auth_persistent_nonce_store_detects_replay_across_instances(tmp_path):
    """Two IPCAuth instances on the same nonce-store file share replay state.

    This is the regression test for H-1: the container worker is recreated per
    ``docker exec``, so an in-memory store starts empty on each call. A
    persistent store lets a second worker process detect a nonce already seen
    by the first within the TTL window.
    """
    store_path = tmp_path / "nonces.db"
    payload = {"command": "do_work"}

    # First worker process: signs, verifies, marks the nonce, exits.
    auth1 = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=60, nonce_store_path=store_path)
    signed = auth1.sign(payload)
    auth1.verify(signed)

    # Second worker process — fresh IPCAuth, same nonce-store file + secret.
    auth2 = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=60, nonce_store_path=store_path)
    with pytest.raises(IPCAuthError, match="Replay attack detected"):
        auth2.verify(signed)


def test_ipc_auth_in_memory_nonce_store_is_default(tmp_path):
    """Without nonce_store_path the legacy in-memory behaviour is unchanged.

    A second instance does NOT see the first's nonces — this is the pre-H-1
    behaviour, preserved for the long-lived host-side IPCAuth and for callers
    that have not opted into the persistent store.
    """
    payload = {"command": "do_work"}
    auth1 = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=60)
    signed = auth1.sign(payload)
    auth1.verify(signed)

    auth2 = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=60)
    # No replay error — separate in-memory stores.
    auth2.verify(signed)


def test_ipc_auth_persistent_nonce_store_ttl_cleanup(tmp_path):
    """Expired nonces are cleaned up, so the store does not grow unbounded."""
    import sqlite3

    store_path = tmp_path / "nonces.db"
    ttl = 60
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=ttl, nonce_store_path=store_path)
    payload = {"command": "do_work"}
    signed = auth.sign(payload)
    auth.verify(signed)

    # Manually backdate the stored nonce past the TTL.
    with sqlite3.connect(str(store_path)) as conn:
        conn.execute("UPDATE seen_nonces SET ts = ?", (time.time() - ttl - 1,))

    # A new auth opens the store and runs cleanup on init; the stale entry is gone.
    auth2 = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=ttl, nonce_store_path=store_path)
    with sqlite3.connect(str(store_path)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM seen_nonces").fetchone()[0]
    assert count == 0
    # And the backdated nonce is now acceptable again.
    auth2.verify(signed)


def test_ipc_auth_persistent_nonce_store_fallback_on_unwritable_path(tmp_path):
    """If the nonce-store file cannot be opened, auth degrades to in-memory.

    Verification still works (sign/verify round-trip); only cross-process
    replay protection is lost. The path pointing at a directory-of-a-file
    simulates an unwritable location without depending on filesystem perms.
    """
    unwritable_path = tmp_path / "does_not_exist_dir" / "nonces.db"
    auth = IPCAuth(secret=_TEST_SECRET, nonce_ttl_seconds=60, nonce_store_path=unwritable_path)
    payload = {"command": "do_work"}
    signed = auth.sign(payload)
    # Round-trip succeeds despite the bad path.
    assert auth.verify(signed) == payload
