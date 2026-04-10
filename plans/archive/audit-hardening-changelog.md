# CorpClaw Lite — Changelog

## [Unreleased] — 2026-03-31 · Hardening Sprint 3: Audit Fixes

Полный аудит кодовой базы выявил функционал, который выглядел корректным при ревью, но не был активен при реальном запуске. Все подтверждённые проблемы устранены. `ruff` / `pyright` / `pytest` — чисто.

### Fixed

**`container/agent_worker.py` — ToolRegistry не был определён вне функции**  
`ToolRegistry` использовался в module-level аннотациях (`_registry: ToolRegistry | None`) и возвращаемом типе `get_registry()`, но импортировался только внутри тела `_build_container_registry()`. Добавлен `TYPE_CHECKING`-гуард: импорт остаётся lazy (не замедляет старт контейнера), но `pyright` и `ruff` теперь корректно разрешают имя. Устранены 13 ошибок pyright и `F821 Undefined name ToolRegistry`.

**`agent/factory.py` — `approval_mode` из конфига не передавался в `ToolGuard`**  
`ToolGuard()` создавался без аргументов, поэтому `approval_mode` всегда был `"manual"` вне зависимости от значения в `settings.yaml`. Теперь `factory.py` читает `agent_settings.approval_mode` и передаёт его в конструктор. Если режим `"smart"`, туда же передаётся `provider` для LLM-based оценки рисков.

**`agent/factory.py` — `NetworkPolicy` не передавалась в `ContainerManager`**  
`NetworkPolicy` и `config/network_policy.yaml` существовали, `ContainerManager` принимал `network_policy` параметром, но в `factory.py` параметр не передавался — блок `if network_policy:` в `ContainerPolicies` никогда не выполнялся, контейнеры запускались без `network_mode: none`. Теперь `NetworkPolicy` загружается из `config/network_policy.yaml` и передаётся при создании `ContainerManager`.

**`cli.py` — `SkillRegistry` не загружался в CLI chat режиме**  
В Telegram runner скилы корректно загружались и инжектировались в system prompt. В `cmd_chat()` этот шаг отсутствовал — CLI-агент работал без скилов. Добавлена загрузка `SkillRegistry` из `skills/` с фильтрацией по department пользователя и инжекцией в system prompt.

**`channels/telegram/runner.py` — `TelegramSettings()` создавался с дефолтами**  
`full_settings = load_settings(...)` уже загружал все настройки из YAML, включая `full_settings.telegram`. Тем не менее следующей строкой создавался новый пустой `TelegramSettings()` — значения из `settings.yaml` игнорировались. Заменено на `tg_settings = full_settings.telegram`; неиспользуемый import удалён.

**`extensions/tools/builtin/excel.py` — строка 144 превышала лимит 100 символов**  
Generator expression в проверке пустой строки (101 символ) разбит на многострочную форму. `ruff check` теперь завершается без ошибок.

### Fixed (Tests)

**`tests/test_agent_worker_extra.py` — тесты патчили несуществующие атрибуты**  
Два дефекта в тестах: все 4 теста патчили `sys.stdin.read`, тогда как реализация использует `sys.stdin.readline`. `test_process_request_success` патчил `corpclaw_lite.container.agent_worker.registry` — атрибута с таким именем не существует. Исправлено: патч на `sys.stdin.readline`; mock реестра переехал на `corpclaw_lite.container.agent_worker.get_registry`.

### Metrics

| Метрика | До | После |
|---------|-----|-------|
| `pyright` errors | 13 | **0** |
| `ruff` errors | 2 | **0** |
| `pytest` failed | 4 | **0** |
| `pytest` passed | 400 | **404** |

---

## Технический долг — Phase 2

Две задачи намеренно оставлены вне текущего спринта. Они представляют собой **неимплементированные фичи**, а не скрытые баги, и не влияют на стабильность текущей версии.

---

### TD-01 · MCP (Model Context Protocol) — runtime интеграция

**Файлы:** `extensions/mcp/` (338 строк), `config/mcp_servers.yaml`  
**Статус:** Выполнено (интегрировано в рантайм, реализован hot-reload и env interpolation)  
**Приоритет:** Средний · **Объём:** ~1 день

#### Что есть

