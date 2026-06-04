# Changelog

Все заметные изменения проекта CorpClaw Lite документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [0.1.6] — 2026-06-04

Фокус версии — трансляция внутренней работы субагентов в пользовательские статусы Web/Telegram.

### Added

- Добавлены subagent-aware status callbacks для tool calls, parallel tool batches и LLM stages
  внутреннего `AgentLoop` субагента.
- Telegram и Web теперь показывают вложенные статусы с человекочитаемым именем субагента,
  например `Research Agent: 🤔 Думаю...` или `Document Agent: 📂 Читаю файл...`.

### Changed

- `dispatch_subagent` больше не оставляет пользовательский статус зависшим на «Делегирую
  субагенту...»: дальнейшие tool/LLM стадии субагента прокидываются через существующий
  callback pipeline без раскрытия task, arguments, results, prompt или reasoning content.
- Runtime context `ToolRegistry.execute()` расширен служебными callback-параметрами для
  `DispatchSubagentTool`; tool schema и LLM-visible arguments не изменились.

### Verified

- Focused проверки после форматирования: `52 passed` по subagent/status pipeline, progress и Web.
- Полная проверка: ruff clean, `uv run pyright src/` — `0 errors` (17 существующих
  matplotlib stub warnings), `uv run pytest tests/ -v` — `1038 passed, 1 skipped`.

## [0.1.5] — 2026-06-04

Фокус версии — улучшение пользовательской видимости текущих действий агента в Web/Telegram
и закрепление процесса версионирования после функциональных доработок.

### Added

- Добавлен агрегированный статус для параллельных tool calls: AgentLoop теперь поддерживает
  `on_tool_batch_start`, а Web/Telegram показывают один понятный статус вроде «Работаю с
  файлами...» или «Выполняю 2 действия...» вместо гонки нескольких отдельных tool-статусов.
- Введено проектное правило: после существенных функциональных изменений обновлять версию и
  `CHANGELOG.md`; перед будущим выбором номера версии запрашивать подтверждение пользователя,
  чтобы при крупных изменениях можно было поднять minor-версию.

### Changed

- Web-канал больше не превращает технический LLM stage `finished` в пользовательский статус
  «В обработке...». Неизвестные/unmapped LLM stages игнорируются, а финальный переход остаётся
  только через `request_finished`.
- Parallel-safe инструменты больше не отправляют индивидуальные `on_tool_start` статусы внутри
  одной параллельной пачки; одиночные и последовательные tool calls сохраняют прежнее поведение.

### Verified

- Полная проверка: `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v`.
- Результат: ruff clean, pyright `0 errors` (17 существующих matplotlib stub warnings),
  pytest `1033 passed, 1 skipped`.

## [0.1.4-beta] — 2026-06-04

Текущий beta-релиз. Основной фокус — production-ready Web-канал, премиальный русскоязычный
интерфейс, управление конкурентностью локальных LLM, slot affinity для llama.cpp,
экспериментальный persistent KV-cache в файлах и ручные live-тесты на реальном llama-server.

### Added

#### Browser web channel
- Добавлен веб-канал `uv run corpclaw-lite web` на `aiohttp`: локальный login, HttpOnly session
  cookie, WebSocket-чат, статусы выполнения, approvals и минимальный server-rendered UI.
- Веб-интерфейс вынесен в React/Vite приложение: добавлены рабочий chat shell, единый statusline
  вместо потока статусных сообщений, collapsible file explorer, preview drawer, drag-and-drop
  upload/move и batch file operations.
- Web UI получил второй слой production-polish: resizeable файловая панель и preview, сохранение
  ширин в браузере, полноценный проводник с деревом папок, режимами list/grid/details,
  нормальными диалогами операций и expanded preview вместо узких фиксированных областей.
- Web UI расширен fullscreen-режимом файлового менеджера, кнопкой/командой `/new` для сброса
  контекста и индикатором заполненности контекста по backend-reported token usage.
- Контекстное меню файлового менеджера теперь автоматически остаётся внутри видимой области окна,
  включая fullscreen-режим и клики по файлам у правого/нижнего края.
