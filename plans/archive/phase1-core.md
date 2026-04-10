# Фаза 1: Ядро CorpClaw Lite

## Summary

Реализация минимального рабочего ядра агента: LLM-провайдеры, базовые типы данных, Simple ReAct цикл, встроенные файловые инструменты и CLI-канал для ручного тестирования. После завершения фазы возможно ручное взаимодействие с агентом в терминале.

**Стартовое состояние:** Почти пустой проект (`xml_tool_calling.py` уже перенесён из v1). Все директории созданы, но файлы `.py` отсутствуют.

**Результат Фазы 1:** `uv run corpclaw-lite chat` → работающий CLI-агент, способный читать/писать файлы и отвечать на вопросы через локальную или облачную LLM.

---

## Goals

- Запустить минимальный рабочий агентный цикл без излишней сложности
- Покрыть ≥75% кода тестами с самого начала
- Использовать pyright strict с 0 ошибок на протяжении всей разработки
- Установить паттерны кода, которым будут следовать Фазы 2–5

---

## Steps

### Шаг 1: Конфигурация и настройки проекта

**Цель:** Pydantic-настройки + YAML-загрузчик с раскрытием переменных окружения.

**Файлы:**
- `src/corpclaw_lite/config/settings.py` — `Settings(BaseSettings)` модель (~120 строк)
  - `llm: LLMSettings` (providers dict, routing, default)
  - `agent: AgentSettings` (max_iterations=15, max_tool_calls=30, max_wall_time_ms=120000)
  - Загружается из `config/settings.yaml` + env vars как override
- `src/corpclaw_lite/config/loader.py` — `load_settings(path: Path) → Settings`
  - Раскрытие `${VAR}` и `${VAR:-default}` (перенести паттерн из v1 `config/loader.py`)
- `config/settings.yaml` — рабочий пример с OpenAI-совместимым эндпоинтом (Ollama localhost)

**Тест:** `tests/test_config.py` — загрузка настроек из тестового YAML, раскрытие переменных.

---

### Шаг 2: Базовые типы данных

**Цель:** Минимальные dataclass/Protocol типы, используемые во всём проекте.

**Файлы:**
- `src/corpclaw_lite/llm/base.py` — уже существует; **проверить и дополнить** если нужно:
  - `Provider` Protocol (`chat`, `stream`)
  - `LLMResponse` dataclass (`content`, `tool_calls`, `usage`)
  - `StreamChunk` dataclass
  - `ToolCall` dataclass (`id`, `name`, `arguments: dict`)
- `src/corpclaw_lite/extensions/tools/base.py` — `Tool` ABC + `RiskLevel` enum:
  ```python
  class Tool(ABC):
      name: str
      description: str
      params: list[ToolParam]
      risk_level: RiskLevel = RiskLevel.LOW

      @abstractmethod
      async def execute(self, **kwargs: Any) -> str: ...
  ```
  **Только 4 атрибута + execute. Никаких дополнительных полей.**
- `src/corpclaw_lite/users/models.py` — `User` dataclass:
  ```python
  @dataclass
  class User:
      id: int
      telegram_id: int | None
      department: str
      name: str
      created_at: datetime
  ```

**Тест:** `tests/test_types.py` — инстанциация, сериализация базовых типов.

---

### Шаг 3: LLM-провайдеры

**Цель:** Два провайдера (Anthropic + OpenAI-compatible) + XML fallback для локальных LLM.

**Файлы:**
- `src/corpclaw_lite/llm/xml_tool_calling.py` — ✅ **Уже перенесён**, не трогать
- `src/corpclaw_lite/llm/anthropic.py` — `AnthropicProvider(Provider)` (~200 строк):
  - `chat(messages, tools, system) → LLMResponse`
  - `stream(messages, tools, system) → AsyncIterator[StreamChunk]`
  - Основа: адаптировать `src/corpclaw/llm/anthropic.py` из v1 под новый Protocol
- `src/corpclaw_lite/llm/openai.py` — `OpenAIProvider(Provider)` (~220 строк):
  - То же что Anthropic, но через `openai` клиент
  - Поддержка `base_url` → работает с Ollama/LM Studio/vLLM
  - Если `response.tool_calls` пустой → парсить через `xml_tool_calling.parse(content)`
- `src/corpclaw_lite/llm/routing.py` — `ProviderRouter` (~80 строк):
  - `get_provider(task_kind: str) → Provider`
  - По умолчанию → `default` провайдер из конфига

**Тест:** `tests/test_llm_providers.py` — с мок-клиентом anthropic/openai, тест XML-fallback.

---

### Шаг 4: Встроенные инструменты (файловые)

**Цель:** Базовый набор инструментов для работы с файловой системой.

**Файлы:**
- `src/corpclaw_lite/extensions/tools/registry.py` — `ToolRegistry`:
  - `register(tool: Tool) → None`
  - `get(name: str) → Tool | None`
  - `list_all() → list[Tool]`
  - `to_schemas() → list[dict]` — конвертация в OpenAI tool schema формат
- `src/corpclaw_lite/extensions/tools/builtin/files.py` — файловые инструменты (~250 строк):

  | Инструмент | Описание | RiskLevel |
  |------------|----------|-----------|
  | `read_file` | Читает текстовый файл (проверяет расширение — не imagen) | `LOW` |
  | `write_file` | Записывает или создаёт файл | `MEDIUM` |
  | `edit_file` | Замена строки в файле | `MEDIUM` |
  | `list_files` | Список файлов в директории | `LOW` |
  | `search_files` | Поиск по содержимому файлов (grep) | `LOW` |

  **Важно:** Каждый инструмент проверяет, что путь находится внутри `/workspace` (или `cwd` в CLI-режиме). Path traversal `../../` → `ToolError`.

