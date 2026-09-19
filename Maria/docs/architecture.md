# Architecture

Maria is organized around a small set of modules with clear ownership.

## Core Flow

1. `main.py` receives user input from the command line.
2. `session.py` holds the owner's confirmation state and forwards the request.
3. `brain.py` coordinates the request and retrieves recent interaction context.
4. `planner.py` converts explicit commands into structured plan steps.
5. `permissions.py` decides whether each planned action is allowed. Brain
   resolves every step of a plan before any step is dispatched.
6. `tool_manager.py` dispatches allowed actions to registered tools.
7. Tools perform their focused work and return a `ToolExecutionResult`.
8. `memory.py` owns persistent interactions, notes, and explicit memories in
   SQLite.
9. `brain.py` forms the final response and, on recordable completion paths,
   attempts to record the cleaned request and response as an interaction.
10. `logger.py` configures Maria's named logger; application modules emit
    operational events through it.

An explicit memory request uses the same execution path as every other tool
request:

```text
CLI
-> MariaSession
-> MariaBrain
-> Planner
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
-> PermissionManager
-> ToolManager
-> Tool
-> Brain final response
-> Memory.record_interaction
-> SQLite interactions table
```

`MariaBrain.process()` strips non-empty input before planning. On the zero-step,
permission-denial, and post-tool-result branches, Brain passes that cleaned
request and the full final response text to `Memory.record_interaction()`. This
includes help, unsupported commands, malformed-command usage responses,
denials, successful tool results, and returned unsuccessful
`ToolExecutionResult` values. The recording layer stores the supplied text
rather than summaries or only metadata, subject to existing command and tool
output limits. A recording `RuntimeError` is logged without replacing the
primary response, so recording is attempted but a row is not guaranteed when
the database write fails.

`ToolExecutionResult` carries an optional `recorded_message` field. It
defaults to `None`, and any result that does not opt in leaves it as `None`;
`None` makes Brain record the same text it displays, exactly as before. A
tool may instead set `recorded_message` to persist different text than the
displayed `message` for that step; an explicit empty string is honored rather
than falling back to `message`. Brain applies this generically, reading only
`.message` and `.recorded_message`, with no branch on a tool's name or
action.

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
propagates with the previous configuration intact.
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
remains an internal trusted-code boundary.

### Whole-Plan Resolution Before Execution

Executing a plan is two separate phases, and no tool is dispatched while the
first is incomplete.

Resolution comes first and covers the complete plan. Brain walks the steps in
their original order, calls `PermissionManager.check()` once per step, and
keeps each step paired with the result it got back. Nothing executes during
that pass, the plan is neither rebuilt nor mutated, no step's arguments are
copied into a mutable dictionary, and no planner or context lookup happens.

Denial takes precedence over the whole plan. If any step resolves to denied,
the plan is denied, no tool runs, no confirmation becomes pending, and the
earliest denied step in plan order supplies the reason. An earlier allowed step
does not run first, and an earlier confirmation-required step does not prompt.
Confirmation is offered only once resolution has found no denial anywhere.

Approving a plan does not freeze its decisions. Confirmed execution resolves
the same immutable plan's permissions again before dispatching the first tool,
which is an execution-time safety recheck rather than replanning. If that
recheck now finds a denial, nothing executes, the denial is recorded through
the normal path, and the approval the session already consumed stays spent.

Dispatch is the second phase. Steps run in plan order, at most once each, and
each receives the immutable arguments object its `PlanStep` holds. The first
unsuccessful `ToolExecutionResult` stops the plan: later steps do not run, and
Maria neither rolls back nor compensates for the steps that already succeeded.
There is no transaction, undo, or workflow persistence across steps, and the
displayed and recorded text covers exactly the prefix of steps that ran.

The resolved plan is transient execution state. It is private to `brain.py`,
frozen, never persisted, never logged, and never handed to another module, and
it reports which decisions came back without making any of them.
`PermissionManager` itself is unchanged: no new arguments, no bulk-check
method, no altered precedence, and no permission policy in Brain, the session,
`main.py`, Tool Manager, or a tool.

Production plans still contain at most one step, because the planner emits at
most one. These guarantees therefore protect plans a later stage may produce;
they change no command available today. A single-step confirmation keeps its
existing wording, while a longer plan is rendered as a deterministic,
plan-ordered list of each step's tool action and resolved permission outcome.
That rendering repeats no argument value, so it exposes no absolute host path,
no fingerprint, and no canonical plan content; it is not logged, and a pending
confirmation is not written to interaction history.

## Persistence Models

Maria keeps three kinds of persistent data separate:

- **Interaction history** records cleaned requests and final Maria responses
  on the completed paths described above. The brain retrieves recent rows and
  supplies them to the planner as context, although the strict planner
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

## Boundaries

The brain does not run SQL or perform tool work directly. The planner does not
execute tools. Tools do not decide global permissions. The memory module is the
only module that opens SQLite connections or executes SQL.

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
