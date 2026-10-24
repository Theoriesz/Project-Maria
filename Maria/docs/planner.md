# Planner

The planner converts explicit user requests into executable plans.

## Planner Behavior

The planner is command-oriented. It does not attempt general reasoning,
autonomous decomposition, or long-running task execution.

Supported command families:

- Calculator commands
- Safe filesystem commands
- Notes commands
- Explicit persistent-memory commands
- Interaction-history commands
- Audit-summary commands
- Saved-conversation and conversation-summary commands
- Task and reminder commands
- Project, project fact, and project file commands
- Read-only system information commands

Input that matches no command receives the planner's generic no-action
response:

```text
I do not have an executable plan for that yet. Type 'help' to see supported commands.
```

The planner knows nothing about conversation or memory context. Besides the
request text it receives, by keyword only, the request's `TimingContext`,
the one reference instant Brain read and the configured time zone key, which
only the task and reminder commands read; see "Task and Reminder Grammar".
When
reasoning is enabled, Brain answers exactly that generic fallback with the
local model's reply instead of showing it. A reply whose first character after
any spaces, tabs, carriage returns, or line feeds is `{` is treated as a
proposal, of one action or of two to five actions in a `steps` envelope, which
Brain, never the planner, hands to `core/plan_compiler.py`: it either becomes
a model-originated plan of one to five steps that runs only after the owner
approves it, as a whole, or is refused with a fixed message. Any other reply
stays a conversational text reply. Nothing from such a turn, its request
included, is recorded as interaction history, and each approved step runs
only after Maria has committed a content-free audit summary for it, the only
trace it leaves in Maria's database. Help, every
usage response, and every command never reach the model. See
"Deterministic-First Conversation", "Model-to-Plan Compilation Gate", and
"Model-Originated Execution and Audit" in `docs/architecture.md`, and "Plan
Shape" below.

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

The planner receives the request text and nothing else: no interaction
history, stored record, or memory context. Requests such as `please remember
...`, `what do you remember about ...`, and `memories extra` remain
unsupported. With reasoning enabled they go to the local model instead, whose
reply can draw only on memories and notes the owner has enabled for context.
That reply never stores a memory: no memory action is open to the model, so a
proposal naming one is refused with a fixed message.

## Memory Lifecycle Grammar

Memory lifecycle commands use strict command syntax. Command keywords are
case-insensitive, while operand casing and internal spacing are preserved and
only outer whitespace is stripped.

`update memory <id>: <new fact>` creates this step:

```text
PlanStep(
    tool_name="memory",
    action="update",
    arguments={"id": memory_id, "fact": new_fact},
)
```

`forget memory <id>` creates this step:

```text
PlanStep(
    tool_name="memory",
    action="forget",
    arguments={"id": memory_id},
)
```

Both take a positive numeric ID only, never fact text, and reuse `read
interaction`'s exact `id` grammar and validation, unmodified: `id` must be one
or more ASCII digits (`0`-`9`) with a resulting value from 1 to
9223372036854775807, and leading zeroes are accepted however many there are
(`007` parses to `7`). A larger value, Unicode digit lookalikes
(including full-width `１２`, Arabic-Indic `١٢`, and superscript `²`), signs,
decimals, embedded whitespace, and any other non-digit character are rejected.
There is no fact-text lookup, recent-context inference, or fuzzy matching for
these mutations.

`update memory` splits the operand on the first colon only, the same way
`create note` and `edit note` do, so later colons stay literal inside the new
fact. `update memory 005: New: fact` therefore sets the fact to `New: fact` for
memory `5`.

A bare command, a missing colon, or an empty or whitespace-only new fact
returns the matching usage response:

```text
Use: update memory <id>: <new fact>
Use: forget memory <id>
```

