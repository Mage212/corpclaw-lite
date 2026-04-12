# Code Review Fixes v2 — детальный план исправлений

## Summary

Комплексное код-ревью проекта CorpClaw Lite выявило 14 проблем разной критичности.
Все претензии верифицированы чтением исходного кода. Данный план содержит детальное
описание каждой проблемы и конкретное решение.

## Goals

- Исправить 4 критичных бага (P0)
- Устранить 5 средних проблем (P1)
- Провести 5 мелких улучшений (P2)
- Все изменения должны проходить: ruff check, ruff format, pyright strict, pytest

## Steps

### P0-1: Удалить мёртвый `risk` в `loop.py:480`

**Файл:** `src/corpclaw_lite/agent/loop.py`

**Суть проблемы:**
В `_execute_single_tool` на строке 480 вычисляется переменная `risk`:
```python
risk_level = tool.risk_level if tool else None
risk = risk_level.value if risk_level else None  # ← никогда не используется
await self._tool_guard.check(tc.name, tc.arguments, risk_level=risk_level)
```
`risk` — это строковое значение enum'а (`"low"`, `"high"` и т.д.), но оно нигде не
читается. Вероятно, остаток от предыдущей реализации, где `check()` принимал строку
вместо enum.

**Решение:**
Удалить строку 480 (`risk = risk_level.value if risk_level else None`).

**Статус:** [ ] Ожидающий

---

### P0-2: Loop detection warning — дать LLM попытку выйти из loop

**Файл:** `src/corpclaw_lite/agent/loop.py`

**Суть проблемы:**
Когда `SimpleProgressGuard` обнаруживает loop (повторяющаяся ошибка), в context
добавляется assistant-сообщение:
```
"System Guard: You seem to be stuck in a loop..."
```
Но сразу после этого цикл `break`'ается, и возвращается захардкоженный fallback:
```
"I detected a loop and stopped to avoid repeating the same actions."
```
LLM **никогда не видит** это warning-сообщение — оно добавлено в context, но context
не отправляется на следующую итерацию. Warning-сообщение — мёртвая логика.

**Решение (Вариант B — дать LLM попытку):**
Вместо немедленного `break` при loop detection — продолжить цикл, чтобы LLM увидел
warning и попытался сменить стратегию.

**Конкретные изменения:**

1. **Параллельный путь** (строки 290-301):
   - Убрать `break` после `loop_detected = True`
   - После цикла `for tc, result in zip(...)` добавить `if loop_detected: continue`
   - LLM получит warning в context и попытается иначе

2. **Последовательный путь** (строки 339-348):
   - Не `break`, а `continue` после установки `should_stop = True`
   - LLM увидит warning через context

3. **Защита от бесконечного loop:**
   - Budget guard (`SimpleBudgetGuard`) сработает по `max_iterations`/`max_tool_calls`
   - `progress.detect_loop()` использует sliding signature (tool_name + normalized error)
   - Если LLM вызовет другой tool или тот же без ошибки — guard не сработает повторно
   - Добавить счётчик: после 2-го loop detection — `break` без Continue (hard stop)

**Статус:** [ ] Ожидающий

---

### P0-3: Блокировка event loop в `list_active()`/`prune_idle()`

**Файл:** `src/corpclaw_lite/container/manager.py`

**Суть проблемы:**
Два async-метода вызывают синхронный Docker SDK напрямую, без `anyio.to_thread.run_sync`:

- `list_active()` (строка 185): `self._client.containers.list()` — синхронный HTTP-запрос
- `prune_idle()` (строка 195): `self._client.containers.list()`, `container.stop()`,
  `container.remove()` — всё синхронно

При этом в том же классе `ensure_running_async()` и `stop_async()` **правильно**
оборачивают синхронные вызовы через `anyio.to_thread.run_sync`.

