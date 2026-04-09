# План исправлений по результатам код-ревью

## Summary

Исправление всех 17 подтверждённых замечаний из код-ревью CorpClaw Lite.
Приоритет: высокий → средний → низкий. Каждый шаг — атомарный коммит.

## Goals

- Исправить все подтверждённые замечания без нарушения существующих 585 тестов
- Сохранить 0 ошибок в ruff и pyright strict
- Не ухудшить читаемость кода

## Steps

### Phase 1: Высокий приоритет

#### Step 1.1 — SQLite vacuum/cleanup (#23)

**Проблема:** Нет механизма очистки SQLite. DELETE не освобождает место, WAL растёт.
**Файлы:** `src/corpclaw_lite/memory/sqlite.py`

**Действия:**
1. Добавить метод `_sync_vacuum(self) -> None` — выполняет `PRAGMA wal_checkpoint(TRUNCATE)` + `VACUUM`
2. Добавить async-обёртку `vacuum(self) -> None`
3. Вызывать vacuum в `replace_oldest()` — после удаления старых сообщений, с throttling (не чаще раза в час). Добавить `_last_vacuum: dict[str, float]` и константу `_VACUUM_INTERVAL = 3600`
4. Добавить CLI-команду `corpclaw-lite prune --vacuum` для ручного запуска

**Тесты:** Добавить тест на vacuum в `tests/test_memory.py`

---

### Phase 2: Средний приоритет

#### Step 2.1 — Вынести mem_key в User.memory_key() (#3)

**Проблема:** Логика mem_key (`str(user.telegram_id) if user.telegram_id else str(user.id)`) дублируется.
**Файлы:** `src/corpclaw_lite/users/models.py`, `src/corpclaw_lite/agent/loop.py`, `src/corpclaw_lite/channels/telegram/orchestrator.py`, `src/corpclaw_lite/onboarding/finalizer.py`

**Действия:**
1. Добавить метод `memory_key(self) -> str` в `User` в `users/models.py`
2. Заменить все `str(user.telegram_id) if user.telegram_id else str(user.id)` на `user.memory_key()`
3. Заменить `mem_key = str(user.telegram_id)` в orchestrator.py на `user.memory_key()`
4. Заменить `str(user_id)` в finalizer.py на `user.memory_key()` (где user доступен)

**Тесты:** Добавить тест на `User.memory_key()` в `tests/test_types.py` или отдельный файл

#### Step 2.2 — Буферизация stdout/stderr в exec_script (#6)

**Проблема:** `proc.communicate()` читает весь вывод в память — потенциальный OOM.
**Файлы:** `src/corpclaw_lite/extensions/tools/builtin/exec_script.py`

**Действия:**
1. Заменить `proc.communicate()` на потоковое чтение через `asyncio.StreamReader`
2. Создать `_read_stream_limited(stream, max_bytes)` — читает из потока чанками, обрезает при превышении `MAX_OUTPUT_BYTES`
3. Использовать `asyncio.gather` для параллельного чтения stdout и stderr с лимитом

**Приблизительный код:**
```python
async def _read_stream_limited(
    stream: asyncio.StreamReader | None,
    max_bytes: int,
) -> str:
    if stream is None:
        return ""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            chunks.append(chunk[: max_bytes - total + len(chunk)])
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
```

**Тесты:** Добавить тест на huge output в `tests/test_exec_script.py`

#### Step 2.3 — Тесты на race conditions для parallel approval (#15)

**Проблема:** `_approval_lock` в `loop.py` не покрыт тестами на конкурентный доступ.
**Файлы:** `tests/test_agent_loop.py`

**Действия:**
1. Добавить тест `test_parallel_tool_approval_serialized`: два tool_call, оба требуют approval, проверить что `_approval_lock` сериализует вызовы (не более 1 одновременно)
2. Использовать `asyncio.Event` для детекции concurrency
3. Добавить тест `test_parallel_tool_one_approved_one_denied`: два tool_call, один одобрен, другой отклонён

#### Step 2.4 — Добавить development в departments.yaml (#17)

**Проблема:** `config/bootstrap/departments/development.md` существует, но нет entry в `departments.yaml`.
**Файлы:** `config/departments.yaml`

**Действия:**
1. Добавить секцию `development` в `departments.yaml` — скопировать из `engineering` (full access `"*"`), но с описанием "Software development team"
2. Это консистентно с существующим `development.md` bootstrap

---

### Phase 3: Низкий приоритет

