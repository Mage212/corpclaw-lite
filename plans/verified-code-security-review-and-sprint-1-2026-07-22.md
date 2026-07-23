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

- **Severity/status:** High when extensions are enabled / fixed in `78244f8`.
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
- **Regression evidence:** `test_tool_execution_context.py` verifies exact plugin/MCP payloads,
  concurrent ContextVar isolation, direct built-in compatibility and registry RBAC. Full Sprint 2
  gate: 2003 passed / 1 skipped.

#### S2-02 — Anthropic history is sent in OpenAI tool-call format

- **Severity/status:** High correctness when Anthropic is enabled / fixed in `5ccd61e`, hardened
  after independent review in `eef2069`.
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
- **Regression evidence:** deterministic Anthropic tests cover tool-use/result batching, strict
  ordering, malformed arguments/IDs, offered-schema filtering and error results. Signed and
  redacted thinking blocks survive a durable transcript round-trip through bounded, validated
  provider metadata; OpenAI-compatible requests strip that metadata before wire/capture.

#### S2-03 — Compression can reinsert system roles into provider history

- **Severity/status:** High correctness / fixed in `3ba071d`, hardened after independent review in
  `eef2069`.
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
- **Regression evidence:** compression tests verify legacy system removal on both full and noop
  paths. Cross-review added deterministic orphan-result removal, missing-result stubs and durable
  repair of short histories.

#### S2-04 — Main-agent closing mode can force an unrelated terminal tool

- **Severity/status:** Medium/High correctness / fixed in `3ba071d`, hardened after independent
  review in `eef2069`.
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
- **Regression evidence:** the main-agent soft-deadline test preserves its full schema; workflow
  tests prove closing mode retains the configured prerequisites plus its declared terminal tool.

#### S2-05 — Persisted user data is promoted to authoritative system text

- **Severity/status:** High safety / fixed in `3ba071d`, hardened after independent review in
  `eef2069`.
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
- **Regression evidence:** adversarial name/onboarding/preferences/facts/pins remain inside the
  structured user-level envelope, while department/admin layers remain system. Mid-run store-first
  compression restores the generated envelope only in memory and never durably duplicates it.

#### S2-06 — Tool batches and auto-finalize bypass deterministic execution invariants

- **Severity/status:** Medium / fixed in `78244f8`, hardened after independent review in `eef2069`.
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
- **Regression evidence:** batch-budget and mixed-terminal tests prove all-or-nothing admission;
  auto-finalize tests cover guarded allow/deny, unavailable approval and complete durable protocol.
  A rejected Stage-B action is not replayed by Stage C.

#### S2-07 — Loop-exhaustion fallback is saved after context teardown

- **Severity/status:** Medium correctness / fixed in `38f2250`.
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
- **Regression evidence:** session-bound loop exhaustion persists the fallback before contextvar
  teardown; sessionless and exception/cancellation paths retain their previous cleanup contract.

#### S2-08 — Anthropic request options and streaming diverge from chat

- **Severity/status:** Medium / fixed in `5ccd61e`, hardened after independent review in `eef2069`.
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
- **Regression evidence:** chat/stream request parity, thinking modes, fragmented JSON, usage,
  unknown tools, ID/name/argument/opaque-thinking bounds and payload correlation are covered by
  deterministic provider tests. Extended thinking removes incompatible sampling controls.

### Sprint 3 — Operational hardening

Sprint 3 closes the operational-hardening items deferred from Sprints 1–2: container
reuse validation, seccomp fail-fast, IPC secret channel, bounded subprocess reads,
Docker dependency pinning, channel request/session hygiene, bounded shutdown,
loopback health, calibration containment, memory bounds, the `memory-worker run`
CLI crash, and `extra="forbid"` on safety-critical settings models. Delivery is
split into three topic commits merged into the integration branch
`codex/security-s3-operational-hardening`.

#### S3-01 — Container reused without policy re-validation · fixed (`b82eac1`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `container/manager.py:ensure_running`.
- **Trace:** an existing `corpclaw_agent_{id}` container was `restart()`ed/returned
  without inspecting image, NetworkMode, CapDrop, ReadonlyRootfs or seccomp; a
  pre-created or stale container with the same name was silently reused as the
  sandbox.
