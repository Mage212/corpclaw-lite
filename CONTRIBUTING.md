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

1. Create a branch from `main`: `git checkout -b feature/your-feature`
2. Make changes
3. Run the full check before committing (see below)
4. Open a pull request against `main`

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
- `pyright` — type checking in strict mode
- `pytest` — all tests must pass

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

## Pull Requests

- PRs go to `main`
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
