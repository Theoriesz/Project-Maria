# Maria

Maria is a modular personal AI assistant foundation written in Python.

Version 0.1 focuses on architecture, safety, and maintainability. It is not a
prototype of the final assistant and does not include advanced AI features.

## Requirements

- Python 3.11 or newer
- SQLite, included with Python

## Setup

From the `Maria` directory:

```text
python -m pip install -r requirements.txt
python main.py
```

## CLI Exit and Interruption Behavior

Normal CLI completion, including `exit`, `quit`, `q`, EOF, or Ctrl+C at the
main prompt, returns status 0. A startup failure prints a stable, sanitized
message to stderr and returns status 1; startup Ctrl+C prints an interruption
message and returns status 130. Startup failures do not print raw tracebacks or
exception details. Review the configuration and available logs when startup
fails.

At a confirmation prompt, `y` or `yes` approves and `n` or `no` cancels the
pending action, ignoring casing and surrounding whitespace. Blank or
unrecognized input re-prompts instead of approving or cancelling. EOF or
Ctrl+C cancels the pending action immediately, without executing it, even
after earlier invalid input. Ctrl+C returns to the main prompt; if input
remains closed after EOF, the next main prompt exits cleanly.

## Supported Commands

```text
calculate 2 + 2
list .
read file README.md
rename old.txt to new.txt
move source.txt to docs/source.txt
create note Idea: Keep the foundation simple.
notes
read note 1
retitle note 1: Clearer idea
edit note 1: Keep the foundation simple and reviewable.
delete note 1
remember My editor is VS Code
recall editor
memories
system
time
history
search history editor
read interaction 1
delete interaction 1
```

File rename and move operations, `delete interaction <id>`, and the three note
lifecycle commands `retitle note <id>: <new title>`, `edit note <id>: <new
body>`, and `delete note <id>` require explicit confirmation. `delete
interaction <id>` targets at most one interaction row by ID and removes it
when found; it never cascades to notes or memories. Each note lifecycle
command likewise targets at most one note by positive whole-number ID only,
never by title, and never cascades into interaction history or explicit
memories.
Retitling and editing are deliberately separate commands, so neither can
overwrite the other's field. Filesystem
delete functionality remains intentionally not implemented in version 0.1.

## Persistent Memory

Maria stores deliberately remembered facts in SQLite, so they remain available
after the application restarts. The supported syntax is strict:

- `remember <fact>` stores one fact.
- `recall <query>` performs a literal, case-insensitive substring search.
- `memories` lists the newest stored facts.

Command keywords are case-insensitive, and surrounding whitespace is removed
from facts and queries without changing their display casing. `remember` and
`recall` must start the request directly, while `memories` must be the complete
command. Conversational forms such as `please remember ...` are not supported.

```text
remember My editor is VS Code
Remembered memory 1: My editor is VS Code

recall editor
1: My editor is VS Code

remember MY EDITOR IS VS CODE
Already remembered as memory 1: My editor is VS Code

memories
1: My editor is VS Code
```

After restarting Maria, the fact remains available:

```text
recall editor
1: My editor is VS Code
```

Memory commands and their responses also follow the interaction-history
behavior described below, so a fact can appear in both places.

## Data Retention and Privacy

Maria v0.1 stores most non-empty requests it processes and their final response
text as persistent interaction history in its configured SQLite database,
located inside the project directory by default. This can retain file contents
returned by `read file`, note titles and bodies, note creation, retitle, and
edit requests,
calculator expressions and results, explicit memory facts and their
recall/list output, filesystem paths, system information, and failure or denial
messages. Asking Maria to display a sensitive file or note can therefore copy
that returned content into interaction history.

Interaction history, notes, and facts deliberately saved with `remember` are
three separate record types in the same SQLite database, although the same text
can appear in more than one table. The configured operational log
(`logs/maria.log` by default) is a separate store for application events,
warnings, and failures rather than the interaction store. Avoiding full content
in an operational log message does not mean that content is absent from SQLite
interaction history.

Maria currently provides no application-level database encryption, automatic
retention period, or pruning. `history`, `search history <query>`, and
`read interaction <id>` provide read-only listing, searching, and reading of
interaction history; they do not modify or remove any row. `delete interaction
<id>` targets at most one interaction row by ID and removes it when found,
after mandatory explicit confirmation, with no cascade to notes or memories
and no content preview before confirmation. Deletion commits independently,
and Maria then separately attempts to record the short response `Deleted
interaction <id>.` as a new interaction-history row; when that later
recording succeeds, deleting one old row and adding that audit row may leave
the total interaction-row count unchanged, and if it fails, the deletion
remains committed with no audit row guaranteed. There is still no CLI
command to clear, bulk-delete, or prune interaction history, and no
memory-forget command. Retrieval limits affect only how many
rows are read; they do not
remove stored rows, and restarting Maria does not clear them. Protection
depends on access to the machine, user account, and database file and on any
storage encryption in use.

`retitle note <id>: <new title>`, `edit note <id>: <new body>`, and `delete
note <id>` each target at most one note row and change or remove it when
found, after mandatory explicit
confirmation, with no content preview before confirmation. Retitle replaces
only the title, edit replaces only the body, and both preserve `created_at`
while refreshing `updated_at`; delete removes the matching row. An ID that
matches no note changes or removes nothing and returns a not-found result.
None of
them cascade into interaction history or explicit memories, renumber remaining
note IDs, or offer bulk, wildcard, clear-all, or automatic deletion, and there
is no undo, recovery, or note revision history. Each mutation commits
independently, and Maria then separately attempts to record a short,
content-free response (`Retitled note <id>.`, `Edited note <id>.`, or `Deleted
note <id>.`); if that later recording fails, the mutation remains committed
with no audit row guaranteed. The command text itself is still recorded as the
request half of interaction history, so a retitle command retains its new
title and an edit command retains its new body there, and a prior `read note`
may already have copied that note's content into interaction history.
Afterwards changing or deleting the note does not rewrite or erase those
independent interaction rows; they can be removed separately with `delete
interaction <id>`. As elsewhere, Maria promises no secure erasure from SQLite
file pages, database or filesystem backups, Git history, or external copies.

The shipped v0.1 runtime has no built-in networking or cloud integration and
does not itself transmit this history. Configured network-mounted or
synchronized paths and operating-system backup or sync services remain outside
Maria's boundary.

Avoid submitting or requesting display of passwords, API keys, access tokens,
private keys, recovery codes, confidential credentials, or other highly
sensitive secrets. Blank input and CLI-only exit or cancelled-confirmation
paths are not completed interaction records; detailed recording boundaries are
documented in `docs/architecture.md`.

## Tests

Run the standard-library test suite from the nested `Maria` application
directory:

```text
python -m unittest discover -s tests -v
```

Tests use temporary databases, log directories, and filesystem roots, so
production runtime files are not required. The suite covers the CLI,
configuration, permissions, calculator, memory, Logger, Tool Manager, notes,
filesystem, system information, Brain, Planner, and clean startup wiring.

## Project Layout

- `core/`: application coordination, planning, memory, permissions, logging,
  configuration, and constants
- `tools/`: built-in tools
- `database/`: SQLite database
- `config/`: TOML configuration
- `prompts/`: starter prompt text
- `logs/`: runtime logs
- `docs/`: architecture and development documentation

## Scope

Maria v0.1 intentionally excludes internet search, browser automation, voice,
text-to-speech, autonomous agents, multi-user support, and plugin marketplaces.
