# Анализ состояния CorpClaw Lite + Путь развития

**Дата:** 23 марта 2026  
**Тесты:** 151 passed, ruff clean, git: `ce0f790`  
**Итог:** ~87% от дизайн-документа реализовано

---

## 1. Что реализовано — по фазам

### Фаза 1: Ядро — 100%

| Компонент | Файл | Строк |
|-----------|------|-------|
| AgentLoop (ReAct) | `agent/loop.py` | 187 |
| SimpleBudgetGuard + SimpleProgressGuard | `agent/guards.py` | 158 |
| Context builder | `agent/context.py` | 90 |
| VisionProcessor | `agent/vision.py` | 76 |
| SubagentDispatcher | `agent/subagent.py` | 81 |
| AnthropicProvider | `llm/anthropic.py` | 143 |
| OpenAIProvider + Ollama | `llm/openai.py` | 145 |
| XML tool calling fallback | `llm/xml_tool_calling.py` | 129 |
| ProviderRouter | `llm/routing.py` | 30 |
| Files tools (read/write/edit/list/search) | `tools/builtin/files.py` | 214 |
| Web fetch + SSRF protection | `tools/builtin/web.py` | 237 |
| Memory tools (store/recall) | `tools/builtin/memory.py` | 84 |
| Image tool (vision → text) | `tools/builtin/image.py` | 56 |
| Exec script tool | `tools/builtin/exec_script.py` | 72 |
| Excel normalize tool | `tools/builtin/excel.py` | 162 |
| Send file tool | `tools/builtin/send_file.py` | 69 |
| Dispatch subagent tool | `tools/builtin/dispatch.py` | 58 |
| ToolRegistry | `tools/registry.py` | 90 |
| CLI Channel | `channels/cli.py` | ~50 |

### Фаза 2: Расширения и RBAC — 90%

| Компонент | Файл | Статус |
|-----------|------|--------|
| Skills (loader + registry + hotreload) | `extensions/skills/` | ✅ |
| 5 builtin skills | `skills/*.md` | ✅ |
| Plugins (manifest loader + registry) | `extensions/plugins/` | ✅ |
| Subagents (base + registry) | `extensions/subagents/` | ✅ |
| Builtin subagents: filesystem + execution | `extensions/subagents/builtin/` | ✅ |
| Builtin subagents: research + document | `extensions/subagents/builtin/` | ❌ Нет |
| DepartmentManager + PermissionChecker | `departments/` | ✅ |
| departments.yaml | `config/departments.yaml` | ✅ |
| MCP Client + Manager + Adapter | `extensions/mcp/` | ✅ |

### Фаза 3: Безопасность — 95%

| Компонент | Файл | Статус |
|-----------|------|--------|
| ToolGuard (YAML rules, severity, approval) | `security/tool_guard.py` | ✅ |
| tool_guard_rules.yaml | `config/tool_guard_rules.yaml` | ✅ |
| NetworkPolicy | `security/network_policy.py` | ✅ |
| network_policy.yaml | `config/network_policy.yaml` | ✅ |
| IPC Auth (HMAC + nonce, replay protection) | `security/ipc_auth.py` | ✅ |
| CredentialScrubber | `security/credential_scrubber.py` | ✅ |
| ContainerManager | `container/manager.py` | ✅ |
| Container IPC | `container/ipc.py` | ✅ |
| Container policies | `container/policies.py` | ✅ |
| Container agent worker | `container/agent_worker.py` | ✅ |
| **docker/Dockerfile.agent + seccomp** | `docker/` | ❌ Нет |

### Фаза 4: Telegram + память + логи — 80%

