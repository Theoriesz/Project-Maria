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
8. `brain.py` formats the tool result and records the normal request and
   response as an interaction.
9. `logger.py` records important application events.

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

- **Interaction history** records general user requests and Maria responses.
  The brain retrieves recent rows and supplies them to the planner as context,
  although the strict v0.1 planner does not use that context to infer actions.
  Interaction rows are never converted automatically into explicit memories.
- **Notes** are structured title-and-body records deliberately created by the
  user. The notes tool uses the shared `Memory` persistence interface.
- **Explicit memories** are deliberately stored facts. They are created only
  through the strict `remember <fact>` command or the public `Memory` API,
  searched through `recall <query>`, and listed through `memories`.

Because the brain records normal requests and responses, memory commands and
their responses also appear in interaction history. A stored fact can therefore
exist both as an explicit memory and as part of an interaction.

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