При нагрузке (много пользователей, частые prune-циклы) синхронные Docker API-вызовы
заблокируют event loop. Docker SDK делает HTTP-запросы к Docker daemon — каждый может
занимать 10-100ms. Во время блокировки весь async-код (все пользователи, все tool
executions) будет ждать.

**Решение:**

1. `list_active()`: обернуть вызов в `anyio.to_thread.run_sync`:
```python
async def list_active(self) -> list[str]:
    if not self._client:
        return []
    try:
        containers = await anyio.to_thread.run_sync(
            lambda: self._client.containers.list(filters={"name": "corpclaw_agent_"})
        )
        return [str(c.name) for c in containers]
    except Exception:
        return []
```

2. `prune_idle()`: вынести синхронную логику в `_prune_idle_sync()` и обернуть целиком:
```python
def _prune_idle_sync(self) -> int:
    """Synchronous prune implementation — runs in thread."""
    if not self._client:
        return 0
    # ... вся текущая логика prune_idle ...

async def prune_idle(self) -> int:
    return await anyio.to_thread.run_sync(self._prune_idle_sync)
```

3. Добавить `import anyio` на уровне модуля.

**Статус:** [ ] Ожидающий

---

### P0-4: Атомарная загрузка ToolGuard rules

**Файл:** `src/corpclaw_lite/security/tool_guard.py`

**Суть проблемы:**
В `load_file()` (строки 118-124):
```python
for r in rules_data:
    self._rules.append(GuardRule(r))  # ← если GuardRule(r) упадёт на #3
```

Если `GuardRule(r)` бросит исключение (например, невалидный regex в `match_pattern`)
на правиле #3 из 10:
- Правила #1-2 уже добавлены в `self._rules`
- Исключение перехватывается `except Exception` (строка 123), логируется
- Правила #4-10 **никогда не загрузятся**
- Функция `load_file` завершается "успешно"

Для security-critical кода это неприемлемо: половина правил может быть потеряна
без уведомления.

**Решение:**
Двухфазная загрузка — сначала валидация всех правил, потом атомарная замена:

```python
def load_file(self, path: Path | str) -> None:
    file_path = Path(path)
    if not file_path.exists():
        logger.warning("ToolGuard rules file not found: %s", file_path)
        return
    try:
        with open(file_path, encoding="utf-8") as f:
            data = cast(dict[str, Any], yaml.safe_load(f) or {})
        rules_data = cast(list[dict[str, Any]], data.get("rules", []))
        new_rules: list[GuardRule] = []
        for r in rules_data:
            new_rules.append(GuardRule(r))
        self._rules = new_rules  # атомарная замена только если все правила валидны
        logger.info("Loaded %d ToolGuard rules", len(self._rules))
    except Exception as e:
        logger.error("Failed to load ToolGuard rules from %s: %s", file_path, e)
```

Если любой `GuardRule(r)` упадёт — исключение пробросится в `except`, `self._rules`
останется без изменений (старые правила или пустой список).

**Статус:** [ ] Ожидающий

---

### P1-5: Исправить import path `AgentSettings` в `factory.py`

**Файл:** `src/corpclaw_lite/agent/factory.py`

**Суть проблемы:**
Строка 163:
```python
from corpclaw_lite.agent.loop import AgentSettings
```
`AgentSettings` определён в `config/settings.py` и лишь импортирован в `loop.py`
(строка 18). Python делает его доступным в namespace `agent.loop`, но это:
- Вводит в заблуждение (читатель думает, что `AgentSettings` принадлежит `loop.py`)
- Хрупко: если из `loop.py` убрать этот импорт, `factory.py` сломается
- Несогласованно: `build_agent_stack()` на строке 278 правильно импортирует
  `from corpclaw_lite.config.settings import AgentSettings`

**Решение:**
Заменить строку 163:
```python
from corpclaw_lite.config.settings import AgentSettings
```

**Статус:** [ ] Ожидающий

---

### P1-6: Добавить department filtering в SubagentRegistry

