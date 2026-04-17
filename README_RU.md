# CorpClaw Lite

Корпоративный AI-агент для работы в замкнутых контурах — Telegram-бот для рутинных задач через скиллы, плагины и субагенты, работающий с **локальными LLM** (Qwen, Mistral, Llama через Ollama/vLLM/LM Studio) и управляющий доступом по департаментам.

[English version](README.md)

---

## Архитектура

```
Пользователь → Канал (Telegram / CLI) → AgentLoop (ReAct)
                                            │
                                            ▼
                                    LLM Router ──→ Ollama / vLLM / Anthropic
                                            │
                                     tool_calls? ──→ Стек безопасности
                                            │              │
                                            │    ┌─────────┴─────────┐
                                            │    ▼                   ▼
                                            │  ToolGuard          Permission
                                            │  (YAML-правила)     Check (RBAC)
                                            │    │                   │
                                            │    ▼                   ▼
                                            │  Container          Credential
                                            │  (Docker-песочница) Scrubber
                                            │
                                            ▼
                                    Ответ → Память (SQLite) → Канал
```

### Ключевые принципы

- **Простой ReAct-цикл** — никаких LLM-планеров, классический ReAct с бюджетными ограничениями и обнаружением зацикливаний
- **Локальные LLM прежде всего** — XML tool calling fallback, сжатие контекста, модельные пресеты для Qwen/Mistral/Llama
- **Безопасность по дизайну** — ToolGuard (YAML-правила), NetworkPolicy (запрет по умолчанию), IPC Auth (HMAC+nonce), Docker-песочница
- **Расширения через манифесты** — скиллы, плагины, субагенты, MCP-серверы через YAML-конфиги с горячей перезагрузкой
- **Fail-Fast** — ошибки при отсутствии критичных секретов, никаких тихих падений

---

## Быстрый старт

### Требования

