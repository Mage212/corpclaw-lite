# Verified Code & Security Review — Master Register

> Date: 2026-07-22
> Baseline: `pre-release` at `975448a`
> Purpose: canonical, code-traced register for the July 2026 review and its Sprint 1–2 fixes.

## Reading and status rules

Every finding keeps its original evidence after remediation. Status values are:
`verified`, `reduced`, `rejected`, and `fixed`. Severity describes the verified impact,
not the wording of an external report. A finding becomes `fixed` only after its regression
tests and the full repository gate pass.

Historical quality snapshot supplied with the review: Ruff passed, Pyright reported 0 errors,
the full suite reported 1953 passed / 1 skipped, and Bandit reported no High findings. Dependency
audit reported 22 advisories across `mcp`, `pydantic-settings`, and Pillow; no reachable
exploit was established and upgrades remain Sprint 3 work.

## Sprint 1 register

### S1-01 — Nested symlink disclosure during web directory copy

- **Severity/status:** High / fixed in `4199da9`.
- **Boundary:** host workspace isolation.
- **Trace:** `channels/web/files.py::copy_paths` validated only the source root and then used
  `copytree(..., symlinks=False)`, which dereferenced every nested link.
- **Impact/preconditions:** an authenticated user able to create a link in their workspace
  could copy an arbitrary host-readable target into a downloadable regular file. The attack
  was reproduced with a nested link to a file outside the workspace.
- **Root cause:** validation and copy had different recursion semantics.
- **Decision:** reject all links in copied trees; preserve rather than follow any link that
  appears after the pre-scan; post-scan and remove partial output on failure.
- **Rejected alternatives:** resolving links during a pre-scan alone leaves a TOCTOU window;
  silently ignoring links creates incomplete copies. Preserving during copy plus rejecting the
  destination is fail-closed under the race while keeping ordinary `copy2` metadata semantics.
- **Acceptance:** external and in-workspace links are both rejected, no destination remains,
  and ordinary recursive copy/unique naming still work.
- **Regression evidence:**
  `test_web_copy_rejects_nested_symlinks_and_cleans_destination` and
  `test_web_copy_rejects_nested_external_symlink` reproduce both disclosure variants.

### S1-02 — Partial department overlay expands omitted allowlists

- **Severity/status:** High / fixed in `4199da9`.
- **Boundary:** department RBAC configuration.
- **Trace:** `DepartmentConfig` materialised omitted fields as `["*"]` before union merge.
- **Impact/preconditions:** a legitimate private overlay changing only budget/profile could
  silently turn a restricted base department into wildcard access.
- **Root cause:** parsing defaults were confused with overlay patch semantics.
- **Decision:** omitted overlay fields inherit; explicitly present allowlists union; explicit
  wildcard alone expands access. Parse a complete file before replacing live state.
- **Rejected alternative:** replacing the documented union contract with intersection would be
  safer in isolation but would silently break existing private overlays. Presence-aware union
  fixes the defect without changing that public contract.
- **Acceptance:** budget-only overlay preserves description/profile/all allowlists; invalid
  fields leave the previous manager state intact.
- **Regression evidence:** `test_budget_only_overlay_inherits_profile_and_all_allowlists` and
  `test_invalid_overlay_is_atomic`; parser validation covers description, profile, allowlists
  and budget types before replacing live state.

### S1-03 — Department skill allowlist was not applied to prompt selection

- **Severity/status:** High / fixed in `4199da9`.
- **Boundary:** RBAC-controlled system-prompt content.
- **Trace:** `SkillRegistry.get_allowed_skills` checked only `Skill.allowed_for`; the existing
  `PermissionChecker.can_use_skill` was not on the production selection path.
- **Impact:** instructions for a department-denied skill could enter the agent prompt. Plugin
  and MCP runtime/schema checks already exist and are retained as regression contracts.
- **Precondition/root cause:** the department must restrict `allowed_skills` while the skill's
  own `allowed_for` permits the user; production bootstrap constructed a registry without the
  configured `PermissionChecker`.
- **Decision:** inject `PermissionChecker` into the registry and intersect both policies.
- **Rejected alternative:** filtering later in prompt rendering would leave multiple call sites
  with different policy semantics. Registry-level filtering is the shared production boundary.
