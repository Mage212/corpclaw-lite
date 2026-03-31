# Патчноут: Исправление находок аудита CorpClaw Lite

**Дата:** 2026-04-01
**Аудит от:** 2026-03-31
**Статус:** Завершён

---

## Обзор

Проведён полный аудит кодовой базы CorpClaw Lite (428 тестов, 77% покрытие, 80 исходных файлов). Выявлено 12 находок. По результатам верификации: **1 опровергнута**, **7 исправлены**, **4 признаны осознанными trade-offs** (из них 1 отмечена для проработки в будущем).

### Метрики до и после

| Метрика | До | После |
|---------|-----|-------|
| ruff errors | 0 | 0 |
| pyright errors (strict) | 0 | 0 |
| pytest | 428 passed | 428 passed |
| `type: ignore` подавлений | 1 (`attr-defined` в factory.py) | **0** |
| Дублированный код | `_db_connect` в 2 файлах (34 строки) | 0 |
| Файлов-исходников | 80 | 83 (+3 новых) |

---

## Исправленные находки

### H-1: Дедупликация `_db_connect` (High → Fixed)

**Проблема:** Идентичный context manager `_db_connect` (17 строк) дублировался в `memory/sqlite.py:28–44` и `users/manager.py:23–39`. При изменении логики подключения нужно было править оба файла.

**Решение:** Создан модуль `src/corpclaw_lite/utils/db.py` с единственной функцией `db_connect()`. Оба потребителя переведены на импорт из нового модуля. Удалены неиспользуемые импорты (`Generator`, `contextmanager`).

**Затронутые файлы:**
- `[NEW]` `src/corpclaw_lite/utils/__init__.py`
- `[NEW]` `src/corpclaw_lite/utils/db.py`
- `[MOD]` `src/corpclaw_lite/memory/sqlite.py` — удалён `_db_connect`, 10 вызовов → `db_connect`
- `[MOD]` `src/corpclaw_lite/users/manager.py` — удалён `_db_connect`, 5 вызовов → `db_connect`

---

### H-2: Реальный idle tracking для контейнеров (High → Fixed)

**Проблема:** `ContainerManager.prune_idle()` использовал `container.attrs["State"]["StartedAt"]` (время запуска контейнера) как proxy для idle time. Контейнер, запущенный 2 часа назад, но получивший tool call 5 секунд назад, считался idle и мог быть убит.

**Решение:**
1. `ContainerIPC` получил `_last_used: dict[int, float]` — обновляется при каждом `send_tool_call()` через `time.monotonic()`.
2. `ContainerManager` получил параметр `ipc: ContainerIPC | None` в конструктор.
3. `prune_idle()` теперь вызывает `_get_idle_seconds()`, который проверяет IPC `get_last_used()` первым, и лишь при отсутствии данных фолбечит на Docker `StartedAt`.
4. Datetime-парсинг вынесен в приватный `_get_idle_seconds()`.
5. В `factory.py` IPC создаётся до `ContainerManager` и передаётся ему.

**Дизайн-решение:** `time.monotonic()` вместо `time.time()` — не зависит от NTP sync и изменения системных часов.

**Затронутые файлы:**
- `[MOD]` `src/corpclaw_lite/container/ipc.py` — `_last_used` dict, `get_last_used()` метод
- `[MOD]` `src/corpclaw_lite/container/manager.py` — `ipc` параметр, `_get_idle_seconds()`
- `[MOD]` `src/corpclaw_lite/agent/factory.py` — порядок создания IPC/ContainerManager

---

### H-3: Единый `IPCAuth` в `agent_worker.py` (High → Fixed, severity снижена до Low)

**Проблема:** В `process_request()` создавалось два экземпляра `IPCAuth()` — один для `verify()` в try, другой для `sign()` в finally. Каждый имел свой `_seen_nonces`. Технически не баг (sign не проверяет nonces), но code smell.

**Решение:** Один `IPCAuth` создаётся в try. В finally проверяется `if auth is None: auth = IPCAuth()` — fallback для случая, когда ошибка произошла до создания экземпляра.

**Затронутые файлы:**
- `[MOD]` `src/corpclaw_lite/container/agent_worker.py`

---

### M-3: Типизированный Docker exception catch (Medium → Fixed)

**Проблема:** В `ContainerManager` ошибки Docker определялись по строке:
```python
if "404" not in str(_e) and "Not Found" not in str(_e):
```
Хрупко — зависит от текста ошибки сторонней библиотеки. Другие 404-ошибки могли быть поглощены.

**Решение:** Замена на `isinstance(_e, docker.errors.NotFound)` с проверкой `docker is not None` (docker — optional dependency). Исправлено в двух местах: `ensure_running()` и `stop()`.

**Затронутые файлы:**
- `[MOD]` `src/corpclaw_lite/container/manager.py` — 2 блока except

---

### M-4: Убран `setattr` hack для `container_manager` (Medium → Fixed)

