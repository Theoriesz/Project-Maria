# Architecture

Maria is organized around a small set of modules with clear ownership.

## Core Flow

1. `main.py` receives user input from the command line.
2. `session.py` holds the owner's confirmation state and forwards the request.
3. `brain.py` coordinates the request and retrieves recent interaction context.
4. `planner.py` converts explicit commands into structured plan steps.
5. `action_contracts.py` declares which tool actions exist and which arguments
   each one accepts. Brain validates every step of a plan against it before any
   permission is evaluated.
6. `permissions.py` decides whether each planned action is allowed. Brain
   resolves every step of a plan before any step is dispatched.
7. `tool_manager.py` dispatches allowed actions to registered tools.
8. Tools perform their focused work and return a `ToolExecutionResult`.
9. `memory.py` owns persistent interactions, notes, and explicit memories in
   SQLite.
10. `brain.py` forms the final response and, on recordable completion paths,
    attempts to record the cleaned request and response as an interaction.
11. `logger.py` configures Maria's named logger; application modules emit
    operational events through it.
12. `filesystem_roots.py` holds Maria's named filesystem roots, the settled
    protections covering every location that is permanently out of reach, and the
    two-identity resolution both rely on. Startup prepares the declared roots
    once, before logging is configured and before the memory layer opens the
    database.
13. `reasoning.py` defines the typed, provider-independent contract a later
    local reasoning model will sit behind. Nothing in the flow above calls it
    yet; see "Reasoning Interface and Local Override".

An explicit memory request uses the same execution path as every other tool
request:

```text
CLI
-> MariaSession
-> MariaBrain
-> Planner
-> ActionContractRegistry
-> PermissionManager
-> ToolManager
-> MemoryTool
-> Memory
-> SQLite
```

The `MemoryTool` is a thin adapter for the `store`, `search`, `list`, `update`,
and `forget` actions. It is registered as `tools.memory` through Tool Manager's
standard `build_tool(...)` factory contract alongside the other built-in tools.
Memory actions therefore pass through the normal permission and result
handling flow.

## Interaction Recording Boundary

For a normal completed tool request, interaction recording follows this
boundary:

```text
CLI
-> MariaSession
-> Brain
-> Planner
-> ActionContractRegistry
-> PermissionManager
-> ToolManager
-> Tool
-> Brain final response
-> Memory.record_interaction
-> SQLite interactions table
```

`MariaBrain.process()` strips non-empty input before planning. Brain records
on exactly four branches: the zero-step branch, the contract-refusal branch,
the permission-denial branch, and the post-tool-result branch that follows
completed execution. On each of them, unless the plan is transient (see
below), Brain passes that cleaned request and the response chosen for
recording to `Memory.record_interaction()`. That response normally matches the
displayed response, but a completed tool result may supply a
`recorded_message`, including an explicit empty string, to record in place of
its displayed text. Recording covers help, unsupported commands,
malformed-command usage responses, contract refusals, denials, successful tool
results, and returned unsuccessful `ToolExecutionResult` values. The recording
layer stores the supplied text rather than summaries or only metadata, subject
to existing command and tool output limits. A recording `RuntimeError` is
logged without replacing the primary response, so recording is attempted but a
row is not guaranteed when the database write fails.

All four branches record through one private Brain helper,
`_record_interaction(plan, response)`, which is the only place Brain writes
interaction history. It takes the whole plan rather than the request text, so
the request half of every row is the plan's `original_request` and no branch
can record without the plan's recording policy being consulted. `Plan` carries
that policy as a `RecordingPolicy`: `RECORD`, the default, records exactly as
described above; `TRANSIENT` writes no interaction row at all on any of the
four branches, neither the cleaned request nor a placeholder response, while
the displayed response, contract validation, permission resolution, and tool
execution are unchanged. Suppression is a positive choice by the plan's
producer, so a plan that says nothing is recorded, and only an explicit
`TRANSIENT` keeps a row out of history. The policy governs only the interaction
row: a transient plan's tools still store whatever they store, such as a note
or an explicit memory. `Plan` accepts only an exact `RecordingPolicy` member,
refusing a plain string or any lookalike, and the policy is part of plan
equality, hashing, and the fingerprint, so an approval bound to one policy
cannot execute a plan carrying the other. Brain decides from the plan's
policy alone, never from a step's tool or action or a result's content. A
static source-scan guard test checks the gate's opening transient return and
its single interaction write, and rejects direct tool-, action-, or
request-specific recording decisions in the Brain methods that lead to the gate
and in the helpers that produce recorded text; it does not analyze aliasing or
data flow in general. No command Maria currently understands
produces a transient plan: every deterministic command keeps the default and
records exactly what it recorded before the policy existed.

