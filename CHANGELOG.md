# Changelog

Все заметные изменения проекта CorpClaw Lite документируются в этом файле.

Формат основан на [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/).

## [0.2.0] — 2026-06-24

**Major bump** `0.1.13 → 0.2.0`. Фокус версии — **production-ready управление
провайдерами, моделями и параметрами генерации** (D-056). Редизайн устраняет три
workaround'а, выявленных в eval-сессиях: заморозка пресета в инстансе провайдера,
route-contamination (gemma4 agent тихо маршрутизировал vision → qwen), и
rename/restore `model_presets.yaml` с crash-window hazard.

Пресет расщеплён на ортогональные слои: `ModelProfile` (свойства модели) +
`SamplingProfile` (свойства задачи/фазы). Per-call override поднят через второй
независимый contextvar (`RequestOptions`). `PhasePolicy` переключает thinking по
фазе задачи (closing mode off; research gathering off / aggregation on;
auxiliary off через config). `LLMRouter.with_overrides()` — программный atomic
override всех agent-роутов, устраняет контаминацию как класс.

### Breaking changes

- **`config/model_presets.yaml`**: legacy комбинированный формат `presets:` →
  split `models:` + `sampling:`. Back-compat reader в ядре парсит legacy
  `presets:` и split'ит в виртуальные (ModelProfile, SamplingProfile) пары по
  тому же имени — overlay-репо и unmigrated config продолжают работать без
  правок. Миграция рекомендуемая, не обязательная.
- **`RoutingRule.preset`** → deprecated alias. Рекомендуется `sampling:`
  (split-формат). Legacy `preset:` работает через back-compat reader; при
  заданных обоих `sampling` выигрывает.
- **`ModelPreset`** тип deprecated (alias + bridge `profile_from_legacy_preset`).
  Новый код должен использовать `ModelProfile` + `SamplingProfile`.
- `requires_core` plugins: bump до `^0.2.0` (плагины с `^0.1.x` получат
  warn-and-skip при загрузке, advisory — не fatal).

### Added

- **RequestOptions** (`llm/base.py`) — per-call contextvar (второй независимый
  рельс рядом с `BackendRequestOptions`). Несёт per-call `inference` +
  `thinking` override (`ThinkingOverride`: `default`/`off`/`budget`). Provider
  мерджит оба с детерминированным приоритетом. Протокол `Provider` не меняется —
  override через contextvar, не через параметры `chat()`.
- **Расщепление `ModelPreset` → `ModelProfile` + `SamplingProfile`**
  (`llm/presets.py`). `ModelProfile`: `thinking_parser`, `system_prompt_prefix`,
  `default_inference` (свойства модели). `SamplingProfile`: `thinking_mode`,
  `thinking_budget`, `inference_overrides`, ссылка на `ModelProfile` (свойства
  задачи/фазы). Дубликаты пресетов схлопнуты (`gemma4-thinking`/`gemma4-fast` →
  один `gemma4-26b-qat` profile + два sampling). YAML-bool coercion
  (`thinking_mode: off` без кавычек → валидируется).
- **`PhasePolicy`** (`agent/phase_policy.py`) — детектор фазы задачи, per-call
  переключает thinking через `RequestOptions`. `DefaultPhasePolicy` enabled by
  default, но **no-op для main agent в default phase** → меняет behavior только
  в closing_mode (off) и для workflow subagent (gathering off / aggregation on,
  semantic primary: prev turn = `research_list_facts` → on; wall-clock fallback:
  `nudge`/`restrict` → on). `PhasePolicySettings` в `AgentSettings`
  (`enabled`, `aggregation_markers`, `gathering_tools`, per-phase thinking).
- **`LLMRouter.with_overrides()`** (`llm/router.py`) — программный atomic override
  agent-facing роутов. Возвращает новый router с переопределёнными
  sampling/thinking/model in-memory. `apply_to="all_agent_routes"` перестраивает
  все 4 agent-роута сразу → контаминация устранена. `queue`/`cache_manager`
  шарятся, `provider_meta` пересоздаётся с distinct profile_label.
- **`build_agent_stack(settings, *, router_override=None)`** (`agent/factory.py`)
  — инъекция готового router (eval/custom callers).
- **`aux-no-thinking` sampling profile** + auxiliary routes (vision/compress/
  consolidate) → thinking off через config, без PhasePolicy.

### Changed

- **`RoutingRule`** (`config/settings.py`): новые поля `model_profile`, `sampling`
  (предпочтительно), legacy `preset` deprecated. `sampling` выигрывает над
  `preset`.
- **`OpenAIProvider`/`AnthropicProvider`**: `_apply_preset` → `_apply_model_profile`
  + `_apply_sampling` + `_apply_request_options` с merge priority: model_profile
  defaults < sampling overrides < RequestOptions (per-call) < backend extra_body.
  `chat_with_image` почищен от inline preset copy.
- **`config/model_presets.yaml`** мигрирован к `models:`/`sampling:` структуре.
- **`config/settings.yaml`**: routing → `sampling:` на каждом rule;
  `agent.phase_policy` блок (enabled default, markers, per-phase thinking).
- **`calibration/loop.py`**: двух-путная resolution с dead inner loop
  линеаризована (`has_task_route` gate; unreachable model-harvesting loop удалён).

### Removed

- `scripts/eval_live.py` (gitignored, локный): YAML-mutation + tempfile +
  rename/restore `model_presets.yaml` workaround → заменён на `with_overrides()`
  через wrapper `build_agent_stack` (266→174 строк, crash-window hazard устранён).
- Дубликаты пресетов `gemma4-thinking`/`gemma4-fast` (схлопнуты).

### Fixed (post-validation, найдены живым прогоном gemma4-26b-qat перед тегом)

- **Judge route contamination.** Canonical `config/settings.yaml` имел `eval`
  route закомментированным → `_resolve_judge` падал на `default` (агент-модель) →
  судья скорил ответы на той же модели что оценивал. Раскомментирован
  `eval` → cloud/glm-5.2 (safe: `from_settings` skip+warn если cloud не настроен).
- **gemma4 config wrong parser.** `config/model_presets.yaml` декларировал
  `gemma4-26b-qat` с `thinking_parser: source: content` + `<|think|>` prefix,
  но proxy отдаёт reasoning в native `reasoning_content` (Qwen-style), не в
  content-tags. Config исправлен на `source: native`, prefix убран.
- **Thinking-off не подавлял prefix-based thinking.** `thinking_mode=off` ставил
  только `chat_template_kwargs.enable_thinking=False` (Qwen-механизм). Для
  prefix-based моделей (gemma4 `<|think|>`) prefix — это и есть переключатель
  thinking, и он оставался активным → модель продолжала reasoning (89% ходов).
  Добавлен `_thinking_disabled()` helper; `_apply_model_profile` подавляет
  `system_prompt_prefix` при thinking-off (sampling или per-call RequestOptions).
  После фикса: **gemma4+off — reasoning=0 на 100% ходов** (валидировано live).
- **PhasePolicy research timing (logic inversion).** Aggregation-фаза
  детектировалась по prev_tool_calls только → turn с `research_list_facts`
  шёл в gathering (thinking off), а `research_finalize` получал thinking-on
  только если list_facts был в immediately-previous turn (часто пропускалось).
  Фикс: gathering→aggregation переход **monotonic** — `PhaseContext` несёт
  cumulative `tools_used`; как только aggregation marker (`research_list_facts`)
  появляется в cumulative, все последующие turns = aggregation.
- **Aggregation не включала thinking.** `aggregation_thinking="default"`
  производил no-op (RequestOptions=None) → на gemma4+off run финальный synthesis
  шёл без reasoning (модель не обдумывала собранные факты перед отчётом). Фикс:
  `_thinking_options("default")` возвращает RequestOptions (force-on);
  `_apply_request_options` для `mode=default` ставит `enable_thinking=True`,
  отменяя sampling-off. Валидировано: reasoning=2269 в aggregation-turn после
  `research_list_facts`.

### Added (post-validation)

