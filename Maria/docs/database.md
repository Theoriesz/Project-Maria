# Database

Maria v0.1 uses SQLite through `core/memory.py`.

## File

The default database path is `database/maria.db`.

## Tables

### interactions

Stores recent user interactions.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER | Primary key |
| user_input | TEXT | User request |
| response | TEXT | Maria's response |
| created_at | TEXT | UTC ISO 8601 timestamp |

### notes

Stores simple notes created by the notes tool.

| Column | Type | Purpose |
| --- | --- | --- |
| id | INTEGER | Primary key |
| title | TEXT | Note title |
| body | TEXT | Note body |
| created_at | TEXT | UTC ISO 8601 timestamp |
| updated_at | TEXT | UTC ISO 8601 timestamp |

## Ownership

Only `core/memory.py` should open SQLite connections or execute SQL.
