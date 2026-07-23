# AGENTS.md — CorpClaw Lite

Этот файл — первое, что нужно прочитать AI-агенту перед любой работой в этом репозитории.

---

## Что такое этот проект

**CorpClaw Lite** — это редизайн с нуля корпоративного AI-агента [CorpClaw](../), написанный на чистом Python с учётом опыта и ошибок первой версии.

**Назначение:** Надёжный Python AI-агент для корпоративного закрытого контура — Telegram-бот, который выполняет рутинные задачи через скиллы/плагины/субагенты, работает с **локальными LLM** и управляет доступом по департаментам.

**Отличие от v1:** Не enterprise middleware и не конкурент OpenClaw. Фокус на:
- Простоте (минимум кода для максимума ценности)
- Работе с локальными LLM (Qwen, Mistral, Llama через Ollama/vLLM/LM Studio)
- Безопасности встроенной в ядро, а не добавленной поверх
- Лёгком расширении через манифесты (skills, plugins, subagents, channels)

---

## Ключевые документы

| Документ | Описание |
|----------|----------|
| [`plans/corpclaw-lite-design.md`](plans/corpclaw-lite-design.md) | **Главный дизайн-документ** — архитектура, структура, фазовый план |
| [`../docs/FINAL_CRITICAL_ANALYSIS.md`](../docs/FINAL_CRITICAL_ANALYSIS.md) | Финальный критический анализ v1 и стратегия ("Путь C") |
| [`../docs/CRITICAL_ANALYSIS_OVERENGINEERING.md`](../docs/CRITICAL_ANALYSIS_OVERENGINEERING.md) | Разбор оверинжиниринга v1 — чего НЕ повторять |

**Референсные проекты** (в `../references/`):
- [`../references/openclaw/`](../references/openclaw/) — OpenClaw (TypeScript, 857K LOC). Изучать для понимания зрелых архитектурных решений: session loop, file-based approvals, plugin SDK
- [`../references/NemoClaw/`](../references/NemoClaw/) — NVIDIA NemoClaw: security overlay pattern. **Ключевой источник:** 4 слоя безопасности (Network deny-by-default + Filesystem + Process + Inference rerouting) реализованы через YAML-политики в ~2,287 строк
- [`../references/CoPaw/`](../references/CoPaw/) — Alibaba CoPaw (Python). **Ключевой источник:** Tool Guard через mixin pattern, YAML-правила severity levels, one-shot approvals; unified channel pattern; skills как markdown, hot-reload

**Рабочая кодовая база v1** (в `../src/corpclaw/`):
- `../src/corpclaw/llm/` — LLM провайдеры; **`xml_tool_calling.py` скопирован в `src/corpclaw_lite/llm/`** — это критический модуль для локальных LLM, не трогать структуру
- `../src/corpclaw/agent/executor/prompt_loop_executor.py` — рабочий пример ReAct цикла (600+ строк) — брать за основу при написании нового loop.py, (цель ~400 строк была пересмотрена; цикл вырос до ~2600 строк включая fill-close nudge, auto-finalize cascade, streaming telemetry, context persistence)
- `../src/corpclaw/agent/guards.py` — `SimpleBudgetGuard` и `SimpleProgressGuard` — переносить как есть
- `../src/corpclaw/container/` — рабочая Docker-изоляция — адаптировать, упростив state machine
- `../src/corpclaw/memory/sqlite.py` — рабочий SQLite бэкенд памяти — переносить как есть
- `../src/corpclaw/channels/telegram*.py` — рабочие Telegram-модули (плоские файлы, не поддиректория) — адаптировать под новый Channel Protocol

---

## Build / Lint / Test

**Package Manager:** `uv` — всегда и только `uv`. Никогда не использовать `pip` напрямую.

```bash
# Запуск команд
uv run <command>

# Синхронизация зависимостей
uv sync

# Добавление зависимости
uv add <package>
```

**Линтинг и форматирование:**
```bash
uv run ruff check src/ --fix
uv run ruff format src/
```
Ruff rules: `E, F, I, UP, B, C4, SIM, G, A, PERF` (line-length: 100)

**Проверка типов (pyright, не mypy):**
```bash
uv run pyright src/
```

**Тесты:**
```bash
uv run pytest tests/ -v
uv run pytest tests/test_agent_loop.py -v
uv run pytest -k "test_name" -v
uv run pytest tests/ --cov=src/corpclaw_lite --cov-report=term-missing
```

**Ручные live LLM тесты** — не входят в обычный пул и запускаются только явно:
```bash
CORPCLAW_LIVE_LLM_TESTS=1 \
CORPCLAW_LIVE_LLM_BASE_URL=http://192.168.193.178:8080 \
CORPCLAW_LIVE_LLM_MODEL=gpt-oss-20b-UD-Q4_K_XL \
CORPCLAW_LIVE_LLM_SLOTS=0,1,2,3 \
uv run pytest tests/live_llm/ -v -s -o addopts=''

CORPCLAW_LIVE_LLM_TESTS=1 \
CORPCLAW_LIVE_LLM_RUN_SLOW=1 \
CORPCLAW_LIVE_LLM_BASE_URL=http://192.168.193.178:8080 \
CORPCLAW_LIVE_LLM_MODEL=gpt-oss-20b-UD-Q4_K_XL \
CORPCLAW_LIVE_LLM_SLOTS=0,1,2,3 \
uv run pytest tests/live_llm/test_02_cache_file_roundtrip.py::test_cache_file_roundtrip_large -v -s -o addopts=''
```
Без `CORPCLAW_LIVE_LLM_TESTS=1` эти тесты должны уходить в skip. `tests/live_llm/`
исключён из обычного `pytest tests/`, чтобы CI и локальные быстрые проверки не зависели от
реального llama-server.

