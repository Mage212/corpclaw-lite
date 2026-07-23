# Full LLM-context Persistence per Chat — спецификация

**Дата:** 2026-06-28
**Статус:** Draft (ожидает review)
**Backlog:** B-063
**Родственные PR:** #60 (compress button), #54 (agent context), #58 (empty start)

---

## 1. Summary

Текущая модель памяти CorpClaw Lite строго **per-user, single-thread**: полный LLM-facing контекст (system + history + tool_calls + reasoning) существует только in-memory для единственного активного чата. Таблица `web_chat_messages` хранит *user-visible* транскрипт (role/content/tone), а не полный LLM-контекст. Отсюда три ограничения:

1. **Невозможно восстановить контекст чата.** Переключение с активного чата стирает его LLM-контекст (`reset_user_context` → `DELETE FROM messages`). Реактивация старого чата стартует «чистым» — агент не «помнит» прошлый ход, кроме user-visible текста.
2. **Компрессия ограничена активным чатом.** Кнопка «Сжать» (PR #60) работает только с одним in-memory контекстом. Сжать произвольный чат неоткуда — его контекст не сохранён.
3. **KV-cache continuity ломается** при переключении чатов — каждая активация перепроцессит весь prompt.

**Цель:** персистентное хранение полного LLM-context per chat (tool_calls, reasoning, точная схема сообщений), чтобы:
- Любой чат восстанавливался в точное LLM-состояние при активации.
- Компрессия применялась к любому чату (load → compress → write-back).
- Фундамент для датасетов/fine-tuning стал структурированным и per-chat.

---

## 2. Контекст: как есть сейчас (ground truth)

Тщательно проанализированы три подсистемы. Ниже — подтверждённая архитектура с точными ссылками.

### 2.1 Agent memory (`SQLiteMemory`) — per-user, не per-chat

**Файл:** `src/corpclaw_lite/memory/sqlite.py`

Схема таблицы `messages` (строки 44-52):
```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
-- + миграция: ALTER TABLE messages ADD COLUMN reasoning TEXT
```

- **Ключ — `str(user.id)`** (`users/models.py:26`, `memory_key()`). 24 call-site используют один и тот же ключ. **Нет параметра session/chat.**
- **Нет колонок** `tool_calls`, `tool_call_id`, `name`. Структурные tool-вызовы/результаты **не персистятся**.
- `reasoning` колонка существует и populated (assistant-turn'ы), но **audit-only** — `get_history` её не возвращает.

**Методы:**
- `get_history(user_id, limit=20)` → `list[dict]` с `{"role","content"}` (строки 122-148). `reasoning` НЕ выбирается. Default limit=20.
- `add_message(user_id, role, content, reasoning=None)` (101-120) — content stringify'ится; tool_calls теряются.
- `clear(user_id)` → `DELETE FROM messages WHERE user_id=?` (150-159) — без session-scoping.

### 2.2 AgentLoop.run() — контекст in-memory, persistence урезанная

**Файл:** `src/corpclaw_lite/agent/loop.py`

Цепочка внутри `run()`:
```
mem_key = user.memory_key()                        # str(user.id), line 747
history = memory.get_history(mem_key, limit=20)    # role+content only, 751
context = ContextBuilder.build_initial(history)    # дропает tool-role, 799
memory.add_message(mem_key, "user", message)       # 807

per iteration (in-memory only):
  prune_old_tool_results / compressor.compress     # context.messages, НЕ в SQLite
  context.add_tool_calls(response.tool_calls)      # 1257
  context.add_tool_result(tc.id, name, result)     # 1278/1329

final answer:
  _save_turn(mem_key, final, tools_used, reasoning)
    add_message(assistant, final, reasoning)       # текст + reasoning
    add_message(system, "Tools called: …")         # audit-record
  consolidator.maybe_consolidate                    # replace_oldest if >30 msgs
```

- **`tool_calls`/`tool_results` живут только в `ContextBuilder.messages`** (in-memory, per-run). При завершении run они **не сохраняются**.
- `_save_turn` (1474-1507) пишет 2 строки: assistant-text+reasoning и system-record «Tools called in this turn: …».

### 2.3 ContextBuilder — tool-структура in-memory

**Файл:** `src/corpclaw_lite/agent/context.py`

- `add_tool_calls(tool_calls, content)` (30-57) — эмитит assistant-message с `tool_calls` list. **In-memory only.**
- `add_tool_result(tool_call_id, name, result)` (59-68) — `{"role":"tool",...}`. **In-memory only.**
- `build_initial(history)` (108-196) — **дропает tool-role сообщения** (line 194) и мержит system в system-prompt. Даже если tool-сообщения персистированы, реконструкция их обрежет.

### 2.4 WebChatStore — user-visible transcript, отдельный store

**Файл:** `src/corpclaw_lite/channels/web/chat_store.py`

Таблицы `web_chat_sessions` + `web_chat_messages`. Ключ `(user_id, session_id)`. Содержимое `web_chat_messages`: role/content/tone + metadata_json (агрегаты: usage, tools_used). **Нет reasoning, нет tool_calls, нет structured content.**

Оба store в одном `data/memory.db`, но никогда не JOIN'ятся, пишутся независимо.

### 2.5 Переключение чата = RESET, не LOAD

**Файл:** `src/corpclaw_lite/channels/web/orchestrator.py:1228-1277` (`_handle_activate_chat`)

```python
await self._service.reset_user_context(user)        # 1252: DELETE FROM messages + cache reset
new_id = await self._chat_store.activate_session(...)  # 1253: DB flag flip
```

- `reset_user_context` (`service.py:144`) → `memory.clear(user.memory_key())` + `mark_user_cache_reset`.
- Чат, с которого ушли, **теряет agent-context навсегда**. Чат, на который пришли, **стартует пустым**, даже если у него длинный transcript.

### 2.6 Compress flow

Два механизма, оба per-user (loop.py):
- **In-loop** (904-919): `ContextCompressor.compress(context.messages)` — in-memory, не writeback.
- **On-demand** `compress_now` (368-432): `get_history(20)` → compress → clear + re-add. Видит только role+content последних 20 сообщений. **Per-user, не per-chat.**

### 2.7 LLM-payload logging (`logs/llm_payloads.jsonl`)

**Файл:** `src/corpclaw_lite/logging/payload.py` (`PayloadCaptureLogger.capture`, line 132)

Запись per LLM-call:
```json
{"ts","run_id":null (ВСЕГДА!),"phase",
 "request":{"model","messages","tools","params","extra_body"},
 "response":{"content","reasoning","tool_calls","usage","finish_reason"},
 "diagnostic"}
```

- `request.messages` — **точный снимок (snapshot) полного LLM-facing контекста** на момент вызова (system + history + tool_calls + tool_results + последнее user-сообщение). После prune/compress.
- Allowlist включает `request.messages` + `response.tool_calls` + `response.reasoning`.
- Scrubbing credentials, **без truncation** (для датасетов).
- RotatingFileHandler 20MB×3 — **ротация удаляет старое**.
- **Анонимно**: нет `user_id`, `session_id`, `chat_id`. `run_id` всегда `null` (контекстная переменная `set_run_id()` нигде не вызывается).

**Вывод:** `request.messages` — идеальный снимок, но capture — анонимный, ротируемый, опциональный диагностический поток. **НЕ подходит как primary store**, но пригоден для enrichment (§5).

---

## 3. Решения дизайна

### 3.1 НЕ связывать с `llm_payloads.jsonl` как primary store

Зафиксировать `llm_payloads.jsonl` в текущей роли — **диагностика + датасеты** (AGENTS.md §7.1.1). Причины:
- Ротация (20MB×3) несовместима с durable storage.
- Анонимность (нет chat-binding) — потребовала бы полной переработки capture pipeline.
- Это call-stream (N записей за run), а не per-chat context snapshot.

**Но:** добавить `user_id` + `session_id` в capture-запись через contextvar — дёшево и полезно для корреляции trace↔payload↔dataset. Low priority, отдельная опциональная фаза.

### 3.2 Новое хранилище `web_chat_context` — третий store

Архитектурно чистый путь — **отдельная таблица** в том же `memory.db`, не перегружающая ни transcript, ни agent-memory.

**Почему отдельная таблица, а не расширение `web_chat_messages`:** transcript — user-visible (role/content/tone), намеренно append-only и не сжимается. LLM-context — model-facing, сжимаемый, с tool-структурой. Смешивание сломает семантику transcript и UI.

**Почему НЕ перегрузка `SQLiteMemory.messages`:** messages keyed per-user. Делать per-chat partition в той же таблице = ломать 24 call-site + все каналы (telegram не имеет session). Новый store — чистая добавка.

**Схема:**
```sql
CREATE TABLE web_chat_context (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  INTEGER NOT NULL,
  user_id     TEXT NOT NULL,
  role        TEXT NOT NULL,              -- system | user | assistant | tool
  content     TEXT NOT NULL,              -- строка или JSON-сериализованный массив parts
  tool_calls       TEXT,                  -- JSON: [{id,type,function{name,arguments}}] для assistant
  tool_call_id     TEXT,                  -- для role=tool
  name             TEXT,                  -- для role=tool (имя инструмента)
  reasoning        TEXT,                  -- assistant reasoning_content
  seq              INTEGER NOT NULL,      -- порядок для точной реконструкции
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(session_id) REFERENCES web_chat_sessions(id) ON DELETE CASCADE
);
CREATE INDEX idx_web_chat_context_session ON web_chat_context(session_id, seq);
```

**`seq` вместо `timestamp` для детерминированного порядка.**

### 3.3 Ключ — opaque string `f"{user.id}:{session_id}"`

НЕ schema-изменение в `SQLiteMemory`, а thread opaque-ключа в AgentLoop:
- `User.memory_key()` остаётся per-user (для facts/file-changes/cache).
- Новый `context_key(session_id)` — опциональный, только для context-store.
- Telegram/CLI (без session) → fallback на `str(user.id)` (текущее поведение).
- Compressor/consolidator автоматически следуют за ключом (их `_summaries` dict keyed по строке).

### 3.4 Thread `session_id` в `AgentLoop.run()` (минимальная сигнатура)

```python
async def run(self, user: User, message: str, *, session_id: int | None = None, ...):
    context_key = f"{user.id}:{session_id}" if session_id else user.memory_key()
```

Persist в новый `ChatContextStore` (web_chat_context) на каждый assistant-turn + tool-round, **параллельно** с существующей записью в `SQLiteMemory` (которая остаётся для backward-compat / не-web каналов).

### 3.5 Активация чата = LOAD, не RESET

`_handle_activate_chat` вместо `reset_user_context`:
1. Загрузить `web_chat_context` для target session_id → полный message-list (с tool_calls/reasoning).
2. Записать в agent-memory (clear + re-add) — context восстановлен.
3. Cache-reset (KV-cache для нового prompt-prefix).

`reset_user_context` остаётся для `/new` (действительно новая сессия).

### 3.6 Compress-any-chat

`compress_now(session_id)`:
1. Загрузить `web_chat_context` для session_id.
2. `ContextCompressor.compress(messages)` — работает с полным schema (tool_calls присутствуют, в отличие от `get_history`).
3. Write-back: clear + re-add в `web_chat_context` для того session_id.
4. Cache-reset если session активный.

### 3.7 `ContextBuilder.build_initial` — режим full-reconstruction

Сейчас (context.py:194) дропает tool-role сообщения. Для restore нужен **новый режим** (флаг `full_context=True`), который сохраняет tool_calls/tool_results при загрузке из `web_chat_context`. Это изолированная правка в ContextBuilder, не ломающая существующий path.

---

## 4. Фазовый план реализации (4 спринта)

Каждая фаза — отдельный PR в `pre-release`, с gate-прогоном (ruff/pyright/pytest) и верификацией. Фазы **кумулятивные**, но Фаза 1 можно влить без изменения рантайм-поведения (просто копит данные).

### Спринт 1 (S1): Persistence — новая таблица + запись на каждый turn

**Цель:** `web_chat_context` таблица создаётся, AgentLoop пишет полный LLM-context (tool_calls + reasoning) на каждый assistant-turn и tool-round. Чтение/restore **ещё не подключено** — просто копим данные.

**Объём:** ~200 строк, BE-only, низкий риск.

**Шаги:**
1. **`ChatContextStore`** (новый класс, `channels/web/chat_context_store.py`): schema создания таблицы `web_chat_context` (ALTER-on-init паттерн, как WebChatStore), методы `append_context(session_id, user_id, role, content, tool_calls, tool_call_id, name, reasoning)`, `list_context(session_id) -> list[dict]`, `clear_context(session_id)`, `replace_context(session_id, messages)`. JSON-сериализация tool_calls.
2. **`AgentLoop.run(session_id=None)`** — thread опциональный `session_id`. Внутри: при каждом `context.add_tool_calls` / `add_tool_result` / `_save_turn` → параллельная запись в `ChatContextStore` (если session_id передан и store доступен).
3. **`AgentRequestService.run`** — проброс `session_id` в `loop.run()` (оркестратор уже знает session_id из `user_message.session_id`).
4. **Интеграция в orchestrator**: `ChatContextStore` создаётся рядом с `WebChatStore` (тот же db_path). Передаётся в AgentStack/config.
5. **Тесты**: schema creation, append/list/clear roundtrip, tool_calls сериализация корректна, FK cascade при delete_session.

**Верификация:** после PR — прогнать диалог, проверить что `web_chat_context` заполняется (SELECT), рантайм-поведение unchanged.

**Out of scope S1:** restore, compress-any-chat, UI.

---

### Спринт 2 (S2): Restore-on-activate

**Цель:** активация чата грузит его полный LLM-context в agent-memory (return вместо reset). Переключение чатов сохраняет контекст каждого.

**Объём:** ~150 строк, medium риск (меняет activate-контракт).

**Шаги:**
1. **`ContextBuilder.build_initial(history, full_context=False)`** — новый флаг. При `full_context=True`: НЕ дропать tool-role сообщения, восстанавливать tool_calls/tool_call_id/name из history-записей. Изолированная правка в `build_initial`.
2. **`AgentLoop.run(session_id)`** — при наличии session_id грузить history из `ChatContextStore` (вместо/в дополнение к `SQLiteMemory.get_history`). Если context-store непустой для session_id → использовать его (full schema); иначе fallback на старый path.
3. **`_handle_activate_chat`** (orchestrator) — вместо `reset_user_context`: load `web_chat_context` → clear agent-memory → re-add из context-store. Cache-reset. Если context-store пуст (старый чат) → fallback на текущее reset-поведение.
4. **Тесты**: restore roundtrip (запись → активация → контекст идентичен), empty context-store fallback, tool_calls корректно восстанавливаются.

**Верификация:** диалог в чате A → переключение на B → возврат на A → контекст A восстановлен (агент помнит tool_calls/предыдущие ответы).

---

### Спринт 3 (S3): Compress-any-chat

**Цель:** кнопка «Сжать» работает с любым чатом (просматриваемым), не только активным.

**Объём:** ~80 строк, low-medium риск.

**Шаги:**
1. **`AgentLoop.compress_now(session_id)`** — overload: если session_id передан, грузить context из `ChatContextStore` (полный schema), compress, write-back в context-store. Если None → текущее поведение (active in-memory).
2. **`AgentRequestService.compress_user_context(user, session_id=None)`** — thread session_id.
3. **WS `compress` handler** (orchestrator) — принимать опциональный `session_id` из payload; передавать в `compress_now`. In-flight lock остаётся.
4. **FE** (`useWebChatSession.compress`) — отправлять `chatId` в WS compress payload. Кнопка «Сжать» доступна для любого чата (read-only тоже — compress не меняет agent-memory активного, только context-store того чата).
5. **Тесты**: compress arbitrary session (load → compress → write-back → context-store корректно обновлён), active-chat compress unchanged.

**Верификация:** открыть read-only чат → «Сжать» → context-store сжат (SELECT показывает summary вместо старых сообщений).

---

### Спринт 4 (S4, опционально): Capture correlation + dataset export

**Цель:** `llm_payloads.jsonl` получает `user_id` + `session_id` для корреляции trace↔payload↔dataset.

**Объём:** ~30 строк, low риск.

**Шаги:**
1. **Contextvar population**: `AgentLoop.run` вызывает `set_run_id(run_id)` + новый `set_session_context(user_id, session_id)` contextvar (base.py уже имеет `_run_id_ctx`, просто не populated). Провайдеры читают его в capture.
2. **Capture record enrichment**: `payload.py` добавляет `user_id`, `session_id` в запись (вне allowlist — всегда, если contextvar set).
3. **(опц.) Export tool**: CLI/script для выгрузки `web_chat_context` + сшитого `llm_payloads` в training-dataset format (JSONL с полями instruction/messages/...). Отдельный модуль, не рантайм.

**Верификация:** llm_payloads.jsonl содержит user_id/session_id; export формирует валидный датасет.

---

## 5. Затронутые файлы (итог по всем фазам)

| Файл | Фаза | Изменения |
|------|------|-----------|
| `src/corpclaw_lite/channels/web/chat_context_store.py` (новый) | S1 | `ChatContextStore`: schema + CRUD |
| `src/corpclaw_lite/agent/loop.py` | S1-S3 | `run(session_id)`, persist в context-store, `compress_now(session_id)`, load-from-context-store |
| `src/corpclaw_lite/agent/context.py` | S2 | `build_initial(full_context=True)` режим |
| `src/corpclaw_lite/channels/service.py` | S1-S3 | `run`/`compress_user_context` thread session_id |
| `src/corpclaw_lite/channels/web/orchestrator.py` | S1-S3 | ChatContextStore создание, activate=load, compress handler session_id |
| `src/corpclaw_lite/llm/base.py` | S4 | `set_session_context` contextvar |
| `src/corpclaw_lite/logging/payload.py` | S4 | user_id/session_id в capture record |
| `frontend/web/src/chat/useWebChatSession.ts` | S3 | compress отправляет chatId |
| `tests/test_web_channel.py` | S1-S3 | +N тестов |
| `tests/test_agent_loop.py` | S1-S3 | +N тестов |

---

## 6. Риски и митигации

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| **S2 меняет activate-контракт** (return вместо reset) — может сломать существующие сценарии | Medium | S1 копит данные без изменения поведения → S2 включает restore только когда context-store непустой; fallback на reset для старых чатов. Полное тестирование activate-цикла. |
| **`build_initial(full_context=True)` ломает chat-template** (tool-сообщения в начале) | Medium | Изолировать в флаг; тестировать с реальной моделью (live_llm). Qwen/Gemma chat-template чувствительны к порядку ролей. |
| **Двойная запись** (SQLiteMemory + ChatContextStore) — рассинхрон | Low | ChatContextStore — source of truth для restore; SQLiteMemory остаётся для не-web каналов и backward-compat. Не пытаться синхронизировать в realtime. |
| **Производительность** — запись на каждый tool-round | Low | Async (`anyio.to_thread`), batch при возможности. Context-store компактнее чем llm_payloads (нет full request snapshot, только delta). |
| **Миграция старых чатов** — нет context-store, только transcript | Low | Fallback: S2 activate для empty context-store → текущее reset-поведение. Старые чаты стартуют «чистыми» (как сейчас). Backfill tool_calls невозможен (их не было). |

---

## 7. Что НЕ в scope (явно)

- **Per-chat KV-cache save/restore (L2 experimental cache, AGENTS.md §7.2)** — отдельная задача; это о context persistence, не slot cache.
- **Изменение single-active-chat invariant** — остаётся 1 активный чат на пользователя.
- **UI для просмотра raw LLM-context** (admin/debug) — не делаем.
- **Замена SQLiteMemory на ChatContextStore** — SQLiteMemory остаётся; ChatContextStore — дополнение для web/per-chat.
- **Real-time sync между SQLiteMemory и ChatContextStore** — намеренно расходятся.
- **Telegram/CLI per-chat** — Telegram не имеет session concept; fallback на per-user.

---

## 8. Связанные артефакты

- **Backlog:** B-063
- **Родственные PR:** #60 (compress button — S3 расширяет до any-chat), #54 (agent context — unaffected), #58 (empty start — unaffected)
- **AGENTS.md:** §7.1.1 (LLM payload capture), §Reasoning (known limitation note + B-063 reference)
- **Анализ:** проведён систематический разбор memory/context/logging/chat-store (3 Explore-агента, полный трейс по коду с line-numbers)
