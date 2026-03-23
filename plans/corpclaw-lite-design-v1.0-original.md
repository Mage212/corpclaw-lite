# CorpClaw-Lite: Дизайн-документ v1.0

**Дата:** 22 марта 2026  
**Статус:** APPROVED — основание для разработки  
**Задача:** Переписать CorpClaw с нуля, сохранив успешные решения и устранив архитектурные ошибки

---

## 0. Философия проекта

**Главный принцип:** Минимум кода для максимума ценности.

**Мы строим:** Надёжный Python AI-агент для корпоративного закрытого контура — Telegram-бот, который умеет выполнять рутинные задачи через скиллы/плагины/субагенты, работает с локальными LLM и управляет доступом по департаментам.

**Мы НЕ строим:** Enterprise middleware, marketplace расширений, платформу для внешних разработчиков, конкурента OpenClaw по функциональности.

**Метрика успеха:** Маркетолог говорит «нормализуй Excel» в Telegram → агент выполняет через локальную Qwen-7B за 10 секунд, без доступа к exec, без утечки данных. Всё остальное — вторично.

---

## 1. Ключевые архитектурные решения

### 1.1 Агентная цепочка: Simple ReAct Loop

Используем **классический ReAct-цикл** без LLM-based планирования и верификации:

```
Сообщение пользователя
    → Сборка контекста (история + факты + скилы + инструменты)
    → LLM вызов
    → Есть tool_calls? → Выполнить инструменты → добавить результаты в контекст → повторить
    → Нет tool_calls? → Финальный ответ → сохранить в память
```

**Почему:** LLM-based TaskPlanner/TaskVerifier из CorpClaw v1 добавлял 3x задержку и тратил 2 дополнительных вызова к локальной модели. Ни OpenClaw, ни CoPaw, ни NemoClaw не использует LLM-based planning.

**Бюджет:** `SimpleBudgetGuard` (max_iterations, max_tool_calls, max_wall_time_ms) + `SimpleProgressGuard` (детекция зацикливания на той же ошибке инструмента) — сохраняются из v1.

### 1.2 Субагенты: Изолированные Исполнители

Субагенты — ключевая особенность для работы с локальными LLM. Схема работы:

```
Основной агент         Субагент
     │                    │
     │ (видит запрос)     │
     │ → dispatch(task)   │
     │ ─────────────────► │ (получает ПОЛНЫЙ контекст задачи)
     │                    │ (чистая история = только эта задача)
     │                    │ (доступ к инструментам + скилам отдела)
     │                    │ ← результат работы (компактный)
     │ (обогащается только│
     │  результатом)      │
```

**Почему это важно для локальных LLM:**
- Локальные модели (Qwen-7B, Mistral) плохо держат длинный контекст
- Субагент стартует с чистой историей — только задача и контекст от основного агента
- Основной агент получает только компактный результат, не весь trace выполнения
- Это снижает давление на контекстное окно основного агента на 60-80%

**Основной агент знает:**
- `list_files`, `read_file`, `search_files` — инструменты для навигации
- Каталог доступных субагентов и их способностей (через системный промпт)
- При задаче → dispatch нужного субагента с детальным контекстом

**Субагент знает:**
- Свои специализированные инструменты (write_file, edit_file, web_search, exec_script и т.д.)
- Свои скилы (content_writer, code_reviewer и т.д.)
- Только текущую задачу от основного агента (нет истории пользователя)

### 1.3 Обработка изображений: отдельный LLM-вызов

Инструмент `read_image` **не возвращает изображение напрямую в контекст агента**. Он:
1. Принимает путь к файлу
2. Делает **отдельный вызов к vision-провайдеру** с изображением и промптом
3. Возвращает **текстовое описание/анализ** от vision-модели в контекст агента

Это критическое решение для локальных LLM, большинство которых не поддерживают multimodal-режим в нативном tool calling.

### 1.4 Безопасность: Security-first, не Security-added

В отличие от v1, где безопасность наслаивалась поверх архитектуры, в Lite она встроена в ядро:

```
Запрос пользователя
    → ChannelAuth (whitelist, RBAC)
    → ToolGuard (YAML rules, перехват опасных параметров)
    → PermissionCheck (department access)
    → Container (isolated execution, network policy)
    → Credential Scrubber (в логах и ответах)
```

