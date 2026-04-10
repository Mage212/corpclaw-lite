# Calibration Phase — Адаптация окружения под локальную модель

> **Статус: РЕАЛИЗОВАНО** (2026-04-10). Код в `src/corpclaw_lite/calibration/` (1,270 строк), 14 сценариев в `config/calibration_scenarios.yaml`, CLI-команда `corpclaw-lite calibrate`.

## Summary

Одноразовый (или периодический) этап калибровки, при котором мощная облачная модель
анализирует, как малая локальная модель справляется с типовыми сценариями, и
автоматически правит конфигурируемые поверхности — промпты, описания инструментов,
инструкции скиллов, few-shot примеры — чтобы адаптировать окружение под конкретную
модель. После калибровки система работает **только на локальной модели** без облачных
зависимостей.

Вдохновлено [AutoAgent](../references/autoagent/) (thirdlayer.inc) — но вместо
правки Python-кода правятся исключительно YAML/Markdown конфигурации.

## Goals

- Автоматическая адаптация всех конфигурируемых поверхностей под конкретную локальную
  модель (Qwen, Mistral, Llama, LFM и т.д.)
- Regression testing при смене модели: прогон стандартных сценариев с подсчётом score
- Устранение ручной подгонки промптов при деплое на новую модель
- Нулевая облачная зависимость в production — облако нужно только на калибровке

---

## Архитектура: что калибруется (Edit Surface)

Калибратор правит **только** конфигурационные файлы, которые система и так загружает
при старте. Никакого Python-кода.

### Слой 1: System Prompt (`config/bootstrap/*.md`)

| Файл | Что адаптируется | Пример |
|------|-----------------|--------|
| `SOUL.md` | Упрощение формулировок, более явные constraints | «Act immediately if low-risk» → «If user asks to read a file → ALWAYS call read_file» |
| `BEHAVIOR.md` | Правила «когда tool vs текст», формат ответов | Добавление явных «ALWAYS/NEVER» директив |
| `COMPANY.md` | Не трогается (бизнес-контекст) | — |

**Точка интеграции:** `BootstrapLoader` в `config/bootstrap.py` (строка 32) уже
поддерживает hot-reload по mtime. Калиброванные версии файлов кладутся в
`config/calibrated/bootstrap/` и загружаются с приоритетом.

### Слой 2: Tool Descriptions

Текущее состояние: описания — атрибуты Python-классов (`Tool.description`,
`ToolParam.description`). Для калибровки нужен **YAML-override**:

```yaml
# config/calibrated/tool_overrides.yaml
overrides:
  search_files:
    description: "Find text in files. Give path=directory and pattern=search words."
    params:
      path:
        description: "Directory path to search in, e.g. '.' for current"
      pattern:
        description: "Text or regex to find in files"
  read_file:
    description: "Read full text content of one file. Not for images."
```

**Точка интеграции:** `ToolRegistry.to_schemas()` в `registry.py` (строка 72).
Метод строит JSON-схему для LLM — здесь нужно подмешивать override-описания
перед отдачей.

### Слой 3: Skill Instructions (`skills/*.md`)

Калиброванные версии скиллов — в `config/calibrated/skills/`. Загружаются с
приоритетом поверх оригиналов из `skills/`.

**Точка интеграции:** `SkillRegistry.load_directory()` — добавить второй вызов
с calibrated path.

### Слой 4: Few-shot Examples (новый артефакт)

Самое мощное оружие для малых моделей. Калибратор генерирует примеры «вопрос →
правильный tool_call», которые инжектируются в начало контекста:

```yaml
# config/calibrated/few_shots.yaml
model_id: "qwen2.5:7b"
examples:
  - user: "Прочитай файл report.csv"
    assistant:
      tool_calls:
        - name: "read_file"
          arguments:
            path: "report.csv"
  - user: "Какие файлы есть в текущей папке?"
    assistant:
      tool_calls:
        - name: "list_files"
          arguments:
            path: "."
  - user: "Сколько будет 2+2?"
    assistant:
      content: "4"
```