- **Root cause / decision:** `build_docker_args` now stamps generation labels
  (`corpclaw.image`, `corpclaw.strict_capabilities`, `corpclaw.network`);
  `ensure_running` inspects them via `_existing_matches_policy` and stops+removes
  a mismatched container before recreating it with current settings. Unlabelled
  containers (externally created) are treated as a mismatch.
- **Rejected alternative:** re-validating every Docker kwarg on each message —
  too costly for the hot path; label comparison is sufficient and cheap.
- **Regression evidence:** `tests/test_container_manager.py`
  (`test_ensure_running_recreates_on_image_mismatch`,
  `test_ensure_running_recreates_on_unlabelled_container`,
  `test_existing_matches_policy_*`).

#### S3-02 — Seccomp silently skipped when the profile is absent · fixed (`b82eac1`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `container/policies.py:build_docker_args`.
- **Trace:** when `strict_capabilities=True` and the seccomp profile file was
  missing, the profile was silently dropped and Docker's wider default applied —
  contradicting the "hardening on by default" comment.
- **Root cause / decision:** `seccomp_missing_fatal` (default `True`) now raises
  `ContainerPolicyError`; operators who accept the Docker default set it `False`.
- **Regression evidence:** `tests/test_containers.py`
  (`test_build_docker_args_seccomp_missing_fatal_raises`,
  `test_build_docker_args_seccomp_missing_non_fatal_skips`).

#### S3-03 — IPC secret exposed in `docker exec` argv · fixed (`b82eac1`)
- **Severity/status:** Medium (defense-in-depth) / fixed.
- **Boundary:** `container/ipc.py`, `container/agent_worker.py`.
- **Trace:** the secret travelled as `-e CORPCLAW_IPC_SECRET=…` in the exec argv,
  harvestable via `ps`/`/proc/<pid>/cmdline`, undermining the HMAC trust root.
- **Root cause / decision:** the secret is now the first stdin line, the signed
  JSON payload the second; the worker reads both from stdin and constructs
  `IPCAuth(secret=…)` directly. A legacy single-line caller still works.
- **Regression evidence:** `tests/test_container_ipc.py::test_send_tool_call_success`
  (asserts no `-e`/secret in argv; asserts two-line stdin);
  `tests/test_agent_worker_extra.py::test_process_request_reads_secret_from_stdin`.

#### S3-04 — Unbounded MCP/plugin subprocess response line · fixed (`b82eac1`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `extensions/mcp/client.py`, `extensions/plugins/sandbox_proxy.py`.
- **Trace:** `readline()` had no byte cap; a malicious/buggy server or plugin
  could exhaust agent memory with a single unbounded line.
- **Root cause / decision:** both read through a bounded line reader
  (`MAX_RESPONSE_BYTES`, 8 MiB default; MCP per-instance configurable) that raises
  `LimitOverrunError` on overflow → clean `MCPClientError` / plugin kill.
- **Regression evidence:** `tests/test_mcp_client.py::test_mcp_client_oversize_response_rejected`;
  `tests/test_plugins.py::test_plugin_bounded_read_rejects_oversize_line`.

#### S3-05 — Unpinned Docker sandbox build · fixed (`b82eac1`)
- **Severity/status:** Medium (supply chain) / fixed.
- **Boundary:** `docker/Dockerfile`.
- **Trace:** the image `pip install`ed bare package names, so one commit could
  build different dependencies than `uv.lock`, and the list drifted from
  `pyproject.toml`.
- **Root cause / decision:** the Dockerfile now installs from a hash-pinned
  `docker/requirements-docker.txt` generated via
  `uv export --frozen --no-dev --format requirements-txt`, with `--require-hashes`.
  Version bumps for the audited advisories (mcp/pydantic-settings/pillow) are
  deferred to a separate smoke-tested change.
- **Regression evidence:** `docker/requirements-docker.txt` is committed and
  consumed by the Dockerfile.

#### S3-06 — Telegram handlers answer in group chats · fixed (`5810d9d`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `channels/telegram/channel.py`.
- **Trace:** no handler carried a chat-type filter, so `/help`, `/delete`,
  `_handle_text`, `_handle_document` and `_handle_callback` answered in
  `effective_chat` of a group, leaking another user's workflow, files and
  approval prompts.