**ToolGuard** (по образцу CoPaw): YAML-правила, severity levels, inline approval для HIGH/CRITICAL.  
**NetworkPolicy** (по образцу NemoClaw): allowlist для исходящих соединений из контейнеров.  
**Mandatory IPC Auth**: HMAC с nonce (replay protection) — обязателен, не опционален.

---

## 2. Структура проекта

```
corpclaw-lite/
├── src/corpclaw_lite/
│   │
│   ├── agent/
│   │   ├── loop.py              # AgentLoop — ReAct цикл (~400 строк)
│   │   ├── context.py           # Сборка контекста (история + факты + скилы)
│   │   ├── guards.py            # SimpleBudgetGuard + SimpleProgressGuard
│   │   ├── vision.py            # VisionProcessor — отдельный LLM-вызов для изображений
│   │   ├── subagent.py          # SubagentDispatcher — вызов субагентов
│   │   └── prompts.py           # Системные промпты (шаблоны)
│   │
│   ├── llm/
│   │   ├── base.py              # Protocol Provider, LLMResponse, StreamChunk (~100 строк)
│   │   ├── anthropic.py         # AnthropicProvider (~300 строк)
│   │   ├── openai.py            # OpenAIProvider + Ollama/vLLM support (~350 строк)
│   │   ├── xml_tool_calling.py  # XML fallback для локальных LLM (~200 строк) ← ПЕРЕНЕСТИ из v1
│   │   └── routing.py           # ProviderRouter — модель по задаче (~150 строк)
│   │
│   ├── extensions/
│   │   ├── base.py              # Базовые протоколы расширений (Manifest, Loader, Registry)
│   │   ├── registry.py          # UnifiedExtensionRegistry — единый реестр
│   │   ├── watcher.py           # HotReload watcher для всех типов расширений
│   │   │
│   │   ├── tools/
│   │   │   ├── base.py          # Tool protocol (name, description, params, execute, risk_level)
│   │   │   ├── registry.py      # ToolRegistry
│   │   │   ├── guard.py         # ToolGuard — YAML rules, severity, approval
│   │   │   └── builtin/         # Встроенные инструменты
│   │   │       ├── files.py     # read_file, write_file, edit_file, list_files, search_files
│   │   │       ├── web.py       # web_fetch, web_search
│   │   │       ├── memory.py    # memory_store, memory_recall
│   │   │       ├── image.py     # read_image (vision via separate LLM call)
│   │   │       ├── send_file.py # Отправка файла пользователю через канал
│   │   │       └── profile.py   # user_profile
│   │   │
│   │   ├── skills/
│   │   │   ├── base.py          # Skill dataclass (id, description, allowed_for, instructions, path)
│   │   │   ├── loader.py        # Markdown loader
│   │   │   └── registry.py      # SkillRegistry
│   │   │
│   │   ├── plugins/
│   │   │   ├── base.py          # Plugin = Skill + Tool + Script манифест
│   │   │   ├── loader.py        # Plugin loader (читает manifest.yaml)
│   │   │   └── registry.py      # PluginRegistry
│   │   │
│   │   ├── subagents/
│   │   │   ├── base.py          # SubagentSpec (id, name, description, capabilities, allowed_tools)
│   │   │   ├── registry.py      # SubagentRegistry
│   │   │   └── builtin/         # Встроенные субагенты
│   │   │       ├── filesystem.yaml
│   │   │       ├── research.yaml
│   │   │       ├── document.yaml
│   │   │       └── execution.yaml
│   │   │
│   │   └── mcp/
│   │       ├── client.py        # MCP client (stdio + HTTP)
│   │       ├── manager.py       # MCPManager
│   │       └── adapter.py       # MCP Tool → Extension Tool адаптер
│   │
│   ├── channels/
│   │   ├── base.py              # Channel Protocol + ChannelManifest
│   │   ├── registry.py          # ChannelRegistry
│   │   ├── cli.py               # CLIChannel — базовый канал (stdin/stdout)
│   │   └── telegram/
│   │       ├── manifest.yaml    # Manifest: "telegram" channel
│   │       ├── channel.py       # TelegramChannel
│   │       ├── router.py        # MessageRouter (whitelist, user lookup)
│   │       ├── formatter.py     # MarkdownV2 / HTML форматирование
│   │       └── approval_ui.py   # Inline-кнопки (Approve/Deny)
│   │
│   ├── security/
│   │   ├── tool_guard.py        # YAML-rule engine (CoPaw pattern)
│   │   ├── network_policy.py    # Container network allowlist (NemoClaw pattern)
│   │   ├── credential_scrubber.py # Маскирование ключей в логах
│   │   └── ipc_auth.py          # HMAC + nonce (replay protection), mandatory
│   │
│   ├── container/
│   │   ├── manager.py           # Docker lifecycle, resource limits (~500 строк)
│   │   ├── ipc.py               # stdio IPC с контейнером
│   │   └── policies.py          # Seccomp + NetworkPolicy → Docker args
│   │
│   ├── departments/
│   │   ├── manager.py           # Загрузка departments.yaml
│   │   └── permissions.py       # check(user, resource_type, resource_id) → bool
│   │
│   ├── memory/
│   │   ├── sqlite.py            # SQLite backend
│   │   ├── manager.py           # MemoryManager (история + факты)
│   │   ├── consolidation.py     # LLM-based consolidation (simple, optional)
│   │   └── vector/              # Опциональный vector store (Qdrant)
│   │       ├── base.py          # VectorStore Protocol
│   │       └── qdrant.py        # QdrantBackend (if enabled)
│   │
│   ├── users/
│   │   ├── models.py            # User dataclass
│   │   └── manager.py           # UserManager (SQLite)
│   │
│   ├── db/
│   │   └── database.py          # Async SQLite connection
│   │
│   ├── config/
│   │   ├── settings.py          # Pydantic Settings (~200 строк)
│   │   └── loader.py            # YAML + env var expansion
│   │
│   ├── logging/
│   │   ├── agent_logger.py      # Structured agent activity log
│   │   ├── scrubber.py          # Log filter (credential scrubbing)
│   │   └── rotation.py          # RotatingFileHandler setup
│   │
│   └── main.py                  # Typer CLI (~300 строк)
│
├── config/
│   ├── settings.yaml            # Основные настройки
│   ├── departments.yaml         # RBAC конфигурация
│   ├── mcp_servers.yaml         # MCP серверы
│   ├── tool_guard_rules.yaml    # ToolGuard правила (CoPaw pattern)
│   ├── network_policy.yaml      # Network allowlist (NemoClaw pattern)
│   └── bootstrap/               # Модульные инструкции агента
│       ├── SOUL.md              # Личность агента
│       ├── COMPANY.md           # О компании
│       ├── BEHAVIOR.md          # Правила поведения
│       ├── departments/         # Инструкции по департаментам
│       │   ├── marketing.md
│       │   ├── development.md
│       │   └── admin.md
│       ├── subagents/           # Промпты субагентов
│       │   ├── filesystem.md
│       │   ├── research.md
│       │   ├── document.md
│       │   └── execution.md
│       └── users/               # Пользовательские настройки (hotreload)
│           └── {user_id}.md
│
├── skills/                      # Стандартные скилы (markdown)
│   ├── translator.md
│   ├── content_writer.md
│   ├── code_reviewer.md
│   ├── excel_normalizer.md
│   └── doc_writer.md
│
├── plugins/                     # Плагины (каждый — отдельная папка)
│   └── example_plugin/
│       ├── manifest.yaml        # Название, скил, описание инструментов
│       ├── skill.md             # Инструкции как использовать этот плагин
│       ├── tool.py              # Дополнительный Python инструмент (опционально)
│       └── script.sh            # Заготовленный скрипт (опционально)
│
├── docker/
│   ├── Dockerfile.agent         # Образ для пользовательских контейнеров
│   └── seccomp_default.json     # Seccomp профиль
│
├── migrations/                  # Alembic миграции БД
├── tests/
├── pyproject.toml
├── AGENTS.md
└── README.md
```

