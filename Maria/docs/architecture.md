# Architecture

Maria v0.1 is organized around a small set of modules with clear ownership.

## Core Flow

1. `main.py` receives user input from the command line.
2. `brain.py` coordinates the request and retrieves recent interaction context.
3. `planner.py` converts explicit commands into structured plan steps.
4. `permissions.py` decides whether each planned action is allowed.
5. `tool_manager.py` dispatches allowed actions to registered tools.
6. Tools perform their focused work and return a `ToolExecutionResult`.
7. `memory.py` owns persistent interactions, notes, and explicit memories in
   SQLite.
8. `brain.py` forms the final response and, on recordable completion paths,
   attempts to record the cleaned request and response as an interaction.
9. `logger.py` configures Maria's named logger; application modules emit
   operational events through it.

An explicit memory request uses the same execution path as every other tool
request:

```text
CLI
-> MariaBrain
-> Planner
-> PermissionManager
-> ToolManager
-> MemoryTool
-> Memory
-> SQLite
```

The `MemoryTool` is a thin adapter for the `store`, `search`, `list`, `update`,
and `forget` actions. `ToolManager` registers `tools.memory` through the standard tool
factory contract alongside the calculator, notes, filesystem, and system
tools. Memory actions therefore pass through the normal permission and result
handling flow.

## Interaction Recording Boundary

For a normal completed tool request, interaction recording follows this
boundary:

```text
CLI
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

In the current Unreleased development state, `history.read` is the only
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
If confirmation is cancelled, the CLI-only cancellation response is not
recorded. If it is approved, the CLI calls `process()` again with
`confirmed=True`; Brain retrieves context, re-plans, rechecks permission, and
attempts to record only the second call's completed result through the normal
path.

Post-startup processing has no broad exception handler. If an exception
propagates before a recordable branch calls `Memory.record_interaction()`, no
interaction write is attempted.

For each non-empty `process()` call, Brain attempts to retrieve up to three
recent interaction rows and supplies them to Planner as context. A retrieval
`RuntimeError` is logged and produces empty context instead. The v0.1 Planner
currently accepts and discards that context, so retained history does not alter
plans. The retrieval limit controls query results; it is not a retention or
deletion policy.

Operational logging and interaction persistence are separate boundaries.
`logger.py` configures Maria's namespaced file logger; default startup targets
`logs/maria.log` for application events, warnings, and failures.
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
around Brain processing after startup. Confirmation still uses deterministic
re-planning with `brain.process(original_input, confirmed=True)`; binding
confirmation to an immutable plan remains deferred.

## Permission Policy

All planned actions pass through `PermissionManager`. In v0.1,
`filesystem.rename` and `filesystem.move` always require confirmation, while
`filesystem.delete` and `filesystem.write` are always denied. TOML
configuration may add restrictions but cannot remove these mandatory
protections. Missing permission arrays retain the secure baseline, and
malformed present arrays stop configuration loading instead of weakening it.
When an action is both denied and confirmation-required, denial wins.

In the current Unreleased development state, the mandatory confirmation
baseline is eight actions: `filesystem.rename`, `filesystem.move`,
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

## Persistence Models

Maria keeps three kinds of persistent data separate:

- **Interaction history** records cleaned requests and final Maria responses
  on the completed paths described above. The brain retrieves recent rows and
  supplies them to the planner as context, although the strict v0.1 planner
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

## Version 0.1 Scope

This foundation intentionally excludes autonomous agents, voice, browser
automation, internet search, and user interfaces beyond the command line. Future
versions can add richer capabilities by extending the planner, tool manager, and
tool set without rewriting the foundation.

Explicit memory remains intentionally small and strict. Maria does not infer
facts from conversation, learn in the background, or provide embeddings,
semantic search, fuzzy matching, or ranking. In the current Unreleased
development state a memory's fact can be replaced and a memory can be forgotten
by numeric ID only, through `update memory <id>: <new fact>` and `forget memory
<id>`; there is no selection by fact text, no bulk or clear-all deletion, and
no undo or revision history.
