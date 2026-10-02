# Architecture

Maria is organized around a small set of modules with clear ownership.

## Core Flow

1. `main.py` receives user input from the command line. It is also the
   runtime composition point, the one production module that names the local
   reasoning adapter.
2. `session.py` holds the owner's confirmation state and the session's bounded
   conversation, forwards the request, and renders a plan the local model
   proposed for the owner's approval.
3. `brain.py` coordinates the request. On the model-backed conversational path
   only, it reads the owner's enabled memory context, and what a bounded
   literal search finds among enabled memories and notes, through the memory
   layer and `context.py`; it reads no interaction history for planning or
   reasoning.
4. `planner.py` converts explicit commands into structured plan steps.
5. `action_contracts.py` declares which tool actions exist and which arguments
   each one accepts. Brain validates every step of a plan against it before any
   permission is evaluated.
6. `permissions.py` decides whether each planned action is allowed. Brain
   resolves every step of a plan before any step is dispatched.
7. `tool_manager.py` dispatches allowed actions to registered tools.
8. Tools perform their focused work and return a `ToolExecutionResult`.
9. `memory.py` owns persistent interactions, notes, explicit memories, and
   audit summaries in SQLite.
10. `brain.py` forms the final response and, on recordable completion paths,
    attempts to record the cleaned request and response as an interaction. An
    approved model-proposed action records no interaction: Brain commits its
    audit summary before dispatching it and appends the outcome afterwards.
11. `logger.py` configures Maria's named logger; application modules emit
    operational events through it.
12. `filesystem_roots.py` holds Maria's named filesystem roots, the settled
    protections covering every location that is permanently out of reach, and the
    two-identity resolution both rely on. Startup prepares the declared roots
    once, before logging is configured and before the memory layer opens the
    database.
13. `reasoning.py` defines the typed, provider-independent contract a local
    reasoning model sits behind, and `ollama_adapter.py` is the one provider
    that implements it. `conversation.py` uses them to answer the one kind of
    request the planner has no command for, only when a provider is present,
    `config.reasoning.enabled` is true, and the planner returned its exact
    generic fallback. A reply whose first character after any leading JSON
    whitespace is `{` goes back to Brain untouched, as a proposal attempt;
    every other reply is text only. See "Deterministic-First Conversation",
    "Reasoning Interface and Local Override", and "Local Ollama Adapter".
14. `plan_compiler.py` is the deny-by-default gate that turns a reasoning
    model's proposal into a plan, or refuses it. Brain is the only production
    module that imports it. See "Model-to-Plan Compilation Gate".
15. `audit.py` is the closed vocabulary of the content-free audit summaries
    Brain writes, through the memory layer, before an approved model-proposed
    action runs, and `tools/audit.py` lists, reads, and deletes them on the
    owner's commands. See "Model-Originated Execution and Audit".

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
handling flow. An action the local model proposes takes a longer path to its
tool, through the compiler, the owner's approval, and an audit summary; see
"Model-Originated Execution and Audit".

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
its displayed text. Recording covers help, unsupported commands not answered
by the local model, malformed-command usage responses, contract refusals,
denials, successful tool results, and returned unsuccessful
`ToolExecutionResult` values. An unsupported command is answered by the
local model, and so not recorded, only when a reasoning provider is
present, `config.reasoning.enabled` is true, and the planner returned its exact
generic fallback; with disabled settings it keeps its original response and
interaction row even if a provider was injected. The recording
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
produces a transient plan: every deterministic command keeps the default, and
every one that predates the policy records exactly what it recorded before the
policy existed. Transient plans have two producers. `conversation.py` makes
every conversational turn, a reply or a controlled failure, a zero-step
transient plan, so conversation writes no interaction row. `plan_compiler.py`
makes every plan it compiles from a model proposal a one-step transient plan,
which Brain runs on a path of its own that never reaches the four recording
branches, so neither a proposal's approval prompt nor its result is recorded;
a refused proposal is answered without any plan and is not recorded either.
Brain itself never chooses a policy; it only runs the plan it was given.

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

In the current implementation, successful `history.read`, `audit.list`, and
`audit.read` results are the only ones that supply a non-`None`
`recorded_message`. For `history.read` that keeps redisplayed interaction
content from being duplicated into a new interaction row, and the two audit
actions record only `Listed audit summaries.` and `Displayed audit summary
<id>.`, so neither a summary's fields nor a listing reaches interaction
history. Every other current tool result, including `history.delete`,
`notes.retitle`, `notes.edit`, `notes.delete`, and `audit.delete`, leaves the
field as `None` and therefore retains the existing message-as-recorded
behavior; those displayed messages are already short,
content-free summaries, so no override is needed. `read file`, `read note`,
and `recall` are unmodified and still record their full displayed content.

`history.delete`'s row deletion and the subsequent interaction-recording
`INSERT` are separate transactions, following the same boundary as every other
recordable path: `Memory.delete_interaction()` commits independently of the
later `Memory.record_interaction()` call. If the deletion commits but the
following recording call raises `RuntimeError`, that error is logged and the
already-computed successful deletion response is still returned unchanged;
the deletion is not rolled back and no compensating write is attempted, so an
interaction row recording the deletion is not guaranteed in that case.

`notes.retitle`, `notes.edit`, `notes.delete`, and `audit.delete` follow that
identical boundary: `Memory.retitle_note()`, `Memory.edit_note()`,
`Memory.delete_note()`, and `Memory.delete_audit_summary()` each commit in
their own transaction, independently of the later
`Memory.record_interaction()` call. A recording failure after a committed
mutation is logged, does not roll the mutation back, does not
replace the successful response, and triggers no compensating write or retry.

Because Brain records the original cleaned request as the `user_input` half of
an interaction, a `retitle note` command retains its new title and an `edit
note` command retains its new body in interaction history even though the
recorded response is content-free. `recorded_message` can only change the
response half of a recorded interaction, never the request half, so later
editing or deleting the note does not rewrite or remove those independent
interaction rows.

Some paths return before this boundary. Blank CLI input is not processed, and
blank input passed directly to `process()` returns before logging, planning,
memory context retrieval, or recording. `exit`, `quit`, `q`, main-prompt EOF or
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
and only planning pass received. An approved model-originated plan takes
Brain's model-plan path instead: it is checked again without any provider
call, runs at most once behind its committed audit summary, and nothing about
it is recorded as interaction history, whatever happens; see "Model-Originated
Execution and Audit".

Post-startup processing has no broad exception handler. If an exception
propagates before a recordable branch calls `Memory.record_interaction()`, no
interaction write is attempted.

