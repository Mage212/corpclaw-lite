"""Environment filtering for untrusted subprocesses (MCP servers, plugin workers).

H-3 (code review): MCP servers and plugin tools launched as subprocesses
previously inherited the *full* parent ``os.environ``, leaking
``OPENAI_API_KEY``, ``CORPCLAW_IPC_SECRET``, etc. to external executables
fetched on demand (``npx``, ``uvx``, ...).

This module builds a minimal, safe environment by:

* inheriting only an allowlist (PATH/HOME/locale/...) so the subprocess can
  still locate its runtime and encode output;
* merging explicitly-declared per-server vars (from ``mcp_servers.yaml``);
* never passing anything on a secret denylist, regardless of source.

Used by :class:`~corpclaw_lite.extensions.mcp.client.MCPClient`.
"""

from __future__ import annotations

import os

__all__ = ["build_subprocess_env"]

# Minimal environment an untrusted subprocess needs to run (find its binaries,
# encode output, resolve a tmp dir). Keep this short on purpose — anything not
# listed here is not inherited.
_ALLOWED_INHERIT_ENV: frozenset[str] = frozenset(
    {
        # Binary resolution.
        "PATH",
        # User/home — many tools (npx cache, npm config) need HOME.
        "HOME",
        "USER",
        "LOGNAME",
        # Locale / encoding — keeps UTF-8 semantics consistent with the host.
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        # Temp dirs (Python tempfile, Node os.tmpdir, ...).
        "TMPDIR",
        "TMP",
        "TEMP",
        # Terminal metadata; harmless and occasionally read by tooling.
        "TERM",
        # Windows-only; ignored on POSIX but present for cross-platform safety.
        "SYSTEMROOT",
        "APPDATA",
        "LOCALAPPDATA",
    }
)

# Secrets that must NEVER reach an untrusted subprocess, even if they appear in
# the allowlist or are explicitly declared in mcp_servers.yaml. Match is
# case-sensitive on the canonical upper-case names; well-known deploy secrets
# are listed plus a suffix-wildcard family for CORPCLAW_/OPENAI_/ANTHROPIC_.
_SECRET_DENYLIST: frozenset[str] = frozenset(
    {
        "CORPCLAW_IPC_SECRET",
        "OPENAI_API_KEY",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT_ID",
        "ANTHROPIC_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
        "CI_JOB_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "STRIPE_API_KEY",
        "SLACK_TOKEN",
        "SLACK_BOT_TOKEN",
    }
)

# Prefixes for secret-like env vars that are not enumerated above (e.g. a
# future CORPCLAW_PRIVATE_KEY or a vendor-prefixed token). Any inherited or
# user-declared var starting with one of these is dropped.
_SECRET_PREFIXES: tuple[str, ...] = (
    "CORPCLAW_",
    "OPENAI_",
    "ANTHROPIC_",
    "_",  # convention for private/undocumented vars
)


def _is_secret(name: str) -> bool:
    upper = name.upper()
    if upper in _SECRET_DENYLIST:
        return True
    return any(upper.startswith(prefix) for prefix in _SECRET_PREFIXES)


def build_subprocess_env(user_env: dict[str, str] | None) -> dict[str, str]:
    """Build a minimal, secret-free environment for an untrusted subprocess.

    Inherits only the allowlist vars from ``os.environ`` (PATH/HOME/locale/...),
    then layers explicitly-declared per-server vars on top. Anything matching
    the secret denylist (by name or CORPCLAW_/OPENAI_/ANTHROPIC_ prefix) is
    dropped regardless of source.

    Always returns a non-empty dict (PATH/HOME survive unless absent from the
    parent env), so callers should pass it as ``env=`` to the subprocess —
    never ``None``, which would re-introduce full inheritance.
    """
    env: dict[str, str] = {}
    for name in _ALLOWED_INHERIT_ENV:
        if _is_secret(name):
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    if user_env:
        for name, value in user_env.items():
            if _is_secret(name):
                continue
            env[name] = value
    return env
