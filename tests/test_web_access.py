"""B-091 / DC-012: web access toggle helpers (cache-safe tail + contextvar)."""

from __future__ import annotations

from corpclaw_lite.agent.web_access import (
    WEB_ACCESS_MARKER,
    get_web_access,
    inject_web_access_hint,
    reset_web_access,
    set_web_access,
)


def test_inject_off_appends_marker_tail() -> None:
    messages = [
        {"role": "system", "content": "SOUL base"},
        {"role": "user", "content": "hello"},
    ]
    out = inject_web_access_hint(messages, enabled=False)
    assert out[0]["content"] == "SOUL base"
    assert out[-1]["role"] == "user"
    assert WEB_ACCESS_MARKER in str(out[-1]["content"])
    assert "OFF" in str(out[-1]["content"])
    assert "web_fetch" in str(out[-1]["content"])


def test_inject_on_strips_marker_only() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "user", "content": f"{WEB_ACCESS_MARKER} OFF\nold"},
    ]
    out = inject_web_access_hint(messages, enabled=True)
    assert len(out) == 1
    assert out[0]["content"] == "hi"
    assert all(WEB_ACCESS_MARKER not in str(m.get("content", "")) for m in out)


def test_inject_replaces_previous_off_hint() -> None:
    messages = [
        {"role": "user", "content": "q"},
        {"role": "user", "content": f"{WEB_ACCESS_MARKER} OFF\nfirst"},
    ]
    out = inject_web_access_hint(messages, enabled=False)
    assert sum(1 for m in out if WEB_ACCESS_MARKER in str(m.get("content", ""))) == 1


def test_contextvar_default_true() -> None:
    assert get_web_access() is True
    token = set_web_access(False)
    try:
        assert get_web_access() is False
    finally:
        reset_web_access(token)
    assert get_web_access() is True
