# Roadmap

## Current Status

- `v0.2.0` is released. Its annotated tag and GitHub Release exist, and `main` sits at
  the independently verified release merge commit.
- `APP_VERSION` (`core/constants.py`) and the shipped `config/config.toml` version are
  both `0.2.0`, and both stay at `0.2.0` through Stage V1-31. Only Stage V1-32 moves the
  runtime, configuration, and test version literals to `1.0.0`.
- `CHANGELOG.md` carries one permanent `## Unreleased` section. Every merged stage that
  changes user-visible behavior, security or privacy guarantees, data compatibility, or
  upgrade behavior adds an entry there. Documentation-only, audit-only, test-only, and
  truly internal-only stages do not. No intermediate public `0.x` release is created.
- The active effort is **Maria v1.0**. "Personal AI Companion" is the working theme of
  this roadmap and a product description; the final `v1.0.0` release title is a separate
  owner decision required before Stage V1-32.
- Stage V1-01, this roadmap definition, runs on `docs/define-v1.0-roadmap` and changes
  only `docs/roadmap.md`.
- The completed v0.2.0 roadmap is retained in the appendix at the end of this file.
  Nothing in that appendix states a v1.0 requirement.

## Product North Star

Maria is a local-first, single-owner personal AI companion, secretary, researcher, and
development partner.

Maria should:

- communicate naturally;
- remember owner-approved information;
- retrieve relevant personal context safely;
- help with software, websites, documentation, QA, and other projects;
- manage tasks and reminders;
- organize files within explicit safe boundaries;
- research public internet sources and cite them;
- analyze the owner's plans;
- respectfully disagree and recommend better approaches;
- remain transparent about what it did and why.

Maria is not a yes-man, but the owner remains the final authority.

Maria may become increasingly capable after v1.0. It must never silently expand its
permissions, scope, access, or authority.

The North Star is a direction, not a release scope. What any single release promises is
defined by that release's own section.

## Maria v1.0 — Release Promise

Maria v1.0 is the first stable personal AI release. It is not required to complete the
full long-term vision.

> Maria v1.0 is a local-first, single-owner personal AI companion, secretary,
> researcher, and development assistant. It communicates naturally through an
> owner-controlled local model while preserving deterministic commands as a reliable
> fallback. Maria remembers explicitly approved information, retrieves bounded relevant
> local context with visible provenance, manages tasks and reminders, assists with
> projects and file organization, performs cited public-internet research, and proposes
> visible, validated foreground plans that execute only through explicit tools and
> permissions. A simple desktop GUI is the primary daily interface, while the CLI
> remains a first-class recovery and power-user interface. Maria may analyze, disagree,
> and recommend a better approach, but the owner remains the final authority. Ordinary
> conversations are transient by default, personal data remains locally controlled, and
> Maria cannot silently expand its scope, permissions, or authority.

This paragraph is the release promise, not the release title.

## Owner-Approved Decisions for v1.0

### D-01 — Reasoning and action boundary

- Natural conversation becomes the primary interface.
- Existing exact commands remain permanent, deterministic, testable, and usable with no
  model present.
- Model output is untrusted.
- Model-originated action selection is deny-by-default.
- Every model-originated tool, action, and argument requires deterministic validation.
  Registered action membership alone is **not** argument validation.
- Sensitive, mutating, private-data-bearing, and multi-step model-originated work
  requires a visible resolved plan and explicit owner approval.
- One narrow exception exists, and only under the owner-approved policy introduced in
  Stage V1-27: an explicitly allowlisted, single-step, low-risk, public read-only
  research action may execute without confirmation. It must display a visible notice
  that research is being performed and must produce a structured audit summary. Nothing
  else may execute without approval, and the exception never extends to a mutating,
  private-data-bearing, or multi-step action.
- Confirmation binds to the exact immutable plan and target arguments. Maria executes
  the exact approved plan.
- Maria may not re-plan, re-call the model, or substitute arguments after confirmation.
  A changed plan invalidates existing approval.
- Maria may adapt implementation details only while the approved goal, scope, security
  boundary, data boundary, acceptance tests, and user-visible behavior are unchanged.
  Anything outside that boundary requires a new proposal and owner approval.
- A model never directly executes tools, SQL, filesystem operations, network
  operations, shell commands, generated code, application actions, or persistence.

`PermissionManager.check()` returns `ALLOWED` for any `tool.action` present in neither
mandatory set, so deny-by-default cannot be delegated to it. D-01 therefore requires a
separate positive allowlist and argument-contract gate in the model-to-plan compilation
layer, consulted before the permission decision.

### D-02 — v1.0 product scope

Maria v1.0 delivers the capabilities listed under "Required v1.0 Capabilities". It
deliberately does not deliver anything listed under "Explicit Post-v1.0 Scope".

### D-03 — Local reasoning strategy

- v1.0 uses one owner-controlled local open-weight model, on the owner's PC or private
  personal server.
- No cloud-model adapter is required or enabled. No monthly cloud-model API cost.
- No automatic provider, model, endpoint, or destination fallback exists.
- Deterministic commands remain available when the local model is unavailable;
  model-required input receives a controlled unavailable response.
- Provider-specific behavior stays inside the local adapter. Brain, the deterministic
  Planner, PermissionManager, ToolManager, the memory layer, and tools stay
  provider-independent.
- Maria never automatically downloads a model, installs provider software, installs
  dependencies, or launches unrestricted processes.
- Loopback and private-LAN calls count as network transport.
- Tracked configuration contains only a disabled, non-personal reasoning template. When
  reasoning is disabled, machine-specific values such as permitted host, endpoint, model
  path, model identifier, or credential are absent, and the template still parses
  successfully. When reasoning is enabled through the deliberately untracked local
  override, transport class, permitted host, model identifier, hard request deadline,
  maximum input size, and maximum output size are all required and strictly validated,
  and an enabled configuration missing any required active field fails closed.
- No personal host, endpoint, model path, credential, or private-server value may appear
  in tracked configuration.
- No transport, authentication, credential, or cloud code ships for a modality the
  chosen v1.0 local adapter does not use.
- Logging never contains raw prompts, raw responses, selected private context,
  credentials, endpoint URLs, local model paths, headers, or raw provider exception
  text.
- Internet research is a separate explicit capability and does not change this policy.
- Exact local runtime selection is a later stage-specific owner decision, required in
  writing before Stage V1-13 opens.

### Roadmap authority and refinement

The roadmap freezes product boundaries, stage order, security invariants, and completion
outcomes. A future stage may refine implementation details through a read-only audit and
an owner-approved roadmap amendment when direct repository evidence shows a safer or
simpler design. No agent may silently expand scope, skip a prerequisite, weaken a
boundary, or reinterpret a post-v1.0 capability as in-scope.

## Required v1.0 Capabilities

1. Natural conversational assistance.
2. Deterministic command fallback.
3. One owner-controlled local reasoning adapter.
4. One adversarial fake adapter for automated tests.
5. Typed model requests and responses.
6. Model-to-plan compilation and per-action argument validation.
7. Immutable plan approval and execution.
8. Foreground multi-step plans.
9. Explicit context provenance.
10. Tiered personal-context retrieval.
11. Transient ordinary conversation by default.
12. Explicit durable memory, notes, tasks, reminders, and approved summaries.
13. Structured tasks and reminders.
14. Narrow, least-privilege reminder delivery that can notify the owner while the Maria
    GUI and CLI are closed.
15. Project and workspace assistance.
16. File organization inside explicit safe roots.
17. Public web and documentation research with source attribution.
18. Simple first-class desktop GUI.
19. First-class CLI fallback.
20. Schema versioning and tested migration from v0.2.0.
21. Data export.
22. Backup.
23. Restore.
24. Privacy-safe logging and audit behavior.
25. Complete tests, documentation, and upgrade validation.

## v1.0 Operating Policies

### Memory and context policy

Maria may automatically search local stored information, but retrieval is tiered and
bounded.

- **Tier A — automatically usable when relevant:** explicit memories marked
  context-enabled, owner preferences, active tasks and reminders, active-project facts.
- **Tier B — automatically searchable, bounded, with visible provenance:** notes the
  owner has enabled for context, conversation summaries the owner has enabled for
  context, context-enabled explicit memories beyond Tier A's bounded window, project
  records. Under Tier A's ascending-id order those additional memories are the newer
  ones by id, not older ones.
- **Tier C — requires explicit selection or approved workflow scope:** raw interaction
  history, saved conversations, arbitrary files, logs, credentials, sensitive records,
  unrelated project data. Every memory, note, or conversation summary without context
  consent is treated as a sensitive record: Maria cannot tell sensitive free text from
  any other, so consent, not inspection, is the boundary.

Every context item included in reasoning carries deterministic provenance produced by
Maria — a memory, note, conversation summary, task, reminder, or project id, or a file
identity — never supplied by the model. Maria discloses uncertainty, and discloses a possible conflict or
an out-of-date copy only on deterministic evidence: notes that share a title but have
different bodies are named together as possible divergent versions, never as a proven
contradiction; a last-saved date is a fact, not proof that a record is stale; and a
previously supplied copy observed to have changed is reported as invalidated, and the
conversation that carried it is cleared, without labelling the current record stale.

"When relevant" is not yet implemented as relevance. Under the owner-approved
transitional rule introduced in Stage V1-15 (decision V1-15-R1), every context-enabled
explicit memory is eligible to be sent to the owner's local model on **any**
model-backed conversational turn, in ascending memory id order, subject only to hard
item and byte bounds. Maria makes no relevance judgement and claims none. At the Stage
V1-16 relevance-review gate the owner kept that rule unchanged (decision V1-16-R1). Tier
B adds only deterministic literal matching against the current request, which is not a
relevance judgement either. Any later replacement needs a new owner-reviewed relevance
decision.

Storage existence alone is not consent to unrestricted use.

Maria v1.0 ships none of the following: bulk, wildcard, clear-all, or date-range
mutation; automatic retention or pruning; undo or revision history; a secure-erasure
guarantee against SQLite file pages, database or filesystem backups, Git history, or
external copies; or encryption at rest. Each remains available to a later version only
through the gate described under "Permanent Safety and Privacy Boundaries".

### Conversation and audit policy

- Ordinary model-backed conversation is transient by default.
- A full conversation is persisted only through explicit owner action.
- Maria may propose an approved summary for storage.
- Model text never silently becomes an explicit memory, note, task, or reminder.
- Tool-backed model execution stores a structured audit summary rather than raw prompts,
  raw responses, selected private context, or returned private content.
- Audit summaries may identify non-sensitive targets and actions. Sensitive target
  identity is redacted or generalized.
- Existing deterministic commands retain their current recording behavior until a
  separately approved stage deliberately changes it.

### Background work and reminder-delivery policy

Maria v1.0 includes a narrow, least-privilege reminder-delivery subsystem that can
notify the owner while the Maria GUI and CLI are closed.

The subsystem may only:

- read due reminders;
- determine whether a reminder should fire;
- deliver a notification;
- mark a reminder as delivered;
- report missed or overdue reminders.

It may never:

- invoke the reasoning model;
- construct, modify, compile, or approve a plan;
- invoke ToolManager or any general tool;
- access projects, files, memories, notes, or interaction history beyond the minimum
  reminder record needed for delivery;
- perform internet research;
- modify permissions;
- perform general background or project work;
- become a general autonomous agent.

The owner can enable and disable the subsystem, and the whole product works with it
disabled.

Maria does not promise notification delivery while the computer is completely powered
off. Sleeping-device wake behavior depends on the selected operating-system mechanism
and remains a stage-specific owner decision. A missed reminder surfaces as overdue when
the subsystem next becomes available.

The exact operating-system integration — a narrow background process, a task-scheduler
entry, a startup component, a tray process, or another mechanism — is deliberately
deferred to the Stage V1-23 audit and owner decision. Whichever mechanism is chosen is
bounded to the five operations above.

Everything else stays foreground. General unattended project development, long-running
delegated project workflows, autonomous background coding, and general workflow
adaptation while the owner is absent are explicitly post-v1.0.

### Internet research policy

The local model may automatically decide that public research would improve an answer.
Allowed through a bounded read-only research capability: public web search, public
documentation retrieval, public fact verification, source comparison, citations, and
uncertainty reporting.

Not automatically allowed: private-data inclusion in search queries, logging into
accounts, submitting forms, posting content, modifying remote data, executable
downloads, software installation, purchases, and financial operations.

Public research is not equivalent to unrestricted browser automation.

### GUI and CLI policy

- A simple desktop GUI is required for v1.0 and is the normal daily interface.
- The CLI remains a first-class fallback, recovery, debugging, and power-user interface.
- The GUI must not create a second execution or permission architecture.
- Both interfaces reach the same Brain, plan validation, permissions, tools, memory, and
  audit boundaries.

### Tool-creation policy

Before v1.0, Maria does not create or test new executable tools as a product capability.
After v1.0 she may eventually propose, build, and test candidate tools in an isolated
environment. Owner approval is still required before installation, registration,
permission assignment, access to real data, merge, deployment, or production use.

Maria may become self-sufficient, but never self-authorizing.

## v1.0 Architecture Requirements

- The permanent layering invariants continue to hold unchanged. See "Permanent Safety
  and Privacy Boundaries".
- Deny-by-default for model-originated action selection lives in the model-to-plan
  compilation layer, never in `PermissionManager`, and is consulted before the
  permission decision.
- Plans are immutable and fingerprintable; approval binds to the fingerprint.
- Permissions resolve across a whole plan before any step executes.
- The database gains a schema version independent of `APP_VERSION`, with an ordered,
  idempotent, atomic forward migration runner.
- Recording is a generic per-plan policy on `Plan`; Brain keeps no tool-specific branch.
- Timing, scheduling, notification, and concurrency primitives are confined to the
  narrow reminder-delivery subsystem. That subsystem has no import or call path to
  reasoning, model-to-plan compilation, ToolManager, general tools, project assistance,
  or internet research. Its exact module shape is decided during Stage V1-23.
- Exactly two modules may import a network module: the local reasoning adapter and the
  research transport. The reminder-delivery subsystem may use operating-system timing
  and notification primitives, but no network module.
- The two network surfaces have disjoint destination policies. The local reasoning
  adapter is the only component permitted to contact the explicitly configured loopback
  or private-LAN inference host. The research transport is public-only: it accepts
  approved public HTTP or HTTPS destinations and refuses loopback, private, link-local,
  multicast, reserved, unspecified, and cloud-metadata-service destinations on both IPv4
  and IPv6, validating resolved addresses rather than hostname strings alone, refusing
  redirects into a forbidden destination, and refusing a resolution that changes into a
  forbidden range between validation and connection. It never contacts the configured
  local-model endpoint.
- Exactly one package may import a GUI toolkit.
- No production path executes shell commands, subprocesses, dynamically generated code,
  `eval`, `exec`, or model-produced source code. Maria still dispatches registered tools
  through ToolManager; that is the intended execution mechanism and is unaffected.
- Tracked configuration contains only a disabled, non-personal reasoning template that
  parses successfully without active provider fields. Machine-specific provider settings
  live only in a deliberately untracked local override, and an enabled configuration
  missing any required active field fails closed.
- Standard library only. A third-party dependency remains out of scope without explicit
  owner approval.
- Every pre-v1.0 deterministic command, and every non-network deterministic command
  added during v1.0, remains available and usable without a model, network transport, or
  GUI toolkit, at every stage. A deterministic command whose purpose inherently requires
  network access, such as public research, remains present and returns a controlled
  unavailable result when research is disabled or the network is unavailable; it is
  never required to succeed in a no-network run.
- Behavior not deliberately changed by an approved stage remains byte-identical; every
  deliberate change is documented, covered by regression tests, and reflected in the
  changelog when user-visible.

## v1.0 Planned Development Order

Thirty-two focused stages. Each is one architectural seam, one branch, one review.
Stages V1-29 and V1-31 are read-only audits with no branch of their own; any correction
they find gets its own focused `fix/<topic>` branch, never a release branch.

### Stage V1-01 — v1.0 roadmap definition and v0.2.0 history separation

- Branch: `docs/define-v1.0-roadmap`
- Prerequisites: none
- Purpose: Put the locked v1.0 plan first, move the completed v0.2.0 plan into a
  trailing appendix, and split permanent invariants from v0.2.0-only constraints so
  neither can be read as the other.
- Owner-visible result: The roadmap states a correct current status, the North Star,
  every v1.0 stage with its branch, prerequisites and non-goals, and the release freeze.
  No runtime change.
- Architecture affected: `docs/roadmap.md` only.
- Privacy and security: Records that `PermissionManager` is default-allow so
  deny-by-default needs a separate allowlist; that a new action is permitted by default;
  the tiered context policy; the never-log-raw rule; that loopback counts as network
  transport; the reminder-delivery envelope; and the self-review boundary.
- Non-goals: No code, test, schema, configuration, or version change. No changelog
  entry. No other document edited. No historical v0.2.0 behavior rewritten.
- Completion criteria: v1.0 sections precede the appendix; the historical block is moved
  intact, demoted one level, and qualified, with byte-identical bodies; the four
  approved historical notes are inserted; every stage heading matches
  `### Stage V1-NN — ` exactly once; every prerequisite references a strictly
  lower-numbered stage; every branch name is unique; documentation-only validation
  passes with exactly one changed file.
- Changelog: none — documentation only.

### Stage V1-02 — Record the v1.0 freeze in the development contract

- Branch: `docs/record-v1.0-freeze-in-contract`
- Prerequisites: V1-01
- Purpose: `CLAUDE.md` frames `v0.1.0` as the baseline, marks only that tag immutable
  although four annotated tags are published, and says nothing about the `APP_VERSION`
  freeze or the no-intermediate-release rule.
- Owner-visible result: No runtime change. The development contract every session reads
  states the tag inventory and the version freeze.
