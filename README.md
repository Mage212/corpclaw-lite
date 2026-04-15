# CorpClaw Lite

A reliable corporate AI agent for closed-loop environments — a Telegram bot that executes routine tasks through skills, plugins, and subagents, runs on **local LLMs** (Qwen, Mistral, Llama via Ollama/vLLM/LM Studio), and manages access by departments.

## Architecture

```
User → Channel (Telegram / CLI) → AgentLoop (ReAct)
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

### Key Design Principles

- **Simple ReAct Loop** — no LLM-based planners, classic ReAct with budget guards and loop detection
- **Local LLM First** — XML tool calling fallback, context compression, model presets for Qwen/Mistral/Llama
- **Security by Design** — ToolGuard (YAML rules), NetworkPolicy (deny-by-default), IPC Auth (HMAC+nonce), Docker sandbox
- **Manifest-based Extensions** — skills, plugins, subagents, MCP servers via YAML configs with hot-reload
- **Fail-Fast** — errors on missing critical secrets, no silent failures

---

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Docker (optional, for sandbox mode)
- A running LLM (Ollama, vLLM, or LM Studio)

### Installation

```bash
# Clone
git clone https://github.com/your-org/corpclaw-lite.git
cd corpclaw-lite

# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env — set TELEGRAM_BOT_TOKEN, CORPCLAW_IPC_SECRET, OPENAI_BASE_URL
```

### Running

```bash
# Interactive CLI chat (dev mode, no Docker required)
uv run corpclaw-lite chat

# Start Telegram bot
uv run corpclaw-lite telegram

# Build Docker sandbox image (required for production)
cd docker && docker build -t corpclaw-agent-base:latest -f Dockerfile .
```

For dev mode without Docker, set in `config/settings.yaml`:
```yaml
container:
  enabled: false
```

> **Warning:** In dev mode (`enabled: false`), file tools execute directly on the host. Do not use in production.

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from @BotFather |
| `CORPCLAW_IPC_SECRET` | Yes | Random string ≥32 chars (HMAC key for container IPC) |
| `OPENAI_BASE_URL` | Yes | Base provider URL (e.g. `http://localhost:11434/v1` for Ollama) |
| `ANTHROPIC_API_KEY` | No | For Claude fallback/routing |
| `OPENAI_API_KEY` | No | If base provider requires auth (e.g. OpenRouter) |

---

## Features

| Feature | Description |
|---------|-------------|
| **ReAct Agent Loop** | Classic reasoning+acting cycle with budget guards and loop detection |
| **LLM Router** | Route tasks to specific providers (local Ollama, cloud Anthropic, etc.) |
| **Model Presets** | Per-model inference params, thinking config, reasoning strategies |
| **XML Tool Calling** | Fallback parser for local LLMs without native function calling |
| **Context Compression** | 3-level compression for limited context windows (Hermes pattern) |
| **Smart Approvals** | LLM-based risk assessment for dangerous operations |
| **Docker Sandbox** | Per-user containers with resource limits and network deny-by-default |
| **ToolGuard** | 20+ YAML security rules with severity levels (CRITICAL/HIGH/MEDIUM/INFO) |
| **Subagents** | Isolated ReAct loops with specialized tools (60-80% context savings) |
| **Skills** | Markdown-based instructions with TF-IDF semantic matching + hot-reload |
| **Plugins** | Subprocess-sandboxed extensions with manifest.yaml |
| **MCP Integration** | Model Context Protocol servers via stdio JSON-RPC |
| **User Onboarding** | Hybrid deterministic Q&A + LLM profile finalization |
| **Calibration Phase** | Auto-adapt prompts/tool descriptions/few-shots for specific local models |
| **RBAC** | 10 departments with per-department tool permissions and budgets |
| **Hot Reload** | Skills, plugins, and MCP servers reload without restart |

---

## Built-in Tools

| Tool | Risk | Description |
|------|------|-------------|
| `read_file` | LOW | Read files with path traversal protection |
| `write_file` | MEDIUM | Write files, auto-creates parent dirs |
| `edit_file` | MEDIUM | Exact search/replace in files |
| `list_files` | LOW | Directory listing with metadata |
| `search_files` | LOW | Regex search (skips .git/node_modules) |
| `exec_script` | HIGH | Shell commands with timeout (30s default, 120s max) |
| `web_fetch` | MEDIUM | HTTP requests with SSRF protection |
| `read_image` | LOW | Vision analysis via separate LLM call |
| `memory_store` / `memory_recall` | LOW | Per-user persistent facts (SQLite) |
| `normalize_excel` | MEDIUM | Fix Excel formatting (INN, dates, invisible chars) |
| `send_file` | MEDIUM | Deliver files to user via channel |
| `dispatch_subagent` | LOW | Delegate to specialized subagent |

---

## Extensions

### Skills (`skills/*.md`)

Markdown files with YAML frontmatter. Automatically loaded and hot-reloaded.

```markdown
---
id: my_skill
description: "Does something useful"
allowed_for: ["marketing", "engineering"]
keywords: ["report", "generate"]
always: false
---

Instructions for the agent...
```

### Plugins (`plugins/<name>/`)

