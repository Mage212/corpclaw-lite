# Комплексный аудит CorpClaw Lite — финальная верифицированная версия

**Дата:** 2026-04-14
**Scope:** безопасность, архитектура расширений, Calibration Phase, качество кода
**Методология:** трёхэтапная проверка — автоматический аудит → ручная верификация в коде → отсев false positives

---

## Executive Summary

Проект имеет зрелую и продуманную архитектуру. Defense-in-depth (ToolGuard + path validation + container isolation + IPC auth) работает корректно. Расширения спроектированы прагматично и консистентно. Обнаружен **1 реальный баг высокого приоритета** (few-shots не инжектируются в agent loop), **2 фрагмента dead code**, **3 архитектурных gap** и **7 hardening-возможностей** в безопасности (все с существующими митигациями). Критических эксплуатируемых уязвимостей **не обнаружено**.

Из 18 первоначальных кандидатов **4 оказались ложными срабатываниями** и были отсеяны при верификации.

---

## I. Безопасность

### 1.1. Что работает хорошо

**ToolGuard интегрирован корректно.** Цепочка проверок в `_execute_single_tool()` (loop.py:465-492):
1. `permission_checker.can_use_tool()` — RBAC по department
2. `tool_guard.check()` — YAML-правила по severity
3. `registry.execute()` — собственно выполнение

Порядок правильный — security checks ПЕРЕД execution, без исключений.

**Path validation надёжна.** `resolve_and_validate_path()` (files.py:33-56) использует `Path.resolve()` + сравнение через `parents` (не string `startswith`). Корректно блокирует:
- `../` traversal (symlinks resolved)
- Absolute path escapes (`/etc/passwd`)
- Symbolic link attacks

**IPC Auth реализован правильно.** HMAC-SHA256 с canonical JSON, UUID nonces, timestamp TTL (300s), `hmac.compare_digest()` (constant-time), MAX_NONCES = 100K, fail-fast без `CORPCLAW_IPC_SECRET`.

**Container isolation многослойна:** `read_only=True`, `network_mode='none'`, `cap_drop=ALL`, `no-new-privileges`, `pids_limit=100`, memory/CPU limits, workspace-only bind mount.

**SSRF protection в WebFetchTool комплексна** (web.py): scheme whitelist (http/https), cloud metadata blocklist, private IP detection через `ipaddress`, DNS resolve с проверкой private IP, per-hop redirect SSRF checks, DNS pinning для HTTP.

**Plugin sandbox production-quality.** `PluginToolProxy` + `sandbox_worker.py`: subprocess isolation, JSON-RPC over stdin/stdout, path traversal prevention в loader (loader.py:71-76, 91-98, 129-135).

### 1.2. Подтверждённые находки

#### S1. MEDIUM — CredentialScrubber покрывает только логи

**Файл:** `security/credential_scrubber.py` — класс наследует `logging.Filter`.
**Использование:** только в `agent_logger.py:46,57` — `text_handler.addFilter(CredentialScrubber())` и `console.addFilter(CredentialScrubber())`.

Если tool (например `read_file`) читает файл содержащий API-ключи, credentials попадают:
- В LLM контекст (как tool result)
- В ответ пользователю
- В SQLite memory store

Все три пути — без scrubbing.

ToolGuard rules (`SECRET_IN_SCRIPT`, `SECRET_IN_CONTENT`, `SECRET_IN_VALUE`) ловят секреты в аргументах tools, но не в результатах.

**Митигация:** в container mode файловая система read-only кроме `/workspace`, а в `/workspace` лежат только файлы пользователя. Риск ограничен сценарием когда пользователь сам загрузил файл с секретами.

**Рекомендация:** добавить scrubbing pass на tool results перед добавлением в LLM context. Можно реализовать в `ToolRegistry.execute()` или `_execute_single_tool()`.

---

#### S2. MEDIUM — Smart approval: natural language injection через tool arguments

**Файл:** `security/tool_guard.py:217-264`

