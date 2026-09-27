# Database

Maria uses SQLite through `core/memory.py`.

## File

The default database path is `database/maria.db`.

## Schema Version

The database carries its own version, stored in the SQLite header as
`PRAGMA user_version` and readable through `Memory.schema_version()`. The
version this build expects is `DATABASE_SCHEMA_VERSION` in
`core/constants.py`.

That number is deliberately not `APP_VERSION` and is never derived from it.
The application version stays frozen across a release effort while the schema
may advance, and a schema change may ship without any application version
change at all, so tying one to the other would make both wrong. It is a plain
counter over Maria's schema history rather than a release number:

| Version | Meaning |
| --- | --- |
| 0 | SQLite's default. No marker was ever written; the database has to be classified by its structure. |
| 1 | The earliest schema Maria created: `interactions`, `notes`, and the two secondary indexes. |
| 2 | The published v0.2.0 baseline, which adds `memories`. |
| 3 | Adds the per-memory `context_enabled` consent column to `memories`, off for every row. This is the current schema. |

Versions 1 and 2 are the only shapes that can exist without a marker, because
every database Maria creates or migrates from this build onwards is stamped.
That set is therefore closed and cannot grow.

The marker is a header pragma rather than a version table on purpose. It adds
no table, column, or record type of any kind, it reads as `0` from a brand-new
or empty file without creating anything to answer the question, and SQLite
rolls a change to it back with the transaction that made it, so a schema change
and the version recording it cannot come apart. A metadata table was considered
and rejected: it would have introduced exactly the extra table this stage rules
out, would itself have needed drift detection, and would still not have
distinguished "unversioned" from "the table is missing".

## Initialization and Migration

`Memory.initialize()` settles the database's version first and does everything
else from there. It is the only place that classification happens, so no other
method decides for itself what shape of database it is looking at. Reading the
marker is a read: nothing is created, written, or repaired before the outcome
is known.

| Database state | What happens |
| --- | --- |
| No marker, no user-defined object at all (including a zero-byte file) | Created directly at the current schema, with the marker, in one transaction. It is never mistaken for a migrated owner database. |
| No marker, structure matches version 2 | Classified as the v0.2.0 baseline and stamped `2`. No schema statement runs and no row is touched. |
| No marker, structure matches version 1 | Classified as the pre-`memories` baseline, stamped `1`, then migrated forward. |
| Marker below the current version | The structure is confirmed against the frozen schema for that version, then every missing migration runs in ascending order. |
| Marker equal to the current version | The structure is confirmed against the frozen current schema. Nothing runs and nothing is written; the file is left byte for byte as it was. |
| Marker whose structure is not that version's frozen schema | Refused before any migration statement, with nothing written. |
| Marker above the current version | Refused. See "Refusing a newer schema" below. |
| No marker, any user-defined object that is not Maria's exact schema | Refused with `RuntimeError("Unable to initialize Maria database.")` and nothing written: no Maria table is created, no marker is stamped, and the file is unchanged. |
| Corrupt, unreadable, or not a SQLite database | The same `RuntimeError`, raised from the underlying SQLite error. It is never reported as a newer schema, and nothing about the underlying error is logged. |

### What "empty" means

A file is a new database only when it holds no object SQLite did not create
itself: no table, index, view, or trigger of anyone's. Emptiness is
deliberately not decided by looking for Maria's own table names, because a
file full of another application's tables would then look empty and have
Maria's schema built inside it. Only SQLite's reserved `sqlite_` objects are
ignored, and `sqlite_sequence` in particular, which appears as soon as any
`AUTOINCREMENT` table exists and can outlive it, so a genuinely empty file is
not mistaken for a populated one.

A file holding even one unrelated object is therefore somebody else's. It is
refused: no Maria table is created in it, its `user_version` is not stamped,
and it is left byte for byte as it was found.

### What is validated

Neither a missing marker nor a present one is taken as proof of what a
database is. An unversioned file is identified by its structure, and a stamped
file has its structure confirmed against the frozen schema for the version it
claims, because a marker records which build wrote a database rather than what
the database currently is. A migration that ran against a schema its step was
never written for is the one failure a forward-only runner cannot undo, so
that check happens before any migration statement.