**Файлы:**
- `src/corpclaw_lite/extensions/subagents/base.py`
- `src/corpclaw_lite/extensions/subagents/registry.py`
- `src/corpclaw_lite/extensions/subagents/builtin/*.yaml`

**Суть проблемы:**
`SubagentRegistry.get_allowed_subagents()` принимает `user`, но не фильтрует —
возвращает **все** субагенты:
```python
def get_allowed_subagents(self, user: User) -> list[SubagentSpec]:
    """Return subagents the user's department can dispatch."""
    return list(self._subagents.values())
```

`SubagentSpec` не имеет поля `allowed_departments`. Сравните:
- `SkillRegistry.get_allowed_skills()` — фильтрует по `skill.allowed_for`
- `PluginRegistry.get_allowed_plugins()` — фильтрует по `manifest.allowed_departments`
- `PermissionChecker.can_dispatch_subagent()` — проверяет может ли департамент
  вызывать субагентов вообще, но **какие именно** — не фильтруется

Любой департамент с доступом к `dispatch_subagent` может вызвать любой субагент.

**Решение:**

1. Добавить `allowed_departments` в `SubagentSpec`:
```python
@dataclass(frozen=True)
class SubagentSpec:
    ...
    allowed_departments: list[str] = field(default_factory=lambda: ["*"])
```

2. Реализовать фильтрацию в `get_allowed_subagents()`:
```python
def get_allowed_subagents(self, user: User) -> list[SubagentSpec]:
    return [
        spec for spec in self._subagents.values()
        if "*" in spec.allowed_departments or user.department in spec.allowed_departments
    ]
```

3. Обновить `load_directory()` для парсинга `allowed_departments` из YAML.

4. Добавить `allowed_departments` в YAML-файлы субагентов.

**Статус:** [ ] Ожидающий

---

### P1-7: Убрать preset из Anthropic `stream()`

**Файл:** `src/corpclaw_lite/llm/anthropic.py`

**Суть проблемы:**
Docstring (строки 183-185):
> "Model presets ... are NOT applied to streaming requests."

Но строка 192: `system = self._apply_preset(system, kwargs)` — preset **применяется**.

При этом `OpenAIProvider.stream()` действительно **не** применяет preset (строки
324-333 — прямой `kwargs` без вызова `_apply_preset`).

Несогласованность между провайдерами и docstring.

**Решение:**
Привести код в соответствие с docstring. В `stream()` убрать вызов
`self._apply_preset(system, kwargs)`, оставить прямое присвоение `system`:

```python
async def stream(self, ...):
    kwargs: dict[str, Any] = {
        "model": self._model,
        "messages": messages,
        "max_tokens": 4096,
    }
    if system:
        kwargs["system"] = system
    async with self._client.messages.stream(**kwargs) as stream:
        async for text in stream.text_stream:
            yield StreamChunk(content=text)
```

**Статус:** [ ] Ожидающий

---

### P1-8: Удалить дублирующие методы в реестрах

**Файлы:**
- `src/corpclaw_lite/extensions/skills/registry.py`
- `src/corpclaw_lite/extensions/plugins/registry.py`
- `src/corpclaw_lite/extensions/subagents/registry.py`
- Все файлы, вызывающие удалённые методы

**Суть проблемы:**
Три реестра содержат идентичные пары методов:
- `SkillRegistry.get()` ↔ `get_skill()` — оба делают `self._skills.get(skill_id)`
- `PluginRegistry.get()` ↔ `get_plugin()` — оба делают `self._plugins.get(name)`
- `SubagentRegistry.get()` ↔ `get_spec()` — оба делают `self._subagents.get(subagent_id)`

Это раздувает API без добавления ценности. Каждый алиас — потенциальный источник
путаницы («какой метод вызывать?»).