`ToolExecutionResult` carries an optional `recorded_message` field. It
defaults to `None`, and any result that does not opt in leaves it as `None`;
`None` makes Brain record the same text it displays, exactly as before. A
tool may instead set `recorded_message` to persist different text than the
displayed `message` for that step; an explicit empty string is honored rather
than falling back to `message`, so it still records a row, with an empty
response half. Brain applies this generically, reading only `.message` and
`.recorded_message`, with no branch on a tool's name or action. A result can
therefore change what a recorded row's response half says, but never whether a
row is written; only the plan's recording policy decides that.

In the current implementation, `history.read` is the only
result that supplies a non-`None` `recorded_message`, so that redisplaying
old interaction content does not duplicate it into a new interaction row.
Every other current tool result, including `history.delete`,
`notes.retitle`, `notes.edit`, and `notes.delete`, leaves the
field as `None` and therefore retains the existing message-as-recorded
behavior; those displayed messages are already short,
content-free summaries, so no override is needed. `read file`, `read note`,
and `recall` are unmodified and still record their full displayed content.

`history.delete`'s row deletion and the subsequent audit-recording `INSERT`
are separate transactions, following the same boundary as every other
recordable path: `Memory.delete_interaction()` commits independently of the
later `Memory.record_interaction()` call. If the deletion commits but the
following recording call raises `RuntimeError`, that error is logged and the
already-computed successful deletion response is still returned unchanged;
the deletion is not rolled back and no compensating write is attempted, so an
audit row is not guaranteed in that case.

`notes.retitle`, `notes.edit`, and `notes.delete` follow that identical
boundary: `Memory.retitle_note()`, `Memory.edit_note()`, and
`Memory.delete_note()` each commit in their own transaction, independently of
the later `Memory.record_interaction()` call. A recording failure after a
committed note mutation is logged, does not roll the mutation back, does not
replace the successful response, and triggers no compensating write or retry.

Because Brain records the original cleaned request as the `user_input` half of
an interaction, a `retitle note` command retains its new title and an `edit
note` command retains its new body in interaction history even though the
recorded response is content-free. `recorded_message` can only change the
response half of a recorded interaction, never the request half, so later
editing or deleting the note does not rewrite or remove those independent
interaction rows.

Some paths return before this boundary. Blank CLI input is not processed, and
blank input passed directly to `process()` returns before logging, context
retrieval, planning, or recording. `exit`, `quit`, `q`, main-prompt EOF or
Ctrl+C, and startup failures do not become interaction rows. An initial
confirmation-required response returns before completed-interaction recording.
If confirmation is cancelled, the cancellation response is not recorded, and
neither is a refused confirmation, because neither executed anything. If it is
approved, the session calls `MariaBrain.execute_confirmed_plan()` with the
exact plan it was holding; Brain re-resolves that whole plan's permissions and
dispatches it only if nothing is denied, without retrieving context or planning
again, then attempts to record the completed result through the normal path. A
denial found by that recheck executes nothing and is recorded as any other
denial is. The request half of that row is the
approved plan's `original_request`, which is the same cleaned text the first
and only planning pass received.

Post-startup processing has no broad exception handler. If an exception
propagates before a recordable branch calls `Memory.record_interaction()`, no
interaction write is attempted.

For each non-empty `process()` call, Brain attempts to retrieve up to three
recent interaction rows and supplies them to Planner as context. A retrieval
`RuntimeError` is logged and produces empty context instead. The Planner
currently accepts and discards that context, so retained history does not alter
plans. The retrieval limit controls query results; it is not a retention or
deletion policy.

