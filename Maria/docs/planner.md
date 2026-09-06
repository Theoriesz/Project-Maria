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

## Note Creation Grammar

`create note <title>: <body>` requires an explicit colon separator between
the title and the body. Only the first colon is treated as the separator, so
body text containing additional colons (including URLs) is preserved intact.

`create note : <body>` is the explicit way to create an untitled note; the
title is stored as `Untitled`.

When operand text follows the note command but contains no colon, the
planner creates no executable note step and returns the matching usage
response instead. For example, `create note Project Plan` returns:

```text
Use: create note <title>: <body>
```

The truly bare `create note` command (no operand text at all) is unaffected
by this grammar and keeps the planner's existing generic unsupported-command
response.

## Plan Shape

A plan contains:

- The original request
- Zero or more steps
- An optional response when no action should run

Each step names a tool, an action, and JSON-compatible arguments.

## Future Direction

Future versions may support richer planning. Any expansion should preserve the
planner's single responsibility: producing plans, not executing them.