Brain reads no interaction history for planning or reasoning. Until V1-15 it
retrieved up to three recent interaction rows on every request and handed them
to the planner, which discarded them unused; that feed is gone, and
`Planner.create_plan()` now accepts the cleaned request text and nothing else.
The `history` command still reads recent rows through its own tool, and
interaction rows remain Tier C: no automatic path hands one to the planner or
to the reasoning model.

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
operational log does retain, for a failed move, the absolute root path and the
filenames its owner-visible response omits, and a blanket filter would break
that contract while giving false confidence everywhere else. The discipline is
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
knows what its own failure may safely say. The filesystem tool's `list` action
deliberately keeps less: because a reasoning model may propose it, it logs
only the fixed lines `Listed a directory.` and `Directory listing failed.`,
never the resolved directory, the submitted path, or an exception's text,
while `read`, `rename`, and `move` keep their diagnostics and what the owner
is shown is unchanged.

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

`main.py` owns terminal concerns: reading input, printing output, showing
the confirmation prompt, accepting its aliases, and mapping the owner's answer
to the session's confirm or cancel operation. It never calls Planner, builds or
copies a plan, compares fingerprints, evaluates permission, holds the pending
plan, or re-sends the original request after approval. Every way out of the
main loop, including an exception it lets propagate, ends the session, which
discards the session's conversation and any pending approval.

`main.py` is also the runtime composition point. Its default startup is
`build_runtime_brain()`, which calls `MariaBrain.from_defaults()` with
`build_reasoning_provider` as the reasoning factory. That factory constructs
`OllamaReasoningProvider` only when the settled reasoning settings are enabled,
and returns `None` otherwise; Brain receives the result only through the
`ReasoningProvider` interface. `main.py` is the one production module that
names the adapter, and a guard test holds every other production module,
Brain included, to neither naming nor reaching it. Enabled settings the
adapter refuses, such as the `private_lan` transport, stop startup through
the ordinary sanitized failure before anything is created.

## Session Boundary

`core/session.py` owns the confirmation lifecycle for every interface. It needs
no standard input, standard output, or terminal, so a headless caller drives
the same contract the CLI does.

A session is idle or holds exactly one pending approval. Submitting a request
first discards any approval still pending, then plans exactly once. A
confirmation-required response becomes the pending approval: the immutable
`Plan` and the fingerprint captured at that moment, and nothing else. A
model-originated plan that Brain does not refuse comes back as such a
response even when `PermissionManager` allows its action outright, because
every one needs the owner's approval.

The session renders a model-originated plan for approval itself, from the
very plan it is about to bind and before binding it, so what the owner sees is
exactly what they approve. The first line is fixed, `Maria's local reasoning
model proposed this action. It runs only if you approve it.`, and the next
three name the declared tool, the action, and every argument as one JSON
object with its names in sorted order, `{}` when there are none. A whole
number is written in full, and `approval_quoted` writes each name and text
value between quotation marks. A visible ASCII character stays as itself,
except that a quotation mark or a backslash is preceded by a backslash, and a
single space between other characters stays a space. Every other character, a
leading, trailing, or repeated space, a control character, or any character
outside ASCII, becomes a four-digit `\uXXXX` escape, with a character beyond
the Basic Multilingual Plane written as its surrogate pair. Text holding a
surrogate code point is refused rather than quoted, because two separate
surrogates would decode as one different character; no compiled plan can hold
one. The result is printable ASCII that decodes as JSON back to exactly the
argument values, so nothing in it can hide, reorder, or start a new line, and
nothing is truncated. Brain's context notices for that turn follow below it,
once; they
are not kept with the approval or shown again on confirmation. A plan that
cannot be rendered raises `ValueError` and never becomes pending.

Confirming first checks that the pending plan is still an exact `Plan` whose
origin is exactly `DETERMINISTIC`, or still exactly a well-formed
model-originated plan, and only then compares its current fingerprint with the
captured one, because the fingerprint reads the origin and every step. A plan
that fails that check, cannot be fingerprinted at all, or no longer matches
gets the same fixed refusal, never an error. The approval is consumed before
any of this, so a failing tool or a raised exception cannot make it
replayable.
Cancelling discards the pending approval and executes and plans nothing. A
second confirmation, a confirmation after cancellation, and a confirmation
after any intervening request all execute nothing and return one fixed,
content-free refusal, so an approval can never be transferred to a plan the
owner was not shown even when two requests read identically.

The pending approval is transient process state. It is never written to SQLite,
a file, or configuration, never serialized, and never logged; only a plan's
fingerprint could be logged, because the canonical content behind it can
contain note bodies, memory facts, and absolute paths. Its `repr()` is
`PendingApproval(<redacted>)`, which shows none of the request, the arguments,
what the owner was shown, or the fingerprint. `PermissionManager` is
consumed unchanged: the session decides nothing about permission, and Brain
resolves every step of a plan before dispatching any of them. The session
stores no permission resolution alongside the pending plan, because
confirmation re-resolves the plan's current permissions rather than replaying
the decisions that were current when it was shown.

A session also owns its conversation: the completed conversational exchanges
of this session alone, held as a tuple of `ReasoningExchange` values in process
memory. `submit()` hands them to `MariaBrain.process()` with the request, and
only a completed conversational reply replaces them; a command, its result, a
refusal, a failed turn, a model proposal, its approval prompt, or an approved
proposal's result never does, though a turn that had to clear the
conversation, a proposal turn included, still clears it. Whatever comes back is
held to the retention limits again in the session itself, at most eight
exchanges and at most 8 KiB of content. `end()` discards the conversation and
any pending approval, writes nothing, and is idempotent. Brain keeps no state
between requests, so a new session always starts with an empty conversation
and can never observe another session's turns.

## Permission Policy

All planned actions pass through `PermissionManager`.
`filesystem.rename` and `filesystem.move` always require confirmation, while
`filesystem.delete` and `filesystem.write` are always denied. TOML
configuration may add restrictions but cannot remove these mandatory
protections. Missing permission arrays retain the secure baseline, and
malformed present arrays stop configuration loading instead of weakening it.
When an action is both denied and confirmation-required, denial wins.