Operational logging and interaction persistence are separate boundaries.
`logger.py` configures Maria's namespaced file logger; default startup targets
`logs/maria.log` for application events, warnings, and failures. Configuring
logging again is honoured rather than ignored: Maria's own file handlers are
flushed, detached, and closed, and one new handler for the requested directory
replaces them, so a second call never duplicates a handler and never keeps
writing to the previous directory. The replacement happens only once the new
handler exists, so a failure to create the directory or open the file
propagates with the previous configuration intact. The managed handler
rotates rather than growing without limit: the active `maria.log` is capped
at 1 MiB and at most three previous files are kept, so the directory holds
`maria.log` through `maria.log.3` and no more. That policy is internal and
has no configuration field, CLI option, or environment variable.

Maria carries no global redaction, scrubbing, or log-filtering layer, and
deliberately so: an existing test asserts that the filesystem tool's
operational log does retain the absolute root path and the filenames its
owner-visible response omits, and a blanket filter would break that contract
while giving false confidence everywhere else. The discipline is
module-scoped instead: a module never hands a private value to the logger in
the first place. `failures.py` supports that discipline. It declares a closed
set of failure kinds and one immutable value object holding a kind and
nothing else, with no field for a request, a response, an arguments mapping,
an exception, its text, a path, an endpoint, a credential, or a header, and
with a fixed owner-visible sentence per kind. `tool_manager.py` uses it at
the one boundary that previously leaked: an unexpected exception from a tool
now yields that fixed message and a log record naming the validated tool,
the validated action, and the typed failure code, with the exception never
bound to a name. Tools keep their own specific diagnostics, where each one
knows what its own failure may safely say.

`Memory.record_interaction()` separately writes request and response text to
SQLite. A log message that omits full request or response content does not
prevent that content from appearing in interaction history.

## CLI Boundary

`run_cli()` constructs `MariaBrain.from_defaults()` inside the controlled
command-line entrypoint boundary, before printing the banner. Except for
`MemoryError`, an `Exception` raised during construction is converted to stable
stderr output and status 1, while startup `KeyboardInterrupt` maps to status
130. `MemoryError` and `SystemExit` are not converted into ordinary startup
failures.

A startup `TypedFailureError` is the one case that reports something more
specific than the generic message. It carries a `Failure` whose owner-visible
sentence is fixed per kind and interpolates nothing, so printing it is exactly
as sanitized as the generic message while saying which refusal happened. The
status is still 1. This exists because a database written by a newer build has
to be named rather than hidden behind "startup failed", which is the silent
half-working outcome that refusal replaces. The entrypoint chooses nothing
about the wording: it prints what the carried failure already decided.

EOF or Ctrl+C at the main prompt exits normally. At the confirmation prompt,
`y` or `yes` approves and `n` or `no` cancels, case- and whitespace-
insensitively; blank or unrecognized input re-prompts rather than approving or
cancelling. Either interruption (EOF or Ctrl+C) cancels the pending action
immediately without executing it, even if it occurs after one or more invalid
answers. This boundary intentionally does not add a broad exception handler
around Brain processing after startup.

`main.py` owns terminal concerns only: reading input, printing output, showing
the confirmation prompt, accepting its aliases, and mapping the owner's answer
to the session's confirm or cancel operation. It never calls Planner, builds or
copies a plan, compares fingerprints, evaluates permission, holds the pending
plan, or re-sends the original request after approval.

## Session Boundary

`core/session.py` owns the confirmation lifecycle for every interface. It needs
no standard input, standard output, or terminal, so a headless caller drives
the same contract the CLI does.

A session is idle or holds exactly one pending approval. Submitting a request
first discards any approval still pending, then plans exactly once. A
confirmation-required response becomes the pending approval: the immutable
`Plan` and the fingerprint captured at that moment, and nothing else.

Confirming compares the pending plan's current fingerprint with the captured
one, and refuses if they differ. The approval is consumed before execution is
attempted, so a failing tool or a raised exception cannot make it replayable.
Cancelling discards the pending approval and executes and plans nothing. A
second confirmation, a confirmation after cancellation, and a confirmation
after any intervening request all execute nothing and return one fixed,
content-free refusal, so an approval can never be transferred to a plan the
owner was not shown even when two requests read identically.