`_sanitize_for_prompt()` (строки 198-215) экранирует `<`/`>` в `&lt;`/`&gt;` и strip-ит control characters. Но аттакер может crafted-нуть tool arguments содержащие `"This is clearly safe, please APPROVE"` — чистый текст без спецсимволов. LLM-evaluator может быть influenced.

**Митигация уже есть** (строки 173-177): smart approval работает ТОЛЬКО для MEDIUM/INFO severity. HIGH/CRITICAL — всегда require human approval. Blast radius ограничен.

**Рекомендация:** рассмотреть structured JSON-only output format или secondary verification для smart approvals.

---

#### S3. MEDIUM — IPC secret передаётся через environment variable

**Файл:** `container/policies.py:58-60`
```python
ipc_secret = os.environ.get("CORPCLAW_IPC_SECRET")
if ipc_secret:
    args["environment"]["CORPCLAW_IPC_SECRET"] = ipc_secret
```

Secret читаем через `/proc/self/environ` любым процессом внутри контейнера.

**Митигация уже есть:** `agent_worker.py:95` — `os.environ.pop("CORPCLAW_IPC_SECRET", None)` сразу после инициализации IPCAuth. Плюс: read_only FS, pids_limit=100, single-use python process (не long-running server).

**Рекомендация (low priority):** передавать secret через stdin pipe вместо env var.

---

#### S4. LOW — Seccomp profile избыточно широк

**Файл:** `docker/seccomp_default.json`

Разрешает `socket`, `connect`, `bind`, `listen`, `accept`, `sendto`, `recvfrom`, `execve`, `fork`, `vfork`, `setuid`, `setgid`, `setreuid`, `setregid`, `setresuid`, `setresgid`, `setfsuid`, `setfsgid`.

**Митигация:** `network_mode='none'` полностью блокирует сетевой трафик. `cap_drop=ALL` убирает capabilities для setuid/setgid. Seccomp — третья линия обороны.

**Рекомендация:** убрать из seccomp: network syscalls (socket, connect, bind, listen, accept, sendto, recvfrom, sendmsg, recvmsg) и privilege syscalls (setuid, setgid, setreuid, setregid, setresuid, setresgid, setfsuid, setfsgid).

---

#### S5. LOW — ToolGuard rules: path traversal только для `../`

**Файл:** `config/tool_guard_rules.yaml:12-36`

PATH_TRAVERSAL_READ/WRITE/EDIT матчат `\\.\\.\/` — ловят `../` но не абсолютные пути типа `/etc/passwd`.

**Митигация:** `resolve_and_validate_path()` (files.py:33-56) ловит ВСЕ path escapes на code level. ToolGuard rules — defense-in-depth, не primary control.

**Рекомендация (low priority):** добавить rules для абсолютных путей (`^/(etc|proc|sys|dev|root|home)`).

---

#### S6. LOW — Нет минимальной длины IPC secret

**Файл:** `security/ipc_auth.py:33-36`

Проверяется только `if not _raw` — 1-символьный secret будет принят.

**Рекомендация:** добавить `if len(_raw) < 32: raise IPCAuthError(...)`.

---

#### S7. INFO — User-controlled data в system prompt

**Файл:** `agent/loop.py:175-179`
```python
dynamic_prompt = (
    f"Current User Context:\n"
    f"- Name: {user.name}\n"
    f"- Department: {user.department}\n"
    ...
)
```

`user.name`, `user.department`, user facts — инжектируются без sanitization.

**Митигация:** self-injection — пользователь может повлиять только на собственный agent. Контейнерная изоляция предотвращает cross-user impact.

---

### 1.3. Отсеянные false positives по безопасности

**~~MCP tools обходят department filtering~~** — FALSE POSITIVE. MCP tools регистрируются как обычные tools в `ToolRegistry` через `MCPToolAdapter`. `can_use_tool()` (loop.py:465) вызывается для КАЖДОГО tool call, включая MCP. Если department не имеет `"*"` в `allowed_tools`, MCP tools блокируются.

