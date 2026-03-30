# Sprint 2 — Quality & Robustness (SHOULD)

## Summary

Спринт закрывает все задачи категории **SHOULD** из комплексного код-ревью (2026-03-30).  
Sprint 1 (MUST) завершён: coverage 75%, pyright 0 errors, все блокеры деплоя устранены.  
Sprint 2 фокусируется на надёжности в боевых условиях и улучшении качества кода.

**Эстимейт:** 1 день (≈ 5–6 часов)  
**DoD:** `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v` — 0 errors, ≥75% coverage.

---

## Контекст: что уже сделано в Sprint 1

Из списка SHOULD часть задач была закрыта досрочно:

| Задача | Статус | Где сделано |
|--------|--------|-------------|
| **H3** — fire-and-forget задачи | ✅ Закрыто | `runner.py` — `_background_tasks` set |
| **M6** — token estimation для кириллицы | ✅ Закрыто | `compressor.py` — `len(content.encode('utf-8')) // 4` |
| **M8** — фильтр системных директорий в SearchFiles | ✅ Закрыто | `files.py` — `dirs[:] = [d for d in dirs if d not in _skip_dirs]` |
| **H4** — `from __future__ import annotations` | ✅ Закрыто | 6 файлов добавлено |
| **L3** — дублирующий `import asyncio` | ✅ Закрыто | `cli.py` — удалены 3 повторных import |

Остаются для Sprint 2:

---

## Goals

- [ ] Расширить защиту `exec_script.py` от обхода через base64/eval/pipe (H2)
- [ ] Добавить `[build-system]` в `pyproject.toml` (M1)
- [ ] Документировать или задокументировать ограничения NetworkPolicy (C3)
- [ ] Улучшить вывод `ListFilesTool`: размер + дата файлов (M7)
- [ ] Добавить `max_replacements` в `EditFileTool` (M9)
- [ ] Убрать дублирование AGENTS.md/CLAUDE.md (M2)
- [ ] Починить ResourceWarning в тестах — утечки SQLite connections (M5)
- [ ] Сохранить tool_calls в SQLite памяти для полной истории между сессиями (M-extra)
- [ ] Заменить `str().startswith()` на `.is_relative_to()` в `channel.py` (H5 downgraded to LOW — всё же исправить)

---

## Steps

