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
| 3 | Adds the per-memory `context_enabled` consent column to `memories`, off for every row. |
| 4 | Adds the per-note `context_digest` consent column to `notes`, off (NULL) for every row. |
| 5 | Adds the `audit_summaries` and `audit_outcomes` tables, both empty, which hold the audit summaries of approved model-proposed actions. |
| 6 | Adds the `saved_conversations` and `conversation_summaries` tables, both empty, which hold explicitly saved conversations and owner-approved conversation summaries. This is the current schema. |

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
| No marker, structure matches version 2 | Classified as the v0.2.0 baseline and stamped `2`, with no schema statement run and no row touched, then migrated forward. |
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

There are five migrations. `create_memories` brings a version 1 database to
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
version 3 table does. `add_note_context_consent` brings a version 3 database to
version 4 with one statement:

```sql
ALTER TABLE notes ADD COLUMN context_digest TEXT
    CHECK (context_digest IS NULL OR length(context_digest) = 64);
```

With no `DEFAULT` the new column is NULL, meaning consent off, for every note
that already exists, and the resulting definition fingerprints exactly as a
newly created version 4 table does. `create_audit_summaries` brings a version
4 database to version 5 with two statements:

```sql
CREATE TABLE audit_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    decision TEXT NOT NULL,
    target TEXT NOT NULL
);
CREATE TABLE audit_outcomes (
    summary_id INTEGER PRIMARY KEY,
    outcome TEXT NOT NULL
);
```

Both tables start empty, and the step touches no existing table or row. The
frozen version 5 schema is the frozen version 4 schema plus these same two
statements. Adding them changed neither the frozen schemas of versions 1
through 4 nor the statements of the earlier steps. `create_conversation_records`
brings a version 5 database to version 6 the same way, with two statements:

```sql
CREATE TABLE saved_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    transcript TEXT NOT NULL CHECK (length(transcript) > 0)
);
CREATE TABLE conversation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    context_digest TEXT
        CHECK (context_digest IS NULL OR length(context_digest) = 64)
);
```

