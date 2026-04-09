# План исправлений по результатам код-ревью

## Summary

Полный план исправлений всех подтверждённых проблем, выявленных при код-ревью проекта CorpClaw Lite.
Все 580 тестов проходят, ruff и pyright чисты (0 ошибок). План устраняет архитектурные и логические проблемы.

## Goals

- Устранить 4 критических бага
- Улучшить типобезопасность (убрать широкие pyright suppressions)
- Унифицировать Registry API
- Убрать дублирование кода (DRY)
- Дополнить конфигурацию (bootstrap prompts, departments)

## Phases

### Фаза 1: Критические баги (4 пункта) — приоритет: CRITICAL

#### 1.1. `_previous_summary` утечка между пользователями
- **Файл:** `agent/compressor.py:36`
- **Суть:** `ContextCompressor` — singleton, `_previous_summary` сохраняется между вызовами разных пользователей. Саммари User A подмешивается в промпт саммаризации User B.
- **Решение:** Заменить `self._previous_summary: str | None = None` на `self._summaries: dict[str, str] = {}`, ключировать по `mem_key` (user telegram_id). В `_generate_summary` сохранять/читать по ключу.
- **Статус:** [x] Выполнено

#### 1.2. Anthropic preset params игнорируются
- **Файл:** `llm/anthropic.py:68-77`
- **Суть:** `kwargs.setdefault(k, v)` не перезаписывает уже установленный `max_tokens: 4096`. Поля `thinking`, `thinking_budget_tokens`, `system_prompt_prefix` вообще не обрабатываются, тогда как OpenAI-провайдер их обрабатывает.
- **Решение:**
  1. Инвертировать порядок слияния: сначала preset params, потом defaults
  2. `kwargs["max_tokens"] = kwargs.get("max_tokens", 4096)` — default только если не установлен preset'ом
  3. Добавить обработку `thinking`, `thinking_budget_tokens` (аналог `openai.py`)
  4. Добавить обработку `system_prompt_prefix`
  5. Аналогично для `chat_with_image` и `stream`
- **Статус:** [x] Выполнено

#### 1.3. `read_image` и `dispatch_subagent` недоступны департаментам
- **Файл:** `config/departments.yaml`
- **Суть:** Только `engineering` (с `"*"`) может использовать эти инструменты. Остальные департаменты не могут анализировать изображения или делегировать субагентам.
- **Решение:** Добавить `read_image` и `dispatch_subagent` в `allowed_tools` для:
  - `default` — оба
  - `marketing` — оба
  - `finance` — оба
  - `hr` — `read_image`
  - `analytics` — оба
  - `product` — оба
  - `it` — оба
- **Статус:** [x] Выполнено

#### 1.4. `assert` в production коде `orchestrator.py`
- **Файл:** `channels/telegram/orchestrator.py` (10 мест: строки 198, 265-270, 443-444, 487, 493)
- **Суть:** `assert self._xxx is not None` удаляется при `python -O`, приводя к непонятным ошибкам.
- **Решение:** Вынести в приватный метод `_ensure_started()`, проверяющий все preconditions:
  ```python
  def _ensure_started(self) -> None:
      if self._stack is None:
          raise RuntimeError("AgentStack not initialized — call start() first")
      if self._channel is None:
          raise RuntimeError("TelegramChannel not initialized")
      # ... и т.д.
  ```
  Заменить все assert-блоки на вызовы `_ensure_started()`.
- **Статус:** [x] Выполнено

---

### Фаза 2: Высокий приоритет (4 пункта) — приоритет: HIGH

#### 2.1. Широкие pyright-подавления (8 файлов)
- **Файлы:** `anthropic.py`, `openai.py`, `formatting.py`, `channel.py`, `container/manager.py`, `sqlite.py`, `users/manager.py`, `health.py`, `presets.py`
- **Суть:** File-level `# pyright: reportUnknownMemberType=false, ...` отключает strict mode для целых файлов. AGENTS.md требует strict pyright.
- **Решение:**
  1. Удалить file-level suppression комментарии
  2. Запустить `uv run pyright src/` — получить список ошибок
  3. Добавить targeted `# type: ignore[specific-rule]` только на конкретные строки (SDK calls)
  4. Где возможно — добавить explicit type annotations вместо suppressions
- **Статус:** [ ] Отложено (трудоёмко, 14 файлов)

#### 2.2. Синхронный I/O в 6 модулях
- **Файлы:** `presets.py:79`, `tools/registry.py:72`, `skills/loader.py:38`, `plugins/loader.py:31`, `subagents/registry.py:34`, `users/manager.py:122`
- **Суть:** AGENTS.md требует "anyio для async файловых операций". Все используют `path.read_text()` / `open()`.
- **Решение:**
  - Startup-only I/O (`PresetRegistry.from_yaml`, `SubagentRegistry.load_directory`, `ToolRegistry.load_overrides`) — оставить sync с комментарием `# startup I/O, runs once before event loop`
  - Runtime I/O (`SkillLoader.load_from_file`, `PluginLoader.load_manifest`, `UserManager._load_whitelist`) — добавить async-обёртки с `anyio.to_thread.run_sync`
