# CorpClaw Lite

Корпоративный AI-агент для работы в замкнутых контурах — Telegram-бот для рутинных задач через скиллы, плагины и субагенты, работающий с **локальными LLM** (Qwen, Mistral, Llama через Ollama/vLLM/LM Studio) и управляющий доступом по департаментам.

[English version](README.md)

---

## Архитектура

```
Пользователь → Канал (Web / Telegram / CLI) → AgentLoop (ReAct)
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
- Node.js 20+ и npm (для сборки браузерного интерфейса)
- Docker (опционально, для режима песочницы)
- Запущенный LLM (Ollama, vLLM или LM Studio)

### Установка

```bash
git clone https://github.com/Mage212/corpclaw-lite.git
cd corpclaw-lite
uv sync
cp .env.example .env
# Заполните .env — укажите TELEGRAM_BOT_TOKEN, CORPCLAW_IPC_SECRET
# и хотя бы один LLM-провайдер через PROVIDER_<NAME>__*
```

### Запуск

Если пользователя ещё нет в базе, сначала создайте его:

```bash
uv run corpclaw-lite user-create -t <telegram_id> -d engineering
```

CLI-чат запускается от имени существующего пользователя:

```bash
uv run corpclaw-lite chat --telegram-id <telegram_id>
```

Telegram-канал:

```bash
uv run corpclaw-lite telegram
```

Web-канал в обычном локальном режиме:

```bash
uv run corpclaw-lite web-user-link -t <telegram_id> -u <username> -p '<password>'

cd frontend/web
npm ci
npm run build
cd ../..

uv run corpclaw-lite web
```

Откройте `http://127.0.0.1:8090`.

`web-user-link` — основной сценарий для уже существующего Telegram-пользователя. Команда
добавляет логин/пароль к тому же внутреннему `users.id`, поэтому Web, Telegram, память,
workspace и контейнер продолжают работать с одним человеческим профилем. `web-user-create`
используйте только для отдельного web-only аккаунта.

Если включена контейнеризация, Docker должен быть запущен. Образ песочницы можно собрать так:

```bash
docker build -f docker/Dockerfile -t corpclaw-agent-base:latest .
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
├── user_1/             # Данные пользователя (users.id)
│   ├── отчёт.xlsx
│   └── скрипт.py
├── user_900000042/     # Тестовый пользователь
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
| `TELEGRAM_BOT_TOKEN` | Для Telegram | Токен бота от @BotFather |
| `CORPCLAW_IPC_SECRET` | Да | Случайная строка ≥32 символа, HMAC-ключ для IPC host↔container |
| `PROVIDER_<NAME>__TYPE` | Да | Тип провайдера: `openai` или `anthropic` |
| `PROVIDER_<NAME>__BASE_URL` | Да для OpenAI-compatible | URL провайдера, например `http://localhost:8080/v1` для llama.cpp |
| `PROVIDER_<NAME>__API_KEY` | Если нужен провайдеру | Ключ доступа или техническое значение вроде `llamacpp`/`ollama` |

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
| **ToolGuard** | 31 YAML-правило безопасности с уровнями CRITICAL/HIGH/MEDIUM/INFO |
| **Субагенты** | Изолированные ReAct-циклы со специализированными инструментами (экономия 60-80% контекста) |
| **Скиллы** | Markdown-инструкции с TF-IDF семантическим матчем + горячая перезагрузка |
| **Плагины** | Доверенные локальные расширения через manifest.yaml и subprocess-изоляцию |
| **MCP-интеграция** | Model Context Protocol серверы через stdio JSON-RPC |
| **Web-канал** | Браузерный чат, единая statusline выполнения, личный файловый диспетчер и общая идентичность с Telegram |
| **Онбординг** | Гибридный детерминированный Q&A + LLM-финализация профиля |
| **Автокалибровка** | Адаптация промптов/описаний инструментов/few-shots под конкретную локальную модель |
| **RBAC** | 10 департаментов с инструментальными разрешениями и бюджетами |
| **Приватный overlay расширений** | Корпоративные доработки в отдельном приватном репо, компонуются в рантайме — ни одного приватного файла в публичном репо ([документация](../corpclaw-lite/CONTRIBUTING.md#private-extensions-overlay)) |
| **Горячая перезагрузка** | Скиллы, плагины, субагенты и MCP-серверы перезагружаются без перезапуска |

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
| `web_search` | MEDIUM | Поиск источников через DuckDuckGo-compatible backend |
| `read_image` | LOW | Анализ изображений через отдельный LLM-вызов |
| `memory_store` | LOW | Сохранение фактов пользователя в SQLite |
| `memory_recall` | LOW | Поиск сохранённых фактов в SQLite |
| `normalize_excel` | MEDIUM | Исправление форматирования Excel (ИНН, даты, невидимые символы) |
| `excel_inspect` | LOW | Быстрая инспекция Excel-структуры, листов, диапазонов и образцов данных |
| `excel_workbook` | MEDIUM | Чтение и заполнение Excel-книг с учётом формул, диапазонов и пагинации |
| `send_file` | MEDIUM | Отправка файлов пользователю через канал |
| `dispatch_subagent` | LOW | Делегирование специализированному субагенту |
| `diff_text` | LOW | Сравнение текстов и файлов с выводом различий |
| `table_query` | MEDIUM | SQL-запросы к табличным данным (CSV, XLSX, JSON) через DuckDB |
| `chart_generate` | MEDIUM | Генерация графиков (bar, line, pie, scatter, histogram) |
| `convert_format` | MEDIUM | Конвертация между CSV, XLSX, JSON, Markdown |
| `pdf_reader` | LOW | Извлечение текста из PDF с поддержкой диапазонов страниц |
| `research_search` | MEDIUM | Управляемый поиск источников для исследовательского workflow |
| `research_fetch_source` | MEDIUM | Загрузка и кеширование источника для исследования |
| `research_read_source` | LOW | Чтение сохранённого источника с поиском и лимитами вывода |
| `research_store_fact` | LOW | Сохранение проверенного факта исследования с метаданными |
| `research_list_facts` | LOW | Просмотр накопленных фактов исследования |
| `research_finalize` | LOW | Финализация исследовательского ответа с источниками |

---

## Расширения

### Скиллы (`skills/*.md`)

Markdown-файлы с YAML-фронтматтером. Автоматически загружаются и перезагружаются.

**Двуязычный TF-IDF матчинг:** скиллы поддерживают ключевые слова на русском и английском. Стоп-слов для обоих языков встроены в matcher.

| Скилл | Scope | Назначение |
|-------|-------|------------|
| `translator` | main | Перевод текстов между языками |
| `excel_normalizer` | document-agent | Нормализация Excel: ИНН, даты, невидимые символы |
| `excel_filler` | document-agent, data-agent | Заполнение Excel-шаблонов с сохранением формул и структуры |
| `meeting_summary` | document-agent | Структурированные итоги встреч с задачами и решениями |
| `data_analyst` | data-agent | Анализ данных, графики, SQL-запросы, конвертация форматов |

```markdown
---
id: my_skill
description: "Описание для семантического матчинга"
allowed_for: ["marketing", "engineering"]
keywords: ["отчёт", "report", "генерация"]
scope: ["main"]  # ["*"] для всех, ["data-agent"] для сабагента
always: false
---

Инструкции для агента...
```

### Плагины (`plugins/<name>/`)

Сложные локальные расширения с subprocess-изоляцией. Это защита от падений и
зависаний плагина, но не полноценная security-песочница: сторонние плагины
нельзя подключать как недоверенный код без отдельной контейнерной изоляции.

```
plugins/my_plugin/
├── manifest.yaml      # Обязательно
├── skill.md           # Опционально — инструкции для агента
├── tool.py            # Опционально — доверенный инструмент в subprocess
└── scripts/           # Опционально
```

### Субагенты (`config/subagents/*.yaml`)

Специализированные агенты с изолированным контекстом и отфильтрованными инструментами:

| Субагент | Инструменты | Назначение |
|----------|-------------|------------|
| `filesystem-agent` | read_file, list_files, search_files, write_file, edit_file | Файловые операции и поиск |
| `document-agent` | read/write/edit_file, normalize_excel, list_files | Создание и редактирование документов |
| `execution-agent` | exec_script, write_file, read_file | Выполнение скриптов и команд |
| `research-agent` | research_search, research_fetch_source, research_read_source, research_store_fact, research_list_facts, research_finalize, web_fetch, web_search | Веб-исследование, проверка источников и финализация ответа |
| `data-agent` | table_query, chart_generate, convert_format, pdf_reader, diff_text, excel_workbook, read/write_file, list_files, search_files, send_file | Анализ данных, SQL, графики, Excel и конвертация |

### MCP-серверы (`config/mcp_servers.yaml`)

```yaml
servers:
  - name: filesystem
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
```

---

## Веб-интерфейс

Веб-канал даёт тот же агентный backend, что Telegram/CLI, но через браузер:

- локальные аккаунты с паролем и HttpOnly session cookie;
- личный workspace пользователя (`workspaces/user_<users.id>`) общий для Telegram и Web;
- современный React/Vite UI с отдельной production-сборкой;
- сворачиваемый файловый диспетчер: дерево папок, поиск, предпросмотр, drag-and-drop,
  загрузка, скачивание, переименование, перемещение, копирование и удаление с подтверждением;
- чат с режимами `execute` и `chat`;
- единая statusline выполнения: WebSocket обновляет текущий статус модели в одной строке,
  не засоряя историю чата отдельными служебными сообщениями;
- подтверждения опасных действий через интерактивный UI.

### Production-like запуск

```bash
uv run corpclaw-lite web-user-link -t <telegram_id> -u <username> -p '<password>'

cd frontend/web
npm ci
npm run build
cd ../..

uv run corpclaw-lite web
```

По умолчанию сервер слушает `http://127.0.0.1:8090`. Настройки находятся в
`config/settings.yaml` → `web_channel`.
Веб-интерфейс собирается отдельным React/Vite приложением в `frontend/web/dist`; если сборки нет,
backend покажет явное предупреждение вместо пустого интерфейса.

### Frontend-разработка

Для работы над UI удобнее держать backend и frontend dev server в разных терминалах.

Терминал 1 — backend и API:

```bash
uv run corpclaw-lite web
```

Терминал 2 — Vite dev server:

```bash
cd frontend/web
npm ci
npm run dev
```

Откройте `http://127.0.0.1:5173`. Vite проксирует `/api` и `/ws` в backend на
`http://127.0.0.1:8090`, поэтому frontend можно менять без пересборки `dist`.

### Пользователи и workspace

Если пользователь уже работает через Telegram, используйте `web-user-link`: тогда веб-вход
получит тот же профиль, память и рабочее пространство по внутреннему `users.id`.
`web-user-create` оставлен только для явных web-only аккаунтов без Telegram. Для исправления
случайно созданного дубля есть `web-user-merge --source-user-id <duplicate> --target-user-id
<canonical>`. Для переноса старых данных из `user_<telegram_id>` в `user_<users.id>` используйте
`uv run corpclaw-lite user-migrate-canonical-ids`.

Это важно архитектурно: контейнер, память и директория `workspaces/user_<id>` теперь привязаны к
внутреннему `users.id`, а не к Telegram ID и не к web-логину. Один человек должен иметь один
профиль, к которому могут быть привязаны разные способы входа.

> Если `container.enabled=true`, для веб-канала также нужен `CORPCLAW_IPC_SECRET`, как и для
> Telegram. Файловые инструменты агента выполняются в контейнере, а операции веб-диспетчера
> дополнительно проверяют границы личного workspace на стороне хоста.

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

В веб-интерфейсе используется тот же словарь статусов, но отображение другое: текущий статус
обновляется в одной statusline над чатом, а не добавляется в историю отдельными сообщениями.

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
| `config/calibration_scenarios.yaml` | 20+ тестовых сценариев для калибровки |
| `config/bootstrap/*.md` | Идентичность агента: SOUL.md, COMPANY.md, BEHAVIOR.md |
| `config/bootstrap/departments/*.md` | Системные промпты по департаментам |
| `config/bootstrap/subagents/*.md` | Системные промпты по субагентам |

### LLM Router

Маршрутизация задач к провайдерам через routing rules в `config/settings.yaml`.
Провайдеры регистрируются через env (`PROVIDER_*__*`), модели и sampling-профили —
через routing rules:

```yaml
# .env
PROVIDER_LLAMACPP__TYPE=openai
PROVIDER_LLAMACPP__BASE_URL=http://localhost:11434/v1
PROVIDER_LLAMACPP__API_KEY=ollama
```

```yaml
# config/settings.yaml
llm:
  routing:
    - task_kind: "default"
      provider: "llamacpp"
      model: "qwen3.6-35b-a3b"
      sampling: "temperature-0.4"      # ссылка на sampling-профиль
    - task_kind: "vision"
      provider: "llamacpp"
      model: "qwen3.6-35b-a3b"
      sampling: "aux-no-thinking"      # thinking off для экстракции
    - subagent_id: "research-agent"
      provider: "llamacpp"
      model: "qwen3.6-35b-a3b"
```

### Модельные профили и sampling (D-056)

Пресет расщеплён на два ортогональных слоя: `ModelProfile` (свойства модели) +
`SamplingProfile` (свойства задачи/фазы). Хранятся в `config/model_presets.yaml`:

```yaml
models:                          # ModelProfile — свойства модели
  qwen3.6-35b-a3b:
    thinking_parser: {source: native}    # reasoning в reasoning_content (Qwen-style)
    default_inference: {temperature: 0.7, top_p: 0.95, top_k: 20}

sampling:                        # SamplingProfile — свойства задачи/фазы
  temperature-0.4:
    model: qwen3.6-35b-a3b
    thinking_mode: default
    inference_overrides: {temperature: 0.4}
  aux-no-thinking:               # thinking off для экстракции (vision/compress/consolidate)
    model: qwen3.6-35b-a3b
    thinking_mode: off
    inference_overrides: {temperature: 0.2}
```

**Приоритет merge:** `model_profile defaults < sampling overrides < RequestOptions
(per-call) < backend extra_body (transport)`.

**PhasePolicy** (`agent/phase_policy.py`) — per-call переключение thinking по
фазе задачи: closing-mode → off; research gathering → off, aggregation → on
(monotonic переход: `research_list_facts` в cumulative tools → все последующие
turns = aggregation, thinking force-on). Auxiliary calls (vision/compress/
consolidate) → off через `aux-no-thinking` sampling (config-driven).

**`LLMRouter.with_overrides()`** — программный atomic override всех agent-роутов
in-memory (для testing/A/B). Перестраивает default/vision/compress/consolidate
роуты сразу → устраняет route-contamination.

Legacy формат (`presets:` комбинированный блок, `RoutingRule.preset`) всё ещё
поддерживается back-compat reader'ом — overlay/unmigrated config работает без
правок.

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
uv run corpclaw-lite chat --telegram-id <tg_id>                 # Интерактивный CLI
uv run corpclaw-lite chat --telegram-id <tg_id> --setup         # Онбординг пользователя

# Telegram
uv run corpclaw-lite telegram                                   # Запуск бота

# Web
uv run corpclaw-lite web                                        # Запуск backend + собранного UI
uv run corpclaw-lite web-user-link -t <tg_id> -u <login> -p '<password>'
uv run corpclaw-lite web-user-create -u <login> -p '<password>' -d <department>
uv run corpclaw-lite web-user-password -u <login> -p '<new_password>'
uv run corpclaw-lite web-user-merge --source-user-id <id> --target-user-id <id>

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
| Agent Core | ~4 400 | 13 |
| Extensions | ~9 100 | 51 |
| Channels | ~6 500 | 22 |
| Calibration | ~1 560 | 8 |
| LLM Providers | ~4 000 | 9 |
| Eval harness (B-060) | ~1 860 | 9 |
| Container | ~830 | 6 |
| Security | ~800 | 6 |
| Memory | ~840 | 4 |
| Onboarding | ~630 | 5 |
| Прочее | ~3 170 | ~27 |
| **Исходный код** | **~34 500** | **~160** |
| **Тесты** | **~30 300** | **~139** (1476 тестов собрано) |

---

## Лицензия

Лицензировано под Apache License, Version 2.0. См. [LICENSE](LICENSE).
