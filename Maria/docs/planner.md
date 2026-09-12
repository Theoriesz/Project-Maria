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

## Note Lifecycle Grammar

Note lifecycle commands use strict command syntax. Command keywords are
case-insensitive, while operand casing and internal spacing are preserved and
only outer whitespace is stripped.

`retitle note <id>: <new title>` creates this step:

```text
PlanStep(
    tool_name="notes",
    action="retitle",
    arguments={"id": note_id, "title": new_title},
)
```

`edit note <id>: <new body>` creates this step:

```text
PlanStep(
    tool_name="notes",
    action="edit",
    arguments={"id": note_id, "body": new_body},
)
```

`delete note <id>` creates this step:

```text
PlanStep(
    tool_name="notes",
    action="delete",
    arguments={"id": note_id},
)
```

All three take a positive numeric ID only, never a title, and reuse `read
interaction`'s exact `id` grammar and validation, unmodified: `id` must be one
or more ASCII digits (`0`-`9`) with a resulting value of at least 1, and
leading zeroes are accepted (`007` parses to `7`). Unicode digit lookalikes,
signs, decimals, embedded whitespace, and any other non-digit character are
rejected. There is no title lookup, recent-context inference, or fuzzy matching
for these mutations.

`retitle note` and `edit note` split the operand on the first colon only, the
same way `create note` does, so later colons stay literal inside the new title
or body. `retitle note 005: New: Title` therefore sets the title to `New:
Title` for note `5`.

A bare command, a missing colon, or an empty or whitespace-only new title or
body returns the matching usage response:

```text
Use: retitle note <id>: <new title>
Use: edit note <id>: <new body>
Use: delete note <id>
```

Empty content is deliberately rejected before any confirmation prompt, because
a blank title or body can never be stored; there is no implicit `Untitled`
fallback for these mutations and no way to clear a note's body.

An invalid ID returns:

```text
Note id must be a positive whole number.
```

Because the operand after each fixed prefix is parsed as one value, an embedded
extra token (for example `delete note 1 extra` or `retitle note 5 extra:
Title`) is an invalid ID, not a separate unsupported command. A blank ID with
non-empty content, such as `retitle note : Title`, is likewise an invalid ID.
`notes.retitle`, `notes.edit`, and `notes.delete` all require mandatory
explicit confirmation before they execute; see `docs/architecture.md`'s
Permission Policy section.

Near-prefix, plural, and bulk variants such as `retitle notes <id>`, `edit
notes <id>`, `delete notes <id>`, `delete note1`, `clear notes`, and `purge
note <id>` remain unsupported and receive the planner's existing generic
unsupported-command response.

`rename note` is not a note lifecycle command. Text beginning with `rename `
is matched earlier by the existing filesystem grammar, so `rename note 1 to
title` produces a `filesystem.rename` step from its `<source> to
<destination>` form and never routes to the notes tool. Retitling is therefore
spelled `retitle`, which avoids that collision.

## Interaction History Grammar

Interaction-history commands use strict command syntax. Command keywords are
case-insensitive, while the search query keeps its original casing and
internal spacing after surrounding whitespace is removed.

The exact command `history` creates this step:

```text
PlanStep(
    tool_name="history",
    action="list",
    arguments={},
)
```

`search history <query>` creates this step:

```text
PlanStep(
    tool_name="history",
    action="search",
    arguments={"query": query},
)
```

`read interaction <id>` creates this step:

```text
PlanStep(
    tool_name="history",
    action="read",
    arguments={"id": interaction_id},
)
```

`id` must be one or more ASCII digits (`0`-`9`) with a resulting value of at
least 1; leading zeroes are accepted (`007` parses to `7`). Unicode digit
lookalikes, signs, decimals, embedded whitespace, and any other non-digit
character are rejected. A bare or whitespace-only `search history` returns:

```text
Use: search history <query>
```

A bare or whitespace-only `read interaction` returns:

```text
Use: read interaction <id>
```

An invalid `read interaction` ID returns:

```text
Interaction id must be a positive whole number.
```

`delete interaction <id>` creates this step:

```text
PlanStep(
    tool_name="history",
    action="delete",
    arguments={"id": interaction_id},
)
```

`delete interaction <id>` reuses `read interaction`'s exact `id` grammar and
validation, unmodified. A bare or whitespace-only `delete interaction`
returns:

```text
Use: delete interaction <id>
```

An invalid `delete interaction` ID returns the same message as an invalid
`read interaction` ID:

```text
Interaction id must be a positive whole number.
```

Because the operand after the fixed `delete interaction ` prefix is parsed as
one value, an embedded extra token (for example `delete interaction 1
extra`) is an invalid ID, not a separate unsupported command. `history.delete`
requires mandatory explicit confirmation before it executes; see
`docs/architecture.md`'s Permission Policy section.

Near-prefix and plural variants such as `histories`, `history list`,
`search historyfoo`, `read interactions <id>`, `delete interactions <id>`,
and `delete interaction1` remain unsupported and receive the planner's
existing generic unsupported-command response.

## Plan Shape

A plan contains:

- The original request
- Zero or more steps
- An optional response when no action should run

Each step names a tool, an action, and JSON-compatible arguments.

## Future Direction

Future versions may support richer planning. Any expansion should preserve the
planner's single responsibility: producing plans, not executing them.