- Architecture affected: `CLAUDE.md` only.
- Privacy and security: Purely protective. It removes the ambiguity under which a future
  session could create a public release artifact mid-effort or bump a version site on a
  feature branch.
- Non-goals: No module-responsibility change. No scope-boundary promotion; each
  capability's own stage moves its own entry. The existing default-allow explanation
  stays verbatim. No philosophy or priority change. Not folded into V1-03.
- Completion criteria: All four published tags are stated immutable; `APP_VERSION` and
  the shipped configuration version are stated frozen at `0.2.0` through Stage V1-31,
  with only the owner-approved `release/v1.0.0` stage moving them to `1.0.0`; no
  intermediate public release branch, tag, or GitHub Release is permitted; exactly one
  changed file; documentation-only validation passes, with no test run and no
  compilation check, because no executable, test, or configuration file is modified.
- Changelog: none — documentation only.

### Stage V1-03 — Immutable, fingerprintable plan value objects

- Branch: `feature/immutable-plan-objects`
- Prerequisites: V1-01
- Purpose: `PlanStep.arguments` is a plain dictionary inside a frozen dataclass, so its
  contents stay mutable and the generated hash raises. No confirmation can bind to a
  plan today.
- Owner-visible result: None. Every deterministic command behaves identically.
- Architecture affected: `core/planner.py`, `core/brain.py`, `core/tool_manager.py`,
  argument access in `tools/`, planner tests.
- Privacy and security: The fingerprint derives from a canonical serialization that can
  contain note bodies, memory facts, and absolute paths, so only the fingerprint may be
  logged. Immutability must be real rather than a docstring or a shallow copy.
- Non-goals: No approval binding yet. No grammar, command, or help-text change. No
  model, compiler, or schema change.
- Completion criteria: `Plan` and `PlanStep` are hashable; structurally equal plans
  produce an identical fingerprint and any single differing argument character changes
  it; mutating a returned plan's arguments raises; every tool resolves arguments through
  a non-mutating API; the planner and planner-regression tests pass with no assertion
  changes; no log line emitted during planning contains an argument value.
- Changelog: none required — internal only. The guarantee it enables is recorded by
  V1-04.

### Stage V1-04 — Plan-bound confirmation and interface-agnostic session contract

- Branch: `feature/plan-bound-confirmation`
- Prerequisites: V1-03
- Purpose: The CLI discards the returned plan and re-sends raw text; Brain re-retrieves
  context, plans a second time, and executes whatever the second plan says. A test
  currently locks that behavior in.
- Owner-visible result: Confirming a sensitive action executes exactly the plan Maria
  showed. Prompt wording, accepted answers, and CLI output are unchanged; an approval
  whose plan no longer matches is refused explicitly instead of executing something
  else.
- Architecture affected: `core/brain.py`, a new core session module, `main.py`,
  `core/logger.py`, brain, main, and startup-integration tests.
- Privacy and security: The pending-approval object lives in process memory only; it is
  never persisted and never logged in raw form. Any intervening request invalidates it,
  so approval can never be replayed against a later plan. Confirmation text may name a
  target but never dumps stored record content.
- Non-goals: No multi-step plans. No model, compiler, or GUI. No change to which actions
  require confirmation, to prompt wording, to accepted answers, or to what cancelling
  records. `PermissionManager` is untouched.
- Completion criteria: Planning happens exactly once per approved command, and the
  deliberately locked-in assertion that it happens twice is rewritten with the reversal
  called out in the pull request; a fingerprint mismatch rejects and executes nothing;
  `main.py` contains no permission logic, plan construction, or confirmation policy; a
  headless test drives a complete confirm cycle and a complete cancel cycle with no
  standard input; CLI output, banner, and exit codes stay identical for every existing
  tested path; a second logging configuration call is honoured or raises, never silently
  ignored.
- Changelog: one entry — confirming a sensitive action now executes the exact plan that
  was shown, and the confirmation contract moved from the CLI into shared core behavior.

### Stage V1-05 — Whole-plan permission resolution before any execution

- Branch: `feature/whole-plan-permission-resolution`
- Prerequisites: V1-04
- Purpose: Brain checks permission at the top of a loop iteration and executes at the
  bottom of the same iteration, so a denial at a later step would return only after
  earlier steps already ran. This is latent only because the planner emits at most one
  step, and it must close before anything can emit several.
- Owner-visible result: Maria resolves the complete plan and its permission outcome
  before executing any step. Nothing partially runs before a denial. Single-step
  commands are unchanged.
- Architecture affected: `core/brain.py` execution loop, `core/permissions.py` consumed
  unchanged, the core session module, brain tests.
- Privacy and security: A denial is now reached before any side effect. Resolved-plan
  rendering is deterministic and lists every step's tool, action, and resolved
  permission outcome in plan order. This stage deliberately omits argument values and
  raw fingerprints, so the rendering cannot expose absolute host paths or private
  argument content. Exact validated argument rendering remains required before
  model-originated execution in V1-18, after V1-06's per-action contracts can classify
  and validate path arguments safely. `PermissionManager` stays unchanged so its
  deliberately locked-in default-allow tests remain valid.
- Non-goals: The planner still emits at most one step; multi-step generation is V1-19.
  No transaction, rollback, or compensation across steps: stop-on-failure with an
  explicit report is the documented, tested semantics. No new commands and no change to
  any permission decision.
- Completion criteria: A synthetic two-step plan whose second step is denied executes
  zero steps, asserted by counting tool invocations; a synthetic two-step plan requiring
  confirmation executes zero steps before approval and each step exactly once after;
  single-step output text and recorded rows stay identical against a temporary database;
  multi-step resolved-plan rendering follows plan order, names every step's tool and
  action, names every resolved permission outcome, omits argument values, omits raw
  fingerprints, is neither logged nor persisted, and contains no absolute host path.
- Changelog: one entry — Maria now resolves permissions for an entire plan before
  executing any part of it.

### Stage V1-06 — Per-action argument contracts, declared and enforced

- Branch: `feature/action-argument-contracts`
- Prerequisites: V1-05
- Purpose: Nothing declares valid arguments for an action. ToolManager validates only
  that an action is registered, which is membership and not argument validation, and the
  tools are inconsistent: some perform strict type and range checks while the filesystem
  tool coerces with a bare string conversion.
- Owner-visible result: Valid deterministic commands are unchanged. Malformed arguments
  now produce an explicit typed refusal naming the field instead of being silently
  coerced into a tool.
- Architecture affected: a new core action-contract module, `core/brain.py`,
  `core/tool_manager.py`, and the tools whose ad hoc coercion is tightened.
- Privacy and security: The registry is an independent declarative table and is never
  derived from a tool's registered actions at runtime, because deriving it would inherit
  the permission layer's default-allow property. The gate runs before
  `PermissionManager`, establishing the required ordering: positive allowlist and
  argument validation first, permission decision second. Validation rejects; it never
  truncates, coerces, or repairs. Rejection messages name the action and the offending
  field only, never the offending value.
- Non-goals: No change to `core/permissions.py`, to either mandatory set, or to the
  permission tests. No model, compiler, new tool, new action, or schema change. The
  tools keep their own internal validation as defence in depth.
- Completion criteria: Every registered tool action has exactly one contract entry
  declaring required and optional arguments, type, bounds, and, for path arguments, a
  named root class; a consistency test fails in both directions; unknown tool, unknown
  action, extra argument, missing required argument, wrong type, and out-of-range value
  are each rejected with a distinct typed failure before `PermissionManager` is
  consulted, asserted by call ordering; the permission module and its tests are
  unchanged; every valid deterministic command produces identical user-visible output
  including the planner regression corpus, and each deliberately changed
  malformed-argument path is enumerated, regression-tested, and recorded.
- Changelog: one entry — declarative per-action argument contracts are enforced before
  permissions are checked, and the enumerated malformed-argument paths now produce
  explicit typed errors instead of silent coercion.

### Stage V1-07 — Privacy-safe logging spine and typed failure classes

- Branch: `feature/privacy-safe-logging-spine`
- Prerequisites: V1-01
- Purpose: The ban on logging prompts, responses, private context, credentials, endpoint
  URLs, model paths, headers, and raw provider exception text has no enforcement today,
  and the existing house style is the opposite. The discipline and its test harness must
  exist before the first module that could leak.
- Owner-visible result: User-facing error text becomes consistent and stops carrying raw
  exception strings. Diagnostics for deterministic commands stay exactly as useful as
  today.
- Architecture affected: `core/logger.py` for bounded log growth, shared typed failure
  class definitions, `core/tool_manager.py` where raw exception text reaches the user,
  and a reusable sentinel-based log assertion helper in the tests.
- Privacy and security: Deliberately no global redaction filter, because an existing
  test asserts that the log does contain the absolute root path and repeated filenames
  the response omits; a blanket filter would break that contract and give false
  confidence. The discipline is module-scoped: new subsystems never hand raw values to
  the logger at all. An unbounded log file becomes a privacy problem at conversational
  volume, so log growth is bounded.
- Non-goals: Does not change the filesystem tool's existing, deliberately tested
  log-versus-response asymmetry. No global redaction or scrubbing filter. No model,
  network, adapter, or schema change.
- Completion criteria: A reusable sentinel-based log assertion helper exists and at
  least one test proves a known sentinel value never reaches a temporary log file;
  ToolManager no longer interpolates raw exception text into a user-visible message,
  proven by a test; the log file has a bounded-growth policy with a test; the filesystem
  tool's existing diagnostics assertions pass unchanged; no test touches the live log;
  the deliberate error-text change is enumerated, regression-tested, and recorded, while
  every other user-visible string stays identical.
- Changelog: one entry — typed failure classes and a privacy-safe logging discipline
  with bounded log growth, and tool execution failures no longer surface raw exception
  text.

### Stage V1-08 — Named safe filesystem roots

- Branch: `feature/named-safe-filesystem-roots`
- Prerequisites: V1-06, V1-07
- Purpose: The shipped safe filesystem root is the project directory itself, so the only
  root today contains Maria's own database, log, configuration, and source, while
  filesystem reading and listing are permitted by default. This must close before any
  model-originated plan can ever name a path.
- Owner-visible result: The owner declares named roots with per-root policy. Maria's
  live database, logs, credentials, private local configuration, runtime-generated
  state, and personal-data backups become unreachable through every filesystem and
  project capability. A named read-only root for Maria's own source is defined but stays
  inert until Project Maria is explicitly activated in V1-24.
- Architecture affected: `core/config.py` where a single path becomes named roots with
  fail-closed validation, `core/constants.py` for the permanent exclusion set,
  `tools/filesystem.py` root resolution and display paths, `config/config.toml` where
  the shipped default stops being the project directory, the action-contract registry
  path root class, and the filesystem tests.
- Privacy and security: The permanent exclusions are enforced unconditionally in code
  after path resolution and hold even when a root is deliberately configured to contain
  them. Maria's source and tracked documentation are not permanently excluded; they are
  reachable only through a root whose policy is read-only and whose activation is
  explicit. Tracked, non-personal configuration templates are part of that source tree;
  the untracked machine-specific override is not. Per-root read-only versus organize
  policy prevents a later model-originated plan from mutating inside a read-only root.
  Malformed root configuration aborts loading, matching the existing permission-array
  precedent. Filesystem deletion and writing stay mandatorily denied. Rejection messages
  continue never to name the rejected absolute path.
- Non-goals: No new filesystem action, no delete, no write. No organization behavior,
  which is V1-25. No project activation, which is V1-24. No write access to Maria's own
  source under any configuration. No content indexing, globbing, recursive walking, or
  archive handling. No change to the existing filesystem command grammar or read limit.
  No model and no compiler.
- Completion criteria: The owner has decided in writing, before the branch is opened,
  the exact named roots, the policy attached to each root, the shipped default root, and
  the definition of the Project Maria read-only root. A test proves the live database,
  log, untracked local
  configuration override, runtime state, and personal-data backups are rejected by every
  filesystem action even when a root is deliberately configured to contain them; a test
  proves the Maria-source root is read-only, so every mutating action inside it is
  refused while reads succeed; per-root policy is enforced for rename and move; a
  symbolic link pointing outside a root is rejected; the read limit, replacement
  decoding, and content-free rejection messages are preserved with no weakened
  assertion; the shipped default root changes as a deliberate, security-motivated,
  user-visible change that is documented, regression-tested, and recorded, while every
  other filesystem behavior stays identical; `docs/tools.md` is updated in this pull
  request.
- Changelog: one entry — the single filesystem safe root becomes named roots with
  per-root policy, Maria's live database, logs, credentials, private local
  configuration, runtime state, and personal-data backups are excluded from all of them,
  a read-only root for Maria's own source is defined but not yet activatable, and the
  shipped default root changes.

### Stage V1-09 — Database schema versioning and migration foundation

- Branch: `feature/database-schema-versioning`
- Prerequisites: V1-01
- Purpose: There is no version marker of any kind: no schema version pragma, no schema
  table, no migration runner, no drift detection. Creating tables only when absent is a
  silent no-op against an older incompatible table, and tested migration from v0.2.0
  needs an identity that does not yet exist.
- Owner-visible result: Existing databases keep working untouched and are stamped as the
  baseline. A database written by a newer build is refused with an explicit message
  instead of silently half-working.
- Architecture affected: the memory layer as sole SQL owner, `core/constants.py` for a
  schema version constant distinct from `APP_VERSION`, memory tests, and
  `docs/database.md`.
- Privacy and security: Any pre-migration backup copy is written inside Maria's own
  database directory, never into a user-facing safe root and never anywhere tracked by
  Git. Migration logs step identifiers and version numbers only, never row content.
  Refusing a newer schema fails closed with a typed error rather than attempting a
  best-effort partial migration. An unversioned database is the canonical v0.2.0
  baseline, and that classification is explicit rather than inferred at each call site.
- Non-goals: Introduces no new table, column, or record type beyond the version stamp.
  The schema version is not `APP_VERSION` and must not be derived from it, because the
  application version stays frozen while the schema advances. No downgrade migration, no
  export or backup command, no encryption at rest, no journaling change, no pruning.
- Completion criteria: The schema version is readable and writable only through the
  memory layer, and the SQL-ownership guard tests still pass for every other module; a
  database created by the v0.2.0 initialisation with no version marker is classified as
  the baseline and stamped; a reusable fixture rebuilds the exact v0.2.0 baseline schema
  in a temporary directory and every later schema-touching stage reuses it unchanged;
  migration is ordered, idempotent, and atomic, an injected mid-migration failure leaves
  the prior version with no partial schema, and re-running initialisation on a migrated
  database is a no-op; a database stamped newer than the code raises a typed error and
  refuses to open, leaving the file untouched; no test reads, writes, or copies the live
  database; `docs/database.md` is updated in this pull request.
- Changelog: one entry — database schema versioning independent of the application
  version, with an ordered, idempotent, atomic forward migration from the v0.2.0
  baseline and a fail-closed refusal to open a newer schema.

### Stage V1-10 — Memory record-type boundary decision

- Branch: `refactor/memory-record-type-boundaries`
- Prerequisites: V1-09
- Purpose: The memory module is large, owns every SQL statement, and is defended by
  several hand-written guard tests. Version 1.0 roughly triples the number of record
  types, so the shape is decided once, before the growth, rather than implicitly by
  whoever adds the next table.
- Owner-visible result: None.
- Architecture affected: the memory module, or a memory package with one stable public
  facade; the existing SQL-ownership guard tests; a one-line development-contract
  amendment only if the shape changes.
- Privacy and security: Whichever shape is chosen, the sole-SQL-owner boundary stays
  mechanically enforced. The hand-written guards are replaced by one generalised guard
  that enumerates production modules, so a new module cannot quietly import the database
  driver without a matching test being written.
- Non-goals: No new record type, behavior change, schema change, or public API change
  visible to tools. No convenience refactor outside the memory boundary. If the owner
  chooses to keep a single module, this stage is the generalised guard test and the
  wording only.
- Completion criteria: The owner has approved one of the two shapes in writing before
  the branch is opened; the public memory API and every existing call site are
  unchanged; one generalised guard test enumerates production modules, asserts only the
  memory boundary imports the database driver, and fails when a new importing module is
  added; the full suite passes with no assertion changes outside the replaced guards.
- Changelog: none required — internal refactor with no user-visible, security, privacy,
  data, or upgrade effect.

### Stage V1-11 — Generic per-plan recording policy

- Branch: `feature/plan-recording-policy`
- Prerequisites: V1-05
- Purpose: Brain records unconditionally on all three terminal paths, and the existing
  recorded-message field replaces only the response half, so no value means record
  nothing and an empty string would still insert a row. A conversational reply is most
  naturally a zero-step plan, which the result-level mechanism cannot even reach.
- Owner-visible result: None. Every existing command records exactly what it records
  today; the guarantee simply becomes verifiable.
- Architecture affected: `core/planner.py` where `Plan` carries a recording policy,
  `core/brain.py` where the no-steps, permission-denied, and completed paths honour it
  generically, `core/tool_manager.py`, and the persistence section of
  `docs/architecture.md`.
- Privacy and security: The default is today's behavior, so nothing silently stops being
  auditable; suppression is a positive, explicit choice by the plan's producer.
  Suppression must be able to cover the request half too, since the cleaned user input
  is currently always persisted. A suppressed response writes nothing at all, so the
  interaction table cannot be used to infer that a transient exchange happened. Brain
  stays generic, enforced by a source-scan guard test.
- Non-goals: Nothing becomes transient in this stage. No conversation, model, summary,
  or audit record. No change to any existing command's recorded text. No schema change.
- Completion criteria: The policy is expressible on a zero-step plan, a completed plan,
  and a permission-denied plan, and is honoured on all three; a test proves a
  transient-policy zero-step plan writes zero rows, verified by querying a temporary
  database directly; a regression test over the full existing command corpus proves
  recorded text is identical for every command; a guard test asserts `core/brain.py`
  contains no tool-name or action branch.
