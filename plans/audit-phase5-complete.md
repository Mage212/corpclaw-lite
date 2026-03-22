# Аудит проекта CorpClaw Lite — после Phase 5

**Дата:** 23 марта 2026
**Статус сборки:** 81 тест ✅ | pyright 0 ошибок | ruff 0 ошибок | coverage 78%

---

## 1. Общая статистика кодовой базы

- **src/ Python LOC:** ~4,274 строк (56 файлов)
- **tests/ Python LOC:** ~1,977 строк (20 файлов)
- **Config YAML:** 4 файла
- **Bootstrap промпты:** 2 файла (SOUL.md, COMPANY.md)
- **Примеры скилов:** 5 файлов

---

## 2. Реализовано согласно дизайн-документу

| Компонент | Дизайн (§) | LOC | Соответствие |
|-----------|------------|-----|--------------|
| Simple ReAct Loop | §1.1 (~400 строк) | 164 | ✅ Компактнее плана |
| SimpleBudgetGuard + ProgressGuard | §1.1 | 158 | ✅ Перенесено из v1 |
| LLM провайдеры (Anthropic, OpenAI) | §5 | 203 | ✅ |
| xml_tool_calling.py | §5 | 129 | ✅ Перенесено из v1 |
| ProviderRouter | §5 | 29 | ✅ Lookup-таблица |
| Tool (5 атрибутов) | §12 | 40 | ✅ |
| Skill (6 атрибутов) | §12 | 23 | ✅ |
| ToolGuard (CoPaw pattern) | §6.1 | 133 | ✅ Phase 5: full evaluation |
| PermissionChecker (RBAC, 6 методов) | §4.2 | 60 | ✅ |
| SubagentDispatcher + DispatchSubagentTool | §1.2 | 78 + 58 | ✅ Phase 5 fix |
| VisionProcessor | §1.3 | 40 | ⚠️ Skeleton |
| Channel Protocol | §7.1 | 32 | ✅ |
| Telegram (inline approval кнопки) | §7.2 | 155 + 193 | ✅ |
| IPC Auth (HMAC + nonce) | §6.3 | 83 | ✅ Mandatory, fail-fast |
| NetworkPolicy | §6.2 | 49 | ✅ Упрощённый |
| CredentialScrubber | §6 | 50 | ✅ |
| BootstrapLoader | §3.3 | 76 | ✅ mtime кэш |
| AgentLogger (JSONL + RotatingFileHandler) | §11 | 90 | ✅ |
| MCP integration | §2 | 308 | ✅ client + manager + adapter |
| Skills HotReload | §3.2 | 75 | ✅ Async polling |
| Docker container | §8 | 318 | ⚠️ Skeleton |
| CLI (typer) | §14 | 346 | ✅ Все команды |
| Skills (5 примеров) | §2 | — | ✅ |
| Plugin system | §3.1, §12 | 197 | ✅ Manifest loader |
| SQLite Memory | §9.1 | 97 | ✅ |
| DepartmentManager | §4.1 | 57 | ✅ |
| Config (Pydantic + YAML + env) | §2 | 111 | ✅ |
| UserManager | §2 | 96 | ✅ |

---

## 3. Расхождения с дизайн-документом

### 3.1 Нереализованные компоненты