The comparison is a fingerprint of everything the database declares. It is
deliberately exhaustive rather than sufficient for the differences anyone
happened to think of: anything it does not recognise compares unequal and is
refused, which is the direction a schema classifier has to fail in.

**Every user-defined object counts** — tables, explicit indexes, views, and
triggers alike, each with its name and the table it belongs to. Only the
`sqlite_` objects SQLite maintains itself are excluded, because they follow
from the schema rather than defining it.

**What SQLite reports structurally** is read from its own catalogue:

| Source | Fields |
| --- | --- |
| `pragma_table_list` | object type, column count, `WITHOUT ROWID` flag, `STRICT` flag |
| `pragma_table_xinfo` | cid, name, declared type, `NOT NULL`, default, primary-key position, and the hidden/generated classification — `table_info` omits generated columns entirely, which would hide one |
| `pragma_index_list` + `pragma_index_xinfo` | explicit index name, uniqueness, origin, partial flag, and per entry the sequence, cid, column, `ASC`/`DESC`, collation, and whether it is a key column |
| `pragma_foreign_key_list` | id, sequence, target table, from, to, update action, delete action, match mode |

**What SQLite reports nowhere** — a conflict clause, a collation on a column
no index covers, a table option, an `AUTOINCREMENT` clause, a `CHECK`
condition, a trigger body — comes from the statement that created the object,
reduced to typed tokens.

### The typed token form

The statement is never compared as text, neither raw nor whitespace-stripped.
It is tokenized first, and each token keeps the kind of thing it is:

| Kind | Example |
| --- | --- |
| keyword | `AUTOINCREMENT`, `PRIMARY`, `CHECK` |
| identifier | `fact`, and `"fact"`, `` `fact` ``, `[fact]` reduced to the same |
| number | `0` |
| string | `'0'` |
| blob | `x'00'` |
| operator | `(`, `,`, `>`, `>=` |

Typing is what makes the canonical form safe to normalize. Whitespace, line
breaks, comments, casing, and the choice of identifier quoting all disappear,
because none of them changes what a schema means — while two things that read
alike stay apart:

- **A number is not a string.** `CHECK (length(fact) > 0)` and
  `CHECK (length(fact) > '0')` are different conditions and compare unequal.
- **A quoted name is not the keyword it spells.** `CONSTRAINT "autoincrement"
  PRIMARY KEY` does not declare `AUTOINCREMENT`, and is refused.
- **`AUTOINCREMENT` is the clause, not the word.** It is recognised only
  immediately after `PRIMARY KEY`, past nothing but the sort order and
  conflict resolution SQLite allows in between. The keyword in a comment, in a
  string, as a constraint's name, or as a column name is not that clause.

`CHECK` conditions are lifted out of the statement and compared separately, so
that a check declared beside its column and the same check declared for the
table compare alike — SQLite evaluates both per row. Parentheses that merely
wrap a whole condition are dropped. All of these therefore state the same rule
and are all accepted:

```sql
CHECK (length(fact) > 0)
check(length(fact)>0)
CHECK ( LENGTH ( "fact" ) > 0 )
CHECK ((length([fact]) > 0))
```

while `CHECK (length(fact) >= 0)`, `CHECK (length(fact) > '0')`, a check on a
different column, an extra check, a check present only in a comment or inside
a string literal, and a missing check are all refused.

This is canonicalization, not a SQL equivalence engine. A condition rewritten
into a different but equivalent form — `0 < length(fact)` for
`length(fact) > 0` — is refused rather than recognised. That is deliberate:
guessing at equivalence is how a validator starts accepting things it should
not, and a refusal is recoverable while a false acceptance is not.

### What this refuses

Alongside the obvious differences — a missing table, a missing or extra
column, a changed column definition, a different primary-key shape, a missing
`normalized_fact` uniqueness constraint, a missing index, a half-created
database, and a foreign database that merely reuses the table names — the
fingerprint refuses a database that agrees with the baseline on everything a
narrower comparison would have looked at:

| Difference | Caught by |
| --- | --- |
| `CONSTRAINT "autoincrement" PRIMARY KEY` instead of the real clause | typed tokens, clause position |
| `CHECK (length(fact) > '0')` | typed tokens, number vs string |
| an extra `VIRTUAL` or `STORED` generated column | `pragma_table_xinfo` hidden classification |
| `normalized_fact` declared `COLLATE NOCASE` | `pragma_index_xinfo` collation |
| a `STRICT` table | `pragma_table_list` strict flag |
| a `WITHOUT ROWID` table | `pragma_table_list` wr flag |
| `UNIQUE ON CONFLICT IGNORE` | typed tokens (no pragma reports it) |
| a `DESC` secondary index | `pragma_index_xinfo` direction |
| a secondary index with a changed collation, or a partial one | `pragma_index_xinfo`, `pragma_index_list` |
| the exact baseline plus an unrelated view or trigger | object inventory |

Migrations are ordered, forward-only, idempotent, and atomic. The set of steps
is validated before the database is opened, so a gap, a repeat, a step below
the oldest recognisable schema, or a step beyond what this build expects is
refused rather than discovered part-way up. Every version from the earliest
recognised one up to `DATABASE_SCHEMA_VERSION` must also have a frozen schema,
because a database claiming a version with none could not be checked before
being migrated; a build missing one refuses to start rather than trusting the
marker. Each step then runs inside one
explicit `BEGIN IMMEDIATE` transaction together with the `PRAGMA user_version`
write that records it. The explicit transaction is required rather than
decorative: Python's `sqlite3` driver opens one implicitly only for
row-changing statements, so schema statements would otherwise each commit
alone. Because both the schema change and the marker are in the same
transaction, a failure part-way through leaves the database whole at the
previous version with no partial schema, and re-running initialization resumes
at exactly the step that failed and applies it once.

A database held by another process is contention rather than failure: a
`SQLITE_BUSY` or `SQLITE_LOCKED` result anywhere in initialization or in
`Memory.schema_version()` applies nothing, leaves the version where it was,
and raises the shared
`RuntimeError("Database is busy. Try again shortly.")` rather than the
initialization message, so a retryable condition is not reported as a broken
database. Both boundaries classify contention themselves rather than
inheriting the shared conversion, because that conversion logs the exception
on its way past and these record a fixed event code instead.

There are two migrations. `create_memories` brings a version 1 database to
version 2 by adding the `memories` table; Maria does not copy or infer explicit
memories from existing interaction rows. `add_memory_context_consent` brings a
version 2 database to version 3 with one statement:

```sql
ALTER TABLE memories ADD COLUMN context_enabled INTEGER NOT NULL DEFAULT 0
    CHECK (context_enabled IN (0, 1));
```

The `DEFAULT 0` is what turns consent off for every memory that already exists;
SQLite records the added column in the table's definition without rewriting any
row, and the resulting definition fingerprints exactly as a newly created
version 3 table does. Neither step rewrites a row or deletes anything.

Every step runs its own frozen historical statements, never the live schema
statements a new database is created from. The version 2 step therefore creates
the version 2 `memories` table exactly as v0.2.0 did. Had it kept the live
statement once that statement described version 3, a version 1 database would
have received a version 3 table stamped as version 2, the version 3 step would
then have failed on a column that already existed, and every later start would
have refused the database. A test applies each step to its frozen predecessor
and requires the frozen successor, so no step can drift that way again.

### No pre-migration backup

V1-09 writes no backup copy before migrating, and that is a decision rather
than an omission; V1-15's step keeps it true. Every step is additive schema
change plus the version stamp, committed in a single transaction that SQLite
rolls back in full on any failure, so the failure a backup would protect
against — a half-applied migration — cannot be reached. No step reads, rewrites,
transforms, or removes an owner record, so there is nothing for a copy to
preserve that the original does not still hold. Writing one anyway would place
a second complete copy of every interaction, note, and memory beside the live
database indefinitely, which is a privacy cost with no reliability gain.

A pre-migration copy becomes necessary when a migration rewrites or drops
owner data, which no migration here does. The owner-facing backup capability
is a separate, later concern and is not what this would have been.

### Refusing a newer schema

A database stamped above `DATABASE_SCHEMA_VERSION` belongs to a build that
knew things this one does not, so Maria refuses it instead of half-working
against it. The refusal happens before any write: no migration runs, no schema
is created, no marker is changed, and the file and its directory are left byte
for byte as they were found.

The refusal raises `TypedFailureError` carrying
`Failure(FailureKind.DATABASE_SCHEMA_TOO_NEW)`. That value holds a kind and
nothing else, and its message is fixed and interpolates nothing, so no path,
row, SQL statement, or SQLite error text can travel with it. `main.py` prints
that fixed message and exits with status 1:

```
Maria: This database was written by a newer version of Maria and cannot be opened by this one.
```

A corrupt file, an I/O error, or a locked database is never converted into
this diagnosis; each keeps its own existing controlled result.

### What initialization logs

Nothing the initialization boundary writes to the log is derived from what
went wrong. A successful run records the schema version it reached and, for
each migration, that step's name and the version it produced. A refusal
records one fixed sentence carrying a stable event code and nothing else:

```
Database initialization stopped: database_unreadable.
Database initialization stopped: database_busy.
Reading the database schema version stopped: database_unreadable.
Reading the database schema version stopped: database_busy.
```

No row value, note title or body, memory fact, interaction request or
response, database path, SQL statement, SQL parameter, SQLite message, or
traceback is written on any path — success, migration failure, rollback,
drift refusal, newer-schema refusal, or an unreadable file. The underlying
exception is still attached to the raised `RuntimeError` as its `__cause__`
for a caller that needs it; it is simply never written down. Reading the
schema version through `Memory.schema_version()` records failures the same
way, since it reads the same header of the same file.

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
failures, and the initial confirmation-required response are not recorded.
With reasoning enabled, unsupported input is answered conversationally instead,
and no conversational request, reply, or controlled failure is recorded; the
conversation lives only in the session's memory. An
approved confirmation is processed again and its completed result follows the
normal recording path.

Successful writes are committed to the configured SQLite file and remain
available across normal restarts until the database is removed or altered
externally. Maria provides no automatic retention period, pruning, or
archival. `history`, `search history <query>`, and `read interaction <id>`
list, search, and read existing rows without modifying them; `delete
interaction <id>` targets at most one row by ID and removes it when found,
after mandatory confirmation. There is still no CLI command to clear,
bulk-delete, or automatically prune interaction history. Calling data
"recent" does not imply that older rows expire.

### notes

Stores simple notes created by the notes tool.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable note identifier |
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

`retitle note <id>: <new title>` and `edit note <id>: <new body>` each target
at most one row by primary-key ID, after mandatory explicit confirmation and
with no preview of stored content beforehand. Retitle runs a single
parameterized `UPDATE notes SET title = ?, updated_at = ? WHERE id = ?` and
edit runs a single parameterized `UPDATE notes SET body = ?, updated_at = ?
WHERE id = ?`, so each writes exactly one content column and neither can
overwrite the other's field. Neither reads the row first. `created_at` is
always preserved, and `updated_at` receives the current UTC timestamp
generated once per call; that timestamp is not guaranteed to be strictly
later than the previous value, and ordering ties are broken by descending
`id` as usual. Writing a value identical to the stored one still counts as a
successful update and still refreshes `updated_at`, because success is
determined by the row matching `WHERE id = ?` rather than by the value
changing. Duplicate titles across notes remain allowed, since the
`idx_notes_title` index is non-unique rather than a unique constraint.

`delete note <id>` removes at most one row through a single parameterized
`DELETE FROM notes WHERE id = ?` statement, under the same confirmation rule
and without reading the row first. It does not cascade to `interactions` or
`memories` and does not renumber remaining rows' IDs. For Maria's implicit-ID
inserts, SQLite AUTOINCREMENT does not automatically reuse an ID from a
previously committed row, even after the table has been emptied. Each of the
three mutations commits independently first; Maria then
separately attempts to record a short, content-free audit interaction
(`Retitled note <id>.`, `Edited note <id>.`, or `Deleted note <id>.`) as its
own transaction. If that later recording fails, the mutation remains
committed and no audit row is guaranteed. Because the original command is
stored as the request half of an interaction, a retitle or edit command
retains its new title or body in the `interactions` table, and a prior `read
note` may already have copied that note's content there; changing or deleting
the note afterwards does not rewrite or remove those independent rows. There
is no bulk, wildcard, range, or automatic note deletion, no undo or revision
history, and no guarantee of secure erasure from SQLite file pages, backups,
database copies, or external storage.

### memories

Stores facts that the user deliberately saves as explicit memories.

```sql
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL CHECK (length(fact) > 0),
    normalized_fact TEXT NOT NULL UNIQUE
        CHECK (length(normalized_fact) > 0),
    created_at TEXT NOT NULL,
    context_enabled INTEGER NOT NULL DEFAULT 0
        CHECK (context_enabled IN (0, 1))
);
```