**Точка интеграции:** `ContextBuilder.build_initial()` в `context.py` (строка 107).
Few-shot примеры вставляются между system prompt и user history как пары
user/assistant сообщений.

### Слой 5: Agent Settings

Калибратор может рекомендовать изменения числовых параметров:

```yaml
# config/calibrated/settings_override.yaml
model_id: "qwen2.5:7b"
agent:
  max_steps: 20           # слабая модель чаще ошибается -> больше шагов
  max_tool_calls: 40
  max_history: 10          # меньше контекст -> меньше history
  compression:
    max_context_tokens: 6000  # под реальное окно модели
```

**Точка интеграции:** `load_settings()` в `loader.py` — мерж override поверх
базовых настроек.

---

## Компоненты реализации

### Структура файлов

```
src/corpclaw_lite/
├── calibration/
│   ├── __init__.py           # Exports
│   ├── scenarios.py          # CalibrationScenario dataclass + loader
│   ├── runner.py             # CalibrationRunner — прогон сценариев через AgentLoop
│   ├── trajectory.py         # TrajectoryRecorder — structured JSON-лог tool_calls
│   ├── scorer.py             # CalibrationScorer — сравнение expected vs actual
│   ├── analyzer.py           # CalibrationAnalyzer — вызов облачной модели
│   ├── editor.py             # ConfigEditor — применение / откат правок
│   └── loop.py               # CalibrationLoop — оркестрация полного цикла
config/
├── calibration_scenarios.yaml   # Стандартный набор тестовых сценариев
└── calibrated/                  # Результат калибровки (gitignored)
    ├── metadata.yaml            # model_id, timestamp, score, iterations
    ├── bootstrap/
    │   ├── SOUL.md
    │   └── BEHAVIOR.md
    ├── tool_overrides.yaml
    ├── few_shots.yaml
    ├── skills/                  # Калиброванные skill instructions
    └── settings_override.yaml
tests/
├── test_calibration_scenarios.py
├── test_calibration_scorer.py
├── test_calibration_runner.py
└── test_trajectory_recorder.py
```

---

## Детальный план по файлам

### Шаг 1: `calibration/scenarios.py` — Модели данных и загрузчик

```python
@dataclass
class ScenarioExpectation:
    """What we expect the agent to do."""
    tool_calls: list[str]         # Названия tools в порядке вызова
    must_read: str | None = None  # Файл, который обязательно должен быть прочитан
    contains: str | None = None   # Подстрока в финальном ответе
    has_content: bool = True      # Агент должен дать непустой ответ

@dataclass
class ScenarioSetup:
    """Filesystem state to prepare before running."""
    files: list[tuple[str, str]]  # (path, content) — создать перед запуском

@dataclass
class CalibrationScenario:
    """Single test scenario."""
    id: str
    user_message: str
    expected: ScenarioExpectation
    setup: ScenarioSetup | None = None
    category: str = "general"     # Для группировки: "tool_use", "no_tool", "multi_step"

def load_scenarios(path: Path) -> list[CalibrationScenario]:
    """Load scenarios from YAML file."""
```

**~60 строк.** Чистые dataclass-ы + YAML-загрузчик. Никаких зависимостей кроме
`pyyaml` (уже в проекте).

---

### Шаг 2: `calibration/trajectory.py` — TrajectoryRecorder

Записывает каждый шаг AgentLoop в structured формат для последующего анализа:

```python
@dataclass
class TrajectoryStep:
    """Single step in agent execution."""
    step_type: str          # "llm_call" | "tool_call" | "tool_result" | "final_answer"
    tool_name: str | None
    tool_args: dict | None
    tool_result: str | None
    content: str | None
    timestamp_ms: float

@dataclass
class Trajectory:
    """Full execution trace for one scenario."""
    scenario_id: str
    steps: list[TrajectoryStep]
    final_answer: str
    stats: RunStats
    
    def tool_calls_sequence(self) -> list[str]:
        """Return ordered list of tool names called."""
        return [s.tool_name for s in self.steps 
                if s.step_type == "tool_call" and s.tool_name]
    
    def to_dict(self) -> dict:
        """Serialize for JSON logging and cloud model analysis."""
```