---

## 3. Единая система расширений

### 3.1 Принцип: Unified Extension Model

Все расширения (tools, skills, plugins, subagents, mcp, channels) строятся по **единой модели через манифест**:

```yaml
# Манифест расширения (пример для плагина)
name: excel_normalizer
version: "1.0.0"
type: plugin                 # tool | skill | plugin | subagent | channel

# Метаданные
description: "Нормализует Excel файлы в стандартный формат"
author: "CorpClaw Team"

# Доступность
allowed_departments: [marketing, development, admin]

# Компоненты плагина
skill: skill.md              # Инструкции для агента
tool: tool.py                # Дополнительный инструмент (опц.)
script: normalize.py         # Готовый скрипт (опц.)

# Зависимости
requires:
  packages: [openpyxl]
  tools: [read_file, write_file]
```

**Единый Registry:** `UnifiedExtensionRegistry` — централизованный каталог, который знает о всех расширениях. Загружен один раз при старте, обновляется через HotReload.

### 3.2 HotReload для всех расширений

Единый `ExtensionWatcher` через `watchdog` отслеживает изменения в:
- `skills/` — перезагружает Skill dataclass
- `plugins/` — перезагружает манифест и инструмент
- `config/bootstrap/` — инвалидирует кэш системных промптов
- `channels/` — уведомляет ChannelRegistry (не перезапускает сервер)

