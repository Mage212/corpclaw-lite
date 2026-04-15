# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is this project

**CorpClaw Lite** — корпоративный AI-агент: Telegram-бот для рутинных задач через скиллы/плагины/субагенты, работающий с **локальными LLM** (Qwen, Mistral, Llama через Ollama/vLLM/LM Studio) и управляющий доступом по департаментам.

Ключевой принцип: простота > enterprise-архитектура. Минимум кода для максимума ценности.

## Build / Lint / Test

**Package Manager:** `uv` only. Never `pip`.

```bash
uv sync                    # Sync dependencies
uv add <package>           # Add dependency
uv run <command>           # Run commands
```

```bash
# Lint + format (always run ruff format after ruff check)
uv run ruff check src/ --fix && uv run ruff format src/

# Type check (pyright, NOT mypy)
uv run pyright src/

# Tests
uv run pytest tests/ -v
uv run pytest tests/test_agent_loop.py -v     # Single test file
uv run pytest -k "test_name" -v                # Single test by name
uv run pytest tests/ --cov=src/corpclaw_lite --cov-report=term-missing

# Full check before committing
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

**Ruff rules:** `E, F, I, UP, B, C4, SIM, G, A, PERF` (line-length: 100)
**Type checker:** pyright strict mode (`typeCheckingMode = "strict"`)
**Test markers:** `integration` (requires LLM+Docker), `docker_required`, `llm_required`

## Architecture

### Core loop — Simple ReAct (NO LLM-based planning)

```
Message → Context build → LLM call → tool_calls? → Execute → append results → repeat
                                          → no tool_calls? → Response → save to memory
