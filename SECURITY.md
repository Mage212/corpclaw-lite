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
- **Network Policy** — Deny-by-default network access for containers with explicit allowlist.
- **IPC Authentication** — HMAC-SHA256 signed payloads with nonce-based replay protection (300s TTL).
- **Credential Scrubber** — Automatic masking of API keys and tokens in logs and output.
- **RBAC** — Department-based access control with per-department tool permissions and budgets.
