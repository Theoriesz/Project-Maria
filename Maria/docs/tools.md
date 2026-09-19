# Tools

Tools are small modules that expose controlled capabilities to Maria.

## Declared Argument Contracts

Every built-in tool action is declared once in `core/action_contracts.py`,
which is the positive allowlist of the 22 supported tool actions and of the
arguments each one accepts. A tool's `actions` set answers only whether an
action is registered; the contract answers which arguments it takes, whether
each is required, its exact type, any numeric bound, and, for a path field, the
named root class it belongs to. The registry is hand-written and immutable, is
never derived from the loaded tools, and a test proves the declared set and the
loaded set match in both directions.

Validation runs before `PermissionManager` for a planned request, and again
inside `ToolManager.execute_tool()` for any direct call, so neither route can
reach a tool with an unsupported action or a malformed argument. It rejects
rather than repairs: no value is converted, parsed, truncated, clamped,
defaulted, or normalized, and `bool` is not accepted where an integer is
required. Unknown tool, unknown action, missing required argument, extra
argument, wrong type, and out-of-range value each produce a distinct fixed
refusal naming the tool, action, and field only, never the submitted value.

Tools keep their own argument checks as defence in depth, so a malformed value
passed straight to a tool object is refused there too rather than converted.
An absent optional argument still receives its existing tool-owned default:
`filesystem.list` defaults an absent `path` to the safe root, `notes.create`
defaults an absent `title` to `Untitled`, and the listing actions default an
absent `limit` to 20. A value that is *provided* is never repaired into a
default. Every text field declares that blank or whitespace-only content is
malformed, so a provided blank title, path, fact, query, body, or target is
refused rather than replaced or defaulted. `calculator.calculate.expression`
additionally declares the established 200-character maximum, measured on the
stripped text. A provided `limit` must be an actual `int` between 1 and 100;
values outside that window are refused rather than clamped. A path field's root
class is classification metadata only; the registry resolves no path and
touches no filesystem, and `FilesystemTool` remains solely responsible for
canonical resolution, safe-root containment, symlink handling, and collisions.

`core/memory.py` still clamps a listing limit internally, but that clamp is no
longer the acceptance policy: it sits below the Brain, Tool Manager, and tool
gates as defence in depth and is unchanged.

## Built-in Tools

- `calculator`: basic arithmetic
- `filesystem`: read, list, rename, and move inside the configured safe root
- `history`: list, search, read, and delete SQLite-backed interaction-history
  rows
- `memory`: store, search, list, update, and forget explicit SQLite-backed
  memories
- `notes`: create, list, read, retitle, edit, and delete SQLite-backed notes
- `system`: read-only OS, CPU, RAM, disk, and time information

## Calculator Tool

The calculator parses expressions with Python's AST and evaluates only a strict
arithmetic subset. It supports integer and finite floating-point literals; the
binary operators `+`, `-`, `*`, `/`, `//`, `%`, and `**`; and unary `+` and `-`.
Booleans, complex numbers, names, calls, and other syntax are unsupported.

Maria limits expressions to 200 characters, absolute exponent values to
100, and integer intermediate or final values to 4,096 bits. All floating-point
intermediate and final values must be finite. Integer multiplication and power
are checked before evaluation when their result is predictably oversized, and
any value that exceeds a bound produces a controlled calculation failure.

These bounds protect calculator execution; they are not arbitrary-precision
math guarantees.

## Filesystem Tool

The filesystem tool supports `read`, `list`, `rename`, and `move` inside the configured
safe root. Rename and move always require explicit confirmation.

Every path operand must be an actual `str`. A value of any other type is refused
with `Path must be text.` rather than being rendered into a path, so a malformed
direct call can no longer become a real filesystem target. `list` still defaults
an absent `path` to the safe root.

Internally the tool still resolves every operand to a canonical absolute path, and all
containment validation, symlink resolution, and actual filesystem work happen on those
absolute paths. Only the user-facing message text is rendered differently.

