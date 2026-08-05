# Architecture

Maria v0.1 is organized around a small set of modules with clear ownership.

## Core Flow

1. `main.py` receives user input from the command line.
2. `brain.py` coordinates the request.
3. `planner.py` converts explicit commands into a plan.
4. `permissions.py` decides whether each action is allowed.
5. `tool_manager.py` executes loaded tools.
6. `memory.py` stores interactions and notes in SQLite.
7. `logger.py` records important application events.

## Boundaries

The brain does not run SQL or perform tool work directly. The planner does not
execute tools. Tools do not decide global permissions. The memory module is the
only module that opens SQLite connections.

## Version 0.1 Scope

This foundation intentionally excludes autonomous agents, voice, browser
automation, internet search, and user interfaces beyond the command line. Future
versions can add richer capabilities by extending the planner, tool manager, and
tool set without rewriting the foundation.