The emptiness check runs before ID parsing, so an invalid ID combined with
empty content (for example `update memory abc:`) returns the usage response
rather than the invalid-ID response. Empty content is deliberately rejected
before any confirmation prompt, because a blank fact can never be stored and
there is no way to clear a memory's text. The planner never returns `New memory
fact is required.`; that exact message belongs to direct `MemoryTool` and
`Memory` validation.

An invalid ID returns:

```text
Memory id must be a positive whole number.
```

Because the operand after each fixed prefix is parsed as one value, an embedded
extra token (for example `forget memory 1 extra` or `update memory 5 extra:
Fact`) is an invalid ID, not a separate unsupported command. A blank ID with
non-empty content, such as `update memory : Fact`, is likewise an invalid ID.
`memory.update` and `memory.forget` both require mandatory explicit
confirmation before they execute; see `docs/architecture.md`'s Permission
Policy section.

Only a literal ASCII space separates each prefix from its operand, so `update
memory\t5: Fact` is unsupported while extra spaces are tolerated. This matches
the note lifecycle grammar and differs from `remember` and `recall`, which
accept any whitespace separator.

Near-prefix, plural, and bulk variants such as `update memories <id>`, `forget
memories <id>`, `forget memory1`, `delete memory <id>`, `clear memories`, and
`purge memory <id>` remain unsupported and receive the planner's existing
generic unsupported-command response. The exact command `memories` continues to
list stored facts.

## Memory Context Consent Grammar

`enable memory context <id>` and `disable memory context <id>` each create one
step addressed by id alone:

```text
PlanStep(
    tool_name="memory",
    action="enable_context",   # or "disable_context"
    arguments={"id": memory_id},
)
```

They reuse the same `id` grammar and validation as `forget memory`: one or more
ASCII digits with a value from 1 to 9223372036854775807, any number of leading
zeroes accepted, and every larger value, Unicode digit lookalike, sign,
decimal, embedded token, or other character rejected with `Memory id must be a
positive whole number.` A bare command returns its usage response:

```text
Use: enable memory context <id>
Use: disable memory context <id>
```

Keywords are case-insensitive and only a literal ASCII space separates the
prefix from the id. Near misses such as `enable memory <id>`, `enable context
for memory <id>`, `enable memory contexts <id>`, and `turn on memory context
<id>` remain unsupported. Both actions require mandatory explicit
confirmation. The enable confirmation also states the V1-15 rule: an enabled
memory may be sent to the local reasoning model on any conversational turn,
within Maria's memory context limits, and Maria does not choose memories by
relevance.

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
or more ASCII digits (`0`-`9`) with a resulting value from 1 to
9223372036854775807, and leading zeroes are accepted however many there are
(`007` parses to `7`). A larger value, Unicode digit lookalikes, signs,
decimals, embedded whitespace, and any other non-digit character are
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

## Note Context Consent Grammar

`enable note context <id>` and `disable note context <id>` each create one step
addressed by id alone:

```text
PlanStep(
    tool_name="notes",
    action="enable_context",   # or "disable_context"
    arguments={"id": note_id},
)
```

They reuse the same `id` grammar and validation as `delete note`: one or more
ASCII digits with a value from 1 to 9223372036854775807, any number of leading
zeroes accepted, and every larger value, Unicode digit lookalike, sign,
decimal, embedded token, or other character rejected with `Note id must be a
positive whole number.` A bare command returns its usage response:

```text
Use: enable note context <id>
Use: disable note context <id>
```

Keywords are case-insensitive and only a literal ASCII space separates the
prefix from the id. Near misses such as `enable note <id>`, `enable notes
context <id>`, and `turn on note context <id>` remain unsupported. Both
actions require mandatory explicit confirmation. The enable confirmation also
states the V1-16 rule: the note's entire title and body may be sent to the
local reasoning model on a later conversational turn when a word of four or
more characters from the request appears literally in its title or its body,
within Maria's context limits, and Maria does not check the note for passwords
or other secrets. Below that notice it shows the note's exact current title and
body, which the notes tool adds when confirmation is offered; the planner's
step still names the id alone.

The planner never searches anything itself. The Tier B search terms are
derived from a conversational request only after the planner has returned its
generic fallback for it; see "Tier B Search Context" in `docs/architecture.md`.

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

`id` must be one or more ASCII digits (`0`-`9`) with a resulting value from 1
to 9223372036854775807, the largest integer SQLite stores and so the largest
id any record can have; leading zeroes are accepted however many there are
(`007` parses to `7`). Unicode digit lookalikes, signs, decimals, embedded
whitespace, and any other non-digit character are rejected, and so is a larger
value, with the same invalid-ID response and before any step is planned or any
confirmation is asked for. The planner strips the leading zeroes and refuses
more than 19 significant digits before any conversion, then converts at most 19
digits and compares the result with 9223372036854775807. An operand of any
length therefore gets an answer, and Python's limit on converting long digit
strings to an integer, which stays in force, is never reached. A bare or
whitespace-only `search history` returns:

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

## Audit Summary Grammar

Audit-summary commands use strict command syntax, and their keywords are
case-insensitive. They show and remove the content-free audit summaries Maria
commits when the owner approves an action the local reasoning model proposed;
see "Audit Tool" in `docs/tools.md` and "Model-Originated Execution and Audit"
in `docs/architecture.md`.

The exact command `audit` creates this step:

```text
PlanStep(
    tool_name="audit",
    action="list",
    arguments={},
)
```

`read audit <id>` creates this step:

```text
PlanStep(
    tool_name="audit",
    action="read",
    arguments={"id": summary_id},
)
```

`delete audit <id>` creates this step:

```text
PlanStep(
    tool_name="audit",
    action="delete",
    arguments={"id": summary_id},
)
```

`read audit` and `delete audit` take a positive numeric ID only and reuse
`read interaction`'s exact `id` grammar and validation, unmodified: `id` must
be one or more ASCII digits (`0`-`9`) with a resulting value from 1 to
9223372036854775807, and leading zeroes are accepted however many there are
(`007` parses to `7`). A larger value, Unicode digit lookalikes, signs,
decimals, embedded whitespace, and any other non-digit character are rejected.
A bare or whitespace-only `read audit` or `delete audit` returns the matching
usage response:

```text
Use: read audit <id>
Use: delete audit <id>
```

An invalid ID returns:

```text
Audit summary id must be a positive whole number.
```

Because the operand after each fixed prefix is parsed as one value, an
embedded extra token (for example `delete audit 1 extra`) is an invalid ID,
not a separate unsupported command. Each prefix is matched exactly, with one
ASCII space between its words and one after it. A tab in place of either
space, as in `read audit\t5`, or a second space between the words leaves the
command unsupported; any further whitespace before the ID is ignored.

`audit.delete` requires mandatory explicit confirmation before it executes;
see `docs/architecture.md`'s Permission Policy section. The confirmation also
states that deleting an audit summary does not undo the action and that
Maria's operational log may still show that a model-proposed action ran. It is
asked for even when no summary has that ID, because Maria does not look the
summary up before asking; the confirmed deletion then answers `Audit summary
not found: <id>.` An ID above 9223372036854775807 is different: no summary can
have one, so it gets the invalid-ID response and no confirmation is asked for.

Near-prefix and plural variants such as `audits`, `audit list`, `audit 1`,
`read audits <id>`, `delete audits <id>`, and `delete audit1` remain
unsupported and receive the planner's existing generic unsupported-command
response. `list audits` is not an audit command: it keeps its existing
meaning, a `filesystem.list` step for the path `audits`.

`help` lists the three commands last, after `delete interaction <id>`:

```text
- audit
- read audit <id>
- delete audit <id>
```

Only these owner commands reach the audit actions. None of the three is
model-eligible, so a reasoning model's proposal naming one is refused with
`Maria does not accept that action from the reasoning model.`, and audit
summaries are never sent to the model as context.

## Conversation Record Grammar

Saved-conversation and conversation-summary commands use strict command
syntax, and their keywords are case-insensitive; see "Conversations Tool" and
"Conversation Summaries Tool" in `docs/tools.md` and "Saved Conversations and
Summaries" in `docs/architecture.md`. Each creates one step:

| Command | Tool and action | Arguments |
| --- | --- | --- |
| `save conversation` | `conversations.save` | `{}` |
| `conversations` | `conversations.list` | `{}` |
| `read conversation <id>` | `conversations.read` | `{"id": id}` |
| `delete conversation <id>` | `conversations.delete` | `{"id": id}` |
| `summarize conversation` | `conversation_summaries.create` | `{}` |
| `summaries` | `conversation_summaries.list` | `{}` |
| `read summary <id>` | `conversation_summaries.read` | `{"id": id}` |
| `edit summary <id>: <new text>` | `conversation_summaries.edit` | `{"id": id, "text": new_text}` |
| `delete summary <id>` | `conversation_summaries.delete` | `{"id": id}` |
| `enable summary context <id>` | `conversation_summaries.enable_context` | `{"id": id}` |
| `disable summary context <id>` | `conversation_summaries.disable_context` | `{"id": id}` |

The four argumentless commands match only the whole request, so a
conversational phrasing such as `save the conversation` or
`summarize conversation please` still gets the generic fallback, and reaches
the model when reasoning is enabled. The planner never supplies session text:
the save step carries no `transcript` and the summary step no `text`. Brain
supplies each from the session, as its contract-declared session input, only
when the planner's plan reaches `process`, and refuses a plan that already
carries one.

The seven ID commands take a positive numeric ID through the same parser as
`read interaction` and the audit commands, which made twenty ID-only planner
commands; V1-22's sixteen task and reminder ID commands make thirty-six. An
invalid ID returns
`Saved conversation id must be a positive whole number.` or
`Conversation summary id must be a positive whole number.`, and a bare command
returns its usage response:

```text
Use: read conversation <id>
Use: delete conversation <id>
Use: read summary <id>
Use: edit summary <id>: <new text>
Use: delete summary <id>
Use: enable summary context <id>
Use: disable summary context <id>
```

`edit summary` splits its operand on the first colon only, so the new text may
itself contain colons; outer whitespace is removed from both parts, and an
empty text or a missing colon gets the usage response. `delete conversation`,
`summarize conversation`, `edit summary`, `delete summary`, and both summary
consent commands always require confirmation. `list summaries` is not a
summary command: it keeps its existing meaning, a `filesystem.list` step for
the path `summaries`.

`help` lists the eleven commands after `disable memory context <id>` and
before the task and reminder commands:

```text
- save conversation
- conversations
- read conversation <id>
- delete conversation <id>
- summarize conversation
- summaries
- read summary <id>
- edit summary <id>: <new text>
- delete summary <id>
- enable summary context <id>
- disable summary context <id>
```

None of the eleven actions is model-eligible, so a reasoning model's proposal
naming one is refused, and a saved conversation is never sent to the model.

## Task and Reminder Grammar

Task and reminder commands use strict command syntax, and their keywords are
ASCII case-insensitive; see "Tasks Tool" and "Reminders Tool" in
`docs/tools.md` and "Tasks and Reminders" in `docs/architecture.md`. Each
creates one step, carrying canonical values only:

| Command | Tool and action | Arguments |
| --- | --- | --- |
| `tasks` / `completed tasks` | `tasks.list` | `{"status": "open"}` / `{"status": "completed"}` |
| `due tasks` | `tasks.due` | `{"as_of": local today, YYYY-MM-DD}` |
| `add task <text>` | `tasks.create` | `{"text": text}` |
| `add task due <date>: <text>` | `tasks.create` | `{"text": text, "due": YYYY-MM-DD}` |
| `read`, `complete`, `reopen`, `delete task <id>` | `tasks.read`, `.complete`, `.reopen`, `.delete` | `{"id": id}` |
| `edit task <id>: <new text>` | `tasks.edit` | `{"id": id, "text": new_text}` |
| `reschedule task <id>: <date or none>` | `tasks.reschedule` | `{"id": id, "due": YYYY-MM-DD or "none"}` |
| `enable` / `disable task context <id>` | `tasks.enable_context` / `.disable_context` | `{"id": id}` |
| `reminders` / `completed reminders` | `reminders.list` | `{"status": "open"}` / `{"status": "completed"}` |
| `due reminders` | `reminders.due` | `{"as_of": reference instant, YYYY-MM-DDTHH:MM:SSZ}` |
| `add reminder <timing>: <text>` | `reminders.create` | `{"text": text, "when": canonical, "repeat": "never"}` |
| `read`, `complete`, `reopen`, `delete reminder <id>` | `reminders.read`, `.complete`, `.reopen`, `.delete` | `{"id": id}` |
| `edit reminder <id>: <new text>` | `reminders.edit` | `{"id": id, "text": new_text}` |
| `reschedule reminder <id>: <timing>` | `reminders.reschedule` | `{"id": id, "when": canonical, "repeat": "never"}` |
| `enable` / `disable reminder context <id>` | `reminders.enable_context` / `.disable_context` | `{"id": id}` |
| `reminder delivery` | `reminders.delivery_status` | `{}` |
| `enable reminder delivery` / `disable reminder delivery` | `reminders.enable_delivery` / `.disable_delivery` | `{}` |

`<date>` is `today`, `tomorrow`, a weekday name, `YYYY-MM-DD`, or
`in <N> day(s)`, and a reminder's `<timing>` is a `<date>`, one space, and
`H:MM` or `HH:MM`, optionally one more space and a `+HH:MM` or `-HH:MM`
offset, or `in <N> minute(s)` or `in <N> hour(s)`; there is no `on` or `at`.
`add reminder` splits at the first colon that ends a whole timing, so a
time's or an offset's own colon is never the separator and a later colon
stays in the text. `add task due` applies when the operand begins with
`due ` and holds a colon, and `edit` and `reschedule` split on the first colon
only. The planner resolves every date and time once, against the request's
`TimingContext`, in the configured zone, so the plan carries canonical values
and the confirmation shows them before anything is stored. Without a timing
context it answers `Maria cannot resolve dates and times for this request.`;
without a usable zone, `due tasks` answers the zone message and a new date or
time answers it followed by `Nothing was saved.`, as do a past, too distant,
skipped, or ambiguous date or time; `due reminders` compares saved instants
with the reference instant and needs no zone, and an undated `add task` and
`reschedule task <id>: none` need no timing at all.

New text must be one line of 1 to 512 UTF-8 bytes, with no outer whitespace
and no control or formatting character, or the shared text message is the
answer and nothing is offered. Every `<id>` takes the shared ID parser, so
an invalid one returns `Task id must be a positive whole number.` or
`Reminder id must be a positive whole number.`, and a bare or malformed
command returns its fixed usage line. `tasks`, `completed tasks`, `due tasks`,
`reminders`, `completed reminders`, and `due reminders` match only the whole
request. `list tasks` and `list reminders` keep their meaning as
`filesystem.list` steps, `move task 3 to tomorrow` is still a file move, and
no command begins with `remind me` or `add a task to`: those natural-language
forms reach the generic fallback, and the reasoning model when it is enabled.

The three reminder-delivery commands of V1-23-R1, `reminder delivery`,
`enable reminder delivery`, and `disable reminder delivery`, each match only
the whole request, in any letter case and with outer whitespace ignored, and
plan one step with no arguments; anything more, such as
`enable reminder delivery now`, plans no delivery step. None is open to the
reasoning model.

`add task`, `add reminder`, the edit, reschedule, delete, and consent
commands, and `enable reminder delivery` and `disable reminder delivery`
always require confirmation; listing, the due queries, reading, completing,
reopening, and `reminder delivery` run without asking. `help` lists the
twenty-nine commands after the conversation-record commands and before
`system`:

```text
- add task <text>
- add task due <date>: <text>
- tasks
- completed tasks
- due tasks
- read task <id>
- complete task <id>
- reopen task <id>
- edit task <id>: <new text>
- reschedule task <id>: <date or none>
- delete task <id>
- enable task context <id>
- disable task context <id>
- add reminder <date> <time> [<offset>]: <text>
- add reminder in <N> minutes|hours: <text>
- reminders
- completed reminders
- due reminders
- read reminder <id>
- complete reminder <id>
- reopen reminder <id>
- edit reminder <id>: <new text>
- reschedule reminder <id>: <date> <time> [<offset>] or in <N> minutes|hours
- delete reminder <id>
- enable reminder context <id>
- disable reminder context <id>
- reminder delivery
- enable reminder delivery
- disable reminder delivery
```

Only `tasks.create` and `reminders.create` are model-eligible, and only for a
request in one of the four natural-language forms that Maria's own parse
reads; see "Model-to-Plan Compilation Gate" in `docs/architecture.md`.

## Project Grammar

The thirteen project commands of V1-24-R1 use strict command syntax, and
their keywords are ASCII case-insensitive; see "Projects Tool" in
`docs/tools.md` and "Projects and Project Files" in `docs/architecture.md`.
The planner matches them before the task and reminder commands, the fact
commands before the project commands, and `read project file` before
`read project`, so a longer command is never taken for a shorter one. Each
creates one step:

| Command | Tool and action | Arguments |
| --- | --- | --- |
| `projects` | `projects.list` | `{}` |
| `create project <name>: <location>` | `projects.create` | `{"name": name, "location": location}` |
| `read`, `delete`, `activate project <id>` | `projects.read`, `.delete`, `.activate` | `{"id": id}` |
| `retitle project <id>: <new name>` | `projects.retitle` | `{"id": id, "name": new_name}` |
| `deactivate project` | `projects.deactivate` | `{}` |
| `project facts <project id>` | `projects.list_facts` | `{"id": project_id}` |
| `add project fact <project id>: <text>` | `projects.add_fact` | `{"id": project_id, "text": text}` |
| `edit project fact <fact id>: <new text>` | `projects.edit_fact` | `{"id": fact_id, "text": new_text}` |
| `delete project fact <fact id>` | `projects.delete_fact` | `{"id": fact_id}` |
| `project files` / `project files <folder>` | `projects.list_files` | `{}` / `{"folder": folder}` |
| `read project file <path>` | `projects.read_file` | `{"path": path}` |
| `read project file <path> lines <from>-<to>` | `projects.read_file` | `{"path": path, "start": from, "end": to}` |

`projects`, `deactivate project`, and `project files` match only the whole
request, in any letter case and with outer whitespace ignored. `create
project` splits its operand at the first colon, each side stripped and
non-empty, so a name never holds a colon; `retitle project`, `add project
fact`, and `edit project fact` split at the first colon too, and their text
must be non-empty. In `read project file`, a final ` lines <word>` after a
non-empty path is always the range, which must be `<from>-<to>` with each
number from 1 to 999,999 and `<from>` no greater than `<to>`, or the range
message is the answer. Every `<id>` takes the shared ID parser, so an invalid
one returns `Project id must be a positive whole number.` or `Fact id must be
a positive whole number.`, and a request that is only a command's words, or
whose operand is malformed, returns that command's fixed usage line rather
than reaching the model. The planner checks only that shape: names, locations,
fact text, and paths are checked by the tool and the project file reader.
`list projects` keeps its meaning as a `filesystem.list` step.

`retitle project`, `delete project`, `activate project`, `add project fact`,
`edit project fact`, and `delete project fact` always require confirmation;
the other seven run without asking. None of the thirteen is open to the
reasoning model. `help` lists them after the reminder-delivery commands and
before `system`, with one more line on `@` references, fourteen lines in all:

```text
- projects
- create project <name>: workspace/<folder> or project_maria
- read project <id>
- retitle project <id>: <new name>
- delete project <id>
- activate project <id>
- deactivate project
- project facts <project id>
- add project fact <project id>: <text>
- edit project fact <fact id>: <new text>
- delete project fact <fact id>
- project files [<folder>]
- read project file <path> [lines <from>-<to>]
- @<path> or @<path>:<from>-<to> in a question names an active project's file
```

An `@` reference is not planner grammar: a request holding one reaches the
generic fallback like any other unsupported text, and Brain settles its
references against the active project only on the model path.

## Plan Shape

A plan contains:

- The original request
- Zero to five steps, in order, `PLAN_MAX_STEPS`; a plan is flat, so no step
  holds another step or a plan
- An optional response when no action should run
- A recording policy, `RECORD` by default; the planner never sets any other
- An origin, `DETERMINISTIC` by default; the planner never sets any other

Each step names a tool, an action, and its arguments.

The origin is a `PlanOrigin`. The only other one is `MODEL`, and only
`core/plan_compiler.py` sets it, on the plan it compiles from one local-model
proposal after the planner has returned its generic fallback. A `MODEL` plan
always has exact text as its request, one to five steps in an exact tuple, and
the `TRANSIENT` recording policy, and never a response for when no action
should run; `Plan` refuses to construct one of any other shape, and
`is_model_plan()` reports whether a value is exactly such a plan, answering no
rather than raising for one missing any of its own fields. Every plan,
whatever its origin, holds at most five steps, counted by reading them rather
than by the length their container reports: `Plan` refuses more, and wherever
a deterministic plan is confirmed or run it is read again, as one snapshot,
by `plan_snapshot()`, while a model-originated one is checked again by
`is_model_plan()`. Both read every field from the plan itself, as
`own_origin()` does, so a deleted field is never read as its class default.
In V1-19, only the model proposal path gains multi-step production. The
deterministic command planner keeps its existing zero-or-one-step grammar;
internally constructed deterministic plans retain multi-step support within
the five-step bound. The planner has no grammar for chaining commands, and
text such as `calculate 2 + 2 and then show time` stays one command. The
planner never sees a model's reply or a compiled plan, and an approved
compiled plan runs as compiled, never planned again. Brain requires exactly
one approval from the owner for every `MODEL` plan, for the whole plan, even
when every action is allowed outright, and refuses one with any denied action
before offering it; see "Model-Originated Execution and Audit" in
`docs/architecture.md`.

`Plan` and `PlanStep` are immutable, hashable value objects. `PlanStep`
arguments are a read-only mapping snapshotted at construction, whose keys are
strings and whose values are strings or integers; any other key or value type
is refused. `Plan.fingerprint` returns a stable SHA-256 digest of the plan's
complete logical content, so structurally equal plans share one fingerprint
while a differing step order or a single differing character does not. The
origin joins that content only when it is `MODEL`, so every deterministic plan
keeps the fingerprint it had before origins existed, while a model-originated
plan fingerprints differently from an otherwise identical deterministic one.
See `docs/api.md` for the full contract.

## Future Direction

Future versions may support richer planning. Any expansion should preserve the
planner's single responsibility: producing plans, not executing them.