**MCP серверы** перезапускаются gracefully при изменении `mcp_servers.yaml`.

**Субагенты** обновляются при изменении их YAML/MD-конфигов.

### 3.3 Модульные инструкции агента (Bootstrap)

Системный промпт собирается из модулей с HotReload:

```
Системный промпт = SOUL.md + COMPANY.md + BEHAVIOR.md
                 + departments/{slug}.md     (по департаменту)
                 + users/{user_id}.md        (персональные настройки, опц.)
                 + [активные скилы]
                 + [каталог субагентов]
```

Каждый модуль кэшируется с TTL. При изменении файла — инвалидируется кэш конкретного модуля, не весь промпт.

### 3.4 Шаблоны для создания расширений

Команды CLI для генерации стартовых шаблонов:

```bash
uv run corpclaw-lite generate skill my_skill
uv run corpclaw-lite generate plugin my_plugin --with-tool --with-script
uv run corpclaw-lite generate subagent my_agent
uv run corpclaw-lite generate channel my_channel
```

---

## 4. Ролевой доступ (RBAC)

### 4.1 Конфигурация департаментов

```yaml
# departments.yaml
departments:
  marketing:
    name: "Маркетинг"
    profile: readonly
    allowed_tools: [read_file, list_files, web_search, web_fetch, memory_store, memory_recall, send_file, read_image]
    allowed_skills: [translator, content_writer, excel_normalizer]
    allowed_plugins: [excel_normalizer]
    allowed_subagents: [filesystem-agent, research-agent, document-agent]
    allowed_mcp: [web_search]
    budget:
      max_steps: 12
      max_tool_calls: 24
      max_wall_time_ms: 90000

  development:
    name: "Разработка"
    profile: coding
    allowed_tools: [read_file, write_file, edit_file, list_files, search_files, memory_store, memory_recall, web_fetch, web_search]
    allowed_skills: [translator, code_reviewer, doc_writer, excel_normalizer]
    allowed_plugins: "*"
    allowed_subagents: [filesystem-agent, research-agent, document-agent, execution-agent]
    allowed_mcp: [filesystem, web_search]
    budget:
      max_steps: 20
      max_tool_calls: 40
      max_wall_time_ms: 180000

  admin:
    name: "Администраторы"
    allowed_tools: "*"
    allowed_skills: "*"
    allowed_plugins: "*"
    allowed_subagents: "*"
    allowed_mcp: "*"
```

### 4.2 Проверка прав: минимальная сигнатура

```python
class PermissionChecker:
    def can_use_tool(self, user: User, tool_name: str) -> bool: ...
    def can_use_skill(self, user: User, skill_id: str) -> bool: ...
    def can_use_plugin(self, user: User, plugin_name: str) -> bool: ...
    def can_dispatch_subagent(self, user: User, subagent_id: str) -> bool: ...
    def can_use_mcp(self, user: User, server_name: str) -> bool: ...
    def get_budget(self, user: User) -> BudgetConfig: ...
```

---

## 5. Роутинг моделей (Multi-Provider)

Провайдер определяется по типу задачи через конфигурацию:

```yaml
# settings.yaml
providers:
  default: "local"
  named:
    local:
      type: openai
      model: "qwen2.5-7b-instruct"
      base_url: "${LLM_BASE_URL:-http://localhost:1234/v1}"
    vision:
      type: openai
      model: "${VISION_MODEL:-qwen2.5-vl-7b}"
      base_url: "${VISION_LLM_BASE_URL:-}"
    cloud:
      type: anthropic
      model: "claude-3-5-sonnet-20241022"
  routing:
    - task_kind: vision       # read_image tool → vision model
      provider: vision
    - task_kind: subagent     # субагент execution-agent → cloud (если настроен)
      subagent_id: execution-agent
      provider: cloud
```

