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
system
time
```

File rename and move operations require explicit confirmation. Delete
functionality is intentionally not implemented in version 0.1.

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