In the current implementation, the mandatory confirmation baseline is
thirteen actions: `audit.delete`, `filesystem.rename`, `filesystem.move`,
`history.delete`, `notes.retitle`, `notes.edit`, `notes.delete`,
`notes.enable_context`, `notes.disable_context`,
`memory.update`, `memory.forget`, `memory.enable_context`, and
`memory.disable_context`. The confirmation for `memory.enable_context` also
carries a fixed notice, from `CONFIRMATION_NOTICES` in `core/permissions.py`,
that an enabled memory may be sent to the local reasoning model on any
conversational turn within Maria's memory context limits, and that Maria does
not choose memories by relevance. The confirmation for `notes.enable_context`
carries one that the note's entire title and body may be sent on a later
conversational turn when a word of four or more characters from the request
appears literally in its title or body, and that Maria does not check the note
for passwords or other secrets. The confirmation for `audit.delete` carries one
that deleting an audit summary removes it from Maria's database but does not
undo the action, and that Maria's operational log may still show that a
model-proposed action ran.
`audit.list`, `audit.read`, `history.list`, `history.search`, `history.read`,
`notes.create`, `notes.list`, `notes.read`, `memory.store`, `memory.search`,
and `memory.list` are allowed by default. These entries live
in `MANDATORY_CONFIRMATION_ACTIONS` in `core/permissions.py` rather than in
`config/config.toml`, so configuration can add restrictions on top of them but
can never remove them.

A model-originated plan meets one further requirement, which belongs to Brain
rather than to `PermissionManager`: it always needs exactly one approval from
the owner, even when its action is allowed outright, and a denied action is
refused before any approval is offered. That requirement adds no entry to
either mandatory set and changes no decision `PermissionManager` makes, so the
three model-eligible actions keep their permission policies; see
"Model-Originated Execution and Audit".

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

Every contract also declares its model eligibility, `MODEL_ELIGIBLE` or
`NOT_MODEL_ELIGIBLE`, as a required keyword with no default, so no action can
be declared without that decision. Only `calculator.calculate`,
`filesystem.list`, and `system.current_time` are model-eligible; every other
action, the three audit actions included, is not. The
classification is independent of permission and approves nothing by itself;
see "Model-to-Plan Compilation Gate".

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
earlier one is incomplete. A model-originated plan keeps that order, its
argument contract checked among the compiler's gates before its permission is
resolved, but it takes a path of its own in Brain and none of its refusals is
recorded; see "Model-Originated Execution and Audit".

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
Brain's rule that a model-originated plan always needs the owner's approval is
keyed to the plan's origin rather than to any action, and it never overrides a
denial or changes what `PermissionManager` decides.
The tools also keep their own argument checks as defence in depth, so a
malformed value is refused rather than converted even on a path that did not
come through Brain.

Production plans still contain at most one step, because the planner emits at
most one and the compiler exactly one. These guarantees therefore protect plans
a later stage may produce; they change no command available today. A
single-step confirmation keeps its existing wording, while a longer plan is
rendered as a deterministic, plan-ordered list of each step's tool action and
resolved permission outcome. That rendering repeats no argument value, so it
exposes no absolute host path, no fingerprint, and no canonical plan content;
it is not logged, and a pending confirmation is not written to interaction
history. A model-originated plan is never rendered that way: the session shows
every one of its argument values exactly, as "Session Boundary" describes. A
model-supplied path is always plain text relative to the default named root,
never a resolved host path, and that approval text is likewise never logged,
recorded, kept in the conversation, or sent to the model.

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
`docs/database.md` describes the rules; the four record types below are
unchanged by them. The current schema is version 5, whose one appended
migration, `create_audit_summaries`, creates the two empty audit tables,
`audit_summaries` and `audit_outcomes`, and changes no existing table or row.

Maria keeps four kinds of persistent data separate:

- **Interaction history** records cleaned requests and the responses chosen
  for recording on the four recording branches described above, for every
  plan whose recording policy is `RECORD`, whether by default or set
  explicitly; a `TRANSIENT` plan adds no row. Neither Brain nor the planner
  reads these rows, and no automatic path supplies one to the reasoning model.
  Interaction rows are never converted automatically into explicit memories.
- **Notes** are structured title-and-body records deliberately created by the
  user. The notes tool uses the shared `Memory` persistence interface. A note's
  title and body are edited through two separate confirmation-gated commands,
  so neither can overwrite the other; both preserve `created_at` and refresh
  `updated_at`. A note may also be deleted by numeric ID. None of those
  mutations cascade into interaction history or explicit memories. Each note
  also carries the owner's context consent, off unless the owner enables that
  one note through a confirmation-gated command; the consent is stored as the
  identity of exactly the title and body the owner approved, and a retitle or
  an edit turns it off in the same transaction. Only such notes can reach the
  reasoning model, through a literal search; see "Tier B Search Context"
  below.
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
  Neither mutation cascades into interaction history or notes. Each memory
  also carries the owner's context consent, off unless the owner enables that
  one memory through a confirmation-gated command; updating a memory turns its
  consent off in the same transaction, because consent covers the exact text
  the owner approved. Only enabled memories can reach the reasoning model; see
  "Tier A Memory Context" and "Tier B Search Context" below.
- **Audit summaries** record that the owner approved one model-proposed
  execution attempt and that Maria authorised it for dispatch. Each is one
  `audit_summaries` row, holding Maria's own UTC timestamp, the action's
  canonical identifier, the decision `owner_approved`, and a generalised
  target, `none` or `named_filesystem_root`, with at most one `audit_outcomes`
  row, `succeeded` or `failed`, appended after dispatch. Every value comes
  from the closed vocabulary in `core/audit.py` and is validated when written
  and again when read; none holds the request, the proposal, an argument, a
  fingerprint, private context, a root's name, a host path, a tool's result,
  or exception text. Only Brain writes them, through the memory layer and only
  for an approved model-originated plan, and normal writes only append. The
  confirmation-gated `delete audit <id>` is the one removal: it takes one
  summary together with its outcome in one transaction, and ids are never
  reused. Nothing cascades between summaries and the other record types.
  Neither Brain nor the planner reads them, and no automatic path supplies one
  to the reasoning model; see "Model-Originated Execution and Audit" below.

On the completed paths described above, memory commands and their responses
also appear in interaction history when recording succeeds. A stored fact can
therefore exist both as an explicit memory and as part of an interaction.
Because Brain records the cleaned request verbatim, an `update memory <id>:
<new fact>` command retains its new fact in `interactions.user_input` even
though the displayed and recorded response is only `Updated memory <id>.`;
`recorded_message` can shorten the response half, never the request half.
Forgetting or later updating that memory does not rewrite or remove those
independent interaction rows.

Each persistent mutation a command makes commits in its own transaction, and
Maria then makes a separate attempt to record the command and its short
response. The two are never combined into one transaction: if the later
recording fails, the mutation remains committed, the successful response is
unchanged, and no interaction row recording it is guaranteed. There is no
compensating rollback, retry, or replay.

## Reasoning Interface and Local Override

