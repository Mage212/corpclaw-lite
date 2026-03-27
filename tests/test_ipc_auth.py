import time

import pytest

from corpclaw_lite.security.ipc_auth import IPCAuth, IPCAuthError


def test_ipc_auth_verify_success():
    auth = IPCAuth(secret="test_secret", nonce_ttl_seconds=10)
    payload = {"command": "do_something", "args": {"x": 1}}

    signed = auth.sign(payload)

    assert "signature" in signed
    assert "nonce" in signed
    assert "timestamp" in signed

    verified_payload = auth.verify(signed)
    assert verified_payload == payload


def test_ipc_auth_detects_tampering():
    auth = IPCAuth(secret="test_secret", nonce_ttl_seconds=10)
    payload = {"command": "do_something"}
    signed = auth.sign(payload)

    # Tamper payload
    signed["payload"] = {"command": "do_evil_things"}

    with pytest.raises(IPCAuthError, match="Invalid signature"):
        auth.verify(signed)


def test_ipc_auth_detects_replay():
    auth = IPCAuth(secret="test_secret", nonce_ttl_seconds=10)
    payload = {"command": "test"}
    signed = auth.sign(payload)

    auth.verify(signed)  # first time okay

    with pytest.raises(IPCAuthError, match="Replay attack detected"):
        auth.verify(signed)  # second time fails


def test_ipc_auth_ttl_expiration():
    auth = IPCAuth(secret="test_secret", nonce_ttl_seconds=0)  # Expires immediately
    payload = {"command": "test"}
    signed = auth.sign(payload)

    # ensure it's "old"
    time.sleep(0.01)

    with pytest.raises(IPCAuthError, match="expired"):
        auth.verify(signed)


def test_ipc_auth_rejects_future_timestamp() -> None:
    """A message with a timestamp far in the future must be rejected."""
    auth = IPCAuth(secret="test_secret", nonce_ttl_seconds=10)
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
    signed["signature"] = hmac.new(b"test_secret", msg.encode(), hashlib.sha256).hexdigest()

    with pytest.raises(IPCAuthError, match="future|range|expired"):
        auth.verify(signed)


def test_ipc_auth_missing_secret_raises_ipc_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing CORPCLAW_IPC_SECRET must raise IPCAuthError, not ValueError."""
    monkeypatch.delenv("CORPCLAW_IPC_SECRET", raising=False)
    with pytest.raises(IPCAuthError):
        IPCAuth(secret=None)