### Задача 1 — H2: Расширить BLOCKED_PATTERNS в `exec_script.py`

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/exec_script.py`  
**Эстимейт:** 20 минут  
**Приоритет:** SHOULD (текущие паттерны обходятся через `base64 -d | bash`, `eval`, `find / -delete`)

**Текущие паттерны** покрывают только прямые инвокации. Нужно добавить:

```python
BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    # Существующие (уже есть):
    re.compile(r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)*/\s*$"),
    re.compile(r"rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+/\b"),
    re.compile(r"mkfs\."),
    re.compile(r"dd\s+.*of=/dev/"),
    re.compile(r":\(\)\{.*\|.*&\s*\};:"),  # fork bomb
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"chmod\s+777\s+/\s*$"),

    # НОВЫЕ — добавить:
    re.compile(r"base64\s+-d\s*\|"),              # base64 decode → pipe (obfuscated payload)
    re.compile(r"base64\s+--decode\s*\|"),         # вариант
    re.compile(r"\beval\s+['\"`]"),               # eval с кодом (не eval переменной)
    re.compile(r"find\s+/\s+.*-delete"),           # find / -delete (destructive)
    re.compile(r"find\s+/\s+.*-exec\s+rm"),        # find / -exec rm
    re.compile(r"curl\s+.*\|\s*(ba)?sh"),           # curl | bash (RCE download)
    re.compile(r"wget\s+.*\|\s*(ba)?sh"),           # wget | bash
    re.compile(r"python.*-c.*os\.system"),          # python -c 'os.system(...)'
    re.compile(r"sudo\s+"),                        # sudo (не должно быть в sandbox)
    re.compile(r"chmod\s+[0-7]*[67][0-7]{0,2}\s+/"),  # chmod с широкими правами на /
]
```

**Принципы выбора паттернов:**
- Только очевидно деструктивные / RCE-vectors
- Не блокировать `eval` переменных (только с явным строковым аргументом)
- Не блокировать `find` без `-delete`/`-exec rm`

**Проверка:** обновить `tests/test_exec_script.py` — добавить тест-кейсы для каждого нового паттерна.

---

### Задача 2 — M1: Добавить `[build-system]` в `pyproject.toml`

**Файл:** `pyproject.toml`  
**Эстимейт:** 5 минут  
**Приоритет:** SHOULD (нужен для корректного `uv build` и будущей дистрибуции)

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

Также добавить:
```toml
[tool.hatch.build.targets.wheel]
packages = ["src/corpclaw_lite"]
```

**Почему hatchling:** стандартный бэкенд для `uv`-based проектов, не требует дополнительных зависимостей.

**Проверка:** `uv build --wheel` — должен создать `.whl` файл.

---

### Задача 3 — C3: NetworkPolicy — документировать ограничения

**Файлы:** `src/corpclaw_lite/security/network_policy.py`, `README.md`  
**Эстимейт:** 30 минут  
**Приоритет:** SHOULD (known limitation без документации вводит разработчиков в заблуждение)

**Проблема:** `ALLOWED_DOMAINS` env var создаётся в Docker-контейнере, но ни один компонент её не читает. `network_mode=none` блокирует весь трафик, включая разрешённые хосты.

**Два варианта решения:**

**Вариант A (документировать)** — минимальный effort:
- Добавить в `network_policy.py` развёрнутый комментарий с инструкцией по ручной настройке iptables/custom Docker network
- Добавить секцию в README: "Known Limitations → NetworkPolicy"

**Вариант B (реализовать)** — полная реализация:
- Создать Docker custom network с именем `corpclaw_agent`
- При старте контейнера подключать к custom network вместо `network_mode=none`
- В контейнере настроить iptables через entrypoint: разрешить allowlist хосты, заблокировать остальное
- Это требует `--cap-add NET_ADMIN` у агентного контейнера

**Рекомендация для Sprint 2: Вариант A** — задокументировать. Вариант B — в Sprint 3 или backlog.

**Изменения для Варианта A:**
```python
# В network_policy.py — расширить комментарий:
logger.warning(
    "NetworkPolicy: network_mode='none' blocks ALL traffic including allowlist.\n"
    "To enable allowlist-based networking:\n"
    "  1. Create Docker custom network: docker network create corpclaw_agent\n"
    "  2. Run container with --network corpclaw_agent\n"
    "  3. Add iptables rules in container entrypoint for ALLOWED_DOMAINS\n"
    "  4. See docs/network_policy.md for setup guide."
)
```

---

### Задача 4 — M7: Добавить file size и дату в `ListFilesTool`

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/files.py`  
**Эстимейт:** 20 минут  
**Приоритет:** SHOULD (LLM лучше принимает решения, видя размер файла)

**Текущий вывод:**
```
[DIR]  src/
[FILE] README.md
[FILE] pyproject.toml
```

**Целевой вывод:**
```
[DIR]  src/                 (4 items)
[FILE] README.md            3.2 KB   2026-03-30
[FILE] pyproject.toml       1.0 KB   2026-03-29
```

**Изменения:**
```python
def _format_entry(path: Path) -> str:
    if path.is_dir():
        try:
            child_count = sum(1 for _ in path.iterdir())
            return f"[DIR]  {path.name:<30} ({child_count} items)"
        except PermissionError:
            return f"[DIR]  {path.name}"
    else:
        stat = path.stat()
        size = _format_size(stat.st_size)
        mdate = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
        return f"[FILE] {path.name:<30} {size:>8}   {mdate}"

def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"
```