**~80 строк.** Интегрируется в `AgentLoop.run()` через callback-hook — без
изменения сигнатуры существующих методов.

**Интеграция в AgentLoop:**

В `loop.py` добавляется опциональный параметр `trajectory_recorder`:

```python
async def run(
    self,
    user: User,
    message: str,
    ...,
    trajectory_recorder: TrajectoryRecorder | None = None,  # NEW
) -> tuple[str, RunStats]:
```

Recorder вызывается в трёх точках:
1. После `self._provider.chat()` — записать `llm_call`
2. После `self._registry.execute()` — записать `tool_call` + `tool_result`
3. При возврате final answer — записать `final_answer`

Это 6-8 строк вставок в существующий код, без изменения логики.

---

### Шаг 3: `calibration/scorer.py` — Подсчёт score

```python
@dataclass
class ScenarioResult:
    """Result of running one scenario."""
    scenario: CalibrationScenario
    trajectory: Trajectory
    passed: bool
    failure_reason: str | None = None

class CalibrationScorer:
    """Compare actual trajectory against expected outcome."""
    
    def score(self, scenario: CalibrationScenario, 
              trajectory: Trajectory) -> ScenarioResult:
        """Score a single scenario execution."""
        
        # Check 1: Were the expected tools called?
        actual_tools = trajectory.tool_calls_sequence()
        expected_tools = scenario.expected.tool_calls
        if not self._tools_match(expected_tools, actual_tools):
            return ScenarioResult(scenario, trajectory, passed=False,
                failure_reason=f"Expected tools {expected_tools}, got {actual_tools}")
        
        # Check 2: Was the expected file read?
        if scenario.expected.must_read:
            read_args = [s.tool_args for s in trajectory.steps 
                        if s.tool_name == "read_file"]
            if not any(scenario.expected.must_read in str(a) for a in read_args):
                return ScenarioResult(...)
        
        # Check 3: Does the answer contain expected text?
        if scenario.expected.contains:
            if scenario.expected.contains.lower() not in trajectory.final_answer.lower():
                return ScenarioResult(...)
        
        return ScenarioResult(scenario, trajectory, passed=True)
    
    def _tools_match(self, expected: list[str], actual: list[str]) -> bool:
        """Check if expected tools were called (order matters, extras allowed)."""
        # Subsequence check: expected tools must appear in order within actual
```

**~80 строк.** Чисто детерминистическая проверка — без LLM-вызовов.

---

### Шаг 4: `calibration/runner.py` — Прогон сценариев

```python
class CalibrationRunner:
    """Run calibration scenarios through a real AgentLoop."""
    
    def __init__(
        self,
        agent_loop: AgentLoop,
        user: User,
        system_prompt: str | None,
        workspace_dir: Path,
    ) -> None: ...
    
    async def run_all(
        self, scenarios: list[CalibrationScenario]
    ) -> list[ScenarioResult]:
        """Run all scenarios and return scored results."""
        results = []
        scorer = CalibrationScorer()
        
        for scenario in scenarios:
            # 1. Setup: create test files in temp workspace
            self._setup_workspace(scenario)
            
            # 2. Run through AgentLoop with TrajectoryRecorder
            recorder = TrajectoryRecorder(scenario.id)
            answer, stats = await self._agent_loop.run(
                user=self._user,
                message=scenario.user_message,
                system_prompt=self._system_prompt,
                trajectory_recorder=recorder,
            )
            
            # 3. Score
            trajectory = recorder.finalize(answer, stats)
            result = scorer.score(scenario, trajectory)
            results.append(result)
            
            # 4. Cleanup workspace
            self._cleanup_workspace()
            
            # 5. Clear memory between scenarios
            if self._agent_loop.memory:
                await self._agent_loop.memory.clear(str(self._user.id))
        
        return results
```

**~120 строк.** Ключевая деталь: между сценариями очищаются workspace и memory,
чтобы сценарии были независимыми.

---

### Шаг 5: `calibration/analyzer.py` — Анализ через облачную модель

