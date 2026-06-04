# CorpClaw Lite - анализ идей из референсов

> Дата: 2026-05-13  
> Целевой проект: `corpclaw-lite`  
> Референсы: CoPaw, Gaia, Hermes Agent, NanoBot, NanoClaw, NemoClaw, OpenClaw, ZeroClaw, большой CorpClaw  
> Цель документа: оценить полезность предложенных заимствований и зафиксировать, что именно стоит внедрять, зачем это нужно и что лучше отложить.

---

## 1. Главный вывод

`corpclaw-lite` не нужно превращать в большой CorpClaw или в копию OpenClaw/Hermes. Его ценность в другом:

- local-first работа с моделями;
- простой ReAct loop без тяжелого LLM-планировщика;
- безопасность перед выполнением инструмента;
- департаменты как реальная runtime-политика;
- контейнер как граница изоляции;
- минимальная, понятная эксплуатация в закрытом контуре.

Поэтому идеи из референсов нужно фильтровать по одному критерию: усиливает ли идея приватность, надежность или управляемость `corpclaw-lite`, не превращая его в чрезмерно сложную платформу.

Практический вывод:

- брать точечные runtime-гарантии;
- усиливать локальные модели как production-сценарий;
- улучшать диагностику и recovery;
- не переносить большие UI, SDK, marketplace и self-modifying agent flows до появления реальной потребности.

---

## 2. Что уже есть в corpclaw-lite

Перед планированием важно не дублировать реализованные механики.

| Область | Что уже есть | Польза |
|---------|--------------|--------|
| ToolGuard | YAML-правила, severity, approval flow, fail-closed для опасных инструментов | Операционные правила безопасности можно менять без переписывания кода |
| IPC Auth | HMAC, nonce, timestamp, TTL | Контейнер не может подделать ответ или переиспользовать старое сообщение |
| Credential scrubbing | Очистка логов и результатов инструментов | Секреты не попадают обратно в LLM-контекст и пользовательские ответы |
| NetworkPolicy | Контейнерная сеть deny-all | Даже скомпрометированный tool не получает произвольный outbound |
| SSRF protection | `web_fetch` проверяет URL, DNS, private ranges и делает DNS pinning | HTTP-инструменты защищены от metadata endpoints и DNS rebinding |
| Local LLM router | Routing по task kind и subagent id | Можно использовать разные модели для main, vision, compression, calibration |
| LLM queue | Ограничение параллельных inference-запросов | Локальный GPU/llama.cpp не перегружается неконтролируемо |
| Model presets | YAML-пресеты reasoning/inference params | Модельные особенности вынесены из кода |
| Context compression | pruning tool outputs, LLM-summary, tail protection | Локальные модели с малым контекстом остаются работоспособными |
| Loop guard | Детекция повторяющейся ошибки инструмента | Агент не застревает бесконечно на одном failure mode |
| Parallel tools | `parallel_safe` и `asyncio.gather` | Несколько read-only инструментов могут выполняться быстрее |
| Subagents | Изолированный AgentLoop с filtered ToolRegistry | Основной агент разгружает контекст и не получает лишние инструменты |

Следствие: многие идеи из внешнего анализа полезны не как новые подсистемы, а как усиление уже существующих механизмов.

---

## 3. Критерии оценки заимствований

Каждая идея оценивается по пяти вопросам:

1. Увеличивает ли она приватность данных?
2. Помогает ли она работать с локальными моделями стабильнее?
3. Упрощает ли диагностику для администратора?
4. Снижает ли риск обхода department policy?
5. Не добавляет ли она больше сложности, чем пользы?

Если идея хороша для single-user agent, но ухудшает корпоративную модель доверия, она не должна переноситься напрямую.

---

## 4. P0 - внедрять в первую очередь

### 4.1 Error classifier с recovery/failover

Источник: Hermes Agent.

Идея: ошибки LLM/API/runtime классифицируются не как обычный `Exception`, а по типам:

- auth;
- billing/quota;
- rate limit;
- overloaded;
- timeout;
- context overflow;
- malformed tool call;
- model capability mismatch;
- container/IPC failure.

