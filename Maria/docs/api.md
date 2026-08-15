# Internal API

Maria v0.1 exposes internal Python interfaces between modules. These are not
public network APIs.

## Brain

`MariaBrain.process(user_input: str, confirmed: bool = False) -> BrainResponse`

Coordinates a request from input to plan, permission check, tool execution, and
interaction-history recording.

## Planner

`Planner.create_plan(user_input: str, context: dict | None = None) -> Plan`

Returns a `Plan` containing zero or more `PlanStep` objects.

## Memory

The memory layer owns all SQLite access. Other modules should call methods such
as `record_interaction`, `get_recent_interactions`, `create_note`, `list_notes`,
and `get_note` instead of writing SQL directly.

### Interaction History

`Memory.record_interaction(user_input: str, response: str) -> int`

Stores the supplied request and response strings as full text, adds a UTC ISO
8601 `created_at` timestamp, and returns the inserted interaction ID. The method
does not strip, summarize, or validate its inputs as non-empty; Brain calls it
with the stripped request and final response text on recorded paths. SQLite
failures raise `RuntimeError("Unable to record interaction.")`.

Passing complete request and response text means tool output such as file or
note contents can become persistent interaction history. Callers must not treat
these rows as facts deliberately saved to explicit memory.

`Memory.get_recent_interactions(limit: int = 5) -> list[dict[str, Any]]`

Returns interaction dictionaries containing exactly `id`, `user_input`,
`response`, and `created_at`. Rows are ordered by `created_at` descending and
then `id` descending; an empty table returns an empty list. Requested integer
limits are clamped to a minimum of 1 and maximum of 50. SQLite failures raise
`RuntimeError("Unable to fetch recent interactions.")`.

"Recent" limits the query result and does not delete, expire, or prune older
rows. Maria v0.1 exposes no interaction-history delete or prune API method or
CLI command. Brain requests up to three rows for context; retrieval failure
falls back to empty context, while recording failure is logged without
replacing the user response.

### Explicit Memories

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
Decisions follow denied, confirmation-required, then allowed precedence.

`load_config` requires a present `[permissions]` value to be a TOML table and
validates `require_confirmation` and `deny` as arrays of lowercase
`tool.action` identifiers. Each identifier contains exactly one period, and
its tool and action segments start with a letter and otherwise contain only
lowercase ASCII letters, digits, or underscores. Duplicate identifiers within
one array are rejected, while syntactically valid identifiers for future tools
are retained. Malformed permission values raise `ValueError`.

Missing permission arrays and explicitly empty arrays use the mandatory secure
baseline: `filesystem.rename` and `filesystem.move` require confirmation, while
`filesystem.delete` and `filesystem.write` are denied. Configuration can add
restrictions but cannot remove this baseline. `PermissionManager` applies the
same mandatory policy when constructed directly, and denial wins if an action
also requires confirmation.