`core/reasoning.py` is the contract every later model stage builds on, and it
deliberately contains no transport, no model, and no provider. A
`ReasoningRequest` holds one bounded prompt; in `history`, the earlier
exchanges of the current conversation, oldest first; and in `context`, the
records Maria selected — Tier A memories, then Tier B search matches — each a
`ContextItem` from `core/context.py` bearing the source and record id Maria
read. Both are empty by default. A request's one switch,
`proposal_instructions`, must be exactly `True` or `False` and is `False` by
default; it selects Maria's own fixed `PROPOSAL_INSTRUCTIONS`, which no caller
can change, and a request with it off is exactly what a request was before
the flag existed. Each
`ReasoningExchange` is one user text and one assistant text, exact, non-empty,
and redacted from every representation, and so is every `ContextItem`. There
is no field through which a caller's own system prompt, a template, or raw
interaction history could travel, and a `ReasoningResponse` holds one bounded,
untrusted string.

A request's size is defined once, by `request_content_bytes`: the UTF-8 length
of the prompt, plus the UTF-8 length of every exchange's user and assistant
text, plus the fully rendered context — Maria's fixed context instructions
and the delimited data block with every header, source, and id — which is
nothing when the context is empty, plus, when the flag selects them, the
proposal instructions and, with context, the two-byte separator that joins them
to the context instructions. No role name, message structure, model
identifier, or transport framing counts. The context is validated again every
time it is measured, rather than trusted from construction: at most 16 Tier A
memories first, rendering on their own to at most 2 KiB, then at most 8 Tier B
items grouped by source, each group in strictly ascending id order with no
memory in both tiers, at most 24 items rendering to at most 4,096 bytes in all.
Construction bounds that total by the 1 MiB hard
ceiling. `request_reasoning` measures it again against the configured
`max_input_bytes` before the provider is asked, re-reading every text rather
than trusting construction and stopping as soon as the total passes the
bound, so a request tampered into holding far more is never copied to be
measured. The
`ReasoningProvider` protocol is what the local adapter,
`core/ollama_adapter.py`, implements.
`request_reasoning` is the one boundary through which a provider is asked
anything: it answers without asking when reasoning is disabled or the request
is over budget, asks at most once, and returns either a fresh response holding
inert text or a typed `Failure` from the reasoning kinds in `failures.py`. A
provider's own exception never travels, a reply that is not exactly a bounded
response is malformed, and an oversized reply is discarded whole rather than
truncated. The module's own imports include no network, threading,
asynchronous, or subprocess module and nothing from planning, permissions,
dispatch, persistence, or tools, so nothing in it can turn a reply into an
action. A reply's one route to an action runs through Brain, which hands a
proposal attempt to the compiler, and from there only through a permission
check, the owner's approval, and a committed audit summary; see
"Model-Originated Execution and Audit". Prompts, replies, the host, and the
model identifier are never shown by `repr()` or `str()`, never interpolated
into an exception message, and never logged; the boundary logs only an outcome
and a failure code.

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

## Local Ollama Adapter

`core/ollama_adapter.py` is the one local runtime the owner selected for v1.0
(decision V1-13-R1): Ollama's native HTTP API on the owner's own machine.
`OllamaReasoningProvider` implements `ReasoningProvider`, and everything
specific to Ollama — the destination, the request and reply shapes, and the
HTTP framing — is confined to that module. It is the only production module
that imports a network module, and it imports only `socket`: no HTTP library,
so no proxy, redirect, or URL handling exists to be reached. Only `main.py`
constructs it, and only for enabled settings; Brain, the planner, memory, and
tools never import or reach it.

The destination is fixed in the module and is not configuration: the IPv4
loopback address `127.0.0.1`, port `11434`, and path `/api/chat`. Construction
re-validates the settings and refuses, with a fixed `ValueError`, disabled
settings, any transport but `loopback`, any host but `127.0.0.1`, and a model
identifier that designates an Ollama cloud model (a `cloud` tag or one ending
`-cloud`). So neither a `private_lan` host nor another loopback address can
ever be contacted. Each request opens one IPv4 connection straight to the
numeric address, with no name resolution, no proxy, no redirect, no retry,
and no fallback of any kind.

The request carries only the model identifier, as an opaque JSON string, and
the conversation, with `stream` false: each earlier exchange as a `user`
message followed by an `assistant` message, oldest first, and then the prompt
as the final `user` message. With the proposal-instructions flag off, a
request is what it was before the flag existed: with neither history nor
context it is the prompt as one user message, byte for byte the request V1-13
sent; with no context it is byte for byte the one V1-14 sent; and with Tier A
context alone it is byte for byte the one V1-15 sent. Brain's production path
always turns the flag on, so every request it sends begins with one `system`
message holding Maria's fixed proposal instructions. When the request carries
context, Maria's fixed context instructions follow them in that same message,
after a blank line, or form the only `system` message when the flag is off;
neither ever contains stored text. The delimited data block then follows as a
`user` message of its own. The context instructions are the V1-15 text unless
a Tier B item is present, when a fixed variant also says some records were
found because they contain a word from the current request. There is no
option, tool, or thinking request. Before anything is serialized, the adapter
validates the context again and measures the prompt, history, rendered
context, and any selected instructions together against its own
`max_input_bytes` with the same `request_content_bytes` accounting, and
refuses an over-budget request as input over budget without opening a
connection. Of the reply, only
`message.content` is kept, as inert text. Thinking text, tool calls, and every
other field are ignored after bounded JSON parsing; only `message.content` is
returned. The module has no import path to planning, permissions, dispatch, or
persistence, so nothing in it can turn a reply into an action.

One absolute monotonic deadline, `request_deadline_seconds` from the start of
`respond`, covers connecting, sending, the response headers, and the body.
Every socket wait is given only the time that remains, so a runtime that
trickles bytes cannot stretch it. The status line and headers may take at
most 16 KiB. Only `200` carries an answer; any other status, a redirect
included, is unavailable. The body must be framed by one `Content-Length` or
by chunked transfer coding, so a reply that ends early can always be told
from a complete one. The whole body, chunk framing included, is bounded
before parsing by an envelope of 64 KiB plus six times `max_output_bytes`,
because JSON may spell one byte of the answer as up to six. The extracted
answer must then be non-empty and fit within `max_output_bytes`. Nothing
oversized is read in full, and nothing is truncated or returned in part.

Every other outcome is one typed reasoning failure: input over budget before
any connection; timeout at any stage; unavailable when nothing answers or the
status is not success; truncated when the reply ends early, `done` is false,
or `done_reason` is `length`; malformed for any other unexpected shape; and
output over budget for an oversized envelope or answer. The failure is raised
outside every handler, so no caught exception travels with it as a cause or
context, and every connection is closed on every path. The adapter logs
nothing itself; `request_reasoning` records the outcome code, and no prompt,
reply, address, model identifier, endpoint, header, or runtime error text
reaches a log or a failure message.

