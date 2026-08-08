# Internal API

Maria v0.1 exposes internal Python interfaces between modules. These are not
public network APIs.

## Brain

`MariaBrain.process(user_input: str, confirmed: bool = False) -> BrainResponse`

Coordinates a request from input to plan, permission check, tool execution, and
memory recording.

## Planner

`Planner.create_plan(user_input: str, context: dict | None = None) -> Plan`

Returns a `Plan` containing zero or more `PlanStep` objects.

## Memory

The memory layer owns all SQLite access. Other modules should call methods such
as `record_interaction`, `get_recent_interactions`, `create_note`, `list_notes`,
and `get_note` instead of writing SQL directly.

`Memory.store_memory(fact: str) -> tuple[int, bool]`

Strips the fact and raises `ValueError` when it is empty. A new fact returns
`(memory_id, True)`. A case-insensitive exact duplicate returns the existing ID
as `(memory_id, False)` without changing the original fact or timestamp. SQLite
failures raise `RuntimeError("Unable to store memory.")`.

`Memory.get_memory(memory_id: int) -> dict[str, Any] | None`

Returns one explicit memory by ID with exactly `id`, `fact`, and `created_at`,
or `None` when the ID does not exist. SQLite failures raise
`RuntimeError("Unable to read memory.")`. This is an internal persistence
method, not a CLI command.

`Memory.search_memories(query: str, limit: int = 20) -> list[dict[str, Any]]`

Strips and validates the query, raising `ValueError` when it is empty. Results
use `casefold()` normalization and literal SQLite `instr()` substring matching.
They contain `id`, `fact`, and `created_at`, ordered by newest timestamp and
then descending ID; no match returns an empty list. SQLite failures raise
`RuntimeError("Unable to search memories.")`.

`Memory.list_memories(limit: int = 20) -> list[dict[str, Any]]`

Returns explicit memories with the same `id`, `fact`, and `created_at` shape.
Rows are ordered by newest timestamp and then descending ID. An empty table
returns an empty list. SQLite failures raise
`RuntimeError("Unable to list memories.")`.

Search and list default to 20 results. Direct API limits are clamped to a
minimum of 1 and a maximum of 100.

## Memory Tool

`MemoryTool.execute(action: str, arguments: dict[str, Any]) -> ToolExecutionResult`

The memory tool supports `store`, `search`, and `list`. Successful stores return
data shaped as `{"id": int, "fact": str, "created": bool}`. For a duplicate,
`fact` is the original stored display text. Successful searches and lists
return a list of memory dictionaries as `data`; no match or an empty table is a
successful result with `data=[]`.

Expected validation and persistence failures are converted into unsuccessful
`ToolExecutionResult` values. The tool calls the shared `Memory` interface and
does not execute SQL.

## Tool Manager

`ToolManager.execute_tool(tool_name, action, arguments) -> ToolExecutionResult`

Tools must expose `name`, `description`, `actions`, and an `execute` method.

## Permissions

`PermissionManager.check(tool_name, action) -> PermissionResult`

Permission results are one of `allowed`, `requires_confirmation`, or `denied`.