- Changelog: none required — mechanism only, with no user-visible, privacy, data, or
  upgrade effect until V1-14 uses it.

### Stage V1-12 — Reasoning provider interface and adversarial fake

- Branch: `feature/reasoning-provider-interface`
- Prerequisites: V1-07
- Purpose: Define the typed model request and response contract, the typed failure
  classes, the strictly validated provider configuration, the local-override mechanism,
  and the deliberately hostile fake adapter every later model stage is proven against,
  with no transport, no real model, and no network code anywhere.
- Owner-visible result: Still no model-backed behavior; the shipped configuration keeps
  reasoning disabled and parses successfully. An enabled configuration missing a
  required active field now aborts startup loudly instead of being silently accepted.
- Architecture affected: a new core reasoning-interface module, `core/config.py`,
  `config/config.toml` as a disabled non-personal template, `Maria/.gitignore` for the
  local-override rule, and the tests where the adversarial fake lives and is never
  registered as a production provider.
- Privacy and security: Tracked configuration contains only a disabled, non-personal
  reasoning template. When reasoning is disabled, machine-specific values such as
  permitted host, endpoint, model path, model identifier, or credential are absent and
  the template still parses successfully. When reasoning is enabled through the
  deliberately untracked local override, transport class, permitted host, model
  identifier, hard request deadline, maximum input size, and maximum output size are all
  required, type-checked, range-checked, and enumeration-checked, with no silent
  default, and an enabled configuration missing any one of them aborts loading. No
  personal host, endpoint, model path, credential, or private-server value may appear in
  tracked configuration. Unknown keys are rejected inside the reasoning block only, so
  the existing tested ignore-unknown-keys rule elsewhere stays green unmodified. The
  resolved configuration becomes sensitive once enabled and is never represented,
  formatted, or interpolated into a log line. No credential, token, header, or
  authentication field ships at all. The interface carries no context field, so no
  personal context can be embedded in a prompt template.
- Non-goals: No socket, TLS, URL, HTTP, asynchronous, threading, or subprocess import.
  No real model, transport, Brain wiring, or compilation. No cloud adapter, second
  provider, or automatic provider, model, endpoint, or destination fallback. The shipped
  default stays disabled, so the product remains deterministic-only out of the box.
- Completion criteria: Request and response are frozen dataclasses with explicit bounded
  fields and short purpose-first docstrings; failure classes cover at minimum
  unavailable, timeout, malformed output, over-budget, and refused; the adversarial fake
  can emit malformed structure, unknown tool, unknown action, extra arguments,
  out-of-range values, injected instruction text, truncated output, and oversized output
  on demand; a disabled tracked template parses successfully with no active provider
  field present; an enabled configuration missing transport class, permitted host, model
  identifier, hard request deadline, maximum input size, or maximum output size fails
  closed, one test per field; every wrong type, out-of-range value, bad enumeration
  value, and unknown key inside the block aborts loading, one test per case; the owner
  has chosen the local-override mechanism in writing, the tracked template contains no
  machine-specific value, `Maria/.gitignore` gains the protecting rule in this pull
  request, and a test asserts the tracked configuration parses to a disabled provider
  with no host, endpoint, model path, or credential present; the new configuration
  fields do not break the existing direct construction sites in the tests; a guard test
  asserts the interface module imports no network, threading, asynchronous, or
  subprocess module; a sentinel test asserts the fake's payload text, the host, and the
  model identifier never appear in a temporary log; the existing unknown-keys test and
  the shipped-configuration version test pass untouched.
- Changelog: one entry — a typed, provider-independent local reasoning interface with
  typed failure classes and fail-closed provider configuration, disabled by default,
  with machine-specific settings confined to an untracked local override.

### Stage V1-13 — Selected local reasoning adapter

- Branch: `feature/local-reasoning-adapter`
- Prerequisites: V1-12
- Purpose: Implement the one owner-controlled local runtime behind the V1-12 interface,
  including the project's first transport if the selected modality needs one, with a
  hard deadline, bounded input and output, and typed failures.
- Owner-visible result: Still nothing through the CLI, because the adapter is
  deliberately not wired into Brain. The owner verifies it through its own tests, its
  deadline behavior, its unavailable path, and one controlled manual compatibility
  checkpoint.
- Architecture affected: a new core local adapter module only, with all
  provider-specific behavior confined there; `core/config.py` consumed; `README.md`,
  whose shipped-runtime networking statement this stage corrects; and
  `requirements.txt`, which must remain standard-library only.
- Privacy and security: Loopback and private-LAN calls count as network transport and
  receive the full discipline of declared transport class, permitted host, hard
  deadline, and bounded input and output, all supplied by the untracked local override
  rather than tracked configuration. Typed failure classes only: never raw provider
  exception text, endpoint URLs, headers, credentials, local model paths, prompts, or
  responses in a log or in a user-facing message. No transport, authentication, or
  credential code ships for any modality the selected runtime does not use. Maria never
  automatically downloads a model, installs provider software, installs dependencies, or
  launches an unrestricted process. There is no provider, model, endpoint, or
  destination fallback: failure is a typed unavailable result, never a retry elsewhere.
- Non-goals: No Brain wiring, plan compilation, or context in the request. No cloud
  adapter, second adapter, or streaming design that bypasses the output bound. No model
  management, download, or installation surface. No third-party dependency. No
  real-model call is added to the automated suite.
- Completion criteria: The exact local runtime and transport modality are selected by
  the owner in writing and recorded before the branch is opened; an import-boundary
  guard test asserts Brain, the planner, permissions, ToolManager, the memory layer,
  configuration, and every tool import no network or threading module; deadline
  exceeded, oversized input, and oversized or truncated output each return a distinct
  typed failure, one test per case, with no hang and no unbounded read; a sentinel test
  drives one succeeding and one failing call with a known prompt and response and
  asserts that neither string, nor the host, nor the model identifier, nor raw exception
  text reaches a temporary log; all automated adapter tests use a local stub transport
  and never contact a real endpoint; the local-override ignore rule introduced in V1-12
  is re-verified present before the adapter can be enabled, and tracked configuration
  still contains no machine-specific value; `README.md`'s statement that the shipped
  runtime has no built-in networking or cloud integration is corrected in this pull
  request so it accurately describes the disabled, owner-controlled local reasoning
  adapter, because this is necessarily the first network-capable runtime stage; V1-13
  owns the local-adapter and networking wording, while V1-27 owns the
  public-internet-research wording; `requirements.txt` is unchanged and the full
  suite passes with no model running and no network available. In addition, before V1-14
  opens, the selected real owner-run local runtime must pass one controlled manual
  compatibility checkpoint through the real adapter, recorded in the pull request: one
  successful bounded request, one provider-unavailable result, one hard-deadline result,
  one oversized-output refusal, and proof that no prompt, response, host, model path,
  endpoint, or raw exception reached a log. That checkpoint is manual and is never added
  to the automated suite.
- Changelog: one entry — one owner-controlled local reasoning adapter with an enforced
  transport class, hard request deadline, bounded input and output, and typed failures.
  This is the first network-capable code in Maria's runtime; it is disabled by default
  and not yet reachable from any interface.

### Stage V1-14 — Deterministic-first conversation with bounded session context

- Branch: `feature/transient-text-only-conversation`
- Prerequisites: V1-04, V1-11, V1-13, and the V1-13 manual compatibility checkpoint
- Purpose: Wire the model into Brain for the first time, behind the deterministic
  planner, producing text only, and give it a bounded in-memory view of the current
  active session so follow-up turns are understood. Model output cannot select a tool,
  an action, or an argument at this stage and cannot reach ToolManager by construction.
- Owner-visible result: With reasoning configured, Maria converses and follows up within
  the current session: input that today receives the generic no-action response receives
  a real reply from the owner's local model, a second turn that depends on the first is
  understood, and nothing is written to interaction history. Ending the session discards
  the conversation entirely. With reasoning disabled, which remains the shipped default,
  every behavior is exactly as today. When the model is unavailable the owner receives a
  controlled response while every deterministic command still works.
- Architecture affected: `core/brain.py`, where the model is consulted only where the
  deterministic planner returns its generic no-action fallback; the core session module,
  which owns the bounded in-memory turn buffer; the adapter and the reasoning interface.
  The planner is unchanged, so resolution order lives in Brain and the deterministic
  path stays testable with no model.
- Privacy and security: The session context contains only the current request and a
  bounded set of earlier turns from the same active session. Hard limits cap both the
  retained turn count and the total bytes handed to the adapter, oldest turns are
  dropped first, and an oversized single turn is refused rather than silently truncated.
  The context carries no explicit memory, note, task, reminder, file, raw interaction
  history, or previous-session content, and a test inspects the request the adapter
  receives and asserts none of them are present. The buffer lives in process memory
  only, is never persisted, and is destroyed when the session ends. Model output is
  untrusted text and is never parsed into a tool, action, argument, or command; a
  behavioral test asserts ToolManager execution is never reached on any conversational
  path. Conversation is transient by default through the V1-11 policy, so nothing
  conversational reaches the database. Provider failure produces a controlled message,
  never a traceback and never raw provider text.
- Non-goals: No action selection, plan construction, or tool execution from model
  output. No personal context beyond the bounded session buffer: no memories, notes,
  tasks, reminders, files, raw interaction history, or context carried over from any
  earlier session. No conversation persistence, summaries, or audit records. No
  summarisation or compaction of dropped turns. No change to the planner's grammar, help
  text, or any deterministic command.
- Completion criteria: With reasoning disabled the full existing suite passes unmodified
  and the unrecognised-input fallback text is exactly today's text; resolution order is
  deterministic-planner-first, asserted by test; a follow-up test proves a second turn
  that depends on the first is understood within one session; the hard turn-count and
  total-byte limits are enforced and asserted at the boundary, with oldest turns dropped
  first and an oversized single turn refused; a session-isolation test proves a new
  session starts with empty context and cannot observe any earlier session's turns, and
  a test proves the buffer is discarded when the session ends; a test asserts the
  adapter request contains no memory, note, task, reminder, file, raw interaction, or
  previous-session content; a test drives the adversarial fake through malformed,
  oversized, truncated, injection-shaped, and action-shaped responses and proves each is
  rejected or rendered as inert text with zero tool invocations; a conversational
  exchange, including a multi-turn exchange, leaves the temporary database row count
  unchanged while every deterministic command's recorded rows stay identical; a
  model-unavailable run returns a controlled message and every deterministic command
  still succeeds in the same session; the deliberate change for previously unrecognised
  input is documented and tested in both the disabled and enabled configurations.
- Changelog: one entry — transient, text-only conversational responses with a bounded
  in-memory view of the current session, so follow-up turns are understood, using the
  owner's local model. Model output cannot select or execute any action, conversation is
  not recorded, and the session context is discarded when the session ends.

### Stage V1-15 — Memory context consent and Tier A assembly

- Branch: `feature/tier-a-context-consent`
- Prerequisites: V1-10, V1-14
- Purpose: Add the per-memory context-consent flag with a conservative default, assemble
  Tier A context with deterministic provenance, and retire the recent-interaction
  plumbing that currently feeds Tier C raw history into the planning path and which the
  planner discards unused.
- Owner-approved amendment (V1-15-R1): the V1-15 read-only entry audit found that this
  entry named record types that do not exist yet, promised a claim Maria cannot prove,
  and omitted modules the typed request and session boundary require. The owner
  approved the corrections below, and this entry is amended in the V1-15 pull request
  itself. The permanent safety boundaries are unchanged.
- Owner-visible result: With reasoning enabled, Maria answers conversational requests
  with the explicit memories the owner has enabled for context. In V1-15, Tier A
  consists of explicit memories only: a preference is represented only when the owner
  stores it as a memory and enables it, and structured tasks, reminders, and project
  facts do not exist until later stages add them. After every model-backed
  conversational response Maria names, from her own database ids, exactly which
  memories she supplied to that request; she does not and cannot claim which ones the
  model used. When no memory context was sent she says "No memory context supplied."
  When context was sent she says she cannot verify whether those memories are still
  current or whether they conflict. Every pre-existing and newly created memory
  defaults to not context-enabled, and raw interaction history is no longer assembled
  into any context.
- Transitional selection rule: an enabled memory is eligible on **any** model-backed
  conversational turn — a deliberate V1-15 policy, not relevance inference. Memories
  are taken in ascending id order, at most 16 of them and at most 2 KiB of fully
  rendered context including ids and framing, whole or not at all. The current request,
  the rendered context, and the retained conversation together fit the 8 KiB
  conversational limit or the configured input budget, whichever is smaller, with the
  request first, then fitting context, then the newest fitting earlier exchanges.
  Memories left out by a bound are disclosed; ids a bounded read did not retrieve are
  never named. The enable confirmation and the documentation state this rule. The
  relevance-review gate in Stage V1-16 revisits it.
- Architecture affected: the memory layer for the consent column, its forward migration,
  the one-id consent toggle, and the bounded Tier A read; a new core context module
  carrying provenance value objects, the selection rule, and the fixed notices;
  `core/brain.py` for reading Tier A on the conversational path and removal of the
  recent-interaction feed; `core/planner.py` for the consent commands and the context
  argument it currently deletes unused; `core/reasoning.py` for the typed,
  provenance-bearing context field and the shared byte accounting;
  `core/conversation.py` for the budget order, the owner-visible notices, and
  revocation checks; `core/ollama_adapter.py` for rendering context as delimited data;
  `core/session.py` for the supplied-memory fingerprints kept beside the conversation;
  `tools/memory.py`; the contract registry; `core/permissions.py`.
- Privacy and security: Storage existence is not consent to unrestricted use, so the
  migration sets every pre-existing memory to not context-enabled and that default is
  stated in the changelog and documentation. Enabling a memory widens automatic
  exposure, so the enable action is confirmation-required and is added to the mandatory
  set explicitly rather than left to the default-allow path; disabling is
  confirmation-required too. Consent applies to exact text, so updating a memory turns
  its consent off in the same transaction. Every context item carries a deterministic
  provenance identity produced by Maria, never by the model, and a model reply claiming
  another id cannot change the disclosure. Stored memory text is untrusted data: it is
  never placed in a system-level instruction, it travels only as clearly delimited
  data separate from Maria's fixed instructions, and it never grants tool authority.
  The session tracks fingerprints of supplied memories and discards its whole earlier
  conversation before sending anything once one is disabled, changed, or forgotten,
  including through another database connection; if Tier A cannot be read, neither
  context nor earlier exchanges are sent. This does not promise instantaneous
  revocation across a concurrent read-and-send race or erasure of text already
  delivered to a model. Tier C sources remain unreachable by any automatic path.
  Maria claims no record is stale, fresh, consistent, or conflicting without evidence;
  no `updated_at` column is added in V1-15. No retrieved fact or note body is logged.
- Non-goals: No Tier B bounded search, which is V1-16. No embeddings, semantic search,
  vector store, ranking, or inferred relevance. No inferred preferences, behavioral
  learning, or automatic enabling. No task, reminder, preference, or project-fact
  record type. No file content, log content, or model-originated provenance.
- Deliberate deterministic-command exceptions: every existing command keeps its text
  and recording behavior, except that the two new consent commands exist, `help` lists
  them, and updating a memory whose consent was on says that its consent was turned
  off. Updating a memory whose consent was off answers byte for byte as before.
- Completion criteria: The migration runs through the V1-09 runner from the frozen
  v0.2.0 baseline fixture and is tested for idempotency and atomicity; the version 2
  step runs its frozen historical statement, so version 1 reaches version 3 and
  reopens; a test asserts no interaction-history row can reach a reasoning request or a
  plan under any configuration; every context item disclosed to the owner carries its
  record id, and with every memory disabled no context is sent and Maria says so; the
  consent toggle takes a positive decimal integer id with a minimum of 1, affects
  exactly one row, is confirmation-required, and has a contract registry entry; a
  request with no context is byte-identical to V1-14's at the adapter boundary; Brain
  no longer passes recent interactions into planning and the planner no longer accepts
  and discards a context argument it never used; every deterministic command stays
  identical apart from the exceptions above and still works with no model present.
- Changelog: one entry — per-memory context consent, off by default for existing
  memories and confirmation-required to change, plus Tier A context assembly with
  deterministic provenance; raw interaction history is no longer assembled into the
  planning path.

### Stage V1-16 — Tier B bounded context search with visible provenance

- Branch: `feature/tier-b-bounded-context-search`
- Prerequisites: V1-15
- Purpose: Add the bounded automatic literal search over consented notes and
  context-enabled memories beyond Tier A's window that the tiered policy allows, with
  hard bounds, disclosed sources, and a registration point that later record types
  extend.
- Owner-approved amendment (V1-16-R1): the V1-16 read-only entry audit found that this
  entry described Tier B memories as "older" when, under Tier A's ascending-id order,
  they are the newer ones; that notes had no way to exclude sensitive text from
  automatic search; that its conflict and staleness criteria asked for claims Maria has
  no evidence for; and that it omitted the modules consent, revocation, and the request
  boundary require. The owner approved the corrections below, and this entry is amended
  in the V1-16 pull request itself. The permanent safety boundaries are unchanged.
- Relevance-review gate: this stage's read-only entry audit reviewed V1-15's
  transitional Tier A selection rule and presented the owner with keeping it or
  replacing it with an explicit relevance rule. The owner kept it unchanged (decision
  V1-16-R1): every context-enabled memory is eligible on every model-backed
  conversational turn, the first 16 in ascending id order are considered, and Tier A
  renders to at most 2 KiB on its own.
- Owner-visible result: With reasoning enabled, Maria also supplies, and names, stored
  records that literally contain a word of the current request: context-enabled memories
  beyond Tier A's window, and notes the owner has enabled for context with new
  confirmation-required commands `enable note context <id>` and `disable note context
  <id>`. Every note starts with consent off. After each response Maria names each record
  she supplied by source and id, such as `memory 21` or `note 4`, names matches the
  limits left out, and names same-title notes with different bodies as possible
  divergent versions; she does not claim the model used any record, and she makes no
  staleness or contradiction claim.