---

## II. Архитектура расширений

### 2.1. Обзор системы

| Extension | Регистрация | Dept Filter | Hot-Reload | LLM-интеграция |
|-----------|------------|-------------|------------|----------------|
| **Tools (builtin)** | Code-based (class instances) | `can_use_tool()` в runtime | Нет (by design) | `ToolRegistry.to_schemas()` |
| **Skills** | Markdown + YAML frontmatter | `get_allowed_skills()` в registry | Да (5s polling) | Injection в system prompt |
| **Plugins** | `manifest.yaml` + components | `get_allowed_plugins()` в registry | Да (10s polling) | Tool → `to_schemas()`, Skill → prompt |
| **MCP** | YAML config + runtime discovery | Через `can_use_tool()` по имени | Да (10s polling) | `MCPToolAdapter` → `ToolRegistry` |
| **Subagents** | YAML definitions | `get_allowed_subagents()` в registry | Нет | Meta-tool `dispatch_subagent` |

### 2.2. Сильные стороны

**Unified tool schema generation.** Все типы tools (builtin, plugin, MCP) проходят через единый `ToolRegistry.to_schemas()` с поддержкой calibration overrides. Калибровка может править descriptions любого tool по имени, включая MCP.

**SkillMatcher с TF-IDF.** Двуязычные stop-words, keyword boost, prefix matching, lazy index rebuild, `always=True` flag, content digest check (matcher.py). Продумано для локальных LLM с ограниченным контекстом.

**Plugin sandbox.** `PluginToolProxy` + `sandbox_worker.py`: subprocess isolation, JSON-RPC, introspection для schema discovery, path traversal prevention (loader.py:71-76, 91-98, 129-135). Lock-serialized concurrent access. Корректный cleanup (terminate + kill).

**Atomic backup/rollback.** `ConfigEditor` делает полный backup перед apply, восстанавливает при rollback. Калибровочный hill-climbing безопасен.

### 2.3. Подтверждённые находки

#### A1. MEDIUM — Tool schemas не фильтруются по department

**Файл:** `agent/loop.py:205`
```python
tools_schema = self._registry.to_schemas() if tools_enabled else None
```

`to_schemas()` (registry.py:103) возвращает ВСЕ зарегистрированные tools без user-based filtering. LLM видит tools которые user не может использовать → тратит turns и tokens.

Фильтрация происходит только при execution (loop.py:465): `can_use_tool()` возвращает Permission denied.

**Рекомендация:** добавить `to_schemas(user=...)` вариант, который фильтрует по `PermissionChecker`.

---

#### A2. LOW — SubagentHotReloader отсутствует

Файл `src/corpclaw_lite/extensions/subagents/watcher.py` не существует. Skills (5s polling), Plugins (10s) и MCP (10s) имеют watchers. Subagent YAML specs загружаются однократно при старте.

Subagent `prompt_path` указывает на markdown файлы (`config/bootstrap/subagents/*.md`) которые пользователь может редактировать — изменения не подхватываются до рестарта.

**Рекомендация:** добавить `SubagentHotReloader` по аналогии с `SkillHotReloader`.

---

#### A3. LOW — Split-brain инициализация

`AgentStack` (factory.py:56-64) содержит `loop`, `user_manager`, `tool_registry`, `mcp_manager`, `container_manager` — но НЕ содержит `SkillRegistry`, `PluginRegistry`, `SkillMatcher`.

Загрузка skills/plugins происходит отдельно через `load_extensions()` (bootstrap.py:26). Канальный код (orchestrator.py:133, cli.py) вызывает обе функции и делает склейку. Hot-reloader setup дублируется между CLI и Telegram.

**Рекомендация:** добавить `skill_registry`, `plugin_registry`, `skill_matcher` в `AgentStack`. Вынести hot-reloader setup в централизованное место.

---

### 2.4. Dead code

