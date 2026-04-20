# Changelog

Все заметные изменения проекта CorpClaw Lite документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

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
