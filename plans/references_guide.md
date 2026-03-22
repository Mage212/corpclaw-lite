# CorpClaw Lite: Детальный справочник архитектурных решений

Этот документ — живой архив принятых инженерных решений из референсных проектов. Используйте его как **первую точку поиска** при планировании и реализации.

---

## Содержание
1. [Безопасность: Network Policy (NemoClaw)](#1-безопасность-network-policy-nemoclaw)
2. [Безопасность: ToolGuard — YAML-правила для инструментов (CoPaw)](#2-безопасность-toolguard--yaml-правила-для-инструментов-copaw)
3. [Безопасность: IPC авторизация между контейнерами (NanoClaw)](#3-безопасность-ipc-авторизация-между-контейнерами-nanoclaw)
4. [Каналы: BaseChannel и MessageRenderer (CoPaw)](#4-каналы-basechannel-и-messagerenderer-copaw)
5. [LLM Роутинг: Local vs Cloud (CoPaw)](#5-llm-роутинг-local-vs-cloud-copaw)
6. [Skills: Загрузка, Синхронизация и Hot-Reload (CoPaw)](#6-skills-загрузка-синхронизация-и-hot-reload-copaw)
7. [Память: Компактификация и конфигурируемый бэкенд (CoPaw)](#7-память-компактификация-и-конфигурируемый-бэкенд-copaw)
8. [Sandbox: Blueprint, профили и filesystem policy (NemoClaw)](#8-sandbox-blueprint-профили-и-filesystem-policy-nemoclaw)
9. [Код из v1: Что переносить напрямую](#9-код-из-v1-что-переносить-напрямую)
10. [Код из v1: XML Tool Calling (критично для локальных LLM)](#10-код-из-v1-xml-tool-calling-критично-для-локальных-llm)

---

## 1. Безопасность: Network Policy (NemoClaw)

**Проблема:** Агент в Docker-контейнере может делать произвольные сетевые запросы — украсть данные через `curl`, обойти ограничения через DNS rebinding и т.д.

**Решение NemoClaw:** Принцип "deny-by-default". ВСЕ исходящие соединения заблокированы по умолчанию. Разрешения задаются явным allowlist в YAML:

```yaml
# references/NemoClaw/nemoclaw-blueprint/policies/openclaw-sandbox.yaml

network_policies:
  telegram:                              # Именованная политика
    name: telegram
    endpoints:
      - host: api.telegram.org
        port: 443
        protocol: rest
        enforcement: enforce             # Жёсткое применение
        tls: terminate                   # TLS терминируется на gateway
        rules:
          - allow: { method: GET, path: "/bot*/**" }
          - allow: { method: POST, path: "/bot*/**" }
                                         # Только Telegram Bot API, ничего лишнего

  github:
    name: github
    endpoints:
      - host: github.com
        port: 443
        access: full
    binaries:                            # ⚠️ ключевая фича: правило применяется
      - { path: /usr/bin/git }           # только для указанных бинарников.
```

**Ключевые концепции для `corpclaw-lite`:**
- `binaries:` — привязка правила к конкретному бинарнику предотвращает ситуацию, когда `python` читает секреты и отправляет на `github.com`, пользуясь политикой для `git`
- `enforcement: enforce` vs `log` — начинать с `log` при отладке, переводить в `enforce` в production
- `tls: terminate` — gateway может инспектировать трафик для логирования подозрительных запросов

**Файлы референсa:**
- [`references/NemoClaw/nemoclaw-blueprint/policies/openclaw-sandbox.yaml`](../references/NemoClaw/nemoclaw-blueprint/policies/openclaw-sandbox.yaml)
- [`references/NemoClaw/nemoclaw-blueprint/blueprint.yaml`](../references/NemoClaw/nemoclaw-blueprint/blueprint.yaml) — профили (default, ncp, nim-local, vllm)

**Как адаптировать для corpclaw-lite:**
В `config/network_policy.yaml` объявить per-user сети Docker, затем применять iptables-правилами при создании контейнера. Базовый allowlist: только `api.telegram.org` и локальные LLM эндпоинты.

---

## 2. Безопасность: ToolGuard — YAML-правила для инструментов (CoPaw)

**Проблема:** LLM может сгенерировать деструктивную команду (`rm -rf /`, `curl | bash`, exfiltration) в параметрах инструмента.

**Решение CoPaw:** `ToolGuardMixin` встраивается в агентный цикл через Python MRO, перехватывая КАЖДЫЙ вызов инструмента до его выполнения.

### Архитектура в 3 слоях:

```
AgentLoop._execute_tool(tool_name, args)
    ↓
ToolGuardMixin._acting(tool_call)  ← перехватчик
    ├── engine.is_denied(tool_name) → auto-deny без approve
    ├── engine.is_guarded(tool_name)
    │       ├── svc.consume_approval() → pre-approved → execute
    │       └── engine.guard(tool_name, args) → findings?
    │               ├── findings → _acting_with_approval() → ожидание
    │               └── no findings → super()._acting() → execute
    └── (не guarded) → super()._acting() → execute
```

### Структура правил YAML:
```yaml
# rules/dangerous_shell_commands.yaml
- id: SHELL_PIPE_TO_EXEC
  tool: execute_shell_command         # optional: пусто = все инструменты
  params: [command]                   # optional: пусто = все параметры
  category: command_injection
  severity: HIGH                      # CRITICAL / HIGH / MEDIUM / INFO
  patterns:
    - "curl.*\\|.*(?:sh|bash)"
    - "wget.*\\|.*(?:sh|bash)"
  exclude_patterns:
    - "^#"                            # исключить комментарии
  description: "Piping downloaded content directly to a shell"
  remediation: "Download to a file first and inspect before execution"
```

### Как работает `RuleBasedToolGuardian`:
1. Загружает правила при старте из папки `rules/` (YAML)
2. Для каждого `tool_call` — перебирает все параметры как `str`
3. Запускает `re.search(pattern, value_str)` для каждого скомпилированного паттерна
4. Возвращает список `GuardFinding` с severity, matched_pattern, snippet

**Что происходит при срабатывании:**
- `CRITICAL` или `HIGH` → агент блокируется, пользователь получает "⚠️ Risk Detected" с параметрами и кнопками Approve/Deny
- Denial record добавляется в историю с меткой `TOOL_GUARD_DENIED_MARK`
- При следующем reasoning агент "видит" что застрял и отправляет "⏳ Waiting for approval" вместо нового вызова
- `/approve` → single-use токен потребляется, инструмент выполняется, denied-messages чистятся из памяти

**Файлы референса:**
- [`references/CoPaw/src/copaw/agents/tool_guard_mixin.py`](../references/CoPaw/src/copaw/agents/tool_guard_mixin.py) — перехватчик (352 строки)
- [`references/CoPaw/src/copaw/security/tool_guard/guardians/rule_guardian.py`](../references/CoPaw/src/copaw/security/tool_guard/guardians/rule_guardian.py) — YAML-движок (383 строки)
- `references/CoPaw/src/copaw/config/config.py:445` — `ToolGuardRuleConfig`, `ToolGuardConfig`

**Как адаптировать для corpclaw-lite:**
Вместо Mixin встроить проверку напрямую в `AgentLoop._execute_tool()`. Убрать `session_id` approve service (заменить на Telegram inline-кнопки). Хранить approved-токены в памяти процесса (dict `tool_name → asyncio.Event`).

---

## 3. Безопасность: IPC авторизация между контейнерами (NanoClaw)

**Проблема:** Субагент в контейнере отправляет IPC команды хосту. Нужно гарантировать что субагент отдела "analysts" не может управлять задачами отдела "sysadmins".

**Решение NanoClaw:** Авторизация строится на **файловой системе** (директория = identity), а не на HMAC токенах. Каждый контейнер пишет в свою директорию `/ipc/{group_folder}/tasks/*.json`. Хост читает файлы и определяет источник по DirectoryPath, а не по содержимому файла.

```typescript
// references/nanoclaw/src/ipc.ts

// Авторизация: identity = имя директории (не содержимое файла)
const isMain = folderIsMain.get(sourceGroup) === true;

// Правило: main может слать любому, остальные — только себе
if (isMain || (targetGroup && targetGroup.folder === sourceGroup)) {
    await deps.sendMessage(data.chatJid, data.text);
}
// Nested authorization для операций:
case 'pause_task':
    const task = getTaskById(data.taskId);
    if (task && (isMain || task.group_folder === sourceGroup)) {
        // Только владелец задачи или main
    }
```

**Ключевые тесты авторизации** (из `ipc-auth.test.ts`):
- non-main НЕ может планровать задачи для другого департамента
- Нельзя зарегистрировать группу с path traversal path `../../outside`
- non-main НЕ может регистрировать новые группы
- `isMain` НЕ может быть установлен через IPC (только через конфигурацию хоста)

**Файлы референса:**
- [`references/nanoclaw/src/ipc.ts`](../references/nanoclaw/src/ipc.ts) — IPC dispatch (456 строк)
- [`references/nanoclaw/src/ipc-auth.test.ts`](../references/nanoclaw/src/ipc-auth.test.ts) — 33 теста авторизации (679 строк)

**Как адаптировать для corpclaw-lite:**
В `container/ipc.py` — каждый контейнер пишет в `/workspace/{user_id}/ipc/`. Хост авторизует по `user_id` из пути. HMAC подписывает сообщение (усиление по сравнению с NanoClaw) чтобы нельзя было подделать путь symlink-ом.

---

## 4. Каналы: BaseChannel и MessageRenderer (CoPaw)

**Проблема:** Telegram, CLI, Web UI — все принимают сообщения по-разному, но агентская логика должна быть одинаковой.

**Решение CoPaw:** `BaseChannel` — абстрактный класс с единым lifecycle. Каждый канал реализует только `send()`, `start()`, `stop()` и `build_agent_request_from_native()`. Вся остальная логика — в базовом классе.

### Ключевые концепции BaseChannel:

```python
# references/CoPaw/src/copaw/app/channels/base.py

class BaseChannel(ABC):
    # Политики доступа (allowlist/denylist по sender_id)
    dm_policy: str = "open"          # "open" | "allowlist"
    group_policy: str = "open"
    allow_from: set[str]             # разрешённые sender_id
    require_mention: bool            # игнорировать сообщения без @упоминания

    # Дебаунсинг (объединяет несколько быстрых сообщений в одно)
    _debounce_seconds: float = 0.0

    # Единый метод обработки — все каналы используют одинаковый pipeline:
    # payload → AgentRequest → process(request) → Event stream → send()
    async def consume_one(self, payload: Any) → None: ...

    # Проверка allowlist
    def _check_allowlist(self, sender_id, is_group) → tuple[bool, str | None]:
        if self.dm_policy == "open": return True, None
        if sender_id in self.allow_from: return True, None
        return False, f"Not authorized. Your ID: {sender_id}"
```

### MessageRenderer — независимый рендеринг по стилю канала:

```python
# references/CoPaw/src/copaw/app/channels/renderer.py

@dataclass
class RenderStyle:
    supports_markdown: bool = True
    supports_code_fence: bool = True
    use_emoji: bool = True
    filter_tool_messages: bool = False   # скрыть детали tool_call от юзера
    filter_thinking: bool = False        # скрыть CoT reasoning

# Использование:
# Telegram → RenderStyle(supports_markdown=True, use_emoji=True)
# CLI → RenderStyle(supports_markdown=False, use_emoji=False)
```

**Файлы референса:**
- [`references/CoPaw/src/copaw/app/channels/base.py`](../references/CoPaw/src/copaw/app/channels/base.py) — полный lifecycle (841 строк)
- [`references/CoPaw/src/copaw/app/channels/renderer.py`](../references/CoPaw/src/copaw/app/channels/renderer.py) — рендеринг (374 строки)
- `references/CoPaw/src/copaw/app/channels/registry.py` — реестр каналов

**Как адаптировать для corpclaw-lite:**
```python
# channels/base.py (упрощённый вариант)
class Channel(Protocol):
    async def send_message(self, chat_id: str, text: str, **opts) → None: ...
    async def request_approval(self, chat_id: str, action: str) → bool: ...
    async def start(self) → None: ...
    async def stop(self) → None: ...
```
Telegram реализует inline-кнопки Approve/Deny через `InlineKeyboardMarkup`. CLI реализует через `input("Approve? [y/N]: ")`.

---

## 5. LLM Роутинг: Local vs Cloud (CoPaw)

**Проблема:** В privacy-sensitive задачах нужен локальный LLM, для сложных — облако.

**Решение CoPaw:** `RoutingChatModel` — прокси над двумя endpoint-ами. `RoutingPolicy` принимает решение на основе `mode` из конфига.

```python
# references/CoPaw/src/copaw/agents/routing_chat_model.py

class RoutingPolicy:
    def decide(self, *, text: str = "", channel: str = "",
               tools_available: bool = True) → RoutingDecision:
        if self.cfg.mode == "cloud_first":
            return RoutingDecision(route="cloud", reasons=["mode:cloud_first"])
        return RoutingDecision(route="local", reasons=["mode:local_first"])

class RoutingChatModel(ChatModelBase):
    def __init__(self, *, local_endpoint: RoutingEndpoint,
                 cloud_endpoint: RoutingEndpoint,
                 routing_cfg: AgentsLLMRoutingConfig) → None:
        # Прозрачная обёртка — агент не знает какой LLM используется
        ...

    async def __call__(self, messages, tools=None, ...) → ChatResponse:
        decision = self.policy.decide(tools_available=tools is not None)
        endpoint = self.local_endpoint if decision.route == "local" else self.cloud_endpoint
        return await endpoint.model(messages=messages, tools=tools, ...)
```

**Как адаптировать для corpclaw-lite:**
В `settings.yaml` описать профили:
```yaml
routing:
  vision: anthropic       # vision задачи → Anthropic Vision
  subagent: local         # субагенты → локальный Ollama
  default: local          # по умолчанию → локальный
```
`ProviderRouter` маппит `task_type → Provider` по этому конфигу.

**Файлы референса:**
- [`references/CoPaw/src/copaw/agents/routing_chat_model.py`](../references/CoPaw/src/copaw/agents/routing_chat_model.py) — роутер (123 строки)

---

## 6. Skills: Загрузка, Синхронизация и Hot-Reload (CoPaw)

**Проблема:** Skills хранятся как Markdown-файлы. Нужно загружать их в агент, обновлять без рестарта, поддерживать builtin + кастомные.

**Решение CoPaw:** Трёхслойная архитектура директорий:
- `builtin/` — скилы из репозитория (только чтение)
- `customized/` — пользовательские переопределения
- `active/` — то что реально загружено в агент (sync из builtin+customized)

```python
# references/CoPaw/src/copaw/agents/skills_manager.py

# Структура skill-а:
# skills/my_skill/
#   SKILL.md         — основной файл (YAML frontmatter + инструкции)
#   references/      — справочные материалы
#   scripts/         — скрипты для запуска

class SkillInfo(BaseModel):
    name: str
    description: str = ""       # из YAML frontmatter
    content: str                # полный текст SKILL.md
    source: str                 # "builtin" | "customized" | "active"
    path: str
    references: dict[str, Any]  # дерево файлов references/
    scripts: dict[str, Any]     # дерево файлов scripts/

# Приоритет: customized переопределяет builtin с тем же именем
def _dedupe_skills_by_name(skills: list[SkillInfo]) → list[SkillInfo]:
    merged: dict[str, SkillInfo] = {}
    for skill in skills:
        merged[skill.name] = skill  # последний побеждает
    return list(merged.values())

# Синхронизация
def sync_skills_to_working_dir(skill_names=None, force=False) → tuple[int, int]:
    # builtin → active (если нет в active, или force=True)
    # customized → active (переопределяет builtin)
    ...
```

**Как адаптировать для corpclaw-lite:**
`ExtensionWatcher` следит за `skills/*.md` через `watchdog`. При изменении — перезагружает реестр без рестарта. Skill-файл парсится через `python-frontmatter` для извлечения метаданных.

**Файлы референса:**
- [`references/CoPaw/src/copaw/agents/skills_manager.py`](../references/CoPaw/src/copaw/agents/skills_manager.py) — полная система syncing (949 строк)
- `references/CoPaw/src/copaw/agents/skills/` — примеры builtin скилов

---

## 7. Память: Компактификация и конфигурируемый бэкенд (CoPaw)

**Проблема:** Локальные LLM имеют контекстное окно 8K-32K. История разговора быстро его заполняет.

**Решение CoPaw:** `MemoryManager.compact_memory()` — периодическая компактификация, которая сворачивает старые сообщения в summary через второй LLM-вызов.

```python
# references/CoPaw/src/copaw/agents/memory/memory_manager.py

class MemoryManager(ReMeLight):
    async def compact_memory(self, messages: list[Msg],
                             previous_summary: str = "") → str:
        """Compact messages into condensed summary.
        
        Ключевые параметры из конфига:
        - max_input_length: максимум токенов перед компактификацией
        - memory_compact_ratio: доля сообщений для компактификации (0.5 = старая половина)
        """
        return await super().compact_memory(
            messages=messages,
            as_llm=self.chat_model,
            compact_ratio=memory_compact_ratio,  # например 0.5
            previous_summary=previous_summary,   # накопительный summary
        )

    async def summary_memory(self, messages: list[Msg]) → str:
        """Полный summary с возможностью записи в файл через file tools."""
        ...
```

**Конфигурируемый бэкенд хранилища:**
```python
# Автовыбор по платформе:
memory_store_backend = os.environ.get("MEMORY_STORE_BACKEND", "auto")
if memory_store_backend == "auto":
    memory_backend = "local" if platform.system() == "Windows" else "chroma"

# Vector search — только если есть API key для embeddings
vector_enabled = bool(embedding_api_key) and bool(embedding_model_name)
```

**Как адаптировать для corpclaw-lite:**
SQLite базовый бэкенд (перенести из v1). Компактификация — периодическая (раз в N сообщений), а не per-request. Qdrant для vector search — disabled по умолчанию, включается через `config/settings.yaml`.

**Файлы референса:**
- [`references/CoPaw/src/copaw/agents/memory/memory_manager.py`](../references/CoPaw/src/copaw/agents/memory/memory_manager.py) — MemoryManager (292 строки)

---

## 8. Sandbox: Blueprint, профили и filesystem policy (NemoClaw)

**Проблема:** Нужно изолировать агента так, чтобы он не мог читать/писать за пределами своего workspace.

**Решение NemoClaw:** Версионированный Blueprint + filesystem_policy в YAML.

```yaml
# references/NemoClaw/nemoclaw-blueprint/policies/openclaw-sandbox.yaml

filesystem_policy:
  read_only:
    - /usr
    - /lib
    - /app
    - /etc
    - /sandbox/.openclaw        # Иммутабельный конфиг gateway — агент не может
                                # тамперить auth токены или CORS настройки
  read_write:
    - /sandbox                  # Рабочая директория агента
    - /tmp
    - /sandbox/.openclaw-data   # Writable данные (симлинк из .openclaw)

# blueprint.yaml — профили инференса:
profiles:
  - default     # NVIDIA Endpoint API
  - nim-local   # Локальный NIM на http://nim-service.local:8000/v1
  - vllm        # vLLM на localhost:8000 (credential_default: "dummy" для OpenAI-совместимого)
  - ncp         # NVIDIA Cloud Platform
```

**Ключевое решение: read-only конфиг mount**
```yaml
# Gateway config иммутабелен:
read_only:
  - /sandbox/.openclaw       # агент не может изменить token или CORS
# Но данные агента записываемы через symlink:
read_write:
  - /sandbox/.openclaw-data  # на самом деле живёт здесь → symlinked from .openclaw
```

**Как адаптировать для corpclaw-lite:**
```python
# container/policies.py
CONTAINER_BINDS = {
    "/host/config/agent_readonly.yaml": {"bind": "/app/config.yaml", "mode": "ro"},
    "/host/workspaces/{user_id}": {"bind": "/workspace", "mode": "rw"},
}
RESOURCE_LIMITS = {
    "nano_cpus": 2_000_000_000,  # 2 CPU
    "mem_limit": "1g",
    "pids_limit": 256,
}
```

**Файлы референса:**
- [`references/NemoClaw/nemoclaw-blueprint/policies/openclaw-sandbox.yaml`](../references/NemoClaw/nemoclaw-blueprint/policies/openclaw-sandbox.yaml) — полная policy (174 строки)
- [`references/NemoClaw/nemoclaw-blueprint/blueprint.yaml`](../references/NemoClaw/nemoclaw-blueprint/blueprint.yaml) — профили (66 строк)

---

## 9. Код из v1: Что переносить напрямую

Эти модули написаны хорошо и не требуют рефакторинга:

| Модуль v1 | Путь | Что делает | Действие |
|-----------|------|------------|----------|
| `xml_tool_calling.py` | `src/corpclaw/llm/xml_tool_calling.py` | XML парсинг tool_calls для локальных LLM | ✅ Скопирован |
| `SimpleBudgetGuard` | `src/corpclaw/agent/guards.py` | Лимит итераций, инструментов, времени | ✅ Перенести |
| `SimpleProgressGuard` | `src/corpclaw/agent/guards.py` | Детекция зацикливания (3 одинаковых ошибки подряд) | ✅ Перенести |
| `memory/sqlite.py` | `src/corpclaw/memory/sqlite.py` | SQLite бэкенд для истории сессий | ✅ Перенести |
| `llm/anthropic.py` | `src/corpclaw/llm/anthropic.py` | Anthropic provider + streaming | 🔄 Адаптировать под новый Protocol |
| `llm/openai.py` | `src/corpclaw/llm/openai.py` | OpenAI-compatible provider | 🔄 Адаптировать |
| `container/ipc.py` | `src/corpclaw/container/ipc.py` | IPC host↔container | 🔄 Добавить HMAC обязательно |
| `channels/telegram/` | `src/corpclaw/channels/telegram/` | Telegram бот | 🔄 Адаптировать под Channel Protocol |

**Что НЕ переносить:**
- `agent/orchestration/` — TaskPlanner, Verifier, ObjectiveStorage → заменить простым ReAct
- `extensibility/` — весь каталог compatibility, AvailabilityResolver → заменить manifest loader
- `plugins/manager.py` (весь) → заменить на `SkillInfo`-подобный простой loader
- `governance/` — весь каталог → заменить structured JSON logging
- `approvals/service.py` → заменить ToolGuard + Telegram inline кнопки

---

## 10. Код из v1: XML Tool Calling (критично для локальных LLM)

**Проблема:** LocalLLM (Qwen, Mistral, Llama) часто возвращают инструменты в неправильном JSON-формате или смешивают XML и JSON.

**Решение v1:** Парсер который:
1. Пытается распарсить стандартный OpenAI `tool_calls` формат
2. Ищет `<tool_call>name\nparams</tool_call>` теги в тексте
3. Ищет ````json { "tool": ... }``` ` блоки в Markdown
4. Возвращает normalized `ToolCall` объекты из любого из форматов

```python
# src/corpclaw/llm/xml_tool_calling.py — уже скопирован в corpclaw-lite

# Пример XML формата который понимает парсер:
"""
<tool_call>
read_file
{"path": "/workspace/report.xlsx"}
</tool_call>
"""

# Пример JSON в Markdown который понимает парсер:
"""
```json
{"tool": "read_file", "parameters": {"path": "/workspace/report.xlsx"}}
```
"""
```

**Как использовать:** В `OpenAIProvider.chat()` — если `response.choices[0].message.tool_calls` пустой, передать `response.choices[0].message.content` в XML-парсер.

**Файл:** `src/corpclaw_lite/llm/xml_tool_calling.py` (уже в проекте)

---

## Быстрый поиск по задаче

| Задача | Смотреть раздел |
|--------|----------------|
| Блокировать `rm -rf` или `curl | bash` | Раздел 2 (ToolGuard) |
| Разрешить контейнеру только api.telegram.org | Раздел 1 (Network Policy) |
| Авторизация запросов из субагента по user_id | Раздел 3 (IPC Auth) |
| Отправить форматированный ответ в Telegram | Раздел 4 (BaseChannel + Renderer) |
| Роутить vision-задачи на Anthropic, остальные на Ollama | Раздел 5 (LLM Routing) |
| Добавить новый скил без перезапуска | Раздел 6 (Skills Hot-Reload) |
| Сжать длинную историю чтобы не переполнить контекст | Раздел 7 (Memory Compaction) |
| Ограничить права контейнера на filesystem | Раздел 8 (Sandbox Policy) |
| Написать провайдера для Qwen через Ollama | Раздел 10 (XML Tool Calling) |
| Перенести рабочий агентный цикл из v1 | Раздел 9 + `prompt_loop_executor.py` |