| Column | Type and constraints | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable memory identifier |
| fact | TEXT NOT NULL, non-empty check | Stripped original text retained for display |
| normalized_fact | TEXT NOT NULL, UNIQUE, non-empty check | `fact.casefold()` value used for exact duplicate prevention and search |
| created_at | TEXT NOT NULL | UTC ISO 8601 timestamp produced by Maria's timestamp helper |
| context_enabled | INTEGER NOT NULL, DEFAULT 0, only 0 or 1 | The owner's consent to supply this memory to the local reasoning model; see "Memory Context Consent" below |

The unique `normalized_fact` constraint makes case-insensitive exact duplicates
idempotent. Storing a duplicate returns the existing ID and keeps the original
display casing and creation timestamp; it does not add or update a row.

`update memory <id>: <new fact>` and `forget memory <id>` each change at most
one row by primary-key ID, after mandatory explicit confirmation and with no
preview of stored content beforehand. Update runs a parameterized
`UPDATE memories SET fact = ?, normalized_fact = ? WHERE id = ?`, writing the
stripped fact and its `casefold()` form together so display text and search
text can never drift apart. It does not read the row first. When that row
matched, the same transaction then runs `UPDATE memories SET context_enabled =
0 WHERE id = ? AND context_enabled = 1`, because consent covers the exact text
the owner approved; the first statement already holds the write lock, so no
other connection can enable consent in between, and a failure rolls the new
fact back too. The update reports whether consent had been on, and only then
does the owner see an extra sentence saying it was turned off. `created_at` is
always preserved and no `updated_at` column was added, so — unlike a retitled
or edited note — an updated memory keeps its existing position in the
`created_at DESC, id DESC` ordering used by `memories` and `recall` rather than
moving to the top. Writing a value identical to the stored one still counts as
a successful update, because success is determined by the row matching `WHERE
id = ?` rather than by the value changing. A change that affects only letter
casing rewrites the displayed `fact` while leaving `normalized_fact` identical,
so it updates successfully and never conflicts with itself.

Unlike note titles, memory facts stay unique. If the new fact's `casefold()`
form already belongs to a *different* row, SQLite's unique constraint fires and
Maria reports the controlled result `Memory <id> conflicts with existing memory
<other id>.` instead of a raw constraint error. The violation is recognised by
SQLite's exact extended `SQLITE_CONSTRAINT_UNIQUE` result code rather than by
exception text, so the table's `CHECK` constraints — which share the same
primary result code — are never mistaken for uniqueness conflicts. The
conflicting row is then identified through one parameterized lookup on
`normalized_fact`. Neither row is changed, merged, or deleted, and only numeric
IDs are reported. Because `casefold()` is full Unicode folding, visually
different facts can collide: storing `straße` and then updating another memory
to `STRASSE` is a conflict, since both fold to `strasse`.

`forget memory <id>` removes at most one row through a single parameterized
`DELETE FROM memories WHERE id = ?` statement, under the same confirmation rule
and without reading the row first. The forgotten fact is never read, logged, or
returned. It does not cascade to `interactions` or `notes` and does not
renumber remaining rows' IDs. For Maria's implicit-ID inserts, SQLite
AUTOINCREMENT does not automatically reuse an ID from a previously committed
row, even after the table has been emptied. Both mutations commit
independently first; Maria then separately attempts to record a short,
content-free audit interaction (`Updated memory <id>.` or `Forgot memory
<id>.`) as its own transaction. If that later recording fails, the mutation
remains committed and no audit row is guaranteed. Because the original command
is stored as the request half of an interaction, an update command retains its
new fact in the `interactions` table, and an earlier `remember`, `recall`, or
`memories` response may already have copied a fact there; updating or
forgetting the memory afterwards does not rewrite or remove those independent
rows, which can be removed separately with `delete interaction <id>`. There is
no memory mutation by fact text, no fuzzy or contextual selection, no bulk,
wildcard, range, or automatic deletion, no undo or revision history, and no
guarantee of secure erasure from SQLite file pages, backups, database copies,
or external storage.

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