Ollama's own local-only setting is separate and is the owner's to configure.
The adapter's refusal of cloud-tagged model identifiers is defense in depth
and cannot prove the runtime will not reach a cloud model.

## Deterministic-First Conversation

With a reasoning provider present and reasoning enabled, Maria answers the one
kind of request its planner has no command for, its exact generic fallback,
with the owner's local model, in the context of the current session's earlier
conversational turns: with a text reply or, when the model proposes one of the
three actions it may propose and the proposal passes every check, with a plan
offered for the owner's approval. Any other proposal is refused. Everything
else is exactly as it was.

```text
CLI
-> MariaSession (conversation and supplied-record fingerprints)
-> MariaBrain.process(request, *conversation, supplied_context=...)
-> Planner                               always first, text only
-> provider, enabled, exact fallback?    otherwise the ordinary path
-> Memory.list_context_memories(17)      the one Tier A read
-> Memory.context_*_by_id(...)           recheck of supplied records, if any
-> Memory.search_context_*(terms, 9)     Tier B reads, only when there are terms
-> conversation.converse(proposals=True) recheck, budget, selection, notices
-> request_reasoning -> ReasoningProvider
-> reply opens with "{"?                 if so, to Brain's compiler instead
-> zero-step TRANSIENT plan
-> Brain's ordinary zero-step path       no contract, permission, tool, or row
-> MariaSession keeps the new conversation and fingerprints
```

Resolution order lives in Brain. The planner runs first on every request and
is unchanged. Brain consults the model only when all three hold: a reasoning
provider was injected, `config.reasoning.enabled` is true, and the planner's
plan is exactly its generic no-action fallback, recognised by
`conversation.is_generic_fallback()`: a default-recorded plan with no steps
whose response is exactly `GENERIC_FALLBACK_RESPONSE`, a named copy of the
planner's text that a test holds to the planner's real output. Help,
`commands`, and every usage error are zero-step plans with other text, so they
never reach the model; blank input returns before planning; and the
confirmation path never plans at all. With no provider, which is what the
shipped configuration produces, or with disabled settings even when a provider
was injected, the fallback is answered and recorded exactly as before: its
original response and its interaction row.

`conversation.converse()` settles the whole turn:

- The budget is 8 KiB (`CONVERSATION_MAX_BYTES`) or the configured
  `max_input_bytes`, whichever is smaller, counted as `request_content_bytes`
  counts: the current request, Maria's fixed proposal instructions with the
  separator that joins them to any context instructions, the fully rendered
  context, and the UTF-8 text of the retained exchanges' user and assistant
  halves.
- Every context read is settled first. A failed or untrustworthy read, or a
  supplied memory or note that has since been disabled, changed, deleted, or
  forgotten, clears the earlier conversation before anything is sent; see
  "Tier A Memory Context" and "Tier B Search Context" below.
- A request that is over the budget together with the proposal instructions
  is refused with the fixed input-over-budget message before the provider is
  asked; the instructions are never dropped to make it fit. Text the interface
  cannot carry, such as a lone surrogate, is refused with its own fixed
  message.
- The request and the proposal instructions come first, then as much Tier A
  context as its rule fits, then as many Tier B matches as fit, memories
  before notes, then the newest earlier exchanges that fit beside all of them,
  at most eight (`CONVERSATION_MAX_EXCHANGES`); the oldest are dropped first,
  whole. Nothing is truncated. A conversation that would then carry more than
  64 supplied records is cleared before sending instead.
- The provider is asked exactly once through `request_reasoning`. A typed
  failure, a crash, or a malformed reply becomes that failure's fixed message.
- A reply whose first character after JSON whitespace (a space, tab, carriage
  return, or line feed) is `{` is a proposal attempt. Nothing here parses it:
  it goes back to Brain untouched, with the turn's notices, and Brain hands it
  to the compiler; see "Model-to-Plan Compilation Gate". It never becomes an
  exchange, and the fingerprints of the records the request carried are not
  kept either, while any clearing this turn already made stands. Any other
  first character, other whitespace or `[` included, keeps a reply
  conversational.
- Any other reply is kept only if its whole exchange, request and reply
  together, fits the budget. Otherwise the reply is discarded with a fixed
  message: it is not shown in part, kept, or recorded.
- A reply that passes is shown verbatim and becomes the newest exchange. The
  session keeps the result, again bounded to eight exchanges and the budget,
  with the fingerprints of the records that request carried.
- Every outcome then ends with Maria's own notices about context, after a
  blank line. They are built from the sources and ids Maria selected, never
  from the reply, and they never enter the request or the exchange the session
  keeps. After a proposal attempt they follow whatever Brain answers, a
  refusal or the approval prompt, exactly once, and they are never kept with
  a pending approval or shown again when it is confirmed.

Every other outcome, reply or refusal, is a zero-step plan that
`conversation.py`, its producer, marks `TRANSIENT`, and Brain accepts nothing
else as a conversational outcome: anything but an exact zero-step, transient,
deterministic plan is refused with a fixed message. Brain runs it through its
ordinary zero-step path, which returns before contract validation, permission
resolution, or tool dispatch, and whose recording gate writes nothing for a
transient plan. No conversational request or reply is ever recorded, and Brain
never chooses a recording policy itself. A conversational reply is untrusted,
inert text: nothing parses it, it is never handed to the planner, it cannot
become a pending approval, and a reply that reads like a command or an
approval does nothing. Only a proposal attempt is ever parsed, by the compiler
alone, and even a compiled one runs only after the owner approves it.

What the model sees is Maria's fixed proposal instructions, the current
request, the session's own completed conversational exchanges, the explicit
memories the owner has enabled for context, and notes the owner has enabled
for context that literally contain a word of the request. It never sees a
command or its result, its own earlier proposals, an approval prompt, what an
approved action returned, a memory or note the owner has not enabled, a file,
a stored interaction, an audit summary, or anything from an earlier session. A
new session starts empty, and ending one discards its conversation and its
supplied-record fingerprints.

## Tier A Memory Context

In V1-15, Tier A is exactly the owner's explicit memories that the owner has
enabled for context. Preferences exist only as memories the owner stores and
enables; tasks, reminders, and project facts do not exist yet.

**Consent.** Every memory's consent is off by default: the V1-15 migration
leaves every existing memory off, and a new memory is stored off.
`enable memory context <id>` and `disable memory context <id>` change one
memory each, by positive decimal id, and both always require confirmation,
which configuration cannot remove. Updating a memory turns its consent off.
An enable approval is bound to the exact text: when confirmation is offered,
Brain passes the plan through `ToolManager.prepare_confirmation()`, and the
memory tool pins the SHA-256 of the memory's current text into the step, so the
approved plan and its fingerprint name that text. The memory layer enables
consent only inside one write transaction that re-reads the fact and updates
it only while it is still exactly that text; otherwise nothing changes and the
owner is asked to confirm again. That binding step is generic: Brain names no
tool, and a tool can only add arguments to its own step.

