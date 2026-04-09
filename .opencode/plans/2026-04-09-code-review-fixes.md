# План исправлений по результатам код-ревью

## Summary

Исправление всех подтверждённых претензий код-ревью: 5 критических багов,
проблемы безопасности, архитектурные улучшения и low-priority правки.
Работа разбита на 6 фаз, каждая — независимый коммит.

## Ссылка на ревью

Подробности каждой претензии — в результатах код-ревью (сессия opencode).
Все претензии перепроверены чтением исходного кода.

---

## Phase 0 — Критические баги (P0)

> Цель: исправить 5 багов, которые вызывают потерю данных или некорректное поведение.

### 0.1. Timeout handler: неверный ключ памяти

**Файл:** `src/corpclaw_lite/agent/loop.py:232`
**Проблема:** `str(user.id)` вместо `mem_key` — timeout-сообщение сохраняется
в чужую историю, пользователь его не увидит.
**Исправление:** Заменить `str(user.id)` на `mem_key`.

```python
# Было:
await self._memory.add_message(str(user.id), "assistant", msg)
# Станет:
await self._memory.add_message(mem_key, "assistant", msg)
```

### 0.2. AgentLogger handler leak в CLI chat loop

**Файл:** `src/corpclaw_lite/cli.py:356`
**Проблема:** `AgentLogger()` создаётся на каждой итерации цикла, добавляя
новый `RotatingFileHandler` к глобальному `agent_activity` логгеру.
**Исправление:** Вынести `AgentLogger` создание за пределы цикла (один раз
после `_run()` старта).

```python
# Перенести перед while-циклом (после строки 297):
agent_activity_logger = AgentLogger(log_dir=PROJECT_ROOT / _log.log_dir)
# Внутри цикла использовать agent_activity_logger.log_request(...)
```

### 0.3. IPCToolProxy не проксирует parallel_safe и terminal

**Файл:** `src/corpclaw_lite/container/proxy.py:56-65`
**Проблема:** `from_tool()` копирует name/description/params/risk_level,
но не parallel_safe и terminal. Proxy всегда `parallel_safe=True, terminal=False`.
**Исправление:** Добавить копирование атрибутов.

```python
@classmethod
def from_tool(cls, tool: Tool, ipc: ContainerIPC) -> IPCToolProxy:
    proxy = cls(
        name=tool.name,
        description=tool.description,
        params=tool.params,
        risk_level=tool.risk_level,
        ipc=ipc,
    )
    proxy.parallel_safe = tool.parallel_safe
    proxy.terminal = tool.terminal
    return proxy
```

### 0.4. SQLiteMemory content round-trip bug

**Файл:** `src/corpclaw_lite/memory/sqlite.py:119,143-146`
**Проблема:** Строка `"true"` (валидный JSON) десериализуется в `True`,
`"123"` в 123, `"null"` в None. Храним строки — должны получать строки.
**Исправление:** В `_sync_get_history` убрать `json.loads` — всегда
возвращать строку. Callers (`context.py`) уже делают `str(item["content"])`.

```python
# Было:
try:
    content: Any = json.loads(content_str)
except json.JSONDecodeError:
    content = content_str
# Станет:
content = content_str
```

### 0.5. IPC stdout contamination protection

**Файл:** `src/corpclaw_lite/container/ipc.py:131-132`
**Проблема:** Если stdout содержит артефакты от библиотек, `json.loads` падает.
**Исправление:** Читать только последнюю строку stdout (agent_worker пишет
JSON в последнюю строку).

```python
lines = stdout.decode("utf-8").strip().split("\n")
response_str = lines[-1]  # agent_worker всегда пишет JSON последней строкой
```

---

## Phase 1 — Безопасность (P1)

> Цель: устранить подтверждённые проблемы безопасности.

### 1.1. Seccomp профиль: убрать опасные syscalls

