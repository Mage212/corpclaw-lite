# Обновление документации — комплексный аудит 2026-04-10

## Summary

Полное обновление всей документации проекта для приведения в соответствие с текущим состоянием кодовой базы (~93 Python-файлов, 596 тестов, ~12,700+ строк). Аудит выявил 6 «фантомных» файлов в AGENTS.md, 2 крупных неописанных модуля (calibration/, onboarding/), устаревшие метрики в README и ARCHITECTURE, и 10 завершённых планов без пометки.

## Goals

- AGENTS.md точно отражает текущую структуру проекта и все реализованные подсистемы
- README.md содержит актуальные метрики
- docs/ARCHITECTURE.md обновлён с новыми модулями и метриками
- Завершённые планы архивированы, актуальные — обновлены
- CLAUDE.md синхронизирован с AGENTS.md

---

## Steps

### Step 1: Архивация завершённых планов

Переместить 10 файлов из `plans/` в `plans/archive/`:

| Файл | Причина |
|------|---------|
| `sprint-1-must.md` | Все задачи выполнены |
| `sprint-2-should.md` | Все задачи выполнены |
| `status-analysis-2026-03.md` | Снапшот устарел |
| `phase1-core.md` | Фаза 1 завершена |
| `phase2_status.md` | Фаза 2 завершена |
| `audit-phase5-complete.md` | Большинство gaps закрыты |
| `audit-fix-patchnotes.md` | Исправлено |
| `audit-hardening-changelog.md` | Исправлено |
| `2026-04-09-coverage-75.md` | Покрытие уже ~79% |
| `corpclaw-lite-design-v1.0-original.md` | Исторический архив |

**Статус:** [ ]

---

### Step 2: Обновление статусов актуальных планов

Обновить чекбоксы/статусы в 4 планах:

| План | Что сделать |
|------|-------------|
| `calibration-phase.md` | Отметить все реализованные шаги (код на 1,270 строк, 14 сценариев, CLI-команда — полностью готово). Добавить секцию "Реализация" наверх |
| `hermes-integration-analysis.md` | Отметить реализованные: Context Compression, Smart Approvals, Tool Output Pruning, Sanitize Tool Pairs |
| `code-review-phase7.md` | Обновить статус каждого finding'а: 17 исправлено, 3 частично, 3 не исправлено (low/by-design). Добавить сводную таблицу |
| `code-review-fixes.md` | Обновить счётчик тестов 580 -> 596. Пометить отложенные задачи в Phase 2 и 3 |

**Статус:** [ ]

---

### Step 3: AGENTS.md — обновление дерева проекта

**3.1. Удалить 6 «фантомных» записей:**
- `extensions/tools/guard.py` — не существует, ToolGuard в `security/tool_guard.py`
- `memory/manager.py` — не существует, логика в `sqlite.py`
- `memory/vector/` — не существует
- `logging/scrubber.py` — не существует, scrubber в `security/credential_scrubber.py`
- `logging/rotation.py` — не существует, ротация встроена в `agent_logger.py`
- `channels/registry.py` — не существует

**3.2. Добавить недокументированные файлы в дерево:**

Новое дерево (полное):

