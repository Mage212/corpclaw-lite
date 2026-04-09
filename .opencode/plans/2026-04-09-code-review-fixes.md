# План исправлений — Код-ревью v2 (апрель 2026)

## Summary

Полная перепроверка 21 претензии из код-ревью. Результат:
- **Подтверждено полностью:** 14
- **Частично подтверждено:** 4
- **Опровергнуто:** 3

Предыдущий план (Phase 0-5) — полностью выполнен. Этот план содержит
**только новые подтверждённые проблемы**, не покрытые предыдущим планом.

## Принятые архитектурные решения

- **Error handling:** Typed exceptions (MemoryError, ToolExecutionError, ContainerIPCError)
- **runner.py:** Полный рефакторинг → TelegramBotOrchestrator
- **ExecScriptTool:** Удалить blocklist полностью (ToolGuard YAML — единственный контроль)
- **Plugin loader:** Subprocess isolation для загрузки tool.py

## Опровергнутые претензии (НЕ исправляются)

| # | Претензия | Причина опровержения |
|---|-----------|---------------------|
| 7 | asyncio вместо anyio | anyio используется только для to_thread.run_sync и Path. asyncio — единственный целевой event loop. Не баг. |
| 8 | Mutable Pydantic defaults | Pydantic v2 deep-copies `[]` и `{}`. Безопасно. |
| 9 | Несогласованные usage keys | Оба провайдера нормализуют к `input_tokens`/`output_tokens`. Никто эти ключи не читает. |
| 11 | arguments=None крашнет ToolGuard | Pydantic ToolCall.arguments — required field. None вызывает ValidationError до check(). |
| 18a | `risk` не используется в loop.py:451 | `risk` используется на следующей строке 452: `risk_level=risk`. |

---

## Phase 1: Quick Wins

> Тривиальные исправления, 0 риск регрессии, 30-60 минут

### 1.1 Удалить мёртвый код

**#18b — `CB_DELETE_OPEN` не используется**
- Файл: `src/corpclaw_lite/channels/telegram/callback_data.py:19`
- Действие: Удалить константу и убрать из `__all__`

**#18c — `fallback_resolver` не используется**
- Файл: `src/corpclaw_lite/extensions/tools/builtin/_path_utils.py:23`
- Действие: Удалить параметр из сигнатуры `resolve_container_path()` и docstring.
  Обновить все вызовы (в `image.py`, `send_file.py`) — убрать передачу аргумента.

### 1.2 Исправить log level

**#12 — `to_docker_args()` логирует WARNING каждый раз**
- Файл: `src/corpclaw_lite/security/network_policy.py:61`
- Действие: Заменить `logger.warning` на `logger.debug`. Известное ограничение,
  не warning. Вызывается при создании контейнера (~1 раз на сессию).

### 1.3 Добавить `from __future__ import annotations`

**#10 — Отсутствует в ~9 значимых файлах (33 всего, но 22 — пустые __init__.py)**

Файлы для исправления:
- `container/policies.py`
- `extensions/plugins/base.py`
- `extensions/plugins/loader.py`
- `extensions/plugins/registry.py`
- `extensions/skills/base.py`
- `extensions/skills/loader.py`
- `extensions/skills/registry.py`
- `extensions/subagents/base.py`
- `extensions/subagents/registry.py`

### 1.4 Статус
- [x] 1.1 Удалить мёртвый код (CB_DELETE_OPEN, fallback_resolver)
- [x] 1.2 Исправить log level в network_policy.py
- [x] 1.3 Добавить future annotations в 9 файлов

---

## Phase 2: Error Infrastructure

> Введение typed exceptions, замена silent swallowing.
> 3-4 часа. Зависимость: Phase 1.

### 2.1 Создать иерархию исключений

Новый файл: `src/corpclaw_lite/exceptions.py`