The pending approval is transient process state. It is never written to SQLite,
a file, or configuration, never serialized, and never logged; only a plan's
fingerprint could be logged, because the canonical content behind it can
contain note bodies, memory facts, and absolute paths. `PermissionManager` is
consumed unchanged: the session decides nothing about permission, and Brain
resolves every step of a plan before dispatching any of them. The session
stores no permission resolution alongside the pending plan, because
confirmation re-resolves the plan's current permissions rather than replaying
the decisions that were current when it was shown.

## Permission Policy

All planned actions pass through `PermissionManager`.
`filesystem.rename` and `filesystem.move` always require confirmation, while
`filesystem.delete` and `filesystem.write` are always denied. TOML
configuration may add restrictions but cannot remove these mandatory
protections. Missing permission arrays retain the secure baseline, and
malformed present arrays stop configuration loading instead of weakening it.
When an action is both denied and confirmation-required, denial wins.

In the current implementation, the mandatory confirmation baseline is
eight actions: `filesystem.rename`, `filesystem.move`,
`history.delete`, `notes.retitle`, `notes.edit`, `notes.delete`,
`memory.update`, and `memory.forget`.
`history.list`, `history.search`, `history.read`, `notes.create`,
`notes.list`, `notes.read`, `memory.store`, `memory.search`, and `memory.list`
remain allowed by default. These entries live
in `MANDATORY_CONFIRMATION_ACTIONS` in `core/permissions.py` rather than in
`config/config.toml`, so configuration can add restrictions on top of them but
can never remove them.

Direct tool invocation bypasses planning and permission checks and therefore
remains an internal trusted-code boundary. It does not bypass the action
contracts: `ToolManager.execute_tool()` validates them itself before reaching a
tool, so a direct caller cannot submit an unsupported action or a malformed
argument.

### Declared Action Contracts

`core/action_contracts.py` is the positive allowlist of every supported tool
action and of the arguments each one accepts. It is an explicit, hand-written,
immutable table: it is not generated by inspecting loaded tools, not derived
from a tool's `actions` set, not read from configuration, and never persisted.
That independence is the point. Tool Manager only knows whether an action is
registered, which is membership rather than argument validation, and
`PermissionManager` returns `ALLOWED` for any identifier absent from both
mandatory sets, so neither can serve as the positive gate. A test proves the
declared set and the loaded set match in both directions, and a duplicate
declaration raises instead of silently replacing the earlier one, which a plain
dictionary literal would do.

Each contract declares, for one exact tool action, which fields are required,
which are optional, the exact accepted type of each, every bound that applies
to that type, and, for a path field, the named root class it belongs to. An int
field declares a numeric minimum and maximum; a str field declares whether
blank text is accepted and its minimum and maximum length. A bound left
declared as absent means the established domain genuinely has none, and a str
field's blank policy is mandatory, so it can never be omitted by accident.
Metadata that cannot apply to the declared type — a length bound on an int, a
numeric bound on a str, a negative or reversed length pair, a root class on an
int — is rejected when the contract is constructed rather than ignored.

Every str field in the shipped registry rejects blank text, because every tool
that takes text already refuses it: `FilesystemTool` raises `Path is required.`,
`CalculatorTool` returns `No expression provided.`, and the notes, history, and
memory paths each return their own required-value message. Only
`calculator.calculate.expression` declares a maximum length, 200 characters,
measured on the stripped text exactly as `CalculatorTool` measures it before
handing the expression to `evaluate_expression`. No field declares a minimum
length beyond being non-blank.

Validation rejects; it never repairs. It performs no coercion, parsing,
truncation, clamping, default insertion, or normalization of casing or
whitespace, and type identity is exact, so `bool` never satisfies an `int`
field and no object is accepted merely because it could be converted to text.
It consumes the original `PlanStep` and its immutable `PlanArguments`, never
rebuilding the plan, replacing a step, or copying arguments into a mutable
mapping, so a plan's fingerprint is identical before and after.

Failures are typed: unknown tool, unknown action, missing required argument,
extra argument, wrong type, and out-of-range value. Each carries only the
failure kind, the tool name, the action when known, and the offending field
name, and each renders one fixed refusal naming the action and field but never
the submitted value, a repr, an object address, or a host path. When several
defects exist, the first reported one is deterministic: plan-step order, then
contract lookup, then missing required fields in declared order, then extra
fields in sorted order, then present fields in declared order with type checked
before bounds.