- Web download приведён к явному режиму загрузки: файлы отдаются с исходным именем, image preview
  использует отдельный inline endpoint, а ссылки на файлы от агента получили TTL и user-boundary.
- Web layout получил защиту от схлопывания центральной панели: файловый preview теперь открывается
  явно как side preview или fullscreen preview, resize учитывает минимальную ширину чата, а topbar
  переносит элементы в compact-режиме.
- Web file explorer получил компактный режим по ширине самой панели: на узкой панели дерево папок
  открывается отдельным drawer, а таблица файлов превращается в читаемый список без обрезанных
  колонок.
- Web-чат получил безопасный Markdown/GFM-рендер ответов модели: списки, таблицы, ссылки,
  inline-code и code blocks с кнопкой копирования теперь отображаются как полноценный ответ, а не
  как сырой текст.
- Web-чат получил persistent transcript в SQLite: история текущей сессии переживает refresh,
  logout/login и reconnect, `/new` открывает новую пустую сессию, а долгие запросы больше не
  привязаны к одному старому WebSocket-соединению.
- Web UI получил премиальный русскоязычный рабочий интерфейс: операционный центр с обзором
  выполнения, последние файлы и результаты, сворачиваемые панели, отдельное меню пользователя,
  явная новая сессия и collapsible operation center.
- Web frontend получил runtime-проверку REST/WebSocket JSON-контрактов без новых зависимостей,
  а TypeScript-проверка усилена `noUncheckedIndexedAccess` и `exactOptionalPropertyTypes`.
- Web-канал теперь распознаёт `502 upstream_error / Connection refused` от OpenAI-compatible
  LLM gateway как ожидаемую недоступность backend-модели и отдаёт пользователю warning без
  traceback в обычном сценарии.
- Web file API расширен операциями tree/search/preview/rename/move/copy/batch delete; все операции
  сохраняют host-side boundary checks личного workspace.
- Web shutdown теперь явно останавливает контейнеры пользователей, которые поднимались или
  переиспользовались текущим web-процессом.
- Добавлены локальные веб-аккаунты в `UserManager`: `web-user-create`, `web-user-password`,
  PBKDF2-хэширование пароля и SQLite-сессии с CSRF token.
- Добавлена привязка web-логина к существующему Telegram-профилю: `web-user-link`,
  `web-user-create --telegram-id` и безопасное слияние дублей через `web-user-merge`, чтобы
  веб-канал использовал ту же память и workspace.
- Внутренний ключ пользователя унифицирован на `users.id`: контейнеры, workspace, memory,
  onboarding и user bootstrap больше не используют `telegram_id` как технический идентификатор.
  Добавлена миграция `user-migrate-canonical-ids` для старых данных.
- Добавлен личный файловый веб-диспетчер: list/upload/download/delete/mkdir с проверкой границ
  workspace, лимитом размера и переиспользованием правил безопасных расширений.
- Вынесен `AgentRequestService` для channel-neutral запуска agent workflow: сборка prompt,
  skill matching, container preflight, approval callback и structured activity logging.

#### Host-side web search
- Добавлен инструмент `web_search` через `ddgs` с явным backend DuckDuckGo, лимитами
  конкурентности и безопасным контрактом `query -> URL/snippet`.
- `web_fetch` усилен `format=raw|text`, User-Agent и process-level backpressure; `format=text`
  очищает HTML в компактный текстовый вид для локальных LLM.
- `research-agent` теперь использует цепочку `web_search -> web_fetch(format="text")` для
  веб-исследований, а доступ к поиску выдан тем же департаментам, где уже был разрешён web fetch.

#### PDF extraction cleanup
- `pdf_reader` теперь очищает PDF extraction от непечатаемых control-символов, которые могут
  появляться в формулах после `pypdf.extract_text()` и ломать LLM/tool context.
- Добавлен параметр `output_path` для сохранения очищенного PDF-текста в `.md`, `.markdown` или
  `.txt` без промежуточного копирования сырого вывода через `write_file`.
