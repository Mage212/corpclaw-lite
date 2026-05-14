# CorpClaw Lite — Архитектура проекта

> Версия документа: 2026-04-17
> Версия проекта: Phase 7 (Production-Ready) — ~132 модулей, ~21.7K LOC, 932 теста

---

## Обзор

**CorpClaw Lite** — корпоративный AI-агент для закрытого контура: Telegram-бот, выполняющий рутинные задачи через инструменты и скиллы, работающий с локальными LLM и управляющий доступом по департаментам.

### Дополнительные архитектурные документы

| Документ | Назначение |
|----------|------------|
| [`REFERENCE_IDEAS_ANALYSIS.md`](REFERENCE_IDEAS_ANALYSIS.md) | Оценка идей из CoPaw, Gaia, Hermes, NanoBot, NanoClaw, NemoClaw, OpenClaw, ZeroClaw и большого CorpClaw: что внедрять в `corpclaw-lite`, что уже есть, что отложить и почему |

### Ключевые принципы дизайна

| Принцип | Описание |
|---------|----------|
| **Simple ReAct Loop** | Классический цикл без LLM-планировщиков |
| **Local LLM First** | Оптимизация для Qwen, Mistral, Llama (8K-64K контекст) |
| **Security by Design** | Безопасность встроена в ядро, не добавлена поверх |
| **Manifest-based Extensions** | Skills, plugins, subagents через YAML-манифесты |
| **Fail-Fast** | Ошибка при отсутствии критичных секретов |

### LLM Queue, Slot Affinity и KV-cache

Текущая эксплуатационная модель CorpClaw Lite: **один активный рабочий поток на
пользователя**. Поэтому persistent KV-cache scoped по пользователю, агенту, модели,
system prompt и набору tools; полноценный `conversation_id/session_id` пока не
вводится.

Команда `/new` очищает память пользователя и помечает LLM cache как сброшенный:
следующий запрос этого пользователя не восстанавливает L2 cache и очищает live slot
перед новым prompt processing. Если в будущем появятся несколько параллельных потоков
на пользователя (Telegram topics, mission sessions, независимые CLI-сессии), cache
scope нужно расширить реальным `conversation_id` или `session_id`.

---

## Структура проекта