**Проблема:** `build_agent_stack()` прикреплял `container_manager` к `user_manager` через:
```python
user_manager._container_manager = container_manager  # type: ignore[attr-defined]
```
`UserManager` не имел этого атрибута в `__init__`. Потребители доставали его через `getattr(user_manager, "_container_manager", None)` — нетипизировано.

**Решение:** Return type расширен с 4-tuple до 5-tuple:
```python
def build_agent_stack() -> tuple[
    AgentLoop, UserManager, ToolRegistry, MCPManager | None, ContainerManager | None
]:
```
`setattr` и все `getattr` удалены. `ContainerManager` добавлен в `TYPE_CHECKING` блок.

**Затронутые файлы:**
- `[MOD]` `src/corpclaw_lite/agent/factory.py` — 5-tuple return, удалён `type: ignore`
- `[MOD]` `src/corpclaw_lite/channels/telegram/runner.py` — 5-tuple распаковка
- `[MOD]` `src/corpclaw_lite/cli.py` — 5-tuple распаковка, удалён `getattr`
- `[MOD]` `tests/test_factory.py` — 7 мест обновлено: `_, _, _, _` → `_, _, _, _, _`

---

### M-5: Корректный shutdown Health server (Medium → Fixed)

**Проблема:** `run_health_server()` создавал `web.AppRunner` и `TCPSite`, но не возвращал runner. При `task.cancel()` в finally корутина прерывалась, но `AppRunner.cleanup()` не вызывался — TCP-сокет мог не освободиться. При быстром рестарте: `Address already in use` на порту 8080.

**Решение:**
1. `run_health_server()` теперь возвращает `AppRunner` (тип `Any` — aiohttp untyped).
2. В `runner.py` заменён `create_task` на прямой `await` (не блокирует, т.к. `TCPSite.start()` только регистрирует сокет в event loop).
3. В `finally` добавлен `await _health_runner.cleanup()`.
4. Бонус: ошибки bind (порт занят) теперь видны сразу при запуске, а не проглатываются.

**Затронутые файлы:**
- `[MOD]` `src/corpclaw_lite/logging/health.py` — return `AppRunner`
- `[MOD]` `src/corpclaw_lite/channels/telegram/runner.py` — `await` + `cleanup()` в finally
- `[MOD]` `tests/test_llm_advanced.py` — `assert runner is not None`

---

### L-1: Вынос шаблонов из `cli.py` (Low → Fixed)

**Проблема:** 4 строковых шаблона (`_SKILL_TEMPLATE`, `_PLUGIN_MANIFEST_TEMPLATE`, `_PLUGIN_SKILL_TEMPLATE`, `_SUBAGENT_TEMPLATE`) — 68 строк статического текста — лежали в `cli.py`. Логически не связаны с CLI-парсингом.

**Решение:** Вынесены в `src/corpclaw_lite/templates.py`. `cmd_generate()` импортирует их lazy (в теле функции).

**Затронутые файлы:**
- `[NEW]` `src/corpclaw_lite/templates.py`
- `[MOD]` `src/corpclaw_lite/cli.py` — удалены inline-шаблоны, lazy import

---

## Опровергнутая находка

### M-2: Оценка токенов для кириллицы (Medium → Опровергнуто)

**Суть претензии:** `_estimate_tokens()` в `compressor.py` использует `len(content.encode("utf-8")) // 4` — якобы неточно для кириллицы.

**Почему опровергнуто:** Комментарий в коде (строки 239–241) объясняет: `bytes // 4` **намеренно** даёт завышенную оценку для кириллицы (2 bytes / 4 ≈ 0.5 «токена» на символ). Это **защитная** стратегия: лучше сжать контекст раньше, чем получить неожиданный обрыв. Альтернатива `len(text) // 4` занижала бы и вызывала бы context limit hits. Это осознанный и задокументированный trade-off.

---

## Пропущенные находки (осознанные trade-offs)

### M-1: `MCPHotReloader` аннотация до импорта → Пропущено навсегда

Аннотация `mcp_reloader: MCPHotReloader | None = None` на строке 269 runner.py стоит до `from ... import MCPHotReloader` на строке 271. Благодаря `from __future__ import annotations` аннотация — строка, не runtime-ссылка. Никакого эффекта на выполнение. Исправлять никогда не надо.

### L-2: `channel.py` 497 строк → Пропущено до органического роста

19 методов в одном классе `TelegramChannel`. Это цельный протокол от `/start` до `/error`. Делить искусственно — ухудшение. Исправлять при >700 строк или при добавлении voice/video/webhook handler.

### L-3: Magic strings в `NormalizeExcelTool` → Пропущено до интернационализации

Захардкоженные `"инн"`, `"дата"`, `"%d.%m.%Y"`, INN длины 10/12. При добавлении KZ/BY/UZ ИНН — вынести в `config/excel_normalization.yaml`.

---

## Будущая работа: L-4 — Global mutable state в `health.py`