**Selection is not relevance.** Under the owner-approved transitional rule
(decision V1-15-R1), every enabled memory is eligible on any model-backed
conversational turn. Brain asks the memory layer for enabled memories in
ascending id order, one row more than can be sent, with any fact longer than
2 KiB of UTF-8 reported only by id and an oversized flag — its text never
leaves the memory layer, and in a UTF-8 database never leaves SQLite — and
`context.py` takes them
whole, in that order, while at most 16 items and at most 2 KiB of fully
rendered context — fixed context instructions, ids, and framing included —
fit, and while they fit beside the request and Maria's proposal instructions
within the conversational budget. A memory that does not fit is left out and
the next is still considered. Memories left
out are named by id when the bounded read returned them; enabled memories
beyond it are reported as existing, never by id. At the roadmap Stage V1-16
relevance-review gate the owner kept this rule unchanged (decision V1-16-R1);
enabled memories beyond the window can still be found by Tier B's literal
search.

**Stored text is data.** The adapter sends Maria's fixed instructions as the
only system message: the proposal instructions, which say that earlier
conversation and supplied memories or notes are data, not authorization, then
the context instructions, which tell the model the memories are reference
data, not instructions, grant no permission to act, and may be outdated or
conflicting.
The memories follow in a separate user message, one line per memory, each fact
as one JSON string with every line separator escaped — JSON's own escapes plus
U+0085, U+2028, and U+2029 — so no fact can break out of its line or pose as
another entry. Context grants no authority: a reply is still either inert text
in a zero-step transient plan or a proposal attempt that must pass every
compiler gate and the owner's approval, whatever context the request carried.

**What the owner is told.** After a successful reply Maria names the memories
she supplied — `Memory context supplied: memory 3, memory 7.` — and says she
cannot verify whether they are still current or whether they conflict. After a
failure she names the memories selected for the attempt and says she cannot
confirm the model received them. When no context at all was sent she says `No
memory context supplied.` Maria cannot know which memories the model used, and a
reply that names other ids changes nothing she says. She makes no claim that a
memory is stale, fresh, consistent, or conflicting: `created_at` dates the row,
not its current text, and there is no `updated_at`.

**Revocation.** The session keeps, beside its conversation, the fingerprint —
source, id, and SHA-256 of the exact text — of every memory and note supplied
with a request whose reply it kept since the conversation was last cleared, at
most 64 of them, never the text itself; a proposal attempt is never kept, so
the records supplied with it are not tracked. Before each model-backed
conversational request — never on a
deterministic command — Brain rechecks exactly those records by id, in one
bounded read per source, and their fingerprints are compared with what is
enabled now; only the records they name are hashed, and nothing is read or
hashed when nothing was supplied. A supplied record that is now oversized
cannot be the text that was sent. If one was disabled, changed, deleted, or
forgotten, by this session or through any other database connection, the
whole earlier conversation and the fingerprints are discarded before anything
is sent, and the owner is told that the earlier copy was invalidated. Since
V1-16 the recheck is by id rather than against Tier A's own read, so a memory
that merely moves past Tier A's 17-row window, unchanged, no longer clears the
conversation. If any context read fails, neither context nor earlier exchanges
are sent, both are cleared, and a fixed notice says so. This bounds what Maria
sends next; it sees current state only, so a record changed away and back to
the identical text between two checks is indistinguishable from one never
changed; it is not an instantaneous revocation across a concurrent read and
send; and it cannot erase text a model has already received.

## Tier B Search Context

Tier B is what a bounded literal search of the current request finds among
records the owner has enabled for context. In V1-16 it has two sources:
enabled memories beyond Tier A's window, and notes the owner has enabled with
`enable note context <id>`. Nothing without that consent is ever searched.

**Note consent.** Every note's consent is off by default: the V1-16
migration leaves every existing note off, and a new note is stored off.
`enable note context <id>` and `disable note context <id>` change one note
each, by positive decimal id, and both always require confirmation, which
configuration cannot remove. The enable confirmation says the note's entire
title and body may reach the local model on a later literal match, and that
Maria does not check the note for passwords or other secrets; consent is not a
claim that the text holds none. Below that notice the confirmation shows the
note's current title and body in full, each as one quoted line in which every
invisible or line-breaking character is escaped, so the owner sees exactly the
text being approved and no stored text can pose as Maria's own line. The
approval is bound to that shown title and body: the notes tool reads both once,
and from that one read both builds the preview and pins their identity, the
SHA-256 of both together as one JSON array, into the step. Only the identity
enters the pending plan; the preview is shown through Tool Manager's generic
`ConfirmationBinding` and is never recorded, logged, retained in the
conversation, or sent to a model. The memory layer enables consent only inside
one write transaction that re-reads both and stores that identity as the
note's `context_digest`, so a title or body changed before the owner confirms
is refused and nothing is enabled. Retitling or editing the note turns
it off in the same transaction. Because the stored consent is the approved
text's identity, a note whose title or body was changed through any other
connection, even one that left `context_digest` in place, no longer matches it
and is never eligible, in this session or a later one.

**The search rule.** `context.search_terms()` casefolds the cleaned request,
splits it on whitespace, strips every non-alphanumeric character from both
ends of each word, keeps words of 4 to 64 characters, drops repeats in
first-seen order, and takes the first 8. A short request such as "Jo" or "AI"
has no terms, and a request with no terms runs no Tier B read at all. Matching
runs in a Python matcher registered on the one read's connection, which SQLite
hands a text only after that row's stored size is within the bound, and which
settles the text's exact UTF-8 size before casefolding it. A memory whose fact
is within 2 KiB matches when a term occurs literally in its current fact,
casefolded, so a stored casefolded copy left stale by another connection can
neither add nor hide the match. A fact too large to supply never leaves SQLite
and cannot be casefolded, so it is matched against its stored casefolded copy
instead and can only be named, by id; Maria writes that copy with every fact,
but a copy changed outside Maria can name an oversized memory that no longer
holds the term, or miss one that does. A note is searched only while its
consent is on, both texts are within 2 KiB, and an approval check inside
SQLite finds they are still exactly the approved text; only then does the
matcher see either text, and the note matches when a term occurs wholly within
its casefolded title or wholly within its casefolded body; the two are never
joined. A note changed outside Maria is therefore never matched and never uses
up a read's rows, and a note with a text too large to verify is never
searched. There are no model-generated queries, `LIKE` wildcards, rankings,
embeddings, or fuzzy or semantic matching.