```

Guards: `SimpleBudgetGuard` (max_iter=15, max_tools=30, max_time=120s) + `SimpleProgressGuard` (loop detection after 3 same-tool errors).

**NEVER add:** `TaskPlanner`, `TaskVerifier`, `ObjectiveStorage`, `ProgressGuard LLM-based`.

### Agent Factory (`agent/factory.py`)

`build_agent_stack()` → `AgentStack` — единая точка сборки всего стека агента:

1. **LLM Provider**: `_build_router()` — из settings.yaml + model_presets.yaml, fallback к env vars
2. **Tools**: Container mode → `IPCToolProxy` (7 filesystem tools в Docker); Dev mode → прямая регистрация
3. **Security**: `_build_security_stack()` — ToolGuard (YAML rules) + PermissionChecker (departments)
4. **Extensions**: subagents, host-side tools (web_fetch, read_image), skills, plugins
5. **Memory**: SQLiteMemory + MemoryConsolidator + ContextCompressor
6. **System Prompt**: BootstrapLoader из `config/bootstrap/*.md` + calibrated overrides

`AgentStack` dataclass содержит: `loop`, `user_manager`, `tool_registry`, `mcp_manager`, `container_manager`, `few_shots`, `subagent_registry`, `skill_registry`, `plugin_registry`, `skill_matcher`.

### Context building (`agent/context.py`)

`ContextBuilder.build_initial()` — 4 фазы, критичные для совместимости с Qwen3.5:

1. **Extract system messages** — merge history system messages into system_prompt (предотвращает mid-conversation system messages)
2. **Strip leading assistant messages** — merge into system_prompt (Qwen3.5 требует user-first)
3. **Inject few-shots** — calibration examples (вопрос → tool_call)
4. **Add history + current message** — drop orphaned tool messages

### Parallel tool execution

Условия: >1 tool call + все инструменты `parallel_safe=True`. ToolGuard проверки внутри каждого вызова.
- `parallel_safe=False` → sequential (инструменты с race conditions)
- `terminal=True` → single tool call bypasses LLM re-paraphrase (read_image, dispatch_subagent)

### Provider routing (`llm/router.py`)

YAML-based маршрутизация: `task_kind` (vision, consolidate) и `subagent_id` → конкретный provider.
Поддержка нескольких провайдеров одновременно (local Ollama + cloud Anthropic).

### XML tool calling (`llm/xml_tool_calling.py`)

Для локальных LLM без native function calling:

```xml
<invoke><name>tool_name</name><arguments>{"key": "value"}</arguments></invoke>
```

Двухуровневый парсинг (openai.py): native SDK parsing → XML fallback → repair loop при malformed JSON.

### Key modules

| Path | Role |
|------|------|
| `agent/loop.py` | AgentLoop — ReAct cycle, parallel tool execution, compression, budget guards |
| `agent/context.py` | ContextBuilder — 4-phase message assembly for local LLM compat |
| `agent/compressor.py` | 3-level context compression (prune → sanitize → LLM summarize) |
| `agent/guards.py` | SimpleBudgetGuard + SimpleProgressGuard |
| `agent/vision.py` | VisionProcessor — separate LLM call (NOT in-band) |
| `agent/subagent.py` | SubagentDispatcher — isolated AgentLoop with filtered tools |
| `agent/factory.py` | `build_agent_stack()` → AgentStack — wires all components |
| `agent/prompt.py` | `build_skill_block()` — merges standalone + plugin skills |
| `llm/base.py` | `Provider` / `VisionProvider` protocols, `LLMResponse`, `ToolCall` |
| `llm/openai.py` | OpenAI provider: base_url for Ollama/vLLM, native + XML fallback |
| `llm/anthropic.py` | Anthropic provider: native tool calling |
| `llm/xml_tool_calling.py` | XML parser for local LLMs — DO NOT modify |
| `llm/presets.py` | `ModelPreset`, `ThinkingConfig`, `PresetRegistry` |
| `llm/router.py` | `LLMRouter` — task_kind + subagent_id routing |
| `extensions/tools/base.py` | `Tool` ABC: name, description, params, execute, risk_level, parallel_safe, terminal |
| `extensions/tools/registry.py` | `ToolRegistry`: register, execute, YAML description overrides, `to_schemas_for_user()` |
| `extensions/tools/builtin/` | 13 builtin tools (see table below) |
| `extensions/skills/matcher.py` | TF-IDF semantic matching (bilingual RU+EN stop-words, top_k=3) |
| `extensions/skills/watcher.py` | Hot-reload polling (5s interval) |
| `extensions/plugins/sandbox_proxy.py` | `PluginToolProxy` — JSON-RPC over stdin/stdout subprocess |
| `extensions/plugins/sandbox_worker.py` | Subprocess entry point for plugin tools |
| `extensions/subagents/registry.py` | Loads YAML from `config/subagents/`, 4 builtins |
| `extensions/mcp/` | MCPClient (stdio JSON-RPC), MCPManager, MCPToolAdapter |
| `extensions/bootstrap.py` | `load_extensions()` — shared by CLI + Telegram |
| `security/tool_guard.py` | YAML rules + Smart Approvals (LLM-based risk assessment) |
| `security/network_policy.py` | Deny-by-default for containers |
| `security/credential_scrubber.py` | Masks API keys, tokens in logs |
| `security/ipc_auth.py` | HMAC-SHA256 + nonce replay protection (300s TTL) |
| `container/manager.py` | Per-user Docker containers, resource limits |
| `container/ipc.py` | Stateless docker-exec IPC with signature verification |
| `container/proxy.py` | `IPCToolProxy` — wraps tools for container execution |
| `container/agent_worker.py` | Container-side worker (verifies signatures, executes tools) |
| `channels/base.py` | Channel protocol: start/stop/send_message/send_file/request_approval |
| `channels/telegram/orchestrator.py` | TelegramBotOrchestrator — full lifecycle, hot-reloaders, rate limiting |
| `memory/sqlite.py` | SQLiteMemory — async WAL, auto schema migration |
| `memory/consolidation.py` | MemoryConsolidator — LLM-based compression with cooldown guardrails |
| `config/settings.py` | Pydantic settings hierarchy (Settings → LLM/Agent/Container/Telegram/Skills/Logging) |
| `config/loader.py` | `load_settings()` — env interpolation (`${VAR:-default}`), calibrated overrides |
| `config/bootstrap.py` | BootstrapLoader — modular system prompts with mtime caching |
| `calibration/` | Auto-calibration: CalibrationLoop, ScenarioRunner, Scorer, ConfigEditor |
| `onboarding/` | Hybrid onboarding: deterministic Q&A + LLM finalization |

### Builtin tools

| Tool | Risk | Notes |
|------|------|-------|
| `read_file` | LOW | Path traversal protection |
| `write_file` | MEDIUM | Auto-creates parent dirs |
| `edit_file` | MEDIUM | Exact search/replace, max_replacements |
| `list_files` | LOW | Directory listing with metadata |
| `search_files` | LOW | Regex search, skips .git/node_modules |
| `exec_script` | HIGH | Timeout enforcement (30s default, 120s max), 50KB output truncation |
| `web_fetch` | MEDIUM | SSRF protection (private IPs, DNS rebinding), 1MB size limit |
| `read_image` | LOW | Terminal=True, separate vision LLM call |
| `memory_store` / `memory_recall` | LOW | Per-user SQLite facts |
| `normalize_excel` | MEDIUM | INN fix, date formatting, invisible char removal |
| `send_file` | MEDIUM | 20MB size limit, workspace path resolution |
| `dispatch_subagent` | LOW | Terminal=True, department permission checks |

### Subagents (4 builtins)

| ID | Tools | Purpose |
|----|-------|---------|
| `filesystem-agent` | read_file, list_files, search_files | Filesystem ops and search |
| `document-agent` | read_file, write_file, edit_file, normalize_excel, list_files | Document creation/editing |
| `execution-agent` | exec_script, write_file, read_file | Shell commands and scripts |
| `research-agent` | web_fetch, read_file, search_files, memory_store/recall | Web research and analysis |

### Security stack (executed BEFORE tool invocation)

```
ChannelAuth → ToolGuard (YAML rules + Smart Approvals) → PermissionCheck (department RBAC) → Container (NetworkPolicy deny-by-default) → CredentialScrubber
```

**Smart Approvals:** при `approval_mode="smart"` LLM оценивает реальный риск: APPROVE / DENY / ESCALATE.
**ToolGuard rules:** 20+ YAML правил, severity CRITICAL/HIGH/MEDIUM/INFO, regex patterns на tool arguments.

### Plugin sandbox architecture

```
PluginToolProxy (host) ←→ JSON-RPC over stdin/stdout ←→ sandbox_worker.py (subprocess)
```

- Lazy subprocess spawning (on first execute), reused for subsequent calls
- asyncio.Lock serializes concurrent requests
- 30s timeout per execution
- Introspection via `--introspect` flag (returns tool schema as JSON)

### Data flow

```
Message → Channel (Telegram/CLI) → Orchestrator (auth/user/rate-limit) → AgentLoop →
Provider (Router) → tool_calls → Security stack → Tool execution (host or Docker container) →
Response → Channel
```

## Code style

- Python 3.12+, modern syntax: `list[str]` not `List[str]`, `str | None` not `Optional[str]`
- `from __future__ import annotations` at top of every file
- Async-first (`asyncio`), `anyio` for async file ops
- Pydantic for config/data models, frozen dataclasses for immutable configs
- Protocol-based interfaces (no `Protocol` suffix: `Provider`, `Channel`, `Tool`)
- Naming: `PascalCase` classes, `snake_case` functions, `UPPER_SNAKE_CASE` constants, `_` prefix for private
- Custom exceptions: `CorpClawError`, `StorageError`, `ToolExecutionError`, `ContainerIPCError`, `ToolGuardError`, `PermissionDeniedError`, `BudgetExceededError`, `ApprovalRequest`

```python
# Import pattern
from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING, Any
from pydantic import BaseModel
from corpclaw_lite.extensions.tools.base import Tool

if TYPE_CHECKING:
    from corpclaw_lite.agent.loop import AgentLoop
```

## Commits

- English, concise, conventional style
- Always add trailer: `Co-Authored-By: GLM-5.1`

## CLI commands

```bash
uv run corpclaw-lite chat                       # Interactive CLI chat
uv run corpclaw-lite chat --setup               # Onboarding
uv run corpclaw-lite telegram                   # Start Telegram bot
uv run corpclaw-lite user-list
uv run corpclaw-lite user-create -t <telegram_id> -d <department>
uv run corpclaw-lite user-allow -t <telegram_id> -d <department>
uv run corpclaw-lite user-deny -t <telegram_id>
uv run corpclaw-lite containers                 # Active Docker containers
uv run corpclaw-lite prune                      # Remove idle containers
uv run corpclaw-lite calibrate [options]        # Auto-calibrate for local model
uv run corpclaw-lite generate skill <name>      # Scaffold skill
uv run corpclaw-lite generate plugin <name>     # Scaffold plugin
uv run corpclaw-lite generate subagent <name>   # Scaffold subagent
```

## Config files

| File | Purpose |
|------|---------|
| `config/settings.yaml` | Main config (LLM, agent, container, skills, telegram, logging) |
| `config/model_presets.yaml` | 3 presets: gemma4-thinking, gemma4-fast, qwen3.5-thinking |
| `config/departments.yaml` | 10 departments with RBAC + per-dept budgets |
| `config/tool_guard_rules.yaml` | 20+ security rules (CRITICAL/HIGH/MEDIUM/INFO) |
| `config/network_policy.yaml` | Allowlist: api.anthropic.com, localhost:11434, api.github.com |
| `config/calibration_scenarios.yaml` | 20+ scenarios (tool_use, no_tool, multi_step, error_recovery) |
| `config/mcp_servers.yaml` | MCP servers (currently empty, ready for use) |
| `config/subagents/*.yaml` | 4 subagent definitions |
| `config/bootstrap/*.md` | SOUL.md + COMPANY.md + BEHAVIOR.md |
| `config/bootstrap/departments/*.md` | 10 department-specific prompts |
| `config/bootstrap/subagents/*.md` | 4 subagent-specific prompts |

## Skills (5 loaded)

| Skill | Departments |
|-------|-------------|
| `code_reviewer` | it, admin, default |
| `content_writer` | marketing, hr, admin, default |
| `doc_writer` | it, product, admin, default |
| `translator` | * (all) |
| `excel_normalizer` | marketing, finance, hr, analytics, admin, default |

Skills are selected via TF-IDF semantic matching (top_k=3, threshold=0.08) + keyword boost (0.5). Skills with `always=True` are unconditionally injected.

## Model presets

Managed via `config/model_presets.yaml`, NOT hardcoded in providers.

**ThinkingConfig**: `open_tag`, `close_tag`, `budget_tokens`, `source` ("content" for tag parsing, "native" for reasoning_content field).

Priority: `request-level > preset > provider defaults`

Reasoning: stored in SQLite (`reasoning` column), logged but NOT injected into agent context.

## Hot reload

3 polling-based watchers (not inotify/watchdog):
- `SkillHotReloader` (5s interval) — `skills/*.md`
- `PluginWatcher` (10s interval) — `plugins/*/manifest.yaml`
- `MCPWatcher` (10s interval) — `config/mcp_servers.yaml`

All start as background asyncio tasks, stop via `GracefulShutdown` (SIGINT/SIGTERM).

## Container system

- Per-user Docker: `corpclaw_agent_{user_id}` (bind-mount `/workspace`)
- Image: `corpclaw-agent-base:latest` (python:3.12-slim, non-root uid 1001)
- Limits: 512m RAM, 0.5 CPU, 100 PIDs, cap_drop ALL, seccomp
- IPC: stateless `docker exec` + HMAC-signed JSON + dual timeout
- Dev mode: `container.enabled=false` → tools run directly on host

## Hard rules

- `llm/xml_tool_calling.py` — do NOT modify structure, critical for local LLMs
- `read_image` — separate LLM call, never in-band image data
- Never add versioning/deprecation metadata to tools/skills (was v1 mistake)
- ToolGuard rules in YAML only, not in Python code
- IPC HMAC+nonce is mandatory, fail-fast without `CORPCLAW_IPC_SECRET` (min 16 chars)
- Context building must handle Qwen3.5 quirks (user-first, no mid-conversation system messages)
- Calibration edits ONLY YAML/Markdown "Edit Surfaces", never Python code