```python
class CorpClawError(Exception):
    """Base for all CorpClaw typed exceptions."""

class MemoryError(CorpClawError):
    """Raised when a memory/DB operation fails."""

class ToolExecutionError(CorpClawError):
    """Raised when a tool execution fails."""
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(f"Tool '{tool_name}' failed: {message}")

class ContainerIPCError(CorpClawError):
    """Raised when container IPC communication fails."""
    def __init__(self, user_id: int, message: str):
        self.user_id = user_id
        super().__init__(f"Container IPC error (user={user_id}): {message}")
```

### 2.2 Обновить SQLiteMemory — убрать silent swallowing

**#1 — 93 `except Exception` по всей кодовой базе, БД операции теряют данные**

Файл: `src/corpclaw_lite/memory/sqlite.py`

Изменения во всех `_sync_*` методах — заменить `except Exception → return default`
на `raise MemoryError(...) from e`:

| Метод | Строка | Было | Стало |
|-------|--------|------|-------|
| `_sync_add_message` | 100-101 | `except Exception → silent return` | `raise MemoryError(...) from e` |
| `_sync_get_history` | 145-147 | `except Exception → return []` | `raise MemoryError(...) from e` |
| `_sync_clear` | ~158 | `except Exception → silent` | `raise MemoryError(...) from e` |
| `_sync_store_fact` | ~256 | `except Exception → silent` | `raise MemoryError(...) from e` |
| `_sync_replace_oldest` | ~231 | `except Exception → silent` | `raise MemoryError(...) from e` |
| `_sync_clear_facts` | ~305 | `except Exception → silent` | `raise MemoryError(...) from e` |
| `_init_db` | 86-87 | Оставить — ALTER TABLE expected | Оставить, но CRITICAL для других ошибок |

**Callers — обновить (memory МОЖЕТ падать, агент ДОЛЖЕН продолжать):**

| Caller | Файл | Действие |
|--------|------|----------|
| `AgentLoop.run()` | `agent/loop.py` (6 вызовов add_message) | Обернуть в `try/except MemoryError`, логировать error, продолжить |
| `MemoryConsolidator` | `memory/consolidation.py` | Обернуть _summarize вызовы |
| `OnboardingStorage` | `onboarding/storage.py` | Пробросить |
| `UserManager` | `users/manager.py` | Пробросить |

### 2.3 Обновить ContainerIPC — ошибки как exceptions

**#4 — IPC возвращает ошибки как строки (6 мест)**

Файл: `src/corpclaw_lite/container/ipc.py`

| Строка | Было | Стало |
|--------|------|-------|
| 127 | `return f"Error: timed out"` | `raise ContainerIPCError(user_id, ...)` |
| 137 | `return f"Container execution error: ..."` | `raise ContainerIPCError(user_id, ...)` |
| 147 | `return f"Error from container: ..."` | `raise ContainerIPCError(user_id, ...)` |
| 157 | `return f"Error: Invalid JSON..."` | `raise ContainerIPCError(user_id, ...)` |
| 160 | `return "Error: Security verification failed"` | `raise ContainerIPCError(user_id, ...)` |
| 163 | `return f"Error in Container IPC: {e}"` | `raise ContainerIPCError(user_id, ...)` |

Файл: `src/corpclaw_lite/container/proxy.py`
- Строка 81: `"Error: Cannot dispatch..."` → `raise ContainerIPCError(...)`
- Строка 84: пробросить исключение как есть

Файл: `src/corpclaw_lite/agent/loop.py`
- Добавить `except ContainerIPCError` в `_execute_single_tool` (рядом с `ToolGuardError`)

### 2.4 Статус
- [x] 2.1 Создать `exceptions.py`
- [x] 2.2 Обновить `sqlite.py` — raise вместо silent return
- [x] 2.3 Обновить `ipc.py` + `proxy.py` — raise вместо return string
- [x] 2.4 Обновить callers: `loop.py`, `consolidation.py`, `storage.py`, `manager.py`
- [x] 2.5 Обновить/добавить тесты

---

## Phase 3: Safety & Bug Fixes

> Реальные баги и проблемы безопасности.
> 3-4 часа. Зависимость: Phase 2 (exceptions).

### 3.1 Timeout на subagent dispatch