| Компонент | Секция | Критичность | Комментарий |
|-----------|--------|-------------|-------------|
| Memory Consolidation (LLM-based) | §9.1 | LOW | Опциональная оптимизация, файл не создан |
| VectorMemory (Qdrant) | §9.2 | LOW | Stub 26 строк, TODO. Дизайн помечает "опционально" |
| UnifiedExtensionRegistry | §3.1 | LOW | Осознанный выбор простоты, каждый тип — свой registry |
| ExtensionWatcher (единый) | §3.2 | MEDIUM | Только SkillHotReloader; плагины/субагенты без hot-reload |
| Channel Registry | §7 | LOW | 2 канала, registry избыточен |
| prompts.py | §2 | LOW | Логика разнесена между context.py и bootstrap.py |
| db/database.py | §2 | LOW | SQLite напрямую в memory/sqlite.py |
| Builtin: web.py (web_fetch, web_search) | §2, §4.1 | **HIGH** | Дизайн предусматривает, RBAC ссылается. Нет файла |
| Builtin: memory.py (memory_store, memory_recall) | §2 | MEDIUM | Дизайн предусматривает. Нет файла |
| Builtin: send_file.py | §2 | **HIGH** | Критичен для метрики успеха ("файл обратно") |
| Builtin: profile.py (user_profile) | §2 | LOW | |
| Builtin subagents: research.yaml, document.yaml | §2 | LOW | Только filesystem.yaml + execution.yaml |
| Telegram subdirectory (formatter, router, approval_ui) | §7 | LOW | Монолит в telegram_channel.py — работает |
| Пример плагина | §2 | LOW | plugins/ директория пуста |
| Health HTTP endpoint | §11 | MEDIUM | health.py есть, HTTP сервера нет |
| CI pipeline | §14 Фаза 5 | MEDIUM | Не реализован |

### 3.2 Skeleton-реализации (требуют доработки)

**VisionProcessor** (`agent/vision.py:29`):
- f-string логирование (нарушает конвенцию)
- Отправляет текст `[Attached Image: {path.name}]` вместо base64
- Дизайн (§1.3) описывает как **критическую функцию** для локальных LLM

**ContainerManager / agent_worker.py** (`container/agent_worker.py:37`):
- `f"Mock execution of {tool_name}"` — не выполняет реальные инструменты
- Блокирует пункт деплой-чеклиста: "Docker контейнер выполняет инструменты изолированно"

---

## 4. Заимствованные паттерны из референсных проектов

### 4.1 CoPaw (Alibaba) — Tool Guard

| Паттерн | CoPaw | CorpClaw Lite | Статус |
|---------|-------|---------------|--------|
| YAML rules с severity | ✅ | ✅ | **Adopted** |
| Regex pattern matching | ✅ | ✅ | **Adopted** |
| Inline approve/deny | ✅ | ✅ (Telegram кнопки) | **Adopted** |
| Full evaluation (не first-match) | ✅ | ✅ (Phase 5 fix) | **Adopted** |
| `exclude_patterns` | ✅ | ❌ | Not adopted |
| `remediation` в выводе ошибки | ✅ | ❌ | Not adopted |
| `category` группировка правил | ✅ | ❌ | Not adopted |
| Mixin pattern | ✅ | ❌ (прямая интеграция в loop.py) | Отклонение — проще |

### 4.2 NemoClaw (NVIDIA) — Security Overlay

| Паттерн | NemoClaw (4 слоя) | CorpClaw Lite | Статус |
|---------|-------------------|---------------|--------|
| Network deny-by-default + allowlist | ✅ | ✅ (упрощённый) | **Adopted** (1/4 слоёв) |
| Per-endpoint enforcement (method/path) | ✅ | ❌ | Not adopted |
| Filesystem Policy (landlock) | ✅ | ❌ | Not adopted |
| Process Security (run_as_user) | ✅ | ❌ | Not adopted |
| TLS enforcement | ✅ | ❌ | Not adopted |

**Вывод:** Из 4 слоёв NemoClaw реализован только Network (и тот упрощённый). Осознанный trade-off — полная реализация избыточна для текущего масштаба.

### 4.3 OpenClaw — Session Loop

| Паттерн | OpenClaw | CorpClaw Lite | Статус |
|---------|----------|---------------|--------|
| Graceful restart + drain phase | ✅ | ❌ | Not adopted |
| Lock-based singleton | ✅ | ❌ | Not adopted |
| File-based approval queue | ✅ | ❌ (Future-based) | Отклонение — своё решение |

---

## 5. Соответствие фазовому плану (§14 дизайна)

