# Planner

The planner converts explicit user requests into executable plans.

## Version 0.1 Behavior

The planner is command-oriented. It does not attempt general reasoning,
autonomous decomposition, or long-running task execution.

Supported command families:

- Calculator commands
- Safe filesystem commands
- Notes commands
- Read-only system information commands

## Plan Shape

A plan contains:

- The original request
- Zero or more steps
- An optional response when no action should run

Each step names a tool, an action, and JSON-compatible arguments.

## Future Direction

Future versions may support richer planning. Any expansion should preserve the
planner's single responsibility: producing plans, not executing them.