- Search rule: the terms are the cleaned current request, casefolded and split on
  whitespace, each word stripped of non-alphanumeric characters at both ends, kept at 4
  to 64 characters, de-duplicated in first-seen order, and the first 8 taken; a short
  request such as "Jo" or "AI" has no terms and triggers no Tier B read. A memory matches
  when a term occurs literally in its casefolded fact; a note matches when a term occurs
  wholly within its casefolded title or wholly within its casefolded body, never across
  the two. There are no model-generated queries, wildcard semantics, rankings,
  embeddings, or fuzzy or semantic matching. Tier B memory candidates are the enabled
  memories after the first 16 Tier A considers, so Tier A's 17th, lookahead, row is Tier
  B's first candidate, and a memory Tier A left out for size is never retried; notes are
  searched only while their consent covers exactly their current title and body. Each
  source's read returns at most 9 rows, which bounds the rows returned, not every row
  SQLite examines. Candidates are taken memories first, then notes, each in ascending id
  order, whole or omitted, at most 8 in all.
- Bounds: Tier A stays at 16 items and 2 KiB. Tier A and Tier B together are at most 24
  items and 4,096 fully rendered UTF-8 bytes, framing, source ids, JSON quoting, and
  escaped line separators included. The current request comes first, then Tier A, then
  Tier B memories, then Tier B notes, then retained earlier exchanges, all within the
  existing conversational input budget. The limits are validated again when a reasoning
  request is constructed and inside the adapter before any connection. A request with no
  context keeps V1-14's bytes, and one with Tier A alone keeps V1-15's.
- Architecture affected: the core context module for source-qualified items, the term
  rule, Tier B selection, the combined bounds, rendering, and notices; the memory layer
  for the schema version 4 forward migration, the per-note consent column holding the
  approved text's identity, the one-id note consent toggle, consent turned off by
  retitle and edit, and the bounded read-only search and recheck queries;
  `core/brain.py` for running those reads on the conversational path only;
  `core/conversation.py` for the budget order, the recheck, the 64-record limit, and the
  notices; `core/session.py` for holding at most 64 supplied fingerprints;
  `core/reasoning.py` and `core/ollama_adapter.py` for revalidating and framing the
  combined context; `tools/notes.py`; `core/planner.py`; the contract registry;
  `core/permissions.py`.
- Privacy and security: Hard bounds on result count and total context size, enforced and
  asserted. Tier C sources remain explicit-selection or approved-workflow only, asserted
  by a test that enumerates them; tests prove source and consent exclusion, and cannot
  prove an approved free-text note is free of secrets. Enabling a note widens automatic
  exposure, so both note consent actions are confirmation-required and added to the
  mandatory set; the enable notice says the entire approved title and body may reach the
  local model on a later literal match and that Maria does not check the note for
  secrets. Consent applies to exact text: an enable approval is bound to the title and
  body shown at confirmation time, retitle and edit turn consent off in the same
  transaction, and the stored consent is the approved text's identity, so text changed
  through any other connection is never eligible, in this session or a later one.
  Provenance is deterministic and cannot be supplied or altered by the model. No silent
  inclusion: every Tier B item is disclosed with its source and id. Stored text is
  untrusted, quoted data outside Maria's fixed instructions and grants no authority.
  Before every model-backed request the session's supplied records are rechecked by id;
  one that is missing, disabled, changed, oversized, or unreadable clears the earlier
  conversation and its fingerprints first, a memory merely moved past Tier A's window
  does not, and at most 64 fingerprints are kept. The recheck sees current state only: a
  record changed away and back to the identical approved text between two checks is not
  detectable. An unreadable context read sends the bare current request, as in V1-15. No
  assembled context is cached across requests or logged, and no fact, title, body,
  search term, or digest reaches a log or an error.
- Non-goals: No fuzzy, semantic, ranked, or embedding-based search; the existing literal
  and casefold comparison policy is extended, not replaced. No change to Tier A's
  selection rule. No file content, log content, credentials, or unrelated project data.
  No summaries, project records, tasks, or reminders yet; later stages register them as
  additional context sources. No `updated_at` for memories and no staleness inference.
- Deliberate deterministic-command exceptions: every existing command keeps its text and
  recording behavior, except that the two note consent commands exist, `help` lists
  them, and retitling or editing a note whose consent was on says that its consent was
  turned off. Retitling or editing a note whose consent was off answers byte for byte as
  before.
- Completion criteria: The migration runs through the V1-09 runner from the frozen
  v0.2.0 baseline fixture and from versions 1, 2, and 3, is tested for idempotency,
  atomicity, retry, and drift refusal, and leaves every existing note's consent off; a
  test enumerates Tier C sources and asserts none can enter automatic context; the
  result-count, combined-item, and total-size bounds are enforced and asserted at their
  exact boundaries; every supplied item is disclosed with its source and id; same-title
  notes with different bodies are disclosed as possible divergent versions with both
  ids, never as a contradiction, and no record is labelled stale; a previously supplied
  copy observed changed is reported as invalidated and its conversation cleared; a
  log-sentinel test proves no note title or body, memory fact, search term, or digest
  reaches a temporary log; deterministic commands are unaffected apart from the
  exceptions above and still work with no model present.
- Changelog: one entry — bounded literal search across notes the owner enables for
  context and enabled memories beyond Tier A's window, always shown with sources,
  possible divergent versions, and uncertainty, plus per-note context consent that is
  off by default and confirmation-required to change.

### Stage V1-17 — Model-to-plan compilation gate

- Branch: `feature/model-to-plan-compilation`
- Prerequisites: V1-06, V1-08, V1-12
- Purpose: Build the positive, deny-by-default allowlist and per-action argument
  validation that D-01 requires and that `PermissionManager` structurally cannot
  provide, and prove it entirely against hostile output before it is wired to anything.
- Owner-approved amendment (V1-17-R1): the V1-17 read-only entry audit found that no
  action contract yet declared model eligibility, and that a path argument's named root
  class is classification metadata only, so compilation cannot rely on it to keep a
  model-supplied path inside a root or away from Maria's runtime data. The owner
  approved the following, and this entry is amended in the V1-17 pull request itself.
  The permanent safety boundaries are unchanged. Every action contract declares its
  model eligibility explicitly, with no default and independently of permission: only
  `calculator.calculate`, `filesystem.list`, and `system.current_time` are
  model-eligible, and the other 23 registered actions are not. The compiler keeps its
  own closed positive allowlist, and a proposal compiles only when the allowlist and
  the contract's classification agree. A proposal is exactly one JSON object holding
  `tool`, `action`, and an `arguments` object, at most 4,096 UTF-8 bytes, parsed
  strictly and never repaired; argument text may carry no control, format, surrogate,
  line-separator, or paragraph-separator character; and a compiled plan is the
  existing single-step `Plan`, carrying the owner's cleaned request and transient
  recording. Temporary model-path restriction: a model-supplied path must be plain text
  relative to the default named root, with no root, drive, UNC share, leading `~`,
  parent traversal, NUL, colon or alternate data stream, or reserved device name under
  either the Windows or the POSIX reading. That check is a lexical preflight with no
  filesystem access and cannot establish symlink containment; V1-08's filesystem
  resolver remains responsible for resolved containment and the permanent exclusions
  when a plan eventually executes. Widening the restriction, for example to name a
  non-default root, needs a later owner-approved decision.
- Owner-visible result: None yet. The gate exists and is proven, but nothing reaches it.
- Architecture affected: a new core model-to-plan compilation module owning the positive
  allowlist; the action-contract registry, where model-eligibility flags become
  load-bearing; the planner's plan objects, consumed unchanged.
- Privacy and security: Deny-by-default lives here, not in `PermissionManager`: a tool
  and action pair absent from the positive allowlist is refused regardless of what the
  permission layer would return. Registered-action membership is explicitly not accepted
  as argument validation; every argument is validated against its contract. A path
  argument's named root class is classification metadata only and excludes nothing by
  itself: the compiler applies a lexical preflight to model-supplied path text, and
  V1-08's filesystem resolver enforces resolved containment and the permanent
  exclusions of Maria's runtime-sensitive data when a plan eventually executes. The
  compiler executes nothing: no tool, SQL, filesystem, network, shell, or generated
  code. Rejection reasons are typed and never echo model-supplied text, so injected
  content cannot be laundered into a user-facing message or a log line. A compiled plan
  is made immutable and fingerprinted before it can be rendered.
- Non-goals: Not wired into Brain, which is V1-18. No real adapter required, because it
  is tested exclusively against the adversarial fake. No multi-step plans, context,
  research, or filesystem mutation eligibility yet. `PermissionManager` is unchanged.
- Completion criteria: Every invalid hostile proposal the fake emits is refused before
  ToolManager could be reached, asserted by zero tool invocations: unknown tool, unknown
  action, registered-but-not-model-eligible action, extra argument, missing argument,
  wrong type, out-of-range value, path text the lexical preflight refuses, injected
  instruction text, injected approval or instruction fields, truncated output, oversized
  output, and malformed structure; a valid filename that merely reads like an
  instruction is not refused, and compiles only as inert path data in an otherwise
  unchanged single-step plan, again with zero tool invocations; the path tests are
  lexical and do not prove symlink containment, which remains the V1-08 resolver's
  responsibility at execution; a drift test asserts the allowlist is a strict subset of
  registered tool actions and fails in both directions, and adding a new action without
  an explicit model-eligibility decision fails a test; a test asserts the allowlist is
  consulted before any permission decision; no rejection message or log line contains
  model-supplied text; compilation output is an immutable plan carrying its fingerprint.
- Changelog: none required — internal logic that is unreachable and adds no new network
  or execution surface. The guarantee is recorded by V1-18.

### Stage V1-18 — Model-originated plans reach execution, with audit summaries

- Branch: `feature/model-originated-execution-and-audit`
- Prerequisites: V1-05, V1-10, V1-14, V1-17
- Purpose: Wire the compiler into Brain so model output can, for the first time, propose
  an action, and land the structured audit record in the same pull request so
  model-backed execution can never exist without its audit trail. This stage
  approval-gates its entire model-originated execution surface: every compiled plan is
  shown and approved before it runs, with no automatic execution of any kind. The narrow
  read-only automatic-research exception is introduced separately by V1-27.
- Owner-approved amendment (V1-18-R1). The V1-18 read-only entry audit found one
  conflict in this entry and several points it left undecided, and the owner approved
  the resolution below in writing before the branch opened. This entry is amended in the
  V1-18 pull request itself.
  - Findings: this entry's approval gate could not coexist with its statement that
    compiled plans pass through `PermissionManager` unchanged, because
    `PermissionManager` allows all three model-eligible actions outright. The audit
    lifecycle, proposal detection, request instructions, and argument rendering were
    undecided.
  - Mandatory approval:
    - Every plan the compiler produces carries the `MODEL` origin, a new plan field whose
      default is `DETERMINISTIC`.
    - Brain requires exactly one owner approval for a `MODEL` plan even when its
      permission decision is `ALLOWED`, and a `DENIED` decision refuses before any
      approval or audit.
    - The permission decisions themselves are unchanged and still apply; the approval
      requirement is Brain's, not a `PermissionManager` entry.
    - A `MODEL` plan is transient, has exactly one step and no canned response, and
      bypasses `prepare_confirmation`.
    - The approved plan is that same immutable plan. At confirmation and dispatch it is
      checked again against its shape and origin, the allowlist, the current registry's
      eligibility, its contract, and current permissions, without reparsing, rebuilding,
      substituting arguments, or calling a provider.
    - At confirmation the session checks the plan's exact type, its origin, and a model
      plan's shape before taking its fingerprint, which reads the origin and every
      step. A plan it cannot fingerprint gets the same fixed refusal as one whose
      fingerprint no longer matches.
  - Proposal classification:
    - A reply is a proposal attempt when its first character after JSON whitespace is
      `{`; every other reply stays conversational text.
    - A refused proposal answers with a fixed message and is never repaired or retried.
    - No proposal, refusal, approval prompt, or model-backed result becomes a retained
      exchange or an interaction row.
  - Request instructions and budget:
    - Each model-backed request carries Maria's fixed proposal instructions. A closed,
      exactly boolean request flag selects them and is off by default; with it off a
      request is exactly what it was.
    - The instructions are merged with the context instructions into one system message.
    - Budgeting reserves the request and every required instruction and separator byte
      first, then fits optional context, then earlier exchanges.
    - A request that cannot fit with its instructions is refused with the existing
      over-budget failure and no provider call; it is never sent without them.
  - Execution-attempt semantics:
    - An audit summary records an approved execution attempt that Maria authorised for
      dispatch; it does not prove the tool ran.
    - The summary is committed before dispatch, and the dispatch is refused when it
      cannot be. The outcome, `succeeded` or `failed`, is appended afterwards.
    - A summary without an outcome means the execution is unknown, and the tool is never
      rerun automatically.
  - Storage and deletion:
    - Schema version 5 has two tables, `audit_summaries` and `audit_outcomes`, created by
      one appended migration, `create_audit_summaries`.
    - Deleting by id removes one logical summary: its summary row and its optional
      outcome row, together in one transaction. Summary ids are never reused.
    - The owner approved this deletion of one logical summary under V1-18-R1.
      "Permanent Safety and Privacy Boundaries" names it as the one exception to
      the rule that a destructive or lifecycle action targets at most one row,
      and every other lifecycle action keeps that rule.
  - Owner-visible surface:
    - The commands are `audit`, `read audit <id>`, and `delete audit <id>`, and the last
      always requires confirmation.
    - No audit action is model-eligible, and no summary becomes model context.
    - The approval renders the declared tool, the action, and every argument value
      exactly, through a reversible approval-specific quoter, with no truncation.
  - Permission-risk assessment for the three new actions, all `NOT_MODEL_ELIGIBLE`:
    - `audit.list` and `audit.read` are `ALLOWED`. They read only Maria's own
      content-free audit vocabulary and change nothing. They never reach the model or
      its context, and history records only a fixed label and an id.
    - `audit.delete` is `REQUIRES_CONFIRMATION` and joins the mandatory set, so
      configuration cannot remove it. Deleting a summary removes the record of a
      model-proposed action and cannot be undone. It also leaves untouched whatever the
      operational log may still show, which its fixed notice tells the owner.
  - Footprint: the owner-approved scope is wider than "Architecture affected" names. It
    adds:
    - the plan origin in `core/planner.py`;
    - the request instructions in `core/reasoning.py`, `core/ollama_adapter.py`, and
      `core/conversation.py`;
    - rendering and approval in `core/session.py`;
    - `core/action_contracts.py`, `core/permissions.py`, `core/tool_manager.py`, the
      schema constant, and the schema migration;
    - the new `tools/audit.py`;
    - fixed `filesystem.list` logging in `tools/filesystem.py`.
  - The closed audit vocabulary and its validation live in a new `core/audit.py`, which
    the owner kept after reviewing this stage. It holds no SQL, input, output, or
    logging. The memory layer, Brain, and the audit tool share it, so `core.memory`'s
    guarded import boundary, under which only `Memory` may be imported from it, does
    not widen.
- Owner-visible result: Maria can act on natural language. The owner asks in their own
  words, Maria shows a resolved plan naming the tool, action, and exact arguments, and
  executes it only after approval. Anything not explicitly allowlisted is refused with a
  clear reason. A content-free audit record shows what she actually did.
- Architecture affected: `core/brain.py`, where the deterministic planner, the compiler,
  contract validation, `PermissionManager`, and ToolManager form one ordered path; the
  compilation module; the memory layer for the audit-summary table and its forward
  migration; the core session module for resolved-plan rendering and approval.
- Privacy and security: Approval binds to the compiled immutable plan. Maria may not
  re-plan, re-call the model, or substitute arguments after approval, and a changed plan
  invalidates approval, asserted by a test counting zero provider invocations between
  approval and execution. Compiled plans still pass through `PermissionManager`
  unchanged, so a denied action is refused exactly as for a deterministic plan, and
  under V1-18-R1 every compiled plan also needs the owner's one approval whatever
  `PermissionManager` decides.
  The audit summary stores action, decision, outcome, and a non-sensitive target only,
  never raw prompts, raw responses, selected private context, or returned private
  content, and sensitive target identity is redacted or generalised. Audit records are
  append-only; deletion removes one logical summary, by id, and is
  confirmation-required. Existing deterministic commands keep their current recording
  behavior unchanged.
- Non-goals: No multi-step plans yet, which is V1-19. No model-originated filesystem
  mutation: rename, move, and organization stay deterministic-only until V1-25. No
  research and no network access from the compiler. No tool creation, code generation,
  or code execution. No permission, configuration, or scope change originating from
  model output.
- Deliberate deterministic-command exceptions: the three audit commands are new, and
  every existing command keeps its text and recording behavior, with three exceptions:
  - `help` lists the three new commands.
  - The filesystem `list` action logs fixed lines without its path or exception text,
    while what the owner sees from a listing is unchanged.
  - The thirteen ID-only planner commands share one parser, corrected at the owner's
    request on reviewing this stage; `read note <id or exact title>` uses its
    separate, unchanged lookup path. The parser strips leading zeroes however many
    there are and refuses more than 19 significant digits before any conversion. It
    then converts at most 19 digits and compares the result with 9223372036854775807,
    SQLite's largest integer, which no record's id can exceed. Each of the thirteen
    answers a larger id with its existing invalid-id response before any step,
    approval, or model call. Because the parser never converts more than 19 digits,
    Python's limit on converting long digit strings, which stays in force, is never
    reached. Before, such an id reached the tool and failed, and an operand of more
    than 4,300 digits, leading zeroes included, stopped the command line with an
    unhandled error.