User-facing messages show paths relative to the configured safe root, always using
forward slashes so output is identical on every platform. The safe root itself displays
as `.`:

```text
Not a file: archive/processed/Customer Order.txt
Not a directory: main.py
Source does not exist: inbox/missing.txt
Destination already exists: archive/report.txt
Renamed inbox/old name.txt to archive/new name.txt.
Moved inbox/Customer Order.txt to archive/processed/Customer Order.txt.
```

Displayed paths are derived after resolution, so redundant `.` and `..` components are
removed and an absolute operand inside the safe root is still shown relative to it.
Spaces, Unicode, casing, and quote characters are preserved as literal path characters;
the tool performs no shell or quote parsing.

A path that resolves outside the safe root is rejected with one generic message that
never names the rejected path:

```text
Path is outside Maria's configured safe root.
```

Failures are reported with a sanitized reason. Raw `OSError` and `shutil.Error` text is
never copied into a response, and neither are the `filename`, `filename2`, `strerror`, or
`args` fields those exceptions carry. A reason is chosen as follows:

- For an `OSError`, the reason comes from `os.strerror(exc.errno)`.
- For a `ValueError`, only these two fixed Maria-controlled messages are preserved:

  ```text
  Path is required.
  Path is outside Maria's configured safe root.
  ```

- Every other `ValueError`, and any failure without a usable numeric error code —
  including `shutil.Error` — uses the fixed fallback:

  ```text
  The operating system reported an unspecified error.
  ```

So a response carries one of these forms:

```text
File read failed: Permission denied
File read failed: Path is outside Maria's configured safe root.
Move failed: The operating system reported an unspecified error.
```

Path-bearing tool result `data` and the operational log are unchanged and still contain
canonical absolute paths and full raw exception diagnostics, which is where the
per-filename detail omitted from responses remains available.

Two limits are worth stating plainly. Recorded request text is not sanitized, so an
absolute path a user typed explicitly can still be retained in interaction history. The
change is also prospective only: existing interaction-history rows are not rewritten.

## History Tool

The history tool supports exactly four actions:

- `list`: list the newest interaction-history rows
- `search`: find interactions by literal, case-insensitive substring across
  the stored request and response
- `read`: display one interaction's complete stored request, response, and
  timestamp
- `delete`: target at most one interaction row by ID and remove it when found

`list` and `search` output one row per line as `<id>: <user_input>`, newest
first, using the same newest-20-with-more-exists convention as notes and
memories:

```text
Showing the newest 20 interactions. More interactions exist.
Showing the newest 20 matches. More matching interactions exist.
```

Empty results are successful operations with these messages:

```text
No interactions found.
No matching interactions found.
```

`search` may match text found only in a row's `response`, but `list` and
`search` results never expose `response` in their returned `data`, which
contains only `id`, `user_input`, and `created_at` for each visible row. Any
character in a displayed `list`/`search` row for which
`character.isprintable()` is `False` (including newlines, tabs, carriage
returns, and other control characters) is replaced with a single ordinary
space; this affects only that row's display text, never the stored row,
never `data`, and never `read`'s full display.

`read` displays the complete stored request and response, unmodified:

```text
Interaction <id>
Created at: <created_at>
Request:
<user_input>
Response:
<response>
```

and returns the complete row (including `response`) as `data`. A missing ID
returns `Interaction not found: <id>.`. Rather than recording that full
historical content again, `read` sets `ToolExecutionResult.recorded_message`
to a fixed `Displayed interaction <id>.` string, so Brain persists that short
summary instead of the redisplayed content.