Для каждого типа задается стратегия:

- retry;
- wait with backoff;
- respect `Retry-After`;
- switch provider;
- compress context;
- disable streaming fallback;
- report actionable diagnostic.

Чем полезно:

- локальные endpoints часто дают неодинаковые ошибки при перегрузе, нехватке VRAM или падении backend;
- retry без понимания причины может только усугубить очередь;
- администратор получает понятную причину, а не абстрактный stack trace;
- recovery становится предсказуемым и тестируемым.

Почему P0:

`corpclaw-lite` делает ставку на локальные модели. Без структурированной обработки ошибок эксплуатация будет нестабильной: разные сбои будут выглядеть одинаково и лечиться одинаковым retry.

Минимальный объем:

- добавить модуль `llm/errors.py` или `runtime/errors.py`;
- нормализовать provider/container/streaming errors в `ErrorKind`;
- добавить policy `ErrorKind -> RecoveryAction`;
- покрыть unit-тестами основные сценарии.

---

### 4.2 Расширенный tool loop detection

Источники: OpenClaw, текущий `SimpleProgressGuard`.

Сейчас есть полезная, но узкая защита: повтор одной и той же ошибки одного инструмента.

Что добавить:

- generic repeat: один и тот же tool call с одинаковыми аргументами несколько раз;
- ping-pong: чередование двух инструментов без изменения результата;
- poll-no-progress: повторный polling, где состояние не меняется;
- global circuit breaker: слишком много неуспешных инструментов за один run;
- no-new-information detection: инструмент успешен, но возвращает тот же hash результата.

Чем полезно:

- не требует дополнительного LLM-вызова;
- защищает бюджет локальной модели;
- снижает риск бесконечной Telegram-сессии;
- делает failure mode объяснимым: "остановлено из-за повторяющихся действий".

Почему P0:

Локальные модели чаще ошибаются в tool calling и могут повторять одну стратегию. Дешевая детерминированная защита даст большой эффект без усложнения архитектуры.

Минимальный объем:

- расширить `SimpleProgressGuard`;
- хранить последние N tool signatures;
- использовать hash нормализованных аргументов и результата;
- добавить trace events для loop stop reason.

---

### 4.3 Compaction с сохранением runtime-идентификаторов

Источник: OpenClaw.

Идея: LLM-summary должен явно сохранять идентификаторы, которые важны для продолжения работы:

- file paths;
- sheet names;
- task ids;
- tool_call ids, если они используются в debug/audit;
- UUID;
- IP, ports, URLs;
- hashes;
- container/session ids;
- имена созданных артефактов.

Чем полезно:

- после сжатия агент не теряет рабочее состояние;
- уменьшается число "я создал файл, но потом забыл его имя";
- проще отлаживать длинные задачи с документами, таблицами и скриптами;
- не требует новой подсистемы, только улучшения prompt и тестов.

Почему P0:

Компрессия уже есть и будет часто использоваться с локальными моделями. Если summary теряет идентификаторы, пользователь видит странные ошибки на поздних шагах.

Минимальный объем:

- усилить prompt в `ContextCompressor._generate_summary`;
- добавить отдельный блок "Preserve verbatim";
- сделать тест: summary сохраняет paths, sheet names, ids, ports;
- при возможности хранить structured metadata отдельно от LLM-summary.

---

### 4.4 Subagent depth limits и restricted inheritance

Источники: Hermes Agent, OpenClaw, NanoBot.

Идея: субагент не должен автоматически наследовать все возможности родителя и не должен бесконечно создавать других субагентов.

Что добавить:

- `max_depth` для dispatch;
- `current_depth` в execution context;
- запрет `dispatch_subagent` внутри leaf-субагента по умолчанию;
- denylist наследования: memory write, send_file, exec_script, dispatch_subagent;
- явное разрешение nested subagents только в YAML;
- audit event при blocked nested dispatch.

Чем полезно:

- предотвращает рекурсивное делегирование;
- сохраняет department boundaries;
- снижает риск privilege expansion через субагента;
- делает выполнение предсказуемым для администратора.