- **+2 research eval-сценария** (`config/eval_scenarios.yaml`, 28 total):
  `research_basic_fact_lookup`, `research_comparison_synthesis`. PhasePolicy
  research gathering/aggregation — главный use-case D-056 — ранее не
  покрывался eval-корпусом. Теперь покрывается.

### Backward compatibility

Legacy `presets:` YAML, `RoutingRule.preset`, и `ModelPreset` тип продолжают
работать через back-compat reader/bridge. Overlay-репо с legacy config НЕ
требует правок. Миграция к split-формату рекомендуемая.

## [0.1.13] — 2026-06-23

Фокус версии — **три фазы инфраструктуры агента**. Phase 0 добавляет
детерминированные guards против характерных циклов локальных LLM (повторы
результатов, остановки mid-workflow). Phase 1 строит файловый фундамент —
tracked-инструменты с журналом изменений и cross-subagent безопасностью записи.
Phase 2 вводит GAIA-style eval harness для измерения качества агента и A/B-замера
эффекта guards на локальных моделях (Qwen3.6, gemma4).

Ключевой методологический результат Phase 2: live A/B-прогоны на 26-сценарном
корпусе (glm-5.2 judge) показали, что после исправлений харнесса обе модели
достигают 88–100% pass rate (gemma4: 81%→100%), а Phase 0 guards дают стабильный
+8% на general-моделях в режиме без thinking. Харнесс выявил, что прежние провалы
моделей были артефактами scorer'а (false zeros), а не реальной неспособностью.

### Added

- **B-055 — Result-based query/tool dedup guard.** `ResultDedupGuard` в
  `agent/guards.py`: блокирует циклы по идентичному результату (hash → count;
  `detect()` при `count >= max_repeats=2`). Wire в `loop.py` после
  `add_tool_result`, ДО `SimpleProgressGuard` (success-loops vs error-loops —
  разные пути). Не-error результаты только. `_DEDUP_INSTRUCTION` (idempotent per
  run). Trace event `dedup_result_triggered`.
- **B-056 — Planning-text guard.** `PlanningTextGuard` в `agent/guards.py`:
  блокирует mid-workflow остановки — агент выдаёт planning-text ("Let me now
  verify…") вместо tool call или финального ответа. `_PLANNING_PHRASES` (19 EN из
  GAIA + 15 RU), `_TOOL_ARTIFACT_RE` regex, length-heuristic, идемпотентность
  через `corrections_used <= max_corrections=2`. Wire в блок финального ответа
  `loop.py`.
- **B-057 — `submit_report` tool для субагентов.** Явный терминатор inner
  agent-loop (`terminal=True`, `parallel_safe=False`). Регистрируется в
  `full_tool_registry` только (main agent терминируется естественным ответом).
  Универсальный one-liner в system_prompt всех субагентов. Сосуществует с B-047
  closing-mode: `submit_report` бесплатный universal closing fallback.
- **B-059 — Security-обёртки для file/exec tools.** `PathValidator`
  (`security/path_validator.py`): symlink-defense, path-traversal protection,
  workspace-root enforcement. Atomic write primitives (`utils/fs.py`):
  `write_file_atomic` (temp + rename), `ensure_parent_dir`.
- **B-040 — File cowork Phase 1: file-change journal + snapshots + recent_files.**
  `FileSnapshotStore` (`agent/file_snapshots.py`): on-disk backup перед мутацией.
  File-change journal (`memory/file_changes.py` + SQLite DAO): create/modify/delete
  с before/after hash и backup-path. `recent_files` injection в system_prompt.
  Двухуровневый read-before-write guard (per-turn `readFileHistory` + per-call
  `beforeContent` exact-match, из OpenCowork).
- **B-058 — `FileStateRegistry` — cross-subagent file safety.** Stale read/writer
  tracking: агент A прочитал файл, агент B записал → A получает stale-read warning.
  Запись после чтения требует `readFileHistory` match. Интегрирован в
  `FileTrackedTool`.
- **B-060 — GAIA-style eval harness.** Пакет `eval/` (9 модулей): `scenarios.py`
  (schema + loader), `scorer.py` (детерминированный pre-check + zero-rules +
  exact-match), `judge.py` (LLM-judge, 7-мерный рубрик), `runner.py` (multi-turn
  execution + scoring pipeline), `loop.py` (A/B orchestration guards on/off),
  `report.py` (PassReport/ABReport + per-scenario deltas), `scores.py`
  (TurnScore/ScenarioScore + weighted overall + pass/fail decision),
  `vision_fixtures.py` (deterministic PNG generator для vision-сценариев).
  Router+executor aligned (D-028): subagent trajectory capture (`record_nested`),
  soft-delegation scoring (delegation — tool_selection/efficiency signal, не
  correctness gate), judge видит tool-surface агента. CLI `eval` subcommand.
  Корпус: 26 сценариев (office aggregation, multi-step, adversarial null-answer,
  negation, memory, error_recovery, vision, personality).