```python
ANALYSIS_PROMPT = """You are an expert AI agent engineer. You are analyzing how a
small local LLM ({model_id}) performs as an agent with tool-calling capabilities.

Below are the FAILED scenarios — cases where the model did not call the right tools
or produced incorrect output.

Your job: suggest specific changes to the agent's configuration to help this model
perform better. You can modify:

1. SYSTEM PROMPT — the instructions given to the model
2. TOOL DESCRIPTIONS — the name and description of each tool
3. FEW-SHOT EXAMPLES — example conversations showing correct tool usage
4. AGENT SETTINGS — numeric parameters like max_steps, max_history

CURRENT SYSTEM PROMPT:
{system_prompt}

CURRENT TOOL SCHEMAS:
{tool_schemas}

FAILED SCENARIOS:
{failures_json}

PREVIOUSLY PASSED SCENARIOS (do not break these):
{passed_json}

Respond with a JSON object containing your proposed changes:
{{
  "reasoning": "Brief analysis of failure patterns",
  "changes": {{
    "system_prompt": {{
      "SOUL.md": "full new content or null to keep",
      "BEHAVIOR.md": "full new content or null to keep"
    }},
    "tool_overrides": {{
      "tool_name": {{
        "description": "new description",
        "params": {{"param_name": {{"description": "new desc"}}}}
      }}
    }},
    "few_shots": [
      {{"user": "...", "assistant": {{"tool_calls": [...], "content": "..."}}}}
    ],
    "settings": {{
      "max_steps": 20,
      "max_history": 10
    }}
  }}
}}
"""

class CalibrationAnalyzer:
    """Send failure analysis to cloud model and get proposed changes."""
    
    def __init__(self, cloud_provider: Provider) -> None:
        self._provider = cloud_provider
    
    async def analyze(
        self,
        model_id: str,
        failed: list[ScenarioResult],
        passed: list[ScenarioResult],
        current_system_prompt: str,
        current_tool_schemas: list[dict],
    ) -> dict:
        """Ask cloud model to propose configuration changes."""
        prompt = ANALYSIS_PROMPT.format(
            model_id=model_id,
            system_prompt=current_system_prompt,
            tool_schemas=json.dumps(current_tool_schemas, indent=2),
            failures_json=self._format_failures(json_serializable(failed)),
            passed_json=self._format_passed(json_serializable(passed)),
        )
        
        response = await self._provider.chat(
            messages=[{"role": "user", "content": prompt}],
            system="You are a precise AI engineering assistant. Respond only with valid JSON.",
        )
        
        return json.loads(response.content)
```

**~100 строк.** Формирует структурированный промпт с провалами и текущей
конфигурацией, парсит JSON-ответ облачной модели.

---

### Шаг 6: `calibration/editor.py` — Применение и откат правок

```python
class ConfigEditor:
    """Apply and rollback calibration changes to config files."""
    
    def __init__(self, project_root: Path) -> None:
        self._root = project_root
        self._calibrated_dir = project_root / "config" / "calibrated"
        self._backup_dir = project_root / "config" / "calibrated" / ".backup"
    
    def apply(self, changes: dict) -> None:
        """Apply proposed changes from CalibrationAnalyzer."""
        self._backup_current()
        
        # 1. System prompt overrides
        if system_prompt := changes.get("system_prompt"):
            bootstrap_dir = self._calibrated_dir / "bootstrap"
            bootstrap_dir.mkdir(parents=True, exist_ok=True)
            for filename, content in system_prompt.items():
                if content is not None:
                    (bootstrap_dir / filename).write_text(content, encoding="utf-8")
        
        # 2. Tool description overrides
        if tool_overrides := changes.get("tool_overrides"):
            path = self._calibrated_dir / "tool_overrides.yaml"
            yaml.dump({"overrides": tool_overrides}, path.open("w"), ...)
        
        # 3. Few-shot examples
        if few_shots := changes.get("few_shots"):
            path = self._calibrated_dir / "few_shots.yaml"
            yaml.dump({"examples": few_shots}, path.open("w"), ...)
        
        # 4. Settings overrides
        if settings := changes.get("settings"):
            path = self._calibrated_dir / "settings_override.yaml"
            yaml.dump({"agent": settings}, path.open("w"), ...)
    
    def rollback(self) -> None:
        """Restore previous calibration state from backup."""
    
    def save_metadata(self, model_id: str, score: float, 
                      iterations: int) -> None:
        """Save calibration metadata."""
```

