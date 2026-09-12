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
`get_note`, `retitle_note`, `edit_note`, and `delete_note` instead of writing
SQL directly.

Every connection uses an explicit 5.0-second SQLite busy timeout
(`DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS`), which governs SQLite's built-in
busy waiting for `SQLITE_BUSY` contention. When statement execution or commit
ultimately raises a SQLite error whose numeric primary result code is
`SQLITE_BUSY` or `SQLITE_LOCKED` (including extended variants of either,
classified by that numeric code, not by matching exception text), every
method below raises `RuntimeError("Database is busy. Try again shortly.")`
instead of its listed operation-specific message; `SQLITE_LOCKED` is
classified the same way but is not guaranteed to use or exhaust the
busy-timeout window. A failure while opening the connection itself is not
covered by this fixed message and continues through that method's existing
operation-specific static `RuntimeError` message. Every other SQLite failure
likewise continues to raise that method's existing operation-specific
`RuntimeError` message, unchanged.

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
rows. `Memory.delete_interaction`, documented below, targets at most one row
by ID and removes it when found; Maria still exposes no prune or bulk-delete
API method or CLI command.
Brain requests up to three rows for context; retrieval failure falls
back to empty context, while recording failure is logged without replacing
the user response.

`Memory.get_interaction(interaction_id: int) -> dict[str, Any] | None`

Returns one interaction by ID with exactly `id`, `user_input`, `response`,
and `created_at`, or `None` when the ID does not exist. SQLite failures raise
`RuntimeError("Unable to read interaction.")`.

`Memory.search_interactions(query: str, limit: int = 20) -> list[dict[str, Any]]`

Strips the query and raises `ValueError("Search query is required.")` when it
is empty. Matching uses SQLite's built-in `LOWER()` for ASCII
case-insensitive comparison — not full Unicode casefold equivalence, unlike
explicit-memory search — together with literal `instr()` substring matching
against both `user_input` and `response`; percent (`%`) and underscore (`_`)
are literal characters, not wildcards. Results contain `id`, `user_input`,
`response`, and `created_at`, ordered by `created_at` descending and then
`id` descending; no match returns an empty list. Requested integer limits are
clamped to the same 1-through-50 range as `get_recent_interactions`. SQLite
failures raise `RuntimeError("Unable to search interactions.")`.

`Memory.delete_interaction(interaction_id: int) -> bool`

Validates `interaction_id` directly, before opening a connection: raises
`ValueError("Interaction id must be a positive whole number.")` when it is
not an `int`, is a `bool`, or is less than 1. For a valid ID, executes
one parameterized `DELETE FROM interactions WHERE id = ?` statement without
first reading the row, and returns `True` when exactly one row was deleted or
`False` when no row matched. Deleted content is never read, logged, or
returned. SQLite failures raise `RuntimeError("Unable to delete
interaction.")`.

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

### Notes

`Memory.retitle_note(note_id: int, new_title: str) -> bool`

Validates both arguments directly, before generating a timestamp or opening a
connection: raises `ValueError("Note id must be a positive whole number.")`
when `note_id` is not an `int`, is a `bool`, or is less than 1, and
`ValueError("New note title is required.")` when `new_title` is not a `str` or
is blank after stripping. Non-string values are rejected rather than coerced.
For valid arguments, executes one parameterized `UPDATE notes SET title = ?,
updated_at = ? WHERE id = ?` statement without first reading the row, writing
only the stripped title and a single current UTC timestamp. `body` and
`created_at` are untouched. Returns `True` when exactly one row was updated or
`False` when no row matched. A title identical to the stored one still returns
`True` and still refreshes `updated_at`, because the row matched; duplicate
titles across notes remain allowed. `updated_at` receives the current
timestamp with no guarantee that it is strictly later than the previous value.
SQLite failures raise `RuntimeError("Unable to retitle note.")`, and
busy/locked contention raises `RuntimeError("Database is busy. Try again
shortly.")`.

`Memory.edit_note(note_id: int, new_body: str) -> bool`

Behaves identically to `retitle_note`, except that it validates `new_body`
with `ValueError("New note body is required.")`, executes `UPDATE notes SET
body = ?, updated_at = ? WHERE id = ?`, leaves `title` and `created_at`
untouched, and raises `RuntimeError("Unable to edit note.")` on SQLite
failure.

`Memory.delete_note(note_id: int) -> bool`

Validates `note_id` the same way, before opening a connection. For a valid ID,
executes one parameterized `DELETE FROM notes WHERE id = ?` statement without
first reading the row, and returns `True` when exactly one row was deleted or
`False` when no row matched. Deleted title and body text is never read,
logged, or returned. Deletion does not cascade to `interactions` or
`memories` and does not renumber remaining note IDs. SQLite failures raise
`RuntimeError("Unable to delete note.")`.