- `document-agent` теперь инструктируется использовать `pdf_reader output_path` для PDF→Markdown
  задач.

#### Excel formula-aware reads
- `excel_workbook action=read` теперь по умолчанию показывает формульные ячейки как
  `formula + cached_value`, чтобы агент видел и саму формулу, и фактическое сохранённое значение
  из workbook.
- Добавлен `formula_mode`: `both` (по умолчанию), `values` для старого value-only поведения и
  `formulas` для чтения только формул.
- `excel_workbook` теперь поддерживает comma-separated mix одиночных ячеек и диапазонов в `cells`,
  например `A1,B2:D4,F8:G9`.
- Промпты Excel-заполнения и субагентов уточнены: для шаблонов с датами/периодами/формулами нужно
  читать диапазоны в `formula_mode=both` и не перезаписывать формульные ячейки без явной просьбы.

#### LLM Queue и backpressure
- Добавлена очередь LLM-запросов `LLMRequestQueue`, ограничивающая реальную inference-
  конкурентность через `llm.max_concurrent_requests`.
- Базовая конкурентность для локальной машины установлена в `4` одновременных запроса.
- Очередь отслеживает позицию запроса, время ожидания, время выполнения и отдаёт эти данные в
  trace/health.
- `SimpleBudgetGuard` теперь может ставиться на pause на время ожидания LLM-слота, чтобы агентный
  budget расходовался на работу модели и инструментов, а не на ожидание очереди.
- `LLMRouter` получил единый путь выполнения через queue/cache для обычных и default-вызовов.

#### llama.cpp Slot Affinity
- Добавлена стратегия очереди `slot_affinity` для llama.cpp-compatible backend.
- Конфигурация по умолчанию: sticky-слоты `0,1,2` для активных пользователей и overflow-слот `3`
  для нагрузки сверх sticky-ёмкости.
- Sticky-слот удерживается за пользователем на `idle_ttl_seconds` после ответа, чтобы сохранить
  горячий KV-cache между последовательными запросами.
- Для llama.cpp-вызовов автоматически добавляются `id_slot` и `cache_prompt`.
- Добавлена политика `auxiliary_policy: "overflow_only"` для вспомогательных LLM-вызовов, чтобы
  они не разрушали sticky-cache основных пользовательских сессий.

#### Persistent Slot KV-cache
- Добавлен `LLMCacheManager` в `src/corpclaw_lite/llm/cache.py`.
- Реализован L1/L2 cache-подход: L1 — живой KV-cache в слоте llama.cpp, L2 — сохранённый
  файловый cache через llama-server slot save/restore/erase API.
- L2 файловый cache помечен как экспериментальная возможность и отключён по умолчанию на
  тестовой машине, чтобы не создавать лишнюю write-нагрузку на SSD.
- Добавлен SQLite index для L2 cache с метаданными scope, размера, возраста, restore count и
  последнего использования.
- Cache scope учитывает `user_id`, `conversation_id`, `agent_id`, провайдера, модель, preset,
  hash system prompt и hash набора tools.
- Это позволяет хранить отдельные cache-файлы для основного агента и субагентов одного
  пользователя.
- Добавлены save policies: `hybrid`, `every_response`, `eviction_only`.
- Добавлены параметры автоочистки: `max_total_bytes`, `max_age_days`,
  `prune_interval_seconds`.
- Добавлена валидация восстановленного cache по фактическим usage-метрикам модели:
  `cached_input_tokens`, prompt tokens и reuse ratio.
- При низком reuse ratio включается безопасный fallback: слот очищается, cache scope сбрасывается,
  запрос повторяется без доверия к старому cache.

#### Token usage и observability
- `TokenUsage` расширен метрикой `cached_input_tokens`, чтобы видеть реальное переиспользование
  prompt cache.
- Добавлены trace/health события для queue, slot affinity и persistent cache: вход/выход из
  очереди, получение/освобождение слота, reuse sticky slot, overflow slot, L1/L2 cache hit,
  restore/save, mismatch validation, prune.
