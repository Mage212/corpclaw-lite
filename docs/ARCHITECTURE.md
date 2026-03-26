# CorpClaw Lite — Архитектура проекта

> Версия документа: 2026-03-27  
> Версия проекта: Phase 1 (MVP) + Hermes Integration

---

## Обзор

**CorpClaw Lite** — корпоративный AI-агент для закрытого контура: Telegram-бот, выполняющий рутинные задачи через инструменты и скиллы, работающий с локальными LLM и управляющий доступом по департаментам.

### Ключевые принципы дизайна

| Принцип | Описание |
|---------|----------|
| **Simple ReAct Loop** | Классический цикл без LLM-планировщиков |
| **Local LLM First** | Оптимизация для Qwen, Mistral, Llama (8K-32K контекст) |
| **Security by Design** | Безопасность встроена в ядро, не добавлена поверх |
| **Manifest-based Extensions** | Skills, plugins, subagents через YAML-манифесты |
| **Fail-Fast** | Ошибка при отсутствии критичных секретов |

---

## Структура проекта

```
corpclaw-lite/
├── src/corpclaw_lite/
│   ├── agent/              # Ядро агента (loop, context, guards, compressor)
│   ├── llm/                # LLM провайдеры (OpenAI, Anthropic, XML fallback)
│   ├── extensions/
│   │   ├── tools/          # Инструменты + registry
│   │   ├── skills/         # Markdown-скиллы
│   │   ├── plugins/        # Плагины с manifest.yaml
│   │   ├── subagents/      # Специализированные субагенты
│   │   └── mcp/            # Model Context Protocol интеграция
│   ├── channels/           # CLI и Telegram каналы
│   ├── security/           # ToolGuard, NetworkPolicy, CredentialScrubber, IPCAuth
│   ├── container/          # Docker-изоляция
│   ├── memory/             # SQLite + консолидация
│   ├── departments/        # RBAC по департаментам
│   ├── users/              # Пользователи + whitelist
│   ├── config/             # Settings, bootstrap prompts
│   └── logging/            # Структурированное логирование
├── config/                 # YAML-конфигурации
├── skills/                 # Markdown-скиллы
├── plugins/                # Директория плагинов
└── tests/                  # Тесты
```

---

## 1. Agent Core

### ReAct Loop (`agent/loop.py`)

Классический цикл Reasoning + Acting:

```
Message → Build Context → LLM Call
  ↓
tool_calls? → Execute → Add Results → Repeat
  ↓
no tool_calls? → Response → Save to Memory
```

**Защиты:**
- `SimpleBudgetGuard` — лимиты итераций, tool calls, времени
- `SimpleProgressGuard` — детекция зацикливания по повторяющимся ошибкам

**Интеграции:**
- Compression (опционально) — ContextCompressor
- Consolidation (опционально) — MemoryConsolidator
- Parallel Tool Execution — для независимых инструментов

### Agent Factory (`agent/factory.py`)

Единая точка сборки всего стека:
- `build_agent_stack()` → `(AgentLoop, UserManager, ToolRegistry)`
- Автовыбор провайдера: `ANTHROPIC_API_KEY` → Anthropic, иначе → OpenAI (Ollama)
- Регистрация всех builtin tools, ToolGuard, PermissionChecker, Memory, Subagents
- Используется CLI (`cli.py`) и Telegram (`runner.py`)

### Context Builder (`agent/context.py`)

Формирует сообщения для LLM:
- System prompt + история + текущее сообщение
- Эвристическая оценка токенов (`len/4`)
- Pruning старых tool results

### Context Compression (`agent/compressor.py`) — NEW

**Проблема:** Локальные LLM имеют ограниченный контекст (8K-32K токенов).

**Решение:** Трёхуровневая компрессия (паттерн Hermes Agent):

| Уровень | Метод | Стоимость | Когда применяется |
|---------|-------|-----------|-------------------|
| 1 | `prune_old_tool_results()` | Бесплатно | Всегда при >10 сообщений |
| 2 | `_sanitize_tool_pairs()` | Бесплатно | После любой компрессии |
| 3 | `_generate_summary()` | LLM-вызов | При превышении threshold |

**Алгоритм:**
1. Защита head (первые 2 сообщения: system + user)
2. Защита tail по токен-бюджету (`protect_tail_tokens`)
3. LLM-суммаризация middle со structured prompt
4. Исправление orphaned tool_call/result пар

**Конфигурация (`CompressionSettings`):**
```yaml
compression:
  enabled: true
  max_context_tokens: 8000
  threshold_ratio: 0.5
  protect_tail_tokens: 3000
  summary_ratio: 0.20
```

### Parallel Tool Execution — NEW

Параллельное выполнение независимых инструментов:

```python
# Условия параллелизации:
# 1. >1 tool call
# 2. Все инструменты имеют parallel_safe=True

results = await asyncio.gather(*[execute_one(tc) for tc in tool_calls])
```

ToolGuard проверки выполняются **внутри** `_execute_single_tool()` для каждого вызова,
поэтому параллельное исполнение безопасно даже с активным ToolGuard.

**Атрибут `parallel_safe`** в Tool:
- `True` (default) — можно выполнять параллельно
- `False` — только последовательно

### Subagent Dispatcher (`agent/subagent.py`)

Делегирование задач специализированным субагентам:
- Изолированный контекст (чистая история)
- Ограниченный набор инструментов
- Специализированный system prompt
- **Снижает нагрузку на контекст основного агента на 60-80%**

### Vision Processor (`agent/vision.py`)

Отдельный LLM-вызов для изображений:
- Кодирование в base64
- Текстовое описание (не изображение в контексте)
- Fallback для text-only провайдеров

---

## 2. LLM Providers

### Protocol Architecture (`llm/base.py`)

**Structural typing** через `typing.Protocol`:
- `Provider` — основной протокол (`chat`, `stream`)
- `VisionProvider` — опциональный (`chat_with_image`)

**Унифицированные модели:**
- `ToolCall(id, name, arguments)`
- `LLMResponse(content, tool_calls, usage)`
- `StreamChunk(content)`

### OpenAI Provider (`llm/openai.py`)

Универсальный провайдер для OpenAI-совместимых API:

| Параметр | Значение |
|----------|----------|
| SDK | `openai.AsyncOpenAI` |
| Base URL | Поддерживается (Ollama, vLLM, LM Studio) |
| Tool Calling | Native + XML Fallback |
| Vision | `image_url` data URI |

### Anthropic Provider (`llm/anthropic.py`)

Провайдер для Claude:
- Native Anthropic tool calling
- Отдельный system prompt параметр
- `max_tokens: 4096` (требование API)

### XML Tool Calling (`llm/xml_tool_calling.py`) — CRITICAL

**Проблема:** Локальные LLM плохо поддерживают native function calling.

**Решение:** Парсинг tool calls из XML-разметки:

```xml
<invoke>
<name>tool_name</name>
<arguments>{"key": "value"}</arguments>
</invoke>
```

**Парсер:**
- Regex-based extraction
- Валидация JSON аргументов
- Проверка имени инструмента в allowed set
- Возврат статуса: `valid`, `malformed_xml`, `invalid_json`, etc.

### Provider Routing (`llm/routing.py`)

YAML-based маршрутизация:

```yaml
llm:
  default: "local"
  named:
    local: {type: openai, model: qwen2.5:14b, base_url: "http://localhost:11434/v1"}
    cloud: {type: anthropic, model: claude-3-5-sonnet}
  routing:
    - task_kind: vision
      provider: cloud
    - subagent_id: code_review
      provider: cloud
```

---

## 3. Extensions System

### Общая архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                         Agent Loop                               │
└─────────────────────────────────────────────────────────────────┘
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
        ┌───────────────┐               ┌───────────────┐
        │  ToolRegistry │               │  SkillRegistry │
        └───────────────┘               └───────────────┘
                │                               │
    ┌───────────┼───────────┐                   │
    ▼           ▼           ▼                   ▼
┌───────┐  ┌──────────┐  ┌────────┐      ┌──────────┐
│Builtin│  │  Plugin  │  │  MCP   │      │  .md     │
│ Tools │  │  Tools   │  │Adapter │      │  Skills  │
└───────┘  └──────────┘  └────────┘      └──────────┘
```

### Tools (`extensions/tools/`)

**Base Class:**
```python
class Tool(ABC):
    name: str
    description: str
    params: list[ToolParam]
    risk_level: RiskLevel     # LOW/MEDIUM/HIGH/CRITICAL
    parallel_safe: bool = True  # NEW
    
    @abstractmethod
    async def execute(self, **kwargs) -> str
