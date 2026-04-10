# Sprint 1 — Production Readiness (Hardening)

## Summary

Спринт закрывает все блокеры первого деплоя выявленные в комплексном код-ревью (2026-03-30).
Проект реализован полностью (Фазы 1–5 ✅), но перед запуском в продакшн необходимо устранить
6 дефектов и добиться coverage ≥ 75%.

**Эстимейт:** 1 день (≈ 6–8 часов работы)  
**DoD:** `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v --cov=src/corpclaw_lite --cov-report=term-missing` — 0 errors, ≥75% coverage.

---

## Goals

- [ ] Закрыть все блокеры деплоя (MUST-список из ревью)
- [x] Coverage ≥ 75% (достигнуто 75% на 4052 строках; исправлен memory leak в rate_limit.py)
- [ ] 0 ruff / pyright ошибок после изменений

---

## Steps

### Задача 1 — H1: Подключить CredentialScrubber к pipeline логирования

**Файлы:** `src/corpclaw_lite/logging/agent_logger.py`  
**Эстимейт:** 15 минут  
**Приоритет:** MUST (security gap — секреты могут утекать в `corpclaw.log`)

```python
# Добавить в setup_logging(), после создания handlers:
from corpclaw_lite.security.credential_scrubber import CredentialScrubber

scrubber = CredentialScrubber()
text_handler.addFilter(scrubber)
console.addFilter(scrubber)
```

**Проверка:** тест `test_setup_logging_has_scrubber` — убедиться, что root logger
имеет handler с CredentialScrubber filter.

---

### Задача 2 — C2: CORPCLAW_ROOT — абсолютные пути для конфигов

**Файлы:** `src/corpclaw_lite/agent/factory.py`, `src/corpclaw_lite/memory/sqlite.py`  
**Эстимейт:** 1.5 часа  
**Приоритет:** MUST (сломает деплой через systemd/Docker с нестандартным CWD)

**factory.py** — заменить `Path("config/...")` на абсолютные пути от корня проекта:

```python
# Определить root один раз в начале factory.py
_PROJECT_ROOT = Path(__file__).parent.parent.parent  # src/ -> corpclaw_lite/ -> corpclaw-lite/

# Использовать везде:
guard_rules = _PROJECT_ROOT / "config" / "tool_guard_rules.yaml"
dept_config  = _PROJECT_ROOT / "config" / "departments.yaml"
subagent_dir = _PROJECT_ROOT / "config" / "subagents"
skills_dir   = _PROJECT_ROOT / "skills"
plugins_dir  = _PROJECT_ROOT / "plugins"
```

**Альтернатива (если нужна гибкость в деплое):** `CORPCLAW_ROOT` env var с fallback:

```python
_PROJECT_ROOT = Path(os.environ.get("CORPCLAW_ROOT", Path(__file__).parent.parent.parent))
```

**sqlite.py** — аналогично для `data/` директории:

```python
# Вместо: self.db_path = Path("data") / db_path
# Стать:
_DATA_DIR = Path(os.environ.get("CORPCLAW_DATA_DIR", Path(__file__).parent.parent.parent / "data"))
self.db_path = _DATA_DIR / db_path
```

**bootstrap.py** — проверить и исправить путь `Path("config/bootstrap")`.

**cli.py** — `BootstrapLoader(Path("config/bootstrap"))` → через `_PROJECT_ROOT`.

**Проверка:** запустить `uv run corpclaw-lite chat` из `/tmp` — должен работать.

---

### Задача 3 — C1 (MEDIUM): Рефакторинг `_sync_replace_oldest`

**Файлы:** `src/corpclaw_lite/memory/sqlite.py`  
**Эстимейт:** 30 минут  
**Приоритет:** MUST (плохой паттерн; 2 соединения вместо одного нарушает атомарность SELECT+DELETE)

```python
def _sync_replace_oldest(self, user_id: str, count: int, summary: str) -> None:
    try:
        with sqlite3.connect(self.db_path) as conn:
            # Выполняем SELECT в том же соединении, передавая conn явно
            cursor = conn.execute(
                """
                SELECT id FROM messages
                WHERE user_id = ?
                ORDER BY timestamp ASC
                LIMIT ?
                """,
                (str(user_id), count),
            )
            ids = [row[0] for row in cursor.fetchall()]
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",  # noqa: S608
                ids,
            )
            conn.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                (str(user_id), "system", f"[Conversation summary]: {summary}"),
            )
    except Exception as e:
        logger.error("Failed to consolidate messages for user %s: %s", user_id, e)
```

Удалить метод `_sync_get_oldest_message_ids` если он больше нигде не используется.

**Проверка:** тест консолидации памяти должен пройти.

---

### Задача 4 — H3: Сохранять reference на fire-and-forget tasks

**Файлы:** `src/corpclaw_lite/channels/telegram/runner.py`  
**Эстимейт:** 15 минут  
**Приоритет:** MUST (исключения из admin_notifier.notify() молча пропадают)

```python
# В run_telegram_bot() — добавить set для хранения tasks:
_background_tasks: set[asyncio.Task[None]] = set()

# Вместо:
asyncio.create_task(admin_notifier.notify(error_summary))

# Стать:
task = asyncio.create_task(admin_notifier.notify(error_summary))
_background_tasks.add(task)
task.add_done_callback(_background_tasks.discard)
```

Тот же паттерн применить к `asyncio.create_task(health.run_health_server())` (L4).

---

### Задача 5 — M6: Исправить формулу `_estimate_tokens` для кириллицы

