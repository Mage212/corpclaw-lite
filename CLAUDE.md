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
- `../src/corpclaw/channels/telegram*.py` — рабочие Telegram-модули (плоские файлы, не поддиректория) — адаптировать под новый Channel Protocol

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
Ruff rules: `E, F, I, UP, B, C4, SIM, G, A, PERF` (line-length: 100)

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

## CLI команды

```bash
uv run corpclaw-lite chat                       # Интерактивный CLI чат
uv run corpclaw-lite chat --setup               # Запуск онбординга
uv run corpclaw-lite telegram                   # Запуск Telegram-бота
uv run corpclaw-lite user-list                  # Список пользователей
uv run corpclaw-lite user-create -t <telegram_id> -d <department>
uv run corpclaw-lite user-allow -t <telegram_id> -d <department>  # Добавить в whitelist
uv run corpclaw-lite user-deny -t <telegram_id>                   # Удалить из whitelist
uv run corpclaw-lite user-revoke -t <telegram_id>                 # Заблокировать сессию
uv run corpclaw-lite containers                 # Активные Docker-контейнеры
uv run corpclaw-lite prune                      # Удаление idle-контейнеров
uv run corpclaw-lite skill list                 # Список загруженных скилов
uv run corpclaw-lite plugin list                # Список плагинов
uv run corpclaw-lite generate skill <name>      # Создание шаблона скила
uv run corpclaw-lite generate plugin <name>     # Создание шаблона плагина
uv run corpclaw-lite generate subagent <name>   # Создание шаблона субагента
uv run corpclaw-lite calibrate [options]        # Авто-калибровка под модель
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

### 7. Model Presets — конфигурация моделей через YAML

Разные локальные LLM требуют разные inference-параметры, форматы thinking-тегов и стратегии парсинга reasoning.
Вместо хардкодинга — система пресетов (`config/model_presets.yaml`):

```yaml
presets:
  qwen3-thinking:
    thinking:
      source: "native"           # "content" (парсинг тегов) или "native" (reasoning_content)
    thinking_budget_tokens: 1024
    inference_params:
      temperature: 1.0
      top_p: 0.95