- **Compatibility:** API wildcard defaults remain; shipped departments explicitly record
  their current effective skill/plugin/MCP wildcards and omitted base wildcards warn.
- **Regression evidence:**
  `test_skill_registry_intersects_skill_and_department_permissions`; existing plugin/MCP scope
  tests verify schema filtering and direct-execution rejection through
  `can_use_registered_tool`.

### S1-04 — Whitelist/revocation cache is stale across processes

- **Severity/status:** High / fixed in `0c5bcde`.
- **Boundary:** Telegram authentication and emergency revocation.
- **Trace:** each `UserManager` cached `whitelist.json` and `revoked_sessions.json` forever.
- **Impact:** a running channel could continue accepting a user after a separate CLI process
  revoked or denied access. Reproduced with two managers created before the mutation.
- **Root cause:** process-local, unversioned caches treated JSON files as mutable shared state.
- **Decision:** SQLite is the sole runtime source. Import legacy JSON exactly once under a
  transaction and retain files only as rollback artifacts.
- **Rejected alternative:** mtime-based cache invalidation still has races and two-file
  consistency problems. SQLite gives one transactional authority without changing public APIs.
- **Reproduction/result:** two managers opened before deny/unrevoke now observe the mutation on
  the next call. `auth_json_import_v1` is written in the same `BEGIN IMMEDIATE` transaction as
  `INSERT OR IGNORE`; malformed input fails before the transaction and marker.
- **Regression evidence:** `test_two_existing_managers_observe_removal`,
  `test_two_existing_managers_observe_unrevoke`,
  `test_legacy_json_imports_once_and_is_not_reimported`, and
  `test_invalid_legacy_json_fails_closed_without_marker`.

### S1-05 — Credential scrubber misses modern tokens and formatted traceback

- **Severity/status:** High / fixed in `0c5bcde`.
- **Boundary:** logs, payload capture, tool-result confidentiality.
- **Trace:** patterns omitted modern token prefixes. Logging filters run before a formatter
  creates traceback text, so `logger.exception` leaked a matching `sk-...` secret.
- **Impact/precondition:** a secret must appear in a logged argument, exception message or stack
  context while the relevant logging/capture path is enabled; the resulting local log then
  becomes a secondary credential store.
- **Root cause:** coverage stopped at pre-format `LogRecord` fields and an outdated token list.
- **Decision:** expand shared patterns and scrub the formatter's final output.
- **Rejected alternative:** extending only the filter cannot clean traceback text created by
  `Formatter.formatException`; suppressing tracebacks would materially reduce diagnostics.
- **Acceptance:** message arguments, exception messages and traceback lines contain only the
  redaction marker for all supported token families.
- **Regression evidence:** parametrised modern-token test plus
  `test_formatter_scrubs_exception_traceback`. Root, activity, trace and payload handlers use
  the final-output scrubbing formatter; payload/tool-result paths share `scrub_text`.

### S1-06 — Native tool calls bypass the offered schema

- **Severity/status:** High correctness/policy boundary / fixed in `71b951c`.
- **Boundary:** phase/closing-mode tool surface offered to the model.
- **Trace:** XML parsing receives `allowed_tool_names`; `_tool_calls_from_native` accepted any
  SDK-provided name. Execution-time department RBAC remained active, so this was not a full
  permission bypass, but closing/phase/tool-surface restrictions could be bypassed.
- **Precondition/root cause:** an OpenAI-compatible backend must return a native call for a
  registry tool omitted from the current schema; native and XML normalization had diverged.
- **Decision:** filter native calls by the exact offered schema and trace rejected names
  without their arguments.
- **Rejected alternative:** relying only on registry RBAC does not enforce temporary workflow
  restrictions. Logging arguments was also rejected because they may contain secrets.
- **Regression evidence:** `test_native_tool_call_outside_offered_schema_is_rejected`; allowed
  native and XML paths remain covered by their existing provider tests. A fully filtered empty
  response continues through the AgentLoop's bounded empty-response retry.

### S1-07 — Queue cancellation can leak a sticky slot lock

- **Severity/status:** High availability / fixed in `71b951c`.
- **Boundary:** shared LLM inference capacity and sticky-slot availability.
- **Trace:** cancellation released `selected_slot.lock` only inside the
  `semaphore_acquired` branch. Cancellation after slot-lock acquisition but while waiting for
  the global semaphore permanently blocked that slot. The sequence was reproduced.
