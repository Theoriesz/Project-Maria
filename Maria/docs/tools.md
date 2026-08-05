# Tools

Tools are small modules that expose controlled capabilities to Maria.

## Version 0.1 Built-in Tools

- `calculator`: basic arithmetic
- `filesystem`: read, list, rename, and move inside the configured safe root
- `notes`: create, list, and read SQLite-backed notes
- `system`: read-only OS, CPU, RAM, disk, and time information

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
not part of version 0.1.
