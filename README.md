# CorpClaw Lite

A corporate AI agent for on-premise environments — a Telegram bot that executes routine tasks through skills, plugins, and subagents, powered by **local LLMs** (Qwen, Mistral, Llama via Ollama/vLLM/LM Studio) with department-based access control.

**[Full documentation in Russian (Полная документация)](README_RU.md)**

## Architecture

```
User → Channel (Web / Telegram / CLI) → AgentLoop (ReAct)
                                      │
                                      ▼
                              LLM Router ──→ Ollama / vLLM / Anthropic
                                      │
                               tool_calls? ──→ Security Stack
                                      │              │
                                      │    ┌─────────┴─────────┐
                                      │    ▼                   ▼
                                      │  ToolGuard          Permission
                                      │  (YAML rules)       Check (RBAC)
                                      │    │                   │
                                      │    ▼                   ▼
                                      │  Container          Credential
                                      │  (Docker sandbox)   Scrubber
                                      │
                                      ▼
                              Response → Memory (SQLite) → Channel
```

## Extending

CorpClaw Lite is extended through manifests — skills (Markdown), plugins (`manifest.yaml`),
subagents (YAML), and channels — without touching core code. Add a file to the right
directory and it is picked up (with hot-reload).

**Private extensions:** customizations that must not enter this public repo (internal tools,
corporate skills, RBAC rules, system prompts) go into an **overlay** — a private directory
whose layout mirrors the project. Configure it via `config/settings.yaml → extensions.extra_paths`;
the core loads defaults plus overlays, with overlay entries overriding or extending defaults.

```yaml
extensions:
  extra_paths:
    - "${CORPCLAW_PRIVATE_EXTENSIONS}"
```

See [CONTRIBUTING.md](CONTRIBUTING.md#private-extensions-overlay) for the overlay layout,
override/merge semantics, and how to keep core changes public while keeping customizations private.

The overlay contract is verified end-to-end by
[`tests/test_overlay_e2e.py`](tests/test_overlay_e2e.py): it activates a sibling
`corpclaw-corp` overlay and asserts that every extension kind loads and is
usable through it, that overlay entries override defaults by id/name (and that
departments union-merge rather than replace), that no private files leak into
this public repository, and that no traces are left behind. The tests
**skip automatically** when the `corpclaw-corp` sibling is absent, so CI without
the private overlay still passes. Run them locally (with `corpclaw-corp` checked
out next to this repo):

```bash
uv run pytest tests/test_overlay_e2e.py -v
```

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Node.js 20+ and npm (for building the browser UI)
- Docker (optional, for sandbox mode)
- A running LLM (Ollama, vLLM, or LM Studio)

### Installation

```bash
git clone https://github.com/Mage212/corpclaw-lite.git
cd corpclaw-lite
uv sync
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN, CORPCLAW_IPC_SECRET,
# and at least one PROVIDER_<NAME>__* LLM provider.
```

### Running

CLI chat uses an existing CorpClaw user, identified by Telegram ID:

```bash
uv run corpclaw-lite chat --telegram-id <telegram_id>
```

Telegram bot:

```bash
uv run corpclaw-lite telegram
```

Browser UI, production-like local mode:

```bash
uv run corpclaw-lite web-user-link -t <telegram_id> -u <username> -p '<password>'

cd frontend/web
npm ci
npm run build
cd ../..

uv run corpclaw-lite web
```

Open `http://127.0.0.1:8090`.

Use `web-user-link` for users who already work through Telegram. It attaches a browser login to
the same internal `users.id`, so the web UI, Telegram bot, memory, workspace, and per-user
container all point at the same human profile.

For frontend development, run the backend and Vite dev server separately:

```bash
uv run corpclaw-lite web

cd frontend/web
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`; Vite proxies `/api` and `/ws` to the backend on port `8090`.

If `container.enabled=true`, Docker must be running and `CORPCLAW_IPC_SECRET` must be set. If the
React build is missing, the backend returns an explicit warning page instead of a blank UI.

## Features

| Feature | Description |
|---------|-------------|
| ReAct Agent Loop | Reasoning+acting with budget guards and loop detection |
| LLM Router | Route tasks to specific providers (local/cloud) |
| Model + Sampling Profiles | Orthogonal `ModelProfile` (model properties) + `SamplingProfile` (task/phase properties) with per-call `RequestOptions` override (D-056) |
| PhasePolicy | Per-phase thinking control — research gathering off, aggregation on; closing mode off |
| Workflow-finalize Guard | Bounded nudge → restrict → auto-finalize cascade — research subagents always produce a report, never lose accumulated work on budget exhaustion |
| Raw LLM Capture | Optional raw request/response logging to `logs/llm_payloads.jsonl` with field-level allowlist + credential scrubbing — for debugging and future fine-tune dataset collection |
| XML Tool Calling | Fallback parser for local LLMs without function calling |
| 29 Built-in Tools | File ops, SQL queries, charts, PDF, Excel workbook/inspection, web search/fetch, research workflows, and more |
| Docker Sandbox | Per-user containers with resource limits and network deny-by-default |
| ToolGuard | 31 YAML security rules with LLM-based Smart Approvals |
| 5 Skills + 5 Subagents | Markdown skills with scope filtering and isolated subagents; plugins are a framework (no plugins shipped) |
| Private Extensions Overlay | Keep corporate customizations in a separate private repo, composed at runtime — no private files in this public repo ([docs](CONTRIBUTING.md#private-extensions-overlay)) |
| TF-IDF Matching | Bilingual (RU+EN) semantic skill selection |
| Web + Telegram Channels | Browser chat (Mistral.ai-style redesign: multi-chat history, Fast/Think/Research depth modes, extensions manager, agent context), collapsible file manager, single statusline, approvals, rate limiting |
| Per-chat LLM-context Persistence | Full LLM-facing context (tool_calls + reasoning) stored per web chat → restore on chat switch, compress-any-chat, and capture correlation for dataset collection (B-063) |
| Workspace Isolation | Unified per-human workspace across linked Telegram and web logins |
| Auto-Calibration | Adapt prompts for specific local models |
| RBAC | 10 departments with per-department permissions |
| Closed-Loop Ready | Local LLMs, no internet required, all data stored locally |

## Documentation

- **[README_RU.md](README_RU.md)** — full documentation (Russian)
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — architecture reference (Russian)
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how to contribute

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