Почему P0:

Субагенты уже есть. Чем больше они будут использоваться, тем важнее ограничить глубину и наследование прав до появления сложных сценариев.

Минимальный объем:

- расширить `SubagentSpec` полями `max_depth`, `allow_nested`, `blocked_tools`;
- передавать execution depth в `SubagentDispatcher`;
- не регистрировать blocked tools в isolated registry;
- добавить тесты на nested dispatch denial.

---

### 4.5 Credential scrubbing audit и расширение паттернов

Источники: ZeroClaw, NemoClaw, текущий `CredentialScrubber`.

В `corpclaw-lite` уже есть очистка результатов инструментов. Ее нужно не переписывать, а усилить.

Что добавить:

- больше паттернов: GitHub PAT, GitLab token, Telegram bot token, JWT, AWS, Google, OpenAI/Anthropic-style keys, private keys, database URLs, bearer URLs;
- тесты на tool output, logs, exception messages;
- redaction для structured dict/list output, если инструменты начнут возвращать не только строки;
- trace event "secret redacted", без записи самого секрета;
- fail-closed режим для записи секретов в memory/skills/config.

Чем полезно:

- секреты не становятся частью долговременной памяти;
- секреты не попадают в LLM context;
- можно безопаснее читать `.env`, logs и конфиги в workspace;
- повышается доверие к agent output в закрытом контуре.

Почему P0:

Приватность - центральное требование проекта. Один пропущенный token в memory может жить дольше, чем исходный файл.

---

## 5. P1 - внедрять после P0

### 5.1 Provider fallback chains

Источники: ZeroClaw RouterProvider/ReliableProvider, Hermes provider UX.

Идея: маршрутизация должна поддерживать цепочку:

1. primary local provider;
2. backup local provider;
3. optional cloud provider только если policy позволяет;
4. graceful failure, если fallback запрещен.

Чем полезно:

- локальный endpoint может быть занят, перегружен или выключен;
- критичные задачи не обязаны падать сразу;
- можно разделить privacy-sensitive задачи и задачи, где разрешен cloud fallback;
- администратор видит, какой provider реально использовался.

Важное ограничение:

fallback в облако должен быть запрещен по умолчанию и требовать явной политики департамента или задачи. Иначе нарушается local-first приватность.

Минимальный объем:

- добавить `fallbacks` в routing rule;
- добавить `allow_cloud_fallback` в policy/departments;
- логировать fallback reason;
- не делать автоматический fallback для файловых/персональных данных без разрешения.

---

### 5.2 Tool name aliasing с безопасной canonicalization

Источник: Hermes Agent.

Идея: локальные модели иногда называют инструменты не тем именем:

- `bash`, `sh`, `cmd` вместо `exec_script`;
- `readfile`, `file_read` вместо `read_file`;
- `sendfile` вместо `send_file`.

Чем полезно:

- повышает совместимость с локальными LLM;
- снижает число repair turns;
- делает tool calling более устойчивым после calibration.

Критичное условие безопасности:

alias должен нормализоваться до canonical tool name до `PermissionChecker` и `ToolGuard`. Нельзя выполнять alias напрямую, иначе можно обойти policy.

Минимальный объем:

- YAML `config/tool_aliases.yaml`;
- `canonicalize_tool_name(raw_name) -> CanonicalToolName`;
- audit original name + canonical name;
- reject ambiguous aliases.

---

### 5.3 Shell evasion detection

Источник: CoPaw.

Идея: ToolGuard должен ловить не только очевидные команды, но и попытки обхода:

- base64 decode + exec;
- `curl | sh`;
- Python `os.system`, `subprocess`, `shutil.rmtree`;
- chmod/chown dangerous patterns;
- writing script to `/tmp` then executing;
- encoded payloads;
- use of shell metacharacters to hide destructive actions.

Чем полезно:

- локальная модель может сгенерировать dangerous shell не злонамеренно, а по привычке;
- пользователь может случайно попросить рискованную операцию;
- контейнер снижает blast radius, но не заменяет pre-execution policy;
- audit становится конкретнее: blocked because of evasion pattern.

