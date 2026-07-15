# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability in CorpClaw Lite, please report it responsibly:

1. **Do not** open a public GitHub issue.
2. Send a detailed report to the maintainer via [GitHub Security Advisories](https://github.com/Mage212/corpclaw-lite/security/advisories/new).
3. Include: steps to reproduce, affected components, potential impact.

We aim to acknowledge reports within 48 hours and provide a fix within 7 days for critical issues.

## Security Features

CorpClaw Lite includes multiple security layers:

- **ToolGuard** — 20+ YAML security rules with severity levels (CRITICAL/HIGH/MEDIUM/INFO) evaluated before every tool execution.
- **Smart Approvals** — LLM-based risk assessment for dangerous operations (APPROVE / DENY / ESCALATE).
- **Docker Sandbox** — Per-user containers with resource limits (CPU, memory, PIDs), capability drops, and seccomp profiles.
- **Network Policy** — Containers always run with `network_mode: none` (deny-all egress). Host-side tools (`web_fetch`) apply SSRF checks separately.
- **IPC Authentication** — HMAC-SHA256 signed payloads with nonce-based replay protection (300s TTL). `CORPCLAW_IPC_SECRET` is injected only on each `docker exec` of the agent worker, not into the long-lived container create environment.
- **Credential Scrubber** — Automatic masking of API keys and tokens in logs and output.
- **RBAC** — Department-based access control with per-department tool permissions. Resource limits (iterations, tool calls, wall-time) are global in settings.yaml.
- **Host tools gate** — Multi-user surfaces (Telegram/Web) refuse `container.enabled=false` unless explicitly overridden for development (`CORPCLAW_ALLOW_HOST_TOOLS=1` **and** `CORPCLAW_ENFORCE_PROD_CONTAINER=false`). Production multi-user deploys must keep containers enabled.