| Компонент | Файл | Статус |
|-----------|------|--------|
| Channel Protocol | `channels/base.py` | ✅ |
| TelegramChannel (polling, approval, send_file) | `channels/telegram/channel.py` | ✅ |
| Telegram runner + bootstrap | `channels/telegram/runner.py` | ✅ |
| SQLite память (история + факты) | `memory/sqlite.py` | ✅ |
| Structured logging (agent_activity.jsonl) | `logging/agent_logger.py` | ✅ |
| Health HTTP endpoint | `logging/health.py` | ✅ |
| Bootstrap (SOUL.md + COMPANY.md) | `config/bootstrap/` | ✅ |
| **BEHAVIOR.md** | `config/bootstrap/BEHAVIOR.md` | ❌ Нет |
| **bootstrap/departments/*.md** | `config/bootstrap/departments/` | ❌ Пусто |
| LLM Consolidation (сжатие истории) | `memory/consolidation.py` | ❌ Нет |
| Vector Memory (Qdrant) | `memory/vector.py` | ⚠️ Stub |

### Фаза 5: Полировка — 75%

| Компонент | Статус |
|-----------|--------|
| MCP интеграция | ✅ |
| Skill HotReload | ✅ |
| CLI: все основные команды | ✅ |
| CLI: generate skill/plugin/subagent | ✅ |
| CLI: generate channel | ❌ Нет |
| README.md | ✅ |
| AGENTS.md | ✅ |
| CI pipeline | ❌ Нет |

---

## 2. Оценка отступлений от дизайн-документа

### UnifiedExtensionRegistry — **принято решение НЕ реализовывать**

**Дизайн:** Один реестр для всех типов расширений.  
**Реальность:** Раздельные `ToolRegistry`, `SkillRegistry`, `PluginRegistry`, `SubagentRegistry`.

**Причина:** Tools, Skills, Plugins и Subagents имеют **разные протоколы** и **разные lifecycle**. Объединение в один реестр означает дженерик `Any`-интерфейс (теряем типизацию) или сложную иерархию (оверинжиниринг из v1). Текущий подход прагматичнее.

✅ **Статус:** Принятое архитектурное решение. Дизайн-документ будет обновлён.

---

### ChannelRegistry — **принято решение заменить на factory**

**Дизайн:** Каналы регистрируются через реестр.  
**Реальность:** Каналы создаются напрямую в `cli.py` и `telegram/runner.py`.

**Причина:** При 2 каналах реестр — оверинжиниринг. Правильное решение — вынести общую bootstrap-логику `_build_agent_loop()` в `agent/factory.py`, чтобы при добавлении нового канала не дублировать 100+ строк.

⚠️ **Статус:** Требует рефакторинга при добавлении 3-го канала. Пока приемлемо.

---

### Синхронный sqlite3 вместо aiosqlite — **принято решение оставить**

**Дизайн §10.1:** «Полностью асинхронный стек, SQLite (aiosqlite)».  
**Реальность:** `memory/sqlite.py` использует `sqlite3` + `anyio.to_thread.run_sync`.

**Причина:** `aiosqlite` тоже использует thread executor под капотом. Прямой `sqlite3 + anyio` даёт тот же результат с меньшей зависимостью. При переходе на PostgreSQL всё равно переписывать.

✅ **Статус:** Принятое техническое решение. `aiosqlite` удалён из deps.

---

### memory/consolidation.py отсутствует — **требует реализации**

**Дизайн:** LLM-based сжатие старых сообщений в summary.  
**Реальность:** История обрезается до `history_limit: 50`, без сжатия.

**Причина критичности:** Целевой сценарий — Qwen-7B (8K-32K токенов). 50 сообщений легко заполнят окно. Без consolidation длинные диалоги будут ломаться.

🔴 **Статус:** Реализовать до тестов с реальными локальными LLM. Механизм: при N > threshold → LLM-вызов на старые 50% → summary → заменить в истории.

---

## 3. Деплой-чеклист (из §16 дизайн-документа)

| Требование | Статус |
|-----------|--------|
| `uv run corpclaw-lite telegram` запускается | ✅ |
| ToolGuard блокирует `rm -rf` | ✅ |
| HotReload skills работает | ✅ |
| 151 тест pass, ruff clean | ✅ |
| Docker контейнер поднимается изолированно | ❌ (нет Dockerfile.agent) |
| `/health` возвращает статус | ✅ (нужен aiohttp) |
| Маркетолог + Excel через локальную LLM | ❓ Требует E2E теста |

---

## 4. Рекомендуемый путь развития

### Спринт A: До первого E2E теста (~3ч)

```
1. docker/Dockerfile.agent + seccomp_default.json  ← самое важное
2. research.yaml + document.yaml субагенты         ← 30 мин, YAML по аналогии  
3. BEHAVIOR.md + departments/*.md                  ← 30 мин, контент
4. mcp_servers.yaml дефолтный шаблон              ← 10 мин
5. Вынести _build_agent_loop() в agent/factory.py ← рефакторинг
```

**После → первый живой E2E тест:** подключить Claude/Ollama, проверить сценарий «маркетолог + Excel» через Telegram.

### Спринт B: Надёжность для локальных LLM (~3ч)

```
6. memory/consolidation.py — LLM-сжатие истории (~3ч)
7. CI pipeline: GitHub Actions (ruff + pyright + pytest) (~1ч)
8. Обновить дизайн-документ (принятые решения) (~30мин)
```

### Отложено (не блокирует MVP)

```
- VectorMemory / Qdrant — включится через config при необходимости
- UnifiedExtensionRegistry — не нужен, текущий подход лучше
- ChannelRegistry — заменяется agent/factory.py
- generate channel CLI — тривиально вручную
```

---

## 5. Итоговый прогресс

```
Фаза 1 (Ядро):              ████████████ 100%
Фаза 2 (Расширения/RBAC):   ██████████░░  90%
Фаза 3 (Безопасность):      ███████████░  95%
Фаза 4 (TG + память + лог): █████████░░░  80%
Фаза 5 (Полировка):         █████████░░░  75%

До MVP:  Спринт A (~3ч)
До prod: Спринт A + B (~6ч)
```