- Python 3.12+
- Менеджер пакетов [uv](https://docs.astral.sh/uv/)
- Docker (опционально, для режима песочницы)
- Запущенный LLM (Ollama, vLLM или LM Studio)

### Установка

```bash
git clone https://github.com/Mage212/corpclaw-lite.git
cd corpclaw-lite
uv sync
cp .env.example .env
# Заполните .env — укажите TELEGRAM_BOT_TOKEN, CORPCLAW_IPC_SECRET, OPENAI_BASE_URL
```

### Запуск

```bash
# Интерактивный CLI-чат (режим разработки, Docker не нужен)
uv run corpclaw-lite chat

# Запуск Telegram-бота
uv run corpclaw-lite telegram

# Сборка Docker-образа песочницы (для продакшена)
cd docker && docker build -t corpclaw-agent-base:latest -f Dockerfile .
```

Для режима разработки без Docker установите в `config/settings.yaml`:
```yaml
container:
  enabled: false
```

> **Внимание:** В dev-режиме (`enabled: false`) файловые инструменты выполняются напрямую на хосте. Не используйте в продакшене.

### Архитектура Workspaces

Пользовательские данные хранятся отдельно от кода приложения:

```
workspaces/
├── user_278278319/     # Данные пользователя (Telegram ID)
│   ├── отчёт.xlsx
│   └── скрипт.py
├── user_42/
└── user_9001/

data/
├── users.db            # Пользователи и разрешения
└── memory.db           # История диалогов и факты
```

- Каждый пользователь изолирован — видит только свой workspace
- Workspaces удобно бекапить и восстанавливать (просто копия директории)
- В контейнере workspace монтируется как `/workspace` (read-write)
- База данных и конфигурация — в отдельных директориях

### Переменные окружения

| Переменная | Обязательна | Описание |
|------------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | Да | Токен бота от @BotFather |
| `CORPCLAW_IPC_SECRET` | Да | Случайная строка ≥32 символа (HMAC-ключ для IPC) |
| `OPENAI_BASE_URL` | Да | URL провайдера (напр. `http://localhost:11434/v1` для Ollama) |
| `ANTHROPIC_API_KEY` | Нет | Для Claude fallback/маршрутизации |
| `OPENAI_API_KEY` | Нет | Если провайдер требует авторизацию (напр. OpenRouter) |

---

## Возможности

| Возможность | Описание |
|-------------|----------|
| **ReAct-цикл агента** | Классический reasoning+acting с бюджетными ограничениями и обнаружением зацикливаний |
| **LLM Router** | Маршрутизация задач к конкретным провайдерам (локальный Ollama, облачный Anthropic и др.) |
| **Модельные пресеты** | Параметры инференса и конфигурация reasoning для каждой модели |
| **XML Tool Calling** | Fallback-парсер для локальных LLM без нативного function calling |
| **Сжатие контекста** | 3-уровневое сжатие для ограниченных контекстных окон |
| **Smart Approvals** | LLM-оценка риска опасных операций |
| **Docker-песочница** | Пользовательские контейнеры с лимитами ресурсов и запретом сети по умолчанию |
| **ToolGuard** | 20+ YAML-правил безопасности с уровнями CRITICAL/HIGH/MEDIUM/INFO |
| **Субагенты** | Изолированные ReAct-циклы со специализированными инструментами (экономия 60-80% контекста) |
| **Скиллы** | Markdown-инструкции с TF-IDF семантическим матчем + горячая перезагрузка |
| **Плагины** | Расширения в subprocess-песочнице через manifest.yaml |
| **MCP-интеграция** | Model Context Protocol серверы через stdio JSON-RPC |
| **Онбординг** | Гибридный детерминированный Q&A + LLM-финализация профиля |
| **Автокалибровка** | Адаптация промптов/описаний инструментов/few-shots под конкретную локальную модель |
| **RBAC** | 10 департаментов с инструментальными разрешениями и бюджетами |
| **Горячая перезагрузка** | Скиллы, плагины и MCP-серверы перезагружаются без перезапуска |

---

## Встроенные инструменты

| Инструмент | Риск | Описание |
|------------|------|----------|
| `read_file` | LOW | Чтение файлов с защитой от path traversal |
| `write_file` | MEDIUM | Запись файлов, автосоздание родительских директорий |
| `edit_file` | MEDIUM | Точный поиск/замена в файлах |
| `list_files` | LOW | Листинг директорий с метаданными |
| `search_files` | LOW | Regex-поиск (пропускает .git/node_modules) |
| `exec_script` | HIGH | Shell-команды с таймаутом (30s по умолчанию, 120s макс) |
| `web_fetch` | MEDIUM | HTTP-запросы с защитой от SSRF |
| `read_image` | LOW | Анализ изображений через отдельный LLM-вызов |
| `memory_store` | LOW | Сохранение фактов пользователя в SQLite |
| `memory_recall` | LOW | Поиск сохранённых фактов в SQLite |
| `normalize_excel` | MEDIUM | Исправление форматирования Excel (ИНН, даты, невидимые символы) |
| `send_file` | MEDIUM | Отправка файлов пользователю через канал |
| `dispatch_subagent` | LOW | Делегирование специализированному субагенту |
| `diff_text` | LOW | Сравнение текстов и файлов с выводом различий |
| `table_query` | MEDIUM | SQL-запросы к табличным данным (CSV, XLSX, JSON) через DuckDB |
| `chart_generate` | MEDIUM | Генерация графиков (bar, line, pie, scatter, histogram) |
| `convert_format` | MEDIUM | Конвертация между CSV, XLSX, JSON, Markdown |
| `pdf_reader` | LOW | Извлечение текста из PDF с поддержкой диапазонов страниц |

---

## Расширения

### Скиллы (`skills/*.md`)

Markdown-файлы с YAML-фронтматтером. Автоматически загружаются и перезагружаются.

**Двуязычный TF-IDF матчинг:** скиллы поддерживают ключевые слова на русском и английском. Стоп-слов для обоих языков встроены в matcher.

| Скилл | Департаменты | Назначение |
|-------|-------------|------------|
| `code_reviewer` | it, admin, default | Ревью кода: баги, стиль, безопасность |
| `content_writer` | marketing, hr, admin, default | Маркетинговый контент, посты, рассылки |
| `doc_writer` | it, product, admin, default | Техническая документация, README, гайды |
| `translator` | * (все) | Перевод текстов между языками |
| `excel_normalizer` | marketing, finance, hr, analytics, admin, default | Нормализация Excel: ИНН, даты, невидимые символы |
| `meeting_summary` | * (все) | Структурированные итоги встреч с задачами и решениями |
| `data_analyst` | analytics, finance, marketing, admin, development, engineering | Анализ данных, графики, SQL-запросы, конвертация форматов |

```markdown
---
id: my_skill
description: "Описание для семантического матчинга"
allowed_for: ["marketing", "engineering"]
keywords: ["отчёт", "report", "генерация"]
always: false
---

Инструкции для агента...
```

### Плагины (`plugins/<name>/`)

Сложные расширения с изоляцией в subprocess:

```
plugins/my_plugin/
├── manifest.yaml      # Обязательно
├── skill.md           # Опционально — инструкции для агента
├── tool.py            # Опционально — инструмент в subprocess-песочнице
└── scripts/           # Опционально
```

### Субагенты (`config/subagents/*.yaml`)

Специализированные агенты с изолированным контекстом и отфильтрованными инструментами:

| Субагент | Инструменты | Назначение |
|----------|-------------|------------|
| `filesystem-agent` | read_file, list_files, search_files, write_file, edit_file | Файловые операции и поиск |
| `document-agent` | read/write/edit_file, normalize_excel, list_files | Создание и редактирование документов |
| `execution-agent` | exec_script, write_file, read_file | Выполнение скриптов и команд |
| `research-agent` | web_fetch, read_file, search_files, list_files, memory_store, memory_recall | Веб-исследование и анализ |
| `data-agent` | table_query, chart_generate, convert_format, pdf_reader, diff_text, read/write_file, list_files, search_files, send_file | Анализ данных, SQL, графики, конвертация |

### MCP-серверы (`config/mcp_servers.yaml`)

```yaml
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

---

## Telegram-бот

### Команды

| Команда | Описание |
|---------|----------|
| `/start` | Регистрация и приветствие |
| `/help` | Список доступных инструментов |
| `/new` | Сбросить историю диалога |
| `/setup` | Пройти онбординг заново |
| `/chat` | Режим диалога — ответы без инструментов |
| `/execute` | Режим исполнения — полный доступ к инструментам |
| `/delete` | Интерактивный менеджер файлов |

### Режимы взаимодействия

- **Режим диалога** (`/chat`) — чистая беседа, инструменты не вызываются. Для вопросов и обсуждений.
- **Режим исполнения** (`/execute`) — полный агент с инструментами, файловыми операциями и субагентами.

### Безопасное удаление файлов

Команда `/delete` открывает **интерактивный менеджер файлов** — безопасная альтернатива удалению через агента:

- Навигация по директориям с пагинацией
- Подтверждение удаления через inline-кнопки (не через LLM)
- Защита системных файлов (`.git`, `config`, `src`, `tests` не удаляются)
- Пустые директории удаляются только при подтверждении

Это критичная функция безопасности — агент **не может удалять файлы** напрямую, только через интерактивный UI с подтверждением пользователя.

### Загрузка файлов

- Перетащите файл в чат Telegram — он сохранится в workspace пользователя
- Поддерживаемые форматы: `.txt`, `.md`, `.json`, `.yaml`, `.py`, `.csv`, `.xlsx`, `.pdf`, изображения
- Блокируемые форматы: `.exe`, `.bat`, `.cmd`, `.sh` и другие исполняемые
- Лимит размера: 20 МБ
- Изображения анализируются через vision-модель (отдельный LLM-вызов)

### Индикаторы прогресса

Во время выполнения инструментов бот показывает статусные сообщения:

- «📂 Читаю файл...» — файловые операции
- «🌐 Ищу информацию...» — web_fetch
- «💻 Запускаю команду...» — exec_script
- «📊 Обрабатываю таблицу...» — normalize_excel
- «🤖 Делегирую субагенту...» — dispatch_subagent
- И другие для каждого инструмента

### Smart Approvals

При `approval_mode="smart"` опасные операции проходят через LLM-оценку риска:

- **APPROVE** — безопасная операция, выполняется автоматически
- **DENY** — опасная операция, блокируется
- **ESCALATE** — неоднозначная операция, запрашивается подтверждение пользователя через inline-кнопки

Подтверждения отображаются как inline-кнопки «✅ Разрешить» / «❌ Отклонить» с таймаутом 5 минут.

### Rate Limiting

Защита от спама: не более 10 сообщений в минуту на пользователя (настраивается в `config/settings.yaml`).

### Уведомления администратора

Ошибки агента автоматически отправляются администраторам (настраивается через `admin_ids` в `config/settings.yaml`).

---

## Безопасность

CorpClaw Lite спроектирован для работы в **замкнутых контурах** — без обязательного доступа к интернету.

### Закрытый контур

- **Локальные LLM** — Ollama, vLLM, LM Studio как основной compute, облачные провайдеры опциональны
- **Нет внешних зависимостей** — все данные хранятся локально (SQLite, файлы)
- **Сетевая изоляция** — контейнеры запускаются с `network_mode: none`, сетевые инструменты выполняются на хосте
- **Манифестные расширения** — скиллы, плагины, субагенты загружаются из локальных файлов

### Изоляция пользователей

- Каждый пользователь получает **отдельный Docker-контейнер** с лимитами: 512 MB RAM, 0.5 CPU, 100 PIDs
- Workspace монтируется как `/workspace` (read-write), остальная ФС — read-only
- Все capabilities сброшены (`cap_drop: ALL`), seccomp-профиль с whitelist syscall
- Контейнеры автоматически удаляются после таймаута неактивности (600s)

### IPC-аутентификация

Коммуникация хост↔контейнер защищена:
- **HMAC-SHA256** подпись каждого запроса и ответа
- **Nonce** с TTL 300s для защиты от replay-атак
- **Constant-time comparison** (`hmac.compare_digest`) для защиты от timing-атак
- **Fail-fast** без `CORPCLAW_IPC_SECRET` (минимум 16 символов)

### Credential Scrubber

Автоматическое маскирование секретов в логах и результатах:
- OpenAI/Anthropic API-ключи (`sk-...`, `sk-ant-...`)
- GitHub PAT (`ghp_...`)
- Bearer-токены, AWS-ключи (`AKIA...`)
- PEM private keys, URL-credentials (`user:pass@host`)
- IPC-секрет из переменной окружения

---

## Конфигурация

| Файл | Назначение |
|------|------------|
| `config/settings.yaml` | Основной конфиг: LLM-провайдеры, агент, контейнеры, скиллы, telegram |
| `config/model_presets.yaml` | Параметры инференса и конфигурация reasoning для каждой модели |
| `config/departments.yaml` | RBAC: инструментальные разрешения и бюджеты по департаментам |
| `config/tool_guard_rules.yaml` | 20+ правил безопасности для ToolGuard |
| `config/network_policy.yaml` | Сетевой allowlist для контейнеров |
| `config/calibration_scenarios.yaml` | 20+ тестовых сценариев для калибровки |
| `config/bootstrap/*.md` | Идентичность агента: SOUL.md, COMPANY.md, BEHAVIOR.md |
| `config/bootstrap/departments/*.md` | Системные промпты по департаментам |
| `config/bootstrap/subagents/*.md` | Системные промпты по субагентам |

### LLM Router

Маршрутизация разных задач к конкретным провайдерам:

```yaml
llm:
  default: "default"
  named:
    default:
      type: "openai"
      model: "qwen3.5-4b"
      base_url: "http://localhost:11434/v1"
      preset: "qwen3.5-thinking"
    cloud:
      type: "anthropic"
      model: "claude-sonnet-4-20250514"
      api_key: "${ANTHROPIC_API_KEY}"
  routing:
    - task_kind: "vision"
      provider: "default"
    - subagent_id: "code_review"
      provider: "cloud"
```

### Модельные пресеты

Разные модели требуют разные параметры инференса и стратегии reasoning:

```yaml
presets:
  qwen3.5-thinking:
    thinking:
      source: "native"          # Использует поле reasoning_content из API
    thinking_budget_tokens: 1024
    inference_params:
      temperature: 0.7
      top_p: 0.95
      top_k: 20
```

**Приоритет:** `уровень запроса > пресет > дефолты провайдера`

---

## Калибровка

Автоматическая адаптация конфигурации под конкретную локальную модель. Облачная модель анализирует ошибки на типичных сценариях и итеративно улучшает промпты, описания инструментов и few-shot примеры.

```bash
# Проверить baseline-оценку без облака
uv run corpclaw-lite calibrate --dry-run

# Полная калибровка (требуется облачный провайдер в settings.yaml)
uv run corpclaw-lite calibrate --cloud-provider cloud --max-iterations 5
```

Калибровка редактирует только YAML/Markdown конфиги, никогда не трогает Python-код.

---

## CLI-команды

```bash
# Чат
uv run corpclaw-lite chat                                       # Интерактивный CLI
uv run corpclaw-lite chat --setup                               # Онбординг пользователя

# Telegram
uv run corpclaw-lite telegram                                   # Запуск бота

# Управление пользователями
uv run corpclaw-lite user-list
uv run corpclaw-lite user-create -t <tg_id> -d <department>
uv run corpclaw-lite user-allow -t <tg_id> -d <department>
uv run corpclaw-lite user-deny -t <tg_id>
uv run corpclaw-lite user-revoke -t <tg_id>

# Расширения
uv run corpclaw-lite skill list
uv run corpclaw-lite plugin list
uv run corpclaw-lite generate skill <name>                      # Скаффолд скилла
uv run corpclaw-lite generate plugin <name>                     # Скаффолд плагина
uv run corpclaw-lite generate subagent <name>                   # Скаффолд субагента

# Docker
uv run corpclaw-lite containers                                 # Активные контейнеры
uv run corpclaw-lite prune                                      # Удаление idle-контейнеров

# Калибровка
uv run corpclaw-lite calibrate --dry-run                        # Baseline-оценка
uv run corpclaw-lite calibrate                                  # Полная калибровка
```

---

## Разработка

### Установка

```bash
uv sync
```

### Проверки (запускать перед коммитом)

```bash
# Полная проверка
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v

# Отдельно
uv run ruff check src/ --fix          # Линт
uv run ruff format src/               # Форматирование
uv run pyright src/                   # Типы (strict mode)
uv run pytest tests/ -v               # Тесты
uv run pytest tests/ --cov=src/corpclaw_lite --cov-report=term-missing  # Покрытие
```

### Статистика проекта

| Компонент | LOC | Файлов |
|-----------|-----|--------|
| Agent Core | 1 901 | 10 |
| Extensions | 2 558 | 43 |
| Channels | 5 037 | 14 |
| Calibration | 1 522 | 8 |
| LLM Providers | 1 195 | 7 |
| Container | 806 | 6 |
| Security | 506 | 5 |
| Memory | 477 | 3 |
| Onboarding | 614 | 5 |
| Прочее | ~2 004 | ~25 |
| **Исходный код** | **~16 620** | **~126** |
| **Тесты** | **~14 685** | **~85** (806 тестов) |

---

## Лицензия

Лицензировано под Apache License, Version 2.0. См. [LICENSE](LICENSE).