- **Root cause / decision:** all handlers register with
  `filters.ChatType.PRIVATE` unless `telegram.allow_groups=True`.
- **Regression evidence:** `tests/test_telegram_channel.py::test_private_chat_filter_derived_from_allow_groups`.

#### S3-07 — Raw exception text surfaced to Telegram users · fixed (`5810d9d`)
- **Severity/status:** Low / fixed.
- **Boundary:** `channels/telegram/orchestrator.py`.
- **Trace:** `reply = f"❌ …: {e}"` leaked filesystem paths, upstream URLs or
  library internals; the detail already went to admins.
- **Decision:** the user reply is now a generic notice; admin notification is
  unchanged.

#### S3-08 — Password change did not invalidate web sessions · fixed (`5810d9d`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `users/manager.py:set_web_password`.
- **Trace:** only `password_hash` was updated; pre-rotation sessions lived to TTL.
- **Root cause / decision:** `set_web_password` now deletes the user's
  `web_sessions` in the same transaction as the password update.
- **Regression evidence:** `tests/test_user_manager.py::test_set_web_password_invalidates_existing_sessions`.

#### S3-09 — REST upload bypassed the rate limiter; client_max_size undercut the cap · fixed (`5810d9d`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `channels/web/orchestrator.py`.
- **Trace:** `_rate_limiter.check` ran only on the WS path; `_handle_upload` and
  other mutating REST handlers were unbounded. `web.Application` was built without
  `client_max_size`, so aiohttp's 1 MiB default silently undercut
  `upload_max_bytes` (20 MiB).
- **Root cause / decision:** the app passes `client_max_size=upload_max_bytes`;
  `_handle_upload` applies the per-user rate limit like the WS path.

#### S3-10 — Shutdown could hang indefinitely · fixed (`5810d9d`)
- **Severity/status:** Medium (availability) / fixed.
- **Boundary:** `channels/{telegram,web}/runner.py`.
- **Trace:** `finally: await orchestrator.stop()` was unbounded; a hung MCP
  disconnect, container stop or websocket close held the process past SIGTERM.
- **Root cause / decision:** both runners bound `stop()` with `asyncio.wait_for`
  over `agent.shutdown_timeout_seconds` (default 30 s).

#### S3-11 — `/health` bound to 0.0.0.0 without auth · fixed (`5810d9d`)
- **Severity/status:** Low / fixed.
- **Boundary:** `logging/health.py`, `channels/telegram/orchestrator.py`.
- **Trace:** the unauthenticated operational endpoint defaulted to all
  interfaces, exposing telemetry on shared hosts.
- **Root cause / decision:** `run_health_server` and `LoggingSettings.health_host`
  default to `127.0.0.1`; the Telegram orchestrator passes the configured host.
- **Regression evidence:** `tests/test_logging_and_security.py`
  (`test_health_server_defaults_to_loopback`,
  `test_logging_settings_health_host_default_loopback`).

#### S3-12 — Calibration filename not contained · fixed (`75ba04b`)
- **Severity/status:** Medium (operator-run; defense-in-depth) / fixed.
- **Boundary:** `calibration/editor.py`.
- **Trace:** a cloud-model JSON key became the written basename with no
  sanitisation, so `../../src/…/cli.py` could write outside the calibrated tree
  (and a bootstrap file is later loaded as the system prompt).
- **Root cause / decision:** every key-derived filename passes through
  `_safe_filename` (alphanumeric/_/-/. only, no separators/`..`, recognised
  extension) before write.
- **Regression evidence:** `tests/test_calibration.py::test_apply_rejects_traversal_filename`.

#### S3-13 — Calibration apply was non-atomic · fixed (`75ba04b`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `calibration/editor.py`, `calibration/loop.py`.
- **Trace:** `apply()` wrote sections sequentially without error handling; a
  mid-way failure left a partial state and the backup was never restored.
- **Root cause / decision:** `apply()` runs its sections in try/except and calls
  `rollback()` on any failure; the loop uses `proposed.get("changes")`.
- **Regression evidence:** `tests/test_calibration.py::test_apply_rolls_back_on_partial_failure`.