**Bounded reads.** The memory search covers only enabled memories after the
16th one Tier A considered, and runs only when Tier A's read found more than
16, so Tier A's 17th, lookahead, row is its first candidate and a memory Tier
A left out for size is never retried. Each source's read returns at most 9
rows in ascending id order; that limit bounds the rows returned, not every row
SQLite examines to find them. A memory beyond 2 KiB of UTF-8, measured exactly
whatever the database file's encoding, is returned only by id and an
oversized flag, so it is named and never supplied, and it counts toward its
read's rows. A note search never returns a note by id only: a note whose title
or body is beyond 2 KiB cannot have its approved identity verified, so it is
neither matched nor named, and a note search result that named one would be
treated as an unreadable read.

**Selection and bounds.** The first 8 rows of each source are candidates,
memories before notes, each in ascending id order, and are taken whole while
fewer than 8 have been taken and everything rendered still fits. Tier A keeps
its 16 items and 2 KiB; both tiers together are at most 24 items and 4,096
fully rendered UTF-8 bytes — context instructions, headers, source ids, JSON
quoting, and escaped line separators included — and everything fits beside
the request and Maria's proposal instructions within the conversational
budget. The request and those instructions come first, then Tier A, then
Tier B memories, then Tier B notes, then earlier exchanges. The limits are
validated again when a `ReasoningRequest` is constructed and in the adapter
before any connection.

**Rendering and notices.** Tier B records follow their own header in the same
data block, each on one line as its source and id and then JSON: a memory's
fact as a string, a note's title and body as one object, with the same line
separator escapes as Tier A. After a successful reply Maria names them —
`Found by search and supplied: memory 21, note 4.` — with the same caveat that
she cannot verify whether they are current or conflict; after a failure she
names them as selected; matches the limits left out are named by id when a
read returned them, and further matches are reported as existing. Notes that
share exactly a title but have different bodies are named together as
possible divergent versions; Maria never calls them contradictory, never calls
a record stale, and treats a note's `updated_at` only as the fact of when it
was last saved.

## Model-to-Plan Compilation Gate

`core/plan_compiler.py` turns one untrusted reasoning-model proposal into a
plan, or refuses it. It is deny-by-default where `PermissionManager` cannot be:
`PermissionManager` allows any action absent from its mandatory sets, so the
positive gate lives here and runs before any permission decision. Brain is the
only production module that imports it, which a guard test enforces. Brain
compiles a proposal only on the model-backed conversational path: when a reply
is a proposal attempt, it hands `compile_proposal` the owner's cleaned request,
the reply exactly as it arrived, and Tool Manager's active contract registry.
A refusal is answered with its fixed message, followed by the turn's context
notices, and nothing is repaired, retried, or asked of the model again.

`compile_proposal` accepts exactly one JSON object holding `tool`, `action`,
and an `arguments` object, which is required even when empty, at most 4,096
UTF-8 bytes measured before parsing. Parsing is strict: a repeated key, `NaN`
or `Infinity`, nesting deeper than eight containers, surrounding prose, a code
fence, a second document, and truncated text are all refused, and nothing is
repaired. The gates run in a fixed order and the first failure decides the
refusal: the byte cap, strict parsing, the proposal's shape, the allowlist and
eligibility, the action contract, then a text check and a path check on each
present argument.

An action is open to the model only when two independent declarations agree:
the compiler's own closed `MODEL_ACTION_ALLOWLIST` and the contract's
`MODEL_ELIGIBLE` classification. A test proves they agree in both directions,
and changing either one alone opens nothing. Every argument is then validated
against its contract. An argument string may contain no control, format,
surrogate, line-separator, or paragraph-separator character; such a value is
refused, never stripped.

A model-supplied path must be plain text relative to the default named root:
no root, drive, UNC share, leading `~`, parent traversal, NUL, colon, or
reserved device name, under the Windows and the POSIX reading alike. Parent
traversal includes the spellings made only of dots and spaces that Windows may
read as it, such as `...` or `. .`. The path must also name something: a
standalone `.` or `./` is refused, while a harmless interior `.`, as in
`a/./b`, is accepted and kept exactly as written. This is a lexical preflight
with no filesystem access, so it cannot see a symbolic link and establishes
nothing about the resolved location; `FilesystemRoots` and `FilesystemTool`
still decide resolved containment and the permanent exclusions when a plan
runs. It is also a temporary restriction: a model cannot yet name a
non-default root or an absolute path.

A passing proposal becomes the existing single-step `Plan`. Its step names
Maria's own declared tool and action and holds the arguments exactly as
supplied; its `original_request` is the owner's cleaned request, never model
text; it is recorded as `TRANSIENT`; its origin is `PlanOrigin.MODEL`; and it
is immutable and fingerprinted like every other plan. A refusal is one
`ProposalRejection` member with a fixed message that names no tool, action,
argument, or value the model supplied, and no parser or contract error
escapes. The gate logs only whether a proposal compiled and, when it did not,
the refusal code, never the proposal, a plan, or a fingerprint.

`recheck_model_plan` applies the same gates again to an already compiled plan,
exactly as it stands: the allowlist with the eligibility the current registry
declares, the argument contract, then the text and path checks. Nothing is
parsed, rebuilt, repaired, or substituted, and no provider is asked anything.
Brain asks it when it offers a compiled plan for approval and again just
before the approved plan runs, because an approval authorises a plan rather
than freezing the registry that was current when it was shown. A refusal logs
only its code.

## Model-Originated Execution and Audit

A plan compiled from a model proposal is the only way model output can lead to
an action, and it has one path to its tool:

```text
MariaBrain.process()                     provider, enabled, exact fallback
-> conversation.converse(proposals=True) one provider call
-> reply opens with "{"                  after JSON whitespace only
-> plan_compiler.compile_proposal()      Tool Manager's active registry
-> one-step TRANSIENT MODEL plan         or a fixed refusal, never retried
-> recheck_model_plan()                  the compiler's gates, unchanged plan
-> PermissionManager                     denied: refused before approval
-> MariaSession                          renders the plan, binds the approval
-> owner approves: MariaSession.confirm()
-> MariaBrain.execute_confirmed_plan()   no planner, context, or provider
-> recheck_model_plan()                  again, against the current registry
-> PermissionManager                     again; denied: nothing runs
-> Memory.record_audit_summary()         committed, or nothing is dispatched
-> ToolManager.execute_tool()            exactly once, approved arguments
-> Memory.record_audit_outcome()         succeeded or failed
-> the result, then "Recorded as audit summary N."
```