| Фаза | Описание | Статус | Детали |
|------|----------|--------|--------|
| **Фаза 1** | Ядро: LLM, Tool, AgentLoop, CLI, builtin tools | ✅ 100% | |
| **Фаза 2** | Расширения: Skills, RBAC, Plugins, Subagents, Router, Vision | ✅ ~95% | Vision — skeleton |
| **Фаза 3** | Безопасность: ToolGuard, Network, IPC, Container, Scrubber | ✅ ~90% | Container — skeleton |
| **Фаза 4** | Telegram + память: Channel, SQLite, consolidation, Qdrant, логи | ⚠️ ~70% | Нет consolidation, Qdrant stub, нет /health HTTP |
| **Фаза 5** | Полировка: MCP, HotReload, Bootstrap, CLI, README, CI | ⚠️ ~60% | MCP ✅, Bootstrap ✅, CLI ✅, нет unified watcher, нет CI |

**Примечание:** MCP, Bootstrap, CLI, HotReload реализованы в Phase 3 коммитах. Phase 4-5 в git-истории — это bugfix раунды, а не фазы из дизайна.

---

## 6. Чеклист деплоя (§16 дизайна)

| # | Пункт | Статус |
|---|-------|--------|
| 1 | `uv run corpclaw-lite telegram` запускается и отвечает | ⚠️ Не проверено в runtime |
| 2 | Маркетолог: «нормализуй Excel» → файл обратно | ❌ Нет send_file tool, нет normalize_excel tool |
| 3 | pytest ≥75% coverage, 0 failures | ✅ 81 passed, 78% coverage |
| 4 | pyright 0 errors (strict) | ✅ |
| 5 | ruff 0 errors | ✅ |
| 6 | Docker контейнер выполняет инструменты изолированно | ❌ Mock execution |
| 7 | ToolGuard блокирует `rm -rf` через exec_script | ✅ CRITICAL rule в YAML |
| 8 | HotReload skills без перезапуска | ✅ SkillHotReloader |
| 9 | IPC требует CORPCLAW_IPC_SECRET | ✅ |
| 10 | /health возвращает метрики | ❌ Нет HTTP сервера |

**Готовность к деплою: 6/10 пунктов**

---

## 7. Рекомендации

### Для минимального рабочего деплоя (приоритет HIGH)

1. **Builtin tools: web_fetch, memory_store, memory_recall, send_file** — без них агент не может выполнять основные задачи из дизайна. RBAC в departments.yaml ссылается на эти инструменты
2. **VisionProcessor** — реальный base64 encoding для изображений (критично для локальных LLM, §1.3)
3. **Container agent_worker** — реальное выполнение инструментов (не mock)
4. **Health HTTP endpoint** — для мониторинга в production

### Можно отложить (приоритет LOW)

- UnifiedExtensionRegistry / unified watcher — усложнение без текущей ценности
- Memory consolidation / Qdrant — оптимизации для масштаба
- CI pipeline — ручной запуск проверок работает
- Telegram formatter/router/approval_ui разделение — монолит адекватен
- Дополнительные субагенты (research.yaml, document.yaml) — добавятся по необходимости
- CoPaw exclude_patterns / remediation — nice-to-have
- NemoClaw полный 4-слойный security — избыточен для текущего масштаба

---

## 8. Git-история фаз

```
a4edd5a fix: Phase 5 — 7 confirmed code review issues
2f098b9 fix: Phase 4 — code review bug fixes (18 confirmed issues)
2932cc7 feat: Phase 3 Block L — Deploy checklist complete, coverage 78%
fb0f8cf feat: Phase 3 Block K — Skill files + departments.yaml
979552d feat: Phase 3 Block J — MCP Integration (client, adapter, manager)
1ee3895 feat: Phase 3 Block I — Subagent prompt loading from prompt_path
7535513 feat: Phase 3 Block H — Telegram approval Future-routing + AgentLoop wiring
306d92e fix: Phase 3 Block G — pyright strict mode, ruff clean (0 errors)
87f940d feat: Phase 3 Block F — Logging, HotReload, CLI, Bootstrap
290fe4f feat: Phase 2 complete — Channels, Memory, Security, Docker, Extensions
c40c0f0 Initial commit for CorpClaw Lite
```