**Файл:** `docker/seccomp_default.json`
**Проблема:** ~250 разрешённых syscalls включая mount, reboot, ptrace, bpf,
kexec_*, init_module, chroot, pivot_root, mknod.
**Исправление:** Удалить из allowlist:
mount, umount2, reboot, ptrace, bpf, kexec_load, kexec_file_load,
init_module, delete_module, finit_module, chroot, pivot_root, mknod,
acct, iopl, ioperm, swapon, swapoff, sethostname, setdomainname,
create_module, get_kernel_syms, query_module, nfsservctl,
process_vm_readv, process_vm_writev, userfaultfd, perf_event_open.

### 1.2. policies.py: убрать seccomp=unconfined

**Файл:** `src/corpclaw_lite/container/policies.py:56`
**Проблема:** seccomp=unconfined при strict_capabilities=True полностью
отключает seccomp.
**Исправление:** Использовать seccomp_default.json вместо unconfined:

```python
if settings.strict_capabilities:
    args["cap_drop"] = ["ALL"]
    seccomp_path = Path(__file__).parent.parent.parent.parent / seccomp_profile_path
    if seccomp_path.exists():
        args["security_opt"].append(f"seccomp={seccomp_path}")
```

### 1.3. ToolGuard rules: расширить покрытие exec_script

**Файл:** `config/tool_guard_rules.yaml`
**Проблема:** Только rm -rf покрывается. Нет правил для Python-based
уничтожения, shutil.rmtree, dd, chmod 777, curl | bash.
**Исправление:** Добавить правила:

```yaml
- id: DANGEROUS_SHUTIL_RMTREE
  description: "Attempting to destroy directory tree via Python"
  tool: "exec_script"
  severity: CRITICAL
  match_param: "script"
  match_pattern: "shutil\\.rmtree"
  require_approval: false

- id: DANGEROUS_PIPE_TO_SHELL
  description: "Pipe to shell execution pattern"
  tool: "exec_script"
  severity: HIGH
  match_param: "script"
  match_pattern: "(curl\\s.*\\|\\s*(ba)?sh|wget\\s.*\\|\\s*(ba)?sh)"
  require_approval: true

- id: CHMOD_777
  description: "Setting overly permissive file permissions"
  tool: "exec_script"
  severity: MEDIUM
  match_param: "script"
  match_pattern: "chmod\\s+(.*\\s)?777"
  require_approval: true
```

### 1.4. ToolGuard rules: web_fetch SSRF

**Файл:** `config/tool_guard_rules.yaml`
**Проблема:** Нет правил для web_fetch. Потенциальный SSRF и data exfiltration.
**Исправление:** Добавить:

```yaml
- id: WEB_FETCH_PRIVATE_IP
  description: "Fetching internal/private network addresses"
  tool: "web_fetch"
  severity: HIGH
  match_param: "url"
  match_pattern: "(127\\.|10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.|localhost|0\\.0\\.0\\.0)"
  require_approval: true

- id: WEB_FETCH_SENSITIVE_PATHS
  description: "Fetching sensitive local paths"
  tool: "web_fetch"
  severity: MEDIUM
  match_param: "url"
  match_pattern: "(file://|metadata\\.google\\.internal)"
  require_approval: true
```

### 1.5. ToolGuard: severity enum валидация

**Файл:** `src/corpclaw_lite/security/tool_guard.py:52`
**Проблема:** GuardRule.severity: str — опечатка в YAML молча понижает
критичность до INFO.
**Исправление:** Валидировать при загрузке:

```python
valid_severities = {s.value for s in RuleSeverity}
if self.severity not in valid_severities:
    logger.warning("Rule '%s': invalid severity '%s', defaulting to INFO", self.id, self.severity)
    self.severity = RuleSeverity.INFO
```

### 1.6. ToolGuard: evaluate() bypass для non-string params

**Файл:** `src/corpclaw_lite/security/tool_guard.py:70`
**Проблема:** non-string параметры молча обходят правило.
**Исправление:** Приводить к строке:

```python
val = arguments.get(self.match_param)
if val is not None and self._regex.search(str(val)):
    return True
```

---

## Phase 2 — Обработка ошибок и типы (P2)

> Цель: убрать хрупкие паттерны, улучшить типобезопасность.

### 2.1. preset: Any — ModelPreset

**Файлы:** `src/corpclaw_lite/llm/anthropic.py:20`, `src/corpclaw_lite/llm/openai.py:28`
**Проблема:** preset: Any | None — циклической зависимости нет.
**Исправление:** Заменить на ModelPreset | None с прямым импортом.