```

Ключевые модули:
- `src/corpclaw_lite/llm/presets.py` — `ModelPreset`, `ThinkingConfig`, `PresetRegistry`
- `config/model_presets.yaml` — определения пресетов
- `ProviderSettings.preset` — ссылка на пресет по имени

**Приоритет параметров:** `request-level > preset > provider defaults`

**Reasoning:**
- Хранится в `SQLiteMemory` (колонка `reasoning` в `messages`)
- Логируется, но **не попадает** в контекст агента (экономия токенов)
- Подготовлено к будущему интеллектуальному подключению в контекст

**НЕЛЬЗЯ** хардкодить логику thinking/reasoning в провайдерах — всё через пресеты.

### 8. Calibration Phase — авто-калибровка под локальную модель

Одноразовый (или периодический) этап, при котором облачная модель анализирует, как локальная модель справляется с типовыми сценариями, и автоматически правит конфигурации.

Ключевые модули (`src/corpclaw_lite/calibration/`, 1,270 строк):
- `CalibrationLoop` — оркестратор калибровочного цикла
- `ScenarioRunner` — запуск сценариев из `config/calibration_scenarios.yaml` (14 сценариев)
- `CalibrationScorer` — оценка результатов (tool accuracy, response quality)
- `ConfigEditor` — правка Edit Surfaces: system prompt, tool descriptions, few-shots, settings
- `TrajectoryRecorder` — запись траекторий для анализа

**Edit Surfaces** — калибратор правит **только** YAML/Markdown конфигурации, не Python-код:
1. System Prompt (`config/bootstrap/*.md`)
2. Tool Descriptions (YAML-override через `ToolRegistry`)
3. Skill Instructions (`skills/*.md`)
4. Few-shot Examples (генерация примеров «вопрос → tool_call»)

**CLI:** `uv run corpclaw-lite calibrate [options]`

### 9. User Onboarding — гибридный онбординг

Детерминистический движок вопросов + LLM-финализация профиля.

Ключевые модули (`src/corpclaw_lite/onboarding/`, 614 строк):
- `OnboardingEngine` — конечный автомат состояний: вопросы → ответы → финализация
- `OnboardingFinalizer` — LLM-вызов для формирования персонализированного профиля
- `OnboardingQuestions` — каталог вопросов (роль, задачи, стиль общения)
- `OnboardingStorage` — SQLite-хранилище ответов и состояния

**Запуск:** `uv run corpclaw-lite chat --setup` или `/setup` в Telegram.
**Результат:** департамент + персонализированный system prompt → сохраняется в user profile.

### 10. Context Compression — 3-уровневое сжатие

Сжатие контекста внутри одной сессии (паттерн Hermes). Критично для локальных LLM (8K-32K контекст).

Три уровня в `agent/compressor.py` (276 строк):
1. **Prune tool results** — замена старых tool outputs (>200 chars) на placeholder
2. **Sanitize orphaned tool pairs** — очистка потерянных tool_call/result
3. **LLM summarization** — сжатие первого полу history в структурированный summary

Конфигурация через `settings.yaml` → `compression.enabled`, `threshold_ratio`, `max_context_tokens`.

### 11. Hot Reload — автоматическая перезагрузка расширений

Три watcher'а (polling-based, не inotify/watchdog):
- `SkillHotReloader` — polls `skills/*.md`, регистрирует/удаляет скилы при изменении
- `PluginWatcher` — polls `plugins/*/manifest.yaml`
- `MCPWatcher` — polls `config/mcp_servers.yaml`

Все watcher'ы запускаются как фоновые задачи в event loop и корректно останавливаются через `GracefulShutdown`.

---

## Структура проекта

```
corpclaw-lite/
├── src/corpclaw_lite/
│   ├── __init__.py, cli.py, exceptions.py, paths.py, templates.py
│   │
│   ├── agent/          # loop.py, context.py, guards.py, vision.py, subagent.py
│   │                    # factory.py — сборка стека агента (AgentStack)
│   │                    # compressor.py — 3-уровневое сжатие контекста
│   │                    # prompt.py — сборка промптов со скилами
│   │                    # constants.py
│   │
│   ├── calibration/    # loop.py, runner.py, scorer.py, analyzer.py, editor.py
│   │                    # scenarios.py, trajectory.py — авто-калибровка под модель
│   │
│   ├── onboarding/     # engine.py, finalizer.py, questions.py, storage.py
│   │                    # гибридный онбординг: детерминистический + LLM-финализация
│   │
│   ├── llm/            # base.py, anthropic.py, openai.py, xml_tool_calling.py
│   │                    # router.py, presets.py
│   │
│   ├── extensions/
│   │   ├── bootstrap.py # единая инициализация всех расширений
│   │   ├── tools/       # base.py, registry.py, builtin/ (files, excel, exec_script,
│   │   │                 #   image, web, memory, dispatch, send_file, _path_utils)
│   │   ├── skills/      # base.py, loader.py, registry.py, matcher.py, watcher.py
│   │   │                 #   matcher.py — TF-IDF семантический выбор скилов
│   │   │                 #   watcher.py — hot-reload .md файлов
│   │   ├── plugins/     # base.py, loader.py, registry.py, sandbox_proxy.py,
│   │   │                 #   sandbox_worker.py, watcher.py
│   │   ├── subagents/   # base.py, registry.py, builtin/
│   │   └── mcp/         # client.py, manager.py, adapter.py, watcher.py
│   │
│   ├── channels/       # base.py, cli.py, telegram/
│   │   │                 #   telegram/: channel.py, runner.py, orchestrator.py,
│   │   │                 #     formatting.py, upload.py, rate_limit.py, progress.py,
│   │   │                 #     callback_data.py, file_manager.py, admin_notifier.py
│   │
│   ├── security/       # tool_guard.py, network_policy.py
│   │                    #   credential_scrubber.py, ipc_auth.py
│   │
│   ├── container/      # manager.py, ipc.py, policies.py
│   │                    #   proxy.py — IPC-прокси для контейнера
│   │                    #   agent_worker.py — worker внутри контейнера
│   │
│   ├── departments/    # manager.py, permissions.py
│   ├── memory/         # sqlite.py, consolidation.py
│   ├── users/          # models.py, manager.py
│   ├── config/         # settings.py, loader.py, bootstrap.py, interpolation.py
│   ├── runtime/        # shutdown.py — graceful shutdown (SIGINT/SIGTERM)
│   ├── utils/          # db.py
│   └── logging/        # agent_logger.py, health.py (/health endpoint)
│
├── config/
│   ├── settings.yaml                      # Основной конфиг
│   ├── model_presets.yaml                  # Model presets: inference params, thinking config
│   ├── departments.yaml                    # 9 департаментов с RBAC
│   ├── tool_guard_rules.yaml               # ToolGuard правила (CoPaw pattern)
│   ├── network_policy.yaml                 # Network allowlist (NemoClaw pattern)
│   ├── calibration_scenarios.yaml          # 14 сценариев калибровки
│   ├── mcp_servers.yaml                    # MCP-серверы (шаблон)
│   ├── subagents/                          # YAML субагентов (document, execution, filesystem, research)
│   └── bootstrap/                          # Модульные промпты (SOUL.md, COMPANY.md, BEHAVIOR.md)
│       ├── departments/                    # Промпты по департаментам (10 файлов)
│       └── subagents/                      # Промпты субагентов (4 файла)
│
├── skills/             # Markdown-скилы
├── plugins/            # Папки плагинов с manifest.yaml
├── plans/              # Планы разработки (archive/ — завершённые)
├── docker/             # Dockerfile, Dockerfile.agent, seccomp_default.json
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