- **Precondition/root cause:** slot affinity is enabled, the global semaphore is saturated, and
  the waiter is cancelled in the partial-acquisition window; cleanup incorrectly modelled two
  independent resources as a single acquisition state.
- **Decision:** track and release every acquired resource independently and idempotently.
- **Rejected alternative:** changing lock acquisition order moves rather than removes the
  cancellation windows and can weaken affinity scheduling.
- **Regression evidence:** `test_cancel_after_slot_lock_before_semaphore_releases_both` plus
  the existing blocked-waiter and double-release tests. Waiting/active lists and a newly owned
  sticky assignment are cleared only for the cancelled entry.

### S1-08 — Terminal tool persists an orphaned tool call

- **Severity/status:** High correctness / fixed in `a5503ff`.
- **Boundary:** durable LLM transcript and provider tool-call protocol.
- **Trace:** assistant tool calls were persisted, while terminal results skipped the `tool`
  role and returned directly. Reloaded history violated OpenAI-compatible tool protocol.
- **Precondition/root cause:** a successful single terminal tool runs with `session_id`; direct
  user return was incorrectly treated as permission to omit the protocol result from storage.
- **Decision:** always persist assistant-call → tool-result → final-assistant. Terminal status
  controls loop termination only.
- **Rejected alternative:** rewriting the call as plain assistant text loses audit fidelity and
  makes later provider replay differ from the trajectory that actually executed.
- **Regression evidence:** `test_terminal_tool_no_double_persist` asserts the exact restored
  role sequence and a single existing tools-note; the next provider request therefore receives
  a complete OpenAI-compatible call/result pair.

### S1-09 — Scheduler completion overwrites pause/dismiss with stale state

- **Severity/status:** High correctness / fixed in `a5503ff`.
- **Boundary:** durable user intent and scheduler ownership across workers/channels.
- **Trace:** a worker claimed a task, ran for an extended period, then wrote its stale full
  object through an update without checking current status or claim token.
- **Impact:** a concurrent user pause/dismiss could be resurrected.
- **Precondition/root cause:** user mutation occurs between claim and worker completion; the
  final write used object replacement rather than ownership-checked field updates.
- **Decision:** atomic `complete_claim` keyed by claim token; preserve user-controlled state
  while recording outcome and clearing the owned claim.
- **Rejected alternative:** status-only optimistic locking cannot distinguish an expired old
  worker from a newer active claim. The claim token is the precise execution ownership key.
- **Regression evidence:** `test_user_state_change_wins_while_claim_runs` covers pause and
  dismiss; `test_stale_worker_cannot_complete_newer_claim` covers token replacement; existing
  once/recurring scheduler tests preserve normal scheduling behaviour.

## Deferred verified register

### Sprint 2 — LLM and extension contracts

#### S2-01 — Runtime execution context leaks into plugin/MCP business arguments

- **Severity/status:** High when extensions are enabled / verified.
- **Boundary:** host runtime identity and callbacks versus an extension's declared tool schema.
- **Trace:** `ToolRegistry.execute` adds `user`, `run_id`, four subagent callbacks and the parent
  trajectory recorder to `arguments`; `PluginToolProxy.execute` serialises the resulting mapping
  as JSON-RPC, while `MCPToolAdapter.execute` forwards its remaining kwargs to the MCP server.
- **Impact/preconditions:** plugin calls can fail on non-serialisable `User`/function objects;
  MCP receives undeclared internal values. The extension must be enabled and selected.
- **Root cause:** runtime control data and LLM-authored business input share one kwargs channel.
- **Decision:** bind immutable `ToolExecutionContext` with `contextvars`; pass only original tool
  arguments to `Tool.execute`; migrate built-ins to the context getter while preserving explicit
  direct-call arguments as a compatibility fallback.
- **Rejected alternatives:** source-kind filtering in the registry would create two execution
  contracts; JSON-coercing runtime objects would preserve the leakage and expose internals.
- **Acceptance:** plugin/MCP observe exactly schema-declared arguments; context is isolated across
  concurrent calls; RBAC still runs before execution; existing direct built-in calls work.
- **Regression evidence:** pending Sprint 2 PR 1.