#### S3-14 — Memory value unbounded; fallback recall full-scan · fixed (`75ba04b`)
- **Severity/status:** Low / fixed.
- **Boundary:** `memory/sqlite.py`.
- **Trace:** `memory_value` had no length cap; the short-token fallback recall
  `SELECT … WHERE user_id=?` loaded every entry into RAM.
- **Root cause / decision:** values are truncated to `_MAX_VALUE_LEN` (8000) on
  write; the fallback SELECT is bounded by `_FALLBACK_RECALL_LIMIT` (500).
- **Regression evidence:** `tests/test_memory.py`
  (`test_store_entry_truncates_oversized_value`,
  `test_fallback_recall_limit_constant_is_bounded`).

#### S3-15 — `memory-worker run` always crashed · fixed (`75ba04b`)
- **Severity/status:** High (runtime bug) / fixed.
- **Boundary:** `cli.py:cmd_memory_worker_run`.
- **Trace:** the command indexed `stack.loop._settings.extensions` /
  `.memory_worker` via `type: ignore`, but `AgentSettings` has neither field, so
  the command always raised `AttributeError`.
- **Root cause / decision:** the command loads the full `Settings` and passes it
  to `resolve_dirs` / `MemoryWorkerService`; the `type: ignore` directives are
  removed.

#### S3-16 — Safety-critical settings silently ignored typos · fixed (`75ba04b`)
- **Severity/status:** Medium / fixed.
- **Boundary:** `config/settings.py`.
- **Trace:** nested `BaseModel`s used the pydantic default `extra="ignore"`, so a
  misspelled routing/isolation/budget/auth/overlay key was silently dropped.
- **Root cause / decision:** `LLMSettings`, `ContainerSettings`, `AgentSettings`,
  `WebChannelSettings` and `ExtensionsSettings` now use `extra="forbid"`.
- **Regression evidence:** `tests/test_misc_modules.py::test_safety_critical_settings_forbid_unknown_keys`;
  `tests/test_containers.py::test_container_settings_rejects_unknown_key`.

#### Deferred from Sprint 3
- **S3-17 — SQLite `_init_db` runs synchronously in `__init__`.** Perf/correctness,
  not security; fixing it converts the store API to async `initialize()` and
  cascades through every construction site (factory, orchestrators, CLI, tests).
  Deferred to a focused PR.
- **S3-18 — `conversation_id="default"` hardcoded in the LLM cache scope.**
  Cache-isolation, not security; threading a real conversation id requires a
  signature change across `call_default_with_slot` / `QueuedProvider` /
  `_execute_with_queue`. Deferred to a focused PR.
- **S3-19 — HTTPS DNS-rebinding (residual).** Retained at the cross-review
  severity: a blind/TOCTOU risk that only yields internal response content when
  the internal endpoint also presents a certificate valid for the
  attacker-controlled hostname. IP-pinning HTTPS via a custom httpx transport is
  fragile across versions and risks breaking the primary fetch path; deferred to
  a focused, smoke-tested PR.
- **Data-location (§4):** `templates_for_testing/` (commercial client data) and
  per-user onboarding profiles still physically live under the public tree. The
  directories are now in `.gitignore` as a commit barrier; the permanent fix
  (remove the files / move onboarding to a runtime data root) is a separate task.
- **Dependency version bumps** for the audited advisories (mcp /
  pydantic-settings / pillow) are deferred behind smoke tests; S3-05 only pins
  the Docker build to the existing lock.

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
Anthropic, MCP and custom plugins were not pilot-ready at this Sprint 1 snapshot.

## Sprint 2 delivery log

| Package | Branch | Status | Verification |
|---|---|---|---|
| PR 1 execution boundary | `codex/security-s2-tool-context` | fixed (`78244f8`) | targeted + final gate |
| PR 2 prompt/transcript | `codex/security-s2-prompt-compression` | fixed (`3ba071d`) | targeted + final gate |
| PR 3 Anthropic parity | `codex/security-s2-anthropic` | fixed (`5ccd61e`) | targeted + final gate |
| PR 4 loop lifecycle | `codex/security-s2-loop-lifecycle` | fixed (`38f2250`) | targeted + final gate |
| Independent-review corrections | `codex/security-s2-loop-lifecycle` | fixed (`eef2069`) | three zonal reviews + final gate |

### Sprint 2 independent-review corrections