**Полная проверка перед завершением работы:**
```bash
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

**Pre-push quality gate (локальный git hook):** те же 4 gate'а (ruff check, ruff format --check, pyright, pytest с `-x` fail-fast) гоняются перед каждым `git push` и блокируют push при любом провале. Хук версионирован в `.githooks/`, активируется одноразово после clone:

```bash
bash scripts/install-hooks.sh          # или вручную: git config core.hooksPath .githooks
```

Bypass: `git push --no-verify` или `CORPCLAW_SKIP_HOOKS=1 git push` — **только** когда CI гарантированно прогонит (PR в pre-release/main) и с объяснением в PR. Tag-push'и (`refs/tags/*`) пропускаются автоматически.

**Важно:** pyright пинят в `pyproject.toml` до конкретной версии (`==X.Y.Z`, совпадающей с CI). Бамп pyright — осознанный акт с полным gate-прогоном. Это устраняет дрейф версий локально/CI (корневая причина релиз-only pyright-провала 0.1.12).

CI гоняется на `push`/`pull_request` в **обе** `main` и `pre-release` — regressions ловятся на PR, не только на релизе.

---

## CLI команды

```bash
uv run corpclaw-lite chat                       # Интерактивный CLI чат
uv run corpclaw-lite chat --setup               # Запуск онбординга
uv run corpclaw-lite telegram                   # Запуск Telegram-бота
uv run corpclaw-lite web                        # Запуск Web-канала (браузерный чат)
uv run corpclaw-lite user-list                  # Список пользователей
uv run corpclaw-lite user-create -t <telegram_id> -d <department>
uv run corpclaw-lite user-link-telegram -t <id> -u <user>  # Привязка TG к каноническому user
uv run corpclaw-lite user-link-web -u <user>              # Привязка web-логина к user
uv run corpclaw-lite user-migrate-canonical-ids           # Миграция на канонические user IDs
uv run corpclaw-lite user-allow -t <telegram_id> -d <department>  # Добавить в whitelist
uv run corpclaw-lite user-deny -t <telegram_id>                   # Удалить из whitelist
uv run corpclaw-lite user-revoke -t <telegram_id>                 # Заблокировать сессию
uv run corpclaw-lite web-user-create -u <user> -p '<password>'    # Web-пользователь
uv run corpclaw-lite web-user-link -t <tg_id> -u <user> -p '<password>'  # Привязать web к TG-аккаунту
uv run corpclaw-lite web-user-password -u <user> -p '<password>'  # Сменить пароль web-пользователя
uv run corpclaw-lite web-user-merge <src> <dst>                    # Объединить web-аккаунты
uv run corpclaw-lite containers                 # Активные Docker-контейнеры
uv run corpclaw-lite prune                      # Удаление idle-контейнеров
uv run corpclaw-lite skill list                 # Список загруженных скилов
uv run corpclaw-lite plugin list                # Список плагинов
uv run corpclaw-lite generate skill <name>      # Создание шаблона скила
uv run corpclaw-lite generate plugin <name>     # Создание шаблона плагина
uv run corpclaw-lite generate subagent <name>   # Создание шаблона субагента
uv run corpclaw-lite calibrate [options]        # Авто-калибровка под модель
uv run corpclaw-lite memory-worker enable -u <id>       # Включить memory worker для пользователя
uv run corpclaw-lite memory-worker disable -u <id>      # Отключить
uv run corpclaw-lite memory-worker status               # Статус всех worker'ов
uv run corpclaw-lite memory-worker run -u <id>          # Разовый запуск (ops/debug)
uv run corpclaw-lite schedule list -u <id>              # Запланированные задачи
uv run corpclaw-lite schedule accept -u <id> -t <task_id>    # Подтвердить задачу
uv run corpclaw-lite schedule dismiss -u <id> -t <task_id>   # Отклонить задачу
uv run corpclaw-lite headless-run -u <id> -t "задача"        # Запуск агента без inbound-сообщения
uv run corpclaw-lite notify-user -u <id> -m "сообщение"      # Proactive push в system session
uv run corpclaw-lite eval                                    # Eval harness (B-060)
```

---

## Архитектура: Ключевые принципы

### 1. Simple ReAct Loop (НЕ LLM-based planning)

Агентный цикл — классический ReAct без LLM-планировщиков:
```
Сообщение → Сборка контекста → LLM вызов
→ tool_calls? → Выполнить → добавить результаты → повторить
→ нет tool_calls? → Ответ → сохранить в память
```
Гарды (`agent/guards.py`) — **6 классов, все детерминированные** (regex/hash/wall-clock, БЕЗ LLM):

1. `SimpleBudgetGuard` — max_iter/tools/time + queue-pause (D-040)
2. `SimpleProgressGuard` — loop detection по ошибкам (B-069)
3. `ResultDedupGuard` — SHA-256 по идентичным результатам, блокировка циклов (B-055/GAIA)
4. `PlanningTextGuard` — bilingual фразы + tool-artifact regex, блокировка mid-workflow остановок (B-056/GAIA)
5. `SoftDeadline` — wall-clock closing-mode (B-046)
6. `TerminalToolMandate` — nudge/restrict к терминальной воронке research (B-047)

**НЕЛЬЗЯ** добавлять: `TaskPlanner`, `TaskVerifier`, `ObjectiveStorage`, `ProgressGuard LLM-based`.

**research.py — намеренно крупная подсистема ядра** (~1590 строк), не god-object и не нарушение «simple». Это `ResearchRuntime` (on-disk state machine) + 7 тонких тулзов (search/fetch/read/list/store_fact/list_facts/finalize) + хелперы локализации (detect_language, двуязычные шаблоны ru/en). **Почему в ядре, не в plugin:** plugin-модель stateless (один subprocess = один execute), а research — 7 stateful тулзов с shared in-memory `ResearchRuntime`; гардам B-046/B-047 нужен runtime-доступ. **Без LLM внутри** — валидация отчётов чисто программная. «Simple» в §1 относится к ReAct-loop'у, не к каждой подсистеме.

### 2. Субагенты — изолированные исполнители

Основной агент знает только: `list_files`, `read_file`, `search_files` + каталог субагентов.
При задаче → основной агент вызывает субагента с полным контекстом задачи.
Субагент: чистая история + специализированные инструменты + свои скилы → возвращает компактный результат.

**Это критично для локальных LLM** — снижает нагрузку на контекстное окно основного агента на 60-80%.

### 3. read_image — отдельный LLM-вызов

`read_image` НЕ возвращает изображение в контекст. Он делает отдельный вызов к vision-провайдеру и возвращает текстовое описание. Это выявленная особенность работы с локальными моделями.

### 4. Единая система расширений через манифесты

Все расширения регистрируются через `manifest.yaml` с полями `name`, `version`, `type`, `description`, `allowed_departments`, `components`. Типы: `tool`, `skill`, `plugin`, `subagent`, `channel`.

**Нет** `ExtensionCatalog`, `CompatibilityStatus`, `EntityManifest`, `AvailabilityResolver` — это было главной ошибкой v1.

#### Приватные расширения и граница публичного/приватного

CorpClaw Lite — **опенсорс-проект**, но параллельно появляются корпоративные доработки (инструменты/скилы/субагенты под внутренние системы, RBAC-правила, системные промпты), которые **не должны** попасть в публичный репозиторий.

**Главный принцип — граница проходит по данным, а не по коду.** Система расширений по задумке не требует правок ядра для нового инструмента/скила/субагента, значит 99% корпоративных доработок — это новый контент (`.md`, `.yaml`, `manifest.yaml`), а не код `src/`. Поэтому:

- **Код RBAC, loop, провайдеры, ToolRegistry, BootstrapLoader** — generic, публикуется в опенсорс.
- **`skills/*.md`, `plugins/*/`, `config/subagents/*.yaml`, `config/bootstrap/*.md`, `config/mcp_servers.yaml`, `config/departments*.yaml`, корпоративные дополнения в `tool_guard_rules.yaml`/`network_policy.yaml`** — приватный контент, живёт в overlay, в опенсорс не попадает.

**Механика overlay** (реализовано в `src/corpclaw_lite/extensions/paths.py` → `resolve_dirs`): ядро грузит расширения из дефолтных директорий **плюс** из `config/settings.yaml → extensions.extra_paths` (mirror-layout — каждый overlay-путь повторяет структуру проекта: `<extra>/skills/`, `<extra>/config/subagents/` и т.д.). Overlay-entries **перекрывают** дефолтные по id/имени (skills/plugins/subagents/bootstrap — override; departments — **union** merge, т.к. это множества разрешений). Приватный overlay физически лежит в отдельном приватном репо (напр. `corpclaw-corp`) и монтируется в деплое через env `${CORPCLAW_PRIVATE_EXTENSIONS}`.

**Перед написанием кода — сначала ответить: ядро или расширение?**
- Новый инструмент/скил/субагент → 99% это **расширение** (приватный overlay). Ядро не трогаем.
- Правка в `loop.py`, `tool_guard.py`, `ToolRegistry`, провайдерах → это **ядро** (публичный репо, feature-ветка + PR).
- Если фича требует и того, и другого — **расщепить на два PR**: generic-хук в публичный репо, приватное расширение в overlay.

**Контракт версий:** плагины декларируют `requires_core` в манифесте (`^0.2.1` = совместим с 0.2.x). Ядро проверяет при загрузке (`PluginRegistry.register`, warn-and-skip при несовпадении) — страховка от молчаливого падения overlay-плагина при развитии ядра.

`AGENTS.md` намеренно не упоминает конкретные корпоративные имена — только принцип. Конкретика живёт в overlay.

#### Двух-репо-модель и её ограничения (HARD RULES)

Overlay — это **отдельный приватный репозиторий** (`corpclaw-corp`), сиблинг публичного. **Приватных файлов в публичном репо быть не должно вообще** — даже под `.gitignore`. gitignore не гарантирует от утечки (`git add -f`, свежие clone без нужного `.gitignore`, merge-tools, stash). Поэтому модель — не «приватные папки тут + gitignore», а **два независимых дерева, склеиваемых путём в рантайме**:

```
~/coding_projects/corpclaw/
├── corpclaw-lite/        ← ПУБЛИЧНЫЙ: src/, skills/, plugins/, config/ (НИКАКИХ приватных файлов)
│   └── config/settings.yaml → extensions.extra_paths: ["${CORPCLAW_PRIVATE_EXTENSIONS}"]
└── corpclaw-corp/        ← ПРИВАТНЫЙ overlay: skills/, plugins/, config/{bootstrap,subagents,departments.yaml,mcp_servers.yaml}
```

Компоновка — через `CORPCLAW_PRIVATE_EXTENSIONS=/abs/path/to/corpclaw-corp`, ядро само подхватывает overlay через `resolve_dirs`. **Репозитории никогда не мержатся друг в друга в git-смысле** — «слияния» не существует, его не нужно контролировать или синхронизировать. Зависимость однонаправленная: overlay зависит от ядра (через `requires_core`), ядро ничего не знает про overlay.

**Жёсткие правила (соблюдать всегда):**
1. **Никогда не класть приватные файлы в этот репо** — даже в gitignored-папку. Приватный контент живёт только в `corpclaw-corp`.
2. **Держать контракт расширений стабильным и аддитивным** — новые поля манифестов опциональны с дефолтами; сигнатуры `Tool.execute`, `resolve_dirs`, `load_directory` не ломаем без мажор-бампа. Overlay зависит от этой поверхности.
3. **Overlay декларирует `requires_core`** — при развитии ядра overlay либо продолжает работать (контракт стабилен), либо падает явно через warn-and-skip, никогда молча.
4. **Фича, требующая и ядра, и overlay → расщепить на два PR**: generic-хук в публичный репо (feature-ветка от `pre-release` + PR), приватное расширение — в overlay.

**Известное ограничение:** `requires_core` грубоват (minor-уровень для 0.x), тонкую несовместимость контракта не поймает. Компенсируется дисциплиной: поверхность расширений — стабильная граница. Если станет узким местом — усиление отдельной задачей (напр. контракт-тесты для расширений).

### 5. Security встроен в ядро

Стек безопасности выполняется **до** вызова инструмента:
```
ChannelAuth (telegram/web access control, before agent loop) → PermissionCheck (dept RBAC, tool allowlist) → ToolGuard (YAML rules, inline approve/deny) → Container (IPCToolProxy for sandboxed tools)
CredentialScrubber применяется к tool results (через ToolRegistry) и логам (как logging Filter), не как синхронный gate в цепочке.
```
- **ToolGuard** (по образцу CoPaw): YAML-правила, severity CRITICAL/HIGH/MEDIUM/INFO, inline approve/deny
- **NetworkPolicy**: deny-all (`network_mode: none`) — zero outbound из контейнера. Allowlist не нужен (контейнер не использует сеть). Эгресс-контроль через SSRF-правила `tool_guard_rules.yaml` (WEB_FETCH_PRIVATE_IP HIGH, WEB_FETCH_SENSITIVE_PATHS MEDIUM, RESEARCH_FETCH_* аналоги) + `is_global` deny в `web_fetch` (CGNAT `100.64/10`, cloud-metadata `169.254.169.254`, private/loopback/link-local/multicast) для web-тулзов, работающих на хосте.
- **Container hardening** (`container.strict_capabilities`, по умолчанию `true`, B-064): `cap_drop: ALL` + deny-by-default seccomp-профиль + явный non-root `user: agent` (UID 1001, продублирован из `USER agent` образа). Контроль для `exec_script` (LLM-управляемый shell) — изоляция контейнера, не regex-блоклист ToolGuard'а (который обходится `python3 -c`). `strict_capabilities: false` — opt-out для dev/debug (контейнер всё равно non-root через `USER agent`).
- **IPC Auth**: HMAC + nonce — **обязательна**, fail-fast при отсутствии `CORPCLAW_IPC_SECRET`
- **Sprint 3 hardening**: `container.seccomp_missing_fatal` (default true) — fail-fast при отсутствии seccomp профиля; `logging.health_host` (default 127.0.0.1) — loopback-only для /health; `agent.shutdown_timeout_seconds` (default 30s) — bounded orchestrator shutdown; `telegram.allow_groups` (default false) — только private chats

### 6. Каналы — расширения с Protocol

CLI — базовый канал. Telegram — первый плагин-канал. Channel Protocol:
```python
async def send_message(chat_id, text, **opts) -> None
async def request_approval(chat_id, action, details) -> bool  # inline кнопки
```
Telegram поддерживает MarkdownV2/HTML форматирование и inline Approve/Deny кнопки.

### 7. Model Profiles + Sampling Profiles + PhasePolicy (D-056)

Пресет расщеплён (D-056) на два ортогональных слоя. `ModelProfile` — свойства
модели (`thinking_parser`, `system_prompt_prefix`, `default_inference`).
`SamplingProfile` — свойства задачи/фазы (`thinking_mode`, `thinking_budget`,
`inference_overrides`, ссылка на `ModelProfile`). Хранятся в
`config/model_presets.yaml` в блоках `models:` + `sampling:`:

```yaml
models:
  gemma4-26b-a4b:
    thinking_parser: {source: native}     # "content" (парсинг тегов) или "native" (reasoning_content)
    default_inference: {temperature: 1.0, top_p: 0.95, top_k: 64}  # офиц. рекомендация

sampling:
  gemma4-default:
    model: gemma4-26b-a4b                 # model-scoped: inference_overrides применяются только при match
    thinking_mode: default                # default | off | budget
  gemma4-fast:
    model: gemma4-26b-a4b
    thinking_mode: off
```

Routing rules ссылаются на sampling-профиль по имени: `sampling: "gemma4-default"`
(legacy `preset:` работает через back-compat reader). Sampling-профили
**model-scoped**: при mismatch `sampling.model` и роут-модели → warn + skip
`inference_overrides` (thinking_mode сохраняется).

Ключевые модули:
- `src/corpclaw_lite/llm/presets.py` — `ModelProfile`, `SamplingProfile`,
  `ThinkingConfig`, `PresetRegistry` (legacy `ModelPreset` deprecated alias + bridge)
- `src/corpclaw_lite/llm/base.py` — `RequestOptions`, `ThinkingOverride`
  (per-call contextvar, ортогональный `BackendRequestOptions`)
- `src/corpclaw_lite/agent/phase_policy.py` — `PhasePolicy`, `DefaultPhasePolicy`
- `config/model_presets.yaml` — определения профилей
- `RoutingRule.sampling` — ссылка на sampling-профиль по имени

**Приоритет merge:** `model_profile defaults < sampling overrides < RequestOptions
(per-call) < backend extra_body (transport)`. Thinking-mode: `off` →
`enable_thinking=False` + подавление prefix; `default` (force-on в aggregation) →
`enable_thinking=True`, отменяет sampling-off.

**PhasePolicy** — per-call переключение thinking по фазе задачи: closing-mode →
off; research gathering → off, aggregation → on (monotonic: `research_list_facts`
в cumulative tools_used → все последующие = aggregation). Main-agent default
phase = no-op.

**Reasoning:**
- Хранится в `ChatContextStore` (колонка `reasoning` в `web_chat_context`)
- Логируется, но **не попадает** в контекст агента (экономия токенов)
- Подготовлено к будущему интеллектуальному подключению в контекст

**Полное хранение LLM-context per chat (B-063 + Sprint 2B / D-078).**
User-visible транскрипт — `web_chat_messages` (role/content/tone). LLM-facing контекст —
**`ChatContextStore`** (`channels/web/chat_context_store.py`, таблица `web_chat_context`):
`role/content/tool_calls/tool_call_id/name/reasoning/seq`, `UNIQUE(session_id, seq)`,
`FK … ON DELETE CASCADE` к `web_chat_sessions`. После 2B (B-102…106) store — **единственный**
transcript при наличии `session_id` (web + Telegram virtual session); dual-write в
`SQLiteMemory.messages` и `MemoryConsolidator` **удалены**.

| Слой | Таблица / API | Роль (0.2.7+) |
|------|----------------|---------------|
| `ChatContextStore` | `web_chat_context` | sole LLM transcript (load/persist/compress) |
| `SQLiteMemory` | `memory_facts` only | cross-chat facts (`store_fact` / `recall_facts`) |
| `WebChatStore` | `web_chat_messages` | UI transcript (не LLM replay) |

Поток persist в `AgentLoop.run(session_id)` (`agent/context_target.py` — contextvar-изоляция):
user-message → assistant tool_calls → tool-result → финальный assistant. Terminal-tools
(`read_image`) skip tool-role. Load: `list_context` only (нет `get_history` fallback).
CLI/subagent: `session_id=None` → empty history, no store writes. Compress: on-demand
`compress_now` + mid-run (B-124) через `_compress_chat` / `_compress_store_transcript`
(store-first `replace_context`). `PRAGMA foreign_keys=ON` → CASCADE.

**Корреляция capture (B-063 S4).** `llm/base.py` дополнительно несёт `_capture_user_id`/
`_capture_session_id` contextvar'ы (`set_capture_context`/`reset_capture_context`), которые
населяются в `AgentLoop.run()`; `logging/payload.py → capture(user_id, session_id)` пишет их
в каждую запись `logs/llm_payloads.jsonl` (вне allowlist, всегда) → корреляция траектории с
конкретным чатом/пользователем для будущего сбора датасета дообучения. Ранее `set_run_id(stats.run_id)`
тоже не вызывался (run_id всегда был null) — пофикшено в S4.

**НЕЛЬЗЯ** хардкодить логику thinking/reasoning в провайдерах — всё через пресеты.

### 7.1 Backend LLM Streaming — подкапотная телеметрия

Основной `AgentLoop` использует streaming **только как внутренний слой наблюдения и
диагностики**, а не как пользовательский streaming-ответ.

Правильный контракт:
```
LLM stream events → telemetry/status/debug → собрать полный LLMResponse
→ только потом parsing reasoning/tool_calls/XML fallback → ReAct decision
```

Ключевые правила:
- `Provider.chat()` остаётся совместимым fallback-контрактом.
- Если провайдер реализует `StreamingProvider.chat_streamed()`, основной `AgentLoop` может
  использовать его при `agent.llm_streaming_enabled: true`.
- На первом этапе streaming включён только для основного агента. Subagents, vision,
  compression, consolidation, onboarding, calibration и ToolGuard остаются на обычном `chat()`,
  пока не принято отдельное решение.
- Tool calls **нельзя исполнять на лету** по partial stream-delta. Сначала нужно собрать полный
  `LLMResponse`, затем применить обычную логику `tool_calls`/XML fallback.
- Reasoning (`reasoning_content`) сохраняется в `response.reasoning`, логируется и может
  сохраняться для audit, но **не попадает** обратно в agent context и не отправляется пользователю.
- Telegram/CLI по-прежнему отправляют финальный ответ целиком. Streaming используется для
  статусов вроде «думаю», «готовлю действие», «собираю ответ» и для диагностики зависаний.

Ключевые события trace (`logs/agent_trace.jsonl`):
- `llm_stream_started`
- `llm_stream_stage`
- `llm_stream_delta` — только при `logging.trace_level: debug_preview|full`
- `llm_stream_stalled`
- `llm_stream_fallback`
- `llm_stream_finished`

Ключевые настройки:
```yaml
agent:
  llm_streaming_enabled: true
  llm_stream_stall_seconds: 20.0
  llm_stream_max_reasoning_chars: 12000
  llm_stream_status_updates: true

logging:
  trace_level: "metadata"  # metadata | debug_preview | full
```

Реальная совместимость проверена на текущем route `provider=litellm`,
`model=llama-qwen3.6-35b-a3b`: модель отдаёт `reasoning_content` stream-delta,
`delta.content`, partial `delta.tool_calls`, `finish_reason=stop|tool_calls` и usage.

### 7.1.1 LLM payload capture — отладка и датасет для дообучения

Параллельно со streaming-телеметрией (§7.1) ядро ведёт **полный захват сырого
LLM-пейлоада** в `logs/llm_payloads.jsonl` (одна запись на LLM-вызов). По
умолчанию **выключено** (`logging.capture_enabled: false`, DC-037 closed-contour);
включается opt-in для диагностики и сбора датасета.

**Два назначения:**

1. **Отладка** — увидеть, что модель *на самом деле* получила и вернула (system
   prompt + history + tools на входе; content/reasoning/tool_calls на выходе),
   включая backend timings и usage. Это незаменимо при разборе галлюцинаций,
   проблем tool-calling, рассинхрона пресетов и stall'ов.

2. **Сбор данных для дообучения** — долгосрочная цель. Накопленные траектории
   (вопрос → полный request → response → tool_calls) формируют датасет для
   future fine-tuning локальной модели под специфику проекта.

**Безопасность:** запись фильтруется allowlist-ом `capture_fields` (только
перечисленные поля). Учётные данные скрабятся (`sk-*`, `Bearer` и т.п.).
Тем не менее — это **сырые промпты и ответы**, поэтому перед включением в
privacy-sensitive деплоях пересмотрите `capture_fields` и убедитесь, что
`logs/` не покидает периметр.

**Связанные настройки (`config/settings.yaml` → `logging`):**
```yaml
logging:
  capture_enabled: false           # opt-in: пишет logs/llm_payloads.jsonl
  capture_fields:                  # allowlist: что сохранять
    - "request.model"
    - "request.messages"
    - "request.tools"
    - "request.params"
    - "request.extra_body"
    - "response.content"
    - "response.reasoning"
    - "response.tool_calls"
    - "response.usage"
    - "response.finish_reason"
  capture_dir: "logs"
```

**Важно для приватного overlay:** `logs/llm_payloads.jsonl` содержит корпоративный
контент (переписку, данные из инструментов). Файл не должен попадать в публичный
репо (уже в `.gitignore`) и в приватный overlay тоже (он runtime-артефакт, не
исходник). При выгрузке датасета для дообучения — отдельный процесс экспорта с
явным аудитом содержимого.

### 7.2 LLM Queue, Slot Affinity и Persistent KV-cache

Проект оптимизирован под локальные LLM, где главный bottleneck — prompt processing больших
контекстов и ограниченная конкурентность GPU. Поэтому LLM-вызовы проходят через очередь,
которая ограничивает реальную параллельность и старается сохранять горячий KV-cache.

Базовая рабочая модель для llama.cpp:
```yaml
llm:
  max_concurrent_requests: 4
  queue:
    enabled: true
    strategy: "slot_affinity"
    slot_affinity:
      provider_names: ["llamacpp"]
      sticky_slot_ids: [0, 1, 2]
      overflow_slot_ids: [3]
      idle_ttl_seconds: 120.0
      cache_prompt: true
      auxiliary_policy: "overflow_only"
    persistent_cache:
      enabled: false
      save_policy: "hybrid"
      validation_min_reuse_ratio: 0.70
      strict_mismatch_retry: true
```

Текущий приоритет эксплуатации: использовать `slot_affinity` и RAM KV-cache в живых слотах.
Физический L2 KV-cache в файлах считается экспериментальной возможностью и по умолчанию
отключён, чтобы не создавать лишнюю write-нагрузку на SSD тестовой машины.

Ключевые правила:
- `LLMRequestQueue` ограничивает число одновременных inference-запросов через
  `llm.max_concurrent_requests` и пишет позицию/ожидание в trace/health.
- Ожидание в LLM-очереди не должно сжигать agent budget: `SimpleBudgetGuard` ставится на pause
  на время ожидания слота и возобновляется после его получения.
- `slot_affinity` применяется только к провайдерам из `provider_names`. Для остальных
  провайдеров очередь работает как обычный concurrency limiter без `id_slot` и `cache_prompt`.
- Sticky-слоты закрепляются за активными пользователями на `idle_ttl_seconds`; overflow-слот
  принимает нагрузку сверх sticky-ёмкости и вспомогательные вызовы при `auxiliary_policy:
  "overflow_only"`.
- Для llama.cpp в запрос добавляются `id_slot` и `cache_prompt`, чтобы backend реально
  переиспользовал slot KV-cache между последовательными запросами.
- `LLMCacheManager` умеет управлять двумя уровнями cache: L1 — живой cache в текущем слоте, L2 —
  экспериментальный файловый cache через llama-server slot save/restore/erase API.
- Persistent cache scope строится не только по пользователю, но и по `conversation_id`,
  `agent_id`, провайдеру, модели, preset, hash system prompt и hash набора tools. Это позволяет
  отдельно хранить cache основного агента и субагентов.
- После restore cache обязательно валидируется по реальным prompt/cache usage-метрикам модели.
  Если reuse ratio ниже порога, слот очищается и запрос повторяется без доверия к старому cache.
- Prune удаляет старые/лишние L2 cache-записи по `max_age_days` и `max_total_bytes`, но не
  трогает cache scopes, которые сейчас активны в слотах.
- Если `persistent_cache.root_dir` не смонтирован в ту же директорию, которую использует
  llama-server `--slot-save-path`, save/restore через API всё ещё работают, но физическая очистка
  файлов на стороне сервера может потребовать отдельного доступа к этой директории.

Практический смысл экспериментального L2 cache: для длинных диалогов локальная модель не должна
заново обрабатывать 30k-100k токенов истории после простоя пользователя. Слот можно освободить,
сохранить KV-cache в файл, а при следующем запросе пользователя/агента восстановить cache за
секунды и продолжить работу. До отдельного решения эту возможность не включать на рабочем ПК,
чтобы не расходовать ресурс SSD в период активных тестов.

### 7.3 Manual Live LLM Tests

`tests/live_llm/` содержит ручные интеграционные тесты против реального llama-server. Они нужны
не для CI, а для отладки локальной производительности и корректности queue/slot/cache логики.
Тесты L2 cache тоже считаются экспериментальными: запускать их осознанно, так как они могут
создавать cache-файлы на стороне llama-server.

Покрываемые сценарии:
- доступность OpenAI-compatible API и llama.cpp `/slots`;
- cache roundtrip на коротком и длинном prompt;
- validation mismatch, когда загруженный cache не соответствует новому prompt;
- 4 параллельных запроса по слотам `0,1,2,3`;
- интеграция `LLMRouter` + `LLMRequestQueue` + `LLMCacheManager`;
- prune/cleanup L2 cache index.

Тесты пишут JSON-отчёты в `reports/live_llm/`. Эти отчёты предназначены для ручного анализа
TTFT, prompt processing, TPS, cache reuse ratio, save/restore latency и поведения слотов.

### 8. Calibration Phase — авто-калибровка под локальную модель

Одноразовый (или периодический) этап, при котором облачная модель анализирует, как локальная модель справляется с типовыми сценариями, и автоматически правит конфигурации.

Ключевые модули (`src/corpclaw_lite/calibration/`, 1,564 строки):
- `CalibrationLoop` — оркестратор калибровочного цикла
- `ScenarioRunner` — запуск сценариев из `config/calibration_scenarios.yaml` (21 сценариев)
- `CalibrationScorer` — оценка результатов (tool accuracy, response quality)
- `ConfigEditor` — правка Edit Surfaces: system prompt, tool descriptions, few-shots, settings
- `TrajectoryRecorder` — запись траекторий для анализа

**Edit Surfaces** — калибратор правит **только** YAML/Markdown конфигурации, не Python-код:
1. System Prompt (`config/bootstrap/*.md`)
2. Tool Descriptions (YAML-override через `ToolRegistry`)
3. Skill Instructions (`skills/*.md`)
4. Few-shot Examples (генерация примеров «вопрос → tool_call»)

**CLI:** `uv run corpclaw-lite calibrate [options]`

### 9. User Onboarding — гибридный онбординг

Детерминистический движок вопросов + LLM-финализация профиля.

Ключевые модули (`src/corpclaw_lite/onboarding/`, 614 строк):
- `OnboardingEngine` — конечный автомат состояний: вопросы → ответы → финализация
- `OnboardingFinalizer` — LLM-вызов для формирования персонализированного профиля
- `OnboardingQuestions` — каталог вопросов (роль, задачи, стиль общения)
- `OnboardingStorage` — SQLite-хранилище ответов и состояния

**Запуск:** `uv run corpclaw-lite chat --setup` или `/setup` в Telegram.
**Результат:** департамент + персонализированный system prompt → сохраняется в user profile.

### 10. Context Compression — 3-уровневое сжатие

Сжатие контекста внутри одной сессии (паттерн Hermes). Критично для локальных LLM (8K-32K контекст).

Три уровня в `agent/compressor.py` (~380 строк):
1. **Prune tool results** — замена старых tool outputs (>200 chars) на placeholder
2. **Sanitize orphaned tool pairs** — очистка потерянных tool_call/result
3. **LLM summarization** — сжатие первого полу history в структурированный summary

Конфигурация через `settings.yaml` → `compression.enabled`, `threshold_ratio`, `max_context_tokens`.

### 11. Hot Reload — автоматическая перезагрузка расширений

Четыре watcher'а (polling-based, не inotify/watchdog):
- `SkillHotReloader` — polls `skills/*.md`, регистрирует/удаляет скилы при изменении
- `PluginWatcher` — polls `plugins/*/manifest.yaml`
- `SubagentHotReloader` — polls `config/subagents/*.yaml`, регистрирует/обновляет субагентов
- `MCPWatcher` — polls `config/mcp_servers.yaml`

Все watcher'ы запускаются как фоновые задачи в event loop и корректно останавливаются через `GracefulShutdown`.

---

## Структура проекта

```
corpclaw-lite/
├── src/corpclaw_lite/
│   ├── __init__.py, cli.py, exceptions.py, paths.py, templates.py
│   │
│   ├── agent/          # loop.py, context.py, guards.py, vision.py, subagent.py
│   │                    # factory.py — сборка стека агента (AgentStack)
│   │                    # compressor.py — 3-уровневое сжатие контекста
│   │                    # prompt.py — сборка промптов со скилами
│   │                    # constants.py, task_run.py — run-scoped состояние
│   │
│   ├── calibration/    # loop.py, runner.py, scorer.py, analyzer.py, editor.py
│   │                    # scenarios.py, trajectory.py — авто-калибровка под модель
│   ├── eval/             # loop.py (EvalLoop), runner.py (EvalRunner), judge.py (LLMJudge),
│   │                    #   scorer.py, scores.py, scenarios.py, corpus_fixtures.py,
│   │                    #   report.py, vision_fixtures.py — eval/auto-debug harness (B-060)
│   ├── scheduler/        # service.py, store.py, models.py, parse.py, parse_assist.py,
│   │                    #   prompt.py — agent-on-schedule (B-118/DC-030)
│   │
│   ├── onboarding/     # engine.py, finalizer.py, questions.py, storage.py
│   │                    # гибридный онбординг: детерминистический + LLM-финализация
│   │
│   ├── llm/            # base.py, anthropic.py, openai.py, xml_tool_calling.py
│   │                    # router.py, presets.py, queue.py (LLMRequestQueue + slot affinity),
│   │                    # cache.py (LLMCacheManager — L1 live / L2 experimental file cache)
│   │
│   ├── extensions/
│   │   ├── paths.py     # resolve_dirs — центральный path resolver для overlay (§4)
│   │   ├── bootstrap.py # единая инициализация всех расширений
│   │   ├── tools/       # base.py, registry.py, scoped.py, context.py (ToolExecutionContext), file_tracked.py (B-040/B-058), builtin/ (35 tools)
│   │   ├── skills/      # base.py, loader.py, registry.py, matcher.py, watcher.py
│   │   │                 #   matcher.py — TF-IDF семантический выбор скилов
│   │   │                 #   watcher.py — hot-reload .md файлов
│   │   ├── plugins/     # base.py, loader.py, registry.py, core_version.py (requires_core),
│   │   │                 #   sandbox_proxy.py, sandbox_worker.py, watcher.py
│   │   ├── subagents/   # base.py, registry.py, watcher.py, builtin/
│   │   └── mcp/         # client.py, manager.py, adapter.py, watcher.py
│   │
│   ├── channels/       # base.py, cli.py, service.py, status.py, telegram/, web/
│   │   │                 #   telegram/: channel.py, runner.py, orchestrator.py,
│   │   │                 #     formatting.py, upload.py, rate_limit.py, progress.py,
│   │   │                 #     callback_data.py, file_manager.py, admin_notifier.py, transport.py
│   │   │                 #     schedule_markup.py (B-143)
│   │   │                 #   web/: runner.py, orchestrator.py, chat_store.py, chat_context_store.py,
│   │   │                 #     files.py, pending_attachments.py, pinned_context_store.py
│   │
│   ├── security/       # tool_guard.py, network_policy.py
│   │                    #   credential_scrubber.py, ipc_auth.py
│   │
│   ├── container/      # manager.py, ipc.py, policies.py
│   │                    #   proxy.py — IPC-прокси для контейнера
│   │                    #   agent_worker.py — worker внутри контейнера
│   │
│   ├── departments/    # manager.py, permissions.py
│   ├── memory/         # sqlite.py (facts-only), file_changes.py, worker.py (B-109 memory curation)
│   ├── users/          # models.py, manager.py
│   ├── config/         # settings.py, loader.py, bootstrap.py, interpolation.py, providers.py
│   ├── runtime/        # shutdown.py — graceful shutdown (SIGINT/SIGTERM)
│   ├── utils/          # db.py, async_helpers.py
│   └── logging/        # agent_logger.py, health.py (/health endpoint), trace.py (agent_trace.jsonl)
│
├── config/
│   ├── settings.yaml                      # Основной конфиг (extensions.extra_paths для overlay)
│   ├── model_presets.yaml                  # Model presets: inference params, thinking config
│   ├── departments.yaml                    # 10 департаментов с RBAC
│   ├── tool_guard_rules.yaml               # ToolGuard правила (31 правило, CoPaw pattern)
│   ├── calibration_scenarios.yaml          # 21 сценарий калибровки
│   ├── mcp_servers.yaml                    # MCP-серверы (шаблон)
│   ├── debug_scenarios.yaml                 # Сценарии auto-debug (синтетические)
│   ├── eval_scenarios.yaml                  # Сценарии eval (B-060)
│   ├── eval/                                # Rubric для LLM judge (judge_turn.md)
│   ├── subagents/                          # YAML субагентов (data/document/execution/filesystem/research-agent)
│   └── bootstrap/                          # Модульные промпты (SOUL.md, COMPANY.md, BEHAVIOR.md)
│       ├── departments/                    # Промпты по департаментам (10 файлов)
│       └── subagents/                      # Промпты субагентов (5 файлов)
│
├── skills/             # Markdown-скилы
├── plugins/            # Папки плагинов с manifest.yaml
├── plans/              # Планы разработки (archive/ — завершённые)
├── docker/             # Dockerfile, seccomp_default.json
└── tests/
```

---

## Стиль кода

### Python версия
Python 3.12+ обязательно. Современный синтаксис типов: `list[str]` не `List[str]`, `str | None` не `Optional[str]`.

### Импорты
```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from corpclaw_lite.extensions.tools.base import Tool

if TYPE_CHECKING:
    from corpclaw_lite.agent.loop import AgentLoop
```

### Форматирование
- Длина строки: **100 символов** (ruff)
- Двойные кавычки для строк
- `ruff format` для всего форматирования

### Типы
- Strict pyright (`typeCheckingMode = "strict"`) **обязательно** — НЕ mypy
- Все функции должны иметь аннотации типов
- `Any` — только если нет другого способа

### Именование
- Классы: `PascalCase` (`AgentLoop`, `ToolGuard`, `SkillRegistry`)
- Функции/методы: `snake_case` (`get_budget`, `can_use_tool`)
- Константы: `UPPER_SNAKE_CASE` (`IMAGE_EXTENSIONS`, `MAX_HISTORY`)
- Приватные методы: с `_` (`_build_context`, `_check_permissions`)
- Протоколы: без суффикса Protocol (`Provider`, `Channel`, `Tool`)

### Async
- Весь проект async-first
- `anyio` для async файловых операций
- Async generators возвращают `AsyncIterator` напрямую

### Ошибки
```python
class ToolGuardError(Exception):
    """Raised when ToolGuard blocks a tool call."""

class PermissionDeniedError(Exception):
    """Raised when user lacks permission for the resource."""
```

---

## Правила работы с расширениями

### Tool
- Атрибуты: `name`, `description`, `params`, `execute`, `risk_level`, `parallel_safe`, `terminal`
- `parallel_safe` — `False` для инструментов с race conditions (по умолчанию `True`)
- `terminal` — `True` если инструмент возвращает результат напрямую без LLM re-paraphrase (напр. vision)
- **НЕЛЬЗЯ** добавлять: `api_version`, `deprecated_since`, `removal_in`, `replacement`, `migration_doc`, `provenance`, `compatibility`, `warning_code`
- Проверка типов файлов **на уровне кода** — IMAGE не читается ReadFileTool и наоборот

### Skill
- Атрибуты: `id`, `description`, `allowed_for`, `instructions`, `path`, `version`, `keywords`, `always`
- `keywords` — список ключевых слов/префиксов для semantic selection (напр. `["excel", "нормализ"]`)
- `always` — `True` если скилл всегда инжектируется в промпт независимо от semantic matching
- **НЕЛЬЗЯ** добавлять: `dependencies`, `resources`, `override_chain`, `pack_id`, `compatibility`, `provenance`
- Markdown-файл в `skills/` — единственный источник

### Plugin
- Структура: `manifest.yaml` + `skill.md` + опционально `tool.py` + опционально `scripts/`
- ToolGuard применяется к script execution автоматически

---

## Что делать с v1 кодом

| Модуль v1 | Действие |
|-----------|----------|
| `llm/xml_tool_calling.py` | ✅ Скопирован в новый проект — использовать без изменений |
| `agent/executor/prompt_loop_executor.py` | ✅ Адаптировано в `agent/loop.py` (~2600 строк), убраны extensibility зависимости |
| `agent/guards.py` | ✅ `SimpleBudgetGuard` + `SimpleProgressGuard` — переносить как есть |
| `container/manager.py` | ✅ Адаптировано — убрана state machine, добавлена NetworkPolicy |
| `container/ipc.py` | ✅ Адаптировано — HMAC обязателен с nonce |
| `memory/sqlite.py` | ✅ Переносить как есть |
| `channels/telegram*.py` | ✅ Адаптировано под новый Channel Protocol (telegram/ с 10 файлами) |
| `llm/anthropic.py`, `llm/openai.py` | ✅ Адаптировано под новый Provider Protocol |
| `agent/orchestration/` | ❌ НЕ переносить — весь LLM-based planning |
| `extensibility/` | ❌ НЕ переносить — весь extensibility framework |
| `plugins/manager.py` | ❌ НЕ переносить — заменить простым manifest loader |
| `governance/` | ❌ НЕ переносить — заменить structured logging |
| `approvals/service.py` | ❌ НЕ переносить — заменить ToolGuard inline кнопками |

---

## Git workflow: ветки и релизы

### Две long-lived ветки

- **`main`** — стабильная релизная ветка. Тег `vX.Y.Z` ставится на её tip. Ничего не
  коммитится в `main` напрямую — только через merge из `pre-release`.
- **`pre-release`** — интеграционная ветка. Все доработки сначала попадают сюда (через
  feature-ветки и PR), проходят ревью и верификацию, и только потом сливаются в `main`.

Feature-ветки (`feat/...`, `fix/...`, `chore/...`) создаются **от `pre-release`**, PR
открывается **в `pre-release`**, после merge ветка удаляется (локально и на remote).

### ⚠️ Регламент: выравнивание веток после релиза (ОБЯЗАТЕЛЬНО)

**Урок 0.1.11 (2026-06):** `main` и `pre-release` однажды разошлись в две независимые
dev-линии (criss-cross merge, ~40 конфликтов при попытке релиза), потому что после релиза
через merge-commit `pre-release` не подтягивался до `main`. Это стоило отдельной
консолидации (`plans/consolidate-main-pre-release.md`). Больше так не повторять.

После **каждого** релиза (`pre-release → main`, merge-commit) — **обязательно**:

1. Переключиться на `pre-release`: `git checkout pre-release && git pull origin pre-release`.
2. Подтянуть `main`: `git merge origin/main --ff-only` (это всегда fast-forward, т.к.
   `pre-release` — строгий предок `main` после релиза).
3. Запушить: `git push origin pre-release`.
4. Проверить нулевое расхождение:
   `git rev-list --count origin/main..origin/pre-release` и обратное должны быть `0`.

Это держит `main` и `pre-release` на одном коммите → следующий релиз — чистый fast-forward,
никаких конфликтов.

### Правила работы с ветками

- **`main` — только через merge из `pre-release`.** Никогда прямой коммит в `main`.
- **`pre-release` — прямой коммит допустим для тривиальных правок**: docstring, комментарии,
  документация (`AGENTS.md`, `README`, `CHANGELOG`), форматирование, правки опечаток. Для
  функциональных изменений (новые фичи, изменения поведения, архитектура, правки `src/`,
  затрагивающие рантайм) — **обязательно feature-ветка от `pre-release` + PR + ревью**.
- **Перед началом новой работы** — убедиться, что локальные `main` и `pre-release`
  актуальны (`git fetch origin --prune && git pull`), и что расхождение между ними = 0
  (см. проверку выше). Если расхождение есть — сначала выровнять, потом работать.
- **Не использовать `git rebase` для веток с merge-коммитами** (ломает историю PR).
  Релиз `pre-release → main` — всегда **"Create a merge commit"**, не "Rebase and merge".
- **Теги релизов** — аннотированные (`git tag -a vX.Y.Z`), на merge-commit релиза в `main`.
- **После merge PR** feature-ветка удаляется и локально (`git branch -d`), и на remote
  (через GitHub UI при merge, либо `git push origin --delete <branch>`).

### Быстрая проверка здоровья веток (запустить при подозрении на расхождение)

```bash
# Расходятся ли main и pre-release?
git merge-base --is-ancestor origin/main origin/pre-release && echo "main ⊆ pre-release (OK)" || echo "DIVERGENT — выровнять!"

# Насколько разошлись?
echo "pre-release ahead: $(git rev-list --count origin/main..origin/pre-release)"
echo "main ahead:        $(git rev-list --count origin/pre-release..origin/main)"
```

Если обе цифры `0` — ветки синхронны. Если любая `>0` после релиза — регламент нарушен,
выровнять перед любой новой работой.

---

## Управление планами

### Сохранение планов
- Все планы сохраняются в `plans/`
- Имена файлов: `plans/<feature-or-task-name>.md`
- `plans/` добавлен в исключения `.gitignore` корневого проекта

### Шаблон плана
```markdown
# <Название задачи>

## Summary
<Краткое описание>

## Goals
- <Цель 1>

## Steps
1. <Шаг 1>
2. <Шаг 2>

## Status
- [x] Выполненный шаг
- [ ] Ожидающий шаг

## Notes
<Контекст>
```

### Workflow
1. Подготовить план → показать пользователю → дождаться одобрения
2. Сохранить план в `plans/`
3. Приступить к выполнению, обновляя статус шагов

---

## Чеклист готовности к деплою

- [ ] `uv run corpclaw-lite telegram` запускается и отвечает
- [ ] Маркетолог говорит «нормализуй Excel» → получает файл обратно через локальную LLM
- [ ] `uv run pytest tests/ -v` — ≥75% coverage, 0 failures
- [ ] `uv run pyright src/` — 0 errors (strict mode)
- [ ] `uv run ruff check src/ && uv run ruff format src/` — 0 errors
- [ ] ToolGuard блокирует `rm -rf` через exec_script
- [ ] Добавление `skills/*.md` → доступен без перезапуска (HotReload)
- [ ] IPC между host и контейнером требует `CORPCLAW_IPC_SECRET`