### Коммиты
- Сообщение коммита на английском, краткое и содержательное
- Всегда добавлять трейлер: `Co-Authored-By: GLM-5.1`

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
- Атрибуты: `name`, `description`, `params`, `execute`, `risk_level`, `parallel_safe`, `terminal`
- `parallel_safe` — `False` для инструментов с race conditions (по умолчанию `True`)
- `terminal` — `True` если инструмент возвращает результат напрямую без LLM re-paraphrase (напр. vision)
- **НЕЛЬЗЯ** добавлять: `api_version`, `deprecated_since`, `removal_in`, `replacement`, `migration_doc`, `provenance`, `compatibility`, `warning_code`
- Проверка типов файлов **на уровне кода** — IMAGE не читается ReadFileTool и наоборот

### Skill
- Атрибуты: `id`, `description`, `allowed_for`, `instructions`, `path`, `version`, `keywords`, `always`
- `keywords` — список ключевых слов/префиксов для semantic selection (напр. `["excel", "нормализ"]`)
- `always` — `True` если скилл всегда инжектируется в промпт независимо от semantic matching
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
| `agent/executor/prompt_loop_executor.py` | ✅ Адаптировано в `agent/loop.py` (509 строк), убраны extensibility зависимости |
| `agent/guards.py` | ✅ `SimpleBudgetGuard` + `SimpleProgressGuard` — переносить как есть |
| `container/manager.py` | ✅ Адаптировано — убрана state machine, добавлена NetworkPolicy |
| `container/ipc.py` | ✅ Адаптировано — HMAC обязателен с nonce |
| `memory/sqlite.py` | ✅ Переносить как есть |
| `channels/telegram*.py` | ✅ Адаптировано под новый Channel Protocol (telegram/ с 10 файлами) |
| `llm/anthropic.py`, `llm/openai.py` | ✅ Адаптировано под новый Provider Protocol |
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
