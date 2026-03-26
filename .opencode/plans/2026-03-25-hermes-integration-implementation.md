# План реализации доработок из Hermes Agent

**Дата:** 25 марта 2026
**Основа:** `plans/hermes-integration-analysis.md`
**Статус:** Готов к выполнению

---

## Обзор фаз

### Фаза 1: Quick Wins (~100 LOC, ~30 мин)

| # | Фича | Файлы | LOC | Сложность |
|---|------|-------|-----|-----------|
| 1.1 | Tool Output Pruning | `context.py`, `loop.py` | ~30 | Очень низкая |
| 1.2 | Media Cache Cleanup | `telegram/file_manager.py` | ~30 | Низкая |

### Фаза 2: Smart Approvals (~60 LOC, ~1 час)

| # | Фича | Файлы | LOC | Breaking Change |
|---|------|-------|-----|-----------------|
| 2.1 | CompressionSettings | `settings.py` | ~15 | Нет |
| 2.2 | Smart Approvals | `tool_guard.py`, `loop.py` | ~60 | `check()` → `async check()` |

### Фаза 3: Context Compression (~300 LOC, ~3-4 часа) — **Критический gap**

| # | Фича | Файлы | LOC | Зависимости |
|---|------|-------|-----|-------------|
| 3.1 | ContextCompressor class | `agent/compressor.py` (новый) | ~250-300 | Provider, settings |
| 3.2 | Интеграция в loop | `loop.py` | ~15 | Фаза 3.1 |
| 3.3 | estimate_tokens() | `context.py` | ~10 | Нет |
| 3.4 | Тесты compressor | `tests/test_compressor.py` (новый) | ~150 | Фаза 3.1-3.3 |

### Фаза 4: Parallel Tool Execution (~50 LOC, ~1 час)

| # | Фича | Файлы | LOC | Риск |
|---|------|-------|-----|------|
| 4.1 | parallel_safe в Tool | `extensions/tools/base.py` | 1 | Низкий |
| 4.2 | Параллельное выполнение | `loop.py` | ~50 | Средний (race conditions) |

---

## Детальный план по шагам

### Фаза 1.1: Tool Output Pruning

- [ ] Добавить в `context.py`:
  - `prune_old_tool_results(protect_tail=6) -> int`
  - `estimate_tokens() -> int`
  - `@property message_count`
- [ ] Добавить в `loop.py` перед `provider.chat()`:
  ```python
  if context.message_count > 10:
      context.prune_old_tool_results()
  ```
- [ ] Добавить тесты в `test_agent_loop.py`

### Фаза 1.2: Media Cache Cleanup

- [ ] Добавить `_sanitize_filename()` в `file_manager.py`
- [ ] UUID prefix для сохранённых файлов
- [ ] Periodic cleanup task в `runner.py`

### Фаза 2.1-2.2: Smart Approvals

- [ ] Добавить `CompressionSettings` в `settings.py`
- [ ] Добавить `approval_mode: str = "manual"` в `AgentSettings`
- [ ] Модифицировать `ToolGuard`:
  - `async def _smart_evaluate(tool_name, args, rule) -> str`
  - `check()` → `async def check()`
- [ ] `loop.py`: `await self._tool_guard.check()`
- [ ] Добавить тесты в `test_tool_guard.py`

### Фаза 3.1-3.4: Context Compression

- [ ] Создать `agent/compressor.py`:
  ```python
  class ContextCompressor:
      def __init__(self, provider: Provider, settings: CompressionSettings): ...
      def should_compress(self, messages: list[dict]) -> bool: ...
      async def compress(self, messages: list[dict]) -> list[dict]: ...
      def _prune_old_tool_results(self, messages, protect_tail) -> tuple[list, int]: ...
      def _sanitize_tool_pairs(self, messages) -> list[dict]: ...
      async def _generate_summary(self, turns) -> str | None: ...
  ```
- [ ] `loop.py`: принять compressor в `__init__`, вызвать в `while True`
- [ ] Тесты:
  - `test_should_compress`
  - `test_prune_tool_results`
  - `test_sanitize_tool_pairs`
  - `test_generate_summary` (mock provider)

### Фаза 4.1-4.2: Parallel Execution

- [ ] `base.py`: `parallel_safe: bool = True` в `Tool`
- [ ] `loop.py`: `_can_parallelize(tool_calls) -> bool`
  - read-only tools → parallel
  - write tools → sequential
- [ ] `asyncio.gather` для безопасных батчей
- [ ] Тесты параллельного выполнения

---

## Рекомендуемый порядок выполнения

```
1.1 → 2.1 → 2.2 → 3.1 → 3.2 → 3.3 → 3.4 → 4.1 → 4.2 → 1.2
```

**Обоснование:**
- 1.1 (Pruning) — standalone, даёт быстрый win
- 2.1-2.2 (Smart Approvals) — breaking change `async check()`, лучше сделать до Compression
- 3.x (Compression) — самая важная фича, зависит от 2.x
- 4.x (Parallel) — оптимизация, можно отложить
- 1.2 (Media Cache) — polish, можно сделать последним

---

## Итоговый объём изменений

| Метрика | Значение |
|---------|----------|
| **Новые файлы** | 2 (`agent/compressor.py`, `tests/test_compressor.py`) |
| **Изменённые файлы** | 5 (`loop.py`, `context.py`, `tool_guard.py`, `settings.py`, `base.py`) |
| **Новый код** | ~500-520 строк |
| **Изменения** | ~50 строк |
| **Новые тесты** | ~200-300 строк |

---

## Конфигурация (settings.yaml)

```yaml
agent:
  compression:
    enabled: true
    threshold_ratio: 0.5          # Порог: сжимать при >50% context length
    max_context_tokens: 8000      # Оценка размера контекста модели
    protect_tail_tokens: 3000     # Сколько токенов хвоста защищать
    summary_ratio: 0.20           # Доля контента для summary
  approval_mode: manual           # manual | smart | off
```

---

## Статус выполнения

- [ ] Фаза 1.1: Tool Output Pruning
- [ ] Фаза 1.2: Media Cache Cleanup
- [ ] Фаза 2.1: CompressionSettings
- [ ] Фаза 2.2: Smart Approvals
- [ ] Фаза 3.1: ContextCompressor class
- [ ] Фаза 3.2: Интеграция в loop
- [ ] Фаза 3.3: estimate_tokens()
- [ ] Фаза 3.4: Тесты compressor
- [ ] Фаза 4.1: parallel_safe в Tool
- [ ] Фаза 4.2: Параллельное выполнение