- Completion criteria: The owner has decided in writing, before the branch is opened,
  the owner-visible audit-summary retrieval and lifecycle surface, meaning how audit
  summaries are listed, read, and removed; the exact command grammar is not fixed here.
  An approved compiled plan executes exactly once with exactly the
  approved arguments and no second model call; a compiled plan targeting a denied action
  is refused by the permission layer and executes nothing; a sentinel test drives a
  model-backed tool execution with a known prompt, known response, and known sensitive
  content and asserts the stored audit row and the log contain none of them and that a
  sensitive target is redacted or generalised; the audit migration runs from the frozen
  v0.2.0 baseline fixture; with reasoning disabled the full deterministic suite passes
  identically and every command works with no model present.
- Changelog: one entry — natural-language requests can now be compiled into a validated,
  allowlisted plan that the owner approves before execution, with content-free
  structured audit summaries for every model-backed tool execution.

### Stage V1-19 — Foreground multi-step plans

- Branch: `feature/foreground-multi-step-plans`
- Prerequisites: V1-18
- Owner-approved amendment (V1-19-R1). The V1-19 read-only entry audit found
  that every model boundary, the owner-approved proposal instructions, and the
  audit gate assumed one step, that this entry left the step bound, plan
  depth, proposal format, and audit design undecided, and that "approved once"
  could be read against the permission-driven approval of deterministic plans.
  The owner approved the resolution below in writing before the branch opened,
  and this entry is amended in the V1-19 pull request itself. It supersedes,
  from this stage on, V1-18-R1's rule that a `MODEL` plan has exactly one
  step; V1-18-R1 stays recorded above exactly as it was approved.
  - Scope: In V1-19, only the model proposal path gains multi-step production.
    The deterministic command planner keeps its existing zero-or-one-step
    grammar; internally constructed deterministic plans retain multi-step
    support within the five-step bound. No command-chaining syntax is added,
    and a conjunction stays ordinary text inside one command.
  - Bound and depth: every plan of any origin holds at most five steps,
    `PLAN_MAX_STEPS`, refused when it is built and counted again wherever a
    plan is confirmed or run, so a plan altered after it was built cannot run.
    A deterministic plan keeps zero to five steps and every freedom its steps
    had; a `MODEL` plan holds one to five in an exact tuple, is transient, and
    has no canned response. A plan is flat: no step holds a step or a plan,
    nothing a step returns adds one, and there are no loops, branches,
    conditionals, or references to another step's output.
  - Proposal format: the single-action object is unchanged, and two to five
    actions are one object of exactly `steps`, a list of single-action objects
    run in order. The 4,096-byte cap covers the whole reply, the nesting cap
    stays eight, strict parsing is unchanged, and so is classification: only a
    reply opening with `{` is a proposal attempt, and one opening with `[`
    stays conversation. Compilation is all or nothing, every action passing
    every existing gate in order before permission is consulted, the first
    failure deciding one fixed refusal, and a malformed envelope has its own
    fixed refusal. Arguments are literal: `$1` or `{{step1}}` is text.
  - Deterministic path: deterministic approval, rendering, execution
    reporting, and recording are unchanged, except for the five-step bound.
  - Approval: a `MODEL` plan of any length needs exactly one owner approval,
    for the whole plan, rendered in full from the very plan the approval
    binds, every step numbered with its exact arguments. At the offer and
    again at confirmation every step is rechecked and its permission resolved
    before any audit write or tool, and any denied step refuses the whole
    plan with the first such step's reason. After approval there is no
    provider call, planning, context retrieval, parsing, confirmation
    preparation, substitution, retry, rollback, or background work.
  - Audit: each step reached for dispatch gets one summary, committed
    immediately before that step, then its outcome. The plan continues only
    after a successful result whose outcome was recorded, and stops when a
    summary cannot be recorded or its id is not valid, when the tool fails,
    when the outcome cannot be recorded, or on a handled `KeyboardInterrupt`;
    the approval stays spent. No schema, grouping field, `not_run` outcome,
    migration, or audit vocabulary is added: a step never reached gets no
    summary, a reached attempt may keep a summary without an outcome, and the
    audit rows cannot reconstruct which summaries belonged to one plan.
  - Report: a plan of several steps answers with one block per step, opening
    `Step i of N, tool.action:` and its status, then the tool's own message
    and the step's audit notice, and, when it stopped for any reason, one
    closing sentence naming where and why it stopped and which steps did not
    run. No
    label or closing sentence repeats an argument value, and nothing claims a
    failed or interrupted step had no effect. A handled interruption reports
    a step not yet dispatched as not run, a step interrupted inside its tool
    as unknown, a step whose tool returned with that known result, and an
    interrupted audit write as unconfirmed; nothing is retried, resumed, or
    recorded as failed because of it. Only `KeyboardInterrupt` is handled, a
    plan of one step lets it propagate as before, and an abrupt end of the
    process can leave no report.
  - Privacy and transience: proposals, refusals, approval displays, step
    results, and partial reports of a `MODEL` plan stay transient: no
    interaction row and no retained exchange, and nothing they contain is
    logged; Brain's new stop line holds only step numbers and a fixed code.
    V1-18's retention and clearing of supplied-context fingerprints is
    unchanged.
  - Instructions: the proposal instructions are the owner-approved revised
    text, 1,327 UTF-8 bytes, 312 more than V1-18's, and no budget grew: under
    the 8,192-byte ceiling a request with no context or earlier conversation
    may carry at most 6,865 prompt bytes in proposal mode.
  - Compatibility: valid single-step proposals, plans, fingerprints, approval
    renderings, results, and audit rows are unchanged, and so is deterministic
    behavior apart from the bound. The deliberate changes are the instruction
    text, the widened `MODEL` shape and its message, the malformed-envelope
    refusal, and the universal step bound.
  - Footprint: the approved scope is wider than "Architecture affected"
    names. It is `core/planner.py`, `core/plan_compiler.py`, `core/brain.py`,
    `core/session.py`, and `core/reasoning.py`, with their tests and the
    documentation. The memory layer, schema, migrations, audit vocabulary,
    tools, Tool Manager, permissions, action contracts, conversation,
    adapters, `main.py`, configuration, and dependencies are unchanged.
- Purpose: Let a plan contain more than one step, now that whole-plan permission
  resolution, immutable fingerprinting, and binding approval are all in place and
  proven. Today the planner only ever emits zero or one step.
- Owner-visible result: A multi-step request shows a numbered, fully resolved plan up
  front, is approved once as a single immutable unit, and on failure states exactly
  which steps ran and which did not.
- Architecture affected: `core/planner.py` for multi-step construction, the compilation
  module, `core/brain.py`, and the core session module for plan rendering.
- Privacy and security: Approval binds to the whole-plan fingerprint; a changed plan
  invalidates approval, and no re-planning or model call may occur after approval. A
  single denied step denies the whole plan before any side effect. Step count and plan
  depth are bounded, and no step may introduce a new step at execution time. Rendered
  targets use sanitised display paths, never absolute host paths.
- Non-goals: No background or deferred execution. No transaction, rollback, or
  compensation across steps: stop-on-failure with an honest report is the documented,
  tested semantics, and the absence of rollback is documented rather than implied away.
  No loops, branches, conditionals, or unbounded plan expansion. No automatic retry or
  autonomous re-planning after failure.
- Completion criteria: A maximum step count is enforced and asserted; a plan containing
  a denied step executes zero steps, and a plan requiring confirmation requests it once
  for the whole plan before any step runs; on failure, execution stops and the response
  names exactly which steps completed; a post-approval plan change is rejected and
  executes nothing, with the provider called zero times between approval and execution;
  single-step behavior, output text, and recorded rows stay identical.
- Changelog: one entry — foreground multi-step plans, shown in full, approved once as an
  immutable unit, with explicit partial-failure reporting.

### Stage V1-20 — Explicit conversation persistence and approved summaries

- Branch: `feature/conversation-summary-persistence`
- Prerequisites: V1-10, V1-14, V1-16
- Owner-approved amendment (V1-20-R1). The V1-20 read-only entry audit found
  that this entry left undecided the route from the session's retained
  exchanges to storage, the raw save's lifecycle and tier, how a summary is
  drafted and accepted, summary consent, recording, and limits, and the
  module footprint; that its capture criterion cannot hold for text already
  present in an exchange; and that Tier B listed summaries without consent.
  The owner approved the resolution below in writing, with decisions D1 to D4
  as recommended, before the branch opened, and this entry is amended in the
  V1-20 pull request itself. The text after this amendment stays recorded
  exactly as it was written.
  - Records (schema 6): one appended step, `create_conversation_records`,
    creates `saved_conversations` and `conversation_summaries`, both empty,
    and changes nothing earlier. A saved conversation is an immutable Tier C
    record: one canonical compact JSON transcript of 1 to 8 `[user,
    assistant]` pairs of non-empty text, at most 8,192 UTF-8 bytes, validated
    on write and again on read, with no consent, no edit, and no context
    source. A conversation summary is one text of 1 to 2,048 UTF-8 bytes
    equal to its own stripped form, with `created_at`, `updated_at`, and a
    nullable `context_digest`. No foreign key, trigger, index, cascade, or
    audit vocabulary is added, and ids are positive and never reused. The
    shared limits, transcript codec, value objects, and fixed messages live
    in a leaf module, `core/conversation_records.py`, with no SQL, I/O, or
    logging.
  - Capture (D4): `save conversation` stores exactly the exchanges the
    session retains, as they stand, reads no other record, and leaves the
    conversation unchanged; like any request it discards a pending approval,
    a pending summary draft included. Its reply is fixed text in which only
    the new id and the exchange count vary, saying the copy is exact and
    that later changes to memories or notes do not change it. With nothing
    retained it answers that there is nothing to save and stores nothing,
    and its ordinary interaction row is still recorded. Saving twice stores
    two rows. The capture criterion below is reworded: the save persists the
    retained exchanges byte for byte and reads no other record, and text
    already in an exchange is kept verbatim even when it quotes a memory,
    note, file, or earlier session.
  - Drafting (D1): `summarize conversation` asks the local model once, and
    only on that request, for a draft of every retained exchange, with
    Maria's fixed 336-byte prompt, no stored context, and no proposal
    instructions, within the configured input budget or refused whole. A
    denied permission, unavailable reasoning, an empty conversation, and a
    recheck of every record supplied to the conversation each refuse before
    any request is made; a revoked, changed, or unreadable record also
    clears the conversation. The reply's outer whitespace is stripped once,
    and an empty draft or one over 2,048 UTF-8 bytes is discarded whole.
    Every outcome is transient, and a draft is never kept as an exchange.
    This is the one route for model-drafted text into storage, and the model
    selects no tool, action, or argument name on it.
  - Acceptance (D2): the draft is shown whole, escaped, in the ordinary
    confirmation of `conversation_summaries.create`, which becomes
    mandatory; its binding pins the draft's digest, and the tool stores only
    text matching it. Confirming makes no provider call, and any other
    request, a cancel, or the end of the session discards the draft. No
    audit summary is written: the owner chose and approved the action, and
    the stored row is its own trace.
  - Consent (D3): a summary's context consent starts off and changes only
    through `enable summary context <id>` and `disable summary context <id>`,
    both confirmation-required. Enabling shows the stored text and is bound
    to it; editing turns consent off in the same transaction; and text
    changed outside Maria is never eligible.
  - Tier B: an enabled summary is a third Tier B source, untitled, matched
    by the V1-16 literal term rule over its one text and named `summary N`,
    taken after memories and notes and supplied whole or not at all within
    the unchanged bounds: 8 Tier B items, 24 combined items, 4,096 bytes, 9
    rows per source, 2,048 bytes per item, and 64 rechecked records. The
    2,048-byte cap never guarantees that a summary is selected. A request
    that selects no summary is byte for byte the V1-19 request for the same
    inputs, configuration, and flags; one that selects a summary carries a
    third fixed framing naming conversation summaries. A supplied summary
    joins the recheck that clears the conversation.
  - Mechanism: the two texts Maria supplies, the transcript and the draft,
    are contract-declared session inputs. Brain supplies one only in
    `process`, generically, for a planner's plan holding exactly
    DETERMINISTIC as its own origin: never at confirmation, for a
    model-originated plan, or on the conversational path. A plan that
    already carries the text, or holds any other step, is refused, and a
    contract declaring a session input cannot be model-eligible.
  - Commands and permissions: eleven commands, all `NOT_MODEL_ELIGIBLE`:
    `save conversation`, `conversations`, `read conversation <id>`,
    `delete conversation <id>`, `summarize conversation`, `summaries`,
    `read summary <id>`, `edit summary <id>: <new text>`,
    `delete summary <id>`, `enable summary context <id>`, and
    `disable summary context <id>`. The action inventory grows from 29 to
    40 and the mandatory confirmation set from 13 to 19, with fixed notices
    for storing a summary and enabling its context; model-eligible actions
    stay at three, and the denied floor is unchanged. `list summaries` stays
    a directory listing.
  - Recording and privacy: save, list, read, edit, delete, and both consent
    commands are recorded with content-free replies, list and read through
    a fixed `recorded_message`; an edit keeps its new text in the request
    half, as a note edit does; summarize is transient throughout. Logs
    carry ids, counts, and fixed codes only, never a transcript, draft,
    body, digest, or plan, and every new storage failure is a fixed message
    with no SQLite detail, contention included.
  - Footprint: new `core/conversation_records.py`, `tools/conversations.py`,
    and `tools/conversation_summaries.py`; changed `core/constants.py`,
    `core/memory.py`, `core/context.py`, `core/conversation.py`,
    `core/brain.py`, `core/action_contracts.py`, `core/permissions.py`,
    `core/planner.py` (routing and help only), and `core/tool_manager.py`;
    and `core/session.py` in its docstring only. `core/reasoning.py`,
    `core/ollama_adapter.py`, `core/plan_compiler.py`, `core/audit.py`,
    `main.py`, configuration, dependencies, and the version literals are
    unchanged.
  - Compatibility: the deliberate changes are the eleven commands and help
    lines, schema 6, the two conversation-clearing notices that now name
    summaries, and Brain's two session inputs. Every existing command's text
    and recording, the proposal allowlist and instructions, the compiler,
    and the audit vocabulary are unchanged.
  - Non-goals added: no automatic or end-of-session summary and no
    compaction of evicted turns; a saved conversation never re-enters a
    session; no transcript edits; no model-eligible conversation action; no
    relevance ranking.
- Purpose: Give the owner a deliberate way to keep the bounded session conversation
  introduced in V1-14, or an approved summary of it, with the same single-row,
  positive-integer-id lifecycle discipline as every other record type. V1-16 is a
  prerequisite because saved summaries become a Tier B source through the context
  module.
- Owner-visible result: Explicit commands to save the current session's bounded
  conversation and to accept a summary Maria proposes, plus list, read, edit, and delete
  for saved summaries by id. Nothing is ever kept without an explicit command, and once
  a session ends its conversation is gone and can no longer be saved.
- Architecture affected: the memory layer for a new table and its forward migration; the
  core session module, whose bounded turn buffer is the only source the save command
  reads; a summaries tool with its build factory and registration; `core/planner.py`
  routing; the contract registry; `core/permissions.py`; the core context module, where
  saved summaries become a Tier B source.
- Privacy and security: Persistence happens only by explicit owner action; the save
  command captures exactly the bounded session buffer and nothing outside it; a proposed
  summary is shown in full before it is stored, and model text never silently becomes a
  memory, note, task, or reminder. Every new action receives a permission-risk
  assessment: save, list, and read are allowed, while edit and delete are
  confirmation-required and single-row by id. No bulk, wildcard, clear-all, date-range,
  or cascade deletion; no automatic retention or pruning; no undo. Summary content is
  never logged.
- Non-goals: No automatic summarisation and no silent persistence. No inferred
  preferences and no behavioral pattern learning. No cross-session memory beyond what
  the owner explicitly saved. No recovery of a conversation from an ended session.
- Completion criteria: The migration runs from the frozen v0.2.0 baseline fixture and is
  idempotent and atomic; a test asserts nothing is stored without an explicit command,
  including after a long multi-turn exchange; a test asserts the save command persists
  exactly the turns held in the bounded session buffer and no memory, note, file, raw
  interaction, or earlier-session content; a test asserts an ended session leaves
  nothing to save; new confirmation entries are added, asserted, and the action
  inventory is updated in the same pull request; ids are positive decimal integers with
  a minimum of 1, and lists follow the existing newest-first, twenty-row, more-results
  convention; tests write only to a temporary database.
- Changelog: one entry — explicit persistence of the current session's bounded
  conversation and owner-approved conversation summaries, with confirmation-required
  editing and single-row deletion.

### Stage V1-21 — Desktop GUI over the shared session

- Branch: `feature/desktop-gui`
- Prerequisites: V1-04, V1-19, V1-20
- Purpose: Add the simple desktop interface that becomes the normal daily surface, as a
  pure presentation layer over exactly the Brain, plan validation, permission, tool,
  memory, and audit path the CLI already uses, placed after multi-step approval and
  conversation exist so it has nothing to reimplement and nothing to rework.
- Owner-visible result: A desktop window for conversation, records, resolved-plan
  approval, and every deterministic command, sharing the CLI's exact approval semantics.
  The CLI is untouched and remains a first-class fallback, recovery, debugging, and
  power-user interface.
- Architecture affected: a new GUI package holding presentation and a headless-testable
  controller; `main.py` for interface selection; the core session module, consumed
  rather than extended; Brain, reused unchanged.
- Privacy and security: The GUI contains no permission logic, plan construction, plan
  mutation, SQL, or tool execution; a guard test asserts it imports neither the memory
  layer, ToolManager, permissions, nor any tool module, and reaches everything through
  the session API. Approval returns the same pending-approval object the core issued,
  and a mismatched fingerprint is rejected identically in both interfaces, asserted by
  one shared test exercised through both. There is no auto-approve, no remembered
  approval, no do-not-ask-again, and no capability the CLI cannot reach. No sensitive
  content appears in window titles, recent-item lists, crash dialogs, clipboard history,
  or the log, and no absolute host path the CLI would have sanitised. A GUI failure or a
  missing toolkit must never disable or degrade the CLI.