- **Статус:** [ ] Отложено

#### 2.3. JSON файлы для ACL данных
- **Файл:** `users/manager.py`
- **Суть:** Whitelist и revoked sessions хранятся в JSON-файлах вместо SQLite. Split-brain с пользователями в БД. При потере JSON — теряется ACL.
- **Решение:**
  1. Создать SQLite таблицы `whitelist(telegram_id INTEGER PRIMARY KEY, department TEXT)` и `revoked_sessions(telegram_id INTEGER PRIMARY KEY)`
  2. `_load_whitelist` → SQL query с in-memory cache
  3. `_save_whitelist` → SQL INSERT/UPDATE
  4. При первом запуске — миграция из JSON-файлов (если существуют)
- **Статус:** [ ] Отложено

#### 2.4. Registry API несогласованность
- **Файлы:** `extensions/skills/registry.py`, `extensions/plugins/registry.py`, `extensions/subagents/registry.py`
- **Суть:** 4 реестра с разными соглашениями: getter (`get`/`get_skill`/`get_plugin`/`get_spec`), register behavior (raise vs overwrite), missing methods (unregister, items, dept filter).
- **Решение:**
  1. Getter: добавить алиас `get()` в каждый реестр (обратная совместимость)
  2. Register: добавить `allow_replace: bool = False` в SkillRegistry и PluginRegistry
  3. Unregister: добавить в SubagentRegistry
  4. Items: добавить во все три реестра
  5. Dept filter: добавить `get_allowed_subagents(user)` в SubagentRegistry
- **Статус:** [x] Выполнено

---

### Фаза 3: Средний приоритет (6 пунктов) — приоритет: MEDIUM

#### 3.1. DRY в `loop.py` — 6 повторений `try/except StorageError`
- **Файл:** `agent/loop.py` (строки 200, 246, 283, 357, 389, 405)
- **Решение:** Вынести helper:
  ```python
  async def _save_memory(self, mem_key: str, role: str, content: str, **kwargs) -> None:
      if not self._memory:
          return
      try:
          await self._memory.add_message(mem_key, role, content, **kwargs)
      except StorageError:
          logger.error("[user=%s] Failed to save %s message", ...)
  ```
  Заменить 6 блоков на вызовы `_save_memory()`.
- **Статус:** [x] Выполнено

#### 3.2. `build_agent_stack()` — God Function (216 строк)
- **Файл:** `agent/factory.py` (строки 152-367)
- **Решение:** Разбить на именованные функции:
  - `_build_security_stack(settings, provider) -> tuple[ToolGuard, PermissionChecker]`
  - `_build_extensions_stack(settings, registry, provider, ...) -> tuple[SubagentRegistry, ...]`
  - `_build_memory_stack(settings, provider) -> tuple[SQLiteMemory, Consolidator | None, Compressor | None]`
  - `_build_system_prompt() -> str | None`
  Основная функция сведётся к ~40 строкам вызовов.
- **Статус:** [x] Выполнено

#### 3.3. God-методы в `orchestrator.py`
- **Файл:** `channels/telegram/orchestrator.py`
- **Суть:** `start()` — 135 строк, `handle_message()` — 177 строк.
- **Решение:**
  - `start()` → извлечь: `_init_logging()`, `_init_skills_and_plugins()`, `_init_onboarding()`, `_init_hot_reloaders()`, `_init_health_endpoint()`
  - `handle_message()` → извлечь: `_check_access_control()`, `_handle_onboarding()`, `_build_system_prompt()`, `_execute_agent()`
- **Статус:** [ ] Отложено

#### 3.4. Onboarding duplication между CLI и Telegram
- **Файлы:** `cli.py` (строки 228-243), `orchestrator.py` (строки 120-130)
- **Суть:** 15-строчный блок конструирования OnboardingEngine идентичен.
- **Решение:** Вынести в метод `AgentStack.create_onboarding_engine(db_path: Path) -> OnboardingEngine | None`. Вызывать из обоих мест. Execution flow (CLI input vs Telegram messages) остаётся channel-specific.
- **Статус:** [ ] Отложено

#### 3.5. Temporary `User(id=0, ...)` (2 места)
- **Файлы:** `orchestrator.py:284`, `channel.py:338`
- **Суть:** Создаётся throwaway User-объект только для передачи telegram_id.
- **Решение:** Добавить перегрузку `Channel.send_message`: принимать `int | User`. Для `int` — отправлять по `chat_id` напрямую. Убрать Temporary User creation.
- **Статус:** [ ] Отложено

#### 3.6. `datetime.now()` → `time.monotonic()` в `rate_limit.py`
- **Файл:** `channels/telegram/rate_limit.py` (строки 32-33)
- **Суть:** Wall-clock time для interval measurement — некорректно при смене системных часов.
- **Решение:** Заменить `datetime.now()` на `time.monotonic()`, хранить `float` вместо `datetime`. Убрать `from datetime import datetime, timedelta`.
- **Статус:** [x] Выполнено