Both start empty, the step touches no existing table, row, or AUTOINCREMENT
high-water mark, and the frozen version 6 schema is the frozen version 5
schema plus these two statements; a database created new at version 6
fingerprints exactly as a migrated one does. Neither table refers to the
other or to any other table: there is no foreign key, trigger, extra index, or
cascade. Adding them changed neither the frozen schemas of versions 1 through
5 nor the statements of the earlier steps. No step rewrites a row or deletes
anything.

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
than an omission; V1-15's, V1-16's, V1-18's, and V1-20's steps keep it true. Every
step is additive schema change plus the version stamp, committed in a single
transaction that SQLite rolls back in full on any failure, so the failure a
backup would protect
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
failures, and the initial confirmation-required response are not recorded. The
desktop GUI records exactly what the CLI records, through the same session and
Brain; its exit words, a request refused for holding a line break, and closing
the window are not recorded, and the GUI writes nothing of its own.
With reasoning enabled, unsupported input goes to the local reasoning model
instead, which may reply conversationally or propose one action or a plan of
two to five, and no
conversational request, reply, or controlled failure is recorded; the
conversation lives only in the session's memory unless the owner keeps a copy
with `save conversation` (see "Saved Conversations and Conversation
Summaries" below). A proposal, its refusal, its
approval prompt, and the result of an approved model-proposed action are not
recorded either. A compiled model-originated plan is transient, so neither its
request nor its result becomes an interaction row, and running an approved one
writes only its audit summary (see "Audit Summaries" below). An approved
confirmation of any other plan is processed again and its completed result
follows the normal recording path.

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
| context_digest | TEXT, NULL or exactly 64 characters | The owner's consent to supply this note to the local reasoning model: NULL while off, otherwise the identity of the exact title and body approved; see "Note Context Consent" below |

Notes are returned newest first by `updated_at` descending and then `id`
descending. The CLI normally returns at most 20 rows. When the notes tool's
`list` action runs at the default limit of 20, it requests one additional row
solely to prove whether a 21st (older) row exists; that extra row is never
included in the displayed message or returned tool data, which both remain
capped at 20. If the extra row is present, the tool appends one line stating
that more notes exist. This does not delete, expire, or prune any row.

`retitle note <id>: <new title>` and `edit note <id>: <new body>` each target
at most one row by primary-key ID, after mandatory explicit confirmation and
with no preview of stored content beforehand. Each runs two parameterized
statements in one transaction: first `UPDATE notes SET context_digest = NULL
WHERE id = ? AND context_digest IS NOT NULL`, which turns the note's context
consent off because consent covers the exact text the owner approved, and
then retitle's `UPDATE notes SET title = ?, updated_at = ? WHERE id = ?` or
edit's `UPDATE notes SET body = ?, updated_at = ? WHERE id = ?`, so each writes
exactly one content column and neither can overwrite the other's field. A
failure in the second rolls the first back. Neither reads the row first, and
the response says consent was turned off only when it had been on.
`created_at` is
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
separately attempts to record the interaction, with a short, content-free
response (`Retitled note <id>.`, `Edited note <id>.`, or `Deleted note
<id>.`), as its own transaction. If that later recording fails, the mutation
remains committed and no such interaction row is guaranteed. Because the
original command is
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
independently first; Maria then separately attempts to record the
interaction, with a short, content-free response (`Updated memory <id>.` or
`Forgot memory <id>.`), as its own transaction. If that later recording
fails, the mutation remains committed and no such interaction row is
guaranteed. Because the original command
is stored as the request half of an interaction, an update command retains its
new fact in the `interactions` table, and an earlier `remember`, `recall`, or
`memories` response may already have copied a fact there; updating or
forgetting the memory afterwards does not rewrite or remove those independent
rows, which can be removed separately with `delete interaction <id>`. There is
no memory mutation by fact text, no fuzzy or contextual selection, no bulk,
wildcard, range, or automatic deletion, no undo or revision history, and no
guarantee of secure erasure from SQLite file pages, backups, database copies,
or external storage.

### audit_summaries

Stores one audit summary for each model-proposed action that the owner
approved and Maria authorised for dispatch; see "Audit Summaries" below. Every
value is Maria's own, from the closed vocabulary in `core/audit.py`.

| Column | Type and constraints | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable summary identifier, never reused |
| created_at | TEXT NOT NULL | Maria's own UTC timestamp to the second, always in the form `YYYY-MM-DDTHH:MM:SSZ`, unlike the other tables' timestamps |
| action | TEXT NOT NULL | Canonical identifier of the approved action: `calculator.calculate`, `filesystem.list`, or `system.current_time` |
| decision | TEXT NOT NULL | What authorised the attempt: always `owner_approved` |
| target | TEXT NOT NULL | Generalised class of what the action acts on: `named_filesystem_root` when its contract declares a path argument, whether or not a path was supplied, otherwise `none` |

### audit_outcomes

Stores the outcome Maria appends to an audit summary after its tool returns.

| Column | Type and constraints | Purpose |
| --- | --- | --- |
| summary_id | INTEGER PRIMARY KEY | The `audit_summaries.id` the outcome belongs to; as the primary key it admits at most one outcome per summary |
| outcome | TEXT NOT NULL | `succeeded` or `failed`, as the tool's own success flag reported |

Neither table has a `CHECK` constraint, a foreign key, a trigger, or a
secondary index. The vocabulary is enforced by the memory layer instead, when
a value is written and again when it is read, and the link between the two
tables is kept by the statements that write and delete them, as described
under "Audit Summaries".

### saved_conversations

Stores one explicit save of a session's retained exchanges; see "Saved
Conversations and Conversation Summaries" below.

| Column | Type and constraints | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable saved-conversation identifier, never reused |
| created_at | TEXT NOT NULL | Maria's own UTC timestamp to the second, in the form `YYYY-MM-DDTHH:MM:SSZ` |
| transcript | TEXT NOT NULL, non-empty | The canonical compact JSON transcript of 1 to 8 `[user, assistant]` pairs, oldest first, at most 8,192 UTF-8 bytes of their text |

### conversation_summaries

Stores one owner-approved conversation summary and its context consent.

| Column | Type and constraints | Purpose |
| --- | --- | --- |
| id | INTEGER PRIMARY KEY AUTOINCREMENT | Stable summary identifier, never reused |
| body | TEXT NOT NULL, at least one byte | The approved or edited text, at most 2,048 UTF-8 bytes and equal to its own stripped form |
| created_at | TEXT NOT NULL | Maria's own UTC timestamp to the second, in the form `YYYY-MM-DDTHH:MM:SSZ` |
| updated_at | TEXT NOT NULL | The same form; equal to `created_at` until the summary is edited |
| context_digest | TEXT, NULL or exactly 64 characters | The owner's context consent: NULL while off, otherwise the lowercase hexadecimal SHA-256 of exactly the approved `body` |

Neither table refers to the other or to any other table. The canonical form,
the limits, and the timestamp form are enforced by the memory layer from
`core/conversation_records.py`, when a value is written and again when it is
read.

The summary body's non-empty check, `length(CAST(body AS BLOB)) > 0`, counts
the bytes the body is stored in. SQLite's `length()` of text counts only the
characters before the first NUL, so a check on it would refuse a body that
begins with NUL, which the summary text rule allows. With the byte count, such
a body is stored and read back exactly, and is shown escaped like any other
control character. Empty text and an empty blob are still refused, in UTF-8
and UTF-16 databases alike. A transcript is canonical JSON, which writes every
NUL as an escape, so its plain `length()` check needs no such care.

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
timestamp for one row, but records only a fixed, content-free response
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
Maria then separately attempts to record the interaction, with a short,
content-free response (`Deleted interaction <id>.`), as its own transaction.
When that later recording succeeds, the total interaction row count may remain
unchanged even though the selected historical content is gone; if it fails,
the deletion remains committed and no such interaction row is guaranteed.
There is no
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
limit clamped to 1 through 100, each as `id`, `fact`, and `oversized`. The
bound is on UTF-8 bytes whatever the file's text encoding, and a fact longer
than `max_fact_bytes` is returned with no text and `oversized` true. SQLite
measures a fact in the file's own encoding (`length(CAST(fact AS BLOB))`, after
reading `PRAGMA encoding`). In a UTF-8 database, which is what Maria creates,
that test is exact and an oversized fact's text never reaches Python. A
database whose file is UTF-16 is still accepted when its schema matches, and
there a fact is stored in between two thirds of and twice its UTF-8 size, so
the test admits every fact stored in at most twice the bound — never more than
`max_fact_bytes` characters — and the memory layer measures each admitted
fact's exact UTF-8 size before returning it. A `TEXT` column does not stop
SQLite storing a BLOB, so a returned fact that is not text makes the whole
read fail with the same fixed `Unable to read memory context.` error, without
the value reaching a log or a message, and the conversational turn fails
closed. A SQLite failure ends the same way: SQLite's own message for a stored
text it cannot decode quotes that text, so the memory layer logs only the fixed
line `Failed to read memory context.`, with no traceback, and raises the fixed
error only after its handler has ended, with no cause or context. Brain asks
for 17 rows —
one more than a request may carry — with a 2,048-byte bound, the most any one
memory could ever occupy, and only on the model-backed conversational path, so
Maria learns whether more enabled memories exist without counting all of them,
and an oversized memory is named as left out without its text ever being
returned.
Existing reads (`get_memory`, `list_memories`, `search_memories`) still return
exactly `id`, `fact`, and `created_at`; the consent flag never appears in
`memories` or `recall` output.

