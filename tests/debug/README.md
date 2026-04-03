# Debug Integration Tests — CorpClaw Lite

Полноценные интеграционные тесты, запускающие **реальный** агентный пайплайн:
реальный LLM, реальные инструменты, реальные файлы на диске.

> **Не входят в обычный набор** `pytest tests/` — запускаются только явно.

---

## Структура

| Файл | Группа | Что тестируется |
|------|--------|----------------|
| `test_A_single_turn.py` | A | Базовые однотурные ответы без инструментов (5 тестов) |
| `test_B_tools.py` | B | Каждый из 12 инструментов по отдельности (12 тестов) |
| `test_C_container.py` | C | Docker lifecycle: старт, IPC, изоляция, prune (6 тестов) |
| `test_D_multistep.py` | D | Многошаговые рабочие сценарии 3+ инструментов (7 тестов) |
| `test_E_subagents.py` | E | Делегация субагентам, изоляция инструментов (6 тестов) |
| `test_F_security.py` | F | Guards, path traversal, SSRF, ToolGuard (8 тестов) |

---

## Запуск

### Быстрая дымовая проверка (Group A, ~1 мин)
```bash
uv run pytest tests/debug/test_A_single_turn.py -v -s
```

### Все тесты без Docker (~5-10 мин)
```bash
uv run pytest tests/debug/ -v -m "not docker_required"
```

### С подробными логами агента
```bash
uv run pytest tests/debug/ -v -s --log-cli-level=DEBUG -m "not docker_required"
```

### Только контейнерные тесты (требует Docker)
```bash
uv run pytest tests/debug/test_C_container.py -v -s
```

### Конкретная группа
```bash
uv run pytest tests/debug/test_D_multistep.py -v -s
```

### Конкретный тест
```bash
uv run pytest tests/debug/test_B_tools.py::test_B01_write_file -v -s
```

---

## Требования

### LLM провайдер
Тесты используют `config/settings.yaml` как есть. Убедитесь что:
- Настроен провайдер в `llm.named.default`
- API ключ задан в `.env` (если используется cloud-провайдер)
- Или Ollama запущен на `localhost:11434` (для локальных моделей)

```bash
# Проверка что LLM отвечает
curl http://localhost:11434/api/tags
```

### Для группы C (Docker)
```bash
# Docker должен быть запущен
docker ps

# Образ должен быть собран
docker images | grep corpclaw-agent-base

# IPC секрет (можно любой для тестов)
export CORPCLAW_IPC_SECRET=debug-integration-secret
```

---

## Интерпретация результатов

### Group E (субагенты)
Тесты E1–E4 зависят от решения LLM вызвать `dispatch_subagent`.
Если тест упал с `AssertionError: Expected tool 'dispatch_subagent' to be used` — значит
модель решила выполнить задачу напрямую, без делегации.

**Это нормально для слабых/локальных моделей.** Рассмотрите:
1. Переключение на более мощный провайдер (cloud в settings.yaml)
2. Добавление субагентного контекста в SOUL.md (системный промпт)

### Таймауты
По умолчанию каждый тест ограничен `max_wall_time_ms=120000` (2 мин) агентным бюджетом.
Для медленных локальных моделей тест F4 может завершиться с `status=budget` — это корректно.

---

## Обычные тесты не пострадали

Debug-папка исключена из стандартного запуска через `pyproject.toml`:
```toml
addopts = "--ignore=tests/debug"
```

```bash
# Убедиться что обычные тесты всё ещё зелёные
uv run pytest tests/ -v
```