- Логика логирования теперь даёт достаточно данных для отладки долгого TTFT, неправильного
  cache restore, очередей и поведения слотов.

#### Manual Live LLM Tests
- Добавлен каталог `tests/live_llm/` с ручными интеграционными тестами против реального
  llama-server.
- Live-тесты не входят в обычный pytest-пул и запускаются только при
  `CORPCLAW_LIVE_LLM_TESTS=1`.
- Медленные сценарии дополнительно требуют `CORPCLAW_LIVE_LLM_RUN_SLOW=1`.
- Покрыты сценарии: доступность API и `/slots`, cache save/restore roundtrip, mismatch
  validation, 4 параллельных запроса по слотам, интеграция router/queue/cache, prune/cleanup.
- Тесты пишут JSON-отчёты в `reports/live_llm/` для ручного анализа TTFT, TPS, prompt processing,
  cache reuse ratio и save/restore latency.

### Changed

- `config/settings.yaml` теперь включает production-oriented настройки очереди, slot affinity и
  экспериментального persistent cache для llama.cpp.
- Текущий эксплуатационный приоритет изменён на `slot_affinity` + RAM KV-cache в живых слотах:
  3 sticky-слота для активных пользователей и 1 общий overflow-слот.
- `pyproject.toml` исключает `tests/live_llm/` из обычного тестового пула.
- LLM streaming продолжает использоваться "под капотом", но теперь работает поверх queue/cache
  слоя, а не в обход контроля конкурентности.
- Слоты рассматриваются как ценный локальный ресурс: проект старается сохранять их состояние,
  а не просто равномерно размазывать запросы по backend.

### Verified

- Полная релизная проверка на `pre-release`:
  - `uv run ruff check src/ tests/`
  - `uv run pyright src/`
  - `uv run pytest tests/ -q`
  - `cd frontend/web && npm run build`
  - `cd frontend/web && npm run test`
- Результат полной проверки: `1017 passed, 1 skipped`; pyright — `0 errors`,
  `17 warnings` по неполным matplotlib-стабам в `chart_generate.py`.
- Ручные live-тесты на `llama-server` с моделью `gpt-oss-20b-UD-Q4_K_XL`:
  - обычный live-прогон: `7 passed, 1 skipped`;
  - slow large cache roundtrip: `1 passed`;
  - запуск без `CORPCLAW_LIVE_LLM_TESTS`: `8 skipped`.
- Практические метрики на реальном backend:
  - 1k prompt cold: TTFT около `0.67s`, prompt processing около `614ms`;
  - 1k prompt warm from cache: TTFT около `0.085s`, prompt processing около `14ms`;
  - 5k prompt cold: TTFT около `2.94s`, prompt processing около `2865ms`;
  - 5k prompt warm from cache: TTFT около `0.084s`, prompt processing около `15ms`;
  - 4 parallel slots: общий wall time около `3.19s`, TTFT по слотам около `2.64-2.68s`.

### Notes

- Persistent cache даёт главный выигрыш именно на длинных локальных контекстах: вместо повторного
  prompt processing на десятках тысяч токенов можно восстановить KV-cache из файла и продолжить
  диалог.
- Пока проект тестируется на рабочем ПК с одним SSD, L2 cache следует держать выключенным и
  включать только для целевых ручных экспериментов.
- Если директория `persistent_cache.root_dir` не является той же директорией, которую использует
  llama-server `--slot-save-path`, API save/restore работает, но физическая очистка server-side
  cache-файлов требует отдельного доступа к этой директории.

## [0.1.2] — 2026-05-08

Текущая рабочая версия. Основной фокус — backend streaming для LLM, детальная
телеметрия выполнения и проверка совместимости с реальной моделью из конфигурации.

### Added

#### Backend LLM Streaming
- Добавлен внутренний streaming-контракт: `LLMStreamEvent`, расширенный `StreamChunk`,
  optional `StreamingProvider.chat_streamed()`.
- `OpenAIProvider` получил `chat_streamed()`: потоково читает chunks, собирает полный
  `LLMResponse`, сохраняет `reasoning_content`, собирает partial tool-call deltas и после
  завершения применяет обычный post-processing.