### Текущее состояние

```python
# health.py:14–15
_start_time = time.time()          # фиксируется при import модуля
_counters: Counter[str] = Counter() # глобальный изменяемый Counter
```

`increment("requests")` вызывается из `runner.py` и `channel.py`. `get_stats()` возвращает словарь с `uptime_seconds`, `requests`, `tool_calls`, `errors` — отдаётся через `/health` endpoint.

### Почему это работает сейчас

CorpClaw Lite — **single-process** приложение. Один Python-процесс → один event loop → один набор глобальных переменных. `_counters` потокобезопасен (GIL + asyncio single-thread). `_start_time` неизменяем после инициализации.

### Когда станет проблемой

1. **Multi-worker deployment** (gunicorn/uvicorn с workers > 1): каждый worker получит свой `_counters`. Health endpoint покажет метрики только одного worker. Uptime каждого worker начнётся с момента fork, а не запуска приложения.

2. **Тесты на метрики**: если потребуется тестировать `increment()` / `get_stats()`, счётчики будут накапливаться между тестами. `Counter` нельзя сбросить без прямого доступа к `_counters`.

3. **Перезагрузка модуля**: при hot-reload (если его делать для health.py) `_start_time` сбросится, uptime обнулится.

### Рекомендуемый план исправления

**Вариант A — минимальный (тесты):**
```python
def reset_stats() -> None:
    """Reset all counters. For testing only."""
    global _start_time
    _start_time = time.time()
    _counters.clear()
```
Добавить `reset_stats()` в `__all__`, вызывать в `conftest.py` фикстурах. Времязатраты: 15 минут.

**Вариант B — полный (multi-worker + Prometheus):**
```python
# Заменить Counter на prometheus_client
from prometheus_client import Counter as PromCounter, Gauge

UPTIME = Gauge("corpclaw_uptime_seconds", "Process uptime")
REQUESTS = PromCounter("corpclaw_requests_total", "Total requests processed")
TOOL_CALLS = PromCounter("corpclaw_tool_calls_total", "Total tool calls")
ERRORS = PromCounter("corpclaw_errors_total", "Total errors")
```
Prometheus агрегирует метрики с нескольких workers через свой scraping. Требуется: `uv add prometheus_client`, изменение `health.py`, обновление `run_health_server` для добавления `/metrics` endpoint. Времязатраты: 1–2 часа.

**Триггер для действия:** появление `workers > 1` в конфигурации deployment или добавление CI-теста, который проверяет значения метрик.

---

## Полная сводка изменений

### По файлам

| Действие | Файл | Находки |
|----------|------|---------|
| NEW | `src/corpclaw_lite/utils/__init__.py` | H-1 |
| NEW | `src/corpclaw_lite/utils/db.py` | H-1 |
| NEW | `src/corpclaw_lite/templates.py` | L-1 |
| MOD | `src/corpclaw_lite/memory/sqlite.py` | H-1 |
| MOD | `src/corpclaw_lite/users/manager.py` | H-1 |
| MOD | `src/corpclaw_lite/container/agent_worker.py` | H-3 |
| MOD | `src/corpclaw_lite/container/ipc.py` | H-2 |
| MOD | `src/corpclaw_lite/container/manager.py` | M-3 + H-2 |
| MOD | `src/corpclaw_lite/logging/health.py` | M-5 |
| MOD | `src/corpclaw_lite/agent/factory.py` | H-2 + M-4 |
| MOD | `src/corpclaw_lite/channels/telegram/runner.py` | M-5 + M-4 |
| MOD | `src/corpclaw_lite/cli.py` | L-1 + M-4 |
| MOD | `tests/test_factory.py` | M-4 |
| MOD | `tests/test_llm_advanced.py` | M-5 |

### По находкам

| # | Severity | Статус | Влияние |
|---|----------|--------|---------|
| H-1 | High | ✅ Fixed | Maintainability — единый source of truth |
| H-2 | High→Medium | ✅ Fixed | Container lifecycle — реальный idle tracking |
| H-3 | High→Low | ✅ Fixed | Code clarity — один IPCAuth экземпляр |
| M-1 | Medium→Low | ⏭️ Skip | Работает через `from __future__` |
| M-2 | Medium | ❌ Refuted | Намеренный и корректный trade-off |
| M-3 | Medium | ✅ Fixed | Robustness — типизированный exception catch |
| M-4 | Medium | ✅ Fixed | Type safety — убран `setattr` + `type: ignore` |
| M-5 | Medium | ✅ Fixed | Clean shutdown — `AppRunner.cleanup()` |
| L-1 | Low | ✅ Fixed | Code organization — шаблоны в отдельном модуле |
| L-2 | Low | ⏭️ Skip | Наблюдение — делить при росте >700 LOC |
| L-3 | Low | ⏭️ Skip | MVP — менять при интернационализации |
| L-4 | Low | ⚠️ Backlog | Global state — исправлять при multi-worker |