```
corpclaw-lite/
├── src/corpclaw_lite/
│   ├── agent/              # Ядро агента (loop, context, guards, compressor, factory, subagent, vision)
│   ├── calibration/        # Авто-калибровка под локальную модель
│   ├── onboarding/         # Гибридный онбординг пользователей
│   ├── llm/                # LLM провайдеры (OpenAI, Anthropic, XML fallback, presets, router)
│   ├── extensions/
│   │   ├── tools/          # Инструменты + registry (18 builtin) + YAML overrides
│   │   ├── skills/         # Markdown-скиллы + TF-IDF matcher + hot-reload (5s)
│   │   ├── plugins/        # Плагины с manifest.yaml + sandbox worker + hot-reload (10s)
│   │   ├── subagents/      # Специализированные субагенты (5 builtin)
│   │   └── mcp/            # Model Context Protocol интеграция + hot-reload (10s)
│   ├── channels/           # CLI и Telegram каналы (12 Telegram-модулей)
│   ├── security/           # ToolGuard (YAML + Smart), NetworkPolicy, CredentialScrubber, IPCAuth
│   ├── container/          # Docker-изоляция + IPC-прокси + agent worker
│   ├── memory/             # SQLite + консолидация
│   ├── departments/        # RBAC по департаментам (10 департаментов)
│   ├── users/              # Пользователи + whitelist + session revocation
│   ├── config/             # Settings, bootstrap prompts, interpolation, loader
│   ├── runtime/            # Graceful shutdown (SIGINT/SIGTERM)
│   ├── utils/              # DB helpers
│   └── logging/            # Структурированное логирование + health endpoint
├── config/                 # YAML-конфигурации + bootstrap prompts
├── skills/                 # 4 Markdown-скилла с scope-фильтрацией (translator, excel_normalizer, meeting_summary, data_analyst)
├── plugins/                # Директория плагинов
├── docker/                 # Dockerfile, Dockerfile.agent, seccomp_default.json
└── tests/                  # Тесты (932 теста, 95 файлов)
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

**AgentConfig** (dataclass) — группирует все зависимости AgentLoop:
- `provider: Provider` — LLM провайдер/router
- `registry: ToolRegistry` — доступные инструменты
- `settings: AgentSettings` — конфигурация (max_steps=15, max_tool_calls=30, max_wall_time_ms=120000)
- `permission_checker`, `tool_guard`, `memory`, `consolidator`, `compressor`, `approval_callback`

**RunStats** (dataclass) — метрики выполнения:
- `iterations`, `tools_used`, `duration_ms`
- `status`: "ok" | "budget" | "loop" | "timeout" | "error"

**Защиты:**
- `SimpleBudgetGuard` — лимиты итераций, tool calls, времени
- `SimpleProgressGuard` — детекция зацикливания (3 повтора одной ошибки → warning, 2 warnings → break)

**Параллельное выполнение инструментов:**
```python
# Условия: >1 tool call + все parallel_safe=True
results = await asyncio.gather(*[execute_one(tc) for tc in tool_calls])
```

**Terminal tools:** `terminal=True` → single tool call bypasses LLM re-paraphrase (read_image, dispatch_subagent).

**Терминация цикла:**
- Нормальный ответ (no tool calls)
- Budget exceeded
- Loop detected (2x warning)
- Timeout (LLM call)
- Terminal tool success

### Agent Factory (`agent/factory.py`)

`build_agent_stack()` → `AgentStack` — единая точка сборки:

1. **LLM Provider**: Из settings.yaml + model_presets.yaml, fallback к env vars (`ANTHROPIC_API_KEY`, `OPENAI_BASE_URL`)
2. **Tools**: Container mode → `IPCToolProxy` (7 filesystem tools); Dev mode → прямая регистрация
3. **Security**: ToolGuard + PermissionChecker
4. **Extensions**: subagents, host-side tools, skills, plugins
5. **Memory**: SQLiteMemory + MemoryConsolidator + ContextCompressor
6. **System Prompt**: BootstrapLoader из `config/bootstrap/*.md` + calibrated overrides
7. **Few-shots**: Из `config/calibrated/few_shots.yaml`

**AgentStack** содержит: `loop`, `user_manager`, `tool_registry`, `mcp_manager`, `container_manager`, `few_shots`, `subagent_registry`, `skill_registry`, `plugin_registry`, `skill_matcher`.

### Context Builder (`agent/context.py`)

`ContextBuilder.build_initial()` — 4 фазы сборки, критичные для Qwen3.5:

| Фаза | Действие | Зачем |
|------|----------|-------|
| 1 | Extract system messages из history → merge в system_prompt | Qwen3.5 ломается на mid-conversation system messages |
| 2 | Strip leading assistant messages → merge в system_prompt | Qwen3.5 требует user-first |
| 3 | Inject few-shots (calibration) | Примеры "вопрос → tool_call" |
| 4 | Add history (user/assistant only) + current message | Drop orphaned tool messages |

### Context Compression (`agent/compressor.py`)

**Проблема:** Локальные LLM имеют ограниченный контекст (8K-64K токенов).

**Решение:** Трёхуровневая компрессия (паттерн Hermes Agent):

| Уровень | Метод | Стоимость | Когда применяется |
|---------|-------|-----------|-------------------|
| 1 | `prune_old_tool_results()` | Бесплатно | Всегда при >15 сообщениях |
| 2 | `_sanitize_tool_pairs()` | Бесплатно | После любой компрессии |
| 3 | `_generate_summary()` | LLM-вызов | При превышении threshold (80% от max_context_tokens) |

**Алгоритм Level 3:**
1. Защита head (первые 2 сообщения: system + user)
2. Защита tail по токен-бюджету (`protect_tail_tokens=3000`)
3. LLM-суммаризация middle со structured prompt (Goal, Progress, Key Decisions, Files, Next Steps)
4. Замена middle на `[Context Summary]` + summary

**Токен-оценка:** UTF-8 bytes / 4 (ASCII), / 2 (non-ASCII).

**Конфигурация (`CompressionSettings`):**
```yaml
compression:
  enabled: true
  max_context_tokens: 64000
  threshold_ratio: 0.8
  protect_tail_tokens: 3000
  summary_ratio: 0.20
  prune_min_messages: 15
```

### Subagent Dispatcher (`agent/subagent.py`)

Делегирование задач специализированным субагентам:
- Создаёт **изолированный AgentLoop** с filtered ToolRegistry
- Provider resolution: `router.for_subagent(spec.id)` → конкретный провайдер
- System prompt: calibrated override > prompt_path > description fallback
- Skill injection: `build_skill_block()` — те же скилы, что у основного агента
- Timeout: `max_wall_time_ms / 1000` секунд
- **Снижает нагрузку на контекст основного агента на 60-80%**

### Vision Processor (`agent/vision.py`)

Отдельный LLM-вызов для изображений:
- Provider resolution: `router.for_task("vision")` → vision-специфичная модель
- base64-encoding → `VisionProvider.chat_with_image()` или text-only fallback
- Terminal tool: результат возвращается напрямую без LLM re-paraphrase

---

## 2. LLM Providers

### Protocol Architecture (`llm/base.py`)

**Structural typing** через `typing.Protocol`:
- `Provider` — основной протокол (`chat()`, `stream()`)
- `VisionProvider` — опциональный (`chat_with_image()`)

**Унифицированные модели:**
- `ToolCall(id, name, arguments)`
- `LLMResponse(content, reasoning, tool_calls, usage)`
- `StreamChunk(content)`
- `TokenUsage(input_tokens, output_tokens)`

### OpenAI Provider (`llm/openai.py`)

Универсальный провайдер для OpenAI-совместимых API:

| Параметр | Значение |
|----------|----------|
| SDK | `openai.AsyncOpenAI` |
| Base URL | Поддерживается (Ollama, vLLM, LM Studio) |
| Tool Calling | Native + XML Fallback + Repair Loop |
| Vision | `image_url` data URI |
| Presets | Inference params + thinking config через PresetRegistry |

**Двухуровневый парсинг:**
1. Native: `message.tool_calls` из SDK
2. XML Fallback: `parse_xml_tool_call(content)` при пустом native
3. Repair Loop: retry при malformed JSON внутри `<arguments>`

### Anthropic Provider (`llm/anthropic.py`)

Провайдер для Claude:
- Native Anthropic tool calling
- Отдельный system prompt параметр
- `max_tokens: 4096` (требование API)

### XML Tool Calling (`llm/xml_tool_calling.py`) — CRITICAL

**Проблема:** Локальные LLM плохо поддерживают native function calling.

**Решение:** Парсинг tool calls из XML-разметки:

```xml
<invoke><name>tool_name</name><arguments>{"key": "value"}</arguments></invoke>
```

**Парсер:**
- Regex-based extraction
- Валидация JSON аргументов
- Проверка имени инструмента в allowed set
- Возврат статуса: `valid`, `malformed_xml`, `invalid_json`, etc.

### Provider Routing (`llm/router.py`)

YAML-based маршрутизация:

```yaml
llm:
  default: "default"
  named:
    default: {type: openai, model: qwen3.5-4b, base_url: "http://localhost:11434/v1"}
    vision: {type: openai, model: qwen3.5-4b, base_url: "http://localhost:11434/v1"}
    cloud: {type: anthropic, model: claude-sonnet-4-20250514}
  routing:
    - task_kind: vision
      provider: vision
    - task_kind: consolidate
      provider: default
    - subagent_id: code_review
      provider: cloud
```

`LLMRouter` методы: `for_task(task_kind)`, `for_subagent(subagent_id)`.

### Model Presets (`llm/presets.py`)

**ThinkingConfig**: `open_tag`, `close_tag`, `budget_tokens`, `source` ("content" для парсинга тегов, "native" для `reasoning_content`).

**Доступные пресеты:**

| Preset | Модель | Thinking | Params |
|--------|--------|----------|--------|
| `qwen3.5-thinking` | Qwen 3.5 | native reasoning_content | temp=0.7, top_p=0.95, top_k=20 |
| `gemma4-thinking` | Gemma 4 | `<\|think\|>` tags | defaults |
| `gemma4-fast` | Gemma 4 | none | defaults |

**Приоритет:** `request-level > preset > provider defaults`

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
│ Tools │  │ Sandbox  │  │Adapter │      │  Skills  │
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
    parallel_safe: bool = True
    terminal: bool = False    # Skip LLM re-paraphrase

    @abstractmethod
    async def execute(self, **kwargs) -> str
```

**Registry:**
- `register()`, `get()`, `unregister()`, `list_all()`
- `execute(name, args, user)` — credential scrubbing автоматически
- `load_overrides(path)` — YAML description overrides (калибровка)
- `to_schemas()` / `to_schemas_for_user()` — OpenAI function schemas

**Builtin Tools (18):**

| Tool | Risk | Назначение |
|------|------|------------|
| `read_file` | LOW | Чтение файлов (path traversal protection) |
| `write_file` | MEDIUM | Запись файлов (auto-create parent dirs) |
| `edit_file` | MEDIUM | Поиск-замена (exact match, max_replacements) |
| `list_files` | LOW | Листинг директорий с метаданными |
| `search_files` | LOW | Regex-поиск (skip .git/node_modules) |
| `exec_script` | HIGH | Shell-команды (timeout 30s/120s max, 50KB truncation) |
| `web_fetch` | MEDIUM | HTTP-запросы (SSRF protection, 1MB limit) |
| `read_image` | LOW | Vision-анализ (terminal=True, separate LLM call) |
| `memory_store` | LOW | Сохранение per-user фактов в SQLite |
| `memory_recall` | LOW | Поиск per-user фактов в SQLite |
| `normalize_excel` | MEDIUM | Нормализация Excel (INN, dates, invisible chars) |
| `send_file` | MEDIUM | Отправка файла (20MB limit) |
| `dispatch_subagent` | LOW | Делегирование субагенту (terminal=True) |
| `diff_text` | LOW | Сравнение текстов/файлов (unified/words/chars) |
| `table_query` | MEDIUM | SQL-запросы к CSV/XLSX/JSON через DuckDB |
| `chart_generate` | MEDIUM | Графики (bar, line, pie, scatter, histogram) |
| `convert_format` | MEDIUM | Конвертация CSV ↔ XLSX ↔ JSON ↔ Markdown |
| `pdf_reader` | LOW | Извлечение текста из PDF (page ranges) |

### Skills (`extensions/skills/`)

Markdown-файлы с YAML frontmatter:

```markdown
---
id: my_skill
description: Описание
allowed_for: ["marketing", "sales"]
version: "1.0.0"
keywords: ["excel", "нормализ"]
always: false
---
# Инструкции для агента
```

**TF-IDF Matcher (`skills/matcher.py`):**
- Bilingual stop-words (188: 108 RU + 80 EN)
- Cosine similarity между query и skill TF-IDF vectors
- Keyword boost: prefix match ("нормализ" → "нормализуй")
- `top_k=3`, `tfidf_threshold=0.08`, `keyword_boost=0.5`
- Skills с `always=True` — безусловная инъекция

**Hot Reload (`skills/watcher.py`):**
- Polling 5 секунд
- Track mtime per file
- Detect: new, modified, deleted

**Загруженные скилы (4):**

| Skill | Scope | Назначение |
|-------|-------|------------|
| `translator` | main | Перевод текстов |
| `excel_normalizer` | document-agent | Нормализация Excel |
| `meeting_summary` | document-agent | Итоги встреч |
| `data_analyst` | data-agent | Анализ данных, SQL, графики |

### Plugins (`extensions/plugins/`)

Комплексные расширения с subprocess sandbox:

```
plugins/
└── my_plugin/
    ├── manifest.yaml      # Обязательно
    ├── skill.md           # Опционально
    ├── tool.py            # Опционально (subprocess isolation)
    └── scripts/           # Опционально
```

**Sandbox Architecture:**
```
PluginToolProxy (host)
  ←→ JSON-RPC over stdin/stdout
  ←→ sandbox_worker.py (subprocess)
```

- Lazy subprocess spawning (on first execute)
- asyncio.Lock для serialization
- 30s timeout per execution
- Introspection: `--introspect` → tool schema as JSON
- Cleanup on hot-reload

**manifest.yaml:**
```yaml
name: my_plugin
version: "1.0.0"
type: plugin
description: "Does something"
allowed_departments: ["*"]
components:
  skill: skill.md
  tool: tool.py
```

### Subagents (`extensions/subagents/`)

5 builtin субагентов:

| ID | Tools | Prompt |
|----|-------|--------|
| `filesystem-agent` | read_file, list_files, search_files, write_file, edit_file | `config/bootstrap/subagents/filesystem.md` |
| `document-agent` | read_file, write_file, edit_file, normalize_excel, list_files | `config/bootstrap/subagents/document.md` |
| `execution-agent` | exec_script, write_file, read_file | `config/bootstrap/subagents/execution.md` |
| `research-agent` | web_fetch, read_file, search_files, list_files, memory_store, memory_recall | `config/bootstrap/subagents/research.md` |
| `data-agent` | table_query, chart_generate, convert_format, pdf_reader, diff_text, read/write_file, list_files, search_files, send_file | `config/bootstrap/subagents/data-agent.md` |

### MCP Integration (`extensions/mcp/`)

Model Context Protocol — внешние инструменты через JSON-RPC:

```yaml
# config/mcp_servers.yaml
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

**Компоненты:**
- `MCPClient` — stdio JSON-RPC, initialize → list_tools → call_tool
- `MCPManager` — загрузка конфига, env interpolation, connect/disconnect/reconnect
- `MCPToolAdapter` — адаптация MCP tool → внутренний Tool (risk_level=MEDIUM)
- `MCPWatcher` — hot-reload (10s polling), diff old vs new servers

---

## 4. Security Layer

### Стек безопасности

```
User Message
     │
     ▼
┌─────────────────┐
│  Channel Auth   │  ← Telegram: whitelist + session check
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ PermissionCheck │  ← Департамент → доступ к tool/skill/plugin/subagent/mcp
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    ToolGuard    │  ← 20+ YAML rules + Smart Approvals (LLM-based)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Container     │  ← NetworkPolicy (deny-by-default)
│  + IPC Auth     │  ← HMAC-SHA256 + Nonce (300s TTL)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Tool Execution  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ CredentialScrub │  ← Маскирование в результатах и логах
└─────────────────┘
```

### ToolGuard (`security/tool_guard.py`)

**Правила:** 20+ YAML правил с regex patterns на tool arguments.

| Severity | Действие |
|----------|----------|
| CRITICAL | Авто-блокировка |
| HIGH + approval | Запрос подтверждения |
| MEDIUM + approval | Запрос подтверждения |
| INFO | Только логирование |

**Категории правил:**
- DANGEROUS_RM, DANGEROUS_SHUTIL_RMTREE — rm -rf, shutil.rmtree
- PATH_TRAVERSAL — `../` в путях
- SECRET_DETECTION — API keys в параметрах
- DANGEROUS_PIPE_TO_SHELL — curl|sh patterns
- ABS_PATH_PROTECTION — /etc, /proc, /sys
- WEB_FETCH_PRIVATE_IP, WEB_FETCH_SENSITIVE_PATHS — SSRF protection
- CHMOD_777, DANGEROUS_DD — permissions, disk ops

**Smart Approvals:** при `approval_mode="smart"` и наличии Provider:
- LLM оценивает реальный риск команды
- `APPROVE` — авто-одобрение
- `DENY` — блокировка
- `ESCALATE` — запрос человеку через channel

### NetworkPolicy (`security/network_policy.py`)

Deny-by-default + allowlist (паттерн NemoClaw):

```yaml
allowlist:
  - "api.anthropic.com"
  - "localhost:11434"
  - "api.github.com"
```

### CredentialScrubber (`security/credential_scrubber.py`)

Маскирование секретов в логах и результатах:
- `sk-*` — OpenAI/Anthropic API ключи
- `ghp_*` — GitHub PAT
- `Bearer *` — токены
- `CORPCLAW_IPC_SECRET` — динамически из env
- Работает как `logging.Filter` + функция `scrub_text()` для tool results

### IPCAuth (`security/ipc_auth.py`)

HMAC-SHA256 аутентификация для IPC:

| Защита | Механизм |
|--------|----------|
| Replay | UUID nonce + кэш с TTL (300s) |
| Tampering | HMAC подпись |
| Timing attack | `compare_digest()` |
| Secret | `CORPCLAW_IPC_SECRET` (min 16 chars) |

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
| `channel.py` | Основной класс (483 LOC), deduplication, inline keyboard approval |
| `runner.py` | Entry point |
| `orchestrator.py` | Полный lifecycle: access control, rate limiting, hot-reloaders, onboarding |
| `formatting.py` | Markdown → MarkdownV2, таблицы → card format |
| `upload.py` | Безопасная загрузка файлов (extension whitelist, path validation) |
| `progress.py` | StatusMessageSession — throttled updates, typing heartbeat |
| `file_manager.py` | DeleteBrowserHandler — интерактивное удаление, pagination, confirmation |
| `rate_limit.py` | Sliding window limiter (10 msg/min) |
| `admin_notifier.py` | Broadcast сообщений администраторам |
| `callback_data.py` | Роутинг callback-данных |

**Режимы:** `/chat` (чистый диалог), `/execute` (с инструментами)

**Команды бота:** `/start`, `/help`, `/new`, `/delete`, `/chat`, `/execute`

---

## 6. Container System

### Docker Isolation (`container/manager.py`)

Один контейнер на пользователя: `corpclaw_agent_{user_id}`

**Docker image:** `corpclaw-agent-base:latest` (python:3.12-slim, non-root uid 1001)

**Применяемые политики:**

| Политика | Значение |
|----------|----------|
| `mem_limit` | 512m |
| `nano_cpus` | 0.5 × 10⁹ |
| `pids_limit` | 100 |
| `cap_drop` | ALL |
| `security_opt` | seccomp (100+ allowed syscalls) |
| `network_mode` | none (deny-by-default) |
| `read_only` | True (except /workspace, /tmp) |
| `workspace` | bind-mount host workspace_base |

**Dev mode:** `container.enabled=false` → инструменты выполняются на хосте.

### IPC Protocol (`container/ipc.py`)

Transport: stateless `docker exec` + stdio

```
Host → sign(payload) → JSON → Container
Container → verify() → execute → sign() → Host
Host → verify(response) → result
```

**Dual timeout:** IPC timeout (host-side) + tool timeout (container-side).

### Agent Worker (`container/agent_worker.py`)

Воркер внутри контейнера:
- Читает signed IPC request из stdin
- Verifies HMAC signature
- Выполняет tool через container-specific ToolRegistry
- Clears CORPCLAW_IPC_SECRET из env после верификации
- Signs response и пишет в stdout

---

## 7. Memory System

### SQLite Backend (`memory/sqlite.py`)

**Таблицы:**
- `messages` — история диалогов (user, assistant, tool, reasoning)
- `memory_facts` — key-value факты (per-user)

**Features:**
- Async API с thread pool delegation
- WAL-режим для конкурентности
- Автоматическая schema migration
- JSON десериализация

### Memory Consolidation (`memory/consolidation.py`)

LLM-based сжатие истории:
- Триггер при превышении threshold (50 сообщений по умолчанию)
- Первая половина → compact summary (3-5 bullet points)
- Cooldown (предотвращает повторную консолидацию)
- Safety guardrails: не консолидирует active workflows

---

## 8. Configuration & RBAC

### Settings (`config/settings.py`)

Pydantic-модели с поддержкой env vars:

```yaml
llm:
  default: "default"
  named: {...}
  routing: [...]

agent:
  max_steps: 15
  max_tool_calls: 30
  max_wall_time_ms: 120000
  max_history: 20
  approval_mode: "manual"  # "manual" | "smart" | "off"
  compression:
    enabled: true
    max_context_tokens: 64000
    threshold_ratio: 0.8

container:
  enabled: true
  max_memory: "512m"
  cpus: 0.5
  idle_timeout_seconds: 600

skills:
  selection_mode: "semantic"
  top_k: 3
  tfidf_threshold: 0.08
  keyword_boost: 0.5
```

### Settings Loader (`config/loader.py`)

- Environment variable interpolation: `${VAR:-default}`
- Calibrated overrides: `config/calibrated/settings_override.yaml`
- Empty string cleaning для optional fields

### Bootstrap Prompts (`config/bootstrap.py`)

`BootstrapLoader` — модульные системные промпты:
- `get_system_prompt()` — SOUL.md + COMPANY.md + BEHAVIOR.md
- `get_department_prompt(dept)` — department-specific prompt
- `get_user_prompt(telegram_id)` — personalized prompt
- Hot-reload через mtime caching
- Calibrated override: `config/calibrated/bootstrap/*.md`

### Departments (`departments/`)

10 департаментов с RBAC:

| Департамент | Tools | Budget (iter/tools/time) |
|-------------|-------|--------------------------|
| default | read, list, search, memory, normalize_excel, read_image, dispatch | 10/20/60s |
| engineering | * (all) | 20/50/120s |
| development | * (all) | 20/50/120s |
| it | file ops + memory + read_image + dispatch | 15/30/90s |
| marketing | content + web_fetch + normalize_excel + send_file | 10/20/60s |
| finance | data + normalize_excel + send_file | 10/20/60s |
| hr | docs + normalize_excel + send_file + read_image | 10/20/60s |
| analytics | data + web_fetch + normalize_excel | 15/30/90s |
| product | research + web_fetch + read_image + dispatch | 10/20/60s |
| admin | * (all) | 25/60/180s |

### PermissionChecker (`departments/permissions.py`)

Централизованная RBAC логика:
- `can_use_tool(user, tool_name)`
- `can_use_skill(user, skill_id)`
- `can_use_plugin(user, plugin_name)`
- `can_dispatch_subagent(user, subagent_id)`
- `can_use_mcp(user, server_name)`
- `get_budget(user)` → `SimpleBudgetGuardConfig`
- Wildcard support: `*` = all

---

## 9. Logging & Health

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

`GET /health` на порту 8080 (aiohttp):
- Uptime, requests, tool_calls, errors

---

## 10. Calibration Phase

### Назначение

Одноразовый (или периодический) этап: облачная модель анализирует, как локальная модель справляется с типовыми сценариями, и автоматически правит конфигурации. После калибровки — **только локальная модель**.

### Ключевые модули (`calibration/`, 1,498 LOC)

| Класс | Назначение |
|-------|------------|
| `CalibrationLoop` | Оркестратор калибровочного цикла |
| `ScenarioRunner` | Запуск сценариев из YAML (20+ сценариев) |
| `CalibrationScorer` | Оценка: tool accuracy, response quality |
| `ConfigEditor` | Правка Edit Surfaces (YAML/Markdown) |
| `TrajectoryRecorder` | Запись траекторий для анализа |
| `CalibrationAnalyzer` | Анализ результатов и рекомендации |

### Edit Surfaces

Калибратор правит **только** YAML/Markdown конфигурации, не Python-код:

| Слой | Что калибруется | Файлы |
|------|----------------|-------|
| 1 | System Prompt | `config/bootstrap/*.md` |
| 2 | Tool Descriptions | YAML-override через `ToolRegistry` |
| 3 | Skill Instructions | `skills/*.md` |
| 4 | Few-shot Examples | `config/calibrated/few_shots.yaml` |

### Сценарии калибровки (20+)

| Категория | Примеры |
|-----------|---------|
| `tool_use` | read_file, write_file, list_files, search_files, edit_file |
| `no_tool` | math, greeting, factual, translation |
| `multi_step` | list_then_read, read_and_edit |
| `error_recovery` | handling missing files |
| `subagent_dispatch` | delegation testing |
| `skill_selection` | semantic skill injection verification |

**CLI:** `uv run corpclaw-lite calibrate [options]`

---

## 11. User Onboarding

### Назначение

Гибридный онбординг: детерминистический движок вопросов + LLM-финализация профиля.

### Ключевые модули (`onboarding/`, 614 LOC)

| Класс | Назначение |
|-------|------------|
| `OnboardingEngine` | Конечный автомат: вопросы → ответы → финализация |
| `OnboardingFinalizer` | LLM-вызов для формирования персонализированного профиля |
| `OnboardingQuestions` | Каталог вопросов (роль, задачи, стиль общения) |
| `OnboardingStorage` | SQLite хранилище ответов и состояния |

### Запуск

- CLI: `uv run corpclaw-lite chat --setup`
- Telegram: команда `/setup`

### Результат

Департамент + персонализированный system prompt → сохраняется в user profile.

---

## 12. Hot Reload & Watchers

### Три watcher'а

| Watcher | Что отслеживает | Интервал | Файл |
|---------|----------------|----------|------|
| `SkillHotReloader` | `skills/*.md` | 5s | `extensions/skills/watcher.py` |
| `PluginWatcher` | `plugins/*/manifest.yaml` | 10s | `extensions/plugins/watcher.py` |
| `MCPWatcher` | `config/mcp_servers.yaml` | 10s | `extensions/mcp/watcher.py` |

### Принцип работы

- Polling-based (не inotify/watchdog) — максимальная совместимость
- Запускаются как фоновые задачи в event loop
- Корректно останавливаются через `GracefulShutdown`
- Detect: создание, изменение, удаление
- Plugin watcher: unregister old tools (kills subprocesses) → reload → re-register

---

## Ключевые метрики

| Компонент | LOC | Файлов |
|-----------|-----|--------|
| Agent Core | ~1,901 | 10 |
| Calibration | ~1,522 | 8 |
| Onboarding | ~614 | 5 |
| LLM Providers | ~1,195 | 7 |
| Extensions | ~2,558 | 43 |
| Security | ~506 | 5 |
| Channels | ~5,037 | 14 |
| Container | ~806 | 6 |
| Memory | ~477 | 3 |
| Config + RBAC | ~511 | 5 |
| Departments | ~130 | 3 |
| Users | ~287 | 3 |
| Runtime | ~47 | 2 |
| Logging | ~178 | 3 |
| Root (cli, etc.) | ~802 | 4 |
| **Исходники** | **~21,712** | **~132** |
| **Тесты** | **~18,507** | **~95** |
| **Тест-кейсов pytest** | **932** | |

---

## Запуск

```bash
# CLI режим
uv run corpclaw-lite chat
uv run corpclaw-lite chat --setup            # Онбординг

# Telegram бот
uv run corpclaw-lite telegram

# Калибровка
uv run corpclaw-lite calibrate [options]

# Управление пользователями
uv run corpclaw-lite user-list
uv run corpclaw-lite user-create -t <tg_id> -d <department>
uv run corpclaw-lite user-allow -t <tg_id> -d <department>
uv run corpclaw-lite user-deny -t <tg_id>
uv run corpclaw-lite user-revoke -t <tg_id>

# Расширения
uv run corpclaw-lite skill list
uv run corpclaw-lite plugin list
uv run corpclaw-lite generate skill <name>
uv run corpclaw-lite generate plugin <name>
uv run corpclaw-lite generate subagent <name>

# Docker
uv run corpclaw-lite containers
uv run corpclaw-lite prune

# Тесты
uv run pytest tests/ -v

# Линтинг
uv run ruff check src/ --fix && uv run ruff format src/
uv run pyright src/
```