A path field's root class is classification metadata only. The registry
resolves no path, inspects no filesystem, creates nothing, and performs no
containment check. `FilesystemTool` remains solely responsible for canonical
resolution, safe-root containment, symlink handling, and collisions.

### Whole-Plan Validation and Resolution Before Execution

Executing a plan is three separate phases, and no phase begins while an
earlier one is incomplete.

Contract validation comes first and covers the complete plan. Brain checks
every step in plan order against the declared contracts and stops at the first
violation. A violation anywhere refuses the whole plan before
`PermissionManager` is consulted at all: zero permission checks, zero tool
calls, no pending confirmation, and one controlled refusal recorded through
Brain's ordinary generic interaction path. Because the allowlist is positive,
an action that is mandatorily denied but not registered as a tool action, such
as `filesystem.write`, is refused here as an unknown action and never reaches
the permission layer. `PermissionManager.check("filesystem", "write")` still
returns denied when called directly; the two layers are defence in depth rather
than a contradiction.

Resolution comes second and also covers the complete plan. Brain walks the
steps in their original order, calls `PermissionManager.check()` once per step,
and keeps each step paired with the result it got back. Nothing executes during
that pass, the plan is neither rebuilt nor mutated, no step's arguments are
copied into a mutable dictionary, and no planner or context lookup happens.

Denial takes precedence over the whole plan. If any step resolves to denied,
the plan is denied, no tool runs, no confirmation becomes pending, and the
earliest denied step in plan order supplies the reason. An earlier allowed step
does not run first, and an earlier confirmation-required step does not prompt.
Confirmation is offered only once resolution has found no denial anywhere.

Approving a plan does not freeze its contract or its decisions. Confirmed
execution validates the same immutable plan against the current contracts and
then resolves its permissions again before dispatching the first tool, which is
an execution-time safety recheck rather than replanning: no planner runs and no
context is retrieved. If that recheck now finds a contract violation or a
denial, nothing executes, the refusal is recorded through the normal path, and
the approval the session already consumed stays spent.

Dispatch is the third phase. Steps run in plan order, at most once each, and
each receives the immutable arguments object its `PlanStep` holds. The first
unsuccessful `ToolExecutionResult` stops the plan: later steps do not run, and
Maria neither rolls back nor compensates for the steps that already succeeded.
There is no transaction, undo, or workflow persistence across steps, and the
displayed and recorded text covers exactly the prefix of steps that ran.

The resolved plan is transient execution state. It is private to `brain.py`,
frozen, never persisted, never logged, and never handed to another module, and
it reports which decisions came back without making any of them.
`PermissionManager` itself is unchanged: no new arguments, no bulk-check
method, no altered precedence, no awareness of arguments or contracts, and no
permission policy in Brain, the session, `main.py`, Tool Manager, or a tool.
The tools also keep their own argument checks as defence in depth, so a
malformed value is refused rather than converted even on a path that did not
come through Brain.

Production plans still contain at most one step, because the planner emits at
most one. These guarantees therefore protect plans a later stage may produce;
they change no command available today. A single-step confirmation keeps its
existing wording, while a longer plan is rendered as a deterministic,
plan-ordered list of each step's tool action and resolved permission outcome.
That rendering repeats no argument value, so it exposes no absolute host path,
no fingerprint, and no canonical plan content; it is not logged, and a pending
confirmation is not written to interaction history.

## Filesystem Roots and Permanent Exclusions

`core/filesystem_roots.py` owns Maria's one canonical model of where a path may
live. `FilesystemRoot` holds a name, a canonical absolute path, a policy, and an
enabled flag; `FilesystemRoots` holds the declared roots as a tuple together with
the name of the default one. Both are sealed the way `core/failures.py` is sealed:
tuple-backed, with no instance dictionary, no writable slot for
`object.__setattr__` to reach through, no subclass route for a payload field, and
no construction path — including `_make` and `_replace` — that skips validation.
The canonical-path invariant is established inside `FilesystemRoot.__new__`, which
requires an absolute concrete path for the platform and stores `path.resolve()`,
so a non-canonical path never exists inside a root.