**ProviderRouter** — простая lookup-таблица. Нет сложного resolver'а из v1. Если роутинг не настроен — используется `default`.

---

## 6. Безопасность

### 6.1 ToolGuard (YAML rules, CoPaw pattern)

```yaml
# config/tool_guard_rules.yaml
rules:
  - id: DANGEROUS_RM
    tools: [exec_script]
    params: [command, args]
    severity: CRITICAL        # INFO | MEDIUM | HIGH | CRITICAL
    patterns:
      - "\\brm\\s+-rf\\b"
      - "\\bdd\\b"
    remediation: "Удаление файлов требует явного подтверждения"
    require_approval: true    # CRITICAL/HIGH → approval inline-кнопками

  - id: PATH_TRAVERSAL
    tools: [read_file, write_file]
    params: [path]
    severity: HIGH
    patterns: ["\\.\\./"]
    remediation: "Path traversal запрещён"

  - id: SECRET_IN_ARGS
    tools: ["*"]
    params: ["*"]
    severity: HIGH
    patterns: ["sk-[A-Za-z0-9]{20,}", "ghp_[A-Za-z0-9]{36}"]
    remediation: "Ключи API не передаются как аргументы"
```

Проверка выполняется **до** вызова инструмента. При CRITICAL/HIGH без `require_approval: true` — заблокировать. С `require_approval: true` — отправить пользователю inline Approve/Deny и ждать.

### 6.2 NetworkPolicy для контейнеров (NemoClaw pattern)

```yaml
# config/network_policy.yaml
default: deny                 # deny-by-default
policies:
  - name: anthropic
    host: api.anthropic.com
    port: 443
  - name: ollama
    host: host.docker.internal
    port: 11434
  - name: github
    host: api.github.com
    port: 443
```

### 6.3 Mandatory IPC Authentication

IPC между host и контейнером всегда аутентифицирован:
- Секрет `CORPCLAW_IPC_SECRET` **обязателен**, fail-fast при запуске если не установлен
- Каждое сообщение содержит `nonce` (UUID) + `timestamp` + HMAC-SHA256
- Host сохраняет `seen_nonces` за последние 5 минут → защита от replay-атак

### 6.4 Ограничения инструментов по типам файлов

На уровне кода каждый инструмент проверяет тип файла и отклоняет несовместимые форматы:

```python
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
TEXT_EXTENSIONS  = {".txt", ".md", ".py", ".js", ".json", ".yaml", ".csv"}

class ReadImageTool(Tool):
    def validate_path(self, path: str) -> None:
        if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
            raise ToolError(f"read_image принимает только изображения: {IMAGE_EXTENSIONS}")

class ReadFileTool(Tool):
    def validate_path(self, path: str) -> None:
        if Path(path).suffix.lower() in IMAGE_EXTENSIONS:
            raise ToolError("Используйте read_image для изображений")
```

---

## 7. Каналы связи

Каналы — это расширения, управляемые через единый реестр. CLI — базовый канал. Telegram — первый плагин-канал.

### 7.1 Channel Protocol

```python
class Channel(Protocol):
    name: str
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_message(self, chat_id: str, text: str, **opts: Any) -> None: ...
    async def send_file(self, chat_id: str, path: Path, caption: str = "") -> None: ...
    async def request_approval(self, chat_id: str, action: str, details: str) -> bool: ...
```

### 7.2 Форматирование и Inline-кнопки

Telegram-канал поддерживает:
- **MarkdownV2 / HTML** — красивый вывод текста (код, жирный, курсив, блоки)
- **Inline-кнопки** через `request_approval()` → ✅ Подтвердить / ❌ Отклонить

Кнопки используются для ToolGuard approvals (HIGH/CRITICAL риск).

---

## 8. Контейнеризация

### 8.1 Per-user Docker контейнеры

```
Запрос пользователя
    → ContainerManager.ensure_running(user_id)
    → Если нет контейнера → docker run corpclaw-agent:latest
    → Монтируется /workspace/{user_id} (r/w)
    → Монтируется /config (read-only) ← Config Immutability
    → Применяются CPU/mem/pids лимиты
    → Применяется NetworkPolicy → iptables rules
    → Применяется seccomp профиль
    → Idle timeout → cleanup
```

