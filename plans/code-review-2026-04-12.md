# Комплексное код-ревью CorpClaw Lite — 12 апреля 2026

## Summary

Полное ревью кодовой базы CorpClaw Lite (коммит `701f213`) с последующей **верификацией каждого пункта** путём чтения реального кода и трассировки связей. Из 20 первоначальных претензий:

- **0 Critical** (единственный был опровергнут)
- **5 подтверждённых Suggestion**
- **8 подтверждённых Nice to have**
- **4 опровергнуто** (ложные срабатывания AI-агентов)

Проект прошёл все детерминированные проверки: `ruff ✅`, `pyright 0 errors ✅`, `pytest 667 passed ✅`.

---

## Детерминированные проверки

| Проверка | Результат |
|----------|-----------|
| `uv run ruff check src/` | All checks passed |
| `uv run pyright src/` | 0 errors, 0 warnings |
| `uv run pytest tests/ -v` | 667 passed, 1 skipped, 24.00s |

---

## Методология верификации

Каждый пункт первоначального ревью был проверен путём:
1. Чтения реального исходного кода с контекстом
2. Трассировки вызовов (caller → callee)
3. Анализа реального attack surface (не теоретического)
4. Классификации: подтверждено / опровергнуто / понижено

---

## Опровергнутые претензии (4) — ложные срабатывания

### #1 ~~Critical~~ `proc.pid is None` в exec_script.py

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/exec_script.py:76`

**Почему опровергнуто:** `asyncio.create_subprocess_shell` — awaitable, возвращающий процесс только **после** fork/exec. `pid` гарантирован. Если процесс мгновенно завершается — `os.getpgid(proc.pid)` вернёт `ESRCH`, который уже перехвачен в `except (ProcessLookupError, OSError)` (строка 80). Код корректен.

### #5 ~~Suggestion~~ `_read_stream_limited` — некорректная арифметика обрезки

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/exec_script.py:116`

**Почему опровергнуто:** Формула `chunk[: max_bytes - total + len(chunk)]` **корректна**:

```
total уже включает len(chunk)
max_bytes - total + len(chunk) = max_bytes - (total - len(chunk))
= сколько байт из текущего chunk нужно оставить
```

Трассировка: max=50000, было 49152, chunk=4096 → total=53248 → срез `[:848]` → ровно 50000 байт суммарно.

### #8 ~~Suggestion~~ `OpenAIProvider.choices[0]` IndexError

**Файл:** `src/corpclaw_lite/llm/openai.py:171`

**Почему опровергнуто:** OpenAI API контракт гарантирует непустой `choices`. При серверных ошибках SDK бросает `APIError` до возврата response. Пустой `choices` невозможен от рабочего сервера.

### #11 ~~Suggestion~~ ContainerManager — слишком широкий `except`

**Файл:** `src/corpclaw_lite/container/manager.py:131-136`

**Почему опровергнуто:**

```python
except Exception as _e:
    _is_not_found = docker is not None and isinstance(_e, docker.errors.NotFound)
    if not _is_not_found:
        raise ContainerManagerError(f"Error checking container: {_e}") from _e
```

Логика корректна: `NotFound` → fall through к созданию контейнера, все остальные ошибки → пробрасываются как `ContainerManagerError`. Это правильный дизайн.

---

## Подтверждённые Suggestion (5)

### #2 `maybe_consolidate` загружает ВСЮ историю ради проверки 6 последних сообщений

**Файл:** `src/corpclaw_lite/memory/consolidation.py:97-102`

**Проблема:**

```python
count = await memory.count_messages(user_id)  # напр. 200
history = await memory.get_history(user_id, limit=count)  # ВСЕ 200 сообщений
tail_window = history[-6:]  # использует только 6
```

История загружается полностью, хотя для проверки workflow-маркеров нужны только последние 6 сообщений. Полная история нужна позже для `old_messages = history[:split]`, но в большинстве случаев (когда tail содержит маркеры) полная загрузка бесполезна.

**Влияние:** Лишний SQLite round-trip + десериализация 200+ сообщений при каждом вызове consolidation. При cooldown (60s) — не критично, но при активной сессии — ощутимо.

**Решение:**

