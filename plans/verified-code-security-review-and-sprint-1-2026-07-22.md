# Verified Code & Security Review — Sprint 1

> Date: 2026-07-22  
> Baseline: `pre-release` at `975448a`  
> Purpose: canonical, code-traced register for the July 2026 review and its fixes.

## Reading and status rules

Every finding keeps its original evidence after remediation. Status values are:
`verified`, `reduced`, `rejected`, and `fixed`. Severity describes the verified impact,
not the wording of an external report. A finding becomes `fixed` only after its regression
tests and the full repository gate pass.

Baseline quality snapshot before Sprint 1: Ruff passed, Pyright reported 0 errors, the full
suite reported 1953 passed / 1 skipped, and Bandit reported no High findings. Dependency
audit reported 22 advisories across `mcp`, `pydantic-settings`, and Pillow; no reachable
exploit was established and upgrades remain Sprint 3 work.

## Sprint 1 register

### S1-01 — Nested symlink disclosure during web directory copy

- **Severity/status:** High / verified (implementation in PR 1, final gate pending).
- **Boundary:** host workspace isolation.
- **Trace:** `channels/web/files.py::copy_paths` validated only the source root and then used
  `copytree(..., symlinks=False)`, which dereferenced every nested link.
- **Impact/preconditions:** an authenticated user able to create a link in their workspace
  could copy an arbitrary host-readable target into a downloadable regular file. The attack
  was reproduced with a nested link to a file outside the workspace.
- **Root cause:** validation and copy had different recursion semantics.
- **Decision:** reject all links in copied trees; preserve rather than follow any link that
  appears after the pre-scan; post-scan and remove partial output on failure.
- **Acceptance:** external and in-workspace links are both rejected, no destination remains,
  and ordinary recursive copy/unique naming still work.

### S1-02 — Partial department overlay expands omitted allowlists

- **Severity/status:** High / verified (implementation in PR 1, final gate pending).
- **Boundary:** department RBAC configuration.
- **Trace:** `DepartmentConfig` materialised omitted fields as `["*"]` before union merge.
- **Impact/preconditions:** a legitimate private overlay changing only budget/profile could
  silently turn a restricted base department into wildcard access.
- **Root cause:** parsing defaults were confused with overlay patch semantics.
- **Decision:** omitted overlay fields inherit; explicitly present allowlists union; explicit
  wildcard alone expands access. Parse a complete file before replacing live state.
- **Acceptance:** budget-only overlay preserves description/profile/all allowlists; invalid
  fields leave the previous manager state intact.

### S1-03 — Department skill allowlist was not applied to prompt selection

- **Severity/status:** High / verified (implementation in PR 1, final gate pending).
- **Boundary:** RBAC-controlled system-prompt content.
- **Trace:** `SkillRegistry.get_allowed_skills` checked only `Skill.allowed_for`; the existing
  `PermissionChecker.can_use_skill` was not on the production selection path.
- **Impact:** instructions for a department-denied skill could enter the agent prompt. Plugin
  and MCP runtime/schema checks already exist and are retained as regression contracts.
- **Decision:** inject `PermissionChecker` into the registry and intersect both policies.
- **Compatibility:** API wildcard defaults remain; shipped departments explicitly record
  their current effective skill/plugin/MCP wildcards and omitted base wildcards warn.

### S1-04 — Whitelist/revocation cache is stale across processes

- **Severity/status:** High / verified (planned PR 2).
- **Boundary:** Telegram authentication and emergency revocation.
- **Trace:** each `UserManager` cached `whitelist.json` and `revoked_sessions.json` forever.
- **Impact:** a running channel could continue accepting a user after a separate CLI process
  revoked or denied access. Reproduced with two managers created before the mutation.
- **Decision:** SQLite is the sole runtime source. Import legacy JSON exactly once under a
  transaction and retain files only as rollback artifacts.

### S1-05 — Credential scrubber misses modern tokens and formatted traceback

