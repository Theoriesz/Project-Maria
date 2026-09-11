# Database

Maria v0.1 uses SQLite through `core/memory.py`.

## File

The default database path is `database/maria.db`.

## Initialization and Migration

`Memory.initialize()` creates required tables and indexes with `IF NOT EXISTS`.
Adding explicit memory is therefore an additive, idempotent migration for an
existing Maria database. It does not alter or remove existing `interactions` or
`notes` data, and repeated initialization preserves stored memories.

Maria does not copy or infer explicit memories from existing interaction rows.

## Tables

### interactions

Stores recorded processed requests and Maria's final response text. These
fields contain the text supplied to the interaction-recording layer, not
summaries or metadata-only representations. Brain supplies the stripped user
request and final response; direct `Memory.record_interaction()` callers supply
their strings unchanged.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable interaction identifier |
| user_input | TEXT NOT NULL | Recorded request text |
| response | TEXT NOT NULL | Recorded final response text |
| created_at | TEXT NOT NULL | UTC ISO 8601 timestamp |

The `idx_interactions_created_at` index supports timestamp-based retrieval.
Recent rows are returned newest first by `created_at` and then descending `id`.
The default direct retrieval limit is 5 and requested integer limits are
clamped to 1 through 50. These limits restrict query results only; they do not
delete or prune older rows.

When a request reaches a recorded path and the database write succeeds, rows
can include file text returned by `read file`, note titles and bodies returned
by `read note`, note-creation requests, calculator expressions and results,
remembered or retrieved facts, filesystem paths and failure messages,
system-information output, and unsupported, malformed, denied, or failed
request responses. Blank input, CLI-only exit and cancellation paths, startup
failures, and the initial confirmation-required response are not recorded. An
approved confirmation is processed again and its completed result follows the
normal recording path.

Successful writes are committed to the configured SQLite file and remain
available across normal restarts until the database is removed or altered
externally. Maria provides no automatic retention period, pruning, archival,
or interaction-history deletion feature. `history`, `search history <query>`,
and `read interaction <id>` list, search, and read existing rows; there is
still no CLI command to clear or delete interaction history. Calling data
"recent" does not imply that older rows expire.

### notes

Stores simple notes created by the notes tool.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER | Autoincrementing primary key |
| title | TEXT NOT NULL | Note title |
| body | TEXT NOT NULL | Note body |
| created_at | TEXT NOT NULL | UTC ISO 8601 timestamp |
| updated_at | TEXT NOT NULL | UTC ISO 8601 timestamp |

Notes are returned newest first by `updated_at` descending and then `id`
descending. The CLI normally returns at most 20 rows. When the notes tool's
`list` action runs at the default limit of 20, it requests one additional row
solely to prove whether a 21st (older) row exists; that extra row is never
included in the displayed message or returned tool data, which both remain
capped at 20. If the extra row is present, the tool appends one line stating
that more notes exist. This does not delete, expire, or prune any row.

### memories

Stores facts that the user deliberately saves as explicit memories.

```sql
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL CHECK (length(fact) > 0),
    normalized_fact TEXT NOT NULL UNIQUE
        CHECK (length(normalized_fact) > 0),
    created_at TEXT NOT NULL
);
```

| Column | Type and constraints | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable memory identifier |
| fact | TEXT NOT NULL, non-empty check | Stripped original text retained for display |
| normalized_fact | TEXT NOT NULL, UNIQUE, non-empty check | `fact.casefold()` value used for exact duplicate prevention and search |
| created_at | TEXT NOT NULL | UTC ISO 8601 timestamp produced by Maria's timestamp helper |

The unique `normalized_fact` constraint makes case-insensitive exact duplicates
idempotent. Storing a duplicate returns the existing ID and keeps the original
display casing and creation timestamp; it does not add or update a row.

## Interaction Retrieval

`history` requests the newest interactions via `get_recent_interactions()`.
`search history <query>` strips the query and uses SQLite's built-in
`LOWER()` for ASCII case-insensitive comparison together with literal
`instr()` substring matching against both `user_input` and `response`;
percent (`%`) and underscore (`_`) are literal characters, not wildcards.
This is not full Unicode casefold equivalence — unlike `recall`, which
compares a precomputed casefolded column — so a search may fail to match
text that differs only by non-ASCII casing. Search is not semantic, fuzzy,
ranked, or model-based.

Both operations order rows by `created_at` descending and then `id`
descending. The CLI normally returns at most 20 rows. Direct
`search_interactions()` calls default to 20 and clamp requested limits to
the same 1-through-50 range as `get_recent_interactions()`.

When `history` or `search history` runs, the tool requests one additional
row solely to prove whether a 21st (older) row exists; that extra row is
never included in the displayed message or returned tool data, which both
remain capped at 20. If the extra row is present, the tool appends one line
stating that more results exist. This does not delete, expire, or prune any
row.