```
src/corpclaw_lite/
├── __init__.py
├── cli.py
├── exceptions.py
├── paths.py
├── templates.py
│
├── agent/
│   ├── loop.py
│   ├── context.py
│   ├── guards.py
│   ├── vision.py
│   ├── subagent.py
│   ├── factory.py           # NEW — сборка всего стека агента
│   ├── compressor.py        # NEW — 3-уровневое сжатие контекста
│   ├── prompt.py            # NEW — сборка промптов со скилами
│   └── constants.py
│
├── calibration/             # NEW MODULE — авто-калибровка под локальную модель
│   ├── loop.py
│   ├── runner.py
│   ├── scorer.py
│   ├── analyzer.py
│   ├── editor.py
│   ├── scenarios.py
│   └── trajectory.py
│
├── onboarding/              # NEW MODULE — онбординг пользователей
│   ├── engine.py
│   ├── finalizer.py
│   ├── questions.py
│   └── storage.py
│
├── runtime/                 # NEW — graceful shutdown
│   └── shutdown.py
│
├── utils/                   # NEW — вспомогательные утилиты
│   └── db.py
│
├── extensions/
│   ├── bootstrap.py         # NEW — единая инициализация расширений
│   ├── tools/
│   │   ├── base.py
│   │   ├── registry.py
│   │   └── builtin/
│   │       ├── files.py
│   │       ├── excel.py
│   │       ├── exec_script.py
│   │       ├── image.py
│   │       ├── web.py
│   │       ├── memory.py
│   │       ├── dispatch.py
│   │       ├── send_file.py     # NEW
│   │       └── _path_utils.py   # NEW
│   ├── skills/
│   │   ├── base.py
│   │   ├── loader.py
│   │   ├── registry.py
│   │   ├── matcher.py           # NEW — TF-IDF семантический матчинг
│   │   └── watcher.py           # NEW — hot-reload
│   ├── plugins/
│   │   ├── base.py
│   │   ├── loader.py
│   │   ├── registry.py
│   │   ├── sandbox_proxy.py     # NEW — subprocess proxy
│   │   ├── sandbox_worker.py    # NEW — subprocess worker
│   │   └── watcher.py           # NEW — hot-reload
│   ├── subagents/
│   │   ├── base.py
│   │   ├── registry.py
│   │   └── builtin/
│   └── mcp/
│       ├── client.py
│       ├── manager.py
│       ├── adapter.py
│       └── watcher.py           # NEW — hot-reload
│
├── channels/
│   ├── base.py
│   ├── cli.py
│   └── telegram/
│       ├── channel.py
│       ├── runner.py            # NEW
│       ├── orchestrator.py      # NEW
│       ├── formatting.py        # NEW
│       ├── upload.py            # NEW
│       ├── rate_limit.py        # NEW
│       ├── progress.py          # NEW
│       ├── callback_data.py     # NEW
│       ├── file_manager.py      # NEW
│       └── admin_notifier.py    # NEW
│
├── security/
│   ├── tool_guard.py
│   ├── network_policy.py
│   ├── credential_scrubber.py
│   └── ipc_auth.py
│
├── container/
│   ├── manager.py
│   ├── ipc.py
│   ├── policies.py
│   ├── proxy.py                 # NEW — IPC-прокси
│   └── agent_worker.py          # NEW — worker внутри контейнера
│
├── departments/
│   ├── manager.py
│   └── permissions.py
│
├── memory/
│   ├── sqlite.py
│   └── consolidation.py
│
├── users/
│   ├── models.py
│   └── manager.py
│
├── config/
│   ├── settings.py
│   ├── loader.py
│   ├── bootstrap.py             # NEW — загрузка SOUL.md
│   └── interpolation.py         # NEW — ${VAR:-default}
│
└── logging/
    ├── agent_logger.py
    └── health.py                # NEW — /health endpoint
```

**Статус:** [ ]

---

### Step 4: AGENTS.md — новые секции

**4.1. Добавить секцию "Calibration Phase" (после "Model Presets"):**

Описание подсистемы авто-калибровки (1,270 строк):
- `corpclaw-lite calibrate` CLI-команда
- 14 сценариев в `config/calibration_scenarios.yaml`
- Edit Surfaces: system prompt, tool descriptions, few-shots, settings
- Облачная модель правит конфиги -> локальная модель работает автономно
- Ключевые классы: `CalibrationLoop`, `ScenarioRunner`, `CalibrationScorer`, `ConfigEditor`

**4.2. Добавить секцию "User Onboarding":**

Описание онбординга (614 строк):
- Гибридный: детерминистический движок вопросов + LLM-финализация профиля
- `corpclaw-lite chat --setup` / `/setup` в Telegram
- Вопросы: роль, задачи, предпочтительный стиль общения
- Результат: департамент + персонализированный system prompt

**4.3. Добавить секцию "Hot Reload" (сборная):**