`core/config.py` owns production policy and the module owns none of it. A root's
name, path, policy, and flag are validated there, and the whole `[filesystem]`
section fails closed: a missing or non-table section, roots that are not an array
of tables, an unknown key at either level, a missing field, a wrongly typed value,
an unknown policy, a repeated name, a repeated or nested canonical path, a missing,
unknown, or disabled default, a root path that is an ordinary file, a name outside
the two Maria accepts, or a production root whose meaning is wrong are all
refusals. Nothing is repaired, coerced, disabled, or substituted. Loading remains
side-effect free.

Creation is a separate, single boundary. `prepare_roots` runs once in
`MariaBrain.from_defaults`, taking the same settled protections the filesystem
tool will use so the two can never disagree about what is protected. It runs
*before* `configure_logging` and before the memory layer opens the database,
because configuring logging creates the active log directory: a root declaration
that names a protected runtime location has to be refused before anything is
created, not after. Nothing is logged on that path either way — a refusal raises,
and `main.py` reports it as a sanitized startup failure. An enabled organizing
root is created when missing; an enabled read-only root is never created and must
already be a directory; a disabled root is neither created nor required; and a
root inside a permanently excluded location is refused whatever its policy. The
repository root itself is not a protected location, so the disabled
`project_maria` declaration stays valid.

Two predicates sit over that one model, and the separation is deliberate.

`FilesystemProtections.excludes` is the complete universal boundary, and it is
the one a filesystem capability applies. It has two halves, because a path can
be out of bounds for two unrelated reasons.

The fixed half, `is_universally_excluded`, follows from the source layout alone:
`Maria/database/`, `Maria/logs/`, the repository's `.git` as a directory or a
gitdir-pointer file, `Maria/config/config.local.toml`, and a set of
repository-scoped components — credential files, cache and environment
directories, local smoke-test artifacts, assistant working state, and
`.tmp`/`.bak` artifacts — inspected at every component of a path's
repository-relative form. The component rules apply only once a path is proven
to lie inside the repository, which is why a workspace may hold its own `env`,
`venv`, or `.claude` directory.

The active half protects where the *running* configuration keeps Maria's live
state: the database file named by `[paths] database` together with its SQLite
`-journal`, `-wal`, and `-shm` sidecars, the whole subtree of `[paths] logs`, and
the file named by `[paths] system_prompt`. Each protected identity covers itself
and everything beneath it, which is free under the ordinary file shape and
fail-closed when one of those names is occupied by a directory or a directory
reparse point. Each sidecar is settled to two identities in the constructor — the
name beside the database and, when that name already resolves elsewhere through a
link, its canonical target — so `protects` stays a pure path comparison while
still covering the location the content really occupies.

Settling happens at construction and only at construction. Copying, deep-copying,
and unpickling a protection value restore the snapshot that was already settled
rather than re-reading the filesystem, so a duplicate can never come back
describing somewhere else; `__copy__` and `__deepcopy__` return the value itself
and `__reduce__` hands the four settled fields to a private rebuild function that
performs no filesystem access. `_make` and `_replace` remain construction, so they
still recompute the derived field and still refuse a forged one. A layout-only policy could not
see any of them, because all three are configurable and a valid configuration may
place them inside an enabled root. `FilesystemProtections` is a sealed,
tuple-backed value carrying those three canonical paths and nothing else;
`core/config.py` builds it from the active configuration and stores it on
`MariaConfig`, and this module never imports `MariaConfig` or reads configuration
itself. `is_universally_excluded` is therefore one half of a policy rather than
the policy, and a capability that applied it alone would hand out whatever the
running configuration happened to place in a root.

`FilesystemProtections` answers two different questions over that same data.
`excludes` settles *direct* reachability, which is what reading and listing need.
`intersects_mutation` settles whether relocating a path would disturb an active
location, which is wider because renaming or moving a directory carries everything
beneath it: a path that merely *contains* the live database, a sidecar, the prompt,
or the log directory is refused as a mutation operand. Keeping the two questions
apart is exactly what lets an ordinary parent stay listable with its protected
children filtered out while remaining immovable. `tools/filesystem.py` asks the
wider question in `_operands`, after placement and direct reachability and before
the policy check, the same-root check, and every existence check, and it widens the
repository barrier the same way, so a synthetic root cannot relocate the checkout by
naming its parent.