#### S2-02 — Anthropic history is sent in OpenAI tool-call format

- **Severity/status:** High correctness when Anthropic is enabled / verified.
- **Boundary:** canonical internal transcript versus provider-native wire protocol.
- **Trace:** `ContextBuilder` stores assistant `tool_calls` followed by role `tool`, and
  `AnthropicProvider.chat` passes `messages` directly to `messages.create` without converting
  them to `tool_use`/`tool_result` content blocks.
- **Impact/preconditions:** the first Anthropic tool call may parse, but the following ReAct turn
  has an invalid provider history and fails or loses its tool-result association.
- **Root cause:** only outbound tool schemas and response blocks were translated; history was not.
- **Decision:** add a strict canonical-to-Anthropic converter, batch consecutive tool results,
  preserve mixed text/tool blocks, reject malformed stored arguments before network I/O, and
  parse native thinking/tool blocks back into `LLMResponse`.
- **Rejected alternatives:** provider-specific history in `ContextBuilder` would contaminate the
  durable canonical transcript; flattening calls to text loses protocol and audit fidelity.
- **Acceptance:** deterministic user → tool_use → tool_result → final exchanges work, including
  multiple results and error results; OpenAI-compatible history remains unchanged.
- **Regression evidence:** pending Sprint 2 PR 3.

#### S2-03 — Compression can reinsert system roles into provider history

- **Severity/status:** High correctness / verified.
- **Boundary:** separately transported trusted system prompt versus durable chat transcript.
- **Trace:** `_save_turn` appends a role=`system` tools marker to `ChatContextStore`;
  `_compress_store_transcript` compresses the raw store; `_maybe_compress_mid_run` assigns the
  returned list directly to `state.context.messages`, bypassing `build_from_full_history`'s
  system extraction.
- **Impact/preconditions:** a session-bound conversation that compresses mid-run can send system
  messages inside `messages` to templates/providers that require system to be separate.
- **Root cause:** no canonical role invariant exists at store/compressor boundaries.
- **Decision:** durable/provider transcripts allow only user/assistant/tool; remove new tools
  markers, normalise before and after compression, and clean legacy system rows on rewrite.
- **Rejected alternatives:** extracting system only during initial load leaves the mid-run path
  inconsistent; retaining markers duplicates structured calls/results.
- **Acceptance:** manual and mid-run compression never return or store role=`system`, including
  noop compression of legacy rows, while complete tool pairs survive.
- **Regression evidence:** pending Sprint 2 PR 2.

#### S2-04 — Main-agent closing mode can force an unrelated terminal tool

- **Severity/status:** Medium/High correctness / verified.
- **Boundary:** deadline adaptation versus the tool surface offered for the user's task.
- **Trace:** both closing-mode call sites derive terminal names from every registered tool with
  `terminal=True`; on the main agent the only such tool may be `read_image`, so a text task is
  narrowed to an image-only schema after the soft deadline.
- **Impact/preconditions:** a long main-agent request crosses the soft deadline while an unrelated
  terminal tool is registered; the model can no longer complete with the appropriate tools.
- **Root cause:** a generic tool attribute was treated as an explicit workflow-finalisation
  contract.
- **Decision:** closing mode narrows schemas only for an active `TerminalToolMandate`, using its
  declared terminal tool/prerequisites. Main-agent closing still emits telemetry and disables
  thinking but preserves its task tool surface.
- **Rejected alternatives:** removing closing mode entirely would restore hard-timeout failures
  for research workflows; selecting any registry terminal repeats the ambiguity.
- **Acceptance:** main text tasks never become read-image-only; configured research workflows
  still narrow to their declared finalisation funnel.
- **Regression evidence:** pending Sprint 2 PR 4.

#### S2-05 — Persisted user data is promoted to authoritative system text

- **Severity/status:** High safety / verified.
- **Boundary:** administrator policy versus user-controlled and model-generated persistent data.
- **Trace:** `_assemble_user_layers` joins onboarding user Markdown, personal instructions and
  tone with the department prompt; recalled facts, name, recent files and pins are then
  interpolated into `dynamic_prompt`, passed as `system_prompt_override`.
- **Impact/preconditions:** content stored through onboarding/memory/profile/file context can be
  interpreted as higher-authority instructions on every later request.