### 2.2. cast(Any, ...) в context.py

**Файл:** `src/corpclaw_lite/agent/context.py:143`
**Исправление:** Убрать бессмысленный cast(Any, ...) — оставить str(...).

### 2.3. ToolRegistry.execute: логировать traceback

**Файл:** `src/corpclaw_lite/extensions/tools/registry.py:96-97`
**Исправление:** Заменить `except Exception as e` на `except Exception` с
`logger.exception("Tool '%s' execution failed", name)`.

### 2.4. loop.py: generic Exception в tool execution

**Файл:** `src/corpclaw_lite/agent/loop.py:474-475`
**Исправление:** Добавить `logger.exception(...)` перед возвратом строки ошибки.

### 2.5. Убрать file-level pyright suppressions (частично)

Оставить suppressions для файлов с внешними SDK без type stubs
(docker, openai, anthropic, telegram-bot). Для sqlite.py — заменить на
точечные `# type: ignore[...]`.

### 2.6. result.startswith("Error") — документировать конвенцию

**Файлы:** `agent/loop.py:331`, `agent/guards.py:63`
**Исправление:** Добавить константу TOOL_ERROR_PREFIX = "Error" в tools/base.py
и использовать её. Не менять логику — только формализовать конвенцию.

---

## Phase 3 — Архитектура (P3)

> Цель: устранить структурные проблемы, улучшить maintainability.

### 3.1. build_agent_stack — dataclass return

**Файл:** `src/corpclaw_lite/agent/factory.py:157-159`
**Исправление:** Создать dataclass AgentStack с полями:
loop, user_manager, tool_registry, mcp_manager, container_manager.
Обновить callers: cli.py, calibration/loop.py.

### 3.2. DRY: shared tool list в factory.py

**Файл:** `src/corpclaw_lite/agent/factory.py:118-126, 144-152`
**Исправление:** Создать функцию `_sandboxed_tool_classes() -> list[Tool]`
и использовать в обоих местах.

### 3.3. DRY: shared PLACEHOLDER constant

**Файлы:** `agent/compressor.py:27`, `agent/context.py:83`
**Исправление:** Импортировать PLACEHOLDER из compressor.py в context.py.

### 3.4. DRY: shared container path translation

**Файлы:** `extensions/tools/builtin/image.py:70-95`, `send_file.py:68-92`
**Исправление:** Создать `extensions/tools/builtin/_path_utils.py` с функцией
`resolve_container_path()`. Использовать в обоих файлах.

### 3.5. Декомпозиция runner.py (run_telegram_bot)

**Файл:** `src/corpclaw_lite/channels/telegram/runner.py` (461 строка)
**Подход:** Разбить на логические блоки:
1. `_build_bot_deps()` — загрузка настроек, agent stack, MCP, extensions
2. `_register_handlers(app, deps)` — регистрация handlers
3. `_setup_hot_reloaders(app, deps)` — watchers
4. `_setup_health_endpoint()` — aiohttp
5. `_run_cleanup(deps)` — finally block

### 3.6. Декомпозиция cli.py (cmd_chat)

**Файл:** `src/corpclaw_lite/cli.py`
**Подход:** Вынести `_run()` closure в отдельную async функцию `_run_cli_chat()`.

### 3.7. Добавить __init__.py во все пакеты

Пакеты без __init__.py:
agent/, llm/, channels/, extensions/, extensions/tools/,
extensions/tools/builtin/, extensions/skills/, extensions/subagents/,
security/, memory/, config/, departments/, users/, container/

### 3.8. Убрать мёртвый код

- `container/manager.py:75` — `_active_containers`
- `channels/telegram/callback_data.py:35-36` — CB_APPROVE/CB_DENY

### 3.9. build_xml_fallback_system / build_xml_repair_prompt

**Файл:** `src/corpclaw_lite/llm/xml_tool_calling.py:168-185`
Оставить (часть публичного API в __all__), добавить комментарий-пояснение.

### 3.10. ApprovalRequest docstring