**#3 — Нет timeout (ЧАСТИЧНО: BudgetGuard между итерациями есть, но зависший tool.execute() — нет)**

Файл: `src/corpclaw_lite/agent/subagent.py`

Обернуть `loop.run()` (строка 90):
```python
try:
    result, _ = await asyncio.wait_for(
        loop.run(user, task_context, system_prompt=system_prompt),
        timeout=self._settings.max_time_ms / 1000 * 2,  # 2x budget guard
    )
except TimeoutError:
    return "Subagent error: execution timed out"
```

Файл: `src/corpclaw_lite/extensions/tools/builtin/dispatch.py`
Аналогично обернуть `self._dispatcher.dispatch()` (строка 62).

### 3.2 PluginRegistry.unregister()

**#14 — PluginHotReloader лезет в `_plugins` напрямую**

Файл: `src/corpclaw_lite/extensions/plugins/registry.py`
Добавить:
```python
def unregister(self, plugin_name: str) -> None:
    """Remove a plugin by name (no-op if not found)."""
    self._plugins.pop(plugin_name, None)
```

Файл: `src/corpclaw_lite/extensions/plugins/watcher.py`
Заменить строку 153:
```python
# Было:
self._plugin_registry._plugins.pop(plugin_name, None)  # type: ignore[attr-defined]
# Стало:
self._plugin_registry.unregister(plugin_name)
```

### 3.3 SkillMatcher: stale index при изменении контента

**#19 — Реальный баг: `_ensure_index` проверяет только IDs, не контент**

Файл: `src/corpclaw_lite/extensions/skills/matcher.py`

Изменить `_ensure_index` (строка 349):
```python
def _ensure_index(self, skills: list[Skill]) -> None:
    current_ids = frozenset(s.id for s in skills)
    if current_ids == self._indexed_ids and self._docs:
        current_digest = hash(tuple(
            (s.id, s.description, s.instructions[:300])
            for s in skills
        ))
        if current_digest == self._content_digest:
            return
    self._rebuild_index(skills)
```

Добавить `self._content_digest: int = 0` в `__init__`, обновлять в конце `_rebuild_index`.

### 3.4 MCP Client concurrency protection

**#16 — Конкурентные вызовы ломают JSON-RPC протокол**

Файл: `src/corpclaw_lite/extensions/mcp/client.py`

Добавить `self._request_lock = asyncio.Lock()` в `__init__`.
Обернуть `_send_request_inner` в lock:
```python
async def _send_request(self, method, params):
    try:
        async with self._request_lock:
            return await asyncio.wait_for(
                self._send_request_inner(method, params),
                timeout=self._total_timeout,
            )
    except TimeoutError as e:
        raise MCPClientError(...) from e
```

### 3.5 Sanitize caption в build_agent_directive

**#15 — Prompt injection через caption (подтверждено, низкий риск)**

Файл: `src/corpclaw_lite/channels/telegram/upload.py`

Добавить функцию санитизации и применить к `relative_path` и `caption`:
```python
def _sanitize_for_prompt(text: str, max_len: int = 500) -> str:
    return text[:max_len].replace("'", "").replace("\n", " ")
```

### 3.6 Удалить ExecScriptTool blocklist

**#13 — Blocklist обходится, создаёт ложное чувство безопасности**

Файл: `src/corpclaw_lite/extensions/tools/builtin/exec_script.py`

- Удалить `BLOCKED_PATTERNS` (~строки 31-54)
- Удалить цикл проверки (~строки 83-91)
- Добавить docstring: security relies on ToolGuard YAML + container isolation
- Container isolation + ToolGuard YAML — единственные линии защиты

### 3.7 Статус
- [x] 3.1 Timeout на subagent dispatch (subagent.py + dispatch.py)
- [x] 3.2 PluginRegistry.unregister() (registry.py + watcher.py)
- [x] 3.3 SkillMatcher stale index fix (matcher.py)
- [x] 3.4 MCP Client concurrency lock (client.py)
- [x] 3.5 Sanitize caption (upload.py)
- [x] 3.6 Удалить ExecScriptTool blocklist (exec_script.py)
- [x] 3.7 Обновить/добавить тесты

