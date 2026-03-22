# AGENTS.md — CorpClaw Lite

Этот файл — первое, что нужно прочитать AI-агенту перед любой работой в этом репозитории.

---

## Что такое этот проект

**CorpClaw Lite** — это редизайн с нуля корпоративного AI-агента [CorpClaw](../), написанный на чистом Python с учётом опыта и ошибок первой версии.

**Назначение:** Надёжный Python AI-агент для корпоративного закрытого контура — Telegram-бот, который выполняет рутинные задачи через скиллы/плагины/субагенты, работает с **локальными LLM** и управляет доступом по департаментам.

**Отличие от v1:** Не enterprise middleware и не конкурент OpenClaw. Фокус на:
- Простоте (минимум кода для максимума ценности)
- Работе с локальными LLM (Qwen, Mistral, Llama через Ollama/vLLM/LM Studio)
- Безопасности встроенной в ядро, а не добавленной поверх
- Лёгком расширении через манифесты (skills, plugins, subagents, channels)

---

## Ключевые документы

| Документ | Описание |
|----------|----------|
| [`plans/corpclaw-lite-design.md`](plans/corpclaw-lite-design.md) | **Главный дизайн-документ** — архитектура, структура, фазовый план |
| [`../docs/FINAL_CRITICAL_ANALYSIS.md`](../docs/FINAL_CRITICAL_ANALYSIS.md) | Финальный критический анализ v1 и стратегия ("Путь C") |
| [`../docs/CRITICAL_ANALYSIS_OVERENGINEERING.md`](../docs/CRITICAL_ANALYSIS_OVERENGINEERING.md) | Разбор оверинжиниринга v1 — чего НЕ повторять |

**Референсные проекты** (в `../references/`):
- [`../references/openclaw/`](../references/openclaw/) — OpenClaw (TypeScript, 857K LOC). Изучать для понимания зрелых архитектурных решений: session loop, file-based approvals, plugin SDK
- [`../references/NemoClaw/`](../references/NemoClaw/) — NVIDIA NemoClaw: security overlay pattern. **Ключевой источник:** 4 слоя безопасности (Network deny-by-default + Filesystem + Process + Inference rerouting) реализованы через YAML-политики в ~2,287 строк
- [`../references/CoPaw/`](../references/CoPaw/) — Alibaba CoPaw (Python). **Ключевой источник:** Tool Guard через mixin pattern, YAML-правила severity levels, one-shot approvals; unified channel pattern; skills как markdown, hot-reload

**Рабочая кодовая база v1** (в `../src/corpclaw/`):
- `../src/corpclaw/llm/` — LLM провайдеры; **`xml_tool_calling.py` скопирован в `src/corpclaw_lite/llm/`** — это критический модуль для локальных LLM, не трогать структуру
- `../src/corpclaw/agent/executor/prompt_loop_executor.py` — рабочий пример ReAct цикла (600+ строк) — брать за основу при написании нового loop.py, упростив до ~400 строк
- `../src/corpclaw/agent/guards.py` — `SimpleBudgetGuard` и `SimpleProgressGuard` — переносить как есть
- `../src/corpclaw/container/` — рабочая Docker-изоляция — адаптировать, упростив state machine
- `../src/corpclaw/memory/sqlite.py` — рабочий SQLite бэкенд памяти — переносить как есть
- `../src/corpclaw/channels/telegram/` — рабочий Telegram-канал — адаптировать под новый Channel Protocol

---

## Build / Lint / Test

**Package Manager:** `uv` — всегда и только `uv`. Никогда не использовать `pip` напрямую.

```bash
# Запуск команд
uv run <command>

# Синхронизация зависимостей
uv sync

# Добавление зависимости
uv add <package>
```

**Линтинг и форматирование:**
```bash
uv run ruff check src/ --fix
uv run ruff format src/
```

**Проверка типов (pyright, не mypy):**
```bash
uv run pyright src/
```

**Тесты:**
```bash
uv run pytest tests/ -v
uv run pytest tests/test_agent_loop.py -v
uv run pytest -k "test_name" -v
uv run pytest tests/ --cov=src/corpclaw_lite --cov-report=term-missing
```