```python
async def maybe_consolidate(self, memory: SQLiteMemory, user_id: str) -> bool:
    count = await memory.count_messages(user_id)
    if count < self._threshold:
        return False

    # Cooldown check
    now = time.monotonic()
    last = self._last_consolidated.get(user_id, float("-inf"))
    if now - last < _COOLDOWN_SECONDS:
        return False

    # 1. Сначала проверяем tail — дёшево
    try:
        tail = await memory.get_history(user_id, limit=6)
    except StorageError:
        return False

    for msg in tail:
        content = str(msg.get("content", ""))
        for marker in _ACTIVE_WORKFLOW_MARKERS:
            if marker in content:
                return False

    # 2. Только если tail чист — загружаем всё для консолидации
    try:
        history = await memory.get_history(user_id, limit=count)
    except StorageError:
        return False

    split = count // 2
    old_messages = history[:split]
    # ... дальнейшая консолидация
```

---

### #3 `CredentialScrubber` пропускает credentials в traceback

**Файл:** `src/corpclaw_lite/security/credential_scrubber.py:38-56`

**Проблема:** Скраббер обрабатывает только `record.msg` и `record.args`:

```python
record.msg = self._scrub(record.msg)
# record.args тоже скрабятся
```

Но при `logger.exception("Error:", exc_info=True)` Python форматирует traceback в `record.exc_text`. Скраббер **не трогает** `exc_text`. Если в traceback попал API key (напр. при логировании ответа от LLM-провайдера), он окажется в логах как есть.

**Влияние:** Креденшелы могут попасть в логи через exception tracebacks, особенно при ошибках LLM-провайдеров.

**Решение:**

```python
def filter(self, record: logging.LogRecord) -> bool:
    """Process the log record and scrub sensitive credentials."""
    # Scrub the fully-formatted message
    formatted = record.getMessage()
    cleaned = self._scrub(formatted)
    record.msg = cleaned
    record.args = ()  # prevent double-formatting

    # Scrub exception traceback text
    if record.exc_text:
        record.exc_text = self._scrub(record.exc_text)

    # Also scrub original args for callers that read them directly
    if isinstance(record.args, tuple):
        record.args = tuple(
            self._scrub(arg) if isinstance(arg, str) else arg for arg in record.args
        )
    elif isinstance(record.args, dict):
        record.args = {
            k: self._scrub(v) if isinstance(v, str) else v
            for k, v in record.args.items()
        }
    return True
```

---

### #7 `SearchFilesTool` читает ВСЕ файлы целиком в память

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/files.py:282`

**Проблема:**

```python
for root, dirs, files in os.walk(resolved):
    for file_name in files:
        content = file_path.read_text(encoding="utf-8", errors="ignore")  # ВЕСЬ файл
        for i, line in enumerate(content.splitlines(), start=1):
            if regex.search(line):
                ...
```

Каждый файл загружается полностью в память, даже если regex совпадёт на первой строке.

**Влияние:** Memory spike при поиске по большим директориям. Для `.logs/` или `data/` с большими файлами — может вызвать OOM.

**Решение:**

```python
MAX_FILE_SEARCH_BYTES = 1024 * 1024  # 1MB limit per file

def _search() -> list[str]:
    results: list[str] = []
    for root, dirs, files in os.walk(resolved):
        dirs[:] = [d for d in dirs if d not in _skip_dirs and not d.startswith(".")]
        for file_name in files:
            file_path = Path(root) / file_name
            if file_path.name.startswith(".") or file_path.suffix.lower() in IMAGE_EXTENSIONS:
                continue

            try:
                matches: list[str] = []
                file_bytes = 0
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, start=1):
                        file_bytes += len(line.encode("utf-8"))
                        if file_bytes > MAX_FILE_SEARCH_BYTES:
                            matches.append(f"{i}: ... (file truncated at 1MB)")
                            break
                        if regex.search(line):
                            matches.append(f"{i}: {line.strip()[:100]}")

                if matches:
                    rel_path = file_path.relative_to(resolved)
                    results.append(f"--- {rel_path.as_posix()} ---")
                    results.extend(matches)
                    if len(results) > self.max_results:
                        results.append("... search truncated.")
                        return results
            except Exception:
                pass
    return results