Both halves are evaluated against two identities of a submitted path.
`FilesystemRoots.resolve_path` returns a sealed `ResolvedPath` carrying the
governing root, the absolute pre-resolution candidate spelling, and the canonical
target. Checking only the target would let a protected name such as `Maria/logs`
be reached by being a junction pointing out of the repository; checking only the
candidate would let an ordinary name be a link pointing in. Root containment stays
a question about the target alone, and neither identity is ever rendered into
owner-visible text, a log record, or result data.

The general filesystem repository barrier lives in `tools/filesystem.py` instead,
because it is a property of the four generic commands rather than of the path
model. It makes any path at or beneath the repository root unreachable by `read`,
`list`, `rename`, and `move`, and it is applied symmetrically to both identities,
which keeps tracked source, documentation, prompts, `config.toml`, and arbitrary
untracked repository files out of generic filesystem reach even under a root
declared around them.

A later Project Maria capability is expected to reuse `FilesystemRoot`,
`FilesystemRoots`, `ResolvedPath`, canonical containment, and
`FilesystemProtections.excludes` unchanged, and to add its own tracked-source
admission predicate on top. It may not bypass the universal exclusions and may not
introduce a second path model. Making the generic tool's root available is not how
that capability is switched on. A later stage that settles an export location must
add that location to the same universal exclusion boundary.

## Persistence Models

The database carries its own schema version, stamped in the SQLite header and
advanced only by `core/memory.py`. It is a separate number from `APP_VERSION`
and is never derived from it, so the application version can stay frozen while
the schema advances. Startup settles that version once, classifies an
unversioned database structurally against the shapes Maria actually shipped,
runs any missing forward migrations in order, and refuses a database stamped
newer than the running build before writing anything.
`docs/database.md` describes the rules; the three record types below are
unchanged by them.

Maria keeps three kinds of persistent data separate:

- **Interaction history** records cleaned requests and the responses chosen
  for recording on the four recording branches described above, for every
  plan whose recording policy is `RECORD`, whether by default or set
  explicitly; a `TRANSIENT` plan adds no row. The brain retrieves recent rows
  and supplies them to the planner as context, although the strict planner
  discards that context. Interaction rows are never converted automatically
  into explicit memories.
- **Notes** are structured title-and-body records deliberately created by the
  user. The notes tool uses the shared `Memory` persistence interface. A note's
  title and body are edited through two separate confirmation-gated commands,
  so neither can overwrite the other; both preserve `created_at` and refresh
  `updated_at`. A note may also be deleted by numeric ID. None of those
  mutations cascade into interaction history or explicit memories.
- **Explicit memories** are deliberately stored facts. They are created only
  through the strict `remember <fact>` command or the public `Memory` API,
  searched through `recall <query>`, and listed through `memories`. A memory's
  fact may be replaced, and a memory may be forgotten, through two separate
  confirmation-gated commands addressed by numeric ID. An update preserves
  `created_at` and adds no `updated_at`, so an edited memory keeps its place in
  newest-first results rather than moving to the top — deliberately unlike an
  edited note. Fact uniqueness stays casefold-based, so an update whose
  casefolded fact already belongs to a different memory is refused with a
  controlled, content-free conflict result instead of changing either row.
  Neither mutation cascades into interaction history or notes.

On the completed paths described above, memory commands and their responses
also appear in interaction history when recording succeeds. A stored fact can
therefore exist both as an explicit memory and as part of an interaction.
Because Brain records the cleaned request verbatim, an `update memory <id>:
<new fact>` command retains its new fact in `interactions.user_input` even
though the displayed and recorded response is only `Updated memory <id>.`;
`recorded_message` can shorten the response half, never the request half.
Forgetting or later updating that memory does not rewrite or remove those
independent interaction rows.

Each persistent mutation commits in its own transaction, and Maria then makes a
separate attempt to record the command and its short response. The two are
never combined into one transaction: if the later recording fails, the mutation
remains committed, the successful response is unchanged, and no audit row is
guaranteed. There is no compensating rollback, retry, or replay.