**Полная проверка перед завершением работы:**
```bash
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

---

## CLI команды (после реализации)

```bash
uv run corpclaw-lite chat                       # Интерактивный CLI чат
uv run corpclaw-lite telegram                   # Запуск Telegram-бота
uv run corpclaw-lite user-list                  # Список пользователей
uv run corpclaw-lite user-create -t <telegram_id> -d <department>
uv run corpclaw-lite containers                 # Активные Docker-контейнеры
uv run corpclaw-lite prune                      # Удаление idle-контейнеров
uv run corpclaw-lite skill list                 # Список загруженных скилов
uv run corpclaw-lite plugin list                # Список плагинов
uv run corpclaw-lite generate skill <name>      # Создание шаблона скила
uv run corpclaw-lite generate plugin <name>     # Создание шаблона плагина
uv run corpclaw-lite generate subagent <name>   # Создание шаблона субагента
```

---

## Архитектура: Ключевые принципы

### 1. Simple ReAct Loop (НЕ LLM-based planning)

Агентный цикл — классический ReAct без LLM-планировщиков:
```
Сообщение → Сборка контекста → LLM вызов
→ tool_calls? → Выполнить → добавить результаты → повторить
→ нет tool_calls? → Ответ → сохранить в память
```
`SimpleBudgetGuard` (max_iter, max_tools, max_time) + `SimpleProgressGuard` (детекция зацикливания).

**НЕЛЬЗЯ** добавлять: `TaskPlanner`, `TaskVerifier`, `ObjectiveStorage`, `ProgressGuard LLM-based`.

### 2. Субагенты — изолированные исполнители

Основной агент знает только: `list_files`, `read_file`, `search_files` + каталог субагентов.
При задаче → основной агент вызывает субагента с полным контекстом задачи.
Субагент: чистая история + специализированные инструменты + свои скилы → возвращает компактный результат.

**Это критично для локальных LLM** — снижает нагрузку на контекстное окно основного агента на 60-80%.

### 3. read_image — отдельный LLM-вызов

`read_image` НЕ возвращает изображение в контекст. Он делает отдельный вызов к vision-провайдеру и возвращает текстовое описание. Это выявленная особенность работы с локальными моделями.

### 4. Единая система расширений через манифесты

Все расширения регистрируются через `manifest.yaml` с полями `name`, `version`, `type`, `description`, `allowed_departments`, `components`. Типы: `tool`, `skill`, `plugin`, `subagent`, `channel`.

**Нет** `ExtensionCatalog`, `CompatibilityStatus`, `EntityManifest`, `AvailabilityResolver` — это было главной ошибкой v1.

### 5. Security встроен в ядро

Стек безопасности выполняется **до** вызова инструмента:
```
ChannelAuth → ToolGuard (YAML rules) → PermissionCheck → Container → CredentialScrubber
```
- **ToolGuard** (по образцу CoPaw): YAML-правила, severity CRITICAL/HIGH/MEDIUM/INFO, inline approve/deny
- **NetworkPolicy** (по образцу NemoClaw): deny-by-default + allowlist для контейнеров
- **IPC Auth**: HMAC + nonce — **обязательна**, fail-fast при отсутствии `CORPCLAW_IPC_SECRET`

### 6. Каналы — расширения с Protocol

CLI — базовый канал. Telegram — первый плагин-канал. Channel Protocol:
```python
async def send_message(chat_id, text, **opts) -> None
async def request_approval(chat_id, action, details) -> bool  # inline кнопки
```
Telegram поддерживает MarkdownV2/HTML форматирование и inline Approve/Deny кнопки.

---

## Структура проекта

```
corpclaw-lite/
├── src/corpclaw_lite/
│   ├── agent/          # loop.py, context.py, guards.py, vision.py, subagent.py
│   ├── llm/            # base.py, anthropic.py, openai.py, xml_tool_calling.py, routing.py
│   ├── extensions/
│   │   ├── tools/      # base.py, registry.py, guard.py, builtin/
│   │   ├── skills/     # base.py, loader.py, registry.py
│   │   ├── plugins/    # base.py, loader.py, registry.py
│   │   ├── subagents/  # base.py, registry.py, builtin/
│   │   └── mcp/        # client.py, manager.py, adapter.py
│   ├── channels/       # base.py, registry.py, cli.py, telegram/
│   ├── security/       # tool_guard.py, network_policy.py, credential_scrubber.py, ipc_auth.py
│   ├── container/      # manager.py, ipc.py, policies.py
│   ├── departments/    # manager.py, permissions.py
│   ├── memory/         # sqlite.py, manager.py, consolidation.py, vector/
│   ├── users/          # models.py, manager.py
│   ├── config/         # settings.py, loader.py
│   └── logging/        # agent_logger.py, scrubber.py, rotation.py
├── config/
│   ├── settings.yaml
│   ├── departments.yaml
│   ├── tool_guard_rules.yaml    # ToolGuard правила (CoPaw pattern)
│   ├── network_policy.yaml      # Network allowlist (NemoClaw pattern)
│   └── bootstrap/               # Модульные промпты (SOUL.md, COMPANY.md ...)
├── skills/             # Markdown-скилы
├── plugins/            # Папки плагинов с manifest.yaml
├── plans/              # Планы разработки (не игнорируются gitignore)
└── tests/
```

---

## Стиль кода

### Python версия
Python 3.12+ обязательно. Современный синтаксис типов: `list[str]` не `List[str]`, `str | None` не `Optional[str]`.

### Импорты
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

### Форматирование
- Длина строки: **100 символов** (ruff)
- Двойные кавычки для строк
- `ruff format` для всего форматирования

### Типы
- Strict pyright (`typeCheckingMode = "strict"`) **обязательно** — НЕ mypy
- Все функции должны иметь аннотации типов
- `Any` — только если нет другого способа

### Именование
- Классы: `PascalCase` (`AgentLoop`, `ToolGuard`, `SkillRegistry`)
- Функции/методы: `snake_case` (`get_budget`, `can_use_tool`)
- Константы: `UPPER_SNAKE_CASE` (`IMAGE_EXTENSIONS`, `MAX_HISTORY`)
- Приватные методы: с `_` (`_build_context`, `_check_permissions`)
- Протоколы: без суффикса Protocol (`Provider`, `Channel`, `Tool`)

### Async
- Весь проект async-first
- `anyio` для async файловых операций
- Async generators возвращают `AsyncIterator` напрямую

### Ошибки
```python
class ToolGuardError(Exception):
    """Raised when ToolGuard blocks a tool call."""