### 8.2 Ресурсные лимиты

```yaml
container:
  resource_limits:
    cpu_quota: 50000      # 50% CPU
    mem_limit: "512m"
    pids_limit: 100
  idle_timeout: 300       # 5 минут → cleanup
  max_per_user: 1         # 1 контейнер на пользователя
```

---

## 9. Память

### 9.1 Базовая память (всегда)

- **SQLite** — история диалогов (rolling buffer, `history_limit: 50`)
- **SQLite** — долгосрочные факты (key-value, с TTL)
- **LLM Consolidation** — периодическое суммирование фактов. Не при каждом запросе — только когда накоплено достаточно сообщений (порог в конфиге).

### 9.2 Векторный поиск (опционально)

Qdrant — опциональное расширение, включается в `settings.yaml`:

```yaml
memory:
  hybrid_search:
    enabled: false    # Qdrant отключён по умолчанию
    qdrant_url: "${QDRANT_URL:-http://localhost:6333}"
```

Если включён — `memory_recall` использует semantic search. Если нет — простой keyword поиск по SQLite.

---

## 10. Асинхронность и масштабирование

### 10.1 Полностью асинхронный стек

- Обработчики каналов, LLM вызовы, инструменты, IPC с контейнерами, SQLite (aiosqlite), MCP — всё `async def`
- **Per-user asyncio.Lock** — один пользователь не запускает параллельные запросы, разные пользователи работают параллельно

### 10.2 Задел под горизонтальное масштабирование

Реализуется через конвенции без лишнего кода прямо сейчас:

1. **User state в SQLite** (не in-memory) — позволит позже заменить на PostgreSQL
2. **Per-session stateless routing** — AgentLoop не хранит state между запросами
3. **Container registry в DB** — данные не привязаны к конкретному процессу
4. **ChannelMessage принимается из очереди** (абстракция) — позволит добавить Celery/Redis Queue

**Что НЕ делается (overengineering):** distributed coordinator, service mesh, leader election.

---

## 11. Логирование

- **`agent_activity.jsonl`** — структурированный лог каждого запроса (user_id, duration, tools_used, tokens). RotatingFileHandler, 10MB, 5 файлов.
- **`corpclaw.log`** — Python DEBUG логи ключевых модулей. RotatingFileHandler, 5MB, 3 файла.
- **CredentialScrubber** — log filter, маскирует API-ключи по regex (`sk-...`, `ghp_...`, и т.д.)
- **`/health` HTTP эндпоинт** — базовые счётчики (requests, tool_calls, errors). Prometheus — опциональное расширение.

---

## 12. Минимальные типы данных

### Tool (5 атрибутов, не 18)

```python
class RiskLevel(str, Enum):
    LOW = "low"        # read, search, recall
    MEDIUM = "medium"  # write, send_file
    HIGH = "high"      # exec_script
    CRITICAL = "critical"

class Tool(ABC):
    name: str
    description: str
    params: list[ToolParam]
    risk_level: RiskLevel = RiskLevel.LOW

    @abstractmethod
    async def execute(self, user: User | None, **kwargs: Any) -> str: ...
```

### Skill (6 атрибутов, не 30)

```python
@dataclass(frozen=True)
class Skill:
    id: str
    description: str
    allowed_for: list[str]   # department slugs, или ["*"]
    instructions: str        # Полный markdown-контент
    path: Path | None = None
    version: str = "1.0.0"
```

### Plugin (манифест + компоненты)

```
plugins/my_plugin/
├── manifest.yaml   # name, version, type, description, allowed_departments, components
├── skill.md        # Инструкции для агента
├── tool.py         # class MyTool(Tool) — опционально
└── scripts/run.sh  # Заготовленный скрипт — опционально
```

---

## 13. Тестирование

```
tests/
├── test_agent_loop.py      # ReAct цикл с mock LLM
├── test_tool_guard.py      # YAML-правила безопасности
├── test_permissions.py     # RBAC
├── test_skills.py          # Загрузка/поиск скилов
├── test_plugins.py         # Загрузка плагинов
├── test_memory.py          # SQLite + consolidation
├── test_subagent.py        # Dispatch субагентов
├── test_containers.py      # Docker lifecycle (mark: requires_docker)
└── test_channels.py        # Telegram router (whitelist, auth)
```

