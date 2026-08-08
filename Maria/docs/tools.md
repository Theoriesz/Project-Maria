# Tools

Tools are small modules that expose controlled capabilities to Maria.

## Version 0.1 Built-in Tools

- `calculator`: basic arithmetic
- `filesystem`: read, list, rename, and move inside the configured safe root
- `memory`: store, search, and list explicit SQLite-backed memories
- `notes`: create, list, and read SQLite-backed notes
- `system`: read-only OS, CPU, RAM, disk, and time information

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