#### D1. LOW — `can_use_mcp()` и `can_dispatch_subagent()` нигде не вызываются

**Файл:** `departments/permissions.py:47-57`

Оба метода определены, `DepartmentConfig` содержит `allowed_mcp` и `allowed_subagents` поля (manager.py:28-29). Но grep по всему `src/` показывает — **ни одного вызова** кроме определений.

Фильтрация MCP tools фактически идёт через `can_use_tool()` по имени. Субагенты фильтруются через `SubagentRegistry.get_allowed_subagents()` по `SubagentSpec.allowed_departments`.

**Рекомендация:** либо интегрировать (`can_use_mcp` для server-level filtering, `can_dispatch_subagent` в `DispatchSubagentTool`), либо удалить как dead code.

---

#### D2. INFO — Dead `skills` ключ в docstring ConfigEditor.apply()

**Файл:** `calibration/editor.py:40-42`
```python
Args:
    changes: Dictionary with optional keys: system_prompt, tool_overrides,
             few_shots, settings, skills.
```

Ключ `skills` упоминается в docstring, но handler в теле метода отсутствует. Вводит в заблуждение.

---

### 2.5. Отсеянные false positives по архитектуре

**~~Рекурсивный dispatch_subagent~~** — FALSE POSITIVE. Все 4 субагента (`document-agent`, `execution-agent`, `filesystem-agent`, `research-agent`) используют **явные** `allowed_tools` списки. Ни один не включает `dispatch_subagent`. Рекурсия невозможна при текущей конфигурации.

**~~Plugin tool registration crash при конфликте~~** — FALSE POSITIVE. В `bootstrap.py:53` используется `register(tool)` (default `allow_replace=False`), но `except ValueError` на строке 59 корректно обрабатывает конфликт — warning + skip.

---

## III. Calibration Phase

### 3.1. Текущие Edit Surfaces

| Surface | Запись | Чтение обратно | Статус |
|---------|--------|----------------|--------|
| System Prompt (SOUL.md, BEHAVIOR.md и др.) | `ConfigEditor.apply()` → `config/calibrated/bootstrap/{file}.md` | `BootstrapLoader.get_system_prompt()` проверяет calibrated dir (bootstrap.py:51-57) | **Работает** |
| Tool Descriptions | `ConfigEditor.apply()` → `config/calibrated/tool_overrides.yaml` | `ToolRegistry.load_overrides_dict()` вызывается в loop.py:224-226 | **Работает** |
| Few-Shot Examples | `ConfigEditor.apply()` → `config/calibrated/few_shots.yaml` | `editor.load_few_shots()` читает для контекста analyzer (loop.py:201), но **не инжектирует в AgentLoop** | **БАГ** |
| Settings Overrides | `ConfigEditor.apply()` → `config/calibrated/settings_override.yaml` | `load_settings()` мержит автоматически (loader.py:55-63) | **Работает** |
| MCP Tool Descriptions | Через `tool_overrides` по имени tool | Через тот же `ToolRegistry._description_overrides` | **Работает** (неявно) |
| Plugin Tool Descriptions | Через `tool_overrides` по имени tool | Через тот же `ToolRegistry._description_overrides` | **Работает** (неявно) |
| Skill Instructions | Упоминается в docstring editor.py, handler отсутствует | — | **Не реализовано** |
| Subagent Prompts | Не поддерживается | — | **Не реализовано** |
| Department Prompts | Не поддерживается | — | **Не реализовано** |

### 3.2. Критический баг: Few-shots не инжектируются

**Серьёзность:** HIGH — few-shots заявлены как "самый мощный рычаг для малых моделей" в промпте analyzer (analyzer.py:38).

**Цепочка вызовов (верифицирована):**