**Origin.** `Plan` carries its origin as a `PlanOrigin`: `DETERMINISTIC`, the
default, for every plan Maria's own code builds, and `MODEL`, which only the
compiler sets. Like the recording policy, it must be an exact member and is
part of plan equality, hashing, and the fingerprint, which it joins only when
it is not the default: every deterministic plan keeps exactly the fingerprint
it had before, while a model-originated plan never fingerprints like an
otherwise identical deterministic one. A `MODEL` plan can only be transient,
with exactly one step and no canned response; any other shape is refused when
the plan is constructed, and every boundary that acts on one asks again
whether it is still exactly that shape.

**Approval.** Brain offers every model-originated plan that passes its checks
for exactly one approval from the owner, even when `PermissionManager` allows
its action outright, and refuses a denied action before any approval is
offered or any audit summary exists. That requirement is Brain's, keyed to the
plan's origin, and adds nothing to `PermissionManager`. Brain and the session
treat every origin but exactly `DETERMINISTIC` this way, so a plan whose
origin was altered can only be refused, never run as a deterministic plan.
The plan offered is
the very plan the compiler built: Brain never passes it through
`ToolManager.prepare_confirmation()`, which refuses a model-originated plan, so
nothing can be added to it. The session renders it and binds the approval to
its fingerprint, as "Session Boundary" describes, and cancelling, a new
request, ending the session, or a changed plan keeps it from running.

**Confirmation.** On approval the session hands that same plan to
`MariaBrain.execute_confirmed_plan()`, after refusing one that is no longer
exactly a well-formed model-originated plan. Brain checks it again, its shape
and origin, `recheck_model_plan` against the registry Tool Manager holds now,
and its current permission, without asking any provider, running the
planner, retrieving context, reparsing the proposal, or rebuilding the plan. A
plan that fails any check is refused and nothing runs, and dispatch itself
refuses a plan that was not approved.

**The audit gate.** Before the tool runs, Brain has the memory layer insert
and commit exactly one audit summary: the contract's canonical identifier, the
decision `owner_approved`, and the target class the contract implies,
`named_filesystem_root` for an action that declares a path argument, whether
or not a path was supplied, and `none` otherwise. The memory layer adds
Maria's own UTC timestamp and validates it like every other field. When the
summary cannot be committed, for example
because the database stays locked, no row was inserted, or the returned id is
not a positive whole number, nothing is dispatched, the owner is told that the
approved action did not run because its audit summary could not be recorded,
and the approval stays spent. Otherwise the step is dispatched exactly once,
with exactly the immutable arguments the owner approved, and Brain appends one
outcome, `succeeded` or `failed` by the tool's own success flag; anything that
is not a real tool result counts as the shared fixed tool failure. The tool's
result is shown, followed by `Recorded as audit summary N.`, or, when the
outcome could not be recorded, by a notice that the summary exists but shows
its outcome as unknown. Nothing is retried, the tool is never rerun
automatically, and nothing about the plan is recorded as interaction history.

**What a summary means.** A summary records that an approved execution attempt
was authorised for dispatch; by itself it does not prove the tool ran. A
summary without an outcome shows `Outcome not recorded; execution is unknown.`,
because Maria may have stopped after committing it, during the tool's run, or
before the outcome could be written. An outcome is inserted only while its
summary still exists, and at most once, and because ids are never reused, no
outcome can attach to a deleted summary or to a later one.

**Audit commands.** `audit` lists the newest 20 summaries by id, reading one
more to say when more exist; `read audit <id>` shows one summary as a labelled
block; and `delete audit <id>` removes one summary together with its outcome
in one transaction. The commands are case-insensitive. `read audit` and
`delete audit` take the same positive whole-number ids as the eleven memory,
note, and interaction commands addressed by id alone; these thirteen ID-only
planner commands share one parser, while `read note <id or exact title>` uses
its separate, unchanged lookup path.
`list audits` is still a filesystem listing. `audit.list` and `audit.read` are
allowed by default, `audit.delete` always requires confirmation, even for an id
that does not exist, and none of the three is model-eligible. In interaction
history, a successful listing or reading records only `Listed audit
summaries.` or `Displayed audit summary <id>.` as its response, and every
other audit reply is short fixed text recorded as shown, so no summary's
fields reach interaction history. There is no search, filtering, range, bulk
or wildcard deletion, editing, pruning, retention policy, or undo, and
summaries never become model context.

**Privacy.** Every value is checked against the vocabulary in `core/audit.py`
when it is written and again when it is read, so a summary can never hold the
request, the proposal, an argument, a fingerprint, private context, a root's
name, a host path, a tool's result, or exception text, and a stored value
outside the vocabulary fails the whole read with a fixed message that repeats
nothing. When the memory layer fails to write or read a summary, it raises a
fixed error with no cause or context and logs at most one fixed line, never
SQLite's text, a stored value, or a traceback. On this path Brain logs only
fixed lines, the declared tool and action, and summary ids, so the operational
log may still show that a model-proposed action ran after its summary is
deleted, which the deletion's confirmation notice says.

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
permission. `core/context.py` imports no Maria module at all.
`core/reasoning.py` reaches nothing but the context module, the shared
failures, and the logger, and `core/ollama_adapter.py` nothing but those and
the reasoning interface. `core/conversation.py` reaches only those, the
logger, and the planner's plan types, never Tool Manager, permissions, the
compiler, or the memory layer. A conversational reply reaches Brain only as
the text of a zero-step transient plan, and a proposal attempt only as the
untouched `ReasoningResponse`, which Brain passes to the compiler and nowhere
else; no module passes a reply to the planner or a tool, and a compiled plan's
arguments reach a tool only after the owner's approval and a committed audit
summary.
`core/plan_compiler.py` reaches only the action contracts, the planner's plan
types, the reasoning response type, and the logger, never Tool Manager, a
tool, permissions, the memory layer, or conversation, and Brain is the only
production module that imports it. `core/audit.py` imports nothing from Maria
but the action contracts; it holds no SQL, reads and stores nothing, and logs
nothing, and the memory layer, Brain, and `tools/audit.py` take every audit
value from it.
`core/ollama_adapter.py` is the only production module that imports a network
module, and `main.py` is the only one that names or reaches it. An
import-boundary guard asserts that Brain, the planner, permissions, Tool
Manager, the memory layer, configuration, and every tool import no network or
threading module, directly or through any Maria module they import, and that
none of them reaches the adapter.

The brain has no memory-specific dispatch branch. `MemoryTool` contains no SQL;
it receives the shared `Memory` object and calls its public persistence methods.
The existing calculator, notes, filesystem, and system tool implementations are
unchanged by explicit memory support. Brain's model-plan path likewise
branches on a plan's origin, never on a tool or action name, and writes audit
summaries only through the memory layer's public methods. `AuditTool`
contains no SQL either: it reaches summaries only through the shared `Memory`
object.

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
