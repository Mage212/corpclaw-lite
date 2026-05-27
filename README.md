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

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (optional, for sandbox mode)
- A running LLM (Ollama, vLLM, or LM Studio)

### Installation

```bash
git clone https://github.com/Mage212/corpclaw-lite.git
cd corpclaw-lite
uv sync
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN, CORPCLAW_IPC_SECRET, OPENAI_BASE_URL
```

### Running

```bash
uv run corpclaw-lite chat       # Interactive CLI (dev mode)
uv run corpclaw-lite telegram   # Start Telegram bot
uv run corpclaw-lite web        # Start browser UI
```

## Features

| Feature | Description |
|---------|-------------|
| ReAct Agent Loop | Reasoning+acting with budget guards and loop detection |
| LLM Router | Route tasks to specific providers (local/cloud) |
| Model Presets | Per-model inference params and reasoning strategies |
| XML Tool Calling | Fallback parser for local LLMs without function calling |
| 18 Built-in Tools | File ops, SQL queries, charts, PDF, Excel, web fetch, and more |
| Docker Sandbox | Per-user containers with resource limits and network deny-by-default |
| ToolGuard | 20+ YAML security rules with LLM-based Smart Approvals |
| 4 Skills / Plugins / 5 Subagents | Markdown skills with scope filtering, subprocess plugins, isolated subagents |
| TF-IDF Matching | Bilingual (RU+EN) semantic skill selection |
| Web + Telegram Channels | Browser UI, Telegram bot, file manager, progress, approvals, rate limiting |
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