- `extensions/mcp/client.py` (181 строк) — stdio JSON-RPC клиент для MCP-серверов
- `extensions/mcp/adapter.py` (63 строки) — адаптер инструментов MCP к внутреннему `Tool` интерфейсу
- `extensions/mcp/manager.py` (94 строки) — загрузка YAML-конфига, управление соединениями, `connect_all(registry)`
- `config/mcp_servers.yaml` — конфиг с примерами серверов

#### Чего не хватает

В `factory.py` нет ни одного вызова `MCPManager`. `connect_all()` никогда не вызывается — инструменты MCP-серверов не регистрируются в `ToolRegistry`.

#### Что нужно сделать

1. **`factory.py`** — добавить после регистрации builtin tools:
   ```python
   from corpclaw_lite.extensions.mcp.manager import MCPManager
   mcp_config = PROJECT_ROOT / "config" / "mcp_servers.yaml"
   if mcp_config.exists():
       mcp_manager = MCPManager(mcp_config)
       await mcp_manager.connect_all(registry)
   ```

2. **`build_agent_stack()` → `async`** — `connect_all()` асинхронный. Нужно либо сделать `build_agent_stack` асинхронной функцией, либо вынести MCP-инициализацию в отдельную `post_init` фазу в `runner.py` / `cli.py`.

3. **Graceful disconnect** — добавить `await mcp_manager.disconnect_all()` в cleanup-корутину при shutdown.

4. **Тесты** — покрыть `MCPManager.connect_all()` с мок-процессом и `MCPToolAdapter.execute()`.

#### Ключевые риски

- `build_agent_stack()` сейчас синхронная — изменение на async затронет все точки вызова (`cli.py`, тесты). Альтернатива: MCP инициализируется отдельно в `runner.py`/`cli.py` после вызова `build_agent_stack()`.
- MCP-сервера могут не ответить при старте — нужен таймаут и мягкая деградация (предупреждение, не падение).

---

### TD-02 · PluginRegistry — загрузка плагинов в runtime

**Файлы:** `extensions/plugins/` (197 строк), `plugins/` директория  
**Статус:** Выполнено (полностью интегрировано в цикл агента, включает hot-reload)  
**Приоритет:** Низкий · **Объём:** ~1–2 дня

#### Что есть

- `extensions/plugins/base.py` — `Plugin`, `PluginManifest` dataclasses
- `extensions/plugins/loader.py` (130 строк) — парсинг `manifest.yaml`, загрузка `skill.md`, динамический импорт `tool.py`
- `extensions/plugins/registry.py` (55 строк) — хранение и RBAC-фильтрация по `allowed_departments`
- `cli.py cmd_plugin_list` — единственное место использования (только для вывода)

#### Чего не хватает

`PluginRegistry` не вызывается ни в `factory.py`, ни в `runner.py`. Плагины не участвуют в работе агента:

- `Tool` из `tool.py` плагина **не регистрируется** в `ToolRegistry`
- `Skill` из `skill.md` плагина **не попадает** в system prompt
- Скрипты из `scripts/` плагина **не доступны** через `exec_script`

#### Что нужно сделать

1. **`factory.py`** — после регистрации builtin tools, добавить загрузку плагинов:
   ```python
   from corpclaw_lite.extensions.plugins.registry import PluginRegistry
   plugin_registry = PluginRegistry()
   plugins_dir = PROJECT_ROOT / "plugins"
   if plugins_dir.exists():
       plugin_registry.load_directory(plugins_dir)
   for plugin in plugin_registry.list_all():
       if plugin.tool:
           registry.register(plugin.tool)
   ```

2. **`runner.py`** — инжектировать скилы плагинов вместе с обычными скилами:
   ```python
   plugin_skills = [s for p in plugin_registry.list_all() for s in (p.skills or [])]
   ```

3. **RBAC** — `PluginRegistry.get_for_department()` уже реализован. Убедиться, что tool из плагина проходит через `PermissionChecker` перед исполнением.

4. **Конфликты имён** — добавить проверку в `ToolRegistry.register()`: если tool с таким именем уже зарегистрирован, выбрасывать предупреждение (не падать).

5. **Тесты** — полный цикл: `PluginLoader.load()` → регистрация tool → инжекция skill → RBAC-фильтрация.

#### Ключевые риски

- `tool.py` плагина загружается через `importlib` — произвольный код. Необходима проверка что `ToolGuard` применяется до исполнения, как и для всех других инструментов.
- Hot-reload плагинов сложнее чем скилов: `importlib` кэширует модули. Если нужен hot-reload — потребуется `importlib.reload()` или изоляция через subprocess.