Описание hot-reload механизма (3 watcher'а):
- `SkillHotReloader` — polls `skills/*.md`
- `PluginWatcher` — polls `plugins/*/manifest.yaml`
- `MCPWatcher` — polls `config/mcp_servers.yaml`
- Все используют polling-based подход (не inotify/watchdog)

**4.4. Добавить секцию "Context Compression" (если отсутствует):**

3-уровневое сжатие (паттерн Hermes):
1. Prune tool results (обрезка длинных ответов)
2. Sanitize orphaned tool pairs
3. LLM summarization первого полу history

**Статус:** [ ]

---

### Step 5: AGENTS.md — правки по мелким расхождениям

**5.1. Добавить 4 недокументированные CLI-команды:**
```
uv run corpclaw-lite user-allow -t <telegram_id> -d <department>
uv run corpclaw-lite user-deny -t <telegram_id>
uv run corpclaw-lite user-revoke -t <telegram_id>
uv run corpclaw-lite calibrate [options]
```

**5.2. Добавить недокументированные конфиги:**
- `config/calibration_scenarios.yaml` — 14 сценариев калибровки
- `config/mcp_servers.yaml` — MCP-серверы (шаблон)
- `config/subagents/*.yaml` — 4 YAML субагента

**5.3. Исправить v1 путь:**
- Было: `../src/corpclaw/channels/telegram/` (поддиректория)
- Стало: `../src/corpclaw/channels/telegram*.py` (плоские файлы)

**5.4. Уточнить ruff rules:**
Добавить в секцию "Линтинг и форматирование":
```
# Ruff rules: E, F, I, UP, B, C4, SIM, G, A, PERF
```

**5.5. Обновить секцию "Что делать с v1 кодом":**
- `agent/executor/prompt_loop_executor.py` — указать, что адаптировано в `agent/loop.py` (509 строк)
- Уточнить, что `channels/telegram/` в v1 — плоские файлы

**Статус:** [ ]

---

### Step 6: README.md — обновление метрик

**6.1. Обновить кол-во тестов:**
- Было: "533 pass / 0 fail"
- Стало: "596 pass / 0 fail"

**6.2. Уточнить размер ReAct Loop:**
- Было: "~290 строк"
- Stо: убрать точную цифру или указать "loop.py + guards.py" (509 + 160)

**6.3. Проверить таблицу "Что работает сейчас":**
Убедиться что Calibration и Onboarding уже отмечены (по данным проверки — да, строки 74-75).

**Статус:** [ ]

---

### Step 7: docs/ARCHITECTURE.md — полное обновление

**7.1. Обновить метрики:**
- Было: "67 модулей, ~8K LOC, 301 тест"
- Стало: "~93 модуля, ~12.7K LOC, 596 тестов"

**7.2. Добавить секцию 10: "Calibration Phase":**
- Назначение, ключевые классы, Edit Surfaces
- Связь с `config/calibration_scenarios.yaml`

**7.3. Добавить секцию 11: "User Onboarding":**
- Гибридный движок, вопросы, LLM-финализация
- CLI и Telegram интеграция

**7.4. Добавить секцию 12: "Hot Reload & Watchers":**
- 3 watcher'а (skills, plugins, MCP)
- Polling-based подход

**7.5. Обновить "Key Metrics":**
Новая таблица LOC по компонентам с calibration и onboarding.

**7.6. Обновить "New Features (Hermes Integration)":**
- Отметить все 4 фичи как реализованные
- Добавить calibration и onboarding в таблицу

**7.7. Обновить "Launch/Run":**
- Добавить `corpclaw-lite calibrate`
- Добавить `corpclaw-lite user-allow/deny/revoke`

**7.8. Обновить дерево файлов в "Project Structure":**
Актуализировать в соответствии с Step 3.

**Статус:** [ ]

---

### Step 8: CLAUDE.md — синхронизация

После завершения всех правок в AGENTS.md — скопировать его содержимое в CLAUDE.md (зеркало).

**Статус:** [ ]

---

### Step 9: Финальная верификация

- [ ] `uv run ruff check src/` — 0 errors
- [ ] `uv run pyright src/` — 0 errors
- [ ] `uv run pytest tests/ -v` — все проходят
- [ ] Все пути файлов в AGENTS.md существуют (проверить скриптом)
- [ ] README.md тесты = фактическое кол-во
- [ ] ARCHITECTURE.md метрики актуальны
- [ ] plans/archive/ содержит 10 файлов, plans/ — 11 актуальных

**Статус:** [ ]

---

## Notes

### Расхождения для будущего (не в этом плане)

| Что | Причина |
|-----|---------|
| `memory/vector/` | Не реализован — убрать из документации когда будет ясно, нужен ли |
| Plugin script path traversal | Finding #4 частично открыт — security issue, не документационная задача |
| code-review-fixes Phase 2 (2.1-2.3) и Phase 3 (3.3-3.5) | Отложенные items — код, не документация |
| Health endpoint: "Опционально" в README | Уточнить статус при следующем спринте |

### Порядок выполнения

Steps 1-2 можно делать параллельно (архивация планов + обновление статусов).
Steps 3-5 — последовательно (AGENTS.md правится один раз).
Steps 6-7 — параллельно после Step 5 (README и ARCHITECTURE независимы).
Step 8 — после Step 5.
Step 9 — финальный.

Оценка: ~2-3 часа работы.