- **B-060 (PR #24) — Harness improvements.** Anti-hallucination секция в
  `config/bootstrap/BEHAVIOR.md` (5 правил: не выдумывать факты, явно говорить "не
  знаю"). Переписанные tool descriptions (`read_file`, `list_files`,
  `dispatch_subagent`) с error-recovery guidance. Few-shots
  (`config/calibrated/few_shots.yaml`, 5 примеров: null-answer honesty,
  read→list_files recovery, data-agent delegation, honest refusal, concise
  answer). Eval trajectory observability: `TurnScore` теперь несёт `final_answer`,
  `tools_called`, `transcript` per turn.
- **B-060 (PR #25) — Scorer fixes из live A/B.** Три детерминированных gap'а:
  (1) `hallucinated_source` false-positive — zero-rule срабатывал на ЛЮБОЕ число в
  null-answer; фикс: don't-know check первым, контекстные числа разрешены
  ("нет инфо, только 28 дней" → pass, не zero); (2) thousands separator —
  `"27 000"` парсилось как `[27, 0]` → wrong_number; фикс: нормализация
  space/NBSP/thin-space между цифрами в `extract_numbers` + `normalize_answer`;
  (3) judge variance на border-кейсах — fixed scoring floors в рубрике
  (`config/eval/judge_turn.md`): "файл отсутствует → correctness 9-10", "correct
  refusal of impossible task → correctness + error_recovery 9-10".

### Changed

- **`agent/guards.py`** — `ResultDedupGuardConfig` и `PlanningTextGuardConfig` в
  `AgentSettings` (`config/settings.py`): configurable `enabled`, `max_repeats`,
  `max_corrections`, `max_length`. Guards читаются из settings в `loop.py`.
- **`docs/ARCHITECTURE.md`** — refresh счётчиков и версии проекта (0.1.12 →
  0.1.13): ~144→~160 модулей, ~30.6K→~34.5K LOC, 1215→1476 pytest-кейсов. Новая
  секция Eval Harness (B-060). `^0.1.12`→`^0.1.13` в plugin-manifest examples.
- **`AGENTS.md`** — §11 Hot Reload: "Три watcher'а" → "Четыре watcher'а" (+
  SubagentHotReloader, добавлен в 0.1.12, не отражён в AGENTS.md). Calibration
  scenarios: 14 → 21. Структура проекта: 28 → 29 builtin tools (добавлен
  `submit_report`).
- **`README.md` / `README_RU.md`** — Features table: 28 → 29 builtin tools.
  Stats table (README_RU): синхронизирована с ARCHITECTURE (устранён дрейф
  ~27.8K vs ~30.6K LOC; теперь ~34.5K / ~160 модулей / 1476 тестов).
- **`CONTRIBUTING.md` / `CLAUDE.md`** — `requires_core` example: `^0.1.11` /
  `^0.1.12` → `^0.1.13` (приведены к единой актуальной версии).

### Verified

- `uv run ruff check src/ tests/` — clean.
- `uv run pyright src/` — `0 errors` (16 существующих matplotlib stub warnings).
- `uv run pytest tests/ -q` — `1476 passed, 1 skipped`.

## [0.1.12] — 2026-06-18

Фокус версии — **архитектура приватных расширений (overlay)**. Корпоративные
доработки (skills, plugins, subagents, bootstrap-промпты, RBAC-правила, MCP)
работают поверх публичного ядра через отдельный приватный репозиторий
(`corpclaw-corp`, sibling публичного), который компонуется с ядром в рантайме
через путь — никогда через git-merge. Ни один приватный файл физически не
попадает в публичный репозиторий; ядро не требует правок для нового расширения
(99% корпоративных доработок — это новый контент в overlay, а не код `src/`).

Контракт: overlay-entries переопределяют дефолты по id/name (skills/plugins/
subagents/bootstrap — replace; departments — union-merge), plugins декларируют
`requires_core` для fail-loud при несовместимости ядра. Подробности — D-050.

### Added

- **PR-1 — Центральный path resolver.** `src/corpclaw_lite/extensions/paths.py`
  → `resolve_dirs(kind, settings, project_root) -> list[Path]` возвращает
  `[default, ...overlays]` для каждого `ExtensionKind` (skills/plugins/
  subagents/mcp/bootstrap). Фильтрует пустые строки (страховка от cwd-leak при
  `${VAR}`→`""`) и несуществующие пути. `ExtensionsSettings.extra_paths` в
  `settings.yaml` — единая точка конфигурации overlay. Mirror-layout: каждый
  overlay-путь повторяет структуру проекта.
- **PR-2 — Multi-directory loading + override-семантика (8 call-site'ов).**
  Все реестры и загрузчики переведены на `resolve_dirs`: skills, plugins,
  subagents, bootstrap-промпты, MCP, watchers (skills/plugins/subagents/mcp),
  `agent.factory._build_extensions_stack`, `cli cmd_skill_list`/`cmd_plugin_list`,
  telegram orchestrator. Override: `load_directory(*, allow_replace=index > 0)` —
  skills/plugins/subagents/bootstrap логируют WARN и заменяют по id/name;
  plugin-tools честно переопределяются (unregister старых → register overlay
  tools, чтобы не было orphan tools при drop/rename tool'а в overlay). MCP
  merge'ит `servers:` по имени (last wins). Bootstrap переопределяет по filename
  на верхнем уровне и first-match high→low для departments/users/subagents.
- **PR-3 — Departments union-merge.** `DepartmentManager.load_file(*, merge=True)`
  для overlay-индексов: allowlists объединяются с wildcard-нормализацией
  (`["*"] + [x] → ["*"]`), budget (`max_iterations`/`max_tool_calls`)
  переопределяется где overlay указывает. `max_time_ms` **никогда** не мерджится
  — всегда из settings (D-037). Отдельный resolver `resolve_department_files`
  (не `resolve_dirs`), т.к. departments merge, а не replace.
- **PR-4 — `requires_core` version contract для plugins.**
  `extensions/plugins/core_version.py`: `get_core_version()` (через
  `importlib.metadata.version` с fallback), `satisfies_core_version(constraint)`
  — caret-совместимый парсер (`^0.1.11` пинит minor для 0.x, major для 1.x+;
  bare = exact). Поле `requires_core: str = ""` в `PluginManifest`. Единственный
  chokepoint проверки — `PluginRegistry.register` (warn-and-skip, не raise),
  покрывает все пути загрузки (bootstrap, CLI, hot-reload). Контракт применяется
  только к plugins (не skills/subagents).
- **W7 — End-to-end верификация overlay.** `tests/test_overlay_e2e.py` (11
  тестов) активирует sibling `corpclaw-corp` через `Settings(extra_paths=[...])`
  и проверяет четыре гарантии: (a) каждый вид расширения грузится иusable через
  overlay, включая реальный `execute()` plugin tool.py через полный путь
  `PluginToolProxy → sandbox_worker` subprocess; (b) overlay выигрывает по
  id/name (replace для skills/subagents/bootstrap, union для departments);
  (c) приватные файлы не утекают в публичный репо (git status clean, scan
  `_plugin_tool*.pyc`, `resolve_dirs` без overlay → только default, diff
  директорий до/после); (d) следов не остаётся. **Skip-if-missing**: без
  corpclaw-corp → 11 skip за ~1s (CI чистый); с corpclaw-corp → 11 pass за ~5s.
  README "Extending" секция документирует запуск.
- **W8 — Документация overlay.** `CONTRIBUTING.md` — секция "Private Extensions
  (Overlay)" (mirror-layout, override/union-семантика, requires_core, split-core-
  and-overlay правило); исправлен Development Workflow (feature-ветки от
  `pre-release`, не main); tiered commit policy. `README.md` — overlay-сниппет +
  ссылка на CONTRIBUTING.md.

### Changed

- **`docs/ARCHITECTURE.md`** — overhaul: новая секция Private Extensions Overlay
  (two-repo модель, `resolve_dirs`, override-семантика таблицей, `requires_core`);
  расширение §LLM Queue/Slot Affinity/KV-cache (`LLMRequestQueue`,
  `SlotAffinityConfig`, `LLMCacheManager` L1/L2); новая подсекция Backend LLM
  Streaming (`llm_streaming_enabled`, trace events, stall detection); 4-й watcher
  (subagents); `requires_core` в plugin manifest example; refresh счётчиков и
  версии проекта (0.1.7 → 0.1.12).
- **`README.md` / `README_RU.md`** — Features table: актуализованы счётчики
  (tools, ToolGuard rules), disambiguated plugins/skills/subagents, добавлена
  строка Private Extensions Overlay.
- **`plans/corpclaw-lite-design.md`** (локальный, gitignored) — заморожен на v2.0
  (Phase 5, 23 Mar 2026): убрано ложное «АКТУАЛЬНЫЙ», добавлен указатель на
  `AGENTS.md` + per-feature plan docs как на авторитетные.

### Decisions

- **D-050 — Private extensions overlay: two-repo model (required).** Приватные
  корпоративные расширения живут в отдельном приватном репо (`corpclaw-corp`) и
  компонуются с публичным ядром в рантайме через
  `config/settings.yaml → extensions.extra_paths`, никогда через git-merge.
  Приватные файлы НИКОГДА не в публичном репо — даже в gitignored-папке (gitignore
  не гарантирует от утечки). Зависимость однонаправленная: overlay декларирует
  `requires_core`, ядро ничего не знает про overlay. Hard rules: (1) никогда не
  класть приватные файлы в публичный репо; (2) держать контракт расширений
  стабильным и аддитивным; (3) overlay декларирует `requires_core` и fail-loud
  (warn-and-skip) при несовместимости; (4) фича, требующая и ядра, и overlay →
  split на два PR. Known limitation: `requires_core` грубоват (minor-уровень для
  0.x), тонкую несовместимость не поймает — компенсируется стабильной границей
  расширений.

### Verified

- `uv run ruff check src/ tests/` — clean.
- `uv run pyright src/` — `0 errors`.
- `uv run pytest tests/test_overlay_e2e.py tests/test_extensions_paths.py
  tests/test_plugin_core_version.py tests/test_department_manager.py
  tests/test_skills.py tests/test_plugins.py tests/test_subagent_registry.py
  tests/test_bootstrap.py tests/test_mcp.py -q` — `79 passed`.
- `tests/test_overlay_e2e.py` — `11 passed` с sibling `corpclaw-corp`,
  `11 skipped` без него (CI-совместимо).

## [non-version] — 2026-06-05

### Changed

- Стабилизирован системный prompt layer проекта: core/subagent prompts, builtin subagent
  metadata и skill instructions приведены к английскому языку; русские фразы оставлены только
  как явно помеченные examples/pattern examples/keywords для распознавания пользовательских
  формулировок.
- Research-agent final answer templates заменены на language-neutral описания секций, чтобы
  системный prompt не смешивал русские и английские заголовки внутри одного шаблона.

### Added

- Добавлена focused-проверка prompt hygiene, запрещающая кириллицу в системных prompt surfaces
  вне явно разрешённых зон examples/patterns/keywords и ловящая транслит в subagent metadata.

### Verified

- `uv run ruff check src/ tests/test_prompt_hygiene.py` — clean.
- `uv run pyright src/` — `0 errors` (17 существующих matplotlib stub warnings).
- `uv run pytest tests/ -v` — `1060 passed, 1 skipped`.

## [0.1.11] — 2026-06-16

Фокус версии — достоверность источников research-агента на локальных LLM. Три задачи
(B-052, B-053, B-054) закрывают три разных режима галлюцинации, выявленных в live-тестах
`deep_research`: (1) веб-поиск падает на transient-блоках → модель выдумывает URL; (2) на
длинном контексте модель «забывает» реальные source_id и выдумывает новые; (3) 404/403-страницы
сохраняются как валидные источники и цитируются в отчёте.

Валидировано live-раном `1b038558` (2026-06-16): 10/10 источников HTTP 200 (было 4/10 живых),
0 галлюцинированных source_id, finalize-валидация пройдена с первого раза, укладывается в
264с из 600с окна. Модель честно признаёт пробелы (HTTP 404) вместо маскировки их фактами.

### Added

- **B-054-2 — Динамический бюджет источников и поиска.** Новые поля `ResearchSettings`:
  `target_usable_sources=5`, `dynamic_budget_max_multiplier=2.5`. Новые методы `ResearchRuntime`:
  `available_sources_count()` (считает только HTTP 2xx), `effective_max_sources()`,
  `effective_search_waves()`. Расширение failure-driven: `limit = min(max(base, base + failed),
  cap)` — чистый рун останавливается на base, каждый не-2xx fetch даёт один дополнительный
  слот чтобы добрать target_usable_sources, жёсткий потолок `base * multiplier`. `reserve_fetch`
  и оба search-бюджета (`reserve_search`, `search_budget_exceeded`) используют динамические
  лимиты. `target_usable_sources` — мягкая цель, на которую модель направляют через промпт,
  а не значение, способное переопределить base.
- **B-054-3 — Валидация недоступных источников в finalize.** `_validate_report` (новая
  проверка 2b) отклоняет ответы, цитирующие source_id или URL со статусом ≠ 2xx. Строит
  карту source_id/url → status по манифесту; недоступные цитаты → Error «remove them from
  citations», модель направляется к `research_list_sources`. Идёт после проверок integrity
  source_id/URL, усиливая их.
- **B-053 — `research_list_sources` + source-anchor в `store_fact`.** Новый инструмент
  `ResearchListSourcesTool` (`research_list_sources`) — зеркало `research_list_facts` для
  источников: возвращает точный список `[source_id → title | url | status]`. Устраняет
  галлюцинацию source_id в `research_store_fact` на длинном контексте (модель получала
  реальные ID от `fetch_source`, но через ~200с «забывала» их и выдумывала 12-hex ID).
  `store_fact` при Unknown source_id теперь возвращает реальные кэшированные source_ids
  (самокорректирующаяся ошибка): жёсткий нудж, если `list_sources` не вызывался, мягкий —
  если вызывался. Счастливый путь `fetch → store` не блокируется. `ResearchRuntime.format_sources_list`,
  флаг состояния `list_sources_called`, `research-agent.yaml` `allowed_tools`.
- **B-052 — Resilience веб-поиска (auto backend + retry) + offline-режим.**
  - `WebSearchTool._search_sync` ретраит `search_retry_attempts` раз с backoff перед тем
    как сообщить об ошибке; каскадный сбой → маркер «unavailable (infrastructure)».
    Удалён мёртвый код (`RatelimitException`, недостижимая ветка `if not results` —
    `ddgs.text()` никогда не возвращает `[]`, всегда бросает `DDGSException`).
  - Бюджет: `ResearchSearchTool` peek-ает бюджет (`search_budget_exceeded`) до HTTP-запроса,
    а unit (`reserve_search`) списывает только при успехе. Инфраструктурные сбои НЕ списывают
    бюджет (`refund_search`). `mark_search_failure` / `is_web_search_degraded`: после ≥2 сбоев
    рун помечается `web_search_degraded`, модель получает `_WEB_SEARCH_DEGRADED_MESSAGE`
    («прекрати веб-поиск, не выдумывай URL»).
  - Offline-режим: правила в `research.md` (остановить поиск, не выдумывать URL, ответить
    из знаний с явной пометкой) + `finalize_report` safety-net баннер «web search was
    unavailable... based on model knowledge», даже если модель его пропустила.

### Changed

- **B-054-1 — Глобальный HTTP-фильтр в `web.py:_fetch`.** Не-2xx ответ (404/403/5xx)
  теперь возвращает `Error: HTTP {code} ... The source is unavailable; do not cache or cite it`
  *до* любого парсинга контента. Поскольку `research_fetch_source.execute` проверяет
  `result.startswith("Error")` до `store_source`, 4xx/5xx страницы физически не попадают в
  манифест. Глобально — лечит и `research_fetch_source`, и `web_fetch` основного агента.
  Редиректы (is_redirect → re-fetch) и 2xx не затронуты.
- **B-052-2 — Конфигурация веб-поиска.** `WebSettings.search_backend` default `duckduckgo`
  → `auto` (8 движков с fallback; `duckduckgo` transient-блокирует серверные запросы через
  `html.duckduckgo.com/html/`). Новые поля: `search_retry_attempts=3`,
  `search_retry_backoff_seconds=1.5`.
- **B-054-4 — Промпт `research.md`.** Добавлено правило: HTTP-error источник (4xx/5xx)
  недоступен навсегда — не ретраить тот же URL, не цитировать; перед `store_fact`/`finalize`
  сверять usable-источники через `research_list_sources`.
- **B-053 — Промпт `research.md`.** Документирован инструмент `research_list_sources` и
  усилен workflow (вызов `research_list_sources` перед `store_fact` для точных source_id).

### Verified

- `uv run ruff check src/ tests/` — clean.
- `uv run pyright src/` — `0 errors` (17 существующих matplotlib stub warnings).
- `uv run pytest tests/ -v` — `1165 passed, 1 skipped` (+105 тестов с 0.1.10).
- Live-ран `1b038558`: 10/10 источников HTTP 200, 0 галлюцинированных source_id, 5 fetch-ошибок
  корректно отбракованы HTTP-фильтром B-054-1, finalize `validation_passed` с первого раза.



Фокус версии — устойчивость research-агента в тяжёлом `deep_research` на локальных LLM.
Комплекс из 5 задач (B-045…B-049) по стратегии finalize-first (D-048): больше окно →
подталкивание модели к `research_finalize` внутри окна → честный skeleton как safety-net.

### Added

- **B-049 — Per-subagent wall-clock budget.** Поле `max_wall_time_ms` в `SubagentSpec` +
  `research-agent.yaml: 600000` (10 мин). `subagent.py` клонирует `AgentSettings` через
  `model_copy` со значением из спеки, так что внутренний `AgentLoop` budget + `SoftDeadline`
  + `asyncio.wait_for` outer limit масштабируются автоматически; родительский агент и
  остальные субагенты остаются на 5 мин. **Багфикс:** `watcher.py:_load_spec` не передавал
  `direct_response` (дивергенция с `registry.py`) — починено, оба лоадера теперь идентичны;
  добавлен регресс-тест `test_watcher_load_spec_matches_registry`.
- **B-047 — Workflow-finalize guard (`TerminalToolMandate`).** Детерминированный гвард в
  `agent/guards.py`, знающий про обязательную терминальную воронку research
  (`store_fact → list_facts → research_finalize`). Эскалация: nudge (60% — системное
  напоминание «прекратите сбор, вызовите list_facts+finalize») → restrict (75% —
  `tools_schema` урезан до `{list_facts, finalize}`). Нейтрален без `terminal_tool`
  (основной агент, не-research субагенты). Строго детерминирован — не нарушает запрет
  AGENTS.md на LLM-based planning. Поля `SubagentSpec.terminal_tool`/`required_before_terminal`
  + wiring через `AgentConfig` → `AgentLoop`. Trace events `workflow_nudge_injected`,
  `workflow_restrict_applied`.
- **B-045 — Честный skeleton на partial-handoff (вариант A).** При таймауте
  `finalize_report(interrupted=True)` рендерит честный баннер «⚠️ Исследование прервано по
  лимиту времени» + собранные факты + источники + «Синтез не выполнен», БЕЗ фейковых секций
  «Противоречия/Гипотезы/Рекомендации» которые обещает finished deep_research. Новые
  `_INTERRUPTED_REPORT_TEMPLATES` (4: ru/en × research/deep_research).

### Changed

- **B-048 — Дедуп фактов в deep_research-шаблоне.** «Ключевые выводы» используют `{facts}`
  (brief, без evidence-экзепшена), «Факты и подтверждения» — новый `{evidence}` (full). Раньше
  обе секции рендерили один `{facts}` → факты дублировались. `_facts_markdown(with_evidence)`.
- **B-046 — SoftDeadline granularity.** Closing-mode логика вынесена в
  `AgentLoop._apply_closing_mode()` и вызывается не только в начале каждой итерации, но и
  **непосредственно перед каждым LLM-вызовом** (3 ветки). Фиксит race: итерация стартовала до
  soft_deadline (248s < 255s), длилась >50s, на 255s мы уже внутри LLM-вызова — теперь closing
  mode включается на следующей итерации до LLM-вызова, а не после.

### Decisions

- **D-047** — направление B-045…B-049 как комплекс.
- **D-048** — стратегия finalize-first: прерывание оставляем (освобождает GPU под
  конкурентные запросы при ограниченных ресурсах), resume (B-050) отнесён в Phase 2.

### Verified

ruff clean, pyright 0 errors (17 существующих matplotlib stub warnings), pytest 1131
passed/1 skipped (+39 новых тестов), coverage 78% (CI-гейт 75%).

## [0.1.9] — 2026-06-15

Фокус версии — частичный handoff и checkpointing для долгих субагентов на локальных LLM (B-036 MVP).

### Added

- `TaskRun` (`agent/task_run.py`) — per-run checkpoint-журнал в
  `workspaces/user_<key>/.task_runs/<run_id>/` (`state.json`, `journal.jsonl`,
  `handoff.md`) по паттерну `ResearchRuntime`. Методы `initialize`,
  `record_tool_call`, `set_phase`, `mark_soft_deadline`, `generate_handoff`.
- `SoftDeadline` (`agent/guards.py`) — wall-clock soft deadline через
  `time.monotonic()`, без `pause()`/`resume()`. Фиксит race из памяти
  `subagent-timeout-race-kills-d-038-rescue`: `asyncio.wait_for` (wall-clock)
  всегда срабатывал раньше `SimpleBudgetGuard` (active time, D-040), поэтому
  rescue D-038 никогда не работал для субагентов. Soft deadline тоже меряет
  wall-clock и корректно срабатывает при queue-wait.
- Closing mode в `AgentLoop.run()`: при достижении soft deadline (по умолчанию
  `soft_deadline_ratio × max_wall_time_ms` = 0.85 × 300000 = 255s) цикл
  урезает `tools_schema` до terminal-инструментов (`research_finalize` и т.п.),
  заставляя модель финализировать вместо hard-cancel. `asyncio.wait_for`
  остаётся аварийным внешним пределом.
- Research partial-handoff: при таймауте research-субагента
  `SubagentDispatcher` возвращает language-aware skeleton через
  переиспользование `finalize_report(answer="")` (факты+источники+limitations),
  а не bare `Subagent error: execution timed out`. Записывается `handoff.md`.
- Trace-события: `agent_soft_deadline_reached`, `subagent_partial_handoff`.
- Универсальный journal tool-вызовов в `AgentLoop._execute_single_tool` (args_hash,
  status, duration, error) — единая точка перехвата.
- `workspace_base` проброшен через `AgentConfig` → `AgentLoop` →
  `SubagentDispatcher` для корректного расположения `.task_runs/`.

### Changed

- `AgentSettings.soft_deadline_ratio: float = 0.85` — новое поле.
- `DispatchSubagentTool.should_return_direct` — задокументировано, что partial
  reports проходят фильтр ошибок и идут пользователю напрямую (main agent не
  повторяет тяжёлую работу).

### Verified

- `uv run ruff check src/ tests/` — clean.
- `uv run pyright src/` — `0 errors` (17 существующих matplotlib stub warnings).
- `uv run pytest tests/ -q` — `1092 passed, 1 skipped`.
- Новые тесты: `tests/test_task_run.py` (5), `tests/test_soft_deadline.py` (7
  включая race-fix contrast с SimpleBudgetGuard), `tests/test_research_partial_handoff.py`
  (3: partial report, non-research bare error, trace event).
- Manual live-LLM check deferred (требует локальную модель; soft_deadline_ratio
  default 0.85 + closing mode non-breaking для short tasks).

## [0.1.8] — 2026-06-14

Фокус версии — детерминированная и аудируемая финализация research-agent (B-037).

### Added

- `detect_language()` — эвристика целевого языка research-задачи по доле кириллицы (порог 0.3);
  язык сохраняется в `state.json` и инжектируется в task_context субагента как жёсткое поле
  `Target language`.
- Language-aware skeleton-шаблоны `_build_report()`: 4 набора заголовков (ru/en × research/
  deep_research) вместо хардкода `## Executive summary` для deep_research.
- `_validate_report()` в `finalize_report()`: соответствие языка ответа целевому, целостность
  source_id/URL против manifest, мандат `research_list_facts` для deep_research, count-assertions
  (число источников в отчёте ≤ реально fetched).
- Гибридный recovery: до 2 retry через Error-строку (терминальный шлюз `loop.py:906-914`
  пропускается), затем deterministic skeleton из stored facts. Защита от зацикливания:
  `finalize_attempts` cap + `SimpleProgressGuard`.
- `finalize_strict: bool = False` в `ResearchSettings` — soft-mode по умолчанию
  (warn+trace, не блокирует); enforce после телеметрии.
- Trace-события `research_finalize_validation_passed/failed/warning/skeleton_fallback` с counts
  (mode, language, fetched_sources, facts_total, list_facts_called, finalize_attempts).
- `mark_list_facts_called()` фиксирует факт вызова `research_list_facts` для аудита deep_research.

### Verified

- `uv run ruff check src/ tests/` — clean.
- `uv run pyright src/` — `0 errors` (17 существующих matplotlib stub warnings).
- `uv run pytest tests/ -q` — `1077 passed, 1 skipped`.
- Новые тесты: `tests/test_research_finalization.py` (17 кейсов: language detection, strict
  validation, recovery, trace, task_context injection).

## [0.1.7] — 2026-06-05

Фокус версии — корректное отображение LLM-очереди и ожидания начала генерации в Web/Telegram.

### Added

- Добавлен backend-контракт `LLMQueueStatus`: очередь LLM теперь отдаёт позицию, примерное
  время ожидания, число активных запросов и фактическое время ожидания слота.
- AgentLoop получил отдельные callbacks для ожидания LLM-слота основным агентом и субагентами,
  а также явные стадии `model_preparing` и `model_waiting`.
- Web и Telegram теперь показывают разные пользовательские состояния: ожидание GPU/LLM-слота
  и ожидание начала генерации ответа модели.
- Статусы LLM-очереди субагентов прокидываются с названием субагента, чтобы было понятно,
  какой именно исполнитель ждёт слот или начало генерации.
- Web timeline получил отдельную фазу `Очередь` и дедупликацию повторяющихся одинаковых
  status-событий.

### Changed

- Telegram больше не использует отдельный pre-run polling очереди: статус ожидания слота теперь
  приходит из самого `LLMRequestQueue`, то есть отражает реальное состояние backend-очереди.
- Queue wait по-прежнему не сжигает agent budget: бюджет ставится на паузу до получения слота и
  возобновляется перед фактическим LLM-вызовом.

### Verified

- Полная проверка: ruff clean, `uv run pyright src/` — `0 errors` (17 существующих
  matplotlib stub warnings), `uv run pytest tests/ -v` — `1055 passed, 1 skipped`.
- Frontend проверки: `npm run test` и `npm run build`.

## [0.1.6] — 2026-06-04

Фокус версии — трансляция внутренней работы субагентов в пользовательские статусы Web/Telegram.

### Added

- Добавлены subagent-aware status callbacks для tool calls, parallel tool batches и LLM stages
  внутреннего `AgentLoop` субагента.
- Telegram и Web теперь показывают вложенные статусы с человекочитаемым именем субагента,
  например `Research Agent: 🤔 Думаю...` или `Document Agent: 📂 Читаю файл...`.

### Changed

- `dispatch_subagent` больше не оставляет пользовательский статус зависшим на «Делегирую
  субагенту...»: дальнейшие tool/LLM стадии субагента прокидываются через существующий
  callback pipeline без раскрытия task, arguments, results, prompt или reasoning content.
- Runtime context `ToolRegistry.execute()` расширен служебными callback-параметрами для
  `DispatchSubagentTool`; tool schema и LLM-visible arguments не изменились.

### Verified

- Focused проверки после форматирования: `52 passed` по subagent/status pipeline, progress и Web.
- Полная проверка: ruff clean, `uv run pyright src/` — `0 errors` (17 существующих
  matplotlib stub warnings), `uv run pytest tests/ -v` — `1038 passed, 1 skipped`.

## [0.1.5] — 2026-06-04

Фокус версии — улучшение пользовательской видимости текущих действий агента в Web/Telegram
и закрепление процесса версионирования после функциональных доработок.

### Added

- Добавлен агрегированный статус для параллельных tool calls: AgentLoop теперь поддерживает
  `on_tool_batch_start`, а Web/Telegram показывают один понятный статус вроде «Работаю с
  файлами...» или «Выполняю 2 действия...» вместо гонки нескольких отдельных tool-статусов.
- Введено проектное правило: после существенных функциональных изменений обновлять версию и
  `CHANGELOG.md`; перед будущим выбором номера версии запрашивать подтверждение пользователя,
  чтобы при крупных изменениях можно было поднять minor-версию.

### Changed

- Web-канал больше не превращает технический LLM stage `finished` в пользовательский статус
  «В обработке...». Неизвестные/unmapped LLM stages игнорируются, а финальный переход остаётся
  только через `request_finished`.
- Parallel-safe инструменты больше не отправляют индивидуальные `on_tool_start` статусы внутри
  одной параллельной пачки; одиночные и последовательные tool calls сохраняют прежнее поведение.

### Verified

- Полная проверка: `uv run ruff check src/ --fix && uv run ruff format src/ && uv run pyright src/ && uv run pytest tests/ -v`.
- Результат: ruff clean, pyright `0 errors` (17 существующих matplotlib stub warnings),
  pytest `1033 passed, 1 skipped`.

## [0.1.4-beta] — 2026-06-04

Текущий beta-релиз. Основной фокус — production-ready Web-канал, премиальный русскоязычный
интерфейс, управление конкурентностью локальных LLM, slot affinity для llama.cpp,
экспериментальный persistent KV-cache в файлах и ручные live-тесты на реальном llama-server.

### Added

#### Browser web channel
- Добавлен веб-канал `uv run corpclaw-lite web` на `aiohttp`: локальный login, HttpOnly session
  cookie, WebSocket-чат, статусы выполнения, approvals и минимальный server-rendered UI.
- Веб-интерфейс вынесен в React/Vite приложение: добавлены рабочий chat shell, единый statusline
  вместо потока статусных сообщений, collapsible file explorer, preview drawer, drag-and-drop
  upload/move и batch file operations.
- Web UI получил второй слой production-polish: resizeable файловая панель и preview, сохранение
  ширин в браузере, полноценный проводник с деревом папок, режимами list/grid/details,
  нормальными диалогами операций и expanded preview вместо узких фиксированных областей.
- Web UI расширен fullscreen-режимом файлового менеджера, кнопкой/командой `/new` для сброса
  контекста и индикатором заполненности контекста по backend-reported token usage.
- Контекстное меню файлового менеджера теперь автоматически остаётся внутри видимой области окна,
  включая fullscreen-режим и клики по файлам у правого/нижнего края.
- Web download приведён к явному режиму загрузки: файлы отдаются с исходным именем, image preview
  использует отдельный inline endpoint, а ссылки на файлы от агента получили TTL и user-boundary.
- Web layout получил защиту от схлопывания центральной панели: файловый preview теперь открывается
  явно как side preview или fullscreen preview, resize учитывает минимальную ширину чата, а topbar
  переносит элементы в compact-режиме.
- Web file explorer получил компактный режим по ширине самой панели: на узкой панели дерево папок
  открывается отдельным drawer, а таблица файлов превращается в читаемый список без обрезанных
  колонок.
- Web-чат получил безопасный Markdown/GFM-рендер ответов модели: списки, таблицы, ссылки,
  inline-code и code blocks с кнопкой копирования теперь отображаются как полноценный ответ, а не
  как сырой текст.
- Web-чат получил persistent transcript в SQLite: история текущей сессии переживает refresh,
  logout/login и reconnect, `/new` открывает новую пустую сессию, а долгие запросы больше не
  привязаны к одному старому WebSocket-соединению.
- Web UI получил премиальный русскоязычный рабочий интерфейс: операционный центр с обзором
  выполнения, последние файлы и результаты, сворачиваемые панели, отдельное меню пользователя,
  явная новая сессия и collapsible operation center.
- Web frontend получил runtime-проверку REST/WebSocket JSON-контрактов без новых зависимостей,
  а TypeScript-проверка усилена `noUncheckedIndexedAccess` и `exactOptionalPropertyTypes`.
- Web-канал теперь распознаёт `502 upstream_error / Connection refused` от OpenAI-compatible
  LLM gateway как ожидаемую недоступность backend-модели и отдаёт пользователю warning без
  traceback в обычном сценарии.
- Web file API расширен операциями tree/search/preview/rename/move/copy/batch delete; все операции
  сохраняют host-side boundary checks личного workspace.
- Web shutdown теперь явно останавливает контейнеры пользователей, которые поднимались или
  переиспользовались текущим web-процессом.
- Добавлены локальные веб-аккаунты в `UserManager`: `web-user-create`, `web-user-password`,
  PBKDF2-хэширование пароля и SQLite-сессии с CSRF token.
- Добавлена привязка web-логина к существующему Telegram-профилю: `web-user-link`,
  `web-user-create --telegram-id` и безопасное слияние дублей через `web-user-merge`, чтобы
  веб-канал использовал ту же память и workspace.
- Внутренний ключ пользователя унифицирован на `users.id`: контейнеры, workspace, memory,
  onboarding и user bootstrap больше не используют `telegram_id` как технический идентификатор.
  Добавлена миграция `user-migrate-canonical-ids` для старых данных.
- Добавлен личный файловый веб-диспетчер: list/upload/download/delete/mkdir с проверкой границ
  workspace, лимитом размера и переиспользованием правил безопасных расширений.
- Вынесен `AgentRequestService` для channel-neutral запуска agent workflow: сборка prompt,
  skill matching, container preflight, approval callback и structured activity logging.

#### Host-side web search
- Добавлен инструмент `web_search` через `ddgs` с явным backend DuckDuckGo, лимитами
  конкурентности и безопасным контрактом `query -> URL/snippet`.
- `web_fetch` усилен `format=raw|text`, User-Agent и process-level backpressure; `format=text`
  очищает HTML в компактный текстовый вид для локальных LLM.
- `research-agent` теперь использует цепочку `web_search -> web_fetch(format="text")` для
  веб-исследований, а доступ к поиску выдан тем же департаментам, где уже был разрешён web fetch.

#### PDF extraction cleanup
- `pdf_reader` теперь очищает PDF extraction от непечатаемых control-символов, которые могут
  появляться в формулах после `pypdf.extract_text()` и ломать LLM/tool context.
- Добавлен параметр `output_path` для сохранения очищенного PDF-текста в `.md`, `.markdown` или
  `.txt` без промежуточного копирования сырого вывода через `write_file`.
- `document-agent` теперь инструктируется использовать `pdf_reader output_path` для PDF→Markdown
  задач.

#### Excel formula-aware reads
- `excel_workbook action=read` теперь по умолчанию показывает формульные ячейки как
  `formula + cached_value`, чтобы агент видел и саму формулу, и фактическое сохранённое значение
  из workbook.
- Добавлен `formula_mode`: `both` (по умолчанию), `values` для старого value-only поведения и
  `formulas` для чтения только формул.
- `excel_workbook` теперь поддерживает comma-separated mix одиночных ячеек и диапазонов в `cells`,
  например `A1,B2:D4,F8:G9`.
- Промпты Excel-заполнения и субагентов уточнены: для шаблонов с датами/периодами/формулами нужно
  читать диапазоны в `formula_mode=both` и не перезаписывать формульные ячейки без явной просьбы.

#### LLM Queue и backpressure
- Добавлена очередь LLM-запросов `LLMRequestQueue`, ограничивающая реальную inference-
  конкурентность через `llm.max_concurrent_requests`.
- Базовая конкурентность для локальной машины установлена в `4` одновременных запроса.
- Очередь отслеживает позицию запроса, время ожидания, время выполнения и отдаёт эти данные в
  trace/health.
- `SimpleBudgetGuard` теперь может ставиться на pause на время ожидания LLM-слота, чтобы агентный
  budget расходовался на работу модели и инструментов, а не на ожидание очереди.
- `LLMRouter` получил единый путь выполнения через queue/cache для обычных и default-вызовов.

#### llama.cpp Slot Affinity
- Добавлена стратегия очереди `slot_affinity` для llama.cpp-compatible backend.
- Конфигурация по умолчанию: sticky-слоты `0,1,2` для активных пользователей и overflow-слот `3`
  для нагрузки сверх sticky-ёмкости.
- Sticky-слот удерживается за пользователем на `idle_ttl_seconds` после ответа, чтобы сохранить
  горячий KV-cache между последовательными запросами.
- Для llama.cpp-вызовов автоматически добавляются `id_slot` и `cache_prompt`.
- Добавлена политика `auxiliary_policy: "overflow_only"` для вспомогательных LLM-вызовов, чтобы
  они не разрушали sticky-cache основных пользовательских сессий.

#### Persistent Slot KV-cache
- Добавлен `LLMCacheManager` в `src/corpclaw_lite/llm/cache.py`.
- Реализован L1/L2 cache-подход: L1 — живой KV-cache в слоте llama.cpp, L2 — сохранённый
  файловый cache через llama-server slot save/restore/erase API.
- L2 файловый cache помечен как экспериментальная возможность и отключён по умолчанию на
  тестовой машине, чтобы не создавать лишнюю write-нагрузку на SSD.
- Добавлен SQLite index для L2 cache с метаданными scope, размера, возраста, restore count и
  последнего использования.
- Cache scope учитывает `user_id`, `conversation_id`, `agent_id`, провайдера, модель, preset,
  hash system prompt и hash набора tools.
- Это позволяет хранить отдельные cache-файлы для основного агента и субагентов одного
  пользователя.
- Добавлены save policies: `hybrid`, `every_response`, `eviction_only`.
- Добавлены параметры автоочистки: `max_total_bytes`, `max_age_days`,
  `prune_interval_seconds`.
- Добавлена валидация восстановленного cache по фактическим usage-метрикам модели:
  `cached_input_tokens`, prompt tokens и reuse ratio.
- При низком reuse ratio включается безопасный fallback: слот очищается, cache scope сбрасывается,
  запрос повторяется без доверия к старому cache.

#### Token usage и observability
- `TokenUsage` расширен метрикой `cached_input_tokens`, чтобы видеть реальное переиспользование
  prompt cache.
- Добавлены trace/health события для queue, slot affinity и persistent cache: вход/выход из
  очереди, получение/освобождение слота, reuse sticky slot, overflow slot, L1/L2 cache hit,
  restore/save, mismatch validation, prune.
- Логика логирования теперь даёт достаточно данных для отладки долгого TTFT, неправильного
  cache restore, очередей и поведения слотов.

#### Manual Live LLM Tests
- Добавлен каталог `tests/live_llm/` с ручными интеграционными тестами против реального
  llama-server.
- Live-тесты не входят в обычный pytest-пул и запускаются только при
  `CORPCLAW_LIVE_LLM_TESTS=1`.
- Медленные сценарии дополнительно требуют `CORPCLAW_LIVE_LLM_RUN_SLOW=1`.
- Покрыты сценарии: доступность API и `/slots`, cache save/restore roundtrip, mismatch
  validation, 4 параллельных запроса по слотам, интеграция router/queue/cache, prune/cleanup.
- Тесты пишут JSON-отчёты в `reports/live_llm/` для ручного анализа TTFT, TPS, prompt processing,
  cache reuse ratio и save/restore latency.

### Changed

- `config/settings.yaml` теперь включает production-oriented настройки очереди, slot affinity и
  экспериментального persistent cache для llama.cpp.
- Текущий эксплуатационный приоритет изменён на `slot_affinity` + RAM KV-cache в живых слотах:
  3 sticky-слота для активных пользователей и 1 общий overflow-слот.
- `pyproject.toml` исключает `tests/live_llm/` из обычного тестового пула.
- LLM streaming продолжает использоваться "под капотом", но теперь работает поверх queue/cache
  слоя, а не в обход контроля конкурентности.
- Слоты рассматриваются как ценный локальный ресурс: проект старается сохранять их состояние,
  а не просто равномерно размазывать запросы по backend.

### Verified

- Полная релизная проверка на `pre-release`:
  - `uv run ruff check src/ tests/`
  - `uv run pyright src/`
  - `uv run pytest tests/ -q`
  - `cd frontend/web && npm run build`
  - `cd frontend/web && npm run test`
- Результат полной проверки: `1017 passed, 1 skipped`; pyright — `0 errors`,
  `17 warnings` по неполным matplotlib-стабам в `chart_generate.py`.
- Ручные live-тесты на `llama-server` с моделью `gpt-oss-20b-UD-Q4_K_XL`:
  - обычный live-прогон: `7 passed, 1 skipped`;
  - slow large cache roundtrip: `1 passed`;
  - запуск без `CORPCLAW_LIVE_LLM_TESTS`: `8 skipped`.
- Практические метрики на реальном backend:
  - 1k prompt cold: TTFT около `0.67s`, prompt processing около `614ms`;
  - 1k prompt warm from cache: TTFT около `0.085s`, prompt processing около `14ms`;
  - 5k prompt cold: TTFT около `2.94s`, prompt processing около `2865ms`;
  - 5k prompt warm from cache: TTFT около `0.084s`, prompt processing около `15ms`;
  - 4 parallel slots: общий wall time около `3.19s`, TTFT по слотам около `2.64-2.68s`.

### Notes

- Persistent cache даёт главный выигрыш именно на длинных локальных контекстах: вместо повторного
  prompt processing на десятках тысяч токенов можно восстановить KV-cache из файла и продолжить
  диалог.
- Пока проект тестируется на рабочем ПК с одним SSD, L2 cache следует держать выключенным и
  включать только для целевых ручных экспериментов.
- Если директория `persistent_cache.root_dir` не является той же директорией, которую использует
  llama-server `--slot-save-path`, API save/restore работает, но физическая очистка server-side
  cache-файлов требует отдельного доступа к этой директории.

## [0.1.2] — 2026-05-08

Текущая рабочая версия. Основной фокус — backend streaming для LLM, детальная
телеметрия выполнения и проверка совместимости с реальной моделью из конфигурации.

### Added

#### Backend LLM Streaming
- Добавлен внутренний streaming-контракт: `LLMStreamEvent`, расширенный `StreamChunk`,
  optional `StreamingProvider.chat_streamed()`.
- `OpenAIProvider` получил `chat_streamed()`: потоково читает chunks, собирает полный
  `LLMResponse`, сохраняет `reasoning_content`, собирает partial tool-call deltas и после
  завершения применяет обычный post-processing.
- `AgentLoop` использует backend streaming при `agent.llm_streaming_enabled: true`, если
  провайдер поддерживает `StreamingProvider`.
- Добавлен fallback: если streaming падает, запрос повторяется через обычный `chat()`.
- Tool calls по-прежнему исполняются только после полной сборки `LLMResponse`, а не по partial
  stream-delta.

#### Observability
- Добавлены trace-события `llm_stream_started`, `llm_stream_stage`, `llm_stream_delta`,
  `llm_stream_stalled`, `llm_stream_fallback`, `llm_stream_finished`.
- `llm_stream_delta` пишется только при `logging.trace_level: debug_preview|full`, чтобы
  metadata-режим не раздувал логи содержимым ответа.
- `logging.trace_level: full` теперь сохраняет полный scrubbed-текст, а не обрезает его до
  preview.
- В `agent_activity.jsonl` добавлена краткая stream-сводка: calls, fallbacks, stalls, events,
  first_event_ms, first_content_ms, first_tool_call_ms.
- В `/health` добавлены counters: `llm_stream_calls`, `llm_stream_fallbacks`,
  `llm_stream_stalls`, `llm_reasoning_chars`, `llm_content_chars`.

#### Telegram/CLI Statuses
- Telegram progress получил coarse LLM-stage статусы: reasoning, preparing tool call,
  assembling answer.
- CLI и Telegram activity logs теперь сохраняют stream summary для каждого запроса.

### Changed

- `OpenAIProvider.stream()` теперь применяет тот же preset/bootstrap kwargs path, что и `chat()`.
- Основной `llm_call_started`/`llm_call_finished` trace расширен безопасными hash/char-метриками
  для content и reasoning.
- В `config/settings.yaml` добавлены настройки:
  - `agent.llm_streaming_enabled`
  - `agent.llm_stream_stall_seconds`
  - `agent.llm_stream_max_reasoning_chars`
  - `agent.llm_stream_status_updates`

### Verified

- Реальный интеграционный запрос к текущей модели `provider=litellm`,
  `model=llama-qwen3.6-35b-a3b`, без установки `max_tokens`.
- Подтверждено, что модель отдаёт:
  - `reasoning_content` stream-delta;
  - `delta.content`;
  - partial `delta.tool_calls`;
  - `finish_reason=stop` для текста;
  - `finish_reason=tool_calls` для вызова инструмента;
  - usage tokens.
- Точечные проверки после изменений:
  - `uv run pytest tests/test_agent_loop.py tests/test_llm_advanced.py tests/test_logging_and_security.py tests/test_health.py -q`
  - `uv run ruff check ...`
  - `uv run pyright ...`

## [0.1.0] — 2026-04-17

Первый публичный релиз.

### Added

#### Ядро агента
- ReAct-цикл агента (AgentLoop) с бюджетными ограничениями (SimpleBudgetGuard) и обнаружением зацикливаний (SimpleProgressGuard)
- Параллельное выполнение инструментов (parallel_safe=True)
- Terminal-инструменты с прямым возвратом результата (без LLM-парафраза)
- ContextBuilder — 4-фазная сборка контекста с совместимостью для Qwen3.5
- 3-уровневое сжатие контекста (prune → sanitize → LLM summarize)
- Фабрика агентов `build_agent_stack()` — единая точка сборки всего стека

#### LLM-провайдеры
- OpenAI-совместимый провайдер (Ollama, vLLM, LM Studio, OpenRouter, Groq)
- Anthropic-провайдер с нативным tool calling
- XML Tool Calling — fallback-парсер для локальных LLM
- LLM Router — YAML-маршрутизация по task_kind и subagent_id
- Модельные пресеты — параметры инференса и ThinkingConfig для каждой модели
- Поддержка reasoning_content (Qwen3, Claude) и XML-тегов thinking

#### Инструменты (18 встроенных)
- read_file, write_file, edit_file, list_files, search_files — файловые операции
- exec_script — выполнение shell-команд с таймаутом
- web_fetch — HTTP-запросы с защитой от SSRF
- read_image — анализ изображений через отдельный LLM-вызов (terminal)
- memory_store, memory_recall — персистентные факты в SQLite
- normalize_excel — исправление форматирования Excel (ИНН, даты, невидимые символы)
- send_file — отправка файлов пользователю
- dispatch_subagent — делегирование субагентам (terminal)
- diff_text — сравнение текстов и файлов с выводом различий
- table_query — SQL-запросы к табличным данным (CSV, XLSX, JSON) через DuckDB
- chart_generate — генерация графиков (bar, line, pie, scatter, histogram)
- convert_format — конвертация между CSV, XLSX, JSON, Markdown
- pdf_reader — извлечение текста из PDF с поддержкой диапазонов страниц

#### Расширения
- Скиллы (4) — Markdown-инструкции с TF-IDF семантическим матчем (двуязычный RU+EN): translator, excel_normalizer, meeting_summary, data_analyst. Каждый скилл имеет `scope` для привязки к конкретному агенту.
- Плагины — subprocess-песочница с JSON-RPC через stdin/stdout
- Субагенты (5): filesystem-agent, document-agent, execution-agent, research-agent, data-agent
- MCP-интеграция — Model Context Protocol через stdio JSON-RPC
- Горячая перезагрузка скиллов (5s), плагинов (10s), MCP-серверов (10s)

#### Безопасность
- ToolGuard — 20+ YAML-правил безопасности (CRITICAL/HIGH/MEDIUM/INFO)
- Smart Approvals — LLM-оценка риска (APPROVE / DENY / ESCALATE)
- Docker-песочница — пользовательские контейнеры с лимитами ресурсов
- Network Policy — запрет сети по умолчанию с allowlist
- IPC Auth — HMAC-SHA256 + nonce с защитой от replay (300s TTL)
- Credential Scrubber — маскирование API-ключей и токенов в логах
- RBAC — 10 департаментов с инструментальными разрешениями и бюджетами

#### Каналы
- Telegram-бот с 7 командами: /start, /help, /new, /setup, /chat, /execute, /delete
- Интерактивный менеджер файлов (/delete) — безопасное удаление через inline-кнопки, без участия LLM
- Режимы взаимодействия: диалог (/chat, без инструментов) и исполнение (/execute, полный доступ)
- Индикаторы прогресса — статусные сообщения для каждого инструмента во время выполнения
- Inline-подтверждения (Smart Approvals) — кнопки «Разрешить»/«Отклонить» для опасных операций
- Rate limiting — 10 сообщений/мин на пользователя (настраиваемый)
- Загрузка файлов с валидацией (whitelist расширений, лимит 20 МБ, санитизация имён)
- Автоматическое разбиение длинных сообщений с сохранением Markdown-форматирования
- Уведомления администратора об ошибках агента
- CLI-чат для разработки

#### Память
- SQLiteMemory — асинхронная WAL, автоматическая миграция схемы
- MemoryConsolidator — LLM-сжатие с cooldown-ограничениями
- ContextCompressor — 3-уровневое сжатие для ограниченных контекстных окон

#### Калибровка
- Автоматическая калибровка промптов и few-shots под конкретную модель
- 20+ тестовых сценариев (tool_use, no_tool, multi_step, error_recovery)
- Итеративное улучшение через облачную модель

#### Онбординг
- Гибридный детерминированный Q&A + LLM-финализация профиля
- Автогенерация пользовательского профиля

#### Инфраструктура
- CI через GitHub Actions (lint → format → typecheck → test)
- 806 тестов, ~75% покрытие кода
- pyright strict mode без ошибок
- ruff линтинг и форматирование

### Security

- ToolGuard с 20+ YAML-правилами
- Docker-песочница с per-user изоляцией
- HMAC+nonce IPC-аутентификация
- Credential Scrubber для маскирования секретов
- Network Policy deny-by-default

[0.1.0]: https://github.com/Mage212/corpclaw-lite/releases/tag/v0.1.0