**~80 строк.** Атомарные операции: backup → apply → (rollback если score упал).

---

### Шаг 7: `calibration/loop.py` — Оркестрация полного цикла

```python
class CalibrationLoop:
    """Orchestrates the full calibration cycle."""
    
    def __init__(
        self,
        local_provider_name: str,
        cloud_provider_name: str,
        scenarios_path: Path,
        project_root: Path,
        max_iterations: int = 5,
    ) -> None: ...
    
    async def run(self) -> CalibrationReport:
        """Run the full calibration loop."""
        scenarios = load_scenarios(self._scenarios_path)
        editor = ConfigEditor(self._project_root)
        
        # Build agent stack for local model
        agent_loop, user, registry, ... = self._build_stack()
        runner = CalibrationRunner(agent_loop, user, system_prompt, workspace)
        
        # Baseline run
        print(f"Running {len(scenarios)} scenarios (baseline)...")
        baseline_results = await runner.run_all(scenarios)
        best_score = self._calc_score(baseline_results)
        print(f"Baseline: {best_score.passed}/{best_score.total} ({best_score.pct:.0f}%)")
        
        # Get cloud provider for analysis
        cloud_provider = self._get_cloud_provider()
        analyzer = CalibrationAnalyzer(cloud_provider)
        
        for iteration in range(1, self._max_iterations + 1):
            failed = [r for r in baseline_results if not r.passed]
            if not failed:
                print("All scenarios passed!")
                break
            
            # Analyze failures
            print(f"\nIteration {iteration}: analyzing {len(failed)} failures...")
            proposed = await analyzer.analyze(
                model_id=self._local_model_id,
                failed=failed,
                passed=[r for r in baseline_results if r.passed],
                current_system_prompt=system_prompt,
                current_tool_schemas=registry.to_schemas(),
            )
            
            # Apply changes
            print(f"  Proposed: {proposed['reasoning']}")
            editor.apply(proposed["changes"])
            
            # Rebuild stack with new config and rerun
            agent_loop, _, registry, ... = self._build_stack()
            runner = CalibrationRunner(agent_loop, user, new_system_prompt, workspace)
            new_results = await runner.run_all(scenarios)
            new_score = self._calc_score(new_results)
            
            # Keep or discard
            if new_score.passed > best_score.passed:
                print(f"  ✅ KEEP: {new_score.passed}/{new_score.total} "
                      f"(+{new_score.passed - best_score.passed})")
                best_score = new_score
                baseline_results = new_results
            elif (new_score.passed == best_score.passed and 
                  self._is_simpler(proposed["changes"])):
                print(f"  ✅ KEEP (simpler): {new_score.passed}/{new_score.total}")
                best_score = new_score
                baseline_results = new_results
            else:
                print(f"  ❌ DISCARD: {new_score.passed}/{new_score.total}")
                editor.rollback()
        
        # Save final metadata
        editor.save_metadata(
            model_id=self._local_model_id,
            score=best_score.pct,
            iterations=iteration,
        )
        
        return CalibrationReport(
            model_id=self._local_model_id,
            baseline=baseline_score,
            final=best_score,
            iterations=iteration,
        )
```

**~150 строк.** Чистый hill-climbing цикл по паттерну AutoAgent.

---

### Шаг 8: Интеграция в существующий код

#### 8a. `BootstrapLoader` — приоритет calibrated

В `config/bootstrap.py`, метод `get_system_prompt()`:

```python
def get_system_prompt(self, extras: dict[str, str] | None = None) -> str:
    # NEW: check calibrated directory first
    calibrated_dir = self._dir.parent / "calibrated" / "bootstrap"
    
    parts: list[str] = []
    for path in sorted(self._dir.glob("*.md")):
        # Use calibrated version if exists
        calibrated_path = calibrated_dir / path.name
        source = calibrated_path if calibrated_path.exists() else path
        content = self._load_cached(source)
        if content.strip():
            parts.append(content.strip())
    ...
```