Минимальный объем:

- расширить `config/tool_guard_rules.yaml`;
- добавить специализированный scanner для `exec_script`;
- тесты на false positive и false negative patterns;
- разные реакции: block, require approval, log.

---

### 5.4 ToolLoader bundles

Источник: Gaia.

Идея: инструменты делятся на activation bundles:

- `always`: доступны всегда;
- `session`: активируются после первого использования или команды;
- `keyword`: появляются в prompt по regex/semantic match;
- `subagent_only`: доступны только специализированным агентам.

Чем полезно:

- меньше prompt bloat;
- локальной модели проще выбирать правильный инструмент;
- отделяет "имеет право" от "стоит показывать сейчас";
- сохраняет department policy как верхний слой, а bundle system работает как оптимизация контекста.

Важное ограничение:

bundle activation не должен давать больше прав, чем department policy. Сначала RBAC, потом bundle filtering.

Минимальный объем:

- добавить `activation` в Tool metadata;
- сделать фильтр перед `to_schemas_for_user`;
- добавить warm window на сессию;
- audit "tool omitted from prompt because inactive".

---

### 5.5 StreamingContextScrubber

Источник: Hermes Agent.

Идея: если в system/context используются служебные блоки вроде `<memory-context>`, streaming output должен уметь вырезать их даже на chunk boundaries.

Чем полезно:

- память и служебные инструкции не утекают пользователю;
- streaming остается безопасным;
- особенно важно для локальных моделей, которые иногда копируют system/context в ответ.

Минимальный объем:

- state machine поверх stream chunks;
- список protected tags;
- тесты, где тег разорван между chunk;
- метрика "context block redacted from stream".

---

## 6. P2 - полезно, но не срочно

### 6.1 Component supervisor

Источник: ZeroClaw.

Идея: важные фоновые компоненты запускаются как supervised tasks:

- Telegram polling;
- health server;
- MCP hot reload;
- plugin watcher;
- scheduler;
- LLM queue maintenance;
- container cleanup.

Чем полезно:

- один упавший watcher не останавливает весь процесс;
- restart с exponential backoff;
- виден статус компонентов;
- проще production-эксплуатация.

Почему P2:

Полезно для надежности, но имеет смысл после стабилизации security и provider recovery.

---

### 6.2 Provider profiles

Источники: Hermes Agent, CoPaw.

Идея: провайдер описывается декларативно:

- identity;
- auth type;
- base URL;
- model catalog;
- capabilities;
- quirks;
- default params;
- health check.

Чем полезно:

- добавление нового локального endpoint не требует Python-кода;
- `doctor` CLI может проверять весь профиль;
- model routing становится понятнее для администратора;
- меньше hardcoded provider logic.

Ограничение:

не нужно сразу делать 29 providers как Hermes. Для `corpclaw-lite` достаточно OpenAI-compatible, Anthropic, llama.cpp/LM Studio/Ollama profiles.

---

### 6.3 Hybrid memory v1 на SQLite FTS5

Источники: Gaia, ZeroClaw.

Идея: память ищется не только по простому списку фактов, а через:

- SQLite FTS5/BM25;
- recency boost;
- optional embeddings позже;
- простое RRF только если появятся два независимых источника поиска.

Чем полезно:

- лучше recall по старым фактам;
- без внешнего Qdrant/FAISS на первом этапе;
- остается compatible с закрытым контуром;
- можно задавать лимит фактов по сложности запроса.

Почему не P0:

Сначала нужно защитить память от утечек секретов. Улучшать поиск до secret hygiene опасно.

---

### 6.4 Basic e-stop

Источник: ZeroClaw.

Идея: аварийная остановка не просто выключает процесс, а замораживает отдельные поверхности:

- freeze tools;
- freeze network;
- freeze subagents;
- stop all active runs;
- require admin resume.

Чем полезно:

- если обнаружена утечка или неправильная политика, можно быстро остановить опасную часть;
- администратор не обязан убивать весь сервер;
- audit фиксирует кто и почему включил e-stop.