**Решение:**
Оставить `get()` как стандартный (как в `dict`). Удалить:
- `SkillRegistry.get_skill()` → обновить все вызовы на `get()`
- `PluginRegistry.get_plugin()` → обновить все вызовы на `get()`
- `SubagentRegistry.get_spec()` → обновить все вызовы на `get()`

Перед удалением найти все callers через grep.

**Статус:** [ ] Ожидающий

---

### P1-9: Anthropic `_apply_preset` — использовать `setdefault` для `max_tokens`

**Файл:** `src/corpclaw_lite/llm/anthropic.py`

**Суть проблемы:**
Строки 73-75:
```python
if self._preset.thinking_budget_tokens:
    budget = self._preset.thinking_budget_tokens
    kwargs["max_tokens"] = budget + 1024  # прямое присвоение, перезаписывает
```

Это **перезаписывает** любой request-level `max_tokens` (включая дефолтный 4096).
В отличие от OpenAI, который использует `setdefault`:
```python
kwargs.setdefault("max_tokens", budget + 1024)  # не перезаписывает
```

Заявленный приоритет "request-level > preset > provider defaults" нарушается
для Anthropic при наличии thinking_budget.

**Решение:**
Заменить строку 75:
```python
kwargs.setdefault("max_tokens", budget + 1024)
```

**Статус:** [ ] Ожидающий

---

### P2-10: Усилить warning в NetworkPolicy при непустом allowlist

**Файл:** `src/corpclaw_lite/security/network_policy.py`

**Суть проблемы:**
`network_policy.py:67-69` возвращает `network_mode: "none"`, блокируя весь исходящий
трафик. Allowlist (`api.anthropic.com`, `localhost:11434`) передаётся как env var
`ALLOWED_DOMAINS`, но ничто его не читает.

Оператор, видящий `network_policy.yaml` с `localhost:11434`, может думать, что
контейнер может достучаться до Ollama. На самом деле — не может.

Код содержит подробный комментарий (строки 45-60), но при загрузке нет warning'а.

**Решение:**
В `load_file()` после загрузки allowlist — если `self.allowlist` непуст, логировать
WARNING о том, что allowlist не энфорсится при `network_mode=none`:

```python
if self.allowlist:
    logger.warning(
        "NetworkPolicy: %d domains in allowlist, but network_mode='none' blocks ALL traffic. "
        "Allowlist is informational only. See network_policy.py for iptables setup guide.",
        len(self.allowlist),
    )
```

**Статус:** [ ] Ожидающий

---

### P2-11: Рефакторинг `_path_utils.py` — убрать обратную зависимость

**Файлы:**
- `src/corpclaw_lite/extensions/tools/builtin/_path_utils.py`
- `src/corpclaw_lite/extensions/tools/builtin/files.py`

**Суть проблемы:**
`_path_utils.py` — утилитарный модуль для path resolution. Но на строке 69 он
импортирует `resolve_and_validate_path` из `files.py` (sibling-модуль). Это создаёт
reverse dependency: утилита зависит от потребителя.

Если `files.py` когда-нибудь импортирует `_path_utils` — circular import. Сейчас
этого нет, но архитектура хрупкая.

**Решение:**
Перенести `resolve_and_validate_path` из `files.py` в `_path_utils.py`. Обновить
импорт в `files.py`:
```python
from corpclaw_lite.extensions.tools.builtin._path_utils import (
    resolve_and_validate_path,
    resolve_container_path,
)
```

**Статус:** [ ] Ожидающий

---

### P2-12: Plugin.scripts — добавить TODO-комментарий

**Файл:** `src/corpclaw_lite/extensions/plugins/base.py`

**Суть проблемы:**
`plugins/loader.py:126-130` загружает `scripts` из manifest и сохраняет в
`Plugin.scripts`. Но нигде в codebase эти скрипты не выполняются и не обрабатываются.
Поле — dead code, но может понадобиться в будущем.