**Проверка:** обновить тест `test_list_files.py` — проверить что `KB` и дата присутствуют в выводе.

---

### Задача 5 — M9: Добавить `max_replacements` в `EditFileTool`

**Файл:** `src/corpclaw_lite/extensions/tools/builtin/files.py`  
**Эстимейт:** 15 минут  
**Приоритет:** SHOULD (LLM ожидает one-shot замену при точном `old_text`, а получает все вхождения)

**Текущее поведение:**
```python
content = content.replace(old_text, new_text)  # заменяет ВСЕ вхождения
```

**Целевое поведение:**
```python
ToolParam(
    name="max_replacements",
    type="integer",
    description="Maximum number of replacements (default: 1). Use 0 for unlimited.",
    required=False,
)

max_repl = int(kwargs.get("max_replacements", 1))
count = content.count(old_text)
if max_repl > 0:
    new_content = old_text
    for _ in range(min(count, max_repl)):
        new_content = content.replace(old_text, new_text, 1)
        content = new_content
else:
    content = content.replace(old_text, new_text)
```

Более чисто через `str.replace(count)`:
```python
max_repl = int(kwargs.get("max_replacements", 1))
count = content.count(old_text)
if max_repl == 0 or max_repl >= count:
    content = content.replace(old_text, new_text)
    applied = count
else:
    content = content.replace(old_text, new_text, max_repl)
    applied = max_repl
```

**Проверка:** тест с 3 вхождениями текста, `max_replacements=1` → только первое заменяется.

---

### Задача 6 — M2: Убрать дублирование AGENTS.md/CLAUDE.md

**Файл:** `CLAUDE.md` (удалить или сделать symlink)  
**Эстимейт:** 5 минут (2 строки bash)

Оба файла идентичны по содержимому. `AGENTS.md` — каноническое имя по конвенции.

**Решение:** удалить `CLAUDE.md`, создать symlink `CLAUDE.md -> AGENTS.md` для совместимости с IDE которые ищут `CLAUDE.md`.

```bash
rm CLAUDE.md
ln -s AGENTS.md CLAUDE.md
```

Проверить не ломает ли `.gitignore` правила.

---

### Задача 7 — M5: Починить ResourceWarning в тестах (SQLite connections)

**Файлы:** тестовые файлы где создаются `SQLiteMemory` объекты  
**Эстимейт:** 30 минут  
**Приоритет:** SHOULD (187 warnings засоряют pytest output, скрывают реальные проблемы)

**Причина:** `SQLiteMemory._init_db()` открывает соединение при инициализации, но тесты не вызывают cleanup метода.

**Решение:** добавить `close()` метод в `SQLiteMemory`:
```python
def close(self) -> None:
    """Close any open database connections. Call in tests for clean teardown."""
    # Не нужно — соединения открываются и закрываются через context manager.
    # Но тест нужно обернуть в try/finally или использовать autouse fixture.
```

Проблема в том, что `_init_db()` вызывает `sqlite3.connect()` без context manager, оставляя соединение незакрытым.

**Исправление в `sqlite.py`:**
```python
def _init_db(self) -> None:
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(self.db_path) as conn:  # context manager гарантирует закрытие
        conn.execute(...)
        conn.execute(...)
```

**Исправление в тестах** — добавить `autouse` fixture:
```python
@pytest.fixture(autouse=True)
def cleanup_sqlite(tmp_path):
    """Ensure SQLite connections are closed after each test."""
    yield
    import gc
    gc.collect()  # Force garbage collection of unreferenced connections
```

---

### Задача 8 — H5 (→ LOW): Заменить `startswith` на `is_relative_to` в `channel.py`

**Файл:** `src/corpclaw_lite/channels/telegram/channel.py`  
**Эстимейт:** 10 минут  
**Приоритет:** LOW (не реальная уязвимость, но паттерн хорошего кода)