**Файл:** `src/corpclaw_lite/security/tool_guard.py:37`
Добавить пояснение почему Exception — design choice, не ошибка.

---

## Phase 4 — Утечки памяти и хрупкость (P4)

> Цель: предотвратить рост словарей в долгоживущих процессах.

### 4.1. ContainerIPC._last_used — bounded dict

**Файл:** `src/corpclaw_lite/container/ipc.py:54`
Ограничить до `_MAX_LAST_USED = 10_000`. При превышении — удалить старые.

### 4.2. MemoryConsolidator._last_consolidated — bounded dict

**Файл:** `src/corpclaw_lite/memory/consolidation.py:59`
Аналогично — `_MAX_TRACKED_USERS = 5000`.

### 4.3. Magic numbers — AgentSettings

**Файл:** `src/corpclaw_lite/agent/loop.py:227,166`
Добавить в AgentSettings:
- `llm_timeout_seconds: int = 120`
- `max_facts_recall: int = 20`

### 4.4. ContainerManager sync — async wrappers

**Файл:** `src/corpclaw_lite/container/manager.py`
Добавить `ensure_running_async()` и `stop_async()` с `anyio.to_thread.run_sync`.
Обновить callers.

---

## Phase 5 — Low priority (P5)

> Цель: подчистить мелочи.

### 5.1. Inline imports — добавить комментарии

**Файлы:** runner.py, factory.py, cli.py
Добавить `# Deferred import: avoids circular dependency`.

### 5.2. Unused user parameter в VisionProcessor.describe()

**Файл:** `src/corpclaw_lite/agent/vision.py:36`
Переименовать в `_user` — placeholder для department-aware filtering.

### 5.3. Anthropic preset inference_params — фильтрация

**Файл:** `src/corpclaw_lite/llm/anthropic.py:58`
Добавить `_ANTHROPIC_STANDARD_PARAMS` frozenset и фильтровать params.

### 5.4. LLMRouter.stream() — убрать type: ignore

**Файл:** `src/corpclaw_lite/llm/router.py:175`
Переписать без `await` — Provider.stream() возвращает AsyncIterator напрямую.

### 5.5. CredentialScrubber: PATTERNS как tuple

**Файл:** `src/corpclaw_lite/security/credential_scrubber.py:20`
Заменить list на tuple — неизменяемый.

### 5.6. os._exit(0) в cli.py — расширить комментарий

**Файл:** `src/corpclaw_lite/cli.py:394`
Пояснить почему необходим os._exit вместо sys.exit.

---

## Порядок выполнения

| Фаза | Затронутые файлы | Риск | Тесты |
|------|-----------------|------|-------|
| P0 | loop.py, cli.py, proxy.py, sqlite.py, ipc.py | Низкий | test_agent_loop, test_cli, test_container_ipc, test_memory |
| P1 | tool_guard.py, tool_guard_rules.yaml, seccomp_default.json, policies.py | Низкий | test_tool_guard, test_container_manager |
| P2 | anthropic.py, openai.py, context.py, registry.py, loop.py, base.py | Низкий | test_llm_providers, test_agent_loop, test_builtin_tools |
| P3 | factory.py, compressor.py, context.py, image.py, send_file.py, runner.py, cli.py, manager.py, callback_data.py, xml_tool_calling.py, tool_guard.py, +15 __init__.py | Средний | test_agent_loop, test_factory, test_telegram, test_container_manager, test_coverage_extras |
| P4 | ipc.py, consolidation.py, loop.py (settings.py), manager.py | Низкий | test_container_ipc, test_consolidation, test_agent_loop |
| P5 | vision.py, anthropic.py, router.py, credential_scrubber.py, cli.py, runner.py, factory.py | Минимальный | Существующие тесты без изменений |

---

## Критерии готовности каждой фазы

```bash
uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v
```

Все 4 команды должны пройти без ошибок перед переходом к следующей фазе.

---

## Статус

- [x] Phase 0 — Критические баги
- [x] Phase 1 — Безопасность
- [x] Phase 2 — Обработка ошибок и типы
- [x] Phase 3 — Архитектура
- [x] Phase 4 — Утечки памяти и хрупкость
- [x] Phase 5 — Low priority