**~8 строк изменений** в существующем файле.

#### 8b. `ToolRegistry.to_schemas()` — override описаний

В `extensions/tools/registry.py`:

```python
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._description_overrides: dict[str, dict] = {}  # NEW
    
    def load_overrides(self, path: Path) -> None:  # NEW
        """Load tool description overrides from YAML."""
        ...
    
    def to_schemas(self) -> list[dict[str, Any]]:
        for tool in self._tools.values():
            # Use override description if available
            override = self._description_overrides.get(tool.name)
            description = override["description"] if override else tool.description
            ...
```

**~25 строк изменений** в существующем файле.

#### 8c. `ContextBuilder.build_initial()` — few-shot injection

В `agent/context.py`:

```python
@classmethod
def build_initial(
    cls,
    user: User,
    message: str,
    history: list[dict[str, Any]] | None = None,
    system_prompt_override: str | None = None,
    few_shots: list[dict[str, Any]] | None = None,  # NEW
) -> ContextBuilder:
    ...
    builder = cls(system_prompt=system)
    
    # NEW: inject few-shot examples before history
    for shot in few_shots or []:
        builder.add_user_message(shot["user"])
        if "tool_calls" in shot.get("assistant", {}):
            # Simulate tool call/result pair
            ...
        else:
            builder.add_assistant_message(shot["assistant"]["content"])
    
    for item in history or []:
        ...
```

**~15 строк изменений** в существующем файле.

#### 8d. `load_settings()` — мерж override

В `config/loader.py`:

```python
def load_settings(path: Path | str | None = None) -> Settings:
    ...
    settings = Settings.model_validate(interpolated)
    
    # NEW: merge calibrated overrides if present
    calibrated = yaml_path.parent / "calibrated" / "settings_override.yaml"
    if calibrated.exists():
        override_data = yaml.safe_load(calibrated.read_text())
        if override_data and "agent" in override_data:
            merged = {**settings.agent.model_dump(), **override_data["agent"]}
            settings.agent = AgentSettings.model_validate(merged)
    
    return settings
```

**~10 строк изменений** в существующем файле.

#### 8e. CLI команда `calibrate`

В `cli.py`, добавить subparser и handler:

```python
# В _build_parser():
cal_p = sub.add_parser("calibrate", help="Calibrate config for local model")
cal_p.add_argument("--local-provider", default="default",
                    help="Named provider for local model")
cal_p.add_argument("--cloud-provider", default="cloud",
                    help="Named provider for cloud analyzer")
cal_p.add_argument("--scenarios", default="config/calibration_scenarios.yaml",
                    help="Path to calibration scenarios")
cal_p.add_argument("--max-iterations", type=int, default=5,
                    help="Max calibration iterations")
cal_p.add_argument("--reset", action="store_true",
                    help="Clear previous calibration before starting")
cal_p.add_argument("--dry-run", action="store_true",
                    help="Run scenarios only, don't calibrate")

# Handler:
def cmd_calibrate(args) -> None:
    from corpclaw_lite.calibration.loop import CalibrationLoop
    loop = CalibrationLoop(
        local_provider_name=args.local_provider,
        cloud_provider_name=args.cloud_provider,
        scenarios_path=Path(args.scenarios),
        project_root=PROJECT_ROOT,
        max_iterations=args.max_iterations,
    )
    report = asyncio.run(loop.run())
    print(f"\nCalibration complete: {report.final.passed}/{report.final.total}")
```

**~50 строк изменений** в существующем файле.

---

### Шаг 9: Стандартные сценарии