**Текущий код:**
```python
if not str(target_path).startswith(str(workspace.resolve())):
```

**Исправление:**
```python
if not target_path.is_relative_to(workspace.resolve()):
```

`Path.is_relative_to()` — доступен с Python 3.9+, работает корректно даже когда имена директорий являются префиксами других (`/workspace` vs `/workspace2`).

---

### Опциональная задача — M-extra: Сохранять tool_calls в SQLite памяти

**Файл:** `src/corpclaw_lite/memory/sqlite.py`, `src/corpclaw_lite/agent/context.py`  
**Эстимейт:** 1.5 часа  
**Приоритет:** SHOULD-stretch (текущий дизайн сознательно упрощён, но это ограничивает качество диалогов)

**Проблема:** При новой сессии агент восстанавливает только пары `user`/`assistant` без `tool_calls`/`tool` результатов. LLM не видит что было сделано в прошлый раз.

**Решение:** расширить схему SQLite:
```sql
-- Уже есть:
CREATE TABLE messages (id, user_id, role, content, timestamp)

-- Добавить колонку:
ALTER TABLE messages ADD COLUMN tool_calls TEXT;           -- JSON array of tool_calls
ALTER TABLE messages ADD COLUMN tool_call_id TEXT;         -- for role=tool messages
```

```python
def add_message(
    self, user_id: str, role: str, content: str,
    *, tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None
) -> None: ...
```

**Риск backward compatibility:** требует миграции существующих DB. Добавить `_migrate_db()` в `_init_db()`.

> [!WARNING]
> Задача опциональна: реализовывать только если по итогам E2E-тестов с реальным LLM обнаружится что отсутствие tool_call истории существенно ухудшает работу агента в повторных сессиях.

---

## Status

- [ ] Задача 1 — H2: BLOCKED_PATTERNS расширение
- [ ] Задача 2 — M1: `[build-system]` в pyproject.toml
- [ ] Задача 3 — C3: NetworkPolicy — документировать ограничения
- [ ] Задача 4 — M7: ListFilesTool с размером и датой
- [ ] Задача 5 — M9: EditFileTool `max_replacements`
- [ ] Задача 6 — M2: Убрать AGENTS.md/CLAUDE.md дублирование
- [ ] Задача 7 — M5: ResourceWarning в тестах
- [ ] Задача 8 — H5→LOW: `is_relative_to` в channel.py
- [ ] (Опц.) M-extra: tool_calls в SQLite памяти
- [ ] Full check: `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v`

---

## Priority Order

1. **Задача 7 (M5)** — быстро, сразу чистит pytest output
2. **Задача 8 (H5)** — 10 минут, defensive code improvement
3. **Задача 6 (M2)** — 5 минут, чистота репо
4. **Задача 2 (M1)** — 5 минут, нужен для деплоя через wheel
5. **Задача 1 (H2)** — 20 минут, security defense-in-depth
6. **Задача 3 (C3)** — 30 минут, документация ограничений
7. **Задача 4 (M7)** — 20 минут, UX для LLM
8. **Задача 5 (M9)** — 15 минут, правильное поведение edit_file
9. **(Опц.) M-extra** — 1.5 часа, только по результатам E2E тестирования

---

## Notes

**Что НЕ входит в Sprint 2 (Backlog / Sprint 3):**

- **C3 Вариант B** — реализация Docker custom network + iptables (требует devops-ресурсов и testing)
- **L1** — загрузка `settings.yaml` через pydantic-settings (enhancement, env vars работают)
- **L2** — добавить `__all__` во все модули (pure style, нет практической пользы)
- **M4** — docker import guard через optional dependency group (minor, текущий `# type: ignore` работает)
- **История tool_calls (M-extra)** — отложить до E2E-тестирования

**Зависимости между задачами:** отсутствуют — все задачи независимы, можно выполнять в любом порядке.