```

**Registry:**
- `register()`, `get()`, `list_all()`
- `execute(name, args, user)` — автоматическая инъекция user
- `to_schemas()` — конвертация в OpenAI function schemas

**Builtin Tools:**

| Tool | Risk | Назначение |
|------|------|------------|
| `read_file` | LOW | Чтение файлов |
| `write_file` | MEDIUM | Запись файлов |
| `edit_file` | MEDIUM | Поиск-замена |
| `list_files` | LOW | Листинг директорий |
| `search_files` | LOW | Regex-поиск |
| `exec_script` | HIGH | Shell-команды |
| `web_fetch` | MEDIUM | HTTP-запросы |
| `read_image` | LOW | Vision-анализ |
| `memory_store` | LOW | Сохранение фактов |
| `memory_recall` | LOW | Извлечение фактов |
| `normalize_excel` | MEDIUM | Нормализация Excel |
| `send_file` | MEDIUM | Отправка файла (channel-specific) |
| `dispatch_subagent` | LOW | Делегирование субагенту |

### Skills (`extensions/skills/`)

Markdown-файлы с YAML frontmatter:

```markdown
---
id: my_skill
description: Описание
allowed_for: ["marketing", "sales"]
version: "1.0.0"
---
# Инструкции для агента
...
```

**Features:**
- Hot reload через polling watcher
- Фильтрация по департаментам
- Только данные, без кода

### Plugins (`extensions/plugins/`)

Комплексные расширения:

```
plugins/
└── my_plugin/
    ├── manifest.yaml      # Обязательно
    ├── skill.md           # Опционально
    ├── tool.py            # Опционально
    └── scripts/           # Опционально
```

### Subagents (`extensions/subagents/`)

Специализированные агенты:

```yaml
# filesystem.yaml
id: filesystem-agent
name: "Filesystem Agent"
description: "Expert for filesystem operations"
allowed_tools: [read_file, list_files, search_files]
prompt_path: "config/bootstrap/subagents/filesystem.md"
```

### MCP Integration (`extensions/mcp/`)

Model Context Protocol — внешние инструменты через JSON-RPC:

```yaml
# config/mcp_servers.yaml
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

---

## 4. Security Layer

### Стек безопасности

```
User Message
     │
     ▼
┌─────────────────┐
│  Channel Auth   │  ← Telegram: проверка user_id
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ PermissionCheck │  ← Департамент → доступ к tool
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    ToolGuard    │  ← YAML rules + Smart Approvals
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Container     │  ← NetworkPolicy (deny-by-default)
│  + IPC Auth     │  ← HMAC + Nonce
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Tool Execution  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ CredentialScrub │  ← Маскирование в логах
└─────────────────┘
```

### ToolGuard (`security/tool_guard.py`)

YAML-правила (паттерн CoPaw):

| Severity | Действие |
|----------|----------|
| CRITICAL | Блокировка |
| HIGH + approval | Запрос подтверждения |
| MEDIUM/INFO | Только логирование |

**Smart Approvals (NEW):**
При `approval_mode="smart"` и наличии Provider:
- LLM оценивает реальный риск
- `APPROVE` — авто-одобрение
- `DENY` — блокировка
- `ESCALATE` — запрос человеку

**Примеры правил:**
```yaml
- id: DANGEROUS_RM
  tool: exec_script
  severity: CRITICAL
  pattern: "rm\\s+-rf|rm\\s+/"

- id: PATH_TRAVERSAL_READ
  tool: read_file
  severity: HIGH
  pattern: "\\.\\./"
  require_approval: true
```

### NetworkPolicy (`security/network_policy.py`)

Deny-by-default + allowlist (паттерн NemoClaw):

```yaml
# config/network_policy.yaml
allowlist:
  - "api.anthropic.com"
  - "localhost:11434"
```

### CredentialScrubber (`security/credential_scrubber.py`)

Маскирование секретов в логах:
- `sk-*` — API ключи
- `ghp_*` — GitHub PAT
- `Bearer *` — токены

### IPCAuth (`security/ipc_auth.py`)

HMAC-SHA256 аутентификация для IPC:

| Защита | Механизм |
|--------|----------|
| Replay | UUID nonce + кэш с TTL |
| Tampering | HMAC подпись |
| Timing attack | `compare_digest()` |

**Fail-fast:** Обязательный `CORPCLAW_IPC_SECRET`

---

## 5. Channels

### Channel Protocol (`channels/base.py`)

```python
class Channel(Protocol):
    name: str
    async def start() -> None
    async def stop() -> None
    async def send_message(user, text, **opts) -> None
    async def send_file(user, path, caption) -> None
    async def request_approval(user, action, details) -> bool
```

### CLI Channel (`channels/cli.py`)

Базовый канал для отладки:
- Вывод через `rich.markdown`
- Подтверждение через `input()`

### Telegram Channel (`channels/telegram/`)

Production-ready интеграция:

| Компонент | Назначение |
|-----------|------------|
| `channel.py` | Основной класс (483 LOC) |
| `runner.py` | Entry point |
| `formatting.py` | Markdown → MarkdownV2 |
| `progress.py` | Индикатор выполнения |
| `upload.py` | Безопасная загрузка файлов |
| `file_manager.py` | Интерактивное удаление |
| `rate_limit.py` | Sliding window limiter |
| `admin_notifier.py` | Уведомления администратору |
| `callback_data.py` | Роутинг callback-данных |

**Команды бота:**
- `/start`, `/help`, `/new`
- `/delete` — менеджер файлов
- `/chat` — режим диалога
- `/execute` — режим с инструментами