All zonal findings were reproduced and traced in the integrated code before acceptance. The
following claims were confirmed and fixed in `eef2069`:

- mid-run durable compression discarded the regenerated, user-level persisted-context envelope;
- the first transcript normalizer removed roles but did not repair orphaned/incomplete tool pairs;
- workflow closing removed configured prerequisite tools;
- a rejected Stage-B auto-finalize action was retried by Stage C;
- Anthropic extended-thinking replay lost signed/redacted blocks, including after restart;
- Anthropic budget thinking retained incompatible sampling controls;
- malformed history and empty tool IDs were not consistently rejected before execution;
- streamed tool metadata and opaque thinking needed additional aggregate bounds;
- normal tool execution errors were not always represented as Anthropic `is_error=true`.

The reviews also re-confirmed ContextVar isolation/reset, pre-execution RBAC, atomic batch budget,
mixed-terminal rejection, main-agent closing behaviour and lifecycle persistence. Claims that did
not reproduce were not added as defects.

### Sprint 2 final verification and rollout record

- `uv run ruff check src/ tests/`: passed.
- `uv run ruff format src/ tests/ --check`: passed; 380 files formatted.
- `uv run pyright src/`: 0 errors; 16 pre-existing matplotlib typing warnings.
- `uv run pytest tests/ -q`: 2003 passed, 1 skipped, 1 aiohttp warning in 192.00 seconds.
- `uvx bandit -r src/ -q -lll`: passed, zero High findings.
- Deterministic smoke covers Anthropic user → tool-use → tool-result → final, durable signed
  thinking replay, exact plugin/MCP business payload, prompt trust separation, legacy transcript
  repair, auto-finalize allow/deny, terminal batching and loop fallback persistence.
- Telegram/Web live smoke, live Anthropic and live llama.cpp were not run because this checkout
  has no pilot credentials/services; these remain desirable but non-blocking under the agreed DoD.
- Public configuration remains unchanged: Anthropic, MCP and plugins are opt-in. Trusted
  administrator plugins and MCP are suitable for a limited pilot; plugin subprocesses remain
  crash isolation, not a sandbox for untrusted code.
- No Sprint 3 container, IPC, channel, dependency or deployment-hardening work was pulled in.

## Sprint 3 delivery log

| Package | Branch | Status | Verification |
|---|---|---|---|
| Commit 1 container/supply-chain | `codex/security-s3-container-supplychain` | fixed (`b82eac1`) | targeted + final gate |
| Commit 2 channels/sessions | `codex/security-s3-channels-sessions` | fixed (`5810d9d`) | targeted + final gate |
| Commit 3 config/calibration/memory | `codex/security-s3-config-calibration-memory` | fixed (`75ba04b`) | targeted + final gate |
| Integration | `codex/security-s3-operational-hardening` | all three merged | final gate |

### Sprint 3 final verification and rollout record

Baseline: `codex/security-s2-loop-lifecycle` tip (`dcdec2e`); the integration branch is
a clean fast-forward over it. Telegram/Web live smoke and live llama.cpp were not run
(no pilot credentials in this checkout); these remain non-blocking under the agreed DoD.

- `uv run ruff check src/ tests/`: passed.
- `uv run ruff format --check src/ tests/`: passed (382 files formatted).
- `uv run pyright src/`: 0 errors; 16 pre-existing matplotlib typing warnings.
- `uv run pytest tests/ -q`: **2022 passed, 1 skipped, 1 aiohttp warning** (~122 s).
- `uvx bandit -r src/ -q -lll`: passed, zero High findings.
- Deterministic coverage added: container revalidation + seccomp fail-fast, IPC stdin
  secret channel, MCP/plugin bounded reads, private-chat filter, password-change session
  invalidation, calibration traversal rejection + partial-failure rollback, memory value
  cap + fallback recall bound, and `extra="forbid"` on five safety-critical settings models.
- Deferred (separate focused PRs): S3-17 (async SQLite init), S3-18 (per-conversation cache
  scope), S3-19 (HTTPS TLS IP-pinning), §4 data-location, dependency version bumps.
- Public configuration remains unchanged; `.gitignore` gained a commit barrier for
  `templates_for_testing/` and `scripts/dev/` (local fixtures, not part of the build).
