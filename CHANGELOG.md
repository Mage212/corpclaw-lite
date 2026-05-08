# Changelog

Все заметные изменения проекта CorpClaw Lite документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

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