The flag is not evidence of anything but consent. `created_at` records when a
memory row was first stored, not when its text last changed, and no
`updated_at` column exists, so Maria makes no claim that an enabled memory is
current, stale, consistent, or conflicting.

Two more reads return enabled memories, shaped and bounded exactly like
`list_context_memories`, and both run only on the model-backed conversational
path. `context_memories_by_id(ids, max_fact_bytes)` rechecks at most 64
memories already supplied to the current conversation, by primary-key id; a
named memory that is disabled or gone is simply absent.
`search_context_memories(terms, after_id, limit, max_fact_bytes)` is Tier B's
memory search: it returns enabled memories whose id is above `after_id` and
that hold one of 1 to 8 casefolded terms as a literal substring, so `%` and `_`
are literal, in ascending id order, at most `limit` rows, clamped to 1 through
100. The limit bounds the rows returned, not every row SQLite examines to find
them. Brain starts the search after the 16th memory Tier A considered and only
when Tier A's read found more than 16.

A fact within the bound is matched against its current text. The statement
hands a fact to a Python matcher, registered on that read's own connection,
only inside a `CASE` whose condition is the stored-size test, and the matcher
settles the exact UTF-8 size before casefolding, so `normalized_fact` plays no
part: a copy left stale by another connection can neither add nor hide a
bounded match. A fact beyond the bound is never read into Python, so it cannot
be casefolded; for it, and only for it, `instr()` over the stored
`normalized_fact` decides, and the row comes back by id only. Maria writes
that copy with every fact she stores or updates, but cannot check it against a
fact too large to read: a copy changed outside Maria can name an oversized
memory that no longer holds the term, or miss one that does, and such a row
counts toward `limit`. An oversized memory is only ever named, never supplied.