---

## Phase 4: Architecture Refactoring

> Крупные структурные изменения. 8-12 часов.
> Зависимость: Phase 2 и 3.

### 4.1 Централизовать project root

**#5 — `Path(__file__).parent.parent.parent.parent` в 4 файлах**

Новый файл: `src/corpclaw_lite/paths.py`

```python
import os
from pathlib import Path

def get_project_root() -> Path:
    env = os.environ.get("CORPCLAW_ROOT", "")
    if env:
        return Path(env)
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    raise RuntimeError("Cannot find project root (no pyproject.toml found)")

PROJECT_ROOT = get_project_root()
DATA_DIR = Path(os.environ.get("CORPCLAW_DATA_DIR", "") or PROJECT_ROOT / "data")
```

Обновить 4 файла:
- `agent/factory.py:49` → `from corpclaw_lite.paths import PROJECT_ROOT`
- `config/loader.py:41` → `from corpclaw_lite.paths import PROJECT_ROOT`
- `memory/sqlite.py:24` → `from corpclaw_lite.paths import DATA_DIR`
- `container/policies.py:57` → `from corpclaw_lite.paths import PROJECT_ROOT`

### 4.2 Рефакторинг runner.py → TelegramBotOrchestrator

**#6 — God function 428 строк, 5-6 вложенных замыканий**

Создать: `src/corpclaw_lite/channels/telegram/orchestrator.py`

```python
class TelegramBotOrchestrator:
    def __init__(self, token: str, settings: Settings): ...

    async def start(self) -> None:
        """Build agent stack, wire components, start bot."""
        # Извлечь из run_telegram_bot() строки 28-107

    async def stop(self) -> None:
        """Graceful shutdown in correct order."""
        # Извлечь из run_telegram_bot() строки 407-455

    async def handle_message(self, update, context) -> None:
        """Main message handler — заменяет _handle_and_reply."""
        # Извлечь из run_telegram_bot() строки 109-272

    async def handle_image(self, update, context) -> None:
        """Image handler — заменяет _on_image."""
        # Извлечь из run_telegram_bot() строки 286-320

    async def send_file_callback(self, chat_id, file_path, caption) -> None:
        """File delivery — заменяет _send_file_cb."""
        # Извлечь из run_telegram_bot() строки 336-338
```

Обновить:
- `channels/telegram/__init__.py` — реэкспорт
- `channels/telegram/runner.py` — thin wrapper (5-10 строк)
- `cli.py` — обновить `cmd_telegram()`

### 4.3 Исправить split_message MarkdownV2 expansion

**#17 — Split до конвертации может превысить 4096 символов**

Файл: `src/corpclaw_lite/channels/telegram/formatting.py`

Конвертировать MarkdownV2 **до** split:
```python
def build_response_parts(text: str) -> list[str]:
    converted = convert_markdown_tables(text)
    try:
        markdownified = _markdownify(converted)
    except Exception:
        markdownified = converted
    return split_message(markdownified, max_length=3800)
```

Обновить `channel.py send_message()`:
- Принимать уже конвертированный текст
- Убрать повторную конвертацию
- Fallback: resend без parse_mode (уже есть)

### 4.4 Статус
- [x] 4.1 Создать `paths.py`, обновить 4 файла
- [x] 4.2 Создать `orchestrator.py`, обновить runner/runner.py/cli.py
- [x] 4.3 Исправить `split_message` в formatting.py + channel.py
- [x] 4.4 Обновить/добавить тесты

---

## Phase 5: Plugin Subprocess Isolation

> Загрузка plugin tool.py в subprocess для sandbox.
> 6-8 часов. Зависимость: Phase 2, 4.

### 5.1 Дизайн

Текущий flow (опасный):
```
PluginLoader.load_plugin()
  → importlib → exec_module(tool.py)  ← ВЫПОЛНЕНИЕ В MAIN PROCESS
  → ToolClass()  ← ЭКЗЕМПЛЯР В MAIN PROCESS
```

