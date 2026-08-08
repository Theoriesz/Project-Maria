# Planner

The planner converts explicit user requests into executable plans.

## Version 0.1 Behavior

The planner is command-oriented. It does not attempt general reasoning,
autonomous decomposition, or long-running task execution.

Supported command families:

- Calculator commands
- Safe filesystem commands
- Notes commands
- Explicit persistent-memory commands
- Read-only system information commands

## Memory Command Grammar

Memory commands use strict command syntax. Command keywords are
case-insensitive, while fact and query text keep their original casing after
surrounding whitespace is removed.

`remember <fact>` creates this step:

```text
PlanStep(
    tool_name="memory",
    action="store",
    arguments={"fact": fact},
)
```

`recall <query>` creates this step:

```text
PlanStep(
    tool_name="memory",
    action="search",
    arguments={"query": query},
)
```

The exact command `memories` creates this step:

```text
PlanStep(
    tool_name="memory",
    action="list",
    arguments={},
)
```

One or more whitespace characters may separate `remember` or `recall` from
its operand. A bare command creates no executable step and returns the matching
usage response:

```text
Use: remember <fact>
Use: recall <query>
```

The planner does not infer commands from recent interaction context. Requests
such as `please remember ...`, `what do you remember about ...`, and
`memories extra` remain unsupported.

## Plan Shape

A plan contains:

- The original request
- Zero or more steps
- An optional response when no action should run

Each step names a tool, an action, and JSON-compatible arguments.

## Future Direction

Future versions may support richer planning. Any expansion should preserve the
planner's single responsibility: producing plans, not executing them.