**Безопасности:**
- Whitelist пользователей
- Rate limiting (10 msg/min)
- Защищённые пути (`.git`, `src/`, `.env`)
- Валидация файлов (extensions, size)

---

## 6. Container System

### Docker Isolation (`container/manager.py`)

Один контейнер на пользователя: `corpclaw_agent_{user_id}`

**Применяемые политики:**

| Политика | Значение |
|----------|----------|
| `mem_limit` | 512m |
| `nano_cpus` | 0.5 × 10⁹ |
| `pids_limit` | 100 |
| `cap_drop` | ALL |
| `security_opt` | seccomp |

### IPC Protocol (`container/ipc.py`)

Transport: `docker exec` + stdio

```
Host → sign(payload) → JSON → Container
Container → verify() → execute → sign() → Host
Host → verify(response) → result
```

### Agent Worker (`container/agent_worker.py`)

Воркер внутри контейнера:
- Ограниченный набор инструментов
- IPCAuth верификация
- Изолированное выполнение

---

## 7. Memory System

### SQLite Backend (`memory/sqlite.py`)

**Таблицы:**
- `messages` — история диалогов
- `memory_facts` — ключ-значение факты

**Features:**
- WAL-режим для конкурентности
- UPSERT для фактов
- Автоматическая десериализация JSON

### Memory Consolidation (`memory/consolidation.py`)

LLM-based сжатие истории:
- Триггер при превышении threshold
- Первая половина → compact summary
- 3-5 bullet points

---

## 8. Configuration & RBAC

### Settings (`config/settings.py`)

Pydantic-модели с поддержкой env vars:

```yaml
llm:
  default: "local"
  named: {...}
  routing: [...]

agent:
  max_steps: 15
  max_tool_calls: 30
  max_wall_time_ms: 120000
  max_history: 20
  consolidation_threshold: 30
  approval_mode: "manual"  # "manual" | "smart" | "off"
  compression:
    enabled: true
    max_context_tokens: 8000

container:
  max_memory: "512m"
  cpus: 0.5
  idle_timeout_seconds: 600
```

### Departments (`departments/`)

```yaml
# config/departments.yaml
marketing:
  name: "Marketing Department"
  allowed_tools: ["*"]
  allowed_skills: ["content-writing", "seo"]
  budget:
    max_iterations: 30
    max_tool_calls: 100
```

### PermissionChecker (`departments/permissions.py`)

Централизованная RBAC логика:
- `can_use_tool(user, tool_name)`
- `can_use_skill(user, skill_id)`
- `can_use_plugin(user, plugin_name)`
- `can_dispatch_subagent(user, subagent_id)`
- `can_use_mcp(user, server_name)`
- `get_budget(user)`

---

## 9. Logging

### Dual Logging

| Лог | Формат | Назначение |
|-----|--------|------------|
| `corpclaw.log` | Текст | DEBUG, человекочитаемый |
| `agent_activity.jsonl` | JSONL | Структурированный, аналитика |

### AgentLogger

```json
{
  "ts": 1234567890.0,
  "user_id": "123",
  "department": "marketing",
  "message_preview": "Нормализуй этот Excel...",
  "duration_ms": 1500.3,
  "tool_count": 3,
  "tools_used": ["read_file", "search_files"],
  "tokens": {"input": 500, "output": 200},
  "status": "ok"
}
```

### Health Endpoint

`GET /health` на порту 8080:
- Uptime, requests, tool_calls, errors

---

## Ключевые метрики

| Компонент | LOC | Файлов |
|-----------|-----|--------|
| Agent Core | ~1130 | 7 |
| LLM Providers | ~480 | 5 |
| Extensions | ~1960 | 20+ |
| Security | ~430 | 4 |
| Channels | ~2080 | 12 |
| Container | ~370 | 4 |
| Memory | ~350 | 2 |
| Config/RBAC | ~590 | 7 |
| Logging | ~140 | 3 |
| **Итого** | **~7800** | **~68** |

---

## Новые фичи (Hermes Integration)

| Фича | Файлы | Назначение |
|------|-------|------------|
| Context Compression | `compressor.py`, `loop.py`, `settings.py` | Управление контекстом для локальных LLM |
| Smart Approvals | `tool_guard.py` | LLM-based оценка рисков |
| Parallel Tool Execution | `base.py`, `loop.py` | Параллельное выполнение независимых инструментов |
| Tool Output Pruning | `context.py` | Дешёвая компрессия старых результатов |

---

## Запуск

```bash
# CLI режим
uv run corpclaw-lite chat

# Telegram бот
uv run corpclaw-lite telegram

# Тесты
uv run pytest tests/ -v

# Линтинг
uv run ruff check src/ --fix && uv run ruff format src/
uv run pyright src/
```