- Coverage: ≥75%
- MyPy: strict mode
- Ruff: E, F, I, UP, B, C4, SIM, G + 100 символов

---

## 14. Фазовый план реализации

### Фаза 1: Ядро (неделя 1)
1. Инициализация `corpclaw-lite/` через `uv init`
2. LLM-провайдеры (Anthropic, OpenAI) + XML-fallback (перенос из v1)
3. Базовые типы: Tool, Skill, User, PermissionChecker
4. **Simple ReAct AgentLoop** (без субагентов, без памяти)
5. CLI-канал для ручного тестирования
6. Встроенные инструменты: read_file, write_file, list_files, search_files
7. Тесты для AgentLoop + инструментов

### Фаза 2: Расширения и RBAC (неделя 1–2)
1. Система скилов (loader, registry, watcher)
2. PermissionChecker + DepartmentManager (YAML)
3. Система плагинов (manifest loader)
4. SubagentDispatcher + SubagentRegistry
5. Встроенные субагенты (filesystem, research, document, execution)
6. ProviderRouter (роутинг моделей по задаче)
7. VisionProcessor (read_image через отдельный LLM-вызов)

### Фаза 3: Безопасность и контейнеры (неделя 2)
1. ToolGuard (YAML rules, severity levels)
2. NetworkPolicy
3. Mandatory IPC Auth (HMAC + nonce)
4. ContainerManager (lifecycle, resource limits, NetworkPolicy)
5. Config Immutability (read-only mount)
6. CredentialScrubber

### Фаза 4: Telegram-канал и память (неделя 2–3)
1. Channel Protocol + ChannelRegistry
2. TelegramChannel (router, formatter, approval_ui)
3. SQLite память (история, факты)
4. LLM Consolidation (опциональная, по порогу)
5. Qdrant (опциональный)
6. Структурированное логирование
7. Health HTTP-эндпоинт

### Фаза 5: Полировка (неделя 3)
1. MCP интеграция (manager, client, adapter)
2. HotReload unified watcher
3. Bootstrap модульные инструкции + hot-reload кэш
4. CLI команды (user-create, containers, prune, skill list, plugin list, generate)
5. README.md + AGENTS.md
6. CI pipeline

---

## 15. Ключевые уроки из v1

| Ошибка v1 | Решение в Lite |
|-----------|----------------|
| 4 параллельных механизма расширения | 1 механизм с типами (plugin, skill, subagent, channel) |
| extensibility/ — 1,863 строк для 0 потребителей | Нет фреймворка. Расширяемость через манифесты |
| Skill dataclass: 30 полей | Skill dataclass: 6 полей |
| Tool базовый класс: 18 properties | Tool базовый класс: 5 properties |
| LLM-based TaskPlanner + TaskVerifier | SimpleBudgetGuard + SimpleProgressGuard |
| HMAC IPC как опциональный | HMAC IPC как обязательный с nonce |
| Governance: gzip, SIEM, SOC-2 | Structured JSON logging + CredentialScrubber |
| Approvals: shadow_mode, cooldown | ToolGuard inline Approve/Deny кнопки |
| Memory: Qdrant + LLM consolidation при каждом запросе | SQLite rolling buffer + редкая consolidation + опциональный Qdrant |
| AgentLoop: 1762 строки God Object | AgentLoop: ~400 строк, делегирует субагентам |
| Документация: 22K строк (дрейф) | README.md + inline docstrings |

---

## 16. Чеклист готовности к деплою

- [ ] `uv run corpclaw-lite telegram` — запускается, отвечает на сообщения
- [ ] Маркетолог говорит «нормализуй Excel» → получает файл обратно
- [ ] `uv run pytest tests/ -v` — ≥75% coverage, 0 failures
- [ ] `uv run mypy src/ --strict` — 0 errors
- [ ] `uv run ruff check src/ --fix && ruff format src/` — 0 errors
- [ ] Docker контейнер поднимается, выполняет инструменты изолированно
- [ ] ToolGuard блокирует `rm -rf` через exec_script
- [ ] Добавление нового `skills/*.md` → скил доступен без перезапуска (HotReload)
- [ ] `/health` возвращает статус и базовые метрики