```

---

### #9 `agent_worker.py` — нет таймаута на выполнение инструмента

**Файл:** `src/corpclaw_lite/container/agent_worker.py:97`

**Проблема:**

```python
result = asyncio.run(tool.execute(**args))
```

Внутри контейнера нет таймаута. Зависший инструмент потребляет CPU/память до внешнего убийства `docker exec` через 30 секунд.

**Влияние:** 30 секунд бесконтрольного потребления ресурсов. Для `exec_script` — `while true; do :; done` жрёт CPU полностью.

**Решение:**

```python
# Добавить в process_request():
_TOOL_TIMEOUT = 25.0  # seconds — leave 5s buffer for IPC timeout (30s)

async def _execute_tool(tool, args):
    return await asyncio.wait_for(tool.execute(**args), timeout=_TOOL_TIMEOUT)

loop = asyncio.new_event_loop()
try:
    result = loop.run_until_complete(_execute_tool(tool, args))
except asyncio.TimeoutError:
    raise TimeoutError(f"Tool '{tool_name}' timed out after {_TOOL_TIMEOUT}s")
finally:
    loop.close()
```

---

### #12 Token estimation недооценивает кириллицу в 2-3 раза

**Файл:** `src/corpclaw_lite/agent/compressor.py:256-277`

**Проблема:**

```python
total += len(content.encode("utf-8")) // 4
```

| Язык | Байт/символ | Оценка (`/4`) | Реально (BPE) | Ошибка |
|------|------------|---------------|---------------|--------|
| English | 1 | N/4 | ~N/4 | ✅ точно |
| Кириллица | 2 | N/2 | ~N-1.5N | ❌ в 2-3x меньше |

**Влияние:** Компрессия срабатывает в 2-3 раза позже для русского текста. Контекстное окно может быть превышено.

**Решение:**

```python
def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
    """Estimate token count — conservative for mixed-language content.

    - ASCII: 1 char = 1 byte → len_bytes/4 ≈ tokens (accurate)
    - Cyrillic: 1 char = 2 bytes, BPE ≈ 1-1.5 tokens/char
      → len_bytes/2 is conservative (won't underestimate)
    """
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            content_bytes = content.encode("utf-8")
            len_bytes = len(content_bytes)
            len_chars = len(content)
            if len_chars > 0:
                ratio = len_bytes / len_chars
                if ratio > 1.3:
                    # Significant non-ASCII content — use conservative estimate
                    total += len_bytes // 2
                else:
                    # Mostly ASCII
                    total += len_bytes // 4
            else:
                total += len_bytes // 4

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

## Подтверждённые Nice to have (8)

### #4 Prompt injection через `user.name` / `user.department`

**Файл:** `src/corpclaw_lite/agent/loop.py:176-177`

**Проблема:** Имя и департамент интерполируются напрямую в system prompt без санитизации. Злонамеренный админ может создать пользователя с именем, содержащим prompt injection.

**Влияние:** Теоретический prompt injection, но attack vector — админ (уже имеет полный доступ).

**Решение:** Использовать `_sanitize_for_prompt`:

```python
from corpclaw_lite.security.tool_guard import ToolGuard

dynamic_prompt = (
    f"Current User Context:\n"
    f"- Name: {ToolGuard._sanitize_for_prompt(user.name, strip_newlines=True)}\n"
    f"- Department: {ToolGuard._sanitize_for_prompt(user.department, strip_newlines=True)}\n"
    ...
)
```

---

### #6 `_prune_last_used` сортирует весь dict при каждом вызове

**Файл:** `src/corpclaw_lite/container/ipc.py:55-60`

**Проблема:** O(n log n) сортировка 10K записей при КАЖДОМ `send_tool_call`.

**Влияние:** При 10-100 пользователях — 0.01ms, незаметно. Проблема только при 10K+.

**Решение:** Прореживание:

```python
def __init__(self, auth: IPCAuth, timeout_seconds: float = 30.0) -> None:
    self.auth = auth
    self.timeout = timeout_seconds
    self._last_used: dict[int, float] = {}
    self._prune_counter = 0

def _prune_last_used(self) -> None:
    self._prune_counter += 1
    if len(self._last_used) <= self._MAX_LAST_USED:
        return
    # Прореживаем раз в 100 вызовов
    if self._prune_counter % 100 != 0:
        return
    sorted_keys = sorted(self._last_used.keys(), key=lambda k: self._last_used[k])
    to_remove = sorted_keys[: len(sorted_keys) - self._MAX_LAST_USED // 2]
    for k in to_remove:
        del self._last_used[k]
```