- **Root cause:** prompt composition grouped data by persistence, not by trust authority.
- **Decision:** keep SOUL/company/department/admin skills in system; render identity, onboarding,
  preferences, facts and file context as structured untrusted user-level data combined with the
  current request and rebuilt each turn without durable duplication.
- **Rejected alternatives:** Markdown fences inside system do not change model authority;
  deleting personalisation would discard intended product behaviour.
- **Acceptance:** adversarial persisted strings appear only in the user message, trusted layers
  remain system, and generated context does not accumulate in `ChatContextStore`.
- **Regression evidence:** pending Sprint 2 PR 2.

#### S2-06 — Tool batches and auto-finalize bypass deterministic execution invariants

- **Severity/status:** Medium / verified.
- **Boundary:** resource limits, ToolGuard/RBAC and terminal side-effect ordering.
- **Trace:** the loop checks the current budget and only then adds the whole batch count, allowing
  overshoot; `_can_parallelize` trusts `parallel_safe` even for terminal tools; auto-finalize
  Stage C calls `tool.execute` directly rather than the guarded registry path.
- **Impact/preconditions:** a final batch can exceed the configured tool budget; a misdeclared
  terminal plugin can race sibling calls; programmatic finalisation can skip normal policy.
- **Root cause:** admission, execution and salvage use separate partial contracts.
- **Decision:** reserve complete batches before persistence/execution; reject mixed terminal
  batches with protocol-valid error results; make all terminal calls non-parallel; route both
  auto-finalize stages through `_execute_single_tool` and persist complete exchanges.
- **Rejected alternatives:** truncating a batch changes model intent and leaves orphan calls;
  duplicating a reduced guard inside auto-finalize will drift from the main path.
- **Acceptance:** no partial or over-budget execution; terminal calls run alone; deny/approval
  applies to auto-finalize; successful salvage stores assistant-call → tool-result → assistant.
- **Regression evidence:** pending Sprint 2 PR 1.

#### S2-07 — Loop-exhaustion fallback is saved after context teardown

- **Severity/status:** Medium correctness / verified.
- **Boundary:** request lifecycle versus durable per-session transcript targeting.
- **Trace:** the main `try/finally` calls `_finalize_turn`, which resets the context target; code
  after that `finally` invokes `_save_turn` for `_LOOP_FALLBACK`, so `_persist_context_msg` sees no
  session/user and silently performs no write.
- **Impact/preconditions:** a session-bound run exits its loop via guard recovery `break`; the
  user sees a fallback answer that is absent after chat reload.
- **Root cause:** response persistence occurs outside the lifetime that owns its storage target.
- **Decision:** persist and trace loop exhaustion before entering the common epilogue; keep the
  epilogue in `finally` for success, exception and cancellation cleanup.
- **Rejected alternatives:** passing session IDs directly only for this answer creates a second
  persistence contract; delaying all resets risks context leakage on exceptions.
- **Acceptance:** fallback is durable for session runs, absent for sessionless CLI/subagents, and
  every contextvar is reset after return or failure.
- **Regression evidence:** pending Sprint 2 PR 4.

#### S2-08 — Anthropic request options and streaming diverge from chat

- **Severity/status:** Medium / verified.
- **Boundary:** provider-independent sampling/phase policy and bounded response accumulation.
- **Trace:** Anthropic ignores `get_request_options`; sampling budget only adjusts `max_tokens`;
  `stream` bypasses profiles, tools and thinking, and no `chat_streamed` implementation exists.
- **Impact/preconditions:** Anthropic routes do not honour closing/research phase thinking policy;
  enabling internal streaming silently changes request semantics and lacks bounded accumulation.
- **Root cause:** Anthropic pre-dates the split model/sampling/request and StreamingProvider
  contracts.
- **Decision:** one request builder for chat/image/stream; native Anthropic thinking mapping with
  documented priority; implement full streamed accumulation/events and hard character bounds.
- **Rejected alternatives:** disabling Anthropic streaming leaves provider behaviour divergent;
  relying solely on `max_tokens` does not defend against a non-conforming endpoint.
- **Acceptance:** chat and streamed chat have equivalent params/results, phase overrides win,
  fragmented tool JSON is assembled before exposure, and limit breaches fail closed.
- **Regression evidence:** pending Sprint 2 PR 3.