## Note Context Consent

`context_digest` is NULL, meaning off, for every note unless the owner turns
consent on, one note at a time. The migration leaves every pre-existing note
off, and a newly created note is off because its insert never names the
column.

`enable note context <id>` and `disable note context <id>` each change at most
one row by primary-key id, after mandatory explicit confirmation, and the
recorded interaction names only the id. A missing id changes nothing and
reports `Note not found: <id>.`

Consent covers exact text, so an enable approval is bound to the title and
body as they were when confirmation was offered, and the confirmation shows the
owner both in full. At that moment `note_snapshot(id)` reads both in one
`SELECT` and returns them with their identity: the SHA-256 of both, as one
compact JSON array, so neither can trade text with the other. The text shown
and the identity pinned into the pending plan therefore come from the same
read; only the identity is kept in the plan. At execution,
`enable_note_context(id, digest)` runs one `BEGIN IMMEDIATE` transaction that
re-reads both, compares their identity with the approved one, and only on a
match runs `UPDATE notes SET context_digest = ? WHERE id = ? AND title = ? AND
body = ?`, storing that identity. Otherwise nothing changes and the owner is
asked to request confirmation again. Disabling runs one `UPDATE notes SET
context_digest = NULL WHERE id = ?` with no text check.

The stored identity is what makes consent durable against text changed
elsewhere. Every note context read recomputes the identity of the title and
body it returns and requires it to equal `context_digest`, so a note changed
through another connection that left `context_digest` in place is never
eligible. A note whose title or body is not text, or is empty, can never
match. The guard compares current text: a note changed away and back to its
approved text is eligible again, and nothing records that it ever changed.

`search_context_notes(terms, limit, max_text_bytes)` is Tier B's note search.
It considers only notes whose `context_digest` is set, whose title and body are
both text within `max_text_bytes` UTF-8 bytes, measured exactly whatever the
file's encoding, and whose title and body are still their approved text, in
ascending id order, at most `limit` rows, clamped to 1 through 100. Every one
of those checks runs inside the statement, row by row, before anything is
matched. One `CASE` settles consent, type, and stored size; only then does a
nested `CASE` hand the title, body, and `context_digest` to an approval check,
a Python function registered on that read's own connection that recomputes the
identity and compares it; and only a note it confirms is handed to the
matcher. A note matches when a term occurs wholly within its casefolded title
or wholly within its casefolded body; the two are never joined. A note changed
through another connection is therefore never matched and never counts toward
`limit`, so it cannot crowd out an approved match. A note whose title or body
is beyond the bound is never searched, returned, or named: its approved
identity cannot be verified without reading the oversized text. Each row holds
`id`, `title`, `body`, and `oversized`, which is always false for a search.
`context_notes_by_id(ids, max_text_bytes)` rechecks at most 64 notes already
supplied to the current conversation with the same approval check, applied
after the read; there a note whose text is now too large comes back by id only,
with `oversized` true, and the conversation treats it as no longer current. A
failed read logs only the fixed line `Failed to read note context.`, with no
traceback, and raises `RuntimeError("Unable to read note context.")` with no
cause or context, and neither a title, a body, a term, nor an identity is ever logged or
returned in a message.

