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

Stores user requests and Maria responses. The application can retrieve a recent
subset as context.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER | Autoincrementing primary key |
| user_input | TEXT NOT NULL | User request |
| response | TEXT NOT NULL | Maria's response |
| created_at | TEXT NOT NULL | UTC ISO 8601 timestamp |

### notes

Stores simple notes created by the notes tool.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER | Autoincrementing primary key |
| title | TEXT NOT NULL | Note title |
| body | TEXT NOT NULL | Note body |
| created_at | TEXT NOT NULL | UTC ISO 8601 timestamp |
| updated_at | TEXT NOT NULL | UTC ISO 8601 timestamp |

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

## Memory Retrieval

`recall <query>` strips and case-folds the query, then uses SQLite `instr()` to
perform literal substring matching against `normalized_fact`. Percent (`%`) and
underscore (`_`) are literal characters, not wildcard operators. Search is not
semantic, fuzzy, ranked, or model-based.

Search and list results contain `id`, `fact`, and `created_at`. Both operations
order rows by `created_at` descending and then `id` descending. The CLI normally
returns at most 20 rows. Direct `search_memories()` and `list_memories()` calls
default to 20 and clamp requested limits to the range 1 through 100.

Literal substring retrieval scans the stored normalized text and is intended
for a small personal database. No update, deletion, or `forget` operation exists
for memories in v0.1.

## Data Considerations

The brain records memory commands and Maria's responses in `interactions` like
other normal requests. A deliberately stored fact can therefore appear in both
the `memories` table and interaction history. Passwords, API keys, access tokens,
and other highly sensitive secrets should not be stored as memories.

## Ownership

Only `core/memory.py` should open SQLite connections or execute SQL.