- Non-goals: No web server, browser interface, or remote access. No GUI-only command,
  shortcut, or setting that bypasses configuration validation. No second execution or
  permission architecture, no voice, and no window outliving the session. No third-party
  GUI toolkit: if the standard-library toolkit is unavailable on the owner's Python
  build, that is an owner decision to raise before implementation, not grounds for a
  dependency. No out-of-session notification delivery, which is V1-23.
- Completion criteria: The standard-library toolkit's availability on the owner's Python
  build is confirmed and recorded before implementation begins, and its absence produces
  a controlled message while the CLI still works, asserted by test; the toolkit is
  imported only by the GUI package, asserted by an import-boundary guard test; a
  headless test drives the controller layer without creating a window, and
  display-dependent tests are explicitly skipped rather than silently passing; one
  shared test asserts identical resolved plans, identical approval requirements,
  identical approval semantics, and identical mismatched-fingerprint rejection through
  the CLI session and the GUI controller; cancelling in the GUI records exactly what
  cancelling in the CLI records; the entire existing CLI suite passes unchanged, and
  running both interfaces in one process does not silently lose logging configuration.
- Changelog: one entry — a simple desktop GUI that reaches the same Brain, plan
  validation, permissions, tools, memory, and audit boundaries as the CLI, including
  identical plan approval, while the CLI remains first-class.

### Stage V1-22 — Structured tasks and reminders

- Branch: `feature/structured-tasks-and-reminders`
- Prerequisites: V1-06, V1-10, V1-15
- Purpose: Add tasks and reminders as first-class record types with due dates and a
  confirmed lifecycle, including natural-language time resolution the owner can see and
  correct, completing the secretary role with no delivery mechanism and no
  out-of-session surface.
- Owner-visible result: Deterministic commands to add, list, read, complete, reopen,
  edit, and delete tasks and reminders with due dates, in both interfaces, usable with
  no model present. The owner can also state a task or reminder in their own words:
  Maria proposes normalized date, time, timezone, and recurrence fields, shows the exact
  resolved values before anything is stored, and asks a clarifying question instead of
  guessing when the wording is ambiguous.
- Architecture affected: the memory layer for two new tables and their forward
  migrations; new task and reminder tools; ToolManager registration; `core/planner.py`
  routing branches; `core/permissions.py` for new permission entries; the contract
  registry and the compiler allowlist for per-action model-eligibility decisions,
  including the normalized timing fields; the core context module, where active tasks
  and reminders become Tier A.
- Privacy and security: Every new action receives an explicit permission-risk
  assessment, because anything in neither mandatory set is permitted by default: create,
  list, read, complete, and reopen are proposed as allowed, while edit and delete are
  confirmation-required. Whether creating a task or reminder from an explicit
  natural-language request requires confirmation stays that explicit risk decision;
  using natural language does not by itself make it a mandatory-confirmation action. The
  mandatory-confirmation baseline is a floor that configuration may extend but never
  shrink, not a cap, and a test proves configuration cannot shrink the extended set.
  Records are targeted single-row by positive integer id, with no bulk, wildcard,
  clear-all, date-range, or cascade deletion, no automatic retention or pruning, and no
  undo. Maria may interpret an explicit natural-language task or reminder request and
  propose normalized date, time, timezone, and recurrence fields, but every proposed
  timing field passes through deterministic argument-contract validation before storage,
  and the exact resolved date, time, timezone, and recurrence are shown to the owner.
  Ambiguous, incomplete, impossible, or timezone-dependent wording triggers a
  clarification request rather than a silent assumption. Casual conversation never
  becomes a task or reminder automatically; only an explicit request does. The model
  cannot bypass the task and reminder allowlist, the argument contracts, the permission
  decision, or the normal audit path, and a proposed timing field remains a proposal
  until the validator accepts it. Task and reminder content is never logged, and the
  retention and deletion documentation is extended to cover the new record types.
- Non-goals: No scheduling, notification, or delivery, which is V1-23. No recurrence
  rules beyond the owner-approved initial recurrence scope. No task or reminder created
  from casual conversation, and no acting on inferred intent where the owner did not
  explicitly ask. No external calendar or account integration. No project linkage yet,
  which is V1-24.
- Completion criteria: The owner has decided in writing, before the branch is opened,
  the canonical timezone source and any override, how relative dates are interpreted,
  how daylight-saving ambiguous or nonexistent local times are handled, the task and
  reminder attachment model and the cascade semantics that follow from it, and the
  initial recurrence scope. All four tool-integration steps are completed for each tool;
  migrations run through the V1-09 runner from the frozen v0.2.0 baseline fixture and
  are idempotent and atomic; every new action has a contract entry, an explicit
  model-eligibility decision, an explicit permission-risk decision, and passes the
  registry and allowlist drift tests; ids are positive decimal integers with a minimum
  of 1, due-now and overdue are queries, and a test asserts nothing is delivered by this
  stage. Tests cover one unambiguous natural-language reminder that resolves and stores
  correctly; an ambiguous request that produces a clarification request and stores
  nothing; an invalid or impossible local time that is refused rather than silently
  shifted; deterministic timezone resolution that does not depend on model output; the
  exact resolved date, time, timezone, and recurrence being shown to the owner before
  storage; and zero task or reminder creation from casual conversation. The generalised
  SQL-ownership guard covers the two new tools; `docs/database.md`, `docs/tools.md`, and
  `docs/api.md` are updated in this pull request; every task and reminder command works
  with no model present.
- Changelog: one entry — structured tasks and reminders with due dates, deterministic
  lifecycle commands, mandatory confirmation on every destructive mutation, and
  natural-language time resolution whose proposed date, time, timezone, and recurrence
  are validated deterministically and shown before anything is stored.

### Stage V1-23 — Narrow closed-app reminder-delivery subsystem

- Branch: `feature/reminder-delivery-subsystem`
- Prerequisites: V1-21, V1-22
- Purpose: Give Maria a narrow, least-privilege mechanism that can notify the owner of a
  due reminder while the Maria GUI and CLI are closed, constructed so it cannot grow
  into a general background agent.
- Owner-visible result: Due reminders reach the owner even with Maria closed, and also
  surface in-session in the GUI and in a CLI-only session. The subsystem can be enabled
  and disabled at any time, and the whole product works with it off.
- Architecture affected: the reminder-delivery subsystem, whose exact module shape and
  operating-system integration mechanism are decided by a Stage V1-23 audit and owner
  decision before the branch is opened; `core/config.py` for the enable and disable
  setting; the core session module for the in-session delivery seam; `main.py` and the
  GUI as in-session surfaces; the memory layer for the delivered marker.
- Privacy and security: The subsystem may only read due reminders, determine whether a
  reminder should fire, deliver a notification, mark a reminder as delivered, and report
  missed or overdue reminders. It may never invoke the reasoning model; construct,
  modify, compile, or approve a plan; invoke ToolManager or any general tool; access
  projects, files, memories, notes, or interaction history beyond the minimum reminder
  record needed for delivery; perform internet research; modify permissions; perform
  general background or project work; or become a general autonomous agent. That
  envelope is enforced by an import-and-call-path guard rather than by convention.
  Reminder text is displayed, never logged. A single-instance guard prevents double
  delivery when a session is also open. Whichever operating-system mechanism is chosen
  must be installable, inspectable, and removable by the owner, and its footprint is
  documented.
- Non-goals: No general unattended project development, long-running delegated
  workflows, autonomous background coding, or workflow adaptation while the owner is
  absent; all remain post-v1.0. No background permission decisions, approvals, model
  calls, tool calls, or network calls. No scheduling of anything other than reminders.
  No promise of delivery while the computer is completely powered off; sleeping-device
  wake behavior depends on the chosen mechanism and is part of the Stage V1-23 owner
  decision.
- Completion criteria: The operating-system mechanism is selected by the owner in
  writing and recorded before the branch is opened, together with an explicit statement
  of its sleeping-device wake behavior and whether that is in scope; a guard test
  asserts the subsystem imports and calls nothing in reasoning, model-to-plan
  compilation, ToolManager, general tools, project assistance, or internet research,
  and no network module; a behavioral test asserts zero tool invocations and zero
  provider invocations across a full delivery cycle; a test asserts it reads no memory,
  note, interaction, project, or file record beyond the reminder fields needed to
  deliver; a reminder is delivered at most once, asserted across a restart; a reminder
  whose window was missed surfaces as overdue when the subsystem next becomes available;
  delivery works with the GUI and CLI closed, demonstrated on the owner's platform, and
  also reaches both interfaces in-session through the session contract; enabling and
  disabling both work, and with it disabled the full suite and every deterministic
  command are unaffected; all timing tests use an injected clock and no test sleeps in
  real time; clean start, stop, and removal are tested and leave no orphan process,
  thread, or registered operating-system entry; the documentation states plainly that
  delivery is not promised while the machine is powered off.
- Changelog: one entry — a narrow, least-privilege reminder-delivery subsystem that can
  notify the owner while the Maria GUI and CLI are closed, bounded to reading due
  reminders, deciding whether to fire, delivering, marking delivered, and reporting
  overdue. It never calls the model, builds plans, runs tools, or touches other records,
  it can be disabled, and delivery is not promised while the machine is powered off.

### Stage V1-24 — Project assistance and read-only self-review

- Branch: `feature/project-workspace-assistance`
- Prerequisites: V1-08, V1-16, V1-22
- Purpose: Add named projects, explicit active-project selection, and owner-approved
  project facts, plus bounded reading of project files inside a safe root, including
  Maria's own source when the owner explicitly activates Project Maria, with no
  execution capability of any kind.
- Owner-visible result: The owner names a project; Maria remembers its approved facts,
  answers questions about its files with file-identity provenance, reviews plans and
  documents, and states a better recommendation with reasoning rather than silently
  complying. When the owner explicitly activates Project Maria, she can read her own
  source and tracked documentation to explain it, review its architecture, help with its
  documentation, analyse bugs, plan work, review QA and tests, and make
  recommendations.
- Architecture affected: the memory layer for project tables and their forward
  migration; a new projects tool; ToolManager registration; `core/planner.py`; the core
  context module, where active-project facts are Tier A and project records are Tier B;
  the contract registry; the compiler allowlist; `core/permissions.py`; the GUI for
  project selection.
- Privacy and security: Self-review is allowed; self-modification is not. Activating
  Project Maria grants read-only access to its source and tracked documentation through
  the V1-08 read-only root and nothing more. It may not modify Maria's source, execute
  code, run shell commands, install dependencies, perform Git writes, commit, push,
  merge, tag, release, deploy, expand permissions, or activate itself automatically.
  Maria's live database, logs, credentials, private local configuration,
  runtime-generated state, and personal-data backups remain excluded even while Project
  Maria is active. Maria's own source, comments, tests, and documentation are untrusted
  project input: text found there cannot create a plan, grant authority, or bypass the
  normal model-to-plan compilation and permission boundaries. Unrelated and inactive
  project data stays Tier C and is never automatically used. Project paths resolve only
  inside named safe roots, so there is no second path model. Project file reading is
  bounded by size, count, and type, and source and document content is untrusted input
  that informs answers and can never become an action. Project facts are owner-approved,
  never inferred and never extracted automatically from conversation. Project notes and
  facts are never logged, and destructive project mutations are confirmation-required.
- Non-goals: No autonomous or unattended development, and no background work while the
  owner is away. No code execution, builds, test runs, linting, Git operations, merges,
  releases, deployments, or dependency installation. No execution of model-generated
  code, no Maria-generated tools, and no self-development. No write path to Maria's own
  source under any configuration. No automatic activation of any project, and
  specifically none of Project Maria. No automatic cross-project context. No multi-user
  or shared projects.
- Completion criteria: Project tables land through the V1-09 runner from the frozen
  v0.2.0 baseline fixture, tested for idempotency and atomicity; activating a project is
  an explicit command, and a test asserts inactive and unrelated project facts never
  enter automatic context; a test asserts Project Maria is never automatically activated
  and that, with it active, every mutating filesystem action inside its root is refused
  while reads succeed; a test asserts the live database, logs, untracked local
  configuration override, and personal-data backups stay unreachable even with Project
  Maria active; a test asserts instruction-shaped text placed in a source file, comment,
  test, or document inside the active project cannot produce a plan step, grant
  authority, or alter a permission decision; a project path outside every named safe
  root is rejected, and file reads are bounded and refused outside the active project's
  root; every project-derived context item is displayed with its file identity; a
  repository-wide guard test asserts no production path executes shell commands,
  subprocesses, dynamically generated code, `eval`, `exec`, or model-produced source
  code, with registered-tool dispatch through ToolManager explicitly exempt and
  unaffected; at least one test exercises Maria stating a contrary recommendation with
  reasoning rather than silently complying; every project command works with no model
  present.
- Changelog: one entry — named projects with explicit activation, owner-approved project
  facts available as Tier A context, and bounded project-file reading with file-identity
  provenance. Maria can review her own source read-only when Project Maria is explicitly
  activated; she cannot modify it, execute anything, or act on instructions found inside
  project files.

### Stage V1-25 — File organization inside safe roots

- Branch: `feature/safe-root-file-organization`
- Prerequisites: V1-08, V1-19
- Purpose: Turn the named roots into an organizing capability, inspecting, renaming, and
  moving files within an organize-policy root, where every operation is enumerated in
  one immutable approved plan.
- Owner-visible result: Maria proposes a tidy-up for a folder the owner names, showing
  every single file and its destination, and performs exactly that after approval, or
  nothing at all.
- Architecture affected: `tools/filesystem.py` organization actions; `core/planner.py`
  organization command grammar; the contract registry safe-root argument class and the
  compiler allowlist, where filesystem mutation becomes model-eligible here and only
  here; `core/permissions.py`; the GUI and CLI for file-plan rendering.
- Privacy and security: Filesystem deletion and writing stay mandatorily denied, and
  every move and rename stays confirmation-required. Confirmation binds to the exact
  enumerated file list: no wildcard or glob may be resolved after approval, enforced by
  construction rather than convention, and a changed list invalidates approval.
  Containment is re-verified per file at execution time, not only at plan time. There is
  no destination overwrite; a collision refuses rather than replaces. Read-only roots,
  including the Project Maria source root, cannot be mutated, and Maria's
  runtime-sensitive data stays excluded through V1-08. File names and paths may
  themselves be sensitive: they are shown to the owner and never logged, and rejection
  messages still never name the rejected absolute path.
- Non-goals: No deletion, no writing of new file content, no archive creation or
  extraction, and no execution of anything found. No permission, ownership, or timestamp
  modification. No operation outside a single organize-policy root and no plan spanning
  multiple roots. No background, scheduled, or automatic organization. No content-hash
  duplicate detection without separate approval.
- Completion criteria: A dry-run preview enumerates every source and destination before
  approval, and approving executes exactly that list and nothing else, asserted against
  a temporary directory; rejecting the plan performs zero filesystem changes, and a plan
  containing one out-of-root target is rejected in its entirety before any file moves; a
  test proves post-approval wildcard expansion is impossible and that a file moved
  outside the root between plan time and execution time is refused at execution time; a
  test proves no organization plan can target a read-only root; destination-collision
  behavior is explicit and tested, and partial failure names exactly which moves
  completed; a model-originated organization plan is rejected unless every path argument
  passes the declared root class, driven by the adversarial fake; filesystem deletion
  and writing remain denied, with a test.
- Changelog: one entry — file organization inside organize-policy safe roots, with a
  fully enumerated preview, collision refusal, plan-bound confirmation, and honest
  partial-failure reporting.

### Stage V1-26 — Bounded read-only research transport

- Branch: `feature/public-research-transport`
- Prerequisites: V1-07, V1-13
- Purpose: Write the project's second and last network surface, read-only by
  construction and public-only by construction, entirely separately from the capability
  that uses it, so the transport policy gets its own dedicated security review.
- Owner-visible result: None yet.
- Architecture affected: a new core research-transport module only, and `core/config.py`
  for budgets, destination policy, and the enable flag.
- Privacy and security: Read-only retrieval only, with a hard deadline, response-size
  cap, request-count cap, redirect cap, content-type allowlist, and typed failures. No
  cookies, credentials, authentication headers, form submission, or state-changing
  request methods, made impossible by construction rather than by policy. No executable
  or archive content types, no downloads written anywhere, and no installation,
  purchase, or financial operation. It never logs URLs, response bodies, headers, or raw
  exception text. The transport accepts only approved public HTTP or HTTPS destinations.
  It rejects loopback, private, link-local, multicast, reserved, unspecified, and
  cloud-metadata-service destinations for both IPv4 and IPv6, and it validates the
  resolved addresses rather than the hostname string alone. It refuses a redirect into
  any forbidden destination and refuses a resolution that changes into a forbidden range
  between validation and connection, so DNS rebinding cannot reach a private host. It
  never contacts the configured private local-model endpoint. The local reasoning
  adapter remains the sole component permitted to contact the explicitly configured
  loopback or private-LAN inference host.
- Non-goals: No browser automation, headless browser, script execution, or crawling. No
  authenticated or paywalled access and no credential storage of any kind. No access to
  any private, loopback, or link-local host for any reason. No wiring into Brain or any
  tool, which is V1-27. No third-party dependency.
- Completion criteria: The owner has decided in writing, before this branch or V1-27
  opens, the approved public search and retrieval sources, the public-host policy,
  whether the research design must remain credential-free and cost-free, and the
  behavior when no acceptable public search source is configured. A guard test asserts
  only this module and the reasoning adapter import network modules, and that Brain, the
  planner, permissions, ToolManager, the memory layer, every tool, and the
  reminder-delivery subsystem do not; state-changing request methods and non-web
  redirect schemes are impossible, asserted by test; oversized, slow, and
  redirect-looping responses each produce a typed failure rather than a hang or
  unbounded read; a sentinel test asserts no URL, header, or body text reaches a
  temporary log file. Destination tests cover a direct forbidden IPv4 address, a direct
  forbidden IPv6 address, a hostname that resolves to a forbidden address, a redirect
  into a forbidden range, a resolution that changes into a forbidden range between
  validation and connection, and the configured local inference host being refused by
  the research transport while remaining usable by the reasoning adapter. All tests use
  a local stub transport and resolver and no test contacts the real internet;
  `requirements.txt` is unchanged.