All three log only the numeric note ID on success, after their transaction
commits, and log nothing extra for a benign not-found result. Note titles and
bodies never appear in log messages.

## Tool Execution Result

`ToolExecutionResult(success: bool, message: str, data: Any = None, recorded_message: str | None = None)`

`message` is the text Brain displays to the user. `recorded_message` is an
optional override for what Brain persists to interaction history instead. It
defaults to `None`, and any result that does not opt in leaves it as `None`;
`None` makes Brain record `message` unchanged. An explicit empty string is
honored and persisted as an empty string rather than falling back to
`message`. Brain applies this generically and never branches on a tool's name
or action.

In the current Unreleased development state, `history.read` is the only
result that supplies a non-`None` `recorded_message`. Every other current
tool result leaves the field as `None` and therefore retains the existing
message-as-recorded behavior.

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

## History Tool

`HistoryTool.execute(action: str, arguments: dict[str, Any]) -> ToolExecutionResult`

The history tool supports `list`, `search`, `read`, and `delete`. `list` and
`search` return at most the newest 20 rows, using the same limit-plus-one
probing pattern as notes and memories; their `data` is a redacted list of
`{"id", "user_input", "created_at"}` dictionaries that excludes `response`,
even when `search` matched inside a row's `response` text. `read` returns
the complete interaction row (including `response`) as `data`, since its
`message` already displays the full stored request and response.
Non-printable characters in a `list`/`search` display row are replaced with
a single space; the underlying stored data and `read`'s full display are
unaffected. `read` sets `recorded_message` to a fixed
`Displayed interaction <id>.` string so redisplaying old content does not
duplicate it into a new interaction row.

`delete` validates its `id` argument the same way as `read` (an actual
`int`, not a `bool`, at least `1`), returning `"Interaction id must be a
positive whole number."` for an invalid direct value without calling
`Memory`. A missing row returns `"Interaction not found: <id>."`. A
successful deletion returns `"Deleted interaction <id>."` with
`data={"id": <id>}` — never the deleted request or response text — and
leaves `recorded_message` as `None`, since that message is already short
and content-free. An ordinary `Memory` failure is wrapped as `"History
deletion failed: Unable to delete interaction."`.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.history` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `history.list`, `history.search`, and `history.read` are allowed by
default, while `history.delete` always requires explicit confirmation. The
tool contains no SQL; `core/memory.py` owns all database operations.

## Notes Tool

`NotesTool.execute(action: str, arguments: dict[str, Any]) -> ToolExecutionResult`

The notes tool supports `create`, `list`, `read`, `retitle`, `edit`, and
`delete`. `create`, `list`, and `read` are unchanged.

`retitle`, `edit`, and `delete` each read an `id` argument and validate it the
same way as the history tool (an actual `int`, not a `bool`, at least `1`),
returning `"Note id must be a positive whole number."` for an invalid direct
value without calling `Memory`. `retitle` additionally reads a `title`
argument and `edit` a `body` argument, each of which must be an actual `str`
that is non-blank after stripping; otherwise they return `"New note title is
required."` or `"New note body is required."`, again without calling `Memory`.
Values are never coerced with `str(...)`, so `None`, numbers, booleans, and
arbitrary objects can never be persisted as note text. Only the stripped value
is passed to `Memory`.

A missing row returns `"Note not found: <id>."`. Success returns `"Retitled
note <id>."`, `"Edited note <id>."`, or `"Deleted note <id>."` with
`data={"id": <id>}` — never the note's title or body — and leaves
`recorded_message` as `None`, since those messages are already short and
content-free. An ordinary `Memory` failure is wrapped as `"Note retitle
failed: Unable to retitle note."`, `"Note edit failed: Unable to edit
note."`, or `"Note deletion failed: Unable to delete note."`; busy/locked
contention is wrapped the same way, for example `"Note deletion failed:
Database is busy. Try again shortly."`.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.notes` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `notes.create`, `notes.list`, and `notes.read` are allowed by default,
while `notes.retitle`, `notes.edit`, and `notes.delete` always require
explicit confirmation. The tool contains no SQL; `core/memory.py` owns all
database operations.

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
baseline: `filesystem.rename`, `filesystem.move`, `history.delete`,
`notes.retitle`, `notes.edit`, and `notes.delete` require
confirmation, while `filesystem.delete` and `filesystem.write` are denied.
Configuration can add restrictions but cannot remove this baseline.
`PermissionManager` applies the same mandatory policy when constructed
directly, and denial wins if an action also requires confirmation.