`delete` validates its `id` argument the same way as `read` (an actual
`int`, not a `bool`, at least `1`), returning `Interaction id must be a
positive whole number.` for an invalid direct value without calling
`Memory`. It never reads or previews the row first. A missing ID returns
`Interaction not found: <id>.`. A successful deletion returns `Deleted
interaction <id>.` with `data={"id": <id>}` — never the deleted request or
response text — and leaves `recorded_message` unset (`None`), since that
message is already short and content-free, unlike `read`'s full-content
display. An ordinary `Memory` failure is wrapped as `History deletion
failed: Unable to delete interaction.`; a busy/locked failure is wrapped the
same way, as `History deletion failed: Database is busy. Try again
shortly.`. `delete` never cascades to `notes` or `memories` and never
performs bulk, wildcard, or range deletion.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.history` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `history.list`, `history.search`, and `history.read` are allowed by
default, while `history.delete` always requires explicit confirmation. The
tool contains no SQL; `core/memory.py` owns all database operations.

## Notes Tool

The notes tool supports `create`, `list`, `read`, `retitle`, `edit`, and
`delete`. `list` returns notes newest
first (by `updated_at`, then descending `id`) and shows at most the newest 20 by
default. When exactly 20 or fewer notes exist, the output is unchanged. When more
than 20 notes exist, only the newest 20 are shown and one line is appended:

```text
Showing the newest 20 notes. More notes exist.
```

An explicit non-default `limit` argument passed to the tool is honored as before
and never produces this suffix. It must be an actual `int`, not a `bool`, between
`1` and `100`; any other value returns `Notes limit must be a number.` or `Notes
limit must be between 1 and 100.` rather than being converted or clamped. Empty
results remain `No notes found.`.

`retitle`, `edit`, and `delete` each validate an `id` argument the same way as
the history tool (an actual `int`, not a `bool`, at least `1`), returning `Note
id must be a positive whole number.` for an invalid direct value without
calling `Memory`. `retitle` additionally requires a `title` argument and `edit`
a `body` argument that is an actual `str` and non-blank after stripping;
anything else returns `New note title is required.` or `New note body is
required.` without calling `Memory`. Non-string values are rejected rather than
coerced, so `None`, numbers, booleans, and arbitrary objects can never become
persisted note text. Only the stripped value reaches `Memory`.

None of the three read or preview the row first. A missing ID returns `Note not
found: <id>.`. Success returns `Retitled note <id>.`, `Edited note <id>.`, or
`Deleted note <id>.` with `data={"id": <id>}` — never the note's title or body —
and leaves `recorded_message` unset (`None`), since those messages are already
short and content-free. `retitle` writes only the title, `edit` writes only the
body, and both refresh `updated_at` with the current UTC timestamp while
preserving `created_at`; a value identical to the stored one still counts as a
successful update and still refreshes `updated_at`. Duplicate note titles remain
allowed. An ordinary `Memory` failure is wrapped as `Note retitle failed: Unable
to retitle note.`, `Note edit failed: Unable to edit note.`, or `Note deletion
failed: Unable to delete note.`; a busy/locked failure is wrapped the same way,
for example `Note deletion failed: Database is busy. Try again shortly.`. None
of the three cascade to `interactions` or `memories`, renumber remaining note
IDs, or perform bulk, wildcard, or range mutation.

Successful operations log only the numeric note ID and fixed wording, such as
`Deleted note <id> through notes tool.`; note titles and bodies never appear in
operational log messages, result data, or mutation response text.

The tool receives the shared `Memory` instance through dependency injection. It
is registered as `tools.notes` through Tool Manager's standard `build_tool(...)`
factory contract and travels through the normal permission path; `notes.create`,
`notes.list`, and `notes.read` are allowed by default, while `notes.retitle`,
`notes.edit`, and `notes.delete` always require explicit confirmation. The tool
contains no SQL; `core/memory.py` owns all database operations.

## Memory Tool

The memory tool supports exactly five actions:

- `store`: store a deliberate fact
- `search`: find facts by literal, case-insensitive substring
- `list`: list the newest stored facts
- `update`: replace one memory's fact by numeric ID
- `forget`: remove one memory by numeric ID

Successful store messages use one of these forms:

```text
Remembered memory <id>: <fact>
Already remembered as memory <id>: <fact>
```

The duplicate form uses the original stored fact and ID. Search and list output
one memory per line as `<id>: <fact>`, newest first. Both show at most the newest
20 results by default; exactly 20 or fewer results leave the output unchanged.
When more than 20 results exist, only the newest 20 are shown and one line is
appended:

```text
Showing the newest 20 memories. More memories exist.
Showing the newest 20 matches. More matching memories exist.
```

`list` appends the first form; `search` appends the second. An explicit
non-default `limit` argument is honored as before and never produces either
suffix. It must be an actual `int`, not a `bool`, between `1` and `100`; any
other value returns `Memories limit must be a number.`, `Memory search limit
must be a number.`, or the matching `must be between 1 and 100.` message rather
than being converted or clamped. Empty results are successful operations with these messages:

```text
No matching memories found.
No memories found.
```

`update` and `forget` each read an `id` argument and validate it directly (an
actual `int`, not a `bool`, at least `1`), returning `Memory id must be a
positive whole number.` for an invalid direct value without calling `Memory`.
`update` additionally reads a `fact` argument, which must be an actual `str`
that is non-blank after stripping; otherwise it returns `New memory fact is
required.`, again without calling `Memory`. That value is never coerced with
`str(...)`, so `None`, numbers, booleans, and arbitrary objects can never be
persisted as memory text, and only the stripped value reaches `Memory`. The
`store` action now rejects a non-`str` `fact` the same way, returning `Memory
fact must be text.` instead of converting it.

A missing row returns `Memory not found: <id>.`. Success returns `Updated
memory <id>.` or `Forgot memory <id>.` with `data={"id": <id>}` — never the old
or new fact — and leaves `recorded_message` unset (`None`), since those messages
are already short and content-free. `update` writes only `fact` and
`normalized_fact` and preserves `created_at`; because no `updated_at` exists, an
updated memory keeps its place in newest-first output. A fact identical to the
stored one, or differing only by casing, still counts as a successful update.

Because memory facts stay unique over `casefold()`, an update whose new fact
already belongs to a different memory returns the controlled failure `Memory
<id> conflicts with existing memory <other id>.` — numeric IDs only, never
either memory's text — and changes, merges, and removes nothing. Unsuccessful
results, including this one, carry no `data`. An ordinary `Memory` failure is
wrapped as `Memory update failed: Unable to update memory.` or `Memory forget
failed: Unable to forget memory.`; a busy/locked failure is wrapped the same
way, for example `Memory forget failed: Database is busy. Try again shortly.`.
Neither action cascades to `interactions` or `notes`, renumbers remaining
memory IDs, or performs bulk, wildcard, or range mutation.

Successful mutations log only the numeric memory ID and fixed wording, such as
`Forgot memory <id> through memory tool.`; benign not-found and conflict
outcomes are not logged. Memory fact text and `normalized_fact` never appear in
operational log messages, mutation result data, or mutation response text.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.memory` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `memory.store`, `memory.search`, and `memory.list` are allowed by
default, while `memory.update` and `memory.forget` always require explicit
confirmation. The tool contains no SQL; `core/memory.py` owns all database
operations.

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

Tools should validate inputs and handle errors gracefully. Every action's
arguments are validated against its declared contract before `permissions.py`
is consulted, and tools retain their own checks as defence in depth.
File-modifying actions must be checked by `permissions.py` before execution. Filesystem
delete behavior remains unimplemented. Interaction-history
deletion (`history.delete`) is checked by `permissions.py` and always
requires explicit confirmation before a row is removed. Note retitling,
editing, and deletion (`notes.retitle`, `notes.edit`, `notes.delete`) are
checked by `permissions.py` the same way and always require explicit
confirmation before a note row is changed or removed. Explicit-memory updating
and forgetting (`memory.update`, `memory.forget`) are checked the same way and
always require explicit confirmation before a memory row is changed or removed;
the memory tool provides no bulk or clear-all action, and no way to select a
memory by its fact text.