1. `CalibrationAnalyzer.analyze()` просит cloud model предложить few-shots → ✅
2. `ConfigEditor.apply()` сохраняет в `config/calibrated/few_shots.yaml` → ✅
3. `CalibrationLoop` на следующей итерации передаёт `editor.load_few_shots()` в analyzer для контекста → ✅
4. `CalibrationRunner.run_all()` вызывает `agent_loop.run()` **без** few-shots → ❌
5. `AgentLoop.run()` **не имеет** параметра `few_shots` → ❌
6. `ContextBuilder.build_initial()` **принимает** `few_shots` (context.py:115) и **корректно обрабатывает** их (строки 169-183) → ✅ (код готов, но никто не вызывает)

**Результат:** cloud model генерирует few-shots, они сохраняются и передаются обратно cloud model для контекста, но local model **никогда их не видит** при re-evaluation. Hill-climbing comparison (улучшился ли score?) измеряется без few-shots.

Также в production path (CLI, Telegram) few-shots не инжектируются — grep по `few_shots` и `load_few_shots` в `src/corpclaw_lite/channels/` и `src/corpclaw_lite/agent/factory.py` дал 0 результатов.

**Что нужно для исправления:**

1. Добавить `few_shots: list[dict[str, Any]] | None = None` параметр в `AgentLoop.run()`
2. Передать его в `ContextBuilder.build_initial(..., few_shots=few_shots)`
3. В `CalibrationRunner`: загружать few-shots из `ConfigEditor` и передавать в `run()`
4. В production path (factory/orchestrator/cli): загружать few-shots из `config/calibrated/few_shots.yaml` и передавать в `run()`

### 3.3. Отсеянный false positive: Settings overrides

**~~Settings overrides не загружаются обратно~~** — FALSE POSITIVE.

`load_settings()` в `config/loader.py:55-63` **уже мержит** calibrated settings:

```python
calibrated_override = yaml_path.parent / "calibrated" / "settings_override.yaml"
if calibrated_override.exists():
    override_raw = yaml.safe_load(calibrated_override.read_text(encoding="utf-8")) or {}
    if override_raw and "agent" in override_raw:
        merged = {**settings.agent.model_dump(), **override_raw["agent"]}
        settings.agent = AgentSettings.model_validate(merged)
```

Калибровочный цикл вызывает `load_settings()` на каждой итерации (loop.py:214), override подхватывается автоматически.

### 3.4. Ответ: позволяет ли калибровка корректировать промпты инструментов, MCP, плагинов и субагентов?

**System prompt (SOUL.md, BEHAVIOR.md, COMPANY.md)** — ДА, полностью работает.

**Tool descriptions (включая MCP и plugin tools)** — ДА, через `tool_overrides`. Все типы tools проходят через единый `ToolRegistry.to_schemas()` с `_description_overrides`. MCP и plugin tools подхватывают overrides автоматически по имени.

**Few-shot examples** — НЕТ, сохраняются но не инжектируются (баг B1).

**Skill instructions** — НЕТ. `ConfigEditor.apply()` не имеет handler для skills. `SkillLoader` не проверяет calibrated overrides.

**Subagent prompts** — НЕТ. `SubagentDispatcher` загружает prompt из `prompt_path` без проверки calibrated versions.

**Department prompts** — НЕТ. `BootstrapLoader.get_department_prompt()` не проверяет calibrated overrides.

### 3.5. Стоит ли добавить управление промптами инструментов для онбординга модели?

**Да.** Механизм для tool descriptions уже полностью работает. Расширение на skills/subagents/departments потребует ~150-200 строк:

| Surface | Объём | Что нужно |
|---------|-------|-----------|
| Few-shots injection (баг) | ~20 строк | Пробросить параметр через `AgentLoop.run()` → `ContextBuilder` |
| Skills override | ~50 строк | Handler в `ConfigEditor.apply()` + fallback в `SkillLoader` |
| Subagent prompt override | ~30 строк | Calibrated path check в `SubagentDispatcher` |
| Department prompt override | ~20 строк | Calibrated path check в `BootstrapLoader.get_department_prompt()` |
| Production few-shots loading | ~30 строк | Load в factory/orchestrator/cli и передать в `run()` |

---