**Решение:**
Добавить TODO-комментарий над полем `scripts`:
```python
# TODO: script execution not yet implemented
scripts: list[Path] = field(default_factory=lambda: [])
```

**Статус:** [ ] Ожидающий

---

### P2-13: Summaries pruning в компрессоре

**Файл:** `src/corpclaw_lite/agent/compressor.py`

**Суть проблемы:**
`self._summaries: dict[str, str] = {}` (строка 35). Ключ — `mem_key` (user identifier).
Каждый уникальный пользователь добавляет одну запись. Нет eviction policy.
При 10,000 пользователей — ~10-20 MB. Растёт линейечно без ограничений.

Сравните с `MemoryConsolidator`, который имеет `_MAX_TRACKED_USERS = 5000`
и метод `_prune_tracked()`.

**Решение:**
Добавить константу и pruning-метод:
```python
_MAX_SUMMARIES = 1000

def _prune_summaries(self) -> None:
    if len(self._summaries) > _MAX_SUMMARIES:
        sorted_keys = sorted(self._summaries, key=self._summaries.get)
        for k in sorted_keys[:len(sorted_keys) - _MAX_SUMMARIES // 2]:
            del self._summaries[k]
```
Вызывать в конце `compress()` после записи summary.

**Статус:** [ ] Ожидающий

---

### P2-14: Исправить транслит в YAML субагентов

**Файлы:**
- `src/corpclaw_lite/extensions/subagents/builtin/execution.yaml`
- `src/corpclaw_lite/extensions/subagents/builtin/filesystem.yaml`

**Суть проблемы:**
4 YAML-файла субагентов с inconsistent `description`:
- `document.yaml`: кириллица (`"Создание, редактирование..."`)
- `research.yaml`: кириллица (`"Поиск информации..."`)
- `execution.yaml`: транслит (`"Vypolnyaet skripty..."`)
- `filesystem.yaml`: транслит (`"Ekspert po rabote..."`)

Два файла написаны по-русски, два — транслитом. Это oversight.

**Решение:**
Привести к единому стилю (кириллица):
- `execution.yaml`: `"Vypolnyaet skripty..."` → `"Выполняет скрипты, тесты и компилирует код. Отлично подходит для запуска bash-команд и скриптов."`
- `filesystem.yaml`: `"Ekspert po rabote..."` → `"Эксперт по работе с файловой системой и поиском. Отлично подходит для анализа кодовой базы и навигации."`

**Статус:** [ ] Ожидающий

---

## Порядок выполнения

| Порядок | Пункт | Сложность | Файлы |
|---------|-------|-----------|-------|
| 1 | P0-1 | 1 строка | `loop.py` |
| 2 | P1-5 | 1 строка | `factory.py` |
| 3 | P1-9 | 1 строка | `llm/anthropic.py` |
| 4 | P0-4 | ~15 строк | `security/tool_guard.py` |
| 5 | P0-2 | ~30 строк | `loop.py` |
| 6 | P0-3 | ~40 строк | `container/manager.py` |
| 7 | P1-7 | ~5 строк | `llm/anthropic.py` |
| 8 | P1-8 | ~20 строк + callers | 3 registry + все вызовы |
| 9 | P1-6 | ~30 строк | `subagents/base.py`, `registry.py`, 4 YAML |
| 10 | P2-10 | ~5 строк | `security/network_policy.py` |
| 11 | P2-11 | ~30 строк | `_path_utils.py`, `files.py` |
| 12 | P2-12 | ~1 строка | `plugins/base.py` |
| 13 | P2-13 | ~10 строк | `compressor.py` |
| 14 | P2-14 | 2 строки | 2 YAML файла |

**Полная проверка после всех изменений:**
```bash
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

## Notes

- Все претензии верифицированы чтением исходного кода
- 4 из 4 P0 багов подтверждены
- 5 из 5 P1 проблем подтверждены
- Решения согласованы с пользователем
- Предыдущий план (code-review-fixes.md v1) архивирован