Почему P2:

Нужно после появления component supervisor или хотя бы централизованного runtime state.

---

### 6.5 Supply-chain scan для skills/plugins

Источники: OpenClaw, Hermes Agent.

Идея: skill/plugin перед активацией проверяется на подозрительные признаки:

- env harvesting;
- file read + network send;
- obfuscated base64;
- subprocess usage;
- unpinned dependencies;
- symlink/hardlink escape;
- world-writable paths.

Чем полезно:

- skills становятся безопасной процедурной памятью;
- плагины не получают trust автоматически;
- можно разрешить user-provided extensions без полного доверия к ним.

Почему P2:

Сначала нужно стабилизировать core security. Scanner становится особенно важен, когда появится внешний skill/plugin exchange.

---

## 7. P3 - отложить до реальной потребности

### 7.1 GAIA mixin architecture

Идея: миксины автоматически добавляют части system prompt через reflection.

Оценка для `corpclaw-lite`: не рекомендуется сейчас.

Почему:

- порядок prompt sections становится неочевидным;
- security/RBAC сложнее проверять;
- reflection скрывает dependencies;
- lite-проекту лучше иметь явную сборку prompt с приоритетами.

Когда вернуться:

если появится много независимых prompt modules и явный section assembler станет громоздким.

---

### 7.2 Dream two-phase consolidation

Источник: NanoBot.

Идея: агент анализирует историю, затем сам редактирует `MEMORY.md`, `SOUL.md`, `USER.md` и создает skills.

Оценка: идея сильная, прямой перенос опасен.

Чем полезно:

- память становится процедурной, а не просто списком фактов;
- агент может улучшать собственные инструкции;
- можно автоматически создавать reusable skills.

Почему отложить:

- self-modification требует approval, provenance, rollback;
- есть риск закрепить ошибочные выводы в памяти;
- есть риск создать skill, который расширяет возможности за пределы department policy.

Безопасный вариант:

сначала proposal-only режим: агент предлагает изменения, человек утверждает, система пишет audit и сохраняет rollback snapshot.

---

### 7.3 NemoClaw shields lockdown через OS immutability

Идея: `locked` состояние через root ownership, immutable flags, auto-restore.

Оценка: полезно для enterprise-hardening, но тяжелое для первой реализации.

Чем полезно:

- конфигурация агента физически защищена от записи;
- временное unlock фиксируется и автоматически откатывается;
- подходит для regulated environments.

Почему отложить:

- платформенно зависит от Linux features;
- усложняет установку;
- сложно тестировать на macOS/Windows/dev environments.

Первый шаг:

логический policy lockdown: запрет изменения config/skills/plugins через tools, file permissions, audit, admin-only unlock с TTL.

---

### 7.4 Multi-format tool parser на 8+ форматов

Источники: Hermes, ZeroClaw.

Оценка: расширять осторожно.

Чем полезно:

- повышает совместимость с локальными моделями;
- снижает количество malformed tool calls.

Риск:

- чем больше форматов, тем выше шанс принять обычный текст за tool call;
- больше поверхность prompt injection;
- сложнее объяснить, почему вызвался инструмент.

Рекомендация:

оставить native + XML/Qwen formats как основной путь. Добавлять новые форматы только после calibration evidence для конкретной модели.

---

### 7.5 Большой Plugin SDK

Источник: OpenClaw.

Оценка: не нужен сейчас.

Почему:

- противоречит lite-подходу;
- требует API stability, lint rules, SDK docs, backward compatibility;
- увеличивает surface для supply-chain атак.

Лучший путь:

маленький manifest-based API, whitelist, scanner, explicit capabilities, stable contract tests.

---

## 8. Оценка предложений из внешнего анализа