- **Severity/status:** High / verified (planned PR 2).
- **Boundary:** logs, payload capture, tool-result confidentiality.
- **Trace:** patterns omitted modern token prefixes. Logging filters run before a formatter
  creates traceback text, so `logger.exception` leaked a matching `sk-...` secret.
- **Decision:** expand shared patterns and scrub the formatter's final output.
- **Acceptance:** message arguments, exception messages and traceback lines contain only the
  redaction marker for all supported token families.

### S1-06 — Native tool calls bypass the offered schema

- **Severity/status:** High correctness/policy boundary / verified (planned PR 3).
- **Trace:** XML parsing receives `allowed_tool_names`; `_tool_calls_from_native` accepted any
  SDK-provided name. Execution-time department RBAC remained active, so this was not a full
  permission bypass, but closing/phase/tool-surface restrictions could be bypassed.
- **Decision:** filter native calls by the exact offered schema and trace rejected names
  without their arguments.

### S1-07 — Queue cancellation can leak a sticky slot lock

- **Severity/status:** High availability / verified (planned PR 3).
- **Trace:** cancellation released `selected_slot.lock` only inside the
  `semaphore_acquired` branch. Cancellation after slot-lock acquisition but while waiting for
  the global semaphore permanently blocked that slot. The sequence was reproduced.
- **Decision:** track and release every acquired resource independently and idempotently.

### S1-08 — Terminal tool persists an orphaned tool call

- **Severity/status:** High correctness / verified (planned PR 4).
- **Trace:** assistant tool calls were persisted, while terminal results skipped the `tool`
  role and returned directly. Reloaded history violated OpenAI-compatible tool protocol.
- **Decision:** always persist assistant-call → tool-result → final-assistant. Terminal status
  controls loop termination only.

### S1-09 — Scheduler completion overwrites pause/dismiss with stale state

- **Severity/status:** High correctness / verified (planned PR 4).
- **Trace:** a worker claimed a task, ran for an extended period, then wrote its stale full
  object through an update without checking current status or claim token.
- **Impact:** a concurrent user pause/dismiss could be resurrected.
- **Decision:** atomic `complete_claim` keyed by claim token; preserve user-controlled state
  while recording outcome and clearing the owned claim.

## Deferred verified register

### Sprint 2 — LLM and extension contracts

- **S2-01 High:** Plugin/MCP tools receive runtime `User` and callbacks as serialised business
  arguments. Introduce a non-serialised `ToolExecutionContext` without breaking `Tool.execute`.
- **S2-02 High when enabled:** Anthropic receives OpenAI-style `tool_calls`/`tool` messages;
  implement `tool_use`/`tool_result` conversion and RequestOptions parity.
- **S2-03 High correctness:** mid-run compression can reinsert `system` roles into a message
  history whose system prompt is transported separately.
- **S2-04 Medium/High correctness:** main-agent closing mode can narrow a text task to only
  `read_image`; restrict only workflows with an explicit terminal contract.
- **S2-05 High safety:** onboarding and recalled memory are interpolated into authoritative
  system text. Represent persisted user data as untrusted structured context.
- **S2-06 Medium:** auto-finalize executes terminal tools outside the ordinary guard/execution
  path; batch budget and terminal-parallel semantics also need normalisation.
- **S2-07 Medium:** loop fallback attempts durable save after resetting the context target.
- **S2-08 Medium:** Anthropic sampling/request overrides and streaming accumulation need parity
  and bounded-memory tests.

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
| PR 1 filesystem/RBAC | `codex/security-s1-filesystem-rbac` | in progress | targeted tests pending final gate |
| PR 2 auth/logging | `codex/security-s1-auth-logging` | planned | — |
| PR 3 native/queue | `codex/security-s1-native-tools-queue` | planned | — |
| PR 4 transcript/scheduler | `codex/security-s1-transcript-scheduler` | planned | — |

The pilot assumption is OpenAI-compatible llama.cpp with built-in tools and shipped subagents.
Anthropic, MCP and custom plugins are not pilot-ready until Sprint 2.