`updated_at` records when a note was last saved, which is a fact about the
row, not evidence that its text is current or stale.

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

## Audit Summaries

An audit summary is one logical record kept in two tables: its
`audit_summaries` row and, once known, its `audit_outcomes` row. It records
that the owner approved one action the local reasoning model proposed, and
that Maria authorised that attempt for dispatch; by itself it does not prove
that the tool ran. Every model-proposed plan needs exactly one approval from
the owner, for the whole plan, even when every permission decision is
`ALLOWED`, which is why `decision` has the single value `owner_approved`.
Brain adds summaries and outcomes only when it dispatches an approved
model-proposed action. A deterministic command, a refused proposal, a plan
with a denied action (refused before any approval is offered), and a plan that
is cancelled, never approved, or refused when it is checked again at
confirmation add nothing to either table. A plan of two to five steps adds one
summary, and then its outcome, for each step Maria reached for dispatch, and
none for any step after it stopped. No column links the summaries of one plan,
so the tables alone cannot say which summaries belonged to one plan or how
many steps it held; V1-19 added no table, column, or vocabulary value, and
V1-20's step to version 6 changed neither audit table.

Every stored value is Maria's own, from the closed vocabulary in
`core/audit.py`, and there is deliberately no column for anything else. A
summary never holds the request, the model's reply or proposal, an argument
value, a plan fingerprint, memory or note text supplied as context, a root's
name, a path, a tool's result, or exception text. A directory listing is
recorded as `named_filesystem_root` whichever directory it listed. The owner
sees the exact arguments when approving and the tool's result when it runs;
neither is stored here, and neither becomes interaction history.