- Changelog: one entry — a bounded, read-only, public-only research transport with hard
  deadlines, size, request, and redirect caps, a content-type allowlist, typed failures,
  and refusal of loopback, private, link-local, multicast, reserved, unspecified, and
  metadata-service destinations, disabled by default and not yet reachable, recorded
  because it ships a second network surface.

### Stage V1-27 — Public research capability with citations

- Branch: `feature/public-research-capability`
- Prerequisites: V1-16, V1-18, V1-26
- Purpose: Expose research as a bounded capability the local model may itself decide to
  use, with deterministic protection of private context, mandatory source attribution,
  and explicit uncertainty reporting. This stage introduces the one owner-approved
  exception to approval-gated model-originated execution: an explicitly allowlisted,
  single-step, low-risk, public read-only research action may run without confirmation,
  with a visible notice and a structured audit summary.
- Owner-visible result: Maria researches public web and documentation sources when she
  judges it helps, shows every source she used, compares sources, and states plainly
  what she is unsure about. When she decides research would help, she says so before she
  does it rather than pausing for approval each time. The owner can see exactly which
  hosts were contacted.
- Architecture affected: a new research tool; ToolManager registration;
  `core/planner.py` for a deterministic research command as well as the model-initiated
  path; the contract registry; the compiler allowlist; `core/permissions.py`; the memory
  layer, where audit summaries record hosts and outcome; and the scope sections of
  `README.md` and `docs/architecture.md`.
- Privacy and security: A deterministic guard refuses any query containing private
  context, including memory, note, task, reminder, summary, interaction, project-fact,
  and file content, asserted by a test that builds a request from input containing a
  stored memory and proves the memory text is absent. The model may decide that research
  helps; it may not decide the destination, transport, or privacy policy. Research
  reaches approved public HTTP or HTTPS destinations only. It cannot reach a loopback,
  private, link-local, multicast, reserved, unspecified, or cloud-metadata-service
  destination on IPv4 or IPv6, and it never contacts the configured private local-model
  endpoint; that endpoint stays reachable only by the local reasoning adapter. The
  execute-without-confirmation policy is scoped to exactly one case: an explicitly
  allowlisted, single-step, low-risk, public read-only research action. Every such
  execution shows a visible notice and writes a structured audit summary. It never
  extends to a mutating action, an action carrying private data, or a multi-step plan,
  each of which still requires a visible resolved plan and explicit approval under D-01.
  The owner can disable automatic research, after which research runs only when
  explicitly commanded. Retrieved content is untrusted input handled exactly like model
  output and like project-file content: it can never become an action without passing
  back through the compilation gate and the contract validator. Not allowed at all:
  account login, form submission, posting, modifying remote data, executable downloads,
  software installation, purchases, and financial operations. No query text, page
  content, or full URL path is logged beyond the permitted host record, and fetched
  content never silently becomes a memory, note, task, or reminder.
- Non-goals: Not unrestricted browser automation. No caching of fetched content into any
  record type without explicit owner action. No search of the owner's private data
  through an external service. No cloud model and no third-party dependency.
- Completion criteria: The owner has decided in writing, before V1-26 or this branch
  opens, the approved public search and retrieval sources, the public-host policy,
  whether the research design must remain credential-free and cost-free, and the
  behavior when no acceptable public search source is configured. Every returned claim
  carries a source identifier and an explicit uncertainty statement, and a result with
  no source is not returned; a test proves a research request aimed at a loopback,
  private, link-local, multicast, reserved, unspecified, or metadata-service
  destination is refused, and that the configured local inference host is refused here
  while remaining usable by the reasoning adapter; a test proves an
  automatic research execution displays its visible notice and writes a structured audit
  summary; a test proves the no-confirmation path refuses any research action that is
  not explicitly allowlisted, is multi-step, mutates anything, or carries private data,
  and that such a request falls back to the approval-gated path; a test proves disabling
  automatic research leaves only the explicitly commanded deterministic path; a test
  proves no Tier A, Tier B, or Tier C private value can appear in an outbound query
  under any configuration; a test proves injected instructions in retrieved text cannot
  produce a plan step; time, byte, request-count, and redirect budgets are enforced and
  tested, all using the stub transport; a permission assessment is recorded for every
  new action with contract registry and allowlist entries and the inventory updated in
  the same pull request; the scope statements in `README.md` and
  `docs/architecture.md` that exclude internet search are corrected in this same pull
  request, since they become false here; V1-27 owns the public-internet-research
  wording, while V1-13 owns the local-adapter and networking wording, which this stage
  does not restate; with research disabled and no network
  available, every deterministic command and the rest of the product are unaffected.
- Changelog: one entry — bounded, read-only public web and documentation research with
  mandatory source attribution, source comparison, and uncertainty reporting; private
  stored context is never placed into a research query.

### Stage V1-28 — Data export, backup, and restore

- Branch: `feature/export-backup-restore`
- Prerequisites: V1-09, V1-18, V1-20, V1-24
- Purpose: Give the owner a complete, inspectable copy of their data and a safe way to
  put it back, keyed on the schema version rather than the frozen application version,
  placed after the last table-adding stage so it must cover every record type v1.0
  ships.
- Owner-visible result: Explicit commands to export data in a readable form, take a
  backup, and restore one, with a clear, explicit refusal rather than a silent
  half-restore when the schema does not match.
- Architecture affected: the memory layer for export, snapshot, restore, and the
  schema-version gate; a data tool with registration and routing; `core/planner.py`;
  `core/permissions.py`; `core/config.py` and `core/constants.py` for the export
  destination; the data retention and privacy section of `README.md`; and
  `docs/database.md`.
- Privacy and security: An export is the owner's whole personal dataset in plain text.
  It may only be written into a declared export destination inside Maria's own data area
  or an explicitly confirmed owner-chosen path, never into an organize or read-only root
  that a model-originated plan can reach, and never over a network. Export artifacts are
  personal-data backups and therefore fall inside the permanent exclusion set. Artifacts
  stamp the schema version, never the application version, which is frozen and would be
  a meaningless compatibility key. Restore is destructive, confirmation-required,
  single-file, takes an automatic pre-restore backup, and decides on schema version
  alone, failing closed with a typed error for an unknown, corrupt, or newer schema
  while leaving the existing database untouched. Restore can resurrect rows a previous
  confirmed deletion removed; this is stated in the confirmation text and in the
  retention documentation, which currently implies deletion is final. No export content
  reaches the log, and no encryption at rest or secure-erasure guarantee is promised.
- Non-goals: No scheduled or automatic backup. No cloud, remote, or network destination.
  No selective, partial, or merging restore in v1.0, and no import of foreign or
  hand-edited data. No undo of a restore beyond the automatic pre-restore backup.
- Completion criteria: The owner has decided in writing, before the branch is opened,
  the default export and backup artifact destination policy. Export covers every table
  present at the current schema version,
  and a test fails if a table exists that export does not cover; a round-trip test on a
  temporary database proves export then restore reproduces every record type exactly,
  including all provenance columns; a backup taken while the application is running is
  internally consistent and is produced through the memory layer, never by copying the
  live file behind it; restoring a frozen v0.2.0 baseline backup migrates forward end to
  end, while restoring a newer or corrupt file raises a typed error and leaves the
  existing database unchanged; a test proves the default artifact destination resolves
  outside every configured safe root; retention and privacy documentation is updated for
  every new record type and for the restore-resurrection caveat; tests never read, copy,
  or write the live database.
- Changelog: one entry — schema-versioned data export, backup, and restore with
  mandatory confirmation on restore and a fail-closed refusal of an unknown or newer
  schema, and documentation now states that restoring a backup can reinstate previously
  deleted records.

### Stage V1-29 — Cross-feature privacy and security audit

- Branch: none. This is a read-only audit; each finding gets its own focused
  `fix/<topic>` branch and pull request, and no functional fix is ever placed on a
  release branch.
- Prerequisites: V1-23, V1-25, V1-27, V1-28
- Purpose: Audit the whole v1.0 surface as one system, concentrating on the seams no
  single feature pull request owned. The permanent unreleased-section policy removes the
  release boundary that would otherwise force this review, and the permission layer is
  default-allow, so an action nobody classified is permitted by omission.
- Owner-visible result: A written finding list. No behavior change unless a correction
  is merged.
- Architecture affected: all of `core/`, `tools/`, the GUI package, the
  reminder-delivery subsystem, `config/`, `tests/`, and `docs/`, reviewed read-only.
- Privacy and security: A complete action classification matrix gives every registered
  tool action exactly one permission classification, `DENIED`, `REQUIRES_CONFIRMATION`,
  or `ALLOWED`, and separately exactly one model-selection classification,
  `MODEL_ELIGIBLE` or `NOT_MODEL_ELIGIBLE`. The two axes are orthogonal: model
  allowlisting is not a fourth permission state, and a model-eligible action still
  receives whatever permission decision its classification requires. A written risk note
  accompanies every action classified `ALLOWED`, and every `MODEL_ELIGIBLE` action is
  individually justified. A three-way drift check covers the compiler allowlist, the
  contract registry, and each tool's registered actions. A repository-wide
  log sentinel sweep covers the adapter, the compiler, the research transport, the
  reminder-delivery subsystem, the filesystem tool, export and backup, and the audit
  summaries. The audit verifies that the reminder subsystem still has no import or call
  path to reasoning, model-to-plan compilation, ToolManager, general tools, project
  assistance, or internet research and still reads no record beyond the reminder fields
  it needs; that Project Maria remains read-only and never activates itself, and that
  instruction-shaped text inside any project file still cannot produce a plan step; that
  Tier C remains unreachable by every automatic retrieval path added during v1.0; that
  the permanent exclusion set still covers the live database, logs, credentials,
  untracked local configuration, and personal-data backups against every root-consuming
  feature; that restore still fails closed; and that audit summaries still redact
  sensitive target identity.
- Non-goals: No feature work, refactor, or documentation rewrite. No version bump,
  release branch, or tag. No functional fix committed on this stage.
- Completion criteria: The inventory, the drift check, and the sentinel sweep are
  complete and written down; each finding is filed with a severity and either a focused
  fix branch or an explicit accepted-risk note; no finding is silently deferred.
- Changelog: none — audit only. Corrections carry their own classification on their own
  branches.

### Stage V1-30 — Documentation synchronization

- Branch: `docs/sync-v1.0-documentation`
- Prerequisites: V1-29
- Purpose: Bring every document into line with shipped behavior before the completion
  audit, so the audit verifies rather than discovers.
- Owner-visible result: `README.md`, `docs/architecture.md`, `docs/api.md`,
  `docs/database.md`, `docs/planner.md`, `docs/tools.md`, `docs/coding_style.md`, and
  this roadmap describe exactly what v1.0 does.
- Architecture affected: documentation only.
- Privacy and security: The retention and privacy sections must cover every new record
  type, the restore-resurrection caveat, the tiered context policy, what audit summaries
  do and do not contain, what the logs do and do not contain, the reminder subsystem's
  least-privilege envelope and its explicit non-promise of delivery while the machine is
  powered off, and the read-only self-review boundary. No secrets, credentials, personal
  identifiers, or absolute machine paths.
- Non-goals: No code, test, schema, or version change. No capability described that has
  not shipped.
- Completion criteria: Every shipped v1.0 capability is documented and every documented
  behavior is shipped; the scope sections of `README.md` and `docs/architecture.md` are
  accurate; this roadmap's status section reflects reality; no runtime file changes; the
  suite passes unchanged.
- Changelog: none — documentation only.

### Stage V1-31 — v1.0 completion audit

- Branch: none. This is read-only; any correction gets its own focused branch merged
  before `release/v1.0.0` is created.
- Prerequisites: V1-30
- Purpose: An explicit go or no-go decision against the v1.0 completion criteria.
- Owner-visible result: A written go or no-go recommendation.
- Architecture affected: the entire repository, verified read-only.
- Privacy and security: Re-verify that the V1-29 findings were actually fixed rather
  than quietly accepted, and re-verify the action inventory after the documentation
  pass, since documentation work can reveal an undocumented action. Re-verify, with
  reasoning, research, and the reminder subsystem each disabled and each enabled, that
  no log contains raw prompts, responses, endpoints, model paths, credentials, or
  private record content. Re-verify that the deterministic-only configuration exposes no
  network transport at all, that the GUI and CLI reach one execution and permission
  path, that Project Maria remains read-only and never self-activates, and that the
  reminder subsystem's envelope holds. Confirm no test reads, writes, or copies the live
  database or log, and that no machine-specific provider value is present in tracked
  configuration.
- Non-goals: No feature work, refactor, or documentation rewrite. No version bump,
  release branch, or tag. No functional fix committed on this stage. No real-model call
  added to the automated suite.
- Completion criteria: Each required v1.0 capability is checked off against a named test
  or documented behavior, and anything unmet is reported as no-go rather than quietly
  deferred; a no-model, no-network, no-GUI run passes for every pre-v1.0 deterministic
  command and every non-network deterministic command added during v1.0, while each
  inherently network-requiring deterministic command returns its controlled unavailable
  result rather than being expected to succeed; the model-unavailable path is
  demonstrated with a provider configured but down; an equivalent real-runtime
  compatibility checkpoint to the V1-13 one is repeated manually against the selected
  local runtime, covering one successful bounded request, one provider-unavailable
  result, one hard-deadline result, one oversized-output refusal, and proof that no
  prompt, response, host, model path, endpoint, or raw exception reached a log, and it
  is recorded in the audit rather than in the suite; a closed-app reminder delivery is
  demonstrated end to end on the owner's platform and the subsystem is shown to be
  cleanly disableable and removable; upgrade validation runs end to end from the frozen
  v0.2.0 baseline, including a backup taken at baseline and restored into the current
  build; `APP_VERSION`, the shipped configuration version, and both hardcoded test
  version literals still read `0.2.0` and the version-match test passes; the published
  tag list is unchanged and no intermediate public release branch or GitHub Release
  exists; the release checklist enumerates every version-touch site; the `## Unreleased`
  section is complete and written in release-ready prose in the repository's existing
  style, and the full suite output is recorded verbatim.
- Changelog: none — audit only.

### Stage V1-32 — v1.0.0 release preparation

- Branch: `release/v1.0.0`
- Prerequisites: V1-31
- Purpose: The single, deliberately final stage that assigns `1.0.0`, converts the
  accumulated unreleased section into a dated release, and produces the independently
  verified merge commit the annotated tag is created from.
- Owner-visible result: Maria reports `v1.0.0`, the changelog carries a dated `1.0.0`
  section, and the annotated tag matches the verified `main` merge commit.
- Architecture affected: `core/constants.py` `APP_VERSION`; `config/config.toml`
  version; the hardcoded version literal in the configuration tests; the banner literal
  in the CLI tests; `CHANGELOG.md`; this roadmap's status section.
- Privacy and security: No functional change on the release branch; corrections go to
  focused fix branches merged before this one, matching the v0.2.0 precedent. The
  shipped-configuration version test must never be weakened or deleted, because it is
  the only automated guard against the constants module and the shipped configuration
  drifting apart. Published tags are immutable: no retag, amend, force-push, or history
  rewrite. No secret, credential, personal identifier, or absolute machine path appears
  in the diff, tag message, or release notes. Nothing is added, committed, pushed, or
  tagged without explicit owner permission, and the tag is created only after the merge
  commit is independently verified.
- Non-goals: No feature work, refactor, bug fix, schema change, or dependency. No
  documentation rewrite beyond version references. No change to the database schema
  version to match the application version; the two identities stay independent. No
  version identifier other than `1.0.0`, no pre-release or development suffix, and no
  second release branch.
- Completion criteria: The final `v1.0.0` GitHub Release title is chosen by the owner in
  writing before the branch is opened; all version sites move to `1.0.0` in one commit,
  with the version-match test passing unweakened; the `## Unreleased` heading becomes a
  dated `1.0.0` section and a fresh empty `## Unreleased` heading is opened above it in
  the repository's existing changelog style; the database schema version is unchanged by
  this stage; the full suite is executed from the application directory and passes, with
  output recorded; the release pull request is merged and the resulting `main` merge
  commit is independently verified before the annotated tag is created, and the tag
  resolves to that commit; no prior published tag is touched.
- Changelog: converts the accumulated `## Unreleased` section into the dated `1.0.0`
  entry and opens a fresh empty `## Unreleased` heading above it.

## Permanent Safety and Privacy Boundaries

These hold across every version and are not superseded by any release section.

- Brain stays generic: no direct database access, no tool-specific logic, and no
  per-tool dispatch branch.
- The memory boundary is the only production code that imports the SQLite driver or
  executes SQL, enforced by guard tests that enumerate production modules.
- The deterministic planner owns command parsing and the help text.
- Every owner-initiated executable tool action follows planner, then permission manager,
  then tool manager, then tool. Direct tool invocation bypasses planning and permission
  checks and remains an internal trusted-code boundary.
- Conversational responses do not execute tools. A model-backed reply that selects no
  action never enters that pipeline, and model output alone can never start it.
- The reminder-delivery subsystem is the sole out-of-session exception to that pipeline.
  It is limited to its five fixed reminder operations through a narrow storage API, and
  it never uses ToolManager or model planning.
- Tools stay thin adapters over the public memory API.
- The mandatory denied set and the mandatory-confirmation set are a floor. Configuration
  may extend them and can never shrink them, and malformed permission arrays abort
  configuration loading. Precedence is denied, then requires-confirmation, then allowed.