---

### #10 TF-IDF digest считается на каждый `match()`

**Файл:** `src/corpclaw_lite/extensions/skills/matcher.py:234-240`

**Проблема:** SHA-256 digest строки ~300*N символов при каждом `match()`, даже когда skills не менялись.

**Влияние:** Для 10 скилов — ~0.01ms. Не bottleneck.

**Решение:** Полагаться только на frozenset ID:

```python
def _ensure_index(self, skills: list[Skill]) -> None:
    """Rebuild the TF-IDF index if the skill set changed."""
    current_ids = frozenset(s.id for s in skills)
    if current_ids == self._indexed_ids and self._docs:
        return  # Skill set unchanged — index is valid
    self._rebuild_index(skills)
```

Инвалидацию при hot-reload делает `SkillHotReloader`.

---

### #13 Tool-role не фильтруется в `get_history()`

**Файл:** `src/corpclaw_lite/memory/sqlite.py:125-147`

**Проблема:** `get_history()` возвращает ВСЕ роли включая `"tool"`. ContextBuilder молча дропает их (строка 194).

**Влияние:** Cosmetic — caller не должен получать orphaned tool-записи.

**Решение:** Фильтровать в `_sync_get_history`:

```python
for r in reversed(rows):
    role = r["role"]
    if role == "tool":
        continue  # Orphaned — no tool_call_id, useless for context
    history.append({"role": role, "content": r["content"]})
```

---

### #14 `verify=False` для HTTP — no-op

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/web.py:178`

**Проблема:** Для HTTP с IP-pinning: `verify = False`. Для plaintext HTTP нет TLS — `verify` не влияет ни на что.

**Влияние:** Cosmetic confusion.

**Решение:** Убрать `verify` для HTTP, оставить только для HTTPS:

```python
client_kwargs: dict[str, Any] = {
    "timeout": timeout,
    "follow_redirects": False,
}
if current_ips and parsed_u.scheme != "https":
    url_to_fetch = current_url.replace(f"://{hostname}", f"://{current_ips[0]}", 1)
    headers = {"Host": hostname}
elif current_ips and parsed_u.scheme == "https":
    # IP-pinned HTTPS — disable TLS verification (cert won't match IP)
    client_kwargs["verify"] = False
    url_to_fetch = current_url
    headers = {}
else:
    url_to_fetch = current_url
    headers = {}
```

---

### #16 `SELECT-then-DELETE` в `_sync_replace_oldest` не атомарен

**Файл:** `src/corpclaw_lite/memory/sqlite.py:202-220`

**Проблема:** При нескольких concurrent writers возможен race между SELECT и DELETE.

**Влияние:** Теоретический — в текущей архитектуре один AgentLoop на одну базу.

**Решение:** `BEGIN IMMEDIATE` для write lock:

```python
def _sync_replace_oldest(self, user_id: str, count: int, summary: str) -> None:
    try:
        with db_connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "SELECT id FROM messages WHERE user_id = ? ORDER BY timestamp ASC LIMIT ?",
                (str(user_id), count),
            )
            ids = [row[0] for row in cursor.fetchall()]
            if not ids:
                return
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",
                ids,
            )
            conn.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                (str(user_id), "assistant", summary),
            )
            conn.commit()
    except Exception as e:
        raise StorageError(f"Failed to replace oldest messages for user {user_id}: {e}") from e
```

---

### #17 Telegram TOCTOU race при переименовании файлов

**Файл:** `src/corpclaw_lite/channels/telegram/channel.py:458`

**Проблема:** Между `target_path.exists()` и `download_to_drive()` другой concurrent upload может создать файл.

**Влияние:** Крайне маловероятно — один бот, один пользователь.

**Решение:** UUID-based имена:

```python
import uuid

