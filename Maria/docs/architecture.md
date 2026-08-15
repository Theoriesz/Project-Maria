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

The `MemoryTool` is a thin adapter for the `store`, `search`, and `list`
actions. `ToolManager` registers `tools.memory` through the standard tool
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
either interruption cancels the pending action without executing it. This
boundary intentionally does not add a broad exception handler around Brain
processing after startup. Confirmation still uses deterministic re-planning
with `brain.process(original_input, confirmed=True)`; binding confirmation to
an immutable plan remains deferred.

## Permission Policy

All planned actions pass through `PermissionManager`. In v0.1,
`filesystem.rename` and `filesystem.move` always require confirmation, while
`filesystem.delete` and `filesystem.write` are always denied. TOML
configuration may add restrictions but cannot remove these mandatory
protections. Missing permission arrays retain the secure baseline, and
malformed present arrays stop configuration loading instead of weakening it.
When an action is both denied and confirmation-required, denial wins.

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
  user. The notes tool uses the shared `Memory` persistence interface.
- **Explicit memories** are deliberately stored facts. They are created only
  through the strict `remember <fact>` command or the public `Memory` API,
  searched through `recall <query>`, and listed through `memories`.

On the completed paths described above, memory commands and their responses
also appear in interaction history when recording succeeds. A stored fact can
therefore exist both as an explicit memory and as part of an interaction.

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
semantic search, fuzzy matching, ranking, update, deletion, or a `forget`
command in v0.1.
