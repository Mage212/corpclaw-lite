# Contributing to CorpClaw Lite

Thanks for your interest in contributing! This guide covers the essentials.

## Quick Start

```bash
# Fork and clone
git clone https://github.com/Mage212/corpclaw-lite.git
cd corpclaw-lite

# Install (requires uv — https://docs.astral.sh/uv/)
uv sync

# Verify everything works
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

## Development Workflow

The project uses two long-lived branches:

- **`main`** — stable release branch. Tagged with `vX.Y.Z`. Never commit directly; only
  updated by merging from `pre-release` on release.
- **`pre-release`** — integration branch. All work lands here first via feature branches and PRs.

1. Create a branch from `pre-release`: `git checkout -b feature/your-feature pre-release`
2. Make changes
3. Run the full check before committing (see below)
4. Open a pull request against **`pre-release`** (not `main`)

## Code Style

- **Python 3.12+** — use modern syntax: `list[str]` not `List[str]`, `str | None` not `Optional[str]`
- **`from __future__ import annotations`** at the top of every file
- **Async-first** — all I/O is async (`asyncio`, `anyio`)
- **Pydantic** for config/data models, frozen dataclasses for immutable configs
- **Protocol-based interfaces** — no `Protocol` suffix (e.g. `Provider`, `Channel`, `Tool`)
- **100 char line length** (enforced by ruff)

### Import Order

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from corpclaw_lite.extensions.tools.base import Tool

if TYPE_CHECKING:
    from corpclaw_lite.agent.loop import AgentLoop
```

### Naming

- Classes: `PascalCase` (`AgentLoop`, `ToolGuard`)
- Functions/methods: `snake_case` (`get_budget`, `can_use_tool`)
- Constants: `UPPER_SNAKE_CASE` (`MAX_HISTORY`, `PLACEHOLDER`)
- Private methods: `_` prefix (`_build_context`, `_check_permissions`)

## Checks

Run these before every commit. CI will run them on your PR too.