- `AgentLoop` использует backend streaming при `agent.llm_streaming_enabled: true`, если
  провайдер поддерживает `StreamingProvider`.
- Добавлен fallback: если streaming падает, запрос повторяется через обычный `chat()`.
- Tool calls по-прежнему исполняются только после полной сборки `LLMResponse`, а не по partial
  stream-delta.

#### Observability
- Добавлены trace-события `llm_stream_started`, `llm_stream_stage`, `llm_stream_delta`,
  `llm_stream_stalled`, `llm_stream_fallback`, `llm_stream_finished`.
- `llm_stream_delta` пишется только при `logging.trace_level: debug_preview|full`, чтобы
  metadata-режим не раздувал логи содержимым ответа.
- `logging.trace_level: full` теперь сохраняет полный scrubbed-текст, а не обрезает его до
  preview.
- В `agent_activity.jsonl` добавлена краткая stream-сводка: calls, fallbacks, stalls, events,
  first_event_ms, first_content_ms, first_tool_call_ms.
- В `/health` добавлены counters: `llm_stream_calls`, `llm_stream_fallbacks`,
  `llm_stream_stalls`, `llm_reasoning_chars`, `llm_content_chars`.

#### Telegram/CLI Statuses
- Telegram progress получил coarse LLM-stage статусы: reasoning, preparing tool call,
  assembling answer.
- CLI и Telegram activity logs теперь сохраняют stream summary для каждого запроса.

### Changed

- `OpenAIProvider.stream()` теперь применяет тот же preset/bootstrap kwargs path, что и `chat()`.
- Основной `llm_call_started`/`llm_call_finished` trace расширен безопасными hash/char-метриками
  для content и reasoning.
- В `config/settings.yaml` добавлены настройки:
  - `agent.llm_streaming_enabled`
  - `agent.llm_stream_stall_seconds`
  - `agent.llm_stream_max_reasoning_chars`
  - `agent.llm_stream_status_updates`

### Verified

- Реальный интеграционный запрос к текущей модели `provider=litellm`,
  `model=llama-qwen3.6-35b-a3b`, без установки `max_tokens`.
- Подтверждено, что модель отдаёт:
  - `reasoning_content` stream-delta;
  - `delta.content`;
  - partial `delta.tool_calls`;
  - `finish_reason=stop` для текста;
  - `finish_reason=tool_calls` для вызова инструмента;
  - usage tokens.
- Точечные проверки после изменений:
  - `uv run pytest tests/test_agent_loop.py tests/test_llm_advanced.py tests/test_logging_and_security.py tests/test_health.py -q`
  - `uv run ruff check ...`
  - `uv run pyright ...`

## [0.1.0] — 2026-04-17

Первый публичный релиз.

### Added

#### Ядро агента
- ReAct-цикл агента (AgentLoop) с бюджетными ограничениями (SimpleBudgetGuard) и обнаружением зацикливаний (SimpleProgressGuard)
- Параллельное выполнение инструментов (parallel_safe=True)
- Terminal-инструменты с прямым возвратом результата (без LLM-парафраза)
- ContextBuilder — 4-фазная сборка контекста с совместимостью для Qwen3.5
- 3-уровневое сжатие контекста (prune → sanitize → LLM summarize)
- Фабрика агентов `build_agent_stack()` — единая точка сборки всего стека

#### LLM-провайдеры
- OpenAI-совместимый провайдер (Ollama, vLLM, LM Studio, OpenRouter, Groq)
- Anthropic-провайдер с нативным tool calling
- XML Tool Calling — fallback-парсер для локальных LLM
- LLM Router — YAML-маршрутизация по task_kind и subagent_id
- Модельные пресеты — параметры инференса и ThinkingConfig для каждой модели
- Поддержка reasoning_content (Qwen3, Claude) и XML-тегов thinking