#### Step 3.1 — AgentConfig dataclass (#1)

**Проблема:** `AgentLoop.__init__` принимает 10 параметров.
**Файлы:** `src/corpclaw_lite/agent/loop.py`, `src/corpclaw_lite/agent/factory.py`, `src/corpclaw_lite/agent/subagent.py`, `tests/test_agent_loop.py`, `tests/test_subagents.py`, `tests/test_factory.py`

**Действия:**
1. Создать dataclass `AgentConfig` в `loop.py`:
```python
@dataclass
class AgentConfig:
    provider: Provider
    registry: ToolRegistry
    settings: AgentSettings
    permission_checker: PermissionChecker | None = None
    tool_guard: ToolGuard | None = None
    memory: SQLiteMemory | None = None
    approval_callback: Callable[[str, str], Awaitable[bool]] | None = None
    consolidator: MemoryConsolidator | None = None
    compressor: ContextCompressor | None = None
    default_system_prompt: str | None = None
```
2. `AgentLoop.__init__` принимает `config: AgentConfig`
3. Обновить все вызовы: `factory.py`, `subagent.py`, тесты
4. `_approval_lock` создаётся внутри `__init__` как раньше

**Тесты:** Обновить все тесты, создающие `AgentLoop` — использовать `AgentConfig`

#### Step 3.2 — Вынести PLACEHOLDER в константный модуль (#2)

**Проблема:** Поздний импорт `from corpclaw_lite.agent.compressor import PLACEHOLDER` в `context.py`.
**Файлы:** `src/corpclaw_lite/agent/compressor.py`, `src/corpclaw_lite/agent/context.py`

**Действия:**
1. Создать `src/corpclaw_lite/agent/constants.py` с `PLACEHOLDER = "[Old tool output cleared to save context space]"`
2. В `compressor.py`: `from corpclaw_lite.agent.constants import PLACEHOLDER`
3. В `context.py`: `from corpclaw_lite.agent.constants import PLACEHOLDER` (top-level import)

#### Step 3.3 — Константа для subagent timeout (#4)

**Проблема:** Магическое число `* 2` в subagent.py.
**Файлы:** `src/corpclaw_lite/agent/subagent.py`

**Действия:**
1. Добавить константу `_SUBAGENT_TIMEOUT_MULTIPLIER = 2` с комментарием: `# Subagent gets 2x the main loop timeout because it starts from scratch with its own budget guard`
2. Заменить `self._settings.max_wall_time_ms / 1000 * 2` на `self._settings.max_wall_time_ms / 1000 * _SUBAGENT_TIMEOUT_MULTIPLIER`

#### Step 3.4 — Документировать Path.cwd() в resolve_and_validate_path (#8)

**Проблема:** `Path.cwd()` как workspace root — неочевидно.
**Файлы:** `src/corpclaw_lite/extensions/tools/builtin/files.py`

**Действия:**
1. Обновить docstring `resolve_and_validate_path`: добавить `Note: When container isolation is enabled, this function runs inside the container where CWD=/workspace. In dev mode (container.enabled=false), CWD is the process working directory.`
2. Это документирование, не изменение логики

#### Step 3.5 — Исправить _normalize_header для кириллицы (#9)

**Проблема:** `_normalize_header` даёт `инн_сотрудника` вместо snake_case.
**Файлы:** `src/corpclaw_lite/extensions/tools/builtin/excel.py`

**Действия:**
1. Обновить docstring: `"""Normalize a column header. For Latin headers, produces snake_case. For Cyrillic headers, preserves characters with underscores for spaces."""`
2. Это документирующее изменение — реальная транслитерация сломала бы LLM, который ожидает кириллические имена колонок (типичные в российских Excel)
3. Альтернатива: переименовать функцию в `_clean_header` для точности

#### Step 3.6 — Разбить excel.py execute на подметоды (#10)

**Проблема:** `noqa: C901` — метод слишком сложный.
**Файлы:** `src/corpclaw_lite/extensions/tools/builtin/excel.py`

**Действия:**
1. Вынести `_normalize_headers(ws, total_cols, total_rows, do_headers) -> dict[str, int]` — возвращает col_types + stats
2. Вынести `_process_data_rows(ws, total_rows, total_cols, col_types, do_dedup, do_empty) -> tuple[list[int], dict[str, int]]` — возвращает rows_to_delete + stats
3. Вынести `_bulk_delete_rows(ws, rows_to_delete) -> None`
4. `execute` вызывает подметоды последовательно