Новый flow:
```
PluginLoader.load_plugin()
  → subprocess: python -m corpclaw_lite.extensions.plugins.sandbox_worker <tool_path>
    → introspect schema → print JSON to stdout
  → main process: PluginToolProxy(schema, subprocess_channel)
  → registry.register(proxy)

PluginToolProxy.execute(**kwargs):
  → JSON-RPC stdin → subprocess → tool.execute(**kwargs) → stdout
  → main process: read result
```

### 5.2 Новые файлы

**`src/corpclaw_lite/extensions/plugins/sandbox_worker.py`** (~100 строк)
- Точка входа: `python -m corpclaw_lite.extensions.plugins.sandbox_worker <tool_path>`
- importlib load → introspect schema → JSON-RPC stdin/stdout
- Команды: `introspect` (schema), `execute` (tool call)

**`src/corpclaw_lite/extensions/plugins/sandbox_proxy.py`** (~80 строк)
- `PluginToolProxy(Tool)` — proxy, делегирует в subprocess
- Schema получена при introspect, хранится в proxy

### 5.3 Обновить PluginLoader

Файл: `src/corpclaw_lite/extensions/plugins/loader.py`
- Заменить прямой importlib на spawn subprocess + introspect + register proxy
- `force_reload`: kill subprocess → respawn

### 5.4 Жизненный цикл

- Subprocess запускается при первой загрузке плагина
- Перезапускается при hot-reload (watcher)
- Убивается при shutdown
- Timeout на каждый execute call

### 5.5 Статус
- [x] 5.1 Создать `sandbox_worker.py`
- [x] 5.2 Создать `sandbox_proxy.py`
- [x] 5.3 Обновить `loader.py`
- [x] 5.4 Обновить `watcher.py` (subprocess lifecycle)
- [x] 5.5 Обновить/добавить тесты

---

## Phase 6: Configurability (опционально)

> Вынести hardcoded значения в settings. 1-2 часа.

### 6.1 Configurable limits

| Значение | Файл | Где хранить |
|----------|------|-------------|
| Health port 8080 | `logging/health.py:42` | `Settings.health_port: int = 8080` |
| Progress 2s transition | `telegram/progress.py:147` | Constructor param `thinking_timeout: float = 2.0` |
| SearchFilesTool 100 | `tools/builtin/files.py:287` | Class attribute `max_results: int = 100` |
| exec_script 50KB | `tools/builtin/exec_script.py:25` | OK — уже именованная константа |

### 6.2 Статус
- [x] 6.1 Health port → settings
- [x] 6.2 Progress transition → constructor param
- [x] 6.3 SearchFilesTool limit → class attribute

---

## Порядок выполнения

```
Phase 1 (Quick Wins)     ← 1 час, 0 риск
    ↓
Phase 2 (Error Infra)    ← 3-4 часа, много файлов
    ↓
Phase 3 (Safety/Bugs)    ← 3-4 часа, зависит от Phase 2
    ↓
Phase 4 (Architecture)   ← 8-12 часов, самый объёмный
    ↓
Phase 5 (Plugin Sandbox) ← 6-8 часов, самый сложный
    ↓
Phase 6 (Config)         ← 1-2 часа, опционально
```

**Проверка после каждой фазы:**
```bash
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

## Не исправляется (оправданно)

| # | Проблема | Почему |
|---|----------|--------|
| 7 | asyncio vs anyio | Не баг, asyncio — единственный target |
| 8 | Mutable Pydantic defaults | Безопасно в Pydantic v2 |
| 9 | Usage keys | Уже нормализованы, никто не читает |
| 11 | arguments=None | Pydantic validation barrier |
| 18a | Unused `risk` | Переменная используется |
| 20a | Health port hardcoded | Default parameter, конфигурируемый |
| 20d | exec_script 50KB | Именованная константа, OK |
| 21 | _SkillDoc not frozen | Frozen сломает rebuild, риск нулевой |