**Файлы:** `src/corpclaw_lite/agent/compressor.py`  
**Эстимейт:** 10 минут  
**Приоритет:** MUST для кириллических диалогов (занижает в 4–12x → агент внезапно упирается в context limit)

```python
def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
    """Rough token estimate. Uses utf-8 byte count / 4 as heuristic.

    Rationale:
    - ASCII (English): 1 char = 1 byte ≈ 0.25 tokens → len_bytes/4 accurate
    - Cyrillic (Russian): 1 char = 2 bytes, but 1–3 tokens in BPE → len_bytes/4 better than len/4
    - Mixed text: utf-8 byte count / 4 is a safe heuristic for both
    """
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content.encode("utf-8")) // 4
        elif content is not None:
            total += len(str(content).encode("utf-8")) // 4

        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                args = tc.get("function", {}).get("arguments", "")
                if isinstance(args, str):
                    total += len(args.encode("utf-8")) // 4
                elif args:
                    total += len(str(args).encode("utf-8")) // 4

    return total
```

---

### Задача 6 — Coverage: добиться ≥75%

**Текущий:** 71% (4027 stmts, 1168 missed)  
**Нужно закрыть:** ~160 дополнительных строк  
**Эстимейт:** 2.5 часа  

**Приоритет файлов** (максимальный вклад в coverage):

#### 6.1 — LLM providers (~100 строк closed)

Создать `tests/test_llm_providers.py` с мок-тестами:

```python
# anthropic.py (29% coverage → ~52 строк missed)
# openai.py   (27% coverage → ~47 строк missed)

@pytest.mark.asyncio
async def test_anthropic_provider_complete(mock_anthropic_client):
    """Test that AnthropicProvider wraps SDK correctly."""
    ...

@pytest.mark.asyncio
async def test_openai_provider_complete(mock_openai_client):
    """Test that OpenAIProvider wraps SDK correctly."""
    ...

@pytest.mark.asyncio
async def test_openai_provider_xml_fallback(mock_openai_client_no_tools):
    """Test XML fallback when tool_choice unsupported."""
    ...
```

Использовать `unittest.mock.AsyncMock` для SDK clients.

#### 6.2 — Telegram channel handlers (~40 строк closed)

Добавить в `tests/test_channels.py`:

```python
async def test_telegram_channel_send_message_splits_long_text():
    ...

async def test_telegram_channel_request_approval_approve():
    ...

async def test_telegram_channel_request_approval_deny():
    ...

async def test_save_and_process_file_safe_path():
    ...
```

Использовать `unittest.mock.AsyncMock` для `telegram.Bot`.

#### 6.3 — Быстрые закрытия (~20 строк closed)

- `security/credential_scrubber.py` (83% → протестировать `filter()` метод + env-based patterns)
- `security/network_policy.py` (83% → тест `to_docker_args()` с настроенным allowlist)
- `logging/health.py` (48% → тест `increment()` + `get_stats()`)
- `channels/cli.py` (78% → тест `request_approval()` через stdin mock)

---

### Задача 7 — LOW (quick wins, ≤ 30 минут)

Выполнить в конце, если позволяет время. Каждая задача ≤ 5 минут:

#### 7.1 — H4: Добавить `from __future__ import annotations`

В 6 файлов (строго первая строка после возможного shebang/docstring):
- `security/ipc_auth.py`
- `security/credential_scrubber.py`
- `security/network_policy.py`
- `channels/cli.py`
- `departments/manager.py`
- `departments/permissions.py`

#### 7.2 — M8: Пропускать системные директории в `SearchFilesTool`

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/files.py`

```python
# В _search() добавить в os.walk:
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache"}

for root, dirs, files in os.walk(resolved):
    # Пропустить системные директории (in-place)
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
    for file_name in files:
        ...
```

#### 7.3 — L3: Убрать дублирующий `import asyncio` внутри функций

**Файл:** `src/corpclaw_lite/cli.py`

Строки 99, 214, 229 — удалить `import asyncio` внутри функций (уже импортирован на строке 25).

---

## Status

- [x] Задача 1 — H1: CredentialScrubber в logging
- [x] Задача 2 — C2: CORPCLAW_ROOT (абсолютные пути)
- [x] Задача 3 — C1: _sync_replace_oldest единое соединение
- [x] Задача 4 — H3: fire-and-forget tasks с reference
- [x] Задача 5 — M6: _estimate_tokens для кириллицы
- [x] Задача 6 — Coverage ≥75%
  - [x] 6.1 — LLM providers mock tests
  - [x] 6.2 — Telegram channel handlers
  - [x] 6.3 — Быстрые закрытия
- [x] Задача 7 — Quick wins (H4, M8, L3)
- [x] Full check: `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v --cov=src/corpclaw_lite`

---

## Notes

**Порядок выполнения критичен:**
1. Сначала Задача 2 (C2 — пути) — она затрагивает наибольшее количество файлов
2. Задача 3 (C1) — изменяет `sqlite.py`, после неё запустить memory tests
3. Задачи 1, 4, 5 — независимые, быстрые
4. Задача 6 — самая долгая, делать в конце когда все исправления стабилизированы
5. Full check финалом

**Не включено в Sprint 1 (backlog):**
- C3: NetworkPolicy allowlist (требует Docker custom network + iptables — отдельная задача)
- M1: `[build-system]` в pyproject.toml (не нужен для деплоя через `uv run`)
- M2: AGENTS.md/CLAUDE.md дубликат (косметика)
- M7: size/date в ListFilesTool (enhancement)
- M9: max_replacements в EditFileTool (enhancement)
- L1: YAML settings loading (enhancement, env vars работают)