**Тест:** `tests/test_builtin_tools.py` — тест каждого инструмента: успешное выполнение + граничные случаи (path traversal, несуществующий файл, неверный тип файла).

---

### Шаг 5: Simple ReAct AgentLoop

**Цель:** Рабочий агентный цикл. Основа: адаптировать `src/corpclaw/agent/executor/prompt_loop_executor.py` из v1, убрав зависимости от `extensibility/` и `orchestration/`.

**Файлы:**
- `src/corpclaw_lite/agent/guards.py` — перенести из v1 без изменений (~120 строк):
  - `SimpleBudgetGuard` — счётчики итераций, вызовов инструментов, walltime
  - `SimpleProgressGuard` — детекция зацикливания (3 подряд одинаковых ошибки)
- `src/corpclaw_lite/agent/context.py` — `ContextBuilder` (~100 строк):
  - `build(user, history, skills, tools) → list[Message]`
  - Собирает системный промпт из SOUL + инструкции скилов + каталог инструментов
- `src/corpclaw_lite/agent/loop.py` — `AgentLoop` (~350 строк):

```
class AgentLoop:
    async def run(self, user, message, channel) → str:
        budget = SimpleBudgetGuard(...)
        progress = SimpleProgressGuard()
        context = ContextBuilder.build(...)

        while budget.ok():
            response = await provider.chat(messages=context.messages, tools=registry.to_schemas())

            if not response.tool_calls:
                break  # Финальный ответ

            for tool_call in response.tool_calls:
                result = await registry.execute(tool_call.name, tool_call.arguments)
                context.add_tool_result(tool_call.id, result)
                progress.record(tool_call.name, result)
                if progress.stuck():
                    break

        return response.content
```

  - `provider.chat()` при needed → XML fallback через `xml_tool_calling.parse()`
  - Никаких TaskPlanner, Verifier, ObjectiveStorage

**Тест:** `tests/test_agent_loop.py` — mock Provider, тест: однократный вызов, мульти-итерация, остановка при отсутствии tool_calls, бюджет-лимит, прогресс-застревание.

---

### Шаг 6: CLI-канал

**Цель:** Простой интерактивный CLI для ручного тестирования агента в терминале.

**Файлы:**
- `src/corpclaw_lite/channels/base.py` — `Channel` Protocol:
  ```python
  class Channel(Protocol):
      async def send_message(self, chat_id: str, text: str, **opts) → None: ...
      async def send_file(self, chat_id: str, path: Path, caption: str = "") → None: ...
      async def request_approval(self, chat_id: str, action: str, details: str) → bool: ...
  ```
- `src/corpclaw_lite/channels/cli.py` — `CLIChannel(Channel)` (~80 строк):
  - `send_message` → `print(text)`
  - `request_approval` → `input("Approve? [y/N]: ")` → `bool`
- `src/corpclaw_lite/main.py` — Typer CLI (~150 строк):
  - `uv run corpclaw-lite chat [--provider anthropic|openai] [--model ...]`
  - `uv run corpclaw-lite version`
  - `uv run corpclaw-lite config show`

**Тест:** `tests/test_channels.py` — unit-тест CLIChannel (mock stdout/stdin).

---

### Шаг 7: Интеграционный тест и сборка

**Цель:** Убедиться что всё работает вместе, CI-готовность.

**Действия:**
- Настроить `pyproject.toml`:
  - `[tool.pyright]` strict mode
  - `[tool.ruff]` правила: E, F, I, UP, B, C4, SIM, G, line-length = 100
  - `[tool.pytest.ini_options]` asyncio_mode = "auto"
  - `pyright` добавить через `uv add --dev pyright`
- `tests/conftest.py` — фикстуры: `mock_provider`, `tmp_workspace`, `agent_loop`
- Smoke-тест: `uv run corpclaw-lite chat --help` должно работать

---

## Status

- [ ] Шаг 1: Конфигурация и настройки проекта
- [ ] Шаг 2: Базовые типы данных
- [ ] Шаг 3: LLM-провайдеры
- [ ] Шаг 4: Встроенные инструменты (файловые)
- [ ] Шаг 5: Simple ReAct AgentLoop
- [ ] Шаг 6: CLI-канал
- [ ] Шаг 7: Интеграционный тест и сборка

---

## Notes

### Зависимости между шагами
```
Шаг 1 (Config) ──► Шаг 3 (LLM)
Шаг 2 (Types)  ──► Шаг 3 (LLM) → Шаг 4 (Tools) → Шаг 5 (AgentLoop) → Шаг 6 (CLI)
```

### Код для переноса из v1 (точные источники)
| Модуль | Источник v1 | Действие |
|--------|------------|----------|
| guards.py | `src/corpclaw/agent/guards.py` | Скопировать, обновить imports |
| anthropic.py | `src/corpclaw/llm/anthropic.py` | Адаптировать под новый Provider Protocol |
| config loader | `src/corpclaw/config/loader.py` | Скопировать env var expansion |
| xml_tool_calling.py | ✅ уже перенесён | Не трогать |

### Что НЕ делаем в Фазе 1
- Нет памяти (SQLite) — добавляется в Фазе 4
- Нет SkillRegistry — добавляется в Фазе 2
- Нет ToolGuard — добавляется в Фазе 3
- Нет Telegram — добавляется в Фазе 4
- Нет Docker-контейнеров — добавляется в Фазе 3