| Предложение | Оценка | Решение |
|-------------|--------|---------|
| Mixin architecture | Полезно как идея modular prompt, но не для текущей архитектуры | Отложить |
| Табличная state machine turn lifecycle | Полезно для читаемости, но loop уже понятен | Рассмотреть после P0 |
| Iteration budget с refund | Частично уже есть pause/resume для queue wait | Не срочно |
| Error classifier | Очень полезно | P0 |
| Hybrid Vector + BM25 + RRF | Полезно позже, начать с SQLite FTS5 | P2 |
| Dream consolidation | Сильно, но опасно | P3/proposal-only |
| StreamingContextScrubber | Полезно для streaming privacy | P1 |
| CoPaw shell evasion | Полезно | P1 |
| NemoClaw shields | Полезно, но тяжело | P3 |
| SSRF DNS pinning | Уже есть для web_fetch | Поддерживать и покрывать тестами |
| OpenClaw loop detection | Полезно как усиление текущего guard | P0 |
| Credential scrubbing in tool output | Уже есть | Усилить тестами и паттернами |
| E-stop | Полезно для production | P2 |
| Dual context management | Уже есть pruning + compression | Усилить ID preservation |
| Prompt caching | Полезно только для Anthropic/cloud | Отложить, если local-first |
| ToolLoader bundles | Полезно | P1 |
| Concurrent tool execution | Уже есть | Улучшать metadata `parallel_safe` |
| Tool aliases | Полезно, но нужно canonicalize до policy | P1 |
| Multi-format parser | Рискованно | P3, evidence-based |
| Everything-is-a-message | Хороший принцип аудита, но SQLite IPC переносить не надо | Использовать как design principle |
| Delegate depth limits | Полезно | P0 |
| Provider profiles | Полезно | P2 |
| RouterProvider/ReliableProvider | Полезно | P1 |
| Auxiliary clients | Частично есть через task routing | Расширять по мере задач |
| Component supervisor | Полезно | P2 |
| Supply-chain audit | Полезно | P2 |
| OpenClaw-scale Plugin SDK | Слишком тяжело | P3/не делать сейчас |

---

## 9. Рекомендуемый backlog

### P0 - security и runtime reliability

1. Error classifier + recovery policy.
2. Stronger loop detection.
3. Compression prompt preserves identifiers.
4. Subagent depth limits and blocked tool inheritance.
5. Credential scrubbing audit tests and expanded patterns.

### P1 - local LLM и tool stability

1. Provider fallback chains with privacy policy.
2. Safe tool alias canonicalization.
3. Shell evasion scanner.
4. ToolLoader bundles.
5. StreamingContextScrubber.

### P2 - production operations

1. Component supervisor.
2. Provider profiles and doctor integration.
3. SQLite FTS5 memory search.
4. Basic e-stop.
5. Supply-chain scan for skills/plugins.

### P3 - advanced platform features

1. Dream consolidation in proposal-only mode.
2. Logical shields, then OS-level lockdown only for enterprise deployments.
3. Additional tool call formats only after calibration evidence.
4. Larger plugin SDK only after external plugin consumers appear.

---

## 10. Что не должно измениться

При внедрении улучшений важно сохранить базовые свойства `corpclaw-lite`:

- не добавлять LLM planner как обязательный слой;
- не отдавать агенту прямой доступ к секретам;
- не делать cloud fallback без explicit policy;
- не расширять права через subagent inheritance;
- не включать внешние plugins/skills без scanner и whitelist;
- не заменять простую YAML-конфигурацию тяжелым SDK;
- не превращать Telegram/CLI runtime в desktop/mobile платформу раньше времени.

---

## 11. Итоговая стратегия

`corpclaw-lite` уже перенес правильные идеи: ToolGuard, deny-all container network, local model presets, queue, XML fallback, subagents, compression, RBAC по департаментам.

Следующий этап зрелости:

1. Лучше понимать сбои.
2. Лучше останавливать циклы.
3. Лучше сохранять рабочее состояние при compression.
4. Жестче ограничивать субагентов.
5. Сильнее защищать секреты.
6. Сделать provider fallback и tool aliases безопасными, а не просто удобными.

Такой путь сохраняет lite-архитектуру и добавляет зрелость там, где она действительно нужна: приватность, надежность, контроль прав и эксплуатация локальных моделей.