- `PermissionManager.check()` returns `ALLOWED` for any tool action present in neither
  set. A new action is therefore permitted by default, and every new action owes an
  explicit permission-risk assessment recorded in the same pull request.
- Configuration validation fails closed. The permission decision function does not.
- Maria's live database, logs, credentials, private local configuration,
  runtime-generated state, and personal-data backups are permanently excluded from every
  general filesystem and project capability, under any configuration.
- Maria's own source repository is not permanently excluded. When the owner explicitly
  activates Project Maria, its source and tracked documentation are readable through the
  project-assistance read path in read-only mode. Self-review is allowed;
  self-modification is not.
- Maria's own source, comments, tests, and documentation are untrusted project input.
  Text found there cannot create a plan, grant authority, or bypass model-to-plan
  compilation and the permission boundary.
- No production path executes shell commands, subprocesses, dynamically generated code,
  `eval`, `exec`, or model-produced source code. Registered-tool dispatch through
  ToolManager is the intended execution mechanism and is unaffected.
- The reminder-delivery subsystem is the only component that may run outside an
  owner-initiated session, and it is bounded to the five operations listed in the
  background-work policy. It is never a general autonomous agent, and it can always be
  disabled.
- Destructive and lifecycle actions target at most one row, selected by a positive
  decimal integer id with a minimum value of 1, and no cascade between record types
  exists without a deliberate documented decision. The one exception, approved by the
  owner under V1-18-R1, is `audit.delete`: it removes one logical audit summary, its
  summary row and its optional outcome row together, in one transaction. Every other
  destructive and lifecycle action still targets at most one row.
- Bulk, wildcard, clear-all, or date-range mutation, automatic retention or pruning,
  undo or revision history, a secure-erasure guarantee, and encryption at rest are not
  permanently forbidden. None of them may be added without a separate owner-approved
  design, permission model, migration plan, and tests.
- Logs never contain raw prompts, raw responses, selected private context, credentials,
  endpoint URLs, local model paths, headers, or raw provider exception text.
- Loopback and private-LAN calls count as network transport.
- Maria never silently expands her permissions, scope, access, or authority.
- Published tags are immutable. Never retag, amend, force-push, or rewrite published
  history.
- The repository is public: no secrets, credentials, personal email addresses, real
  names, or absolute machine paths.

## Explicit Post-v1.0 Scope

The following are out of scope for v1.0. Each requires separate explicit owner approval:

- general unattended project development;
- any background or out-of-session capability beyond the narrow reminder-delivery
  envelope defined in the background-work policy;
- long-running delegated project workflows;
- autonomous background coding;
- general workflow adaptation while the owner is absent;
- behavioral pattern learning;
- inferred preferences without explicit owner review;
- automatic personal model fine-tuning;
- Maria-generated executable tools;
- sandboxed self-development loops as a product feature;
- modification of Maria's own source by Maria;
- digital-twin-level adaptation;
- voice-first interaction;
- broad browser automation;
- cloud-model support;
- self-modification;
- automatic dependency installation;
- autonomous permission expansion;
- autonomous Git, merge, release, or deployment actions.

## v1.0 Completion Criteria

v1.0 is complete when:

- every required v1.0 capability is implemented and checked off against a named test or
  documented behavior;
- every pre-v1.0 deterministic command, and every non-network deterministic command
  added during v1.0, remains available and usable without a model, network transport, or
  GUI toolkit, while a deterministic command that inherently requires network access,
  such as public research, returns a controlled unavailable result instead and is not
  required to succeed without a network;
- behavior not deliberately changed by an approved stage is byte-identical, and every
  deliberate change is documented, covered by regression tests, and reflected in the
  changelog when user-visible;
- no model-originated tool, action, or argument reaches execution without passing the
  positive allowlist, its argument contract, and the permission manager, in that order;
- approval binds to an immutable fingerprinted plan, and a test proves zero provider
  invocations occur between approval and execution;
- every registered tool action carries exactly one permission classification,
  `DENIED`, `REQUIRES_CONFIRMATION`, or `ALLOWED`, and separately exactly one
  model-selection classification, `MODEL_ELIGIBLE` or `NOT_MODEL_ELIGIBLE`, with the two
  axes treated as orthogonal rather than as one four-valued state; a written risk note
  accompanies every `ALLOWED` action, and drift tests fail in both directions;
- Tier C sources are unreachable by every automatic retrieval path, asserted by a test
  that enumerates them;
- every automatically included context item carries Maria-produced provenance;
- ordinary conversation writes nothing to the database, proven by row counts against a
  temporary database;
- log sentinel tests prove no prompt, response, private record content, credential,
  endpoint URL, model path, header, or raw provider exception text reaches a log;
- schema migration from the frozen v0.2.0 baseline fixture runs ordered, idempotent, and
  atomic, and refuses a newer schema with a typed error;
- export covers every table at the current schema version, with a test that fails if any
  table is uncovered, and export, backup, and restore round-trip every record type;
- the GUI and the CLI reach one execution and permission path, asserted by one shared
  test exercised through both;
- the reminder-delivery subsystem delivers a due reminder with the GUI and CLI closed,
  demonstrated on the owner's platform; surfaces a missed reminder as overdue when it
  next becomes available; can be enabled, disabled, and removed cleanly; and is proven
  by guard and behavioral tests to have no import or call path to reasoning,
  model-to-plan compilation, ToolManager, general tools, project assistance, or internet
  research, and to read no record beyond the reminder fields it needs to deliver;
- Maria's live database, logs, credentials, private local configuration, runtime state,
  and personal-data backups are unreachable through every filesystem and project
  capability, including while Project Maria is active;
- Project Maria never activates itself, is read-only when active, and instruction-shaped
  text inside any project file cannot produce a plan step or alter a permission
  decision;
- only the two permitted modules import a network module, and no production path
  executes shell commands, subprocesses, dynamically generated code, `eval`, `exec`, or
  model-produced source code, while registered-tool dispatch through ToolManager
  remains the intended mechanism and is unaffected;
- the research transport reaches only approved public HTTP or HTTPS destinations and
  refuses loopback, private, link-local, multicast, reserved, unspecified, and
  cloud-metadata-service destinations on both IPv4 and IPv6, proven by tests covering a
  direct forbidden IPv4 address, a direct forbidden IPv6 address, a hostname resolving
  to a forbidden address, a redirect into a forbidden range, a resolution change between
  validation and connection, and the configured local inference host being refused by
  research while remaining usable by the reasoning adapter;
- the selected real local runtime has passed its manual compatibility checkpoint, and
  the automated suite still uses only the adversarial fake and local stub transports;
- tracked configuration contains only a disabled, non-personal reasoning template that
  parses successfully with no active provider field present, an enabled configuration
  missing any required active field fails closed, and the local override is protected by
  an ignore rule;
- `requirements.txt` is unchanged and the project is still standard-library only;
- tests use only temporary database and log resources, and the live database and log are
  never read, written, or copied;
- all documentation is synchronized with shipped behavior, and upgrade validation from a
  real v0.2.0 database is demonstrated;
- the full test suite passes from the application directory, with output recorded.

## Release Policy

- `v0.2.0` remains the latest public release until `v1.0.0`.
- No intermediate public `0.x` release branch, tag, or GitHub Release is created.
- Focused branches and pull requests continue normally.
- `CHANGELOG.md` retains one permanent `## Unreleased` section. Every qualifying merged
  stage adds one stable, release-ready bullet or cohesive entry following the
  repository's existing changelog style.
- User-visible behavior, security and privacy guarantees, data compatibility, and
  upgrade behavior qualify. Documentation-only, audit-only, test-only, and truly
  internal-only stages do not.
- Major milestones receive tests, audits, and merge verification, but no public release.
- `APP_VERSION` and the shipped `config/config.toml` version stay `0.2.0` through Stage
  V1-31. Only Stage V1-32 moves the runtime, configuration, and test version literals to
  `1.0.0`. No feature, documentation, milestone, or audit branch changes either value.
- The database schema version is a separate identity from `APP_VERSION` and advances
  independently.
- The final `v1.0.0` GitHub Release title is an owner decision required before Stage
  V1-32. "Personal AI Companion" is a working roadmap theme and product description, not
  a selected release title.
- `release/v1.0.0` is created only after the v1.0 completion audit passes.
- `v1.0.0` is tagged only after the release-preparation pull request is merged and the
  resulting `main` merge commit is independently verified.
- Version-touch checklist for the release stage; every site must move together:
  `APP_VERSION` in `core/constants.py`, the version in `config/config.toml`, the
  hardcoded version literal in the configuration tests, the banner literal in the CLI
  tests, and the `CHANGELOG.md` heading. The shipped-configuration version test must
  never be weakened.
- Published tags `v0.1.0`, `v0.1.1`, `v0.1.2`, and `v0.2.0` are immutable.

## Appendix — Completed v0.2.0 Roadmap

Everything below is the completed v0.2.0 roadmap, retained as history with its body text
unchanged.

### v0.2.0 — Personal Data Control (Released)

This section and every following `v0.2.0` section are v0.2.0-scoped history. Their rules
describe v0.2.0 only. A rule that outlives v0.2.0 is restated under "Permanent Safety
and Privacy Boundaries".

#### Release promise

Maria v0.2.0 gives the owner transparent, positive-numeric-ID-based control over three
separate record types:

- **Interaction history** gains list, search, read-by-id, and single-row deletion.
  Interaction history is never edited, because it is a historical record.
- **Notes** gain separate title editing, body editing, and single-row deletion.
- **Explicit memories** gain fact editing and single-row forgetting.
- Read operations require no confirmation.
- Every persistent update or deletion requires mandatory explicit confirmation that
  configuration can extend but never remove.

All new record IDs referenced by v0.2.0 commands are **positive decimal integers with a
minimum value of 1**.

### v0.2.0 Development Order (Completed)

#### v0.2.0 Step 0 — Roadmap definition

- Branch: `docs/define-v0.2.0-data-control`
- Documentation only. No runtime behavior.

#### v0.2.0 Stage 1 — Interaction-history browsing

- Branch: `feature/interaction-history-browsing`
- Commands:
  - `history`
  - `search history <query>`
  - `read interaction <id>`
- `HistoryTool` actions during this stage are **exactly** `list`, `search`, and `read`.
  `delete` is **not** an available Stage 1 action.
- IDs are positive decimal integers with a minimum value of 1.
- `history` and `search history` show newest-first results with a default visible limit
  of 20 and a limit-plus-one more-results indication, matching the existing
  notes/memories convention.
- `read interaction` displays the full stored request, response, and timestamp.
- Stage 1 introduces a generic optional `recorded_message` contract on
  `ToolExecutionResult` so displayed response text may differ from the response
  persisted in interaction history.
- Brain must consume that field generically and must never branch on the history tool
  name or action.
- `read interaction` persists only a fixed, content-free audit summary rather than
  recursively storing the historical content it displayed.
- Stage 1 has no schema change and no mandatory-confirmation change.

#### v0.2.0 Stage 2 — Single-interaction deletion

- Branch: `feature/interaction-history-deletion`
- Command: `delete interaction <id>`
- Action: `history.delete`
- Stage 2 adds `delete` to `HistoryTool`; it does not exist during Stage 1.
- Positive numeric ID only. Exactly one row per command.
- Mandatory confirmation. No preview of stored content during confirmation.
- No bulk, wildcard, date-range, cascading, or inferred deletion.
- The selected interaction row is removed, but Maria records a new short audit
  interaction for the deletion command itself. The total row count may therefore remain
  unchanged even though the selected historical content is gone.

#### v0.2.0 Stage 3 — Note lifecycle management

- Branch: `feature/note-lifecycle-management`
- Commands:
  - `retitle note <id>: <new title>`
  - `edit note <id>: <new body>`
  - `delete note <id>`
- Actions: `notes.retitle`, `notes.edit`, `notes.delete`
- Positive numeric IDs only.
- Retitle and edit are deliberately separate commands to avoid accidental overwrites.
- `rename note` is not used, because it collides with the existing filesystem `rename`
  command prefix.
- A successful title or body edit preserves `created_at` and refreshes `updated_at`.
- Duplicate note titles remain allowed.
- Every action requires mandatory confirmation.
- No schema change.

#### v0.2.0 Stage 4 — Explicit-memory lifecycle management

- Branch: `feature/memory-lifecycle-management`
- Commands:
  - `update memory <id>: <new fact>`
  - `forget memory <id>`
- Actions: `memory.update`, `memory.forget`
- Positive numeric IDs only.
- A successful update preserves `created_at`. `memories.updated_at` is **not** added in
  v0.2.0, so editing a memory does not move it to the top of newest-first results.
- Casefold-based fact uniqueness is preserved; conflicts are handled through controlled
  results, not exceptions surfaced to the user.
- Both actions require mandatory confirmation.
- No schema change.

#### v0.2.0 Stage 5 — Read-only cross-feature and release-planning audit

- Runs only after all four feature PRs (Stages 1–4) are merged into a clean `main`.
- This stage has no branch or PR of its own unless the audit discovers a correction.
- Any discovered correction receives a separate, focused fix branch.
- Functional fixes are not placed into `release/v0.2.0`.

#### v0.2.0 Stage 6 — Release preparation

- Branch: `release/v0.2.0`
- Version bump, changelog finalization, tests, compilation, PR, verified merge commit,
  annotated tag, and GitHub Release.
- The `v0.2.0` tag is created only after the release-preparation PR is merged and the
  resulting `main` merge commit is independently verified.

### v0.2.0 Permission Baseline

The eight-action mandatory-confirmation set below was the baseline **as of v0.2.0**. It
is a floor, not a cap: later versions add entries, and configuration may extend it but
can never shrink it. See "Permanent Safety and Privacy Boundaries" for the complete
permission picture, including the mandatory denied set and the fact that
`PermissionManager.check()` returns `ALLOWED` for anything in neither set.

Existing mandatory-confirmation actions (unchanged): `filesystem.rename`,
`filesystem.move`.

Six new mandatory-confirmation actions are planned for v0.2.0: `history.delete`,
`notes.retitle`, `notes.edit`, `notes.delete`, `memory.update`, `memory.forget`.

The final mandatory-confirmation baseline is therefore **eight actions**, not nine.
Configuration may add further restrictions on top of this baseline but may never remove
any part of it.

Read-only `history.list`, `history.search`, and `history.read` remain allowed and do not
need entries in the mandatory confirmation set.

### v0.2.0 History Search Policy

Planned v0.2.0 history search is:

- literal substring search across stored `user_input` and `response`;
- newest-first;
- implemented using SQLite-side `LOWER()` comparison, to avoid a schema change;
- **not** advertised as full Unicode casefold equivalence (memories' search, which uses
  a precomputed casefolded column, remains the more precise comparison);
- not fuzzy, semantic, ranked, or AI-based.

No character truncation is added to history list/search previews in v0.2.0. That can be
reconsidered from real usage later.

### v0.2.0 Retention and Deletion Boundaries

- Interactions, notes, and explicit memories are separate record types.
- Deleting one record never cascades into another table.
- Deleting a note or forgetting a memory does not erase copies of that content that may
  already exist in interaction history.
- Users may separately delete the relevant interaction rows by numeric ID.
- v0.2.0 does not promise secure erasure from filesystem backups, Git history, database
  backups, or external copies.
- No bulk deletion or automatic retention exists in v0.2.0.

### v0.2.0 Architecture Requirements

These were the architecture requirements **for v0.2.0**. Several no longer hold for
v1.0 — in particular "No schema migration is required" and the bullet excluding
networking, background services, production threads, UI, AI models and agentic
behavior. "Standard library only" still holds: standard-library-only remains the v1.0
default and a third-party dependency may be added only where the owner explicitly
approves it. The layering invariants in this list are permanent and are restated under
"Permanent Safety and Privacy Boundaries"; see "v1.0 Architecture Requirements" for the
rules that now apply.

- Brain remains generic.
- Memory remains the only production module that owns SQL/`sqlite3`.
- Planner owns deterministic command parsing.
- Every user-facing action follows
  `Planner -> PermissionManager -> ToolManager -> tool`.
- Tools remain thin adapters over public `Memory` methods.
- All six new persistent mutations are mandatory-confirmation actions.
- No schema migration is required.
- Standard library only.
- No WAL, background service, production thread, networking, asynchronous behavior, UI,
  AI model, agentic behavior, or third-party dependency.

### Deferred from v0.2.0

The following are gated future candidates, not commitments, and are not part of v0.2.0:

- bulk clear/delete
- automatic retention or pruning
- date-range deletion
- export/import
- backup and restore
- encryption at rest
- pagination tokens
- filesystem delete
- UI and voice
- AI-model integration
- internet and external services
- tasks and project management
- autonomous behavior
- self-development
- Claude Code skills
- third-party dependencies

Several of these gated candidates were subsequently approved for v1.0: the export half
of "export/import", backup and restore as a separate approval, AI-model integration,
internet and external services, tasks and project management, and the UI half of "UI
and voice". General import of foreign or hand-edited data remains gated, and so does
voice. Each approved item ships in the narrower form defined under "Required v1.0
Capabilities" and "v1.0 Operating Policies", not as the general capability named here.
Every remaining entry stays gated.

### v0.2.0 Completion Criteria (Met)

v0.2.0 is complete when:

- all planned read and lifecycle commands are implemented;
- ID validation accepts positive integers only, with a minimum value of 1;
- mandatory confirmation covers exactly the six new mutations;
- no bulk or cascade deletion path exists;
- the generic `recorded_message` design ships with no history-specific Brain branch;
- no SQL exists outside `Memory`;
- no schema migration was required;
- existing v0.1.2 behavior is preserved;
- tests use only temporary database and log resources;
- retention and privacy documentation is accurate for the shipped behavior;
- the full test suite and compilation pass;
- release preparation and the annotated tag are handled separately, with the merge
  commit independently verified before tagging.