# Вместо цикла while:
safe_name = os.path.splitext(sanitize_filename(file_unique_id or file_name))[0]
ext = os.path.splitext(safe_name)[1] or ".dat"
unique_name = f"{uuid.uuid4().hex[:8]}_{safe_name}{ext}"
target_path = (workspace / unique_name).resolve()
```

---

### #19 `factory.py` — 34 локальных импорта

**Файл:** `src/corpclaw_lite/agent/factory.py`

**Проблема:** ~34 `from corpclaw_lite...` импорта внутри функций. Решает circular imports, но скрывает граф зависимостей.

**Влияние:** Затрудняет понимание зависимостей, статический анализ, замедляет первый вызов.

**Решение:** Долгосрочно — рефакторинг на `factory_providers.py`, `factory_tools.py`, `factory_agent.py`. Краткосрочно — добавить TYPE_CHECKING блок и документировать причину локальных импортов.

---

## Сильные стороны проекта (подтверждены при ревью)

1. **Безопасность в ядре** — ToolGuard YAML rules, NetworkPolicy deny-by-default, IPC HMAC auth, CredentialScrubber, path traversal защита, SSRF-блокировка в web_fetch
2. **Чистая архитектура** — Protocol-based абстракции, без enterprise over-engineering v1
3. **Строгая типизация** — 0 pyright errors (strict mode), современный Python 3.12+ синтаксис
4. **Хорошее тестовое покрытие** — 667 тестов, включая security edge cases
5. **Продуманная калибровка** — Model presets, auto-calibration, context compression
6. **Hot reload** — skills, plugins, MCP без перезапуска
7. **Модульные промпты** — `config/bootstrap/` вместо монолитного system prompt
8. **Гибридный онбординг** — детерминистический + LLM-финализация

---

## Итоговая таблица

| # | Приоритет | Статус | Файл | Суть |
|---|-----------|--------|------|------|
| 2 | Suggestion | ✅ | consolidation.py:97 | Загрузка всей истории ради 6 сообщений |
| 3 | Suggestion | ✅ | credential_scrubber.py:38 | Утечка креденшелов через traceback |
| 7 | Suggestion | ✅ | files.py:282 | Поиск читает все файлы целиком |
| 9 | Suggestion | ✅ | agent_worker.py:97 | Нет таймаута на инструмент в контейнере |
| 12 | Suggestion | ✅ | compressor.py:256 | Токены для кириллицы недооценены в 2-3x |
| 4 | Nice to have | ✅ | loop.py:176 | Prompt injection через user.name |
| 6 | Nice to have | ✅ | ipc.py:55 | Сортировка 10K записей при каждом вызове |
| 10 | Nice to have | ✅ | matcher.py:234 | SHA-256 digest при каждом match() |
| 13 | Nice to have | ✅ | sqlite.py:125 | Tool-role не фильтруется в get_history |
| 14 | Nice to have | ✅ | web.py:178 | verify=False для HTTP — no-op |
| 16 | Nice to have | ✅ | sqlite.py:202 | SELECT-then-DELETE не атомарен |
| 17 | Nice to have | ✅ | channel.py:458 | TOCTOU race при переименовании |
| 19 | Nice to have | ✅ | factory.py | 34 локальных импорта |
| — | — | ❌ | exec_script.py:76 | ~~proc.pid is None~~ — опровергнуто |
| — | — | ❌ | exec_script.py:116 | ~~арифметика обрезки~~ — опровергнуто |
| — | — | ❌ | openai.py:171 | ~~choices[0] IndexError~~ — опровергнуто |
| — | — | ❌ | manager.py:131 | ~~широкий except~~ — опровергнуто |

---

## Рекомендации по порядку исправления

### Сначала (5 Suggestion)

1. **#3 CredentialScrubber** — 10 строк, максимальный security impact
2. **#12 Token estimation** — ~15 строк, критично для русского языка
3. **#7 SearchFilesTool** — ~10 строк, защита от OOM
4. **#2 Consolidation** — ~10 строк, оптимизация SQLite
5. **#9 Agent worker timeout** — ~10 строк, защита ресурсов

### Потом (8 Nice to have)

6. **#4** Prompt injection санитизация
7. **#13** Tool-role фильтрация в get_history
8. **#10** Убрать digest из matcher
9. **#16** BEGIN IMMEDIATE
10. **#14** Убрать verify=False для HTTP
11. **#6** Прореживание prune
12. **#17** UUID для файлов
13. **#19** Рефакторинг factory.py (долгосрочно)