`read interaction <id>` displays the complete stored request, response, and
timestamp for one row, but records only a fixed, content-free audit summary
(`Displayed interaction <id>.`) instead of the historical content it
redisplayed, so repeatedly reading old rows does not duplicate their content
into new interaction rows. `list`, `search`, and `read` are all read-only:
none of them delete, clear, or prune any interaction row.

## Memory Retrieval

`recall <query>` strips and case-folds the query, then uses SQLite `instr()` to
perform literal substring matching against `normalized_fact`. Percent (`%`) and
underscore (`_`) are literal characters, not wildcard operators. Search is not
semantic, fuzzy, ranked, or model-based.

Search and list results contain `id`, `fact`, and `created_at`. Both operations
order rows by `created_at` descending and then `id` descending. The CLI normally
returns at most 20 rows. Direct `search_memories()` and `list_memories()` calls
default to 20 and clamp requested limits to the range 1 through 100.

When the memory tool's `search` or `list` action runs at the default limit of 20,
it requests one additional row from `search_memories()`/`list_memories()` solely
to prove whether a 21st (older) row exists; that extra row is never included in
the displayed message or returned tool data, which both remain capped at 20. If
the extra row is present, the tool appends one line stating that more results
exist. This probing does not apply when an explicit non-default limit is
requested, and it does not delete, expire, or prune any row — it only affects
how the tool decides whether to mention that older rows were left out.

Literal substring retrieval scans the stored normalized text and is intended
for a small personal database. No update, deletion, or `forget` operation exists
for memories in v0.1.

## Data Considerations

Interaction history, notes, and explicit memories are distinct persistent
concepts and use separate tables in the same configured database. The same
text may nevertheless appear more than once: an explicit fact can occur in its
`remember` request, the `memories` table, and later recall or list responses;
a note body can remain in an interaction response as well as the `notes` table.
Likewise, file or note content returned in a response remains independent of
its original source. Removing or changing source data outside Maria does not
automatically remove matching text from existing interaction rows. All three
tables share one database file; external removal or replacement affects all
three datasets.

Maria v0.1 exposes no API or CLI operation to delete or prune interaction
history, delete notes, or delete or forget explicit memories. It also provides
no application-level database encryption. Protection depends on access to the
machine, user account, and database file and on any storage encryption in use;
local storage alone is not a confidentiality guarantee. The v0.1 persistence
layer contains no network-transmission path, and the shipped runtime does not
itself upload this history. Configured network-mounted or synchronized storage
and operating-system backup or sync services remain outside Maria's boundary.

The configured operational log (`logs/maria.log` by default) is separate from
SQLite interaction history. The log records application events, warnings, and
failures; the `interactions` table records request and response text. Omitting
full content from an operational log entry does not mean the content is absent
from an interaction row.

Do not submit or request display of passwords, API keys, access tokens, private
keys, recovery codes, confidential credentials, or other highly sensitive
secrets. In particular, reading a sensitive file or note can copy its returned
content into interaction history.

## Connection Timeouts and Contention

Every `Memory` connection is opened with an explicit SQLite busy-timeout of
5.0 seconds (`DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS` in `core/constants.py`,
passed as `sqlite3.connect(..., timeout=...)`). This configures SQLite's
built-in busy waiting for `SQLITE_BUSY` contention; Maria adds no Python-level
retry loop, sleep, backoff, or whole-operation replay on top of it. A `BUSY`
operation may complete normally if contention clears while SQLite is still
waiting, with no sign that a brief wait occurred, and SQLite may also return
`BUSY` before the full configured window is consumed. The 5.0-second value
configures this busy-wait policy; it is not a guaranteed wall-clock duration
for a complete `Memory` method call.

Separately, when statement execution or commit ultimately raises a SQLite
error whose numeric primary result code is `SQLITE_BUSY` or `SQLITE_LOCKED`
(including extended variants of either), `Memory` converts it into
`RuntimeError("Database is busy. Try again shortly.")`, classified by that
numeric code rather than by matching exception text. `SQLITE_LOCKED` is
classified into this same controlled response, but — unlike `SQLITE_BUSY` —
it is not guaranteed to use or exhaust the busy-timeout window; SQLite can
report it independently of that setting. Every other SQLite failure
(corruption, constraint violations, disk-full, permission errors, schema
problems, and so on) keeps its existing static, operation-specific
`RuntimeError` message, unchanged by this behavior.

This affects only connection-level timeout and error classification. Schema,
the transaction model, commit/rollback ordering, and deterministic connection
closing on both success and failure are unchanged.

## Ownership

Only `core/memory.py` should open SQLite connections or execute SQL.