---

### Фаза 4: Низкий приоритет — Code Smells (10 пунктов) — приоритет: LOW

#### 4.1. `RunStats.status: str` → `Literal`
- **Файл:** `agent/loop.py:81`
- **Решение:** `status: Literal["ok", "budget", "loop", "timeout", "error"] = "ok"`
- **Статус:** [x] Выполнено

#### 4.2. `getattr(tool, "risk_level", None)` → прямой доступ
- **Файл:** `agent/loop.py:480`
- **Решение:** `risk = tool.risk_level.value if tool else None` (tool проверяется выше)
- **Статус:** [x] Выполнено

#### 4.3. `Provider` → `@runtime_checkable`
- **Файл:** `llm/base.py:40`
- **Решение:** Добавить `@runtime_checkable` для консистентности с `VisionProvider`
- **Статус:** [x] Выполнено

#### 4.4. `LLMResponse.usage` → typed model
- **Файл:** `llm/base.py`
- **Решение:**
  ```python
  class TokenUsage(BaseModel):
      input_tokens: int = 0
      output_tokens: int = 0
  ```
  Заменить `usage: dict[str, int]` на `usage: TokenUsage`. Обновить `anthropic.py` и `openai.py`.
- **Статус:** [x] Выполнено

#### 4.5. `field(default_factory=lambda: list[str]())` → `field(default_factory=list)`
- **Файлы:** `skills/base.py:32`, `subagents/base.py:26-27`, `plugins/base.py:31-32, 42-43`
- **Статус:** [x] Выполнено

#### 4.6. `ipc: Any` → `ContainerIPC` в `factory.py:135`
- **Файл:** `agent/factory.py`
- **Решение:** Добавить `TYPE_CHECKING` import `ContainerIPC` и типизировать параметр
- **Статус:** [x] Выполнено

#### 4.7. `ContainerPolicies` → standalone function
- **Файл:** `container/policies.py`
- **Решение:** Убрать класс, оставить функцию `build_docker_args(...)` (единственный static method)
- **Статус:** [x] Выполнено

#### 4.8. Мёртвый код: `PluginManifest.requires`, `force_reload`
- **Файлы:** `plugins/base.py:32`, `plugins/loader.py:49`
- **Решение:**
  - `requires` — удалить поле (не используется нигде)
  - `force_reload` — удалить параметр (не реализован)
- **Статус:** [x] Выполнено

#### 4.9. `hash()` → `hashlib` в `skills/matcher.py`
- **Файл:** `extensions/skills/matcher.py:354-355`
- **Суть:** Python `hash()` рандомизируется при каждом запуске — индекс всегда перестраивается
- **Решение:** Использовать `hashlib.sha256(...).hexdigest()` для стабильного дайджеста
- **Статус:** [x] Выполнено

#### 4.10. `logging.basicConfig` на уровне модуля в `agent_worker.py`
- **Файл:** `container/agent_worker.py:22`
- **Решение:** Перенести внутрь `if __name__ == "__main__"` блока или в `process_request()`
- **Статус:** [x] Выполнено

---

### Фаза 5: Конфигурация (2 пункта) — приоритет: LOW

#### 5.1. Недостающие bootstrap prompts для департаментов
- **Директория:** `config/bootstrap/departments/`
- **Имеется:** `admin.md`, `development.md`, `marketing.md`
- **В departments.yaml:** default, engineering, it, marketing, finance, hr, analytics, product
- **Добавить:** `default.md`, `engineering.md`, `it.md`, `finance.md`, `hr.md`, `analytics.md`, `product.md`
- **Примечание:** `development.md` ↔ `engineering` — нужен алиас или переименование
- **Статус:** [x] Выполнено

#### 5.2. Недостающие subagent prompts
- **Директория:** `config/bootstrap/subagents/`
- **Имеется:** `document.md`, `research.md`
- **В config/subagents/:** research-agent, document-agent, execution-agent, filesystem-agent
- **Добавить:** `execution.md`, `filesystem.md`
- **Обновить:** `prompt_path` в YAML-файлах субагентов
- **Статус:** [x] Выполнено

---

## Порядок выполнения

| Порядок | Фаза | Пунктов | Зависимости |
|---------|-------|---------|-------------|
| 1 | Фаза 1 (Critical) | 4 | Нет |
| 2 | Фаза 4 (Code Smells) | 10 | Нет, быстрые правки |
| 3 | Фаза 3 (Medium) | 6 | 3.2 и 3.3 лучше делать вместе |
| 4 | Фаза 2 (High) | 4 | 2.1 зависит от 4.4 |
| 5 | Фаза 5 (Config) | 2 | Нет зависимостей от кода |

**Полная проверка после каждой фазы:**
```bash
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

## Notes

- Все 580 тестов проходят, ruff и pyright — 0 ошибок (до начала исправлений)
- Широкие pyright suppressions маскируют реальные проблемы — после их снятия возможны новые ошибки
- Конфигурационные правки (Фаза 5) можно делать параллельно с любой другой фазой
