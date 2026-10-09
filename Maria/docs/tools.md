# Tools

Tools are small modules that expose controlled capabilities to Maria.

## Declared Argument Contracts

Every built-in tool action is declared once in `core/action_contracts.py`,
which is the positive allowlist of the 65 supported tool actions and of the
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
`filesystem.list` defaults an absent `path` to its governing root, `notes.create`
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

Some text fields are closed. A task or reminder listing's `status` allows only
`open` and `completed`, and a reminder's `repeat` only `never`. A task's due
date is `ISO_DATE`, or `ISO_DATE_OR_NONE` for a reschedule; a reminder's
`when` is `ZONED_LOCAL_TIME`, a canonical
`YYYY-MM-DDTHH:MM:00+HH:MM[Area/City]`; and a due query's `as_of` is
`ISO_DATE` for tasks and `UTC_INSTANT` for reminders. Each is matched whole,
with calendar validity, and consults no time zone's rules. Task and reminder
text declares a 512-character ceiling, its exact rule being
`core/task_records.py`'s.

Two text fields are session inputs, declared as such on their contracts:
`conversations.save.transcript` and `conversation_summaries.create.text`.
Maria supplies their text herself, from the owner's session, before the
contracts are checked; no request, planner, or model ever provides it, a plan
that already carries it is refused, and a contract that declares one can
never be model-eligible.

Every contract also states, with no default, whether a reasoning model may
ever propose its action. Only five of the 65 actions are model-eligible:
`calculator.calculate`, `filesystem.list`, `system.current_time`,
`tasks.create`, and `reminders.create`, the last two only for a request in one
of the four natural-language forms Maria reads herself. The other 60,
including all three audit actions, all eleven saved-conversation and
conversation-summary actions, the other twenty task and reminder actions, and
the three reminder-delivery actions, are closed to the model. The
classification is independent of permission and approves nothing by itself:
`core/plan_compiler.py` keeps its own closed allowlist and compiles a proposal
only when the two agree and every argument satisfies the action's contract.
The compiled plan meets the same gates again when it is offered for approval
and again before it runs. A refused proposal receives one fixed message that
names no tool, action, argument, or value the model supplied. A compiled plan
always needs exactly one approval from the owner, even when `PermissionManager`
allows its action outright; see "Model-to-Plan Compilation Gate" and
"Model-Originated Execution and Audit" in `docs/architecture.md`.

## Built-in Tools

- `audit`: list, read, and delete content-free audit summaries of approved
  model-proposed actions
- `calculator`: basic arithmetic
- `conversation_summaries`: store an approved conversation summary, list,
  read, edit, and delete summaries, and turn one summary's context consent on
  or off
- `conversations`: save the current session's conversation, and list, read,
  and delete saved conversations
- `filesystem`: read, list, rename, and move inside Maria's named filesystem roots
- `history`: list, search, read, and delete SQLite-backed interaction-history
  rows
- `memory`: store, search, list, update, and forget explicit SQLite-backed
  memories
- `notes`: create, list, read, retitle, edit, and delete SQLite-backed notes
- `reminders`: add, list, query, read, complete, reopen, edit, reschedule, and
  delete reminders, turn one reminder's context consent on or off, and show or
  change the reminder delivery setting; it never delivers or notifies
- `system`: read-only OS, CPU, RAM, disk, and time information
- `tasks`: add, list, query, read, complete, reopen, edit, reschedule, and
  delete tasks, and turn one task's context consent on or off

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

The filesystem tool supports `read`, `list`, `rename`, and `move` inside Maria's named
filesystem roots. Rename and move always require explicit confirmation. There is no
write, delete, copy, overwrite, glob, recursion, bulk, or archive action, and none is
reachable by argument.

`list` is the only filesystem action a reasoning model may propose, and the compiler
accepts a proposed `path` only as plain text relative to the default root: never
absolute, never beginning with `~`, and never stepping up through `..`; see
"Model-to-Plan Compilation Gate" in `docs/architecture.md` for the full rule. Once the
owner approves it, the proposed listing goes through the same resolution, exclusions,
and repository barrier as the owner's own `list` command.