The write order is fixed. Once the owner approves and the plan passes its
checks again, Brain calls `record_audit_summary(action, decision, target)`,
which inserts the summary with Maria's own timestamp, validated like every
other field, and commits it before returning the new id. Only then is the tool
dispatched, exactly once. For a plan of one step, if the
summary cannot be committed, or the returned id is not an exact positive int,
nothing is dispatched, the approval stays spent, and the owner sees `Maria did
not run the approved action because its audit summary could not be
recorded.` In a plan of several steps each step follows this order in turn,
and a summary that cannot be committed for one step leaves that step not run,
the steps before it standing, and the plan stopped; its report says so in that
step's block and in its closing sentence. After the tool returns, `record_audit_outcome(summary_id,
outcome)` appends `succeeded` or `failed`, from the tool's own success flag,
with one statement:

```sql
INSERT INTO audit_outcomes (summary_id, outcome)
SELECT ?, ?
WHERE EXISTS (SELECT 1 FROM audit_summaries WHERE id = ?);
```

The existence check and the insert are the same statement, so no outcome is
ever stored for a summary that no longer exists, and because `summary_id` is
the primary key, a second outcome for one summary is refused. The tool's
result is then shown, followed by `Recorded as audit summary <id>.` or, when
the outcome could not be appended, by `Recorded as audit summary <id>, but its
outcome could not be recorded, so that summary shows the outcome as
unknown.` Nothing is retried. A summary without an outcome, because that write
failed or because Maria stopped before or during the tool's run, reads as
`Outcome not recorded; execution is unknown.`, and Maria never reruns the
tool to find out. In a plan of several steps, a step reached for dispatch may
keep its summary without an outcome even when its tool never ran, if the run
stopped before dispatch. A write interrupted while a plan of several steps
runs may already have committed, so Maria then tells the owner only that she
could not confirm whether it was recorded. No statement updates a summary or
an outcome once it is written; the only later change is deletion.

`list_audit_summaries(limit)` returns summaries newest id first, each with its
outcome or None, through a `LEFT JOIN` of `audit_outcomes` onto
`audit_summaries`, and `get_audit_summary(id)` returns one summary the same
way, or None when no summary has that id; an id larger than SQLite's largest
integer, which no row can have, is answered None without opening the
database. Ordering is by `id` alone. The limit
must be an exact int from 1 to 21 and is never clamped; `audit` asks for 21 so
that it can show the newest 20 and add one line saying that more exist,
without counting them. Every value read back is validated again: the id must
be an exact positive int, the timestamp exact text in the one form naming a
real calendar second, the action one of the three identifiers, and the
decision, target, and outcome exact text spelling a member of their
vocabulary, or no outcome at all. One value outside it fails the whole read
with `Unable to list audit summaries.` or `Unable to read audit summary.`, and
neither that value nor any other is returned, shown, or logged.

`delete audit <id>` removes one logical summary, after mandatory explicit
confirmation that configuration cannot remove, and with no preview of stored
content beforehand; its fixed notice says that the deletion does not undo the
action and that Maria's operational log may still show that a model-proposed
action ran. `delete_audit_summary(id)` runs one `BEGIN IMMEDIATE`
transaction: `DELETE FROM audit_summaries WHERE id = ?`, then, only when that
removed a row, `DELETE FROM audit_outcomes WHERE summary_id = ?`. The summary
and its outcome are removed together or, on any failure, not at all. A
missing id returns False and changes nothing, and the tool reports `Audit
summary not found: <id>.`; an id larger than SQLite's largest integer is
answered the same way without opening the database, though the planner
already refuses such an id before any step, so only a direct caller can pass
one. For Maria's implicit-id
inserts, SQLite
AUTOINCREMENT does not reuse the id of a previously committed summary, even
after the newest summary or every summary has been deleted, so a later
summary can never inherit an earlier one's outcome, and an outcome written
after its summary was deleted is refused by the existence check above. This
is the one deletion that spans two tables, because an outcome is part of its
summary rather than a separate record type; it never cascades into
`interactions`, `notes`, or `memories`. Neither table declares a foreign key,
so these guarantees rest on Maria's own statements: a change made outside
Maria is not prevented, but an outcome row with no summary is never listed or
read.

The audit tool, `tools/audit.py`, reaches these tables only through those
`Memory` methods. `audit` and `read audit <id>` change no audit summary and
are allowed without confirmation by default. No audit action can be proposed
by the model, and audit summaries are never read for planning or reasoning or
supplied as model context. The three commands are ordinary deterministic
commands, recorded as interactions like any other, but no recorded response
repeats a summary beyond its id: `audit` records `Listed audit summaries.`
and `read audit <id>` records `Displayed audit summary <id>.` in place of
what they showed, a deletion records its reply, `Deleted audit summary <id>.`,
and a failure records its fixed message. Deleting an audit summary leaves
those interaction rows in place, and deleting an interaction leaves every
audit summary in place.

Each audit method validates its arguments before it opens the database. An
action, decision, target, or outcome outside the vocabulary raises
`ValueError("An audit summary may hold only values from Maria's audit
vocabulary.")`, an id that is not an exact positive int, including `True`,
raises `ValueError("Audit summary id must be a positive whole number.")`, and
a listing limit that is not an exact int from 1 to 21 raises its own fixed
`ValueError`. Every database failure raises a fixed `RuntimeError` with no
cause and no context: `Database is busy. Try again shortly.` for busy or
locked contention, and otherwise `Unable to record audit summary.`, `Unable to
record audit outcome.`, `Unable to list audit summaries.`, `Unable to read
audit summary.`, or `Unable to delete audit summary.` An insert that adds no
row, and a summary insert that returns no valid id, fail the same way. The log
receives one fixed line, `Failed to <operation>: <reason>.`, whose reason is
`database busy`, `database error`, `no row was inserted`, `no valid id was
returned`, or `a stored value is outside the audit vocabulary`, with no
traceback and no SQLite text. A successful write logs only its id: `Recorded
audit summary <id>.`, `Recorded the outcome of audit summary <id>.`, or
`Deleted audit summary <id>.`

## Saved Conversations and Conversation Summaries

A saved conversation is one `saved_conversations` row written by an explicit
`save conversation`: the canonical compact JSON transcript of exactly the
exchanges the session retained, oldest first, as `[[user, assistant], ...]`
with no spaces between tokens and non-ASCII text kept as itself.
`save_conversation(transcript)` checks, before the database is opened, that
the text is exactly that canonical form for 1 to 8 pairs of non-empty text and
at most 8,192 UTF-8 bytes of it, and then runs one statement and reads
nothing:

```sql
INSERT INTO saved_conversations (created_at, transcript) VALUES (?, ?);
```

A row is never updated, and saving the same exchanges twice stores two rows.
`list_saved_conversations(limit)` and `get_saved_conversation(id)` read rows
newest id first, or by id, and decode every transcript again: a stored text in
any other form, or a timestamp or id Maria could not have written, fails the
whole read with a fixed message. `delete_saved_conversation(id)` removes one
row with `DELETE FROM saved_conversations WHERE id = ?`. A saved conversation
has no consent column and no context method ever reads one.

A conversation summary is one `conversation_summaries` row.
`create_conversation_summary(body)` stores text the owner approved, after
checking that it is non-empty, at most 2,048 UTF-8 bytes, and equal to its own
stripped form; both timestamps are Maria's own and `context_digest` starts
NULL. `edit_conversation_summary(id, new_body)` runs two statements in one
transaction, first clearing consent with
`UPDATE conversation_summaries SET context_digest = NULL WHERE id = ? AND
context_digest IS NOT NULL`, then writing the new body and `updated_at`, and
reports whether consent was cleared, so no other connection can enable consent
between the two and a failure rolls both back.
`delete_conversation_summary(id)` removes one row.
`enable_conversation_summary_context(id, digest)` takes the write lock with
`BEGIN IMMEDIATE`, reads the body, and stores the digest only when it is
exactly the SHA-256 of that body, through
`UPDATE ... SET context_digest = ? WHERE id = ? AND body = ?`; otherwise
nothing changes and the outcome says the text changed.
`disable_conversation_summary_context(id)` sets the digest to NULL. A summary
reads as context-enabled only while its stored digest is the identity of
exactly its current body.

`search_context_summaries(terms, limit, max_text_bytes)` and
`context_summaries_by_id(ids, max_text_bytes)` are the note context reads
applied to one text. Consent, type, and stored size are checked inside SQLite
first; then a function registered on that connection recomputes the approved
identity from the current body; only then does the literal matcher see it. In
a UTF-16 database the stored bound is twice the UTF-8 bound and each admitted
body's exact UTF-8 size is settled afterwards, so the bound is the same in
either encoding. A summary changed outside Maria never matches and never
counts toward a read's limit.

Listings read 21 rows, so the tools can show the newest 20 and say that more
exist. An id that is not an exact positive int raises `ValueError` with a
fixed message before the database is opened, and an id larger than SQLite's
largest integer is answered as absent without opening it. Every database
failure raises a fixed `RuntimeError` with no cause and no context:
`Database is busy. Try again shortly.` for contention, which these methods
classify themselves, as the audit methods do, and otherwise
`Unable to <operation>.` The log receives one fixed line,
`Failed to <operation>: <reason>.`, whose reason is `database busy`,
`database error`, `no row was inserted`, `no valid id was returned`, or
`a stored value is not one Maria can show`, and a successful write logs only
its id. No transcript, body, digest, or SQLite text is ever logged.

The two tools, `tools/conversations.py` and `tools/conversation_summaries.py`,
reach these tables only through those `Memory` methods. Listings and readings
are recorded in interaction history as fixed labels, and every other reply
names at most an id and a count; an `edit summary` request keeps its new text
in the request half, as a note edit does. Deleting either record never
cascades, and AUTOINCREMENT keeps a deleted id from being reused.

## Data Considerations

Interaction history, notes, explicit memories, audit summaries, saved
conversations, and conversation summaries are distinct persistent concepts and
use separate tables in the same configured database; an audit summary spans
two of them, `audit_summaries` and `audit_outcomes`. The same
text may nevertheless appear more than once: an explicit fact can occur in its
`remember` request, the `memories` table, and later recall or list responses;
a note body can remain in an interaction response as well as the `notes` table.
Likewise, file or note content returned in a response remains independent of
its original source. Removing or changing source data outside Maria does not
automatically remove matching text from existing interaction rows. An audit
summary holds none of that text, while a saved conversation or a summary can
repeat any of it. All of these tables share one database file; external
removal or replacement affects all six datasets.

In the current implementation, `delete interaction <id>`,
`delete note <id>`, and `forget memory <id>` each target at most one row by ID
and remove it when found, `delete audit <id>` removes at most one audit
summary, its summary row together with any outcome row,
`delete conversation <id>` and `delete summary <id>` each remove at most one
row, and `retitle note <id>`, `edit note <id>`, `update memory <id>`, and
`edit summary <id>` each change one stored row; Maria still exposes no API or
CLI operation to prune or bulk-delete interaction history, bulk-delete or
clear notes, bulk-delete or clear explicit memories, bulk-delete or clear
audit summaries, or bulk-delete or clear saved conversations or summaries.
Removing one record never
cascades into another record type, so forgetting a memory leaves any copy of
its text that already exists in `interactions` untouched, and deleting an
audit summary leaves the rows the audit commands recorded in `interactions`
untouched. It also provides
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
from an interaction row. In the other direction, deleting an audit summary
removes nothing from the log, which may still show that a model-proposed
action ran, naming its action and the summary's id.

Do not submit or request display of passwords, API keys, access tokens, private
keys, recovery codes, confidential credentials, or other highly sensitive
secrets. In particular, reading a sensitive file or note can copy its returned
content into interaction history, saving a conversation copies whatever its
exchanges said, and enabling a note, memory, or summary for context lets its
entire text reach the local reasoning model: Maria does not check consented
or saved text for secrets and cannot tell sensitive text from any other.

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

## Failures That Could Carry Stored Text

SQLite's own error message can quote stored text. A `TEXT` value that is not
valid UTF-8, which only a change made outside Maria can store, makes Python's
decode error repeat it, and a trigger's `RAISE` can repeat the text an
`INSERT`, `UPDATE`, or `DELETE` writes or removes. Wherever such an error is
logged with its traceback, or travels with the raised failure as its cause or
context, that text reaches a log or an error.

So no method that reads or writes a stored record lets it. Every such method
catches only `sqlite3.Error`, logs one fixed line with no traceback, and
raises its fixed `RuntimeError` only after the handler has ended, so the
failure has no cause and no context. That covers the interaction methods
(`record_interaction`, `get_recent_interactions`, `get_interaction`,
`search_interactions`, `delete_interaction`), the memory methods
(`store_memory`, `get_memory`, `search_memories`, `list_memories`,
`update_memory`, `forget_memory`, and every memory consent and context
method), and the note methods (`create_note`, `list_notes`, `get_note`,
`retitle_note`, `edit_note`, `delete_note`, and every note consent and context
method), with their `_reporting_consent` forms. Their SQL, transactions,
rollback and closing, success logging, recognised uniqueness conflicts, and
owner-visible messages are unchanged. The audit methods
(`record_audit_summary`, `record_audit_outcome`, `list_audit_summaries`,
`get_audit_summary`, and `delete_audit_summary`) and every saved-conversation
and conversation-summary method follow the same rule, as described under
"Audit Summaries" and "Saved Conversations and Conversation Summaries" above.

Busy/locked contention is still converted as described above, and outside the
audit and conversation-record methods that conversion still logs SQLite's busy
exception and attaches it, because its message is SQLite's own fixed text.
Those methods classify contention themselves: they raise the same busy
message, but log only their fixed line and attach nothing. Initialization and `schema_version()`
never read a stored record's text; they record only their fixed event codes,
as described under "What initialization logs", and keep SQLite's exception as
the cause for a caller that needs it.

## Ownership

Only `core/memory.py` should open SQLite connections or execute SQL. Brain
writes audit summaries, and `tools/audit.py` lists, reads, and deletes them,
only through `Memory`'s audit methods; `core/audit.py` holds the audit
vocabulary that the memory layer, Brain, and the audit tool share, and
executes no SQL. `tools/conversations.py` and
`tools/conversation_summaries.py` reach their tables only through `Memory`'s
conversation-record methods, and `core/conversation_records.py` holds the
vocabulary they, the memory layer, and the conversation module share, and
executes no SQL.