Complex extensions with subprocess isolation:

```
plugins/my_plugin/
├── manifest.yaml      # Required
├── skill.md           # Optional — agent instructions
├── tool.py            # Optional — subprocess-sandboxed tool
└── scripts/           # Optional
```

### Subagents (`config/subagents/*.yaml`)

Specialized agents with isolated context and filtered tools:

| Subagent | Tools | Purpose |
|----------|-------|---------|
| `filesystem-agent` | read_file, list_files, search_files | Filesystem operations |
| `document-agent` | read/write/edit_file, normalize_excel | Document processing |
| `execution-agent` | exec_script, write_file, read_file | Code and script execution |
| `research-agent` | web_fetch, search_files, memory | Web research and analysis |

### MCP Servers (`config/mcp_servers.yaml`)

```yaml
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

---

## Configuration

| File | Purpose |
|------|---------|
| `config/settings.yaml` | Main config: LLM providers, agent, container, skills, telegram |
| `config/model_presets.yaml` | Per-model inference params and thinking config |
| `config/departments.yaml` | RBAC: tool permissions and budgets per department |
| `config/tool_guard_rules.yaml` | 20+ security rules for ToolGuard |
| `config/network_policy.yaml` | Network allowlist for containers |
| `config/calibration_scenarios.yaml` | 20+ test scenarios for calibration |
| `config/bootstrap/*.md` | Agent identity: SOUL.md, COMPANY.md, BEHAVIOR.md |
| `config/bootstrap/departments/*.md` | Per-department system prompts |
| `config/bootstrap/subagents/*.md` | Per-subagent system prompts |

### LLM Router

Route different tasks to specific providers:

```yaml
llm:
  default: "default"
  named:
    default:
      type: "openai"
      model: "qwen3.5-4b"
      base_url: "http://localhost:11434/v1"
      preset: "qwen3.5-thinking"
    cloud:
      type: "anthropic"
      model: "claude-sonnet-4-20250514"
      api_key: "${ANTHROPIC_API_KEY}"
  routing:
    - task_kind: "vision"
      provider: "default"
    - subagent_id: "code_review"
      provider: "cloud"
```

### Model Presets

Different models need different inference parameters and reasoning strategies:

```yaml
presets:
  qwen3.5-thinking:
    thinking:
      source: "native"          # Uses reasoning_content field from API
    thinking_budget_tokens: 1024
    inference_params:
      temperature: 0.7
      top_p: 0.95
      top_k: 20
```

**Priority:** `request-level > preset > provider defaults`

---

## Calibration

Auto-adapt configuration for a specific local model. A cloud model analyzes failures on typical scenarios and iteratively improves prompts, tool descriptions, and few-shot examples.

```bash
# Check baseline score without cloud
uv run corpclaw-lite calibrate --dry-run

# Full calibration (requires cloud provider in settings.yaml)
uv run corpclaw-lite calibrate --cloud-provider cloud --max-iterations 5
```

Calibration only edits YAML/Markdown config files, never Python code.

---

## CLI Commands

```bash
# Chat
uv run corpclaw-lite chat                                       # Interactive CLI
uv run corpclaw-lite chat --setup                               # User onboarding

# Telegram
uv run corpclaw-lite telegram                                   # Start bot

# User management
uv run corpclaw-lite user-list
uv run corpclaw-lite user-create -t <tg_id> -d <department>
uv run corpclaw-lite user-allow -t <tg_id> -d <department>
uv run corpclaw-lite user-deny -t <tg_id>
uv run corpclaw-lite user-revoke -t <tg_id>

# Extensions
uv run corpclaw-lite skill list
uv run corpclaw-lite plugin list
uv run corpclaw-lite generate skill <name>                      # Scaffold skill
uv run corpclaw-lite generate plugin <name>                     # Scaffold plugin
uv run corpclaw-lite generate subagent <name>                   # Scaffold subagent

# Docker
uv run corpclaw-lite containers                                 # Active containers
uv run corpclaw-lite prune                                      # Remove idle containers

# Calibration
uv run corpclaw-lite calibrate --dry-run                        # Baseline score
uv run corpclaw-lite calibrate                                  # Full calibration
```

---

## Development

### Setup

```bash
uv sync
```

### Checks (run before committing)

```bash
# Full check
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v

# Individual
uv run ruff check src/ --fix          # Lint
uv run ruff format src/               # Format
uv run pyright src/                   # Type check (strict mode)
uv run pytest tests/ -v               # Tests
uv run pytest tests/ --cov=src/corpclaw_lite --cov-report=term-missing  # Coverage
```

### Project Stats

| Component | LOC | Files |
|-----------|-----|-------|
| Agent Core | 1,888 | 10 |
| Extensions | 3,991 | 38 |
| Channels | 2,546 | 14 |
| Calibration | 1,498 | 8 |
| LLM Providers | 1,126 | 7 |
| Container | 807 | 6 |
| Security | 528 | 5 |
| Memory | 501 | 3 |
| Onboarding | 614 | 5 |
| Other | ~1,855 | ~23 |
| **Source** | **~15,354** | **~119** |
| **Tests** | **~11,197** | **~68** (657 test functions) |

---

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