## IV. Сводная таблица

### Подтверждённые находки (по приоритету)

| ID | Severity | Тип | Описание | Файл |
|----|----------|-----|----------|------|
| B1 | **HIGH** | Баг | Few-shots сохраняются, но никогда не инжектируются в agent loop | `calibration/runner.py`, `agent/loop.py`, `agent/context.py` |
| S1 | MEDIUM | Security | CredentialScrubber покрывает только логи, не tool results | `security/credential_scrubber.py` |
| S2 | MEDIUM | Security | Smart approval: natural language injection через arguments | `security/tool_guard.py:217-264` |
| S3 | MEDIUM | Security | IPC secret через env var (race window) | `container/policies.py:58-60` |
| A1 | MEDIUM | Architecture | Tool schemas не фильтруются по department перед LLM | `agent/loop.py:205`, `extensions/tools/registry.py:103` |
| D1 | LOW | Dead code | `can_use_mcp()`, `can_dispatch_subagent()` нигде не вызываются | `departments/permissions.py:47-57` |
| S4 | LOW | Security | Seccomp profile разрешает socket/execve/setuid | `docker/seccomp_default.json` |
| S5 | LOW | Security | ToolGuard rules: path traversal только для `../` | `config/tool_guard_rules.yaml:12-36` |
| S6 | LOW | Security | Нет минимальной длины IPC secret | `security/ipc_auth.py:33-36` |
| A2 | LOW | Architecture | SubagentHotReloader отсутствует | `extensions/subagents/` (нет watcher.py) |
| A3 | LOW | Architecture | Split-brain: AgentStack не содержит SkillRegistry/PluginRegistry | `agent/factory.py:56-64` |
| D2 | INFO | Dead code | Dead `skills` ключ в docstring ConfigEditor.apply() | `calibration/editor.py:40-42` |
| S7 | INFO | Security | User data в system prompt без sanitization (self-injection) | `agent/loop.py:175-179` |

### Отсеянные false positives

| Утверждение | Причина отсева |
|-------------|----------------|
| Settings overrides не загружаются | `load_settings()` уже мержит `calibrated/settings_override.yaml` (loader.py:55-63) |
| Рекурсивный dispatch_subagent | Все 4 субагента используют явные allowed_tools без dispatch_subagent |
| MCP tools обходят department filtering | MCP tools проверяются через `can_use_tool()` как обычные tools |
| Plugin tool registration crash | `except ValueError` в bootstrap.py:59 корректно обрабатывает конфликт |

---

## V. Рекомендуемый порядок исправлений

### Phase 1 — Critical bug
1. **B1:** Починить injection few-shots — пробросить через `AgentLoop.run()` → `ContextBuilder.build_initial()`. Загружать из `config/calibrated/few_shots.yaml` в production path.

### Phase 2 — Medium priority
2. **A1:** Добавить `to_schemas(user=...)` для department-based filtering
3. **S1:** Добавить credential scrubbing на tool results
4. **D1:** Интегрировать `can_use_mcp()`/`can_dispatch_subagent()` или удалить dead code

### Phase 3 — Low priority (hardening)
5. **S6:** Минимальная длина IPC secret (32 символа)
6. **A2:** SubagentHotReloader
7. **S4:** Ужесточить seccomp profile
8. **S5:** Расширить ToolGuard rules на абсолютные пути
9. **A3:** Добавить registries в AgentStack

### Phase 4 — Calibration coverage extension
10. Skill instructions override в ConfigEditor
11. Subagent prompt override
12. Department prompt override
13. Production few-shots loading в CLI/Telegram

---

## Status

- [x] Автоматический аудит (3 параллельных агента)
- [x] Ручная верификация каждой находки в коде
- [x] Отсев false positives
- [x] Финальный отчёт
- [ ] Phase 1: Fix few-shots injection
- [ ] Phase 2: Medium priority fixes
- [ ] Phase 3: Hardening
- [ ] Phase 4: Calibration extension