```bash
# Lint + format
uv run ruff check src/ --fix
uv run ruff format src/

# Type check (pyright strict, NOT mypy)
uv run pyright src/

# Tests
uv run pytest tests/ -v

# All-in-one
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

### What CI Checks

- `ruff check` — linting (rules: E, F, I, UP, B, C4, SIM, G, A, PERF)
- `ruff format` — formatting
- `pyright` — type checking in strict mode (pinned to a specific version in `pyproject.toml`; bump deliberately)
- `pytest` — all tests must pass

CI runs on `push`/`pull_request` to **both** `main` and `pre-release`, so every PR is gated before merge — not just at release time.

### Pre-push hook (local quality gate)

The repo ships a versioned `pre-push` hook that runs the same four gates locally **before** a push is allowed, so you get fast feedback instead of waiting for CI. It lives in `.githooks/` (version-controlled) and is activated once after clone:

```bash
# one-time setup after clone:
bash scripts/install-hooks.sh
#   or manually:  git config core.hooksPath .githooks
```

The hook runs, in order (fail-fast — stops at the first failing gate):

1. `uv run ruff check src/ tests/`
2. `uv run ruff format --check src/ tests/`
3. `uv run pyright src/`
4. `uv run pytest tests/ -x`   (full suite, stop on first failing test)

**Bypassing:** `git push --no-verify` (native git) or `CORPCLAW_SKIP_HOOKS=1 git push`. Use only when CI will still run on the target branch, and call it out in the PR. Tag pushes (`refs/tags/*`) are skipped automatically.

> Note: the local hook and CI use the **same pinned pyright version** (see `pyproject.toml`). If the hook passes locally, CI's pyright job passes too — no version-drift surprises.

## Writing Tests

- Place tests in `tests/` matching the module structure: `test_agent_loop.py`, `test_tool_guard.py`, etc.
- Use `pytest-asyncio` (auto mode is enabled)
- Mark integration tests: `@pytest.mark.integration`
- Mark Docker tests: `@pytest.mark.docker_required`
- Mark LLM tests: `@pytest.mark.llm_required`

```python
import pytest

@pytest.mark.asyncio
async def test_my_feature():
    result = await my_function()
    assert result == expected
```

## Adding Extensions

### New Tool

Create a file in `src/corpclaw_lite/extensions/tools/builtin/`:

```python
from __future__ import annotations
from typing import Any
from corpclaw_lite.extensions.tools.base import Tool, ToolParam, RiskLevel

class MyTool(Tool):
    name = "my_tool"
    description = "Does something useful"
    params = [ToolParam(name="input", type="string", description="Input data")]
    risk_level = RiskLevel.LOW
    parallel_safe = True
    terminal = False

    async def execute(self, *, input: str, **kwargs: Any) -> str:
        return f"processed: {input}"
```

Register it in `agent/factory.py` in the appropriate section (container or host tools).

**Attributes:**
- `risk_level`: LOW / MEDIUM / HIGH / CRITICAL
- `parallel_safe`: `True` if the tool has no race conditions (default: True)
- `terminal`: `True` if result goes directly to user without LLM re-paraphrase (e.g. vision, file delivery)

### New Skill

Create a Markdown file in `skills/`:

```markdown
---
id: my_skill
description: "Short description for semantic matching"
allowed_for: ["engineering", "marketing"]  # or ["*"] for all departments
version: "1.0.0"
keywords: ["keyword1", "keyword2"]
always: false
---

# Instructions for the agent

Detailed instructions here...
```

Skills are automatically loaded and hot-reloaded (5s polling).

### New Plugin

Create a directory in `plugins/`:

```
plugins/my_plugin/
├── manifest.yaml      # Required
├── skill.md           # Optional
├── tool.py            # Optional (runs in subprocess sandbox)
└── scripts/           # Optional
```

### New Subagent

Create a YAML file in `config/subagents/`:

```yaml
id: my-agent
name: "My Agent"
description: "What this agent specializes in"
allowed_tools: ["read_file", "list_files"]
allowed_departments: ["*"]
prompt_path: "config/bootstrap/subagents/my-agent.md"
```

Create the corresponding prompt in `config/bootstrap/subagents/my-agent.md`.

## Private Extensions (Overlay)

CorpClaw Lite is open source, but you may have customizations (tools/skills/subagents
for internal systems, RBAC rules, system prompts) that must not enter the public repo.
The extension system is designed so that **99% of customizations are new content, not
core code** — so the private/public boundary runs **by data, not by code**.

### What is public vs private

| Public (this repo) | Private (overlay) |
|--------------------|-------------------|
| RBAC engine, loop, providers, `ToolRegistry`, `BootstrapLoader` (generic code in `src/`) | `skills/*.md`, `plugins/*/`, `config/subagents/*.yaml`, `config/bootstrap/*.md`, `config/mcp_servers.yaml`, `config/departments*.yaml`, additions to `tool_guard_rules.yaml`/`network_policy.yaml` |

If a change touches `src/` (loop, guards, providers, registries), it is a **core change** →
PR to this repo. If it only adds an extension file, it is a **private customization** →
belongs in your overlay.

### How overlays work

Configure overlay paths in `config/settings.yaml`:

```yaml
extensions:
  extra_paths:
    - "${CORPCLAW_PRIVATE_EXTENSIONS}"   # e.g. /opt/corpclaw-private
```

Each overlay path **mirrors the project layout** (mirror-layout). The core loads
extensions from the default directories **plus** each overlay path:

```
/opt/corpclaw-private/
├── skills/*.md                       → overlay skills
├── plugins/<name>/manifest.yaml      → overlay plugins
├── config/
│   ├── bootstrap/*.md                → overlay system prompt (per-file override)
│   ├── subagents/*.yaml              → overlay subagents
│   ├── mcp_servers.yaml              → overlay MCP servers (merged by name)
│   └── departments.yaml              → overlay departments (union-merged)
```

**Override semantics** (overlay wins):
- `skills` / `plugins` / `subagents` / `bootstrap` → an overlay entry with the same id/name
  **replaces** the default one (e.g. a private `excel_normalizer` overrides the public one).
- `departments` → **union-merged**: allowlists are combined (a wildcard `["*"]` absorbs
  additions). Budget's `max_iterations`/`max_tool_calls` are overridden by the overlay
  when present.

Overlay files are hot-reloaded alongside the defaults — editing a private skill or plugin
takes effect without a restart.

### Version contract for plugins

A plugin can declare the core version it depends on, so it fails loudly (warn-and-skip at
load) instead of silently breaking when the core moves on:

```yaml
# plugins/crm-integration/manifest.yaml
requires_core: "^0.1.11"   # compatible with 0.1.x; 0.2.0 will skip this plugin
```

Supported syntax: `^` caret (pins minor for 0.x, major for 1.x+), bare version (exact
match), or empty (no constraint).

### Decision rule

Before writing code, answer: **kernel or extension?**

- New tool/skill/subagent → 99% an **extension** (goes in your overlay; core untouched).
- Edit to `loop.py`, `tool_guard.py`, `ToolRegistry`, providers → **core change** (PR here).
- A feature that needs both → **split into two PRs**: a generic hook (public) + a private
  extension (overlay).

## Architecture Notes

Before contributing, understand these core principles:

- **Simple ReAct Loop** — no LLM-based planners. Never add `TaskPlanner`, `TaskVerifier`, `ObjectiveStorage`
- **Local LLM First** — always test with local models (Ollama), not just cloud
- **Security Stack** — `ChannelAuth → ToolGuard → PermissionCheck → Container → CredentialScrubber` executes before every tool call
- **Context Building** — `ContextBuilder.build_initial()` has 4 phases with Qwen3.5 compatibility quirks; changes must respect these phases
- **Calibration** — only edits YAML/Markdown "Edit Surfaces", never Python code
- **`xml_tool_calling.py`** — do not modify; this is the critical bridge for local LLMs

For full architecture documentation, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Commit Messages

- English, concise, conventional commit style
- Examples: `feat: add web_fetch tool`, `fix: handle timeout in container IPC`, `docs: update README`
- **Tiered commit policy**:
  - **`main`** — never commit directly; only updated by merging from `pre-release` on release.
  - **`pre-release`** — a direct commit is acceptable for **trivial** changes (docstrings,
    comments, docs, formatting, typo fixes). **Functional** changes (new features, behavior
    changes, architecture, `src/` runtime edits) require a feature branch off `pre-release`
    plus a pull request with review.

## Pull Requests

- PRs target **`pre-release`** (not `main`). `main` is updated only by release merges.
- Ensure all CI checks pass
- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality

## Reporting Issues

When filing a bug report, please include:

1. Steps to reproduce
2. Expected vs actual behavior
3. Relevant logs from `logs/corpclaw.log` or `logs/agent_activity.jsonl`
4. Your `config/settings.yaml` (redact secrets)

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
