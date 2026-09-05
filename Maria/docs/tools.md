# Tools

Tools are small modules that expose controlled capabilities to Maria.

## Version 0.1 Built-in Tools

- `calculator`: basic arithmetic
- `filesystem`: read, list, rename, and move inside the configured safe root
- `memory`: store, search, and list explicit SQLite-backed memories
- `notes`: create, list, and read SQLite-backed notes
- `system`: read-only OS, CPU, RAM, disk, and time information

## Calculator Tool

The calculator parses expressions with Python's AST and evaluates only a strict
arithmetic subset. It supports integer and finite floating-point literals; the
binary operators `+`, `-`, `*`, `/`, `//`, `%`, and `**`; and unary `+` and `-`.
Booleans, complex numbers, names, calls, and other syntax are unsupported.

Version 0.1 limits expressions to 200 characters, absolute exponent values to
100, and integer intermediate or final values to 4,096 bits. All floating-point
intermediate and final values must be finite. Integer multiplication and power
are checked before evaluation when their result is predictably oversized, and
any value that exceeds a bound produces a controlled calculation failure.

These bounds protect version 0.1 execution; they are not arbitrary-precision
math guarantees.

## Memory Tool

The memory tool supports exactly three actions:

- `store`: store a deliberate fact
- `search`: find facts by literal, case-insensitive substring
- `list`: list the newest stored facts

Successful store messages use one of these forms:

```text
Remembered memory <id>: <fact>
Already remembered as memory <id>: <fact>
```

The duplicate form uses the original stored fact and ID. Search and list output
one memory per line as `<id>: <fact>`. Empty results are successful operations
with these messages:

```text
No matching memories found.
No memories found.
```

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.memory` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path. The tool contains no SQL; `core/memory.py` owns all database operations.

## System Tool

The system tool supports exactly two actions:

- `overview`: OS, CPU, RAM, disk, and current-time summary
- `current_time`: a dedicated, readable current-time report

Both actions are read-only. Planner command aliases:

- `system`, `system info`, `status`, `sys`, and `sys info` map to `overview`
- `time`, `current time`, and `time now` map to `current_time`

The `current_time` action's message is a readable, timezone-aware four-line
report:

```text
Date: 2030-01-02
Time: 03:04:05
UTC offset: +08:00
ISO-8601: 2030-01-02T03:04:05+08:00
```

The `overview` action's final line is a compact reading of the same fields:

```text
Current time: 2030-01-02 03:04:05 UTC+08:00
```

Both formats are derived from a single timezone-aware local datetime capture
per action call, so every value describes the same instant. The UTC offset is
always signed and colonized, including `+00:00` for UTC itself, and formatting
uses only numeric, locale-independent fields.

## Tool Interface

A tool should provide:

- `name`
- `description`
- `actions`
- `execute(action, arguments)`
- `build_tool(...)`

Tool actions should return `ToolExecutionResult`.

## Safety Rules

Tools should validate inputs and handle errors gracefully. File-modifying
actions must be checked by `permissions.py` before execution. Delete behavior is
not part of version 0.1. The memory tool provides no update, delete, or forget
action.