class PermissionDeniedError(Exception):
    """Raised when user lacks permission for the resource."""
```

---

## Правила работы с расширениями

### Tool
- Только 5 атрибутов: `name`, `description`, `params`, `execute`, `risk_level`
- **НЕЛЬЗЯ** добавлять: `api_version`, `deprecated_since`, `removal_in`, `replacement`, `migration_doc`, `provenance`, `compatibility`, `warning_code`
- Проверка типов файлов **на уровне кода** — IMAGE не читается ReadFileTool и наоборот

### Skill
- Только 6 атрибутов: `id`, `description`, `allowed_for`, `instructions`, `path`, `version`
- **НЕЛЬЗЯ** добавлять: `dependencies`, `resources`, `override_chain`, `pack_id`, `compatibility`, `provenance`
- Markdown-файл в `skills/` — единственный источник

### Plugin
- Структура: `manifest.yaml` + `skill.md` + опционально `tool.py` + опционально `scripts/`
- ToolGuard применяется к script execution автоматически

---

## Что делать с v1 кодом

| Модуль v1 | Действие |
|-----------|----------|
| `llm/xml_tool_calling.py` | ✅ Скопирован в новый проект — использовать без изменений |
| `agent/executor/prompt_loop_executor.py` | 🔄 Адаптировать — это основа нового `agent/loop.py`, убрать extensibility зависимости |
| `agent/guards.py` | ✅ `SimpleBudgetGuard` + `SimpleProgressGuard` — переносить как есть |
| `container/manager.py` | 🔄 Адаптировать — убрать state machine, добавить NetworkPolicy |
| `container/ipc.py` | 🔄 Адаптировать — сделать HMAC обязательным с nonce |
| `memory/sqlite.py` | ✅ Переносить как есть |
| `channels/telegram/` | 🔄 Адаптировать под новый Channel Protocol |
| `llm/anthropic.py`, `llm/openai.py` | 🔄 Адаптировать под новый Provider Protocol |
| `agent/orchestration/` | ❌ НЕ переносить — весь LLM-based planning |
| `extensibility/` | ❌ НЕ переносить — весь extensibility framework |
| `plugins/manager.py` | ❌ НЕ переносить — заменить простым manifest loader |
| `governance/` | ❌ НЕ переносить — заменить structured logging |
| `approvals/service.py` | ❌ НЕ переносить — заменить ToolGuard inline кнопками |

---

## Управление планами

### Сохранение планов
- Все планы сохраняются в `plans/`
- Имена файлов: `plans/<feature-or-task-name>.md`
- `plans/` добавлен в исключения `.gitignore` корневого проекта

### Шаблон плана
```markdown
# <Название задачи>

## Summary
<Краткое описание>

## Goals
- <Цель 1>

## Steps
1. <Шаг 1>
2. <Шаг 2>

## Status
- [x] Выполненный шаг
- [ ] Ожидающий шаг

## Notes
<Контекст>
```

### Workflow
1. Подготовить план → показать пользователю → дождаться одобрения
2. Сохранить план в `plans/`
3. Приступить к выполнению, обновляя статус шагов

---

## Чеклист готовности к деплою

- [ ] `uv run corpclaw-lite telegram` запускается и отвечает
- [ ] Маркетолог говорит «нормализуй Excel» → получает файл обратно через локальную LLM
- [ ] `uv run pytest tests/ -v` — ≥75% coverage, 0 failures
- [ ] `uv run pyright src/` — 0 errors (strict mode)
- [ ] `uv run ruff check src/ && uv run ruff format src/` — 0 errors
- [ ] ToolGuard блокирует `rm -rf` через exec_script
- [ ] Добавление `skills/*.md` → доступен без перезапуска (HotReload)
- [ ] IPC между host и контейнером требует `CORPCLAW_IPC_SECRET`