### Named Roots

Maria no longer has a single configured safe directory. `config/config.toml` declares
named roots in a `[filesystem]` section, and the shipped declaration is exactly two:

| Name | Configured path | Policy | Enabled |
|---|---|---|---|
| `workspace` | `~/MariaWorkspace` | `organize` | yes |
| `project_maria` | `..` (Maria's repository root) | `read_only` | no |

`default_root` is `workspace`. A relative path resolves against the default enabled
root, an absolute path is accepted only inside an enabled root, and a `~` prefix
expands first. The current working directory is never consulted, so Maria's roots do
not move with the shell that started it.

A root's policy decides what may happen inside it. An `organize` root permits all four
actions. A `read_only` root permits `read` and `list` only; `rename` and `move` are
refused there, on the source operand and on the destination operand alike.

`project_maria` is declared but disabled, reserved for a later Project Maria capability.
A path beneath a disabled root is treated exactly as a path outside Maria's roots and
produces the same content-free refusal, which never names the root.

Rename and move resolve source and destination independently and then require both to
be in the *same* root. A cross-root operation is refused even when both roots are
`organize` roots.

An enabled `organize` root is created at startup if it is missing. An enabled
`read_only` root is never created and must already exist. A disabled root is neither
created nor required to exist. Configuration loading itself creates nothing.

### Permanently Excluded Paths

Two separate rules keep content out of reach. Each is applied to *two* identities of a
submitted path: the absolute pre-resolution spelling that was asked for, and the
canonical target it resolves to. One identity alone is not enough. Checking only the
target would let a protected name such as `Maria/logs` be reached by making it a
junction pointing out of the repository; checking only the spelling would let an
ordinary-looking link point in. Containment in a root is still decided by the canonical
target alone.

The first rule is the *universal permanent exclusion*, which every filesystem
capability applies, including the Project Maria capability planned for a later stage.
It has two halves.

The fixed half follows from Maria's source layout:

- `Maria/database/` and everything beneath it, including database sidecars
- `Maria/logs/` and everything beneath it, including rotated logs
- the repository's `.git`, whether it is a directory or a gitdir-pointer file
- `Maria/config/config.local.toml`, the owner's private reasoning override, which
  Maria never creates and which only configuration loading at startup reads
- inside the repository, at any depth: `.env` and any `.env.*` component,
  `__pycache__`, `.venv`, `venv`, `env`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `maria_cli_test`, `.claude`, and any component ending `.tmp` or
  `.bak`

The active half protects the runtime locations the *running* configuration chose,
which the fixed half cannot know about. Configuration may legitimately place any of
them inside an enabled root, and they stay unreachable there:

- the active database file, `[paths] database`, and its SQLite sidecars
  `-journal`, `-wal`, and `-shm`
- the active log directory, `[paths] logs`, as a complete subtree, so every rotation
  beneath it is covered without naming rotations
- the active system prompt, `[paths] system_prompt`, as an exact file

The database, its sidecars, and the prompt are each protected as an identity *and*
everything beneath it. Under the ordinary file shape that costs nothing, because a
file has no descendants; it matters when one of those names turns out to be occupied
by a directory or a directory reparse point, where protecting only the exact path
would leave the contents reachable one level down. An unrelated neighbour in the same
directory — `maria.db.notes`, say — is unaffected and stays usable.

A sidecar contributes two identities: the name beside the database, and — when that
name already resolves elsewhere through a link — the canonical target it points at,
so the content stays protected under the name it actually lives at, and so does
anything beneath it.

Both halves are settled once at startup and handed to the filesystem tool, so the
boundary a command meets is the one configuration established.

### Relocating a Directory

`rename` and `move` ask a wider question than `read` and `list` do, because relocating
a directory carries everything beneath it. An operand is refused when it *contains* an
active location as well as when it *is* one: a directory holding the live database, one
of its sidecars, or the system prompt cannot be renamed or moved, and neither can an
ancestor of the active log directory. The rule is pure path arithmetic, so a
destination that does not exist yet is judged the same way — a directory that would
*become* an ancestor of the configured prompt is refused before it is created. The
repository barrier widens for a mutation too: a path that contains the repository is
refused as well as one inside it.

Both identities of both operands are checked, and the check runs after placement and
direct reachability but before the policy check, the same-root check, and every
existence check, so nothing is created, renamed, or moved on the way to finding out.
Every refusal is the same fixed message and names neither the operand nor the active
location that caused it.

This is deliberately *not* the direct rule. An ordinary parent directory holding a
protected child stays readable and listable with that child filtered out; it is simply
immovable. An ordinary sibling directory with no protected descendant renames and moves
normally.

`.env.example` is the one exemption, and it is exact: the spelling must match and it
must be the final component. A *directory* named `.env.example` is therefore not an
allowed subtree, and nothing beneath one is exempt.

The component rules above are repository-scoped: they apply only once a canonical path
is known to lie inside Maria's own repository. A workspace may hold its own `env`,
`venv`, or `.claude` directory and its own `notes.tmp` or `archive.bak` file, and all
of them remain readable, listable, and movable.

The second rule is the *general filesystem repository barrier*. Any path at or beneath
Maria's repository root — by spelling or by target — is unavailable to
`filesystem.read`, `filesystem.list`, `filesystem.rename`, and `filesystem.move`. That
covers tracked source, documentation, prompts, `config.toml`, and any untracked file
beside them. Declaring a root around the repository does not defeat it, and neither
does a repository path that links outward nor a workspace path that links in.

### Listing Behavior

`list` settles each child before describing it. The raw entry name gives the child's
spelling beneath the already-canonical parent, and resolving it gives the target; both
go through the governing-root check, the universal exclusions, and the repository
barrier. Only a child that survives all of it is classified as a file or a directory,
so a child that escapes the root, points into a protected location, or *is* a protected
name pointing outward is never stat'ed.

A child that is conclusively outside the root, universally excluded, or behind the
repository barrier is omitted silently. The parent listing still succeeds, its ordinary
siblings remain visible, and the response names neither the omitted entry nor what it
pointed at. A directory whose every child was filtered away reports the unchanged
`Directory is empty.`

Silence stops there. An unexpected `OSError` while resolving or classifying a child is
not swallowed: the whole listing fails through the usual sanitized
`Directory listing failed: ...` path rather than returning a short listing that looks
complete.

### Paths and Messages

Every path operand must be an actual `str`. A value of any other type is refused with
`Path must be text.` rather than being rendered into a path, so a malformed direct call
can no longer become a real filesystem target. `list` still defaults an absent `path`
to the governing root.

Internally the tool resolves every operand to a canonical absolute path, and all
containment validation, link resolution, and actual filesystem work happen on those
absolute paths. Only the user-facing message text is rendered differently.

User-facing messages show paths relative to the governing root, always using forward
slashes so output is identical on every platform. The root itself displays as `.`:

```text
Not a file: archive/processed/Customer Order.txt
Not a directory: main.py
Source does not exist: inbox/missing.txt
Destination already exists: archive/report.txt
Renamed inbox/old name.txt to archive/new name.txt.
Moved inbox/Customer Order.txt to archive/processed/Customer Order.txt.
```

Displayed paths are derived after resolution, so redundant `.` and `..` components are
removed and an absolute operand inside a root is still shown relative to it. Spaces,
Unicode, casing, and quote characters are preserved as literal path characters; the
tool performs no shell or quote parsing.

Four fixed refusals carry no operand at all. Each interpolates nothing:

```text
Path is outside Maria's configured safe root.
Path is permanently excluded from Maria's filesystem access.
This filesystem root is read-only.
Source and destination must be inside the same filesystem root.
```

No response reveals the submitted absolute path, the owner's home location, the
repository location, a root name, an excluded filename, raw OS exception text, or an
object repr or address.

Failures are reported with a sanitized reason. Raw `OSError` and `shutil.Error` text is
never copied into a response, and neither are the `filename`, `filename2`, `strerror`,
or `args` fields those exceptions carry. A reason is chosen as follows:

- For an `OSError`, the reason comes from `os.strerror(exc.errno)`.
- For a `ValueError`, only Maria's fixed controlled messages are preserved:

  ```text
  Path is required.
  Path must be text.
  Path is outside Maria's configured safe root.
  Path is permanently excluded from Maria's filesystem access.
  This filesystem root is read-only.
  Source and destination must be inside the same filesystem root.
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
Directory listing failed: Path is permanently excluded from Maria's filesystem access.
Rename failed: This filesystem root is read-only.
Move failed: The operating system reported an unspecified error.
```

Path-bearing tool result `data` is unchanged and still contains canonical absolute
paths. The operational log lines of `read`, `rename`, and `move` are unchanged too and
still contain canonical absolute paths and full raw exception diagnostics, which is
where the per-filename detail omitted from responses remains available. `list` is the
exception: because a reasoning model may propose it, it logs only the fixed lines
`Listed a directory.` and `Directory listing failed.`, never the directory, the
submitted path, or exception text. What a listing shows the owner is unchanged.

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

The notes tool supports `create`, `list`, `read`, `retitle`, `edit`,
`delete`, `enable_context`, and `disable_context`. `list` returns notes newest
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
allowed. `retitle` and `edit` also turn the note's context consent off in the
same transaction, because consent covers the exact title and body the owner
approved; only when consent had been on does the success message continue
`Context consent for note <id> was turned off because its title changed.` or
`... because its body changed.`, and otherwise it is exactly as before. An
ordinary `Memory` failure is wrapped as `Note retitle failed: Unable
to retitle note.`, `Note edit failed: Unable to edit note.`, or `Note deletion
failed: Unable to delete note.`; a busy/locked failure is wrapped the same way,
for example `Note deletion failed: Database is busy. Try again shortly.`. None
of the three cascade to `interactions` or `memories`, renumber remaining note
IDs, or perform bulk, wildcard, or range mutation.

`enable_context` and `disable_context` turn one note's context consent on or
off by numeric ID, validated the same way. Consent is off for every note until
the owner turns it on, and it lets the note's entire title and body reach the
local reasoning model on a later conversational turn when a word of the
request appears literally in its title or body; see "Tier B Search Context" in
`docs/architecture.md`. When confirmation is offered, the tool's
`confirmation_binding` reads the note's current title and body once and, from
that one read, shows the owner both in full below the exposure notice, each
quoted on one line with every invisible or line-breaking character escaped,
and adds `text_digest`, the identity of that title and body together, to the
step, so the approval covers exactly the text shown:

```text
This approval covers note <id> exactly as it is stored now, shown quoted:
Title: "<title>"
Body: "<body>"
```

Only `text_digest` enters the pending plan. The shown text is never recorded,
logged, kept in the conversation, or sent to a model; if the note cannot be
read, the request is shown without it and the approval pins nothing, so it can
never enable an existing note. Enabling succeeds only while the note is still
that text:

```text
Enabled context for note <id>.
Context was not enabled for note <id> because its title and body could not be confirmed unchanged since confirmation was requested. Request confirmation again with: enable note context <id>
Disabled context for note <id>.
```

Disabling needs no identity, and a missing ID returns `Note not found: <id>.`.
Neither result carries the title, body, or identity. Enabling does not check a
note for passwords or other secrets, and consent is not a claim that it holds
none.

Successful operations log only the numeric note ID and fixed wording, such as
`Deleted note <id> through notes tool.`; note titles and bodies never appear in
operational log messages, result data, or mutation response text.

The tool receives the shared `Memory` instance through dependency injection. It
is registered as `tools.notes` through Tool Manager's standard `build_tool(...)`
factory contract and travels through the normal permission path; `notes.create`,
`notes.list`, and `notes.read` are allowed by default, while `notes.retitle`,
`notes.edit`, `notes.delete`, `notes.enable_context`, and
`notes.disable_context` always require explicit confirmation. The tool contains
no SQL; `core/memory.py` owns all database operations.

## Memory Tool

The memory tool supports exactly seven actions:

- `store`: store a deliberate fact
- `search`: find facts by literal, case-insensitive substring
- `list`: list the newest stored facts
- `update`: replace one memory's fact by numeric ID
- `forget`: remove one memory by numeric ID
- `enable_context`: turn one memory's context consent on by numeric ID
- `disable_context`: turn one memory's context consent off by numeric ID

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
Consent covers the exact text the owner approved, so an update also turns that
memory's context consent off in the same transaction. When consent had been
on, and only then, the message continues:

```text
Updated memory <id>. Context consent for memory <id> was turned off because its text changed.
```

`enable_context` and `disable_context` validate `id` the same way and change at
most one row. They return `Enabled context for memory <id>.` or `Disabled
context for memory <id>.` with `data={"id": <id>, "context_enabled": <bool>}`,
or `Memory not found: <id>.` — never the fact, so the interaction row a consent
change records carries only the id. Enabling an enabled memory, or disabling a
disabled one, succeeds and changes nothing. A `Memory` failure is wrapped as
`Memory context change failed: <message>`.

An enable approval covers exact text. The tool's `confirmation_binding` adds
`fact_digest`, the SHA-256 of the memory's current text, to the step when
confirmation is offered, and `enable_context` enables consent only while the
memory is still exactly that text, checked and changed in one transaction. If
it is not — or no identity was pinned for a memory that exists — nothing
changes and the result is:

```text
Context was not enabled for memory <id> because it could not be confirmed unchanged since confirmation was requested. Request confirmation again with: enable memory context <id>
```

`disable_context` needs no identity, because it only narrows what can be sent. What consent means, and how enabled
memories reach the model, is described under "Tier A Memory Context" in
`docs/architecture.md`.

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
default, while `memory.update`, `memory.forget`, `memory.enable_context`, and
`memory.disable_context` always require explicit confirmation. The tool contains no SQL; `core/memory.py` owns all database
operations.

## Audit Tool

The audit tool supports exactly three actions:

- `list`: list the newest audit summaries
- `read`: display one audit summary by numeric ID
- `delete`: remove one audit summary, together with its outcome, by numeric ID

An audit summary records that the owner approved one action the local
reasoning model proposed and that Maria authorised that attempt for dispatch.
Brain commits the summary before the action is dispatched and does not run the
action at all if it cannot. After the tool returns, Brain appends the outcome
and follows the action's result with `Recorded as audit summary <id>.`, or with
a notice that the outcome could not be recorded. A model-proposed plan of two
to five steps has one summary for each step Maria reached for dispatch, each
committed before its own step runs, and none for a step after the plan
stopped; each step's block in the plan's report names its own summary. The
listing shows every summary on its own line, and nothing links the summaries
of one plan. A model-proposed action is never recorded as interaction
history, so its summary is its only record in Maria's database; see
"Model-Originated Execution and Audit" in `docs/architecture.md`.

A summary holds only its numeric ID and values Maria generates from the closed
vocabulary in `core/audit.py`: a UTC timestamp to the second in the form
`YYYY-MM-DDTHH:MM:SSZ`, the action's canonical identifier, the owner's
approval, a generalised target, and, once the tool has returned, the outcome
the tool's own success flag reported. The target is shown as
`named filesystem root` for an action whose contract declares a path argument,
whether or not a path was supplied, and as `none` otherwise. No request,
proposal, argument, root name, path, tool result, or exception text is ever
stored, so the tool can never show one. Summaries live in the
`audit_summaries` and `audit_outcomes` tables that schema version 5 added; see
`docs/database.md`.

`list` shows at most the newest 20 summaries, newest ID first, one per line:

```text
3: 2026-10-01T09:46:17Z system.current_time (target: none). Approved by the owner. Outcome not recorded; execution is unknown.
2: 2026-10-01T09:45:02Z filesystem.list (target: named filesystem root). Approved by the owner. Outcome: failed.
1: 2026-10-01T09:44:10Z calculator.calculate (target: none). Approved by the owner. Outcome: succeeded.
```

When more than 20 summaries exist, only the newest 20 are shown and one line
is appended:

```text
Showing the newest 20 audit summaries. More audit summaries exist.
```

An empty result is a successful operation with the message `No audit summaries
found.`.

`read` displays one summary, one field per line:

```text
Audit summary <id>
Created at: <created_at>
Action: <action>
Target: <target>
Decision: approved by the owner
<outcome>
```

`<target>` is `none` or `named filesystem root`, and `<outcome>` is `Outcome:
succeeded`, `Outcome: failed`, or `Outcome not recorded; execution is
unknown.`. A summary without an outcome means nothing is known about whether
the action ran: Maria may have stopped after authorising the attempt or during
the tool's run, or the outcome may not have been written. A summary alone
never proves that the action ran, and Maria never reruns it.

`read` and `delete` validate their `id` argument the same way as the history
tool (an actual `int`, not a `bool`, at least `1`), returning `Audit summary id
must be a positive whole number.` for an invalid direct value without calling
`Memory`. A missing ID returns `Audit summary not found: <id>.`. A successful
deletion returns `Deleted audit summary <id>.` with `data={"id": <id>}`. It
removes the summary and its outcome together in one transaction, never
cascades to `interactions`, `notes`, or `memories`, and never performs bulk,
wildcard, or range deletion. Summary IDs are never reused, so a later summary
can never take a deleted one's ID.

A failure in the memory layer is reported as one fixed message that repeats
nothing from the database or the exception, even when the database is busy:
`Unable to list audit summaries.`, `Unable to read audit summary.`, or `Unable
to delete audit summary.`. A stored value outside the audit vocabulary fails
the whole listing or read the same way rather than being shown.

What the tool gives interaction history to record as a command's response is
fixed text and IDs alone. `list` sets `ToolExecutionResult.recorded_message`
to `Listed audit summaries.` for every successful listing, including an empty
one, and `read` sets it to `Displayed audit summary <id>.`, so no summary's
fields are recorded again. `delete` leaves `recorded_message` unset (`None`),
since `Deleted audit summary <id>.` is already short and content-free.
Successful operations log only fixed wording and, for `read` and `delete`, the
numeric ID, such as `Deleted audit summary <id> through audit tool.`.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.audit` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `audit.list` and `audit.read` are allowed by default, while
`audit.delete` always requires explicit confirmation, which configuration
cannot remove. The confirmation is asked for even when no summary has that ID,
and it adds this notice:

```text
Deleting an audit summary removes it from Maria's database. It does not undo the action, and Maria's operational log may still show that a model-proposed action ran.
```

None of the three actions is model-eligible, so only the owner's own commands
reach them, and audit summaries are never sent to the model as context. The
tool contains no SQL; `core/memory.py` owns all database operations.

## Conversations Tool

The conversations tool supports exactly four actions:

- `save`: store the transcript Maria supplied as one saved conversation
- `list`: list the newest saved conversations
- `read`: display one saved conversation by numeric ID
- `delete`: remove one saved conversation by numeric ID

`save` takes one argument, `transcript`, which is the session input its
contract declares: the canonical compact JSON transcript of exactly the
exchanges the session retains, supplied by Maria from the session, never by a
request or a model. The tool checks that it is exactly that canonical form
before storing it, and stores nothing otherwise. A save replies with only the
new ID and the exchange count:

```text
Saved this session's 2 exchanges as saved conversation 3. It is an exact copy: later changes to memories or notes do not change it.
```

`list` shows at most the newest 20 saved conversations, newest ID first, one
content-free line each, and appends
`Showing the newest 20 saved conversations. More saved conversations exist.`
when more exist; an empty result is `No saved conversations found.`:

```text
3: saved at 2026-10-03T08:09:10Z, exchanges: 2
```

`read` shows one saved conversation with every turn quoted on its own line,
each text escaped exactly as a note's preview is, so no stored character can
start a new line, hide, or pose as another turn:

```text
Saved conversation 3
Saved at: 2026-10-03T08:09:10Z
Exchanges: 2
1. You: "hello there"
   Maria: "Hello."
2. You: "tell me more"
   Maria: "There is more."
```

`read` and `delete` refuse an invalid direct ID with `Saved conversation id
must be a positive whole number.` without calling `Memory`, and a missing ID
returns `Saved conversation not found: <id>.`. A successful deletion returns
`Deleted saved conversation <id>.`, removes one row, never cascades, and never
lets the ID be reused. A memory-layer failure is one fixed message:
`Unable to save the conversation.`, `Unable to list saved conversations.`,
`Unable to read the saved conversation.`, or
`Unable to delete the saved conversation.`.

`list` records `Listed saved conversations.` and `read` records
`Displayed saved conversation <id>.` through `recorded_message`; `save` and
`delete` record their own content-free replies. `conversations.save`,
`conversations.list`, and `conversations.read` are allowed by default, and
`conversations.delete` always requires confirmation. No action is
model-eligible, and a saved conversation is never sent to the model.

## Conversation Summaries Tool

The conversation summaries tool supports exactly seven actions:

- `create`: store an approved summary draft
- `list`: list the newest conversation summaries
- `read`: display one summary by numeric ID
- `edit`: replace one summary's text by numeric ID
- `delete`: remove one summary by numeric ID
- `enable_context`: turn one summary's context consent on
- `disable_context`: turn one summary's context consent off

`create` takes `text`, the session input its contract declares: a draft the
owner's local model wrote from exactly the session's retained exchanges,
supplied by Maria, never by a request or a model's proposal. `create` always
requires confirmation, and the tool's `confirmation_binding` makes that
confirmation the owner's review of exactly that text: it pins the draft's
SHA-256 as `text_digest` and shows the whole draft, escaped, on one line:

```text
This approval stores exactly this draft as a new conversation summary, shown quoted:
"We planned the trip: book the train on Friday."
```

The draft is stored only when `text_digest` matches it, so a draft never shown
in full can never be stored, and the reply is
`Saved as conversation summary <id>. Its context consent is off.`.
`enable_context` is bound the same way to the stored text, which it reads
once to show and pin; the memory layer enables consent only while the summary
still holds exactly that text, and otherwise the owner is asked to request
confirmation again. `edit` replaces the text and turns consent off, saying so
when it was on.

`list` shows the newest 20 summaries, one content-free line each, such as
`1: created at 2026-10-03T08:09:10Z, context off`, and `read` shows one:

```text
Conversation summary 1
Created at: 2026-10-03T08:09:10Z
Updated at: 2026-10-03T08:09:10Z
Context: off
Text: "We planned the trip: book the train on Friday."
```

Invalid IDs return `Conversation summary id must be a positive whole
number.`, missing ones `Conversation summary not found: <id>.`, and text that
is not a valid summary is refused with the vocabulary's fixed message. Every
memory-layer failure is one fixed message. `list` and `read` record
`Listed conversation summaries.` and `Displayed conversation summary <id>.`
through `recorded_message`, and every other reply names at most an ID.
`conversation_summaries.list` and `conversation_summaries.read` are allowed
by default; `create`, `edit`, `delete`, `enable_context`, and
`disable_context` always require confirmation. No action is model-eligible.
These are conversation summaries, never the content-free audit summaries of
the audit tool.

## Tasks Tool

The tasks tool, `tools/tasks.py`, supports `create`, `list`, `due`, `read`,
`complete`, `reopen`, `edit`, `reschedule`, `delete`, `enable_context`, and
`disable_context`, reaching tasks only through the shared `Memory` object. A
task is one line of text, 1 to 512 UTF-8 bytes, with an optional calendar due
date. Every date it stores was resolved before the plan was built, against
the request's own reference instant, so the tool reads no clock and needs no
time zone data.

`create`, `edit`, `reschedule`, `delete`, and both consent actions always
require confirmation, and the confirmation shows exactly what is covered
before anything is stored:

```text
add task due friday: File the tax return
Confirmation required: tasks.create requires explicit confirmation.
This approval adds a task, shown quoted:
Text: "File the tax return"
Due: Friday 2026-10-09, a calendar date with no time
```

An edit or reschedule shows the saved value beside the new one, a deletion
shows the saved task, and enabling consent shows it and pins the identity of
exactly that text and due date. `list`, `due`, `read`, `complete`, and
`reopen` are allowed. Listings show at most 20 tasks and say when more exist,
open ones by due date with undated ones last; `due` lists open tasks due on or
before the plan's local date, each labelled `(due today)` or `(overdue)`.
Completing stamps the time of that completion and reopening clears it,
changing nothing else. Replies that would show a task's text or date record
only fixed lines such as `Added task 4.`, `Listed open tasks.`, or
`Displayed task 4.`; the log names ids and fixed phrases only.

## Reminders Tool

The reminders tool, `tools/reminders.py`, supports the same eleven actions
for reminders, through the shared `Memory` object, and, under V1-23-R1, three
more: `delivery_status`, `enable_delivery`, and `disable_delivery`. A reminder
is one line of text with one moment: the canonical local time, offset, and
IANA zone the owner approved, and the UTC instant they encode, which alone
orders reminders and selects due ones. A reminder never repeats, and the tool
itself never delivers, notifies, schedules, or polls anything: `due` lists the
open reminders whose saved instant is at or before the plan's reference
instant, each labelled `(time reached)`, only when the owner asks. Showing due
reminders unasked is the reminder-delivery subsystem's alone; see "Reminder
Delivery" in `docs/architecture.md`.

The three delivery actions take no argument and are never open to a model.
`delivery_status` (`reminder delivery`) is allowed and replies with the
setting, the time it last changed, and how many reminders' current saved
times were displayed, are unconfirmed, and were not displayed, adding that
Maria cannot see whether the Windows task is installed. `enable_delivery`
(`enable reminder delivery`) and `disable_delivery`
(`disable reminder delivery`) are mandatory confirmations, each showing its
own fixed notice, and write the one delivery setting row; turning delivery on
twice keeps the first time. None of the three sets `recorded_message`: each
records its ordinary reply, which holds no reminder's text or time. `read`
adds one Delivery line when the reminder has a claim for its current saved
time, saying when the claim was started, through which surface, and whether
it was displayed, could not be displayed, or is unconfirmed; a listing never
shows it. A reminder added now says that Maria shows it at most once and only
while reminder delivery is on.

Its confirmations work as the tasks tool's do, and an add shows the resolved
time in full:

```text
Time: Monday 2026-10-05 at 09:00, Europe/Berlin (UTC+02:00), which is 2026-10-05 07:00 UTC
Repeats: never
```

Every reminder is shown from its saved values, which need no time zone data.
When the saved zone's current rules no longer give the saved offset for the
saved local time, the tool adds `Time zone rules for this reminder's zone have
changed since it was set. It stays at its saved instant; reschedule it to use
the current rules.`, and when the saved zone cannot be loaded, `Maria cannot
check this time against current time zone rules.`; a listing marks such a
row `[rules changed]` or `[rules unchecked]` and explains the marker once
below. A correctly chosen time in a repeated hour agrees with the rules it was
resolved under and shows no warning. Neither ever refuses, rewrites, or moves
a reminder.

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
editing, and deletion (`notes.retitle`, `notes.edit`, `notes.delete`) and note
context consent (`notes.enable_context`, `notes.disable_context`) are checked
by `permissions.py` the same way and always require explicit confirmation
before a note row is changed or removed. Explicit-memory updating
and forgetting (`memory.update`, `memory.forget`) are checked the same way and
always require explicit confirmation before a memory row is changed or removed;
the memory tool provides no bulk or clear-all action, and no way to select a
memory by its fact text. Audit-summary deletion (`audit.delete`) is checked the
same way and always requires explicit confirmation before a summary and its
outcome are removed. Saved-conversation deletion (`conversations.delete`) and
every conversation-summary change (`conversation_summaries.create`, `edit`,
`delete`, `enable_context`, and `disable_context`) are checked the same way
and always require explicit confirmation, and so are adding, editing,
rescheduling, deleting, and both consent actions for tasks and reminders,
and turning reminder delivery on or off, while completing and reopening one
changes its status alone and is allowed, as is showing the delivery status.
Neither the tasks nor the reminders tool starts a thread, a timer, or any
background work. An action a reasoning model proposes needs exactly one
approval from the owner before it runs, even when `permissions.py` allows it
outright; a denied one is refused before any approval is offered, and an
approved one does not run at all unless its audit summary is committed first.
No audit action can be proposed by a model.