### Sprint 3 — Operational hardening

- Revalidate existing container image/network/mount/capability/seccomp settings before reuse;
  fail startup when strict seccomp is missing; add workspace/output limits.
- Remove the IPC secret from process argv and bound worker/MCP/plugin line/output sizes.
- Enforce an explicit Telegram private/group policy; sanitise channel errors and approval keys.
- Invalidate web sessions on password change; address login timing, request/body limits,
  trusted-proxy configuration, mutation rate limits and synchronous DB middleware.
- Bound web fetch timeout; retain HTTPS DNS-rebinding as a residual blind/TOCTOU hardening item,
  not a proven internal-content read.
- Make calibration filenames contained and apply/rollback/resource cleanup atomic.
- Fix `memory-worker run`, bound stored fact size, and replace the O(N) cue boost.
- Use `extra="forbid"` for safety-critical nested settings; pin Docker dependencies and upgrade
  audited packages; move onboarding personal data outside the public repository tree.
- Improve ToolGuard deletion variants, plugin-name ownership and container disk/swap/init limits.

## Reduced or rejected external claims

- **Rejected:** critical plugin/MCP RBAC bypass through `IPCToolProxy`. Production factory wraps
  built-ins before extensions; plugins and MCP use their own scoped adapters. Their actual
  defect is S2-01.
- **Reduced:** IPC secret in argv is Medium defense-in-depth, not Critical; an observer with
  Docker/process authority is already near host compromise.
- **Reduced:** container name pre-creation requires Docker authority; revalidation remains
  operational hardening rather than a remote High exploit.
- **Rejected as stated:** HTTPS rebinding does not yield internal response content unless the
  internal endpoint also presents a certificate valid for the attacker-controlled hostname.
- **Rejected in production:** `NetworkPolicy=None`; the production factory always constructs a
  policy, though the constructor default remains a low-level API footgun.
- **Rejected:** container lock-pruning race on the event-loop path; there is no suspension
  between obtaining the lock and entering it.
- **Reduced:** pattern-only `GuardRule` is a latent API defect, but every current regex rule has
  `match_param`; no current policy bypass was found.
- **Rejected as broad claim:** normal static external symlinks are rejected by resolved boundary
  checks. S1-01 concerned nested traversal during copy.
- **By design:** smart approval is explicit opt-in; trusted plugins/MCP are administrator code;
  reasoning-to-content supports local backends; research remains a stateful core subsystem.

## Delivery log

| Package | Branch | Status | Verification |
|---|---|---|---|
| PR 1 filesystem/RBAC | `codex/security-s1-filesystem-rbac` | fixed (`4199da9`) | targeted + final gate |
| PR 2 auth/logging | `codex/security-s1-auth-logging` | fixed (`0c5bcde`) | targeted + final gate |
| PR 3 native/queue | `codex/security-s1-native-tools-queue` | fixed (`71b951c`) | targeted + final gate |
| PR 4 transcript/scheduler | `codex/security-s1-transcript-scheduler` | fixed (`a5503ff`) | targeted + final gate |

## Final verification and rollout record

- `uv run ruff format src/ tests/ --check`: passed.
- `uv run ruff check src/ tests/`: passed.
- `uv run pyright src/`: 0 errors; 16 pre-existing matplotlib typing warnings.
- `uv run pytest tests/ -q`: 1952 passed, 1 skipped, 1 aiohttp warning in 362.85 seconds
  on the final committed code tree.
  This is the actual branch result; the historical review snapshot above is retained separately.
- `uvx bandit -r src/ -q -lll`: passed, zero High findings. The full Bandit scan contains only
  the already reviewed Low/Medium findings.
- Clean-database and legacy-JSON migration, cross-manager deny/revoke, symlink copy, native/XML
  tool calls, terminal persistence and scheduler races were exercised deterministically.
- Telegram/Web live smoke was not run because this checkout has no pilot credentials/services.
  Live llama.cpp smoke remains desirable but non-blocking by the agreed Definition of Done.
- Legacy JSON files are intentionally left unchanged. No dependency upgrade or Sprint 2/3 item
  was pulled into this change set.

The pilot assumption is OpenAI-compatible llama.cpp with built-in tools and shipped subagents.
Anthropic, MCP and custom plugins are not pilot-ready until Sprint 2.