```yaml
# config/calibration_scenarios.yaml
#
# Standard calibration scenarios covering core agent capabilities.
# Categories:
#   tool_use     — must call specific tool(s)
#   no_tool      — must answer without tools
#   multi_step   — requires multiple sequential tool calls
#   error_recovery — must handle tool errors gracefully

scenarios:
  # ── Basic Tool Use ──────────────────────────────────────────────
  - id: read_file_basic
    category: tool_use
    user_message: "Прочитай файл test.txt"
    setup:
      files:
        - path: "test.txt"
          content: "Hello World"
    expected:
      tool_calls: ["read_file"]
      has_content: true

  - id: list_files_basic
    category: tool_use
    user_message: "Какие файлы есть в текущей папке?"
    setup:
      files:
        - path: "readme.md"
          content: "# Project"
        - path: "data.csv"
          content: "a,b,c"
    expected:
      tool_calls: ["list_files"]
      has_content: true

  - id: write_file_basic
    category: tool_use
    user_message: "Создай файл hello.txt с текстом 'Привет мир'"
    expected:
      tool_calls: ["write_file"]
      has_content: true

  - id: search_files_basic
    category: tool_use
    user_message: "Найди все файлы, содержащие слово 'TODO'"
    setup:
      files:
        - path: "main.py"
          content: "# TODO: fix this\nprint('hello')"
        - path: "utils.py"
          content: "def helper(): pass"
    expected:
      tool_calls: ["search_files"]
      contains: "TODO"

  # ── No Tool Needed ─────────────────────────────────────────────
  - id: math_question
    category: no_tool
    user_message: "Сколько будет 15 * 7?"
    expected:
      tool_calls: []
      contains: "105"

  - id: greeting
    category: no_tool
    user_message: "Привет!"
    expected:
      tool_calls: []
      has_content: true

  - id: factual_question
    category: no_tool
    user_message: "Что такое JSON?"
    expected:
      tool_calls: []
      has_content: true

  # ── Multi-Step ─────────────────────────────────────────────────
  - id: list_then_read
    category: multi_step
    user_message: "Посмотри какие файлы есть и прочитай первый."
    setup:
      files:
        - path: "alpha.txt"
          content: "First file content"
        - path: "beta.txt"
          content: "Second file content"
    expected:
      tool_calls: ["list_files", "read_file"]
      has_content: true

  - id: read_and_edit
    category: multi_step
    user_message: "Прочитай config.txt и замени 'debug=true' на 'debug=false'"
    setup:
      files:
        - path: "config.txt"
          content: "mode=production\ndebug=true\nlog=info"
    expected:
      tool_calls: ["read_file", "edit_file"]
      has_content: true

  # ── Error Recovery ─────────────────────────────────────────────
  - id: read_nonexistent
    category: error_recovery
    user_message: "Прочитай файл data.csv"
    # No setup — file doesn't exist
    expected:
      tool_calls: ["read_file"]
      has_content: true  # Should report the error to user

  # ── Web Fetch ──────────────────────────────────────────────────
  - id: web_fetch_basic
    category: tool_use
    user_message: "Скачай содержимое https://httpbin.org/get"
    expected:
      tool_calls: ["web_fetch"]
      has_content: true

  # ── Script Execution ───────────────────────────────────────────
  - id: exec_script_basic
    category: tool_use
    user_message: "Выполни скрипт Python: print(2 + 2)"
    expected:
      tool_calls: ["exec_script"]
      contains: "4"
```

---

### Шаг 10: Тесты

#### `test_calibration_scenarios.py`
- Загрузка YAML → dataclass-ы
- Валидация обязательных полей
- Невалидный YAML → понятная ошибка

#### `test_calibration_scorer.py`
- Точное совпадение tools → passed
- Подмножество tools (часть вызвана) → failed
- Лишние tools (больше чем expected) → passed (extras allowed)
- `contains` check case-insensitive
- `must_read` check

#### `test_calibration_runner.py`
- Mock AgentLoop → проверить что runner корректно setup/cleanup workspace
- Проверить изоляцию между сценариями (memory clear)

#### `test_trajectory_recorder.py`
- Запись tool_call + result → корректная последовательность
- `to_dict()` → JSON-serializable
- `tool_calls_sequence()` → только имена tools

**~200 строк тестов** суммарно.

---

## Объём работы

