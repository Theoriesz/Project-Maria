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

## Supported v0.1 Commands

```text
calculate 2 + 2
list .
read file README.md
rename old.txt to new.txt
move source.txt to docs/source.txt
create note Idea: Keep the foundation simple.
notes
read note 1
remember My editor is VS Code
recall editor
memories
system
time
```

File rename and move operations require explicit confirmation. Delete
functionality is intentionally not implemented in version 0.1.

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

Memory commands and their responses are also recorded in Maria's general
interaction history, so a fact can appear in both places. Do not store
passwords, API keys, access tokens, or other highly sensitive secrets. Version
0.1 has no forget or delete-memory command.

## Tests

From the `Maria` directory:

```text
python -m unittest discover -s tests -v
```

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