#### Step 3.7 — Переименовать coverage-padding тесты (#16)

**Проблема:** `test_more_coverage.py` и `test_coverage_extras.py` — бессмысленные имена.
**Файлы:** `tests/test_more_coverage.py`, `tests/test_coverage_extras.py`

**Действия:**
1. `test_more_coverage.py` — проанализировать содержимое и переименовать по фактическому покрытию (TelegramChannel dummy methods, DeleteBrowserHandler callback)
2. `test_coverage_extras.py` — переименовать по содержимому (DepartmentManager, CLIChannel, container policies, XML tool calling, path utils, runtime shutdown)
3. Возможные имена: `test_telegram_extras.py`, `test_misc_modules.py` или распределить тесты по существующим файлам

#### Step 3.8 — Уточнить network_policy.yaml комментарий (#18)

**Проблема:** `api.anthropic.com` в allowlist при Ollama default — непонятно зачем.
**Файлы:** `config/network_policy.yaml`

**Действия:**
1. Добавить комментарий: `# api.anthropic.com — for cloud consolidation fallback (if configured). Safe to keep even when using Ollama — container only connects if code explicitly requests it.`

#### Step 3.9 — Улучшить get_project_root fallback (#19)

**Проблема:** При pip install pyproject.toml может не найтись.
**Файлы:** `src/corpclaw_lite/paths.py`

**Действия:**
1. Добавить fallback: если обход вверх не нашёл pyproject.toml — вернуть `Path(__file__).resolve().parent.parent` (это `src/`, а для editable install — `corpclaw-lite/`)
2. Это менее точно но лучше чем RuntimeError
3. Оставить warning log при использовании fallback

#### Step 3.10 — Добавить ruff правила A и PERF (#20)

**Проблема:** Нет правил `A` (builtins shadowing) и `PERF` (performance anti-patterns).
**Файлы:** `pyproject.toml`

**Действия:**
1. Добавить `A` и `PERF` в `select` в `[tool.ruff.lint]`
2. Запустить `ruff check src/ --fix` и исправить любые новые предупреждения
3. Проверить что все тесты проходят

#### Step 3.11 — Улучшить диагностику agent_worker (#22)

**Проблема:** Import error в контейнере трудно диагностировать.
**Файлы:** `src/corpclaw_lite/container/agent_worker.py`

**Действия:**
1. Обернуть `_build_container_registry()` в try/except
2. При ImportError — логировать полную ошибку в stderr и вернуть ошибку в response payload
3. Это позволит хосту видеть `"Container tool import failed: No module named 'openpyxl'"` вместо generic "Unknown tool"

#### Step 3.12 — Улучшить onboarding finalizer prompt (#24)

**Проблема:** Локальные LLM могут не следовать формату `===AGENT_INSTRUCTIONS===`.
**Файлы:** `src/corpclaw_lite/onboarding/finalizer.py`

**Действия:**
1. Добавить few-shot пример в `FINALIZATION_PROMPT` — показать ожидаемый формат с примером ответа
2. Это увеличит prompt на ~200 токенов, но значительно повысит follow-rate для маленьких моделей

---

## Status

- [x] Step 1.1 — SQLite vacuum/cleanup
- [x] Step 2.1 — User.memory_key()
- [x] Step 2.2 — exec_script буферизация
- [x] Step 2.3 — Race condition тесты
- [x] Step 2.4 — development department
- [x] Step 3.1 — AgentConfig dataclass
- [x] Step 3.2 — PLACEHOLDER в constants.py
- [x] Step 3.3 — Subagent timeout константа
- [x] Step 3.4 — Документация resolve_and_validate_path
- [x] Step 3.5 — _normalize_header docstring
- [x] Step 3.6 — excel.py рефакторинг
- [x] Step 3.7 — Переименование coverage тестов
- [x] Step 3.8 — network_policy.yaml комментарий
- [x] Step 3.9 — get_project_root fallback
- [x] Step 3.10 — Ruff правила A, PERF
- [x] Step 3.11 — agent_worker диагностика
- [x] Step 3.12 — Onboarding prompt few-shot

## Notes

- Каждый шаг должен проходить: `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v`
- Порядок выполнения: Phase 1 → Phase 2 → Phase 3
- Phase 3 шаги можно выполнять в любом порядке (они независимы)
- Step 3.1 (AgentConfig) — самый объёмный, затронет ~10 файлов