| Компонент | Строки (новый код) | Строки (изменения в существующем) |
|-----------|--------------------|----------------------------------|
| `calibration/scenarios.py` | ~60 | — |
| `calibration/trajectory.py` | ~80 | ~8 (loop.py) |
| `calibration/scorer.py` | ~80 | — |
| `calibration/runner.py` | ~120 | — |
| `calibration/analyzer.py` | ~100 | — |
| `calibration/editor.py` | ~80 | — |
| `calibration/loop.py` | ~150 | — |
| Bootstrap override | — | ~8 (bootstrap.py) |
| Tool description override | — | ~25 (registry.py) |
| Few-shot injection | — | ~15 (context.py) |
| Settings override merge | — | ~10 (loader.py) |
| CLI `calibrate` command | — | ~50 (cli.py) |
| YAML сценарии | ~100 | — |
| Тесты | ~200 | — |
| **Итого** | **~970** | **~116** |

**Суммарно: ~1100 строк**, из которых ~200 — тесты и ~100 — YAML.

---

## Фазирование реализации

### Phase A: Foundation (можно сделать сейчас без облачной модели)
- [ ] `calibration/scenarios.py` — модели данных + загрузчик
- [ ] `calibration/trajectory.py` — TrajectoryRecorder
- [ ] Интеграция TrajectoryRecorder в `AgentLoop.run()`
- [ ] `calibration/scorer.py` — подсчёт score
- [ ] `calibration/runner.py` — прогон сценариев
- [ ] `config/calibration_scenarios.yaml` — стандартные сценарии
- [ ] CLI: `corpclaw-lite calibrate --dry-run` (только прогон + score)
- [ ] Тесты для scenarios, scorer, trajectory

**Ценность:** Regression testing при смене модели. Даёт числовой ответ: «на Qwen
22/25, на Mistral 18/25». Работает без облака.

### Phase B: Integration hooks (подготовка к калибровке)
- [ ] `BootstrapLoader` — fallback на calibrated/ directory
- [ ] `ToolRegistry.load_overrides()` — YAML override описаний
- [ ] `ContextBuilder` — few-shot injection
- [ ] `load_settings()` — merge override
- [ ] `calibration/editor.py` — применение/откат правок
- [ ] Тесты для интеграционных точек

**Ценность:** Даже без автоматического анализатора можно вручную положить
calibrated файлы и они подхватятся.

### Phase C: Full calibration loop
- [ ] `calibration/analyzer.py` — промпт для облачной модели
- [ ] `calibration/loop.py` — полный hill-climbing цикл
- [ ] CLI: `corpclaw-lite calibrate` (полный цикл)
- [ ] `config/calibrated/metadata.yaml` — сохранение результатов
- [ ] Integration test: полный цикл с mock cloud provider

**Ценность:** Полностью автоматическая калибровка `→ uv run corpclaw-lite calibrate`.

### Phase D: Polish
- [ ] `--skills-only` флаг для калибровки только skill instructions
- [ ] `--reset` для сброса предыдущей калибровки
- [ ] Чтение `model_id` из текущего provider для metadata
- [ ] Предупреждение при загрузке calibrated конфига, если model_id не совпадает
- [ ] Документация в README

---

## Риски и митигации

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| Облачная модель генерирует невалидный JSON | Средняя | Retry с более жёстким промптом + JSON-schema validation |
| Калиброванный промпт ломает ранее проходившие сценарии | Средняя | passed-сценарии передаются в контексте анализатору; discard при régression |
| Калибровка занимает слишком много времени | Низкая | `--max-iterations`, параллельный прогон сценариев |
| Малая модель не поддерживает tool calling вообще | Низкая | Baseline score = 0 → калибратор сфокусируется на few-shot примерах |
| Конфликт calibrated и original configs при обновлении проекта | Средняя | `metadata.yaml` хранит версию; предупреждение при загрузке |

---

## Status

- [ ] Phase A: Foundation
- [ ] Phase B: Integration hooks
- [ ] Phase C: Full calibration loop
- [ ] Phase D: Polish

## Notes

- Инспирировано AutoAgent (thirdlayer.inc) — см. `references/autoagent/`
- Ключевое отличие: правим YAML/Markdown, не Python-код → safe by design
- Calibrated configs в `config/calibrated/` добавить в `.gitignore` (привязаны к
  конкретной модели и машине)
- Минимальный прототип (Phase A) работает **без облачной модели** — чистый
  regression testing