`delete interaction <id>` targets at most one row by primary-key ID and
removes it when found, through a single parameterized `DELETE FROM
interactions WHERE id = ?` statement, after mandatory explicit confirmation
and with no preview of stored content beforehand. It does not read the row
first, does not cascade to `notes` or `memories`, and does not renumber
remaining rows' IDs. For Maria's implicit-ID inserts, SQLite AUTOINCREMENT
does not automatically reuse an ID from a previously committed row, even
after the table has been emptied. The deletion commits independently first;
Maria then separately attempts to record a short, content-free audit
interaction (`Deleted interaction <id>.`) as its own transaction. When that
later recording succeeds, the total interaction row count may remain
unchanged even though the selected historical content is gone; if it fails,
the deletion remains committed and no audit row is guaranteed. There is no
bulk, wildcard, range, or automatic deletion, and no guarantee of secure
erasure from SQLite file pages, backups, database copies, or external
storage.

## Memory Context Consent

`context_enabled` is off for every memory unless the owner turns it on, one
memory at a time. The migration leaves every pre-existing memory off, and a
newly stored memory is off because its insert never names the column.

`enable memory context <id>` and `disable memory context <id>` each change at
most one row by primary-key id, after mandatory explicit confirmation, and the
recorded interaction names only the id. A missing id changes nothing and
reports `Memory not found: <id>.`

Consent covers exact text, so an enable approval is bound to the fact as it
was when confirmation was offered. At that moment `memory_fact_digest(id)`
reads the fact and returns the SHA-256 of its exact UTF-8 text, and that
identity becomes part of the plan the owner approves. At execution,
`enable_memory_context(id, digest)` runs one `BEGIN IMMEDIATE` transaction:
the write lock is taken before the fact is read, the fact's digest is compared
with the approved one, and only a match runs `UPDATE memories SET
context_enabled = 1 WHERE id = ? AND fact = ?` with the text just checked. If
the text is not the approved text — changed through Maria or any other
connection, including a casing-only change — or no identity was pinned for a
memory that exists, nothing changes and the owner is asked to request
confirmation again. The guarantee is exact text, not an unedited history: text
changed away and back to the approved text is enabled. Disabling runs one
`UPDATE memories SET context_enabled = 0 WHERE id = ?` with no text check,
because it only narrows what can be sent. Re-enabling an enabled memory, or
disabling a disabled one, succeeds without changing anything. Neither the fact
nor its digest is ever logged or returned in a result.

`list_context_memories(limit, max_fact_bytes)` is the only read of Tier A
context. It returns enabled memories only, in ascending `id` order, with the
limit clamped to 1 through 100, each as `id`, `fact`, and `oversized`. The size
test runs in SQLite on each fact's UTF-8 bytes (`length(CAST(fact AS BLOB))`),
so a fact longer than `max_fact_bytes` is returned as `fact = NULL` with
`oversized` true and its text never reaches Python. Brain asks for 17 rows —
one more than a request may carry — with a 2,048-byte bound, the most any one
memory could ever occupy, and only on the model-backed conversational path, so
Maria learns whether more enabled memories exist without counting all of them,
and an oversized memory is named as left out without its text being read.
Existing reads (`get_memory`, `list_memories`, `search_memories`) still return
exactly `id`, `fact`, and `created_at`; the consent flag never appears in
`memories` or `recall` output.

The flag is not evidence of anything but consent. `created_at` records when a
memory row was first stored, not when its text last changed, and no
`updated_at` column exists, so Maria makes no claim that an enabled memory is
current, stale, consistent, or conflicting.

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
for a small personal database. `update memory <id>: <new fact>` and `forget
memory <id>` change or remove one memory by numeric ID; both are described
under the `memories` table above.

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

In the current implementation, `delete interaction <id>`,
`delete note <id>`, and `forget memory <id>` each target at most one row by ID
and remove it when found, and `retitle note <id>`, `edit note <id>`, and
`update memory <id>` each change one stored field; Maria still exposes no API
or CLI operation to prune or bulk-delete interaction history, bulk-delete or
clear notes, or bulk-delete or clear explicit memories. Removing one record
never cascades into another table, so forgetting a memory leaves any copy of
its text that already exists in `interactions` untouched. It also provides
no application-level database encryption. Protection depends on access to the
machine, user account, and database file and on any storage encryption in use;
local storage alone is not a confidentiality guarantee. Maria's persistence
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