## Reasoning Interface and Local Override

`core/reasoning.py` is the contract every later model stage builds on, and it
deliberately contains no transport, no model, and no provider. A
`ReasoningRequest` holds one bounded prompt and nothing else — there is no
field through which personal context, a system prompt, or a template could
travel — and a `ReasoningResponse` holds one bounded, untrusted string. The
`ReasoningProvider` protocol is what the later local adapter implements.
`request_reasoning` is the one boundary through which a provider is asked
anything: it answers without asking when reasoning is disabled or the prompt
is over budget, asks at most once, and returns either a fresh response holding
inert text or a typed `Failure` from the reasoning kinds in `failures.py`. A
provider's own exception never travels, a reply that is not exactly a bounded
response is malformed, and an oversized reply is discarded whole rather than
truncated. The module's own imports include no network, threading,
asynchronous, or subprocess module and nothing from planning, permissions,
dispatch, persistence, or tools, so a reply has no route to becoming an
action. Prompts, replies, the host, and the model identifier are never shown by
`repr()` or `str()`, never interpolated into an exception message, and never
logged; the boundary logs only an outcome and a failure code.

`ReasoningSettings` describes the one owner-controlled provider: a destination
class (`loopback` or `private_lan`, which names where the host may be and
selects no protocol, port, or runtime), one canonical IP address inside that
class, an opaque model identifier, a hard deadline in whole seconds, and input
and output budgets in UTF-8 bytes. Enabled settings require every field, with
no defaults, and every value is checked by exact type, range, and enumeration.
There is no field for a port, endpoint URL, model path, credential, token, or
header.

The tracked `config/config.toml` carries a disabled template, `[reasoning]`
with `enabled = false` alone, and `core/config.py` refuses a tracked file that
enables reasoning or names any provider value. Machine-specific settings live
only in the owner's untracked `config/config.local.toml`, which
`Maria/.gitignore` ignores, Maria never creates, and the universal permanent
exclusion keeps out of every filesystem capability. `load_config` reads it only
when a caller names it; `MariaBrain.from_defaults()` is the one caller that
does. An absent override leaves the shipped disabled configuration. A present
one must be a regular file — a link, including a dangling one, a directory, or
a special entry is refused before anything is opened — holding exactly one
`[reasoning]` table, which replaces the tracked table completely with nothing
merged or borrowed. The override cannot reach any other section. Every refusal
is a fixed message raised with no cause or context, so neither the file's text
nor a parser's or the operating system's error text survives in it, and
configuration loading logs nothing.

## Boundaries

The brain does not run SQL or perform tool work directly. The planner does not
execute tools. Tools do not decide global permissions. The memory module is the
only module that opens SQLite connections or executes SQL. `core/filesystem_roots.py`
is the only canonical model of where a filesystem path may live, and the only
owner of the universal permanent exclusions; `core/config.py` decides which roots
production accepts, and `tools/filesystem.py` adds the barrier that keeps the
generic filesystem commands out of Maria's own repository. The action-contract
registry declares argument shapes only: it holds no tool logic, never calls a
tool to decide whether arguments are valid, and decides nothing about
permission. `core/reasoning.py` reaches nothing but the shared failures and the
logger; no module passes a reasoning reply to the planner, Brain, or a tool.

The brain has no memory-specific dispatch branch. `MemoryTool` contains no SQL;
it receives the shared `Memory` object and calls its public persistence methods.
The existing calculator, notes, filesystem, and system tool implementations are
unchanged by explicit memory support.

## Scope

This foundation intentionally excludes autonomous agents, voice, browser
automation, internet search, and user interfaces beyond the command line. Future
versions can add richer capabilities by extending the planner, tool manager, and
tool set without rewriting the foundation.

Explicit memory remains intentionally small and strict. Maria does not infer
facts from conversation, learn in the background, or provide embeddings,
semantic search, fuzzy matching, or ranking. A memory's fact can be replaced
and a memory can be forgotten by numeric ID only, through `update memory <id>:
<new fact>` and `forget memory <id>`; there is no selection by fact text, no
bulk or clear-all deletion, and no undo or revision history.