#### Инструменты (18 встроенных)
- read_file, write_file, edit_file, list_files, search_files — файловые операции
- exec_script — выполнение shell-команд с таймаутом
- web_fetch — HTTP-запросы с защитой от SSRF
- read_image — анализ изображений через отдельный LLM-вызов (terminal)
- memory_store, memory_recall — персистентные факты в SQLite
- normalize_excel — исправление форматирования Excel (ИНН, даты, невидимые символы)
- send_file — отправка файлов пользователю
- dispatch_subagent — делегирование субагентам (terminal)
- diff_text — сравнение текстов и файлов с выводом различий
- table_query — SQL-запросы к табличным данным (CSV, XLSX, JSON) через DuckDB
- chart_generate — генерация графиков (bar, line, pie, scatter, histogram)
- convert_format — конвертация между CSV, XLSX, JSON, Markdown
- pdf_reader — извлечение текста из PDF с поддержкой диапазонов страниц

#### Расширения
- Скиллы (4) — Markdown-инструкции с TF-IDF семантическим матчем (двуязычный RU+EN): translator, excel_normalizer, meeting_summary, data_analyst. Каждый скилл имеет `scope` для привязки к конкретному агенту.
- Плагины — subprocess-песочница с JSON-RPC через stdin/stdout
- Субагенты (5): filesystem-agent, document-agent, execution-agent, research-agent, data-agent
- MCP-интеграция — Model Context Protocol через stdio JSON-RPC
- Горячая перезагрузка скиллов (5s), плагинов (10s), MCP-серверов (10s)

#### Безопасность
- ToolGuard — 20+ YAML-правил безопасности (CRITICAL/HIGH/MEDIUM/INFO)
- Smart Approvals — LLM-оценка риска (APPROVE / DENY / ESCALATE)
- Docker-песочница — пользовательские контейнеры с лимитами ресурсов
- Network Policy — запрет сети по умолчанию с allowlist
- IPC Auth — HMAC-SHA256 + nonce с защитой от replay (300s TTL)
- Credential Scrubber — маскирование API-ключей и токенов в логах
- RBAC — 10 департаментов с инструментальными разрешениями и бюджетами

#### Каналы
- Telegram-бот с 7 командами: /start, /help, /new, /setup, /chat, /execute, /delete
- Интерактивный менеджер файлов (/delete) — безопасное удаление через inline-кнопки, без участия LLM
- Режимы взаимодействия: диалог (/chat, без инструментов) и исполнение (/execute, полный доступ)
- Индикаторы прогресса — статусные сообщения для каждого инструмента во время выполнения
- Inline-подтверждения (Smart Approvals) — кнопки «Разрешить»/«Отклонить» для опасных операций
- Rate limiting — 10 сообщений/мин на пользователя (настраиваемый)
- Загрузка файлов с валидацией (whitelist расширений, лимит 20 МБ, санитизация имён)
- Автоматическое разбиение длинных сообщений с сохранением Markdown-форматирования
- Уведомления администратора об ошибках агента
- CLI-чат для разработки

#### Память
- SQLiteMemory — асинхронная WAL, автоматическая миграция схемы
- MemoryConsolidator — LLM-сжатие с cooldown-ограничениями
- ContextCompressor — 3-уровневое сжатие для ограниченных контекстных окон

#### Калибровка
- Автоматическая калибровка промптов и few-shots под конкретную модель
- 20+ тестовых сценариев (tool_use, no_tool, multi_step, error_recovery)
- Итеративное улучшение через облачную модель

#### Онбординг
- Гибридный детерминированный Q&A + LLM-финализация профиля
- Автогенерация пользовательского профиля

#### Инфраструктура
- CI через GitHub Actions (lint → format → typecheck → test)
- 806 тестов, ~75% покрытие кода
- pyright strict mode без ошибок
- ruff линтинг и форматирование

### Security

- ToolGuard с 20+ YAML-правилами
- Docker-песочница с per-user изоляцией
- HMAC+nonce IPC-аутентификация
- Credential Scrubber для маскирования секретов
- Network Policy deny-by-default

[0.1.0]: https://github.com/Mage212/corpclaw-lite/releases/tag/v0.1.0
