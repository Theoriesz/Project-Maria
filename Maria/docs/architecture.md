# Architecture

Maria is organized around a small set of modules with clear ownership.

## Core Flow

1. `main.py` receives user input from the command line, or with `--gui`
   opens the optional desktop window, which receives it instead; see "GUI
   Boundary". It is also the runtime composition point, the one production
   module that names the local reasoning adapter.
2. `session.py` holds the owner's confirmation state and the session's bounded
   conversation, forwards the request, and renders a plan the local model
   proposed for the owner's approval.
3. `brain.py` coordinates the request. It reads one clock once per request,
   right after the empty-request check and before planning, through its one
   seam, `_reference_instant()`, and hands that instant with the configured
   time zone key, as one `TimingContext`, to the planner and to the compiler;
   see "Tasks and Reminders". On the model-backed conversational path only, it
   reads the owner's enabled memory context and enabled open tasks and
   reminders, and what a bounded literal search finds among enabled memories,
   notes, and conversation summaries, through the memory layer and
   `context.py`; it reads no interaction history or saved conversation for
   planning or reasoning. For a planner's plan whose one step's contract
   declares a session input, it supplies that text itself, generically: the
   canonical transcript of the session's retained exchanges for a save, or a
   summary draft from the local model; see "Saved Conversations and
   Summaries".
4. `planner.py` converts explicit commands into structured plan steps,
   resolving a task's date or a reminder's time against the request's timing
   context, so the plan carries canonical values.
5. `action_contracts.py` declares which tool actions exist and which arguments
   each one accepts. Brain validates every step of a plan against it before any
   permission is evaluated.
6. `permissions.py` decides whether each planned action is allowed. Brain
   resolves every step of a plan before any step is dispatched.
7. `tool_manager.py` dispatches allowed actions to registered tools.
8. Tools perform their focused work and return a `ToolExecutionResult`.
9. `memory.py` owns persistent interactions, notes, explicit memories, audit
   summaries, saved conversations, conversation summaries, tasks, and
   reminders in SQLite.
10. `brain.py` forms the final response and, on recordable completion paths,
    attempts to record the cleaned request and response as an interaction. An
    approved model-proposed action records no interaction: Brain commits its
    audit summary before dispatching it and appends the outcome afterwards.
11. `logger.py` configures Maria's named logger; application modules emit
    operational events through it.
12. `filesystem_roots.py` holds Maria's named filesystem roots, the settled
    protections covering every location that is permanently out of reach, and the
    two-identity resolution both rely on. Startup prepares the declared roots
    once, before logging is configured and before the memory layer opens the
    database.
13. `reasoning.py` defines the typed, provider-independent contract a local
    reasoning model sits behind, and `ollama_adapter.py` is the one provider
    that implements it. `conversation.py` uses them to answer the one kind of
    request the planner has no command for, only when a provider is present,
    `config.reasoning.enabled` is true, and the planner returned its exact
    generic fallback. A reply whose first character after any leading JSON
    whitespace is `{` goes back to Brain untouched, as a proposal attempt;
    every other reply is text only. See "Deterministic-First Conversation",
    "Reasoning Interface and Local Override", and "Local Ollama Adapter".
14. `plan_compiler.py` is the deny-by-default gate that turns a reasoning
    model's proposal into a plan, or refuses it. Brain is the only production
    module that imports it. See "Model-to-Plan Compilation Gate".
15. `audit.py` is the closed vocabulary of the content-free audit summaries
    Brain writes, through the memory layer, before an approved model-proposed
    action runs, and `tools/audit.py` lists, reads, and deletes them on the
    owner's commands. See "Model-Originated Execution and Audit".
16. `conversation_records.py` is the vocabulary of saved conversations and
    conversation summaries: their limits, the canonical transcript, value
    objects, and fixed messages. `tools/conversations.py` and
    `tools/conversation_summaries.py` store, list, read, edit, and delete them
    on the owner's commands. See "Saved Conversations and Summaries".
17. `time_resolution.py` is the one module that reads and resolves dates and
    times, `task_requests.py` parses the four natural-language request forms,
    and `task_records.py` is the vocabulary of tasks and reminders: limits,
    statuses, canonical forms, value objects, and fixed messages. All three
    are leaves that log nothing; the first two read no clock, and the third
    reads one only for the memory layer's write stamps. `tools/tasks.py` and
    `tools/reminders.py` add, list, query, read, complete, reopen, edit,
    reschedule, and delete them on the owner's commands. See "Tasks and
    Reminders".

An explicit memory request uses the same execution path as every other tool
request, from either interface:

```text
CLI or desktop GUI
-> MariaSession
-> MariaBrain
-> Planner
-> ActionContractRegistry
-> PermissionManager
-> ToolManager
-> MemoryTool
-> Memory
-> SQLite
```

The `MemoryTool` is a thin adapter for the `store`, `search`, `list`, `update`,
and `forget` actions. It is registered as `tools.memory` through Tool Manager's
standard `build_tool(...)` factory contract alongside the other built-in tools.
Memory actions therefore pass through the normal permission and result
handling flow. An action the local model proposes takes a longer path to its
tool, through the compiler, the owner's approval, and an audit summary; see
"Model-Originated Execution and Audit".

## Interaction Recording Boundary

For a normal completed tool request, interaction recording follows this
boundary, whichever interface took the request:

```text
CLI or desktop GUI
-> MariaSession
-> Brain
-> Planner
-> ActionContractRegistry
-> PermissionManager
-> ToolManager
-> Tool
-> Brain final response
-> Memory.record_interaction
-> SQLite interactions table
```

`MariaBrain.process()` strips non-empty input before planning. Brain records
on exactly four branches: the zero-step branch, the contract-refusal branch,
the permission-denial branch, and the post-tool-result branch that follows
completed execution. On each of them, unless the plan is transient (see
below), Brain passes that cleaned request and the response chosen for
recording to `Memory.record_interaction()`. That response normally matches the
displayed response, but a completed tool result may supply a
`recorded_message`, including an explicit empty string, to record in place of
its displayed text. Recording covers help, unsupported commands not answered
by the local model, malformed-command usage responses, contract refusals,
denials, successful tool results, and returned unsuccessful
`ToolExecutionResult` values. An unsupported command is answered by the
local model, and so not recorded, only when a reasoning provider is
present, `config.reasoning.enabled` is true, and the planner returned its exact
generic fallback; with disabled settings it keeps its original response and
interaction row even if a provider was injected. The recording
layer stores the supplied text rather than summaries or only metadata, subject
to existing command and tool output limits. A recording `RuntimeError` is
logged without replacing the primary response, so recording is attempted but a
row is not guaranteed when the database write fails.

All four branches record through one private Brain helper,
`_record_interaction(plan, response)`, which is the only place Brain writes
interaction history. It takes the whole plan rather than the request text, so
the request half of every row is the plan's `original_request` and no branch
can record without the plan's recording policy being consulted. `Plan` carries
that policy as a `RecordingPolicy`: `RECORD`, the default, records exactly as
described above; `TRANSIENT` writes no interaction row at all on any of the
four branches, neither the cleaned request nor a placeholder response, while
the displayed response, contract validation, permission resolution, and tool
execution are unchanged. Suppression is a positive choice by the plan's
producer, so a plan that says nothing is recorded, and only an explicit
`TRANSIENT` keeps a row out of history. The policy governs only the interaction
row: a transient plan's tools still store whatever they store, such as a note
or an explicit memory. `Plan` accepts only an exact `RecordingPolicy` member,
refusing a plain string or any lookalike, and the policy is part of plan
equality, hashing, and the fingerprint, so an approval bound to one policy
cannot execute a plan carrying the other. Brain decides from the plan's
policy alone, never from a step's tool or action or a result's content. A
static source-scan guard test checks the gate's opening transient return and
its single interaction write, and rejects direct tool-, action-, or
request-specific recording decisions in the Brain methods that lead to the gate
and in the helpers that produce recorded text; it does not analyze aliasing or
data flow in general. Every deterministic command keeps the default, and
every one that predates the policy records exactly what it recorded before the
policy existed; the one command whose plan becomes transient is V1-20's
`summarize conversation`. Transient plans have two producers. `conversation.py`
makes every conversational turn, a reply or a controlled failure, a zero-step
transient plan, so conversation writes no interaction row, and it makes every
outcome of a summary request transient as well, the draft bound for approval
and each fixed refusal alike. `plan_compiler.py`
makes every plan it compiles from a model proposal a transient plan of one to
five steps, which Brain runs on a path of its own that never reaches the four
recording branches, so neither a proposal's approval prompt nor its result,
nor a plan's report, is recorded;
a refused proposal is answered without any plan and is not recorded either.
Brain itself never chooses a policy; it only runs the plan it was given.

`ToolExecutionResult` carries an optional `recorded_message` field. It
defaults to `None`, and any result that does not opt in leaves it as `None`;
`None` makes Brain record the same text it displays, exactly as before. A
tool may instead set `recorded_message` to persist different text than the
displayed `message` for that step; an explicit empty string is honored rather
than falling back to `message`, so it still records a row, with an empty
response half. Brain applies this generically, reading only `.message` and
`.recorded_message`, with no branch on a tool's name or action. A result can
therefore change what a recorded row's response half says, but never whether a
row is written; only the plan's recording policy decides that.

In the current implementation, successful `history.read`, `audit.list`,
`audit.read`, `conversations.list`, `conversations.read`,
`conversation_summaries.list`, and `conversation_summaries.read` results,
and V1-22's twelve, `create`, `list`, `due`, `read`, `edit`, and `reschedule`
for tasks and for reminders, and V1-24-R1's thirteen project actions, are the
only ones that supply a non-`None` `recorded_message`, and V1-25-R1's
`filesystem.organize`: thirty-three actions. The organize action records the counts of
a run, or that it was refused before any change, and never a file name or path; the
request itself is recorded as typed.
Each project action records a fixed line naming ids at most, such as
`Listed projects.`, `Created project <id>.`, `Activated project <id>.`,
`Added fact <id> to project <id>.`, `Listed project files.`, or
`Displayed a project file.`, so no project name, location, fact text, path,
listing, or file text reaches interaction history that way; a refusal
records its own fixed text, which holds none of them either. For
`history.read` that keeps redisplayed interaction content from being
duplicated into a new interaction row, the two audit actions record only
`Listed audit summaries.` and `Displayed audit summary <id>.`, the four
conversation-record actions record only `Listed saved conversations.`,
`Displayed saved conversation <id>.`, `Listed conversation summaries.`, and
`Displayed conversation summary <id>.`, and the task and reminder actions
record only fixed lines such as `Added task <id>.`, `Listed open tasks.`,
`Listed due reminders.`, `Displayed reminder <id>.`, `Edited task <id>.`, and
`Rescheduled reminder <id>.`, so no listing, summary field, transcript,
summary text, task or reminder text, date, or time reaches interaction
history that way. Every other
current tool result, including `history.delete`, `notes.retitle`,
`notes.edit`, `notes.delete`, `audit.delete`, `conversations.save`,
`conversations.delete`, every conversation-summary mutation, the task and
reminder `complete`, `reopen`, `delete`, and consent actions, and V1-23-R1's
three reminder-delivery actions, leaves the field as `None` and therefore
retains the existing message-as-recorded behavior; those displayed messages
are already short, content-free summaries naming at most an id, a setting,
and counts, so no override is needed. A due-reminder notice is never recorded
at all: it reaches an interface through the session's delivery seam, never
through Brain; see "Reminder Delivery".
`read file`, `read note`, and `recall` are unmodified and still record their
full displayed content.

`history.delete`'s row deletion and the subsequent interaction-recording
`INSERT` are separate transactions, following the same boundary as every other
recordable path: `Memory.delete_interaction()` commits independently of the
later `Memory.record_interaction()` call. If the deletion commits but the
following recording call raises `RuntimeError`, that error is logged and the
already-computed successful deletion response is still returned unchanged;
the deletion is not rolled back and no compensating write is attempted, so an
interaction row recording the deletion is not guaranteed in that case.

`notes.retitle`, `notes.edit`, `notes.delete`, `audit.delete`, the
conversation-record mutations, and every task and reminder mutation follow
that identical boundary: `Memory.retitle_note()`, `Memory.edit_note()`,
`Memory.delete_note()`, `Memory.delete_audit_summary()`,
`Memory.save_conversation()`, `Memory.delete_saved_conversation()`,
`Memory.edit_conversation_summary()`, `Memory.delete_conversation_summary()`,
the two summary consent methods, and the task and reminder create, complete,
reopen, edit, reschedule, delete, and consent methods each commit in their
own transaction, independently of the later `Memory.record_interaction()`
call. A recording failure after a committed
mutation is logged, does not roll the mutation back, does not
replace the successful response, and triggers no compensating write or retry.

Because Brain records the original cleaned request as the `user_input` half of
an interaction, a `retitle note` command retains its new title, an `edit
note` command its new body, an `edit summary` command its new text, and an
`add task`, `add reminder`, `edit task`, `edit reminder`, or `reschedule`
command its text or timing phrase in interaction history even though the
recorded response is content-free.
`recorded_message` can only change the response half of a recorded
interaction, never the request half, so later editing or deleting the note or
summary does not rewrite or remove those independent interaction rows.

Some paths return before this boundary. Blank CLI input is not processed, and
blank input passed directly to `process()` returns before logging, planning,
memory context retrieval, or recording. `exit`, `quit`, `q`, main-prompt EOF or
Ctrl+C, and startup failures do not become interaction rows. The desktop GUI
keeps the same rules: blank input, an exit word, text refused for holding a
line break, and closing the window never reach Brain. An initial
confirmation-required response returns before completed-interaction recording.
If confirmation is cancelled, the cancellation response is not recorded, and
neither is a refused confirmation, because neither executed anything. If it is
approved, the session calls `MariaBrain.execute_confirmed_plan()` with the
exact plan it was holding; Brain re-resolves that whole plan's permissions and
dispatches it only if nothing is denied, without retrieving context or planning
again, then attempts to record the completed result through the normal path. A
denial found by that recheck executes nothing and is recorded as any other
denial is. The request half of that row is the
approved plan's `original_request`, which is the same cleaned text the first
and only planning pass received. An approved model-originated plan takes
Brain's model-plan path instead: it is checked again without any provider
call, runs at most once behind its committed audit summary, and nothing about
it is recorded as interaction history, whatever happens; see "Model-Originated
Execution and Audit".

Post-startup processing has no broad exception handler. If an exception
propagates before a recordable branch calls `Memory.record_interaction()`, no
interaction write is attempted.

Brain reads no interaction history for planning or reasoning. Until V1-15 it
retrieved up to three recent interaction rows on every request and handed them
to the planner, which discarded them unused; that feed is gone, and
`Planner.create_plan()` now accepts the cleaned request text and, by keyword
only, the request's `TimingContext`, and nothing else.
The `history` command still reads recent rows through its own tool, and
interaction rows remain Tier C, as saved conversations are: no automatic path
hands one to the planner or to the reasoning model.

Operational logging and interaction persistence are separate boundaries.
`logger.py` configures Maria's namespaced file logger; default startup targets
`logs/maria.log` for application events, warnings, and failures. Configuring
logging again is honoured rather than ignored: Maria's own file handlers are
flushed, detached, and closed, and one new handler for the requested directory
replaces them, so a second call never duplicates a handler and never keeps
writing to the previous directory. The replacement happens only once the new
handler exists, so a failure to create the directory or open the file
propagates with the previous configuration intact. The managed handler
rotates rather than growing without limit: the active `maria.log` is capped
at 1 MiB and at most three previous files are kept, so the directory holds
`maria.log` through `maria.log.3` and no more. That policy is internal and
has no configuration field, CLI option, or environment variable.

Maria carries no global redaction, scrubbing, or log-filtering layer, and
deliberately so: an existing test asserts that the filesystem tool's
operational log does retain, for a failed move, the absolute root path and the
filenames its owner-visible response omits, and a blanket filter would break
that contract while giving false confidence everywhere else. The discipline is
module-scoped instead: a module never hands a private value to the logger in
the first place. `failures.py` supports that discipline. It declares a closed
set of failure kinds and one immutable value object holding a kind and
nothing else, with no field for a request, a response, an arguments mapping,
an exception, its text, a path, an endpoint, a credential, or a header, and
with a fixed owner-visible sentence per kind. `tool_manager.py` uses it at
the one boundary that previously leaked: an unexpected exception from a tool
now yields that fixed message and a log record naming the validated tool,
the validated action, and the typed failure code, with the exception never
bound to a name. Tools keep their own specific diagnostics, where each one
knows what its own failure may safely say. The filesystem tool's `list` action
deliberately keeps less: because a reasoning model may propose it, it logs
only the fixed lines `Listed a directory.` and `Directory listing failed.`,
never the resolved directory, the submitted path, or an exception's text,
while `read` keeps its diagnostics and what the owner is shown is unchanged.
Since V1-25-R1, `rename` and `move` log fixed text only, and `organize` logs fixed
text and counts.

`Memory.record_interaction()` separately writes request and response text to
SQLite. A log message that omits full request or response content does not
prevent that content from appearing in interaction history.

## CLI Boundary

`run_cli()` constructs `MariaBrain.from_defaults()` inside the controlled
command-line entrypoint boundary, before printing the banner. Except for
`MemoryError`, an `Exception` raised during construction is converted to stable
stderr output and status 1, while startup `KeyboardInterrupt` maps to status
130. `MemoryError` and `SystemExit` are not converted into ordinary startup
failures. That handling lives in `_start_brain()`, which `run_gui()` uses too,
so every startup outcome described here reads the same in both interfaces.

A startup `TypedFailureError` is the one case that reports something more
specific than the generic message. It carries a `Failure` whose owner-visible
sentence is fixed per kind and interpolates nothing, so printing it is exactly
as sanitized as the generic message while saying which refusal happened. The
status is still 1. This exists because a database written by a newer build has
to be named rather than hidden behind "startup failed", which is the silent
half-working outcome that refusal replaces. The entrypoint chooses nothing
about the wording: it prints what the carried failure already decided.

EOF or Ctrl+C at the main prompt exits normally. At the confirmation prompt,
`y` or `yes` approves and `n` or `no` cancels, case- and whitespace-
insensitively; blank or unrecognized input re-prompts rather than approving or
cancelling. Either interruption (EOF or Ctrl+C) cancels the pending action
immediately without executing it, even if it occurs after one or more invalid
answers. This boundary intentionally does not add a broad exception handler
around Brain processing after startup.

`main.py` owns terminal concerns: reading input, printing output, showing
the confirmation prompt, accepting its aliases, and mapping the owner's answer
to the session's confirm or cancel operation. It never calls Planner, builds or
copies a plan, compares fingerprints, evaluates permission, holds the pending
plan, or re-sends the original request after approval. Every way out of the
main loop, including an exception it lets propagate, ends the session, which
discards the session's conversation and any pending approval.

Before every prompt, the loop calls `show_due_reminders(session)` inside the
prompt's own `try` (V1-23-R1). It returns at once for a session without a
delivery seam, and otherwise prints the notice the session's
`claim_due_reminders()` returns as one `Maria:` message and records it as
displayed, or as not displayed when printing raises an `Exception`, after
which the prompt follows. Because the check sits inside the prompt's handler
of `EOFError` and `KeyboardInterrupt`, Ctrl+C during the check or while its
notice prints says `Goodbye.` and ends with status 0, exactly as at the
prompt, leaving a claim not yet recorded unconfirmed; a `SystemExit` raised
there is not caught by that handler, propagates, and still ends the session,
and an interrupt during a request propagates as before. `run_cli()` and
`run_gui()` build the session's seam through an injectable delivery factory;
by default, `build_runtime_delivery()` builds it over the runtime Brain's
memory with the interface's channel and an aware UTC clock, and a caller that
injects a Brain factory gets no seam unless it also injects a delivery
factory.

`main.py` is also the runtime composition point. Its default startup is
`build_runtime_brain()`, which calls `MariaBrain.from_defaults()` with
`build_reasoning_provider` as the reasoning factory. That factory constructs
`OllamaReasoningProvider` only when the settled reasoning settings are enabled,
and returns `None` otherwise; Brain receives the result only through the
`ReasoningProvider` interface. `main.py` is the one production module that
names the adapter, and a guard test holds every other production module,
Brain included, to neither naming nor reaching it. Enabled settings the
adapter refuses, such as the `private_lan` transport, stop startup through
the ordinary sanitized failure before anything is created.

## GUI Boundary

The optional desktop GUI (Stage V1-21, amendment V1-21-R1) is PyQt6 with Qt
Widgets: presentation over the same session contract the CLI uses. `main.py`
opens it only for an explicit `--gui`; any other argument is ignored, and the
CLI starts exactly as before. `run_gui()` imports `gui.window` and creates Qt's
application object first, through `prepare_application()`, so a missing or
broken PyQt6, or a Qt that cannot show windows here, prints one fixed message
and returns status 1 before any configuration, log, database, or Brain exists.
Creating that object is where Qt checks its platform, and a failure there is
fatal inside Qt rather than an exception; while it is created, Qt's messages,
which can name host paths, are withheld, and a fatal one becomes the fixed
message and an immediate exit with status 1, even when standard error is
missing or cannot be written. As soon as the object exists, or its creation
fails, the message handler installed before is put back unchanged. On Windows,
a process without a console first sees Qt's own error dialog for a platform
that cannot start.
`run_gui()` then builds Brain through the same `_start_brain()` the CLI uses,
with the same sanitized startup messages and statuses, and hands the window a
session factory, never Brain itself. A window that fails, whose session ends
unexpectedly or cannot end cleanly, or that cannot close in order, is reported
with another fixed message and status 1, and a window failure is logged by its
exception type alone. The CLI never imports the GUI package or Qt.

The package has three modules with separate jobs:

- `gui/controller.py` is Qt-free. It decides what the window shows and which
  owner actions it accepts, from its own state and the worker's events.
- `gui/session_worker.py` runs the window's one worker thread, the narrow
  concurrency exception V1-21-R1 grants. That thread creates the session,
  performs each operation one at a time in the order asked, and ends the
  session; nothing else touches the session. Under V1-23-R1 it also runs the
  session's due-reminder check and the acknowledgment of each notice it
  handed to the window, and for these two alone the amendment supersedes "in
  the order asked"; see below. It is the only production module that imports
  `threading`. Every failure in that thread, of any kind, is
  caught at its boundary and logged as fixed text naming at most its
  exception type, so nothing reaches Python's thread exception hook. A
  failure to create the session or of an operation stops the queue at once;
  `end()` is then attempted once whenever a session exists; and the end
  counts as orderly only after the requested close and an `end()` that
  succeeded. An event that cannot be delivered is only logged, and the
  worker carries on.
- `gui/window.py` draws the window and runs Qt's event loop. It is the only
  production module that imports PyQt6, and it touches widgets only on the GUI
  thread; the worker's events reach it through a queued Qt signal.

The GUI changes no Brain API, and the session API gains only V1-23-R1's
delivery seam and V1-24-R1's `project_choices()`, nine members in all.
Brain is built on the main thread, exactly as for the CLI, and is safe to hand
to the worker because it holds no thread-bound resource: the memory layer
opens and closes one SQLite connection per operation, and the local adapter
opens one socket per request.

When the session delivers reminders, the worker checks for due ones once after
creating the session and once after each operation whose event is a `Reply`,
after that reply has been handed to the window. It never checks after an
approval offer or an acknowledgment, never on a timer, and never once
`close()` was called, not even the first time: `close()` sets a flag before it
queues the end, and every check reads it first. A claimed notice goes to the
window as one `DueReminderNotice` under a notice serial of the worker's own,
counted apart from offer serials, and the worker keeps the notice until the
window acknowledges that serial. The controller adds the notice's text to the
conversation as `Maria: <text>`, in any state but closed, without changing
its state, the offer it shows, or the approval area, and only then sends the
acknowledgment, which the worker queues in order with the owner's operations.
When it reaches the worker, the notice is recorded as displayed, once; a
repeated or unknown serial changes nothing, and no event or check follows. A
notice the window never acknowledges stays unconfirmed: one whose handoff
failed and was only logged, one whose acknowledgment queues behind the close
and so never runs, and one shown just before a session that then ended. A
rendering that raises reaches the window's shield, which closes the window as
a failure, and no acknowledgment is sent, so nothing records it either way. A
failure that is not an `Exception`, raised in the session's claim or record
call, and an `Exception` in the worker's own new lines end the session as a
failed operation does.

Under V1-24-R1 the worker also reads the session's project choices at the
same two points, after the session's due-reminder check: once after creating
the session and once after each operation whose event is a `Reply`, never
after an approval offer and never once `close()` was called. Choices the
session returns go to the window as one `ProjectList`: at most fifty
`(id, name)` pairs in ascending id order and the active project's id, given
only while one is active. A session without a projects seam, or whose read
failed, returns none, and the window keeps what it shows. The controller
shows them as plain-text items, `<id>: <name>`, with ` (active)` on the active
one, or one `No projects` item, without changing its state, the offer it
shows, or the approval area. Activate and Deactivate are accepted only while
the window is ready, and the controller then submits exactly
`activate project <id>` for the chosen item or `deactivate project`, through
the same path as Send, so an activation is still offered for approval and
nothing about it is decided in the GUI. The buttons take no focus.

The window shows a read-only conversation area, a one-line input with Send, a
short status line, and, while an approval is pending, the complete approval
text the core produced, with Approve and Cancel. Requests follow the CLI's
rules: the text is stripped, blank text is ignored, and `exit`, `quit`, or `q`
closes the window without reaching Brain. Text holding a line break is refused
with a fixed message, because the CLI reads one line per request and so can
never send one, and the input has no length cap that could cut a long request
short. Only one request or answer is in flight at a time: a request is accepted
only while the window is idle, and Approve or Cancel only for the offer
showing, which carries a serial, so a second click or a stale callback is
refused.

When a request leaves an approval pending, the worker keeps the exact
`PendingApproval` object the session holds. Approve confirms only while that
very object is still `session.pending_approval`, and then through the existing
`confirm()`, which takes no argument and performs the session's own
fingerprint check; otherwise it confirms nothing, dismisses whatever is
pending, and shows the core's fixed refusal. Cancel calls `cancel()` and shows
"Action cancelled.", as the CLI does. Cancel dismisses a pending approval
only. Nothing stops an operation already running, and Ctrl+C cannot reach the
worker, so a running plan is never interrupted in the GUI: it finishes or
stops on its own terms. Fingerprint rejection, recording, model calls, and
audit summaries are the core's own and identical in both interfaces, which one
shared test proves by running the same scripts through both.

Everything is plain text: requests, replies, results, and approval text are
never interpreted as HTML, no link is opened, no referenced file is loaded, and
no displayed text is executed. The conversation and approval displays cannot
be selected or copied from and have no context menu; Maria writes nothing to
the clipboard, and pasting into the input works. Maria cannot control an
operating-system clipboard history and claims nothing about it. Approve and
Cancel never take keyboard focus, so only a deliberate click answers an offer,
and the window title is always "Maria".

The window and its session end together. Closing the window asks the worker to
end the session after the operation in progress, refuses every action
meanwhile, never shows an approval that arrives, and lets the window close only
once the session has ended; a session that ends for any other reason closes
the window, and the window is asked to close even when clearing it first
fails. No thread is forcibly stopped: the worker is not a daemon, and
`run_window()` waits for it before returning, whatever ended Qt's loop, while
letting Qt deliver events so the window still shows that it is closing and the
session's end is seen. Closing waits for the current operation and has no
general GUI-wide deadline. Provider deadlines apply where configured, but a
stalled operation can delay closing indefinitely. If asking the window to
close, or delivering its events, raises an `Exception`, `run_window()` logs its
type alone, still asks the worker to end, waits for it, restores Ctrl+C, and
returns status 1; anything else raised there propagates only after the same
request, wait, and restore. While the window is open, Ctrl+C in the launching
terminal is ignored, because an exception raised inside a Qt callback ends the
whole process abruptly; the previous handler is restored afterwards. For the
same reason, every callback the window hands to Qt reaches the controller
through a shield: a failure is logged by its exception type alone and marks
the window as failed, and `run_window()` then returns status 1 once the
session has ended. The controller closes a failed window as a failure. It
records the failure and starts closing before anything else, so no later
Send, Approve, or Cancel is accepted, queued ones included, and it asks the
worker to end the session before touching the window again; an operation
already running still finishes, and the session's end is still delivered and
closes the window. Status 0 needs an orderly end that the controller itself
saw, with no failed callback.

What the window shows is presentation state: it is never replayed into the
session's conversation or saved automatically, and a new window starts with
only the banner. Display history is never populated automatically from stored
records, except due-reminder notices from the session's delivery seam, shown
as plain text (V1-23-R1); the project selector, outside the conversation area,
lists project ids and names (V1-24-R1). Explicit record-reading commands still
display their results. The GUI
never configures logging. Its worker logs fixed lines through the same `maria`
logger, so running both interfaces in one process keeps exactly one managed
log handler throughout.

## Session Boundary

`core/session.py` owns the confirmation lifecycle for every interface. It needs
no standard input, standard output, or terminal, so a headless caller drives
the same contract the CLI does. The desktop GUI is such a caller: its one
worker thread drives the session through exactly these methods; see "GUI
Boundary".

A session is idle or holds exactly one pending approval. Submitting a request
first discards any approval still pending, then plans exactly once. A
confirmation-required response becomes the pending approval: the immutable
`Plan` and the fingerprint captured at that moment, and nothing else. A
model-originated plan that Brain does not refuse comes back as such a
response even when `PermissionManager` allows every one of its actions
outright, because every one needs the owner's approval, given once for the
whole plan.

The session renders a model-originated plan for approval itself, from the
very plan it is about to bind and before binding it, so what the owner sees is
exactly what they approve. A plan of one step is four lines. The first is
fixed, `Maria's local reasoning model proposed this action. It runs only if
you approve it.`, and the next three name the declared tool, the action, and
every argument as one JSON object with its names in sorted order, `{}` when
there are none. A plan of two to five steps begins instead with `Maria's local
reasoning model proposed this plan of N actions. They run in order, and only
if you approve the whole plan. Maria stops if a step fails, an audit write
fails, or the run is interrupted.`, with N its step count, and then shows
every step in plan order as a `Step i of N` line followed by the same three
lines; every step is checked for plain names before any of it is rendered. A whole
number is written in full, and `approval_quoted` writes each name and text
value between quotation marks. A visible ASCII character stays as itself,
except that a quotation mark or a backslash is preceded by a backslash, and a
single space between other characters stays a space. Every other character, a
leading, trailing, or repeated space, a control character, or any character
outside ASCII, becomes a four-digit `\uXXXX` escape, with a character beyond
the Basic Multilingual Plane written as its surrogate pair. Text holding a
surrogate code point is refused rather than quoted, because two separate
surrogates would decode as one different character; no compiled plan can hold
one. The result is printable ASCII that decodes as JSON back to exactly the
argument values, so nothing in it can hide, reorder, or start a new line, and
nothing is truncated. Brain's notices follow below it, once: the fixed
approval notice of each step awaiting confirmation, once each, then the
turn's context notices; they are not kept with the approval or shown again
on confirmation. A plan that cannot be rendered raises `ValueError` and never
becomes pending.

What becomes pending is one snapshot of the plan, taken by `plan_snapshot`: a
well-formed model plan is its own, and a deterministic plan that is not an
exact `Plan` holding its steps in an exact tuple is replaced by a copy
holding one bounded read of them and every other field exactly as the plan
holds it. Confirming consumes the approval first, then reads the pending plan
once more, the same way: it must still be an exact `Plan` holding every one
of its own fields, with exactly its own origin, whose steps can be read
within the bound, and exactly a well-formed model-originated plan if it is
not exactly deterministic. Only that snapshot is fingerprinted and compared
with the captured fingerprint, and that very snapshot is what Brain runs,
handed on only if it still holds exactly the field objects that were
fingerprinted, so code that runs while it is fingerprinted cannot swap what
runs. No steps container is read again after the comparison: Brain re-checks
that very snapshot, whose steps are a tuple, so a container that hands out
different steps on a later read cannot change what runs. The snapshot fixes
the plan's own fields and its sequence of steps; the step objects are the
plan's own, and a deterministic plan's are not type-checked, so what code
inside them hands out is outside that guarantee. A plan that cannot be
snapshotted, cannot be fingerprinted at all, or no longer matches gets the
same fixed refusal, never an error, a field deleted from it or one of its
steps included, and so does an approval missing its plan or its fingerprint.
The approval is consumed before any of this, so a failing tool or a raised
exception cannot make it replayable.
Cancelling discards the pending approval and executes and plans nothing. A
second confirmation, a confirmation after cancellation, and a confirmation
after any intervening request all execute nothing and return one fixed,
content-free refusal, so an approval can never be transferred to a plan the
owner was not shown even when two requests read identically.

The pending approval is transient process state. It is never written to SQLite,
a file, or configuration, never serialized, and never logged; only a plan's
fingerprint could be logged, because the canonical content behind it can
contain note bodies, memory facts, and absolute paths. Its `repr()` is
`PendingApproval(<redacted>)`, which shows none of the request, the arguments,
what the owner was shown, or the fingerprint. `PermissionManager` is
consumed unchanged: the session decides nothing about permission, and Brain
resolves every step of a plan before dispatching any of them. The session
stores no permission resolution alongside the pending plan, because
confirmation re-resolves the plan's current permissions rather than replaying
the decisions that were current when it was shown.

A session also owns its conversation: the completed conversational exchanges
of this session alone, held as a tuple of `ReasoningExchange` values in process
memory. `submit()` hands them to `MariaBrain.process()` with the request, and
only a completed conversational reply replaces them; a command, its result, a
refusal, a failed turn, a model proposal, its approval prompt, or an approved
proposal's result never does, though a turn that had to clear the
conversation, a proposal turn included, still clears it. Whatever comes back is
held to the retention limits again in the session itself, at most eight
exchanges and at most 8 KiB of content. `end()` discards the conversation and
any pending approval, writes nothing, and is idempotent. Brain keeps no state
between requests, so a new session always starts with an empty conversation
and can never observe another session's turns.

Only an explicit `save conversation` or `summarize conversation` makes
anything of that conversation durable, and only through Brain and the
ordinary tool path; each is an ordinary submit, so it discards any approval
still pending, a pending summary draft included, and the session is not
changed to keep one. A save leaves the conversation exactly as it was. A
summary draft is never kept as an exchange, and a summary request whose
recheck finds a supplied record changed clears the conversation and its
fingerprints, as a conversational turn would. Once a session has ended there
is nothing left to save.

A session may also hold a reminder-delivery seam (V1-23-R1), given as the
optional `delivery` keyword argument; `delivers_reminders` says whether it
has one. `claim_due_reminders()` claims, through the seam, the due reminders
one check may show and returns them as one `ReminderNotice`, or `None`, and
`record_reminder_notice(notice, displayed)` records whether the interface
displayed it. Both contain every `Exception` from the delivery subsystem,
logging only its type, and let anything else propagate. Neither calls Brain,
a plan, a tool, Tool Manager, permissions, or a provider, and neither touches
the pending approval, the conversation, or the supplied-context fingerprints,
so a check between an offer and its answer leaves the offer exactly as it
was. With delivery off, a check reads the setting and nothing else. A notice
is never recorded as an interaction, kept in the conversation, or sent to a
model.

A session may also hold a projects seam (V1-24-R1), given as the optional
`projects` keyword argument. `project_choices()` returns, through the seam's
one read of the memory layer, the `ProjectChoices` a window's selector lists,
or `None` without a seam or when the read fails or returns anything else. It
contains every `Exception`, logging only its type, lets anything else
propagate, changes nothing, never calls Brain, a plan, a tool, Tool Manager,
permissions, or a provider, and never touches the pending approval, the
conversation, or the supplied-context fingerprints. `main.py` builds the seam
from the runtime Brain's memory through an injectable factory; a caller that
injects a Brain factory gets none unless it also injects a projects factory.

## Permission Policy

All planned actions pass through `PermissionManager`.
`filesystem.rename`, `filesystem.move`, and `filesystem.organize` always require
confirmation, while `filesystem.delete` and `filesystem.write` are always denied. TOML
configuration may add restrictions but cannot remove these mandatory
protections. Missing permission arrays retain the secure baseline, and
malformed present arrays stop configuration loading instead of weakening it.
When an action is both denied and confirmation-required, denial wins.

In the current implementation, the mandatory confirmation baseline is
forty actions: `audit.delete`, `conversations.delete`,
`conversation_summaries.create`, `conversation_summaries.edit`,
`conversation_summaries.delete`, `conversation_summaries.enable_context`,
`conversation_summaries.disable_context`, `filesystem.rename`,
`filesystem.move`, `filesystem.organize`, `history.delete`, `notes.retitle`,
`notes.edit`, `notes.delete`, `notes.enable_context`, `notes.disable_context`,
`memory.update`, `memory.forget`, `memory.enable_context`,
`memory.disable_context`, from V1-22, `create`, `edit`, `reschedule`,
`delete`, `enable_context`, and `disable_context` for both `tasks` and
`reminders`, from V1-23-R1, `reminders.enable_delivery` and
`reminders.disable_delivery`, and, from V1-24-R1, `projects.retitle`,
`projects.delete`, `projects.activate`, `projects.add_fact`,
`projects.edit_fact`, and `projects.delete_fact`. The confirmation for
`memory.enable_context` also
carries a fixed notice, from `CONFIRMATION_NOTICES` in `core/permissions.py`,
that an enabled memory may be sent to the local reasoning model on any
conversational turn within Maria's memory context limits, and that Maria does
not choose memories by relevance. The confirmation for `notes.enable_context`
carries one that the note's entire title and body may be sent on a later
conversational turn when a word of four or more characters from the request
appears literally in its title or body, and that Maria does not check the note
for passwords or other secrets. The confirmation for `audit.delete` carries one
that deleting an audit summary removes it from Maria's database but does not
undo the action, and that Maria's operational log may still show that a
model-proposed action ran. The confirmation for
`conversation_summaries.create` carries one that storing the summary keeps
text the local model wrote, which can repeat anything said in the session,
including text from memories or notes it was given, that its context consent
starts off, and that Maria does not check it for passwords or other secrets;
the confirmation for `conversation_summaries.enable_context` carries the
note notice's counterpart for one summary's entire text. The confirmation for
`reminders.create` carries one that Maria shows a reminder at most once when
it is due, and only while reminder delivery is turned on and able to run, that
it does not repeat, and that `due reminders` shows the reminders whose time has
come, and the confirmations for `tasks.enable_context` and
`reminders.enable_context` carry one that the record's text and timing may be
sent to the local model on a conversational turn while it is open, after the
enabled memories and within the memory context limits, and that Maria does not
check it for secrets. Under V1-23-R1 the confirmation for
`reminders.enable_delivery` carries one that Maria then shows each due reminder
at most once, in the window or the command line and, while she is closed, as a
Windows message box if the owner installed the task she cannot see or install;
that only moving a reminder to a different time lets it be shown again; that a
box shows the text to anyone at the unlocked screen; and that missed reminders
are shown too, five at a time, and marked overdue when more than fifteen
minutes late. The confirmation for `reminders.disable_delivery` carries one
that turning delivery off stops every new notice from the moment it is saved,
while a notice already started may still appear and be recorded and a box
already on screen stays until closed. Under V1-24-R1 the confirmation for
`projects.activate` carries one that, while a project is active, each of its
facts may be sent to the local model on every conversational turn within the
memory context limits, and Maria can read the project's files when the owner
asks with project commands or `@` references and reads none on her own, and
that the project stays active, also after Maria restarts, until the owner
deactivates it or activates another; and the confirmations for
`projects.add_fact` and `projects.edit_fact` share one that, while its project
is active, the fact may be sent on every conversational turn within the same
limits, so it is not always sent, and that Maria does not check it for
secrets: thirteen notices in all. A deterministic
plan's confirmation request shows each notice as part of its step's permission
reason. A model plan's approval shows, after the exact compiled steps and
before the turn's context notices, the notice of every step PermissionManager
resolved as requiring confirmation, each distinct notice once, in plan order
(owner decision A on V1-22-R1), so a reminder created from the owner's own
words says, before it is approved, that Maria shows it at most once and only
while reminder delivery is turned on and able to run.
Brain asks `PermissionManager.confirmation_notice()` for each such step
generically and names no tool or action. The notices are display only: the
plan, its arguments, its fingerprint, the one approval for the whole plan,
and confirmation, which asks the model nothing, are unchanged, and a notice
never enters the retained conversation, interaction history, or a model
request. A plan none of whose steps carries a notice is shown exactly as
before.
`audit.list`, `audit.read`, `conversations.save`, `conversations.list`,
`conversations.read`, `conversation_summaries.list`,
`conversation_summaries.read`, `history.list`, `history.search`,
`history.read`, `notes.create`, `notes.list`, `notes.read`, `memory.store`,
`memory.search`, `memory.list`, `list`, `due`, `read`, `complete`, and
`reopen` for both `tasks` and `reminders`, and `reminders.delivery_status` are
allowed by default. The V1-22 ten each carry a written risk note: a listing, a
due query, or a reading only shows the owner's own records and records a
content-free line, and completing or reopening changes the status alone, never
the text, timing, or consent, reversibly and without deleting anything.
`reminders.delivery_status` shows only the setting and three counts, never a
reminder's text or time. None of the three delivery actions is model-eligible.
A reminder-delivery claim or outcome write is not a tool action at all and
passes through no permission decision; see "Reminder Delivery". These
entries live in `MANDATORY_CONFIRMATION_ACTIONS` in `core/permissions.py`
rather than in `config/config.toml`, so configuration can add restrictions on
top of them but can never remove them.

A model-originated plan meets one further requirement, which belongs to Brain
rather than to `PermissionManager`: it always needs exactly one approval from
the owner, for the whole plan, even when every one of its actions is allowed
outright, and a plan with any denied action is refused before any approval is
offered. That requirement adds no entry to
either mandatory set and changes no decision `PermissionManager` makes, so the
five model-eligible actions keep their permission policies, the two creation
actions remaining confirmation-required in any case; see
"Model-Originated Execution and Audit".

Direct tool invocation bypasses planning and permission checks and therefore
remains an internal trusted-code boundary. It does not bypass the action
contracts: `ToolManager.execute_tool()` validates them itself before reaching a
tool, so a direct caller cannot submit an unsupported action or a malformed
argument.

### Declared Action Contracts

`core/action_contracts.py` is the positive allowlist of every supported tool
action and of the arguments each one accepts. It is an explicit, hand-written,
immutable table: it is not generated by inspecting loaded tools, not derived
from a tool's `actions` set, not read from configuration, and never persisted.
That independence is the point. Tool Manager only knows whether an action is
registered, which is membership rather than argument validation, and
`PermissionManager` returns `ALLOWED` for any identifier absent from both
mandatory sets, so neither can serve as the positive gate. A test proves the
declared set and the loaded set match in both directions, and a duplicate
declaration raises instead of silently replacing the earlier one, which a plain
dictionary literal would do.

Each contract declares, for one exact tool action, which fields are required,
which are optional, the exact accepted type of each, every bound that applies
to that type, and, for a path field, the named root class it belongs to. An int
field declares a numeric minimum and maximum; a str field declares whether
blank text is accepted and its minimum and maximum length. A bound left
declared as absent means the established domain genuinely has none, and a str
field's blank policy is mandatory, so it can never be omitted by accident.
Metadata that cannot apply to the declared type — a length bound on an int, a
numeric bound on a str, a negative or reversed length pair, a root class on an
int — is rejected when the contract is constructed rather than ignored.

Every contract also declares its model eligibility, `MODEL_ELIGIBLE` or
`NOT_MODEL_ELIGIBLE`, as a required keyword with no default, so no action can
be declared without that decision. Only `calculator.calculate`,
`filesystem.list`, `system.current_time`, `tasks.create`, and
`reminders.create` are model-eligible; every other action, the three audit
actions, the eleven conversation-record actions, and the other twenty task
and reminder actions included, is not. The
classification is independent of permission and approves nothing by itself;
see "Model-to-Plan Compilation Gate".

A text field may also declare a `SessionInput`: text that only Maria supplies,
from the owner's session, and that no request, planner, or model provides.
Only a required, non-blank, bounded text field without a root class may
declare one, a contract may hold at most one, and a contract that holds one
cannot be model-eligible. The shipped registry declares two:
`conversations.save.transcript`, the canonical transcript of the retained
exchanges (`CONVERSATION`, at most 65,536 characters), and
`conversation_summaries.create.text`, a summary draft (`CONVERSATION_SUMMARY_DRAFT`,
at most 2,048 characters). Tool Manager's `session_input_argument` answers
which argument of an action takes one, and Brain supplies the text; see
"Saved Conversations and Summaries".

Every str field in the shipped registry rejects blank text, because every tool
that takes text already refuses it: `FilesystemTool` raises `Path is required.`,
`CalculatorTool` returns `No expression provided.`, and the notes, history, and
memory paths each return their own required-value message.
`calculator.calculate.expression` declares a maximum length, 200 characters,
measured on the stripped text exactly as `CalculatorTool` measures it before
handing the expression to `evaluate_expression`. The text identities a
confirmation pins declare exactly 64 characters, and the conversation-record
texts declare their character ceilings above, their exact UTF-8 limits being
settled by `core/conversation_records.py`. A task's or reminder's text
declares a 512-character ceiling, its exact rule settled by
`core/task_records.py`. No other field declares a minimum length beyond being
non-blank.

A text field may also be closed. `allowed_values` restricts it to a declared
set, such as a listing's status, `open` or `completed`, or a reminder's
`repeat`, which is only `never`. `text_format` restricts it to one closed
format, matched whole: `ISO_DATE` (`YYYY-MM-DD`, a real date),
`ISO_DATE_OR_NONE` (that, or `none`), `ZONED_LOCAL_TIME` (a canonical
`YYYY-MM-DDTHH:MM:00+HH:MM[Area/City]` with a real minute, a whole-minute
offset written `+00:00` when zero, and an IANA key of at most 64 characters),
or `UTC_INSTANT` (`YYYY-MM-DDTHH:MM:SSZ`, a real second). A value outside
either is out of range. A format checks syntax and calendar validity only and
consults no time zone's rules; a test holds each to the record layer's own
validators.

Validation rejects; it never repairs. It performs no coercion, parsing,
truncation, clamping, default insertion, or normalization of casing or
whitespace, and type identity is exact, so `bool` never satisfies an `int`
field and no object is accepted merely because it could be converted to text.
It consumes the original `PlanStep` and its immutable `PlanArguments`, never
rebuilding the plan, replacing a step, or copying arguments into a mutable
mapping, so a plan's fingerprint is identical before and after.

Failures are typed: unknown tool, unknown action, missing required argument,
extra argument, wrong type, and out-of-range value. Each carries only the
failure kind, the tool name, the action when known, and the offending field
name, and each renders one fixed refusal naming the action and field but never
the submitted value, a repr, an object address, or a host path. When several
defects exist, the first reported one is deterministic: plan-step order, then
contract lookup, then missing required fields in declared order, then extra
fields in sorted order, then present fields in declared order with type checked
before bounds.

A path field's root class is classification metadata only. The registry
resolves no path, inspects no filesystem, creates nothing, and performs no
containment check. `FilesystemTool` remains solely responsible for canonical
resolution, safe-root containment, symlink handling, and collisions.

### Whole-Plan Validation and Resolution Before Execution

Executing a plan is three separate phases, and no phase begins while an
earlier one is incomplete. A model-originated plan keeps that order, its
argument contract checked among the compiler's gates before its permission is
resolved, but it takes a path of its own in Brain and none of its refusals is
recorded; see "Model-Originated Execution and Audit".

Contract validation comes first and covers the complete plan. Brain checks
every step in plan order against the declared contracts and stops at the first
violation. A violation anywhere refuses the whole plan before
`PermissionManager` is consulted at all: zero permission checks, zero tool
calls, no pending confirmation, and one controlled refusal recorded through
Brain's ordinary generic interaction path. Because the allowlist is positive,
an action that is mandatorily denied but not registered as a tool action, such
as `filesystem.write`, is refused here as an unknown action and never reaches
the permission layer. `PermissionManager.check("filesystem", "write")` still
returns denied when called directly; the two layers are defence in depth rather
than a contradiction.

Resolution comes second and also covers the complete plan. Brain walks the
steps in their original order, calls `PermissionManager.check()` once per step,
and keeps each step paired with the result it got back. Nothing executes during
that pass, the plan is neither rebuilt nor mutated, no step's arguments are
copied into a mutable dictionary, and no planner or context lookup happens.

Denial takes precedence over the whole plan. If any step resolves to denied,
the plan is denied, no tool runs, no confirmation becomes pending, and the
earliest denied step in plan order supplies the reason. An earlier allowed step
does not run first, and an earlier confirmation-required step does not prompt.
Confirmation is offered only once resolution has found no denial anywhere.

Approving a plan does not freeze its contract or its decisions. Confirmed
execution validates the same immutable plan against the current contracts and
then resolves its permissions again before dispatching the first tool, which is
an execution-time safety recheck rather than replanning: no planner runs and no
context is retrieved. If that recheck now finds a contract violation or a
denial, nothing executes, the refusal is recorded through the normal path, and
the approval the session already consumed stays spent.

Dispatch is the third phase. Steps run in plan order, at most once each, and
each receives the immutable arguments object its `PlanStep` holds. The first
unsuccessful `ToolExecutionResult` stops the plan: later steps do not run, and
Maria neither rolls back nor compensates for the steps that already succeeded.
There is no transaction, undo, or workflow persistence across steps, and the
displayed and recorded text covers exactly the prefix of steps that ran.

The resolved plan is transient execution state. It is private to `brain.py`,
frozen, never persisted, never logged, and never handed to another module, and
it reports which decisions came back without making any of them.
`PermissionManager` itself is unchanged: no new arguments, no bulk-check
method, no altered precedence, no awareness of arguments or contracts, and no
permission policy in Brain, the session, `main.py`, the GUI, Tool Manager, or a
tool.
Brain's rule that a model-originated plan always needs the owner's approval is
keyed to the plan's origin rather than to any action, and it never overrides a
denial or changes what `PermissionManager` decides.
The tools also keep their own argument checks as defence in depth, so a
malformed value is refused rather than converted even on a path that did not
come through Brain.

No plan holds more than five steps, `PLAN_MAX_STEPS`, whatever produced it.
Steps are counted by reading them, at most one past the bound, never by the
length their container reports. `Plan` refuses more when it is built, and the
session and Brain read a plan's steps again before they confirm or run it, so
a plan altered after it was built is refused there, runs nothing, and records
nothing. Brain takes one snapshot of a deterministic plan, every field read
from the plan itself and its steps read once, then validates, resolves,
prepares for approval, dispatches, and records exactly that snapshot. The
bound limits how many steps are requested, not time: a container's own code
can still take as long as it likes to hand out any one of them. A plan
is flat: a step holds only a tool name, an action name, and text or
whole-number arguments, so no step can hold another step or a plan, and
nothing a step returns can add one. In V1-19, only the model proposal path
gains multi-step production. The deterministic command planner keeps its
existing zero-or-one-step grammar; internally constructed deterministic plans
retain multi-step support within the five-step bound. For deterministic plans
the guarantees in this section therefore protect what a later stage may
produce and change no command available today; "Model-Originated Execution
and Audit" describes the model path. A single-step confirmation keeps its
existing wording, while a longer deterministic plan is rendered as a
deterministic, plan-ordered list of each step's tool action and resolved
permission outcome. That rendering repeats no argument value, so it exposes
no absolute host path, no fingerprint, and no canonical plan content; it is
not logged, and a pending confirmation is not written to interaction
history. A model-originated plan is never rendered that way: the session shows
every one of its argument values exactly, every step numbered when there are
several, as "Session Boundary" describes. A
model-supplied path is always plain text relative to the default named root,
never a resolved host path, and that approval text is likewise never logged,
recorded, kept in the conversation, or sent to the model.

## Filesystem Roots and Permanent Exclusions

`core/filesystem_roots.py` owns Maria's one canonical model of where a path may
live. `FilesystemRoot` holds a name, a canonical absolute path, a policy, and an
enabled flag; `FilesystemRoots` holds the declared roots as a tuple together with
the name of the default one. Both are sealed the way `core/failures.py` is sealed:
tuple-backed, with no instance dictionary, no writable slot for
`object.__setattr__` to reach through, no subclass route for a payload field, and
no construction path — including `_make` and `_replace` — that skips validation.
The canonical-path invariant is established inside `FilesystemRoot.__new__`, which
requires an absolute concrete path for the platform and stores `path.resolve()`,
so a non-canonical path never exists inside a root.

`core/config.py` owns production policy and the module owns none of it. A root's
name, path, policy, and flag are validated there, and the whole `[filesystem]`
section fails closed: a missing or non-table section, roots that are not an array
of tables, an unknown key at either level, a missing field, a wrongly typed value,
an unknown policy, a repeated name, a repeated or nested canonical path, a missing,
unknown, or disabled default, a root path that is an ordinary file, a name outside
the two Maria accepts, or a production root whose meaning is wrong are all
refusals. Nothing is repaired, coerced, disabled, or substituted. Loading remains
side-effect free.

Creation is a separate, single boundary. `prepare_roots` runs once in
`MariaBrain.from_defaults`, taking the same settled protections the filesystem
tool will use so the two can never disagree about what is protected. It runs
*before* `configure_logging` and before the memory layer opens the database,
because configuring logging creates the active log directory: a root declaration
that names a protected runtime location has to be refused before anything is
created, not after. Nothing is logged on that path either way — a refusal raises,
and `main.py` reports it as a sanitized startup failure. An enabled organizing
root is created when missing; an enabled read-only root is never created and must
already be a directory; a disabled root is neither created nor required; and a
root inside a permanently excluded location is refused whatever its policy. The
repository root itself is not a protected location, so the disabled
`project_maria` declaration stays valid.

Two predicates sit over that one model, and the separation is deliberate.

`FilesystemProtections.excludes` is the complete universal boundary, and it is
the one a filesystem capability applies. It has two halves, because a path can
be out of bounds for two unrelated reasons.

The fixed half, `is_universally_excluded`, follows from the source layout alone:
`Maria/database/`, `Maria/logs/`, the repository's `.git` as a directory or a
gitdir-pointer file, `Maria/config/config.local.toml`, and a set of
repository-scoped components — credential files, cache and environment
directories, local smoke-test artifacts, assistant working state, and
`.tmp`/`.bak` artifacts — inspected at every component of a path's
repository-relative form. The component rules apply only once a path is proven
to lie inside the repository, which is why a workspace may hold its own `env`,
`venv`, or `.claude` directory.

The active half protects where the *running* configuration keeps Maria's live
state: the database file named by `[paths] database` together with its SQLite
`-journal`, `-wal`, and `-shm` sidecars, the whole subtree of `[paths] logs`, and
the file named by `[paths] system_prompt`. Each protected identity covers itself
and everything beneath it, which is free under the ordinary file shape and
fail-closed when one of those names is occupied by a directory or a directory
reparse point. Each sidecar is settled to two identities in the constructor — the
name beside the database and, when that name already resolves elsewhere through a
link, its canonical target — so `protects` stays a pure path comparison while
still covering the location the content really occupies.

Settling happens at construction and only at construction. Copying, deep-copying,
and unpickling a protection value restore the snapshot that was already settled
rather than re-reading the filesystem, so a duplicate can never come back
describing somewhere else; `__copy__` and `__deepcopy__` return the value itself
and `__reduce__` hands the four settled fields to a private rebuild function that
performs no filesystem access. `_make` and `_replace` remain construction, so they
still recompute the derived field and still refuse a forged one. A layout-only policy could not
see any of them, because all three are configurable and a valid configuration may
place them inside an enabled root. `FilesystemProtections` is a sealed,
tuple-backed value carrying those three canonical paths and nothing else;
`core/config.py` builds it from the active configuration and stores it on
`MariaConfig`, and this module never imports `MariaConfig` or reads configuration
itself. `is_universally_excluded` is therefore one half of a policy rather than
the policy, and a capability that applied it alone would hand out whatever the
running configuration happened to place in a root.

`FilesystemProtections` answers two different questions over that same data.
`excludes` settles *direct* reachability, which is what reading and listing need.
`intersects_mutation` settles whether relocating a path would disturb an active
location, which is wider because renaming or moving a directory carries everything
beneath it: a path that merely *contains* the live database, a sidecar, the prompt,
or the log directory is refused as a mutation operand. Keeping the two questions
apart is exactly what lets an ordinary parent stay listable with its protected
children filtered out while remaining immovable. `tools/filesystem.py` asks the
wider question in `_operands`, after placement and direct reachability and before
the policy check, the same-root check, and every existence check, and it widens the
repository barrier the same way, so a synthetic root cannot relocate the checkout by
naming its parent.

Both halves are evaluated against two identities of a submitted path.
`FilesystemRoots.resolve_path` returns a sealed `ResolvedPath` carrying the
governing root, the absolute pre-resolution candidate spelling, and the canonical
target. Checking only the target would let a protected name such as `Maria/logs`
be reached by being a junction pointing out of the repository; checking only the
candidate would let an ordinary name be a link pointing in. Root containment stays
a question about the target alone, and neither identity is ever rendered into
owner-visible text, a log record, or result data.

The general filesystem repository barrier lives in `tools/filesystem.py` instead,
because it is a property of the four generic commands rather than of the path
model. It makes any path at or beneath the repository root unreachable by `read`,
`list`, `rename`, `move`, and `organize`, and it is applied symmetrically to both identities,
which keeps tracked source, documentation, prompts, `config.toml`, and arbitrary
untracked repository files out of generic filesystem reach even under a root
declared around them.

The Project Maria capability of V1-24-R1 reuses this model unchanged: the
project file reader builds every candidate from a root's canonical path as
configuration settled it, applies `FilesystemProtections.excludes` to every
prefix from the root to the candidate before any filesystem call, and adds its
own admission on top, membership of the approved path list in
`core/project_maria_manifest.py`, rather than a tracked-source check; it
introduces no second path model. It reads the `project_maria` root whether or
not that root is enabled for the generic commands, whose repository barrier
keeps refusing every repository path; making the generic tool's root
available is not how the capability is switched on. See "Projects and Project
Files". A later stage that settles an export location must add that location
to the same universal exclusion boundary.

## Persistence Models

The database carries its own schema version, stamped in the SQLite header and
advanced only by `core/memory.py`. It is a separate number from `APP_VERSION`
and is never derived from it, so the application version can stay frozen while
the schema advances. Startup settles that version once, classifies an
unversioned database structurally against the shapes Maria actually shipped,
runs any missing forward migrations in order, and refuses a database stamped
newer than the running build before writing anything.
`docs/database.md` describes the rules; the ten record types below are
unchanged by them. The current schema is version 9, whose one appended
migration, `create_project_records` (V1-24-R1), creates the three empty tables
`projects`, `project_facts`, and `project_activation`, and touches no existing
table or row; version 8's `add_reminder_delivery_state` (V1-23-R1) added each
reminder's delivery state and created the empty one-row delivery setting
table, rewriting no row; version 7's `create_tasks_and_reminders` created the two
empty tables `tasks` and `reminders`, version 6's
`create_conversation_records` the two conversation-record tables, and
version 5's `create_audit_summaries` the two audit tables, the same way.

Maria keeps ten kinds of persistent data separate:

- **Interaction history** records cleaned requests and the responses chosen
  for recording on the four recording branches described above, for every
  plan whose recording policy is `RECORD`, whether by default or set
  explicitly; a `TRANSIENT` plan adds no row. Neither Brain nor the planner
  reads these rows, and no automatic path supplies one to the reasoning model.
  Interaction rows are never converted automatically into explicit memories.
- **Notes** are structured title-and-body records deliberately created by the
  user. The notes tool uses the shared `Memory` persistence interface. A note's
  title and body are edited through two separate confirmation-gated commands,
  so neither can overwrite the other; both preserve `created_at` and refresh
  `updated_at`. A note may also be deleted by numeric ID. None of those
  mutations cascade into interaction history or explicit memories. Each note
  also carries the owner's context consent, off unless the owner enables that
  one note through a confirmation-gated command; the consent is stored as the
  identity of exactly the title and body the owner approved, and a retitle or
  an edit turns it off in the same transaction. Only such notes can reach the
  reasoning model, through a literal search; see "Tier B Search Context"
  below.
- **Explicit memories** are deliberately stored facts. They are created only
  through the strict `remember <fact>` command or the public `Memory` API,
  searched through `recall <query>`, and listed through `memories`. A memory's
  fact may be replaced, and a memory may be forgotten, through two separate
  confirmation-gated commands addressed by numeric ID. An update preserves
  `created_at` and adds no `updated_at`, so an edited memory keeps its place in
  newest-first results rather than moving to the top — deliberately unlike an
  edited note. Fact uniqueness stays casefold-based, so an update whose
  casefolded fact already belongs to a different memory is refused with a
  controlled, content-free conflict result instead of changing either row.
  Neither mutation cascades into interaction history or notes. Each memory
  also carries the owner's context consent, off unless the owner enables that
  one memory through a confirmation-gated command; updating a memory turns its
  consent off in the same transaction, because consent covers the exact text
  the owner approved. Only enabled memories can reach the reasoning model; see
  "Tier A Memory Context" and "Tier B Search Context" below.
- **Audit summaries** record that the owner approved one model-proposed
  execution attempt and that Maria authorised it for dispatch. Each is one
  `audit_summaries` row, holding Maria's own UTC timestamp, the action's
  canonical identifier, the decision `owner_approved`, and a generalised
  target, `none` or `named_filesystem_root`, with at most one `audit_outcomes`
  row, `succeeded` or `failed`, appended after dispatch. Every value comes
  from the closed vocabulary in `core/audit.py` and is validated when written
  and again when read; none holds the request, the proposal, an argument, a
  fingerprint, private context, a root's name, a host path, a tool's result,
  or exception text. Only Brain writes them, through the memory layer and only
  for an approved model-originated plan, and normal writes only append. The
  confirmation-gated `delete audit <id>` is the one removal: it takes one
  summary together with its outcome in one transaction, and ids are never
  reused. Nothing cascades between summaries and the other record types.
  Neither Brain nor the planner reads them, and no automatic path supplies one
  to the reasoning model; see "Model-Originated Execution and Audit" below.
- **Saved conversations** are exact, immutable copies of the exchanges one
  session retained, each stored by an explicit `save conversation` as one
  canonical compact JSON transcript of 1 to 8 `[user, assistant]` pairs, at
  most 8,192 UTF-8 bytes of text, validated when written and again when read.
  They are never edited; `delete conversation <id>` removes one by id after
  confirmation, and ids are never reused. They carry no consent and are Tier
  C: no automatic path reads one, for planning or for the reasoning model.
- **Conversation summaries** are texts of at most 2,048 UTF-8 bytes, each
  equal to its own stripped form, that the owner approved word for word after
  the local model drafted them on request, or later replaced with
  `edit summary`; each keeps `created_at` and `updated_at`. Each carries the
  owner's context consent, off until the owner enables that one summary
  through a confirmation-gated command; the consent is the identity of exactly
  the approved text, and an edit turns it off in the same transaction. Only
  such summaries can reach the reasoning model, through a literal search; see
  "Saved Conversations and Summaries" and "Tier B Search Context" below.
  Nothing cascades between the conversation records and any other record
  type, and these are never the content-free audit summaries above.
- **Tasks** are one line of the owner's text, 1 to 512 UTF-8 bytes, with an
  optional calendar due date, and **reminders** one line of text with one
  moment: `remind_time`, the canonical local time, offset, and IANA zone the
  owner approved, and `remind_utc`, exactly the instant those encode, which
  alone orders reminders and selects due ones. Each is `open` or
  `completed`, keeps `created_at` and `updated_at`, has `completed_at` only
  while completed, and carries its own context consent, the identity of
  exactly its approved text and timing. They are added, edited, rescheduled,
  deleted, and given or denied consent only through confirmation-gated
  commands, one row by id, while listing, due queries, reading, completing,
  and reopening are allowed; nothing recurs, no task is ever delivered, and
  ids are never reused. Tasks and reminders are independent of each other and
  of every other record type, with no link and no cascade; see "Tasks and
  Reminders" below. A reminder also keeps its delivery state, a scheduling
  revision and its current revision's one claim, in its own row; that state
  is not a record type of its own, and neither is the one-row delivery
  setting. See "Reminder Delivery".
- **Projects** each name one place Maria may read: a folder of the workspace
  root, the workspace's top, or Project Maria, with a name of 1 to 64 UTF-8
  bytes on one line and no colon, and UTC stamps; locations never overlap,
  compared ignoring case. **Project facts** are one line of 1 to 512 UTF-8
  bytes each that the owner approved word for word, stored with their
  project's id and the approval digest, the SHA-256 of the compact JSON
  `[project_id, text]`. A project is renamed, deleted, or activated, and a
  fact added, edited, or deleted, only through confirmation-gated commands,
  one row by id; a project is deleted only while inactive and without facts,
  nothing cascades, and ids are never reused. The one-row activation setting,
  which names the active project, if any, with a change counter, is not a
  record type. Only the active project's facts whose stored project and text
  still match their digest can reach the reasoning model; see "Projects and
  Project Files".

On the completed paths described above, memory commands and their responses
also appear in interaction history when recording succeeds. A stored fact can
therefore exist both as an explicit memory and as part of an interaction.
Because Brain records the cleaned request verbatim, an `update memory <id>:
<new fact>` command retains its new fact in `interactions.user_input` even
though the displayed and recorded response is only `Updated memory <id>.`;
`recorded_message` can shorten the response half, never the request half.
Forgetting or later updating that memory does not rewrite or remove those
independent interaction rows.

Each persistent mutation a command makes commits in its own transaction, and
Maria then makes a separate attempt to record the command and its short
response. The two are never combined into one transaction: if the later
recording fails, the mutation remains committed, the successful response is
unchanged, and no interaction row recording it is guaranteed. There is no
compensating rollback, retry, or replay.

## Reasoning Interface and Local Override

`core/reasoning.py` is the contract every later model stage builds on, and it
deliberately contains no transport, no model, and no provider. A
`ReasoningRequest` holds one bounded prompt; in `history`, the earlier
exchanges of the current conversation, oldest first; and in `context`, the
records Maria selected — Tier A memories, tasks, and reminders, then Tier B
search matches — each a `ContextItem` from `core/context.py` bearing the source
and record id Maria read, and for a task or reminder its timing. Both are
empty by default. A request's one switch,
`proposal_instructions`, must be exactly `True` or `False` and is `False` by
default; it selects Maria's own fixed `PROPOSAL_INSTRUCTIONS`, which no caller
can change, and a request with it off is exactly what a request was before
the flag existed. Each
`ReasoningExchange` is one user text and one assistant text, exact, non-empty,
and redacted from every representation, and so is every `ContextItem`. There
is no field through which a caller's own system prompt, a template, or raw
interaction history could travel, and a `ReasoningResponse` holds one bounded,
untrusted string.

A request's size is defined once, by `request_content_bytes`: the UTF-8 length
of the prompt, plus the UTF-8 length of every exchange's user and assistant
text, plus the fully rendered context — Maria's fixed context instructions
and the delimited data block with every header, source, and id — which is
nothing when the context is empty, plus, when the flag selects them, the
proposal instructions and, with context, the two-byte separator that joins them
to the context instructions. No role name, message structure, model
identifier, or transport framing counts. The context is validated again every
time it is measured, rather than trusted from construction: at most 16 Tier A
items first, memories, then tasks, then reminders, rendering on their own to
at most 2 KiB, then at most 8 Tier B
items grouped by source, each group in strictly ascending id order with no
memory in both tiers, at most 24 items rendering to at most 4,096 bytes in all.
Construction bounds that total by the 1 MiB hard
ceiling. `request_reasoning` measures it again against the configured
`max_input_bytes` before the provider is asked, re-reading every text rather
than trusting construction and stopping as soon as the total passes the
bound, so a request tampered into holding far more is never copied to be
measured. The
`ReasoningProvider` protocol is what the local adapter,
`core/ollama_adapter.py`, implements.
`request_reasoning` is the one boundary through which a provider is asked
anything: it answers without asking when reasoning is disabled or the request
is over budget, asks at most once, and returns either a fresh response holding
inert text or a typed `Failure` from the reasoning kinds in `failures.py`. A
provider's own exception never travels, a reply that is not exactly a bounded
response is malformed, and an oversized reply is discarded whole rather than
truncated. The module's own imports include no network, threading,
asynchronous, or subprocess module and nothing from planning, permissions,
dispatch, persistence, or tools, so nothing in it can turn a reply into an
action. A reply's one route to an action runs through Brain, which hands a
proposal attempt to the compiler, and from there only through a permission
check, the owner's approval, and a committed audit summary; see
"Model-Originated Execution and Audit". Prompts, replies, the host, and the
model identifier are never shown by `repr()` or `str()`, never interpolated
into an exception message, and never logged; the boundary logs only an outcome
and a failure code.

`ReasoningSettings` describes the one owner-controlled provider: a destination
class (`loopback` or `private_lan`, which names where the host may be and
selects no protocol, port, or runtime), one canonical IP address inside that
class, an opaque model identifier, a hard deadline in whole seconds, and input
and output budgets in UTF-8 bytes. Enabled settings require every field, with
no defaults, and every value is checked by exact type, range, and enumeration.
There is no field for a port, endpoint URL, model path, credential, token, or
header.

The tracked `config/config.toml` carries a disabled template, `[reasoning]`
with `enabled = false` alone, and `core/config.py` refuses a tracked file that
enables reasoning or names any provider value. Machine-specific settings live
only in the owner's untracked `config/config.local.toml`, which
`Maria/.gitignore` ignores, Maria never creates, and the universal permanent
exclusion keeps out of every filesystem capability. `load_config` reads it only
when a caller names it; `MariaBrain.from_defaults()` is the one caller that
does. An absent override leaves the shipped disabled configuration. A present
one must be a regular file — a link, including a dangling one, a directory, or
a special entry is refused before anything is opened — holding a
`[reasoning]` table, a `[time]` table, or both, and nothing else. A
`[reasoning]` table replaces the tracked table completely with nothing merged
or borrowed; a `[time]` table holds exactly one key, `zone`, an IANA time zone
key of at most 64 characters checked by syntax alone, which becomes
`MariaConfig.time_zone`. The tracked file may not hold `[time]` at all. The
override cannot reach any other section. Every refusal
is a fixed message raised with no cause or context, so neither the file's text
nor a parser's or the operating system's error text survives in it, and
configuration loading logs nothing.

## Local Ollama Adapter

`core/ollama_adapter.py` is the one local runtime the owner selected for v1.0
(decision V1-13-R1): Ollama's native HTTP API on the owner's own machine.
`OllamaReasoningProvider` implements `ReasoningProvider`, and everything
specific to Ollama — the destination, the request and reply shapes, and the
HTTP framing — is confined to that module. It is the only production module
that imports a network module, and it imports only `socket`: no HTTP library,
so no proxy, redirect, or URL handling exists to be reached. Only `main.py`
constructs it, and only for enabled settings; Brain, the planner, memory, and
tools never import or reach it.

The destination is fixed in the module and is not configuration: the IPv4
loopback address `127.0.0.1`, port `11434`, and path `/api/chat`. Construction
re-validates the settings and refuses, with a fixed `ValueError`, disabled
settings, any transport but `loopback`, any host but `127.0.0.1`, and a model
identifier that designates an Ollama cloud model (a `cloud` tag or one ending
`-cloud`). So neither a `private_lan` host nor another loopback address can
ever be contacted. Each request opens one IPv4 connection straight to the
numeric address, with no name resolution, no proxy, no redirect, no retry,
and no fallback of any kind.

The request carries only the model identifier, as an opaque JSON string, and
the conversation, with `stream` false: each earlier exchange as a `user`
message followed by an `assistant` message, oldest first, and then the prompt
as the final `user` message. With the proposal-instructions flag off, a
request is what it was before the flag existed: with neither history nor
context it is the prompt as one user message, byte for byte the request V1-13
sent; with no context it is byte for byte the one V1-14 sent; and with Tier A
context alone it is byte for byte the one V1-15 sent. Brain's production path
always turns the flag on, so every request it sends begins with one `system`
message holding Maria's fixed proposal instructions. When the request carries
context, Maria's fixed context instructions follow them in that same message,
after a blank line, or form the only `system` message when the flag is off;
neither ever contains stored text. The delimited data block then follows as a
`user` message of its own. The context instructions are the V1-15 text unless
a Tier B item is present, when a fixed variant also says some records were
found because they contain a word from the current request; a third fixed
variant, which also names conversation summaries, is used only when a
conversation summary is among the items, so a request without one is byte for
byte what it was before summaries existed. There is no
option, tool, or thinking request. Before anything is serialized, the adapter
validates the context again and measures the prompt, history, rendered
context, and any selected instructions together against its own
`max_input_bytes` with the same `request_content_bytes` accounting, and
refuses an over-budget request as input over budget without opening a
connection. Of the reply, only
`message.content` is kept, as inert text. Thinking text, tool calls, and every
other field are ignored after bounded JSON parsing; only `message.content` is
returned. The module has no import path to planning, permissions, dispatch, or
persistence, so nothing in it can turn a reply into an action.

One absolute monotonic deadline, `request_deadline_seconds` from the start of
`respond`, covers connecting, sending, the response headers, and the body.
Every socket wait is given only the time that remains, so a runtime that
trickles bytes cannot stretch it. The status line and headers may take at
most 16 KiB. Only `200` carries an answer; any other status, a redirect
included, is unavailable. The body must be framed by one `Content-Length` or
by chunked transfer coding, so a reply that ends early can always be told
from a complete one. The whole body, chunk framing included, is bounded
before parsing by an envelope of 64 KiB plus six times `max_output_bytes`,
because JSON may spell one byte of the answer as up to six. The extracted
answer must then be non-empty and fit within `max_output_bytes`. Nothing
oversized is read in full, and nothing is truncated or returned in part.

Every other outcome is one typed reasoning failure: input over budget before
any connection; timeout at any stage; unavailable when nothing answers or the
status is not success; truncated when the reply ends early, `done` is false,
or `done_reason` is `length`; malformed for any other unexpected shape; and
output over budget for an oversized envelope or answer. The failure is raised
outside every handler, so no caught exception travels with it as a cause or
context, and every connection is closed on every path. The adapter logs
nothing itself; `request_reasoning` records the outcome code, and no prompt,
reply, address, model identifier, endpoint, header, or runtime error text
reaches a log or a failure message.

Ollama's own local-only setting is separate and is the owner's to configure.
The adapter's refusal of cloud-tagged model identifiers is defense in depth
and cannot prove the runtime will not reach a cloud model.

## Deterministic-First Conversation

With a reasoning provider present and reasoning enabled, Maria answers the one
kind of request its planner has no command for, its exact generic fallback,
with the owner's local model, in the context of the current session's earlier
conversational turns: with a text reply or, when the model proposes one of the
five actions it may propose, or a plan of two to five of them, and the
proposal passes every check, with a plan offered for the owner's approval. Any other proposal is refused. Everything
else is exactly as it was.

```text
CLI
-> MariaSession (conversation and supplied-record fingerprints)
-> MariaBrain.process(request, *conversation, supplied_context=...)
-> Planner                               always first, text only
-> provider, enabled, exact fallback?    otherwise the ordinary path
-> Memory.list_context_memories(17)      Tier A's memory read
-> Memory.list_context_tasks(17)         Tier A's task read
-> Memory.list_context_reminders(17)     Tier A's reminder read
-> Memory.context_*_by_id(...)           recheck of supplied records, if any
-> Memory.search_context_*(terms, 9)     Tier B reads, only when there are terms
-> conversation.converse(proposals=True) recheck, budget, selection, notices
-> request_reasoning -> ReasoningProvider
-> reply opens with "{"?                 if so, to Brain's compiler instead
-> zero-step TRANSIENT plan
-> Brain's ordinary zero-step path       no contract, permission, tool, or row
-> MariaSession keeps the new conversation and fingerprints
```

Resolution order lives in Brain. The planner runs first on every request and
is unchanged. Brain consults the model only when all three hold: a reasoning
provider was injected, `config.reasoning.enabled` is true, and the planner's
plan is exactly its generic no-action fallback, recognised by
`conversation.is_generic_fallback()`: a default-recorded plan with no steps
whose response is exactly `GENERIC_FALLBACK_RESPONSE`, a named copy of the
planner's text that a test holds to the planner's real output. Help,
`commands`, and every usage error are zero-step plans with other text, so they
never reach the model; blank input returns before planning; and the
confirmation path never plans at all. With no provider, which is what the
shipped configuration produces, or with disabled settings even when a provider
was injected, the fallback is answered and recorded exactly as before: its
original response and its interaction row.

`conversation.converse()` settles the whole turn:

- The budget is 8 KiB (`CONVERSATION_MAX_BYTES`) or the configured
  `max_input_bytes`, whichever is smaller, counted as `request_content_bytes`
  counts: the current request, Maria's fixed proposal instructions with the
  separator that joins them to any context instructions, the fully rendered
  context, and the UTF-8 text of the retained exchanges' user and assistant
  halves.
- Every context read is settled first. A failed or untrustworthy read, or a
  supplied memory, note, summary, task, or reminder that has since been
  disabled, changed, deleted, forgotten, or, for a task or reminder,
  completed, clears the earlier conversation before anything is sent; see
  "Tier A Memory Context" and "Tier B Search Context" below.
- A request that is over the budget together with the proposal instructions
  is refused with the fixed input-over-budget message before the provider is
  asked; the instructions are never dropped to make it fit. Text the interface
  cannot carry, such as a lone surrogate, is refused with its own fixed
  message.
- The request and the proposal instructions come first, then as much Tier A
  context as its rule fits, then as many Tier B matches as fit, memories, then
  notes, then summaries, then the newest earlier exchanges that fit beside all of them,
  at most eight (`CONVERSATION_MAX_EXCHANGES`); the oldest are dropped first,
  whole. Nothing is truncated. A conversation that would then carry more than
  64 supplied records is cleared before sending instead.
- The provider is asked exactly once through `request_reasoning`. A typed
  failure, a crash, or a malformed reply becomes that failure's fixed message.
- A reply whose first character after JSON whitespace (a space, tab, carriage
  return, or line feed) is `{` is a proposal attempt. Nothing here parses it:
  it goes back to Brain untouched, with the turn's notices, and Brain hands it
  to the compiler; see "Model-to-Plan Compilation Gate". It never becomes an
  exchange, and the fingerprints of the records the request carried are not
  kept either, while any clearing this turn already made stands. Any other
  first character, other whitespace or `[` included, keeps a reply
  conversational.
- Any other reply is kept only if its whole exchange, request and reply
  together, fits the budget. Otherwise the reply is discarded with a fixed
  message: it is not shown in part, kept, or recorded.
- A reply that passes is shown verbatim and becomes the newest exchange. The
  session keeps the result, again bounded to eight exchanges and the budget,
  with the fingerprints of the records that request carried.
- Every outcome then ends with Maria's own notices about context, after a
  blank line. They are built from the sources and ids Maria selected, never
  from the reply, and they never enter the request or the exchange the session
  keeps. After a proposal attempt they follow whatever Brain answers, a
  refusal or the approval prompt, exactly once, and they are never kept with
  a pending approval or shown again when it is confirmed.

Every other outcome, reply or refusal, is a zero-step plan that
`conversation.py`, its producer, marks `TRANSIENT`, and Brain accepts nothing
else as a conversational outcome: anything but an exact zero-step, transient,
deterministic plan is refused with a fixed message. Brain runs it through its
ordinary zero-step path, which returns before contract validation, permission
resolution, or tool dispatch, and whose recording gate writes nothing for a
transient plan. No conversational request or reply is ever recorded, and Brain
never chooses a recording policy itself. A conversational reply is untrusted,
inert text: nothing parses it, it is never handed to the planner, it cannot
become a pending approval, and a reply that reads like a command or an
approval does nothing. Only a proposal attempt is ever parsed, by the compiler
alone, and even a compiled one runs only after the owner approves it.

What the model sees is Maria's fixed proposal instructions, the current
request, the session's own completed conversational exchanges, the explicit
memories and the open tasks and reminders the owner has enabled for context,
and notes and conversation summaries the owner has enabled for context that
literally contain a word of the request. It never sees a command or its
result, its own earlier proposals, an approval prompt, what an approved action
returned, a memory, note, summary, task, or reminder the owner has not
enabled, a saved conversation, a file, a stored interaction, an audit summary,
or anything from an earlier session. A
`summarize conversation` request is the one other request Maria sends, and it
carries less: her fixed summary prompt and the retained exchanges only; see
"Saved Conversations and Summaries". A new session starts empty, and ending
one discards its conversation and its supplied-record fingerprints.

## Tier A Memory Context

In V1-15, Tier A was exactly the owner's explicit memories that the owner has
enabled for context. Since V1-22 (decision D6, amendment V1-22-R1) it also
holds the owner's open tasks and reminders that the owner has enabled for
context, after the memories; see "Tasks and reminders" below. Since V1-24
(amendment V1-24-R1) it also holds the facts of the owner's active project,
between the memories and the tasks; see "Project facts" below. Preferences
exist only as memories the owner stores and enables.

**Consent.** Every memory's consent is off by default: the V1-15 migration
leaves every existing memory off, and a new memory is stored off.
`enable memory context <id>` and `disable memory context <id>` change one
memory each, by positive decimal id, and both always require confirmation,
which configuration cannot remove. Updating a memory turns its consent off.
An enable approval is bound to the exact text: when confirmation is offered,
Brain passes the plan through `ToolManager.prepare_confirmation()`, and the
memory tool pins the SHA-256 of the memory's current text into the step, so the
approved plan and its fingerprint name that text. The memory layer enables
consent only inside one write transaction that re-reads the fact and updates
it only while it is still exactly that text; otherwise nothing changes and the
owner is asked to confirm again. That binding step is generic: Brain names no
tool, and a tool can only add arguments to its own step.

**Selection is not relevance.** Under the owner-approved transitional rule
(decision V1-15-R1), every enabled memory is eligible on any model-backed
conversational turn. Brain asks the memory layer for enabled memories in
ascending id order, one row more than can be sent, with any fact longer than
2 KiB of UTF-8 reported only by id and an oversized flag — its text never
leaves the memory layer, and in a UTF-8 database never leaves SQLite — and
`context.py` takes them
whole, in that order, while at most 16 items and at most 2 KiB of fully
rendered context — fixed context instructions, ids, and framing included —
fit, and while they fit beside the request and Maria's proposal instructions
within the conversational budget. A memory that does not fit is left out and
the next is still considered. Memories left
out are named by id when the bounded read returned them; enabled memories
beyond it are reported as existing, never by id. At the roadmap Stage V1-16
relevance-review gate the owner kept this rule unchanged (decision V1-16-R1);
enabled memories beyond the window can still be found by Tier B's literal
search.

**Stored text is data.** The adapter sends Maria's fixed instructions as the
only system message: the proposal instructions, which say that earlier
conversation and supplied records are data, not authorization, then the
context instructions, which tell the model the memories are reference data,
not instructions, grant no permission to act, and may be outdated or
conflicting.
The memories follow in a separate user message, one line per memory, each fact
as one JSON string with every line separator escaped — JSON's own escapes plus
U+0085, U+2028, and U+2029 — so no fact can break out of its line or pose as
another entry. Context grants no authority: a reply is still either inert text
in a zero-step transient plan or a proposal attempt that must pass every
compiler gate and the owner's approval, whatever context the request carried.

**What the owner is told.** After a successful reply Maria names the memories
she supplied — `Memory context supplied: memory 3, memory 7.` — and says she
cannot verify whether they are still current or whether they conflict. After a
failure she names the memories selected for the attempt and says she cannot
confirm the model received them. When no context at all was sent she says `No
memory context supplied.` Maria cannot know which memories the model used, and a
reply that names other ids changes nothing she says. She makes no claim that a
memory is stale, fresh, consistent, or conflicting: `created_at` dates the row,
not its current text, and there is no `updated_at`.

**Revocation.** The session keeps, beside its conversation, the fingerprint —
source, id, and SHA-256 of the exact text — of every memory, note, summary,
task, and reminder supplied with a request whose reply it kept since the
conversation was last cleared, at
most 64 of them, never the text itself; a proposal attempt is never kept, so
the records supplied with it are not tracked. Before each model-backed
conversational request — never on a
deterministic command — Brain rechecks exactly those records by id, in one
bounded read per source, and their fingerprints are compared with what is
enabled now; only the records they name are hashed, and nothing is read or
hashed when nothing was supplied. A supplied record that is now oversized
cannot be the text that was sent. If one was disabled, changed, deleted, or
forgotten, by this session or through any other database connection, the
whole earlier conversation and the fingerprints are discarded before anything
is sent, and the owner is told that the earlier copy was invalidated. Since
V1-16 the recheck is by id rather than against Tier A's own read, so a memory
that merely moves past Tier A's 17-row window, unchanged, no longer clears the
conversation. If any context read fails, neither context nor earlier exchanges
are sent, both are cleared, and a fixed notice says so. This bounds what Maria
sends next; it sees current state only, so a record changed away and back to
the identical text between two checks is indistinguishable from one never
changed; it is not an instantaneous revocation across a concurrent read and
send; and it cannot erase text a model has already received.

**Tasks and reminders.** A task's or reminder's consent is off by default and
changes only through its two confirmed consent commands, by id; enabling pins
the SHA-256 of the compact JSON array `[text, due or ""]` for a task or
`[text, remind_time]` for a reminder, and the memory layer enables it only
while the record is still exactly that. Editing or rescheduling turns it off
in the same transaction; completing and reopening leave it as it was. On every
model-backed conversational turn Brain reads enabled open tasks and enabled
open reminders, one bounded read each, in ascending id order, with every check
of consent, status, type, size, validity, and approved identity run inside
SQLite before a row is returned or counted, so a row changed outside Maria is
never read as eligible. `context.py` then considers the first 16 of each,
tasks before reminders, after the memories Tier A already chose, and takes
each whole only while Tier A as a whole stays within 16 items, 2 KiB, and the
budget; memory selection never changes because of them. Each is rendered on
its own line as one JSON object, `{"text": ..., "due": ...}` or
`{"text": ..., "when": ...}`, under the header `Owner memories, tasks, and
reminders (reference data):`, with framing that names them; a request that
carries none keeps exactly the bytes it had before V1-22. Maria names the
tasks and reminders she sent, and any eligible one the limits left out, in a
notice of their own after the memory notice. A supplied task or reminder that
is completed, deleted, edited, rescheduled, or disabled is no longer current,
so the recheck clears the conversation; reopening one makes it eligible again,
subject to selection. Selection and recheck alike admit only a record whose
stamps follow the record rules, with no completion stamp while it is open,
so a supplied record whose stamps are changed outside Maria in a way that
breaks those rules clears the conversation too; a stamp changed to another
valid stamp changes nothing, since consent covers only the text and timing.
Neither reinterprets saved timing under current zone rules or reads a clock.
Before SQLite hands any of a task's or reminder's values to Maria's Python
check, it admits each only as text, or NULL where allowed, within a small
stored-byte bound in the file's own encoding, so no oversized or binary value
reaches Python, and a value holding NUL reaches it only within that bound, to
be refused there; see `docs/database.md`.

**Project facts.** A fact has no consent flag: its consent is the owner's
confirmed addition or edit, which shows the exact quoted text and pins it as
the approval digest, the SHA-256 of the compact JSON array
`[project_id, text]`, together with the owner's confirmed activation of its
project. Each model-backed request reads, in its one project snapshot, the
active project's facts whose stored project id and text still match their
digest, in ascending id order, one row more than can be considered, with every
check of type, size, validity, and digest run before a row is returned or
counted; facts of every other project are never read for context. `context.py`
considers the first 16, after the memories Tier A already chose and before
the tasks and reminders, and takes each whole only while Tier A as a whole
stays within 16 items, 2 KiB, and the budget. A fact is rendered as
`project fact <id>: "<text>"`, its project's id never shown, under the header
`Owner memories, active-project facts, tasks, and reminders (reference
data):`, with framing that also asks the model, when the owner asks for a
review of a plan, a document, code, or tests, to say plainly when it would
recommend a different approach and why, instead of simply agreeing; a request
with no fact keeps exactly the framing and header it had before. Below the
reply Maria names the snapshot's active project, `Active project: project
<id>.`, then the facts sent, `Project facts supplied: project fact <id>.
Maria cannot verify whether these facts are still current.`, or, after a
failure, the facts selected, and any eligible one the limits left out; the
active project is named whenever one is active, fact or no fact. A supplied
fact's fingerprint digest is its approval digest, so the recheck, which reads
the supplied facts by id within the same snapshot and only for the active
project, finds it current only while it is still a fact of the active project
with its approved text: an edit, a deletion, a deactivation, or a switch to
another project clears the conversation, with the fixed notice `This
session's earlier conversation was cleared because a project fact supplied to
it no longer belongs to the active project with the same approved text, so the
copy supplied earlier was invalidated.` A request carrying a fact is also held
to the final check described under "Projects and Project Files". The digest
is an integrity check against changes made outside Maria's confirmed commands,
not authentication.

## Tier B Search Context

Tier B is what a bounded literal search of the current request finds among
records the owner has enabled for context. V1-16 gave it two sources, enabled
memories beyond Tier A's window and notes the owner has enabled with
`enable note context <id>`, and V1-20 adds a third, conversation summaries the
owner has enabled with `enable summary context <id>`. Nothing without that
consent is ever searched, and a saved conversation never is.

**Note consent.** Every note's consent is off by default: the V1-16
migration leaves every existing note off, and a new note is stored off.
`enable note context <id>` and `disable note context <id>` change one note
each, by positive decimal id, and both always require confirmation, which
configuration cannot remove. The enable confirmation says the note's entire
title and body may reach the local model on a later literal match, and that
Maria does not check the note for passwords or other secrets; consent is not a
claim that the text holds none. Below that notice the confirmation shows the
note's current title and body in full, each as one quoted line in which every
invisible or line-breaking character is escaped, so the owner sees exactly the
text being approved and no stored text can pose as Maria's own line. The
approval is bound to that shown title and body: the notes tool reads both once,
and from that one read both builds the preview and pins their identity, the
SHA-256 of both together as one JSON array, into the step. Only the identity
enters the pending plan; the preview is shown through Tool Manager's generic
`ConfirmationBinding` and is never recorded, logged, retained in the
conversation, or sent to a model. The memory layer enables consent only inside
one write transaction that re-reads both and stores that identity as the
note's `context_digest`, so a title or body changed before the owner confirms
is refused and nothing is enabled. Retitling or editing the note turns
it off in the same transaction. Because the stored consent is the approved
text's identity, a note whose title or body was changed through any other
connection, even one that left `context_digest` in place, no longer matches it
and is never eligible, in this session or a later one.

**The search rule.** `context.search_terms()` casefolds the cleaned request,
splits it on whitespace, strips every non-alphanumeric character from both
ends of each word, keeps words of 4 to 64 characters, drops repeats in
first-seen order, and takes the first 8. A short request such as "Jo" or "AI"
has no terms, and a request with no terms runs no Tier B read at all. Matching
runs in a Python matcher registered on the one read's connection, which SQLite
hands a text only after that row's stored size is within the bound, and which
settles the text's exact UTF-8 size before casefolding it. A memory whose fact
is within 2 KiB matches when a term occurs literally in its current fact,
casefolded, so a stored casefolded copy left stale by another connection can
neither add nor hide the match. A fact too large to supply never leaves SQLite
and cannot be casefolded, so it is matched against its stored casefolded copy
instead and can only be named, by id; Maria writes that copy with every fact,
but a copy changed outside Maria can name an oversized memory that no longer
holds the term, or miss one that does. A note is searched only while its
consent is on, both texts are within 2 KiB, and an approval check inside
SQLite finds they are still exactly the approved text; only then does the
matcher see either text, and the note matches when a term occurs wholly within
its casefolded title or wholly within its casefolded body; the two are never
joined. A note changed outside Maria is therefore never matched and never uses
up a read's rows, and a note with a text too large to verify is never
searched. A conversation summary follows the note rule for its one text: it is
searched only while its consent is on, its text is within 2 KiB, and the
approval check finds it is still exactly the approved text, and it matches
when a term occurs wholly within its casefolded text. There are no
model-generated queries, `LIKE` wildcards, rankings, embeddings, or fuzzy or
semantic matching.

**Bounded reads.** The memory search covers only enabled memories after the
16th one Tier A considered, and runs only when Tier A's read found more than
16, so Tier A's 17th, lookahead, row is its first candidate and a memory Tier
A left out for size is never retried. Each source's read returns at most 9
rows in ascending id order; that limit bounds the rows returned, not every row
SQLite examines to find them. A memory beyond 2 KiB of UTF-8, measured exactly
whatever the database file's encoding, is returned only by id and an
oversized flag, so it is named and never supplied, and it counts toward its
read's rows. A note search never returns a note by id only: a note whose title
or body is beyond 2 KiB cannot have its approved identity verified, so it is
neither matched nor named, and a note search result that named one would be
treated as an unreadable read. A summary search behaves the same way.

**Selection and bounds.** The first 8 rows of each source are candidates,
memories, then notes, then summaries, each in ascending id order, and are
taken whole while fewer than 8 have been taken and everything rendered still
fits. Tier A keeps
its 16 items and 2 KiB; both tiers together are at most 24 items and 4,096
fully rendered UTF-8 bytes — context instructions, headers, source ids, JSON
quoting, and escaped line separators included — and everything fits beside
the request and Maria's proposal instructions within the conversational
budget. The request and those instructions come first, then Tier A, then
Tier B memories, then Tier B notes, then Tier B summaries, then earlier
exchanges. The limits are validated again when a `ReasoningRequest` is
constructed and in the adapter before any connection. A summary's 2,048-byte
cap never guarantees that it is selected: one that does not fit is left out
whole and named by id.

**Rendering and notices.** Tier B records follow their own header in the same
data block, each on one line as its source and id and then JSON: a memory's
fact as a string, a note's title and body as one object, a summary's text as a
string, with the same line separator escapes as Tier A. The header and the
context instructions name conversation summaries only when a summary is among
the items, so a request without one is exactly what it was before. After a successful reply Maria names them —
`Found by search and supplied: memory 21, note 4.` — with the same caveat that
she cannot verify whether they are current or conflict; after a failure she
names them as selected; matches the limits left out are named by id when a
read returned them, and further matches are reported as existing. Notes that
share exactly a title but have different bodies are named together as
possible divergent versions; Maria never calls them contradictory, never calls
a record stale, and treats a note's `updated_at` only as the fact of when it
was last saved.

## Saved Conversations and Summaries

V1-20 lets the owner keep what a session holds, in two record types that are
never written without an explicit command; see "Persistence Models".

**Session inputs.** Two action contracts declare an argument whose text only
Maria supplies, from the session: `conversations.save` takes the canonical
transcript of the retained exchanges, and `conversation_summaries.create` a
summary draft. The planner's `save conversation` and `summarize conversation`
plans name the action and no text. In `process`, and only there, Brain reads a
deterministic plan once, as a bounded snapshot, asks Tool Manager's
`session_input_argument` whether its step declares a session input, and lets
`conversation.py` settle the declaration and build the resulting plan, so
Brain names no tool or action and chooses no recording policy. Only a
planner's plan holding exactly DETERMINISTIC as its own origin is asked. A
plan that already carries the declared argument, or holds any other step, is
refused with a fixed message, and so is a declared input of any kind Brain
does not name, so a new kind is refused until Brain is changed to supply it.
Nothing is supplied at confirmation, for a model-originated plan, which the
compiler refuses for these actions in any case, or on the conversational
path, and a contract that declares a session input cannot be model-eligible.

**Saving.** The transcript is one compact JSON array of `[user, assistant]`
pairs, oldest first: exactly the exchanges the session retains and nothing
else, since no notice, context item, command result, proposal, or approval
text is ever an exchange. The save reads no other record and asks no model,
and the memory layer's write is one INSERT. The plan keeps its default
`RECORD` policy, so the interaction row holds the command and the fixed reply,
which names only the new id and the exchange count; with no retained exchange
the plan becomes a zero-step plan answering that there is nothing to save,
recorded the same way. The conversation is left as it was. Because a save
rechecks nothing, text quoted from a record revoked since is kept verbatim,
which the reply's sentence about an exact copy states; a saved conversation
never reaches the model, so that copy cannot travel back to it.

**Drafting.** For `summarize conversation`, Brain first checks the step's
permission, so a denied summary never sends the exchanges anywhere, and then
requires an injected provider and enabled settings. With exchanges retained,
it rechecks by id every record supplied to the conversation, with no Tier A
read and no search, and `conversation.draft_summary()` refuses before any
request when there is no exchange, when that recheck fails or finds a record
changed, which also clears the conversation and its fingerprints, or when the
fixed 336-byte prompt with every retained exchange is over `max_input_bytes`;
the exchanges are never cut down to fit. Otherwise the provider is asked once,
through `request_reasoning`, with that prompt, every retained exchange, no
context, and the proposal instructions off, so a reply opening with `{` is
only text. The reply's outer whitespace is stripped once, and an empty result
or one over 2,048 UTF-8 bytes is discarded whole with a fixed message. Every
outcome is a transient plan, and a draft is never kept as an exchange.

**Approval.** A draft is bound, exactly, as the step's `text`, and
`conversation_summaries.create` always requires confirmation. Through Tool
Manager's generic `ConfirmationBinding`, the summaries tool pins the draft's
SHA-256 as `text_digest` and shows the whole draft below the fixed notice, as
one escaped line that decodes to exactly the bound text. Confirming makes no
provider call: the session hands Brain exactly the snapshot it fingerprinted,
and the tool stores the text only while the pinned digest still matches it.
Any other submit, a cancel, or the end of the session discards the draft, and
a store that fails still spends the approval. No audit summary is written: the
owner chose the action and approved its exact text, and the stored row is its
own trace.

**Consent and context.** A stored summary's consent is off.
`enable summary context <id>` reads the summary once, shows its whole text,
and pins its identity; the memory layer enables consent only inside one write
transaction that still finds exactly that text, and stores the identity as the
summary's `context_digest`. `edit summary` turns consent off in the same
transaction as the new text, and a summary changed any other way no longer
matches its identity and is never eligible. An enabled summary is a Tier B
source; see "Tier B Search Context".

## Model-to-Plan Compilation Gate

`core/plan_compiler.py` turns one untrusted reasoning-model proposal into a
plan, or refuses it. It is deny-by-default where `PermissionManager` cannot be:
`PermissionManager` allows any action absent from its mandatory sets, so the
positive gate lives here and runs before any permission decision. Brain is the
only production module that imports it, which a guard test enforces. Brain
compiles a proposal only on the model-backed conversational path: when a reply
is a proposal attempt, it hands `compile_proposal` the owner's cleaned request,
the reply exactly as it arrived, Tool Manager's active contract registry, and
the request's one timing context, which only a creation step uses.
A refusal is answered with its fixed message, followed by the turn's context
notices, and nothing is repaired, retried, or asked of the model again.

`compile_proposal` accepts exactly one JSON object, at most 4,096 UTF-8 bytes
however many actions it proposes, measured before parsing. A proposal for one
action holds exactly `tool`, `action`, and an `arguments` object, which is
required even when empty. A proposal for two to five actions, run in order,
holds exactly one field, `steps`, a list of two to five objects each shaped
exactly as a proposal for one action; one action is never proposed inside an
envelope, so no plan has two spellings. An object holding exactly `steps`
whose list is anything else, including fewer than two or more than five
actions, a nested list or envelope, or an action with a missing or extra
field, is refused with one fixed message, `The proposed plan did not have
exactly a steps list of 2 to 5 actions, each with exactly a tool, an action,
and an arguments object.` Any other object keeps the single-action refusal. A
reply opening with `[` is never a proposal attempt at all. Parsing is strict:
a repeated key at any level, `NaN` or `Infinity`, nesting deeper than eight
containers, surrounding prose, a code fence, a second document, and truncated
text are all refused, and nothing is repaired. The gates run in a fixed order
and the first failure decides the refusal: the byte cap, strict parsing, the
proposal's shape, and then, for each action in order, the allowlist and
eligibility, the action contract, then a text check and a path check on each
present argument. Compilation is all or nothing: a refusal of any action
refuses the whole proposal, and no part of it becomes a plan.

An action is open to the model only when two independent declarations agree:
the compiler's own closed `MODEL_ACTION_ALLOWLIST` and the contract's
`MODEL_ELIGIBLE` classification. A test proves they agree in both directions,
and changing either one alone opens nothing. Every argument is then validated
against its contract. An argument string may contain no control, format,
surrogate, line-separator, or paragraph-separator character; such a value is
refused, never stripped.

A model-supplied path must be plain text relative to the default named root:
no root, drive, UNC share, leading `~`, parent traversal, NUL, colon, or
reserved device name, under the Windows and the POSIX reading alike. Parent
traversal includes the spellings made only of dots and spaces that Windows may
read as it, such as `...` or `. .`. The path must also name something: a
standalone `.` or `./` is refused, while a harmless interior `.`, as in
`a/./b`, is accepted and kept exactly as written. This is a lexical preflight
with no filesystem access, so it cannot see a symbolic link and establishes
nothing about the resolved location; `FilesystemRoots` and `FilesystemTool`
still decide resolved containment and the permanent exclusions when a plan
runs. It is also a temporary restriction: a model cannot yet name a
non-default root or an absolute path.

A passing proposal becomes one `Plan` with one step per proposed action, in
the proposed order. Each step names Maria's own declared tool and action and
holds the arguments exactly as supplied, never repaired, reordered, defaulted,
or substituted: a value such as `$1` or `{{step1}}` is only literal text, and
no step can use another's result. The plan's `original_request` is the owner's
cleaned request, never model text; it is recorded as `TRANSIENT`; its origin
is `PlanOrigin.MODEL`; and it is immutable and fingerprinted like every other
plan, the fingerprint covering every step and their order. A refusal is one
`ProposalRejection` member with a fixed message that names no tool, action,
argument, or value the model supplied, and no parser or contract error
escapes. The gate logs only whether a proposal compiled and, when it did not,
the refusal code, never the proposal, a plan, or a fingerprint.

**Creation steps (V1-22).** `tasks.create` and `reminders.create` are the one
exception to arguments being kept as supplied, because the model never decides
whether the owner asked. A proposal holding a creation step passes its own
gates, in this order, the first failure deciding: allowlist and eligibility
for every step; the raw checks of each creation step's arguments, missing and
extra names, then type, size, unsafe characters, and printable ASCII for a
timing phrase (`text` 1 to 512 UTF-8 bytes and not blank, `when` and `due` 1
to 64 characters, `repeat` exactly `never`); then `core/task_requests.py`'s
parse of the owner's own request into one to five items in the four closed
forms; then the positional binding, one creation step per item, in order, of
its kind, with exactly its body as `text` and its phrase equal after
canonical tokenization (ASCII letters lowercased, tokens joined by single
spaces, digits as written), and a task's `due` present exactly when its item
names a date;
then the resolution of each item's own parsed phrase at the shared reference
instant, and the rule that no two items share a kind, a body, and a resolved
timing; and finally the ordinary step checks, on arguments Maria builds from
her own extraction. No model value enters such a plan. A proposal with no
creation step compiles exactly as before. The eight refusals these gates add
are fixed: not a creation request, an unclear request, a mismatch, a past
time, a time beyond the horizon, a nonexistent local time, an ambiguous local
time, and an unavailable time zone, so the compiler's kinds grow from 13 to
21. See "Tasks and Reminders".

`recheck_model_plan` applies the same gates again to every step of an already
compiled plan, in order, exactly as it stands: the allowlist with the
eligibility the current registry declares, the argument contract, then the
text and path checks. A plan holding a creation step is first checked against
its own request again, with no clock: the request is parsed once more, and
the plan must still hold exactly one creation step per item, in order, with
each item's body, a task's due date exactly when its item is dated, and no two
items alike; each canonical timing is then checked by its contract's format,
and nothing is resolved again. Nothing is
parsed, rebuilt, repaired, or substituted, and no provider is asked anything.
Brain asks it when it offers a compiled plan for approval and again just
before the approved plan runs, because an approval authorises a plan rather
than freezing the registry that was current when it was shown. A refusal logs
only its code.

## Model-Originated Execution and Audit

A plan compiled from a model proposal is the only way model output can lead to
an action, and it has one path to its tool:

```text
MariaBrain.process()                     provider, enabled, exact fallback
-> conversation.converse(proposals=True) one provider call
-> reply opens with "{"                  after JSON whitespace only
-> plan_compiler.compile_proposal()      Tool Manager's active registry
-> TRANSIENT MODEL plan of 1 to 5 steps  or a fixed refusal, never retried
-> recheck_model_plan()                  every step, unchanged plan
-> PermissionManager                     every step; any denied: refused whole
-> MariaSession                          renders every step, binds the approval
-> owner approves: MariaSession.confirm()
-> MariaBrain.execute_confirmed_plan()   no planner, context, or provider
-> recheck_model_plan()                  again, every step, current registry
-> PermissionManager                     again, every step; denied: none runs
-> for each step, in order:
   -> Memory.record_audit_summary()      committed, or this step does not run
   -> ToolManager.execute_tool()         at most once, approved arguments
   -> Memory.record_audit_outcome()      succeeded or failed
   -> the next step only after success with its outcome recorded
-> one step: the result, then "Recorded as audit summary N."
-> several steps: a numbered report of every step
```

**Origin.** `Plan` carries its origin as a `PlanOrigin`: `DETERMINISTIC`, the
default, for every plan Maria's own code builds, and `MODEL`, which only the
compiler sets. Like the recording policy, it must be an exact member and is
part of plan equality, hashing, and the fingerprint, which it joins only when
it is not the default: every deterministic plan keeps exactly the fingerprint
it had before, while a model-originated plan never fingerprints like an
otherwise identical deterministic one. A `MODEL` plan can only be transient,
with exact text as its request, one to five steps in an exact tuple, each an
exact `PlanStep` named by exact text with its immutable arguments, and no
canned response; any other shape is refused when the plan is constructed, and
every boundary that acts on one asks again whether it is still exactly that
shape. A plan missing any of its own fields, or holding a step missing one,
is not, and the question is answered no rather than raising; the boundaries
read a plan's fields from the plan itself, so a deleted field is never read
as its class default. V1-19 widened V1-18's one step to one to five.

**Approval.** Brain offers every model-originated plan that passes its checks
for exactly one approval from the owner, for the whole plan, even when
`PermissionManager` allows every one of its actions outright, and refuses a
plan with any denied action, giving the first such step's reason with the
turn's notices once, before any approval is offered or any audit summary
exists. That requirement is Brain's, keyed to the
plan's origin, and adds nothing to `PermissionManager`. A plan with no
origin of its own, one whose origin was deleted included, is refused by Brain
before either path is chosen and by the session at confirmation, and every
other plan whose own origin is not exactly `DETERMINISTIC` is treated this
way, so neither can ever run as a deterministic plan. An origin reassigned to
`DETERMINISTIC` is an exact member, so the final object alone cannot show
the change; the fingerprint captured when approval was offered detects a
change made after that, and the session refuses the confirmation.
The plan offered is
the very plan the compiler built: Brain never passes it through
`ToolManager.prepare_confirmation()`, which refuses a model-originated plan, so
nothing can be added to it. The session renders it and binds the approval to
its fingerprint, as "Session Boundary" describes, and cancelling, a new
request, ending the session, or a changed plan keeps it from running.

**Confirmation.** On approval the session hands that same plan to
`MariaBrain.execute_confirmed_plan()`, after refusing one that is no longer
exactly a well-formed model-originated plan. Brain checks it again, its shape,
origin, and step count, `recheck_model_plan` for every step against the
registry Tool Manager holds now, and every step's current permission, all
before any audit write, without asking any provider, running the planner,
retrieving context, reparsing the proposal, or rebuilding the plan. A plan
that fails any check is refused whole and nothing runs, and dispatch itself
refuses a plan that was not approved.

**The audit gate.** Before a step's tool runs, Brain has the memory layer
insert and commit exactly one audit summary for that step: the contract's
canonical identifier, the
decision `owner_approved`, and the target class the contract implies,
`named_filesystem_root` for an action that declares a path argument, whether
or not a path was supplied, and `none` otherwise. The memory layer adds
Maria's own UTC timestamp and validates it like every other field. When the
summary cannot be committed, for example
because the database stays locked, no row was inserted, or the returned id is
not a positive whole number, nothing is dispatched, the owner is told that the
approved action did not run because its audit summary could not be recorded,
and the approval stays spent. Otherwise the step is dispatched exactly once,
with exactly the immutable arguments the owner approved, and Brain appends one
outcome, `succeeded` or `failed` by the tool's own success flag; anything that
is not a real tool result counts as the shared fixed tool failure. The tool's
result is shown, followed by `Recorded as audit summary N.`, or, when the
outcome could not be recorded, by a notice that the summary exists but shows
its outcome as unknown. Nothing is retried, the tool is never rerun
automatically, and nothing about the plan is recorded as interaction history.

**Plans of several steps.** A plan of two to five steps runs each step in
order through that same gate: its own summary is committed immediately before
that step is dispatched, the step is dispatched at most once, and its outcome
is appended immediately after. The next step is reached only when this one
succeeded by its tool's own success flag and its outcome was recorded, so the
plan stops at the first step whose summary cannot be recorded or whose id is
not valid, which then does not run, at the first step whose tool fails, and
after the first step whose outcome cannot be recorded. There is no rollback,
compensation, retry, or resumption. The approval is spent whatever happens.
The owner is shown one report, a block per step in plan order, each opening
`Step i of N, tool.action:` and saying `completed.`, `failed.`, `not run.`,
`not run, because its audit summary could not be recorded.`, or
`interrupted, so its outcome is unknown.`, followed, for a step that reached
them, by its tool's own message and its audit notice. A plan that stopped,
for any reason, ends with one sentence such as `Maria stopped at step 2 and
did not run step 3. Nothing that ran was undone.`, naming why when an audit
write or an interruption stopped it, even when no step was left unrun; it says
`after` for a step that completed and `at` otherwise, and the last clause
appears only when some dispatch was entered. No label or closing sentence
repeats an argument value, and nothing in the report claims a failed or
interrupted step had no effect. Only a plan in which every step completed with
its outcome recorded has no closing sentence.

`KeyboardInterrupt`, and nothing broader, is handled during such a run, only so
the report can say what is known. It stops the plan and is reported against
the step it reached: a step not yet dispatched is `not run.`; one whose tool
was entered without returning is `interrupted, so its outcome is unknown.`;
one whose tool had returned keeps its known result. An interrupted audit write
may already have committed, so the report says that Maria could not confirm
whether the summary or the outcome was recorded, never that it is absent or
unknown. No outcome is ever written merely because a run was interrupted, and
nothing is retried or resumed. Every other exception propagates exactly as
before, a plan of one step lets an interruption propagate exactly as V1-18
did, and an abrupt end of the process can leave no report at all. All of this
concerns the CLI, where Ctrl+C reaches the thread running the plan. In the
desktop GUI the plan runs on the worker thread, which Ctrl+C never reaches, so
a GUI run is not interrupted; see "GUI Boundary".

**What a summary means.** A summary records that an approved execution attempt
was authorised for dispatch; by itself it does not prove the tool ran. A
summary without an outcome shows `Outcome not recorded; execution is unknown.`,
because Maria may have stopped after committing it, before or during the
tool's run, or before the outcome could be written. An outcome is inserted
only while its summary still exists, and at most once, and because ids are
never reused, no outcome can attach to a deleted summary or to a later one. A
plan of several steps has one summary for each step Maria reached for
dispatch, and none for a step after it stopped. No stored value links the
summaries of one plan, so the audit rows alone cannot say which summaries
belonged to one plan or how many steps it held.

**Audit commands.** `audit` lists the newest 20 summaries by id, reading one
more to say when more exist; `read audit <id>` shows one summary as a labelled
block; and `delete audit <id>` removes one summary together with its outcome
in one transaction. The commands are case-insensitive. `read audit` and
`delete audit` take the same positive whole-number ids as the eleven memory,
note, and interaction commands addressed by id alone, since V1-20 the seven
saved-conversation and summary commands, and since V1-22 the sixteen task and
reminder commands addressed by id; these thirty-six ID-only planner commands
share one parser, while `read note <id or exact title>` uses its separate,
unchanged lookup path.
`list audits` is still a filesystem listing. `audit.list` and `audit.read` are
allowed by default, `audit.delete` always requires confirmation, even for an id
that does not exist, and none of the three is model-eligible. In interaction
history, a successful listing or reading records only `Listed audit
summaries.` or `Displayed audit summary <id>.` as its response, and every
other audit reply is short fixed text recorded as shown, so no summary's
fields reach interaction history. There is no search, filtering, range, bulk
or wildcard deletion, editing, pruning, retention policy, or undo, and
summaries never become model context.

**Privacy.** Every value is checked against the vocabulary in `core/audit.py`
when it is written and again when it is read, so a summary can never hold the
request, the proposal, an argument, a fingerprint, private context, a root's
name, a host path, a tool's result, or exception text, and a stored value
outside the vocabulary fails the whole read with a fixed message that repeats
nothing. When the memory layer fails to write or read a summary, it raises a
fixed error with no cause or context and logs at most one fixed line, never
SQLite's text, a stored value, or a traceback. On this path Brain logs only
fixed lines, the declared tool and action, and summary ids, and, when a plan of
several steps stops, the step number, the step count, and a fixed stop code,
never a report, so the operational log may still show that a model-proposed
action ran after its summary is deleted, which the deletion's confirmation
notice says.

## Tasks and Reminders

V1-22 adds tasks and reminders as two independent record types (amendment
V1-22-R1, decisions D1 to D9 with D1a and D7a), with twenty-six commands, all
usable with no model present, and two request forms of the model path. V1-22
itself delivers, notifies, schedules, polls, repeats, and generates nothing:
due-now and overdue are queries the owner types. Showing due reminders
unasked is V1-23-R1's narrow subsystem; see "Reminder Delivery".

**The time zone (D1, D1a).** Dates and times are resolved in one IANA zone,
the private override's `[time] zone`, which `core/config.py` checks by syntax
alone and the tracked configuration may not declare. `core/time_resolution.py`
loads it through standard-library `zoneinfo`, from time zone files on
zoneinfo's search path or, when none provides the key, from the optional
pinned data-only `tzdata` in `requirements-time.txt`, which zoneinfo imports
itself and Maria never imports or installs. No other source is used: never
UTC, a fixed offset, or the operating system's zone. A missing zone, or one
whose data cannot be loaded, refuses `due tasks` and every new date or time
with a fixed message and stores nothing, while every other operation on
existing records, adding an undated task included, works without it. Each
reminder keeps the zone it was resolved in, and a later configuration change
reinterprets nothing stored. There is no per-request zone; a request naming
another zone or an abbreviation is unclear.

**The reference instant (D2).** Brain reads its one clock seam once per
request, after the empty-request check and before planning, and builds one
`TimingContext` of that aware UTC instant and the configured key. Every timing
decision of the request uses it: the planner's commands, every creation step
of a model proposal, however late the model replies, and `due tasks` and
`due reminders`. Recheck and confirmation read no clock, and a plan is never
resolved again. `today`, `tomorrow`, a weekday (the first such date strictly
after today), `YYYY-MM-DD`, and `in N days` are calendar dates in the zone, and
a reminder's wall time is applied on that date; `in N minutes` and
`in N hours` are elapsed time, counted on UTC instants from the reference
truncated to the minute, and shown with the true offset where they land. N
is ASCII digits with no leading zero: days 1 to 366, minutes 1 to 1,440, hours
1 to 168. New timing is refused when a reminder's instant is not after the
reference, when a task's date is before local today, or when the local date is
beyond the inclusive horizon, the same month and day ten years after local
today, February 29 mapping to February 28. Existing overdue records stay
fully usable, and an approval stores exactly the approved values even when
they have become due.

**Daylight saving (D3).** A local time a change skips is refused, never
shifted. One it repeats is refused unless the owner gives an offset, and only
an offset that equals one of the local time's valid offsets is accepted; any
other offset, on any local time, is refused. So an explicit offset is
accepted only when it matches the configured zone at the given local time:
an ordinary local time has one valid offset, which changes nothing, and for a
repeated local time the offset selects the occurrence (D1 as the owner
clarified it on review). An offset never names another zone. Valid offsets
are found per PEP
495, keeping each fold's offset only when converting the instant it implies
back gives the same wall time.

**Stored timing.** A task's `due_date` is a calendar date with no time or
zone. A reminder stores `remind_time`, the canonical RFC 9557 local time,
offset, and key exactly as approved, and `remind_utc`, which must equal that
local date and time minus the offset, on every write and every read; that
takes offset arithmetic and no zone data, and a row that breaks it, or any
other rule, is corrupt and refused privately on read, a listing holding it
refusing as a whole. A later rule change is not corruption: the saved values
agree with the zone's current rules when the saved offset is one at which the
saved local time exists now, so a correctly chosen repeated time agrees.
Otherwise the original values are shown with a fixed warning, the saved
instant keeps ordering and due selection, and nothing is rewritten or
refused; a zone that cannot be loaded adds a fixed note instead. Only the
saved zone is consulted, never the configured one.

**Records and lifecycle (D4, D5, D7, D7a).** The two record types have no
link to each other or to anything else and no recurrence; a reminder's plan
carries `repeat: "never"`. Creating, editing, rescheduling, deleting, and both
consent changes are mandatory confirmations whose previews show the exact
text and resolved timing, or the saved values beside the new ones, before
anything is stored; listing, due queries, reading, completing, and reopening
are allowed. Completing stamps the time of that completion, reopening clears
it, and nothing restores an earlier completion time; neither changes the
text, the timing, `updated_at`, or the consent.

**Natural-language requests (D8, D9).** A model proposal may create a task or
reminder only from a request that is, whole, one to five items in four closed
forms: `remind me <timing> to <body>`, `remind me to <body> <timing>`,
`add a task to <body>`, and `add a task to <body> by <date>`, joined only by
` and ` or `, and ` before a form prefix. `core/task_requests.py` prepares
the request (any control, format, surrogate, or separator character makes it
unsupported, one trailing `.` is removed, and a request then ending in `?` is
a question), matches keywords, timing tokens, and joiners ASCII
case-insensitively with exactly one space between them, keeps each body
verbatim apart from its surrounding whitespace, and refuses a body holding a
guarded token: `?`, `!`, `;`, a double quotation mark, a sentence break, a
form prefix, a timing word or token, or a retraction, hedge, or example
phrase, the closed lists being those of the amendment. Text that does not
begin with a form prefix is not a request; a request that begins as one but
does not complete it is unclear; two items alike are unclear. The compiler
then binds the proposal to those items and builds every step from Maria's own
extraction, as "Model-to-Plan Compilation Gate" describes, and a casual
mention never produces an approval.

## Reminder Delivery

V1-23 (amendment V1-23-R1, decisions D1 to D13 with a verification-timing
amendment) lets Maria show due reminders unasked, through one claim protocol
shared by three surfaces: the closed-app pass, the command line, and the
desktop window. Delivery is off until the owner turns it on, and no task is
ever delivered.

**The subsystem.** It is four modules. `deliver_reminders.py` is the
closed-app entry point and composition point; it is not an interface, accepts
no command, builds no Brain, and imports neither `main.py`, the GUI,
`core/config.py`, reasoning, nor the adapter. `core/runtime_paths.py` is the
leaf that reads the tracked `config.toml`'s `[paths]` with the one path rule
`core/config.py` also uses, and nothing else, never the private override.
`core/reminder_delivery.py` holds `claim_notice` and `record_notice`, the
`ReminderNotice` value, the notice text, and the session's seam; it runs no
SQL, calls no Windows function, starts no thread, and reads no clock of its
own: `claim_notice` receives the store, the instant, and the channel, and the
seam calls the clock it is given once per check. `core/desktop_notice.py`
is the only module that calls a Windows notice function, through
standard-library `ctypes`: `SHQueryUserNotificationState` and `MessageBoxW`,
the latter with exactly `MB_OK`, `MB_ICONINFORMATION`, and `MB_TOPMOST`, and
it names no foreground or clipboard function. These are operating-system
notification primitives, not a GUI toolkit. The subsystem's callers are
`core/session.py`, `main.py`, and `gui/session_worker.py`.

**One claim protocol.** A check reads the setting first, and with delivery
not on reads nothing else. Otherwise it reads at most six due reminders with no
claim for their current scheduling revision, oldest first, renders the first
five notice entries before any claim, and claims those five one at a time,
each in its own write transaction that re-reads the setting and the row under
the write lock and commits before anything is displayed. The read and every
claim use the same gates, a positive id among them, and the read applies
them before its limit, so a row Maria could not have written never takes a
place among the six. A reminder that no longer qualifies is skipped, as a due
reminder left unclaimed. A claim that finds delivery off, or that raises,
stops the batch; every claim whose call returned is retained and displayed,
and the notice ends with the more-due line whenever a due reminder was left
unclaimed: a sixth candidate, a skipped one, or any the batch stopped before
claiming. A claim whose commit succeeded but whose call raised, the retained
claims when packaging them into one notice fails, and every claim of a process
that dies before its display stay unconfirmed and undisplayed, and none is
retried. Each outcome is written for its own claim only, identified by the
reminder id and revision, never by a timestamp, and an outcome whose claim no
longer exists is dropped. The memory layer's methods are described in
`docs/database.md`, under "Reminder Delivery State".

**The guarantee.** A scheduling revision begins when a reminder is created,
and a new one begins exactly when an approved reschedule stores a different
saved instant, which also clears the claim; for each reminder and revision at
most one claim is ever committed, so no crash, restart, upgrade, or second
process shows a reminder twice in one revision. A reschedule to the same
instant, an edit, completion, reopening, consent changes, and turning delivery
on or off keep both. A fresh request to move a reminder to a time that has
passed is refused at planning, as in V1-22, so only a late approval can restore
an earlier time, and a reminder already shown at that time may then be shown
once more. A claim judges the row as it is when the claim commits: a change
made and undone before then is not detected. The per-reminder claim is the
single-instance guard: Task Scheduler's IgnoreNew only keeps the task's own
scheduled runs from overlapping.

**The closed-app pass.** The owner registers one per-user Task Scheduler task,
`MariaReminderDelivery`, from the template `docs/reminder-delivery-task.xml`,
which is documentation: placeholders only, no absolute path, never loaded by
Maria. While the owner is signed in, it starts pythonw on
`deliver_reminders.py` once a minute, with the interactive token and least
privilege, never waking the computer, never overlapping its own scheduled
runs, and with no execution time limit. Maria never registers, changes,
runs, or deletes the task, never calls schtasks, and starts no process. One
pass reads the clock once; reads the tracked `[paths]`, or exits 1; exits 2
when the Windows notice functions are unavailable; asks Windows for the
notification state once, claiming only on `S_OK` with
`QUNS_ACCEPTS_NOTIFICATIONS`, deferring every other known state with status 0
and a failed query, a raised call, or an unknown value with status 2; opens
the existing database read-write without creating it and runs the interfaces'
read-only structure check, exiting 3 unless it is exactly the current schema;
exits 0 when delivery is not on; configures its own log, which opens no file
until a line is written, or exits 5 having claimed nothing; claims; shows one
message box for exactly the retained claims; records each outcome, displayed
when the box returned a button and not displayed when it returned 0 or raised;
and exits with a fixed status, 5 outranking 4, which outranks 3, which outranks
0. Before its first claim it writes nothing except, at most, the log
directory. Its entry boundary catches `Exception` only, logging the type; a
`KeyboardInterrupt` or `SystemExit` keeps Python's default handling.

**In session.** The command line checks before every prompt and the window's
worker after it creates the session and after each reply, as "CLI Boundary"
and "GUI Boundary" describe, both through the session's seam, which
"Session Boundary" describes. In-session delivery does not consult the
notification state.

**Logging and live files.** The pass writes its own bounded `delivery.log`
beside `maria.log`, with the same size and backup bounds, because Python does
not support several processes logging to one file; it is configured before the
first claim and opened only when the first line is written, so an idle,
disabled, or deferred pass writes nothing. Lines name ids, channels, outcomes,
counts, and fixed phrases only, and reminder text is displayed, never logged.
`delivery.log` is best effort: passes started by hand or by another task can
write it at the same time as a scheduled one, so lines can interleave or be
lost, and a refused rotation can lose older backups; no claim, display,
outcome, or exit status depends on it, and Maria never reads it. It and its
backups are live runtime files.

**What is not promised.** Delivery is not promised while the computer is
powered off, asleep, or hibernated, or while the owner is signed out, and the
computer is never woken. A reminder due then is shown by a later pass once the
owner is signed in and the computer is awake: the first that Windows does not
defer, that no box still open holds back, and whose notice has room among its
five, oldest first. It is labelled overdue when shown more than fifteen
minutes after its saved time, and due otherwise. Presence is checked before a
box is shown, not during it. A box waits until someone closes it, and while it
waits no later pass of the task starts, so an unanswered box delays later
desktop notices without limit; in-session delivery is unaffected. Displayed
never means read.

## Projects and Project Files

V1-24 (amendment V1-24-R1, decisions D1 to D17 with five owner corrections)
adds named projects, owner-approved project facts as Tier A context, and
bounded, read-only reading of project files, Maria's own source included when
the owner activates Project Maria. Nothing in it writes, renames, or moves a
file, starts a process, or works on its own.

**Modules.** `core/project_records.py` is the vocabulary: limits, the path
grammar, the two roots and project locations, the activation states and serial
rules, the value objects, the fact approval digest, every fixed project text,
and the rendering of the file block; it imports only the standard library and
`core/task_records.py`, and holds no SQL, I/O, or logging.
`core/project_files.py` is the reader: the lexical phase, the filesystem
phase, the `@` reference grammar, decoding, the display, and model file
parts. `core/project_file_access.py` is the only module that calls the nine
Windows functions reading uses, through standard-library `ctypes` with every
argument and result type pinned, and the descriptor steps through `msvcrt`.
`core/project_maria_manifest.py` is data only. `tools/projects.py` holds the
thirteen commands and is the only module that calls the memory layer's
`activate_project` and `deactivate_project`, the only methods whose SQL writes
the setting. The memory layer owns the three tables of schema 9 and every
read and write of them; see `docs/database.md`, under "Projects". Brain reads
each model request's project snapshot, settles its `@` references, and hands
the conversation module the final check; `core/context.py`,
`core/conversation.py`, `core/reasoning.py`, and the adapter carry facts and
file parts as described below. None of the thirteen actions is model-eligible.

**Activation.** The setting is at most one row of `project_activation`. No row
is never activated; a well-formed row naming an existing project, or none, is
active or inactive; anything else is unreadable, which `projects` reports,
which is never taken for never activated, and which only the owner's two
commands replace. Every change raises a serial by one, so a token, the project
id and the serial, names exactly one state Maria wrote; at the largest serial,
SQLite's largest integer, an activation is refused and a deactivation clears
the project and keeps the serial, so no earlier token can recur. Only
`activate project`, which needs approval, and `deactivate project`, which
needs none because it only narrows exposure, change it; a fresh database, a
migration, either interface starting, the closed-app pass, and model plans
leave it as it was.

**One snapshot per request.** A model request first reads, in one read
transaction, the setting, the active project's row, its eligible facts, and
the recheck of facts supplied earlier in the conversation. Every fact, file
part, and notice of the request comes from that snapshot; file reads run after
it and are attributed to its project, and nothing reads the setting again to
choose a project. Deterministic project commands take a snapshot of their own
and need no final check, because nothing they show reaches a model.

**The final check.** A request carrying any project fact or file part is
handed to the reasoning boundary only after one more read of the setting, in
its own read transaction, after every size check Maria makes before the
hand-off. It is sent only if the setting is readable and its token equals the
snapshot's; otherwise nothing is sent and the owner is told that the active
project changed while Maria prepared the request. A switch or deactivation
committed before that read stops the request, including a switch away and back
within one second, which the serial tells apart; one committed after it does
not, and the request goes out with its snapshot's facts and parts, its notices
naming the snapshot's project, while the change applies from the next request.
After the check nothing is read again, rendered again, or substituted. The
check covers the setting only: a fact edited or deleted after the snapshot
does not stop the request. The serial orders Maria's own writes; a write from
outside Maria can set any value, including a token a request already holds,
and the check cannot tell such a write apart.

**The lexical phase.** Every project file operation starts with checks that
make no filesystem call: the path grammar; the candidate built from the
root's canonical path as configuration settled it, the project folder, and the
typed components, with `FilesystemProtections.excludes` applied to every
prefix from the root to the candidate; and admission by name. For Project
Maria the typed path must equal a member of the approved list, ignoring ASCII
letter case, and the member's spelling is used from then on; for a workspace
project no component may begin with "." and the final one must have an
admitted type.

**The filesystem phase.** An operation is one folder check for
`create project`, one listing, or one file read, run on the calling thread,
the command line's main thread or the window's one worker, from start to end
with no other Maria work in between; no handle outlives it, and none spans a
database transaction or a provider call. The thread's placeholder mode is set
once to expose placeholders and restored exactly once, also after a failure;
a restore that does not report the exposed mode withholds the result, is
reported, and trips a process-wide latch that refuses every later project file
operation until Maria restarts. The root is opened by its canonical path;
each component after it is first looked up by enumeration and refused there
when a reparse point, offline, recall-on-open, or recall-on-data-access
attribute shows, so a link or placeholder is refused before it is opened.
Each, the root included, is opened by handle with sharing that never allows
delete, held, and checked on that handle: its type, its reparse tag,
the offline and recall-on-data-access attributes, a pending deletion, and its
final path, which must be exactly the expected path, so a short-name alias or
a component replaced by a link is refused. A file is opened with read sharing
only, so an existing writer makes the open fail and no writer can join; its
link count, size, and identity are checked on that handle before any data is
read, so a hard link is refused before reading, and it is then read whole,
at most 524,288 bytes, through a binary, non-inheritable descriptor that takes
over the handle in a three-state transfer; afterwards its identity, size,
last-write time, and every held final path are checked again. Every handle,
search, and descriptor belongs to the operation and is released exactly once,
newest first, by its own function. A listing is built from find data alone,
and its normal ends, an empty folder and the end of the directory, are not
failures. On another system, or without any of the functions, every operation
is refused before any filesystem call.

**Stated residuals.** The folders above a root are not held, and a change there
is caught only by the root's final-path check and the checks after the read.
Final paths are compared one handle at a time, not atomically. An entry that
becomes a placeholder or a link between its lookup and its open is opened and
then refused on the handle before any data is read; whether that open alone
starts a download is not known, so Maria does not promise that a read never
starts one, and project folders under cloud synchronization are not supported,
a rule she states but cannot detect. An interruption can leave one handle or
descriptor open until the process ends, and a failed release keeps it until
then too; "no handle remains" holds after every operation whose cleanup
succeeded. Privileged code can bypass sharing, FAT always reports one link,
the file index is not guaranteed unique on ReFS, and a drive without a
drive-letter final path cannot be read.

**Decoding and display.** Decoding uses only the bytes the filesystem phase
released: one leading UTF-8 byte-order mark is removed, a NUL byte, invalid
UTF-8, and an empty file are refused, and lines end at line feeds. A display
shows at most 131,072 bytes of decoded text under a header naming the project,
path, lines, line count, size, and the first 16 digits of the SHA-256 of
exactly the bytes read, with a carriage return before a line feed shown as a
line feed and every other control, format, surrogate, or separator character
but tab shown as an escape, so no file text can pose as Maria's own. Refusals
are fixed texts that never tell an absent file from a protected one.

**Model use of files.** While a project is active, each whitespace-separated
word of a model request beginning with exactly one `@` is a reference,
`@<path>` or `@<path>:<from>-<to>`; `@@` and a lone `@` are text, and while no
project is active every `@` is text. A malformed reference, a fourth distinct
one, a part over 16,384 bytes, an unreadable file, and an unreadable or failed
setting each refuse the whole request with a fixed reason before anything is
sent. Each distinct file is read once and its parts share its SHA-256. The
request then carries the owner's text as typed, Tier A and Tier B context as
any request does, no proposal instructions, no earlier exchange, and the file
block as its own message just before the prompt, with fixed framing that says
the parts are reference data, that the model cannot propose actions in this
reply, and that it should say plainly when it would recommend a different
approach. Its sizes are measured in UTF-8 bytes: P, the request; C, the
rendered context; F, the file framing, plus 2 for its separator when C is not
0; and D, the file block. P + C must fit within min(8,192, the configured
input budget), F + D within 32,768, and P + C + F + D within the configured
input budget, and the reasoning boundary and the adapter measure the same
number. The reply is plain text: it never reaches the compiler, it is shown
with Maria's notices, which name each part by project, path, lines, and the
first 16 digits of its SHA-256, and nothing of it, its parts, or the request
is kept in the conversation or recorded. Text inside a project file is data
and cannot create a plan step, grant authority, or change a permission.

**Project Maria.** Admission is membership of the approved list, a sorted
tuple of unique ASCII paths that the code holds as data; Maria never runs Git,
reads `.git`, or checks whether a path is tracked, so a file not on the list
is never read or listed, tracked or not, and the run-time exclusions still
apply to members. A listing shows only members and the folders that hold
them. The list changes only in the pull request that adds, renames, or
removes a file, and three tests keep it true in an isolated copy. The reader
uses the `project_maria` root's settled path whether or not the root is
enabled for the generic commands, whose repository barrier keeps refusing
every repository path, so rename and move stay refused there, and write and
delete stay denied everywhere.

**No execution.** A repository-wide guard proves, statically over every
production module and in a run of every project command with every process and
code-execution function made to fail, that no production path starts a
process, runs a shell command, or evaluates, compiles, or executes code;
registered-tool dispatch through ToolManager's fixed module list is the one
dynamic import.

## File Organization

V1-25-R1 adds one batch to the filesystem tool and keeps everything else of it as it was.
`organize folder <folder>` is a deterministic planner command that yields one
`filesystem.organize` step carrying only the folder. The action is declared not
model-eligible, so a reasoning model can never propose it, and neither `rename` nor
`move`; a model proposal naming any of the three is refused by the compiler's closed
allowlist before permissions are asked.

Three modules and one tool method share the work, each on one side of a line. The tool
(`tools/filesystem.py`) settles the folder through the existing path gates, builds the
protection predicate from the existing protections, and is the only one that knows
configuration. `core/file_organization.py` holds the decisions: the offer, the check of
every pin, the held chain, one operation's checks, the rename and its verification and
classification, the stop and release rules, the counts, and every fixed text; it calls
no Windows function and imports no configuration. `core/organize_access.py` is the one
additional `ctypes` module of this stage, calling exactly seven Windows functions with
every argument and result type pinned, and holds no policy. `core/project_file_access.py`
and `core/desktop_notice.py` keep their own functions, and no two of the three import each
other.

When the owner is about to decide, `ToolManager.prepare_confirmation` asks the tool for its
`confirmation_binding`, which reads the folder once and adds the pinned operations to the
step, so the approved plan, and the fingerprint the approval binds, name exactly the files
that will move. A failed binding adds nothing, and a step with no pinned operation runs
nothing. Nothing is held while the owner decides. After approval the session confirms the
very plan it offered, and the tool runs only the pinned operations: it enumerates nothing.

Execution holds one chain of directory handles from the root to the folder, every handle
opened relative to its held parent without delete sharing, and requires the root's file
system to be exactly NTFS. Each operation opens its category folder, creating it only when
that was pinned, opens the source, checks it and its digest, looks the destination up, and
renames the held source into the held category folder with replacement off. Every handle
belongs to a handle set that releases it exactly once, newest first, attempts every handle
even when a release fails or is interrupted, and re-raises an interruption only afterwards.
A handle that the checks before the first move cannot release is counted on the batch, even
when a refusal is already known, and stops the run before any creation or rename.
An unexpected `STATUS_PENDING` keeps the request's memory for the life of the process and
stops organization until Maria restarts.

Each operation ends as moved, reported but unconfirmed, not moved after an attempt, outcome
unknown, refused before an attempt, or not attempted, and the six counts add up to the
planned count. The run stops at the first operation that is not a clean move. Only a native
failure reported by the rename call is classified as not moved or unknown; a failure nobody
expected is outcome unknown once the rename was called and refused before it, and a folder
whose creation call failed unexpectedly is unknown, with only the type name logged. Nothing is
copied, overwritten, deleted, retried, or rolled back; there is no transaction, so "nothing
at all" means only that nothing changes when the owner does not approve, a new request
replaces the pending one, or the checks before the first move refuse.

The request is recorded as typed (as every command is), the response as counts only
through `ToolExecutionResult.recorded_message`, which Brain applies generically, and the
log holds fixed text and counts. The report, which names the files, and the preview are
displayed and never recorded or logged. An interrupted run logs one count line, releases
every handle, and shows no report. Brain, the session, Tool Manager, the compiler, the
memory layer, and the interfaces are unchanged.

What this does not promise is stated, not enforced: the offer's listing is by path; the
final-path check compares spellings; the digest is checked metadata, not a content hash; a
second name that appears at any moment except the one read after the move is not seen; a
program that makes the destination appear as another name for the same file just before a
move lets the move succeed; and cloud folders, case-sensitive folders, network shares,
other file systems, privileged callers, and Windows builds other than 10.0.26300 are
unsupported. Legacy `rename` and `move` are Windows-only, call `os.rename` only, need an
existing destination parent, never move a folder into itself, log fixed text, and stay
path-based check-then-act without any of this protection.

## Boundaries

The brain does not run SQL or perform tool work directly. The planner does not
execute tools. Tools do not decide global permissions. The memory module is the
only module that opens SQLite connections or executes SQL. `core/filesystem_roots.py`
is the only canonical model of where a filesystem path may live, and the only
owner of the universal permanent exclusions; `core/config.py` decides which roots
production accepts, and `tools/filesystem.py` adds the barrier that keeps the
generic filesystem commands out of Maria's own repository. The action-contract
registry declares argument shapes only: it holds no tool logic, never calls a
tool to decide whether arguments are valid, and decides nothing about
permission. `core/context.py` imports no Maria module at all.
`core/reasoning.py` reaches nothing but the context module, the shared
failures, the logger, and, since V1-24, `core/project_records.py` and the leaf
`core/task_records.py` it imports, and `core/ollama_adapter.py` nothing but
those and the reasoning interface. `core/conversation.py` reaches only those,
the logger, the planner's plan types, and two leaf declarations that import
nothing from Maria, the action contracts and `core/conversation_records.py`,
never Tool Manager, permissions, the compiler, or the memory layer. A
summary draft reaches storage only as the declared argument of the planner's
`summarize conversation` step, after the owner approved exactly its text. A
conversational reply reaches Brain only as
the text of a zero-step transient plan, and a proposal attempt only as the
untouched `ReasoningResponse`, which Brain passes to the compiler and nowhere
else; no module passes a reply to the planner or a tool, and a compiled plan's
arguments reach a tool only after the owner's approval and a committed audit
summary.
`core/plan_compiler.py` reaches only the action contracts, the planner's plan
types, the reasoning response type, the logger, and, since V1-22, the two
leaves `core/task_requests.py` and `core/time_resolution.py`, and, since
V1-24, through the planner, the leaves `core/project_records.py` and
`core/task_records.py`, never Tool Manager, a tool, permissions, the memory
layer, or conversation, and Brain is the only production module that imports
it. `core/time_resolution.py`
imports only the standard library, and is the only production module that
imports `zoneinfo`; it never imports `tzdata`, which zoneinfo loads itself
when installed. `core/task_requests.py` imports only it, and
`core/task_records.py` only the standard library; none of the three holds
SQL, logs, or reaches a tool, and only `core/task_records.py` reads a clock,
for the memory layer's write stamps. Brain reads its one reference instant
through `_reference_instant()`, its only clock read, once per request. `core/audit.py` imports nothing from Maria
but the action contracts; it holds no SQL, reads and stores nothing, and logs
nothing, and the memory layer, Brain, and `tools/audit.py` take every audit
value from it.
`core/ollama_adapter.py` is the only production module that imports a network
module, and `main.py` is the only one that names or reaches it. An
import-boundary guard asserts that Brain, the planner, permissions, Tool
Manager, the memory layer, configuration, and every tool import no network or
threading module, directly or through any Maria module they import, and that
none of them reaches the adapter.
The desktop GUI package imports from Maria only `core/session.py`,
`core/constants.py`, `core/logger.py`, and its own modules, never the memory
layer, Tool Manager, permissions, the planner, Brain, the compiler, or a tool,
and it executes no SQL and builds no plan. `gui/window.py` is the only
production module that imports PyQt6, and only its core, GUI, and widget
modules; `gui/session_worker.py` is the only one besides the adapter that
imports anything from the guarded network and threading set, and imports
`threading` alone, under the owner's amendment V1-21-R1. `main.py` imports the
GUI only inside `run_gui()`, so the CLI never loads Qt.
The reminder-delivery subsystem of V1-23-R1 reaches, at any depth, only
`core/runtime_paths.py`, `core/reminder_delivery.py`, `core/desktop_notice.py`,
the memory layer and the leaves it imports, the constants, and the logger:
never configuration, reasoning or the adapter, permissions, the planner, the
compiler, the task-request parser, Tool Manager, a tool, the conversation, the
session, a GUI module, or `main.py`, and no Maria module it reaches imports a
network, threading, process, or asynchronous module. Static guards compute that
reach over Maria's own modules only, treating the standard library as leaves;
`logging.handlers`, which every Maria process loads, imports socket and
threading itself, so a behavioral guard runs full cycles with socket creation,
network connections, thread starts, and new processes patched to fail and
proves that the delivery code uses none of them. A SQL trace proves that the
delivery code reads only catalogue metadata, the setting, and the five
reminder fields a notice shows and a claim compares, and writes only claim
columns, and a file-access guard that a pass opens only the tracked
configuration, its database, and its log, and it never reaches the project
file reader, its Windows functions, the approved list, or the projects tool.
`core/desktop_notice.py`, `core/project_file_access.py` (V1-24-R1), and the
system tool are the only production modules that import `ctypes`. The
subsystem is the one exception to the Planner, PermissionManager, Tool Manager
pipeline, out of session and, for its claim and outcome writes, in session: it
is limited to its fixed reminder operations through the memory layer's narrow
delivery methods and never uses Tool Manager or model planning.

The brain has no memory-specific dispatch branch. `MemoryTool` contains no SQL;
it receives the shared `Memory` object and calls its public persistence methods.
The existing calculator, notes, filesystem, and system tool implementations are
unchanged by explicit memory support. Brain's model-plan path likewise
branches on a plan's origin, never on a tool or action name, and writes audit
summaries only through the memory layer's public methods. `AuditTool`
contains no SQL either: it reaches summaries only through the shared `Memory`
object, and so do `ConversationsTool`, `ConversationSummariesTool`,
`TasksTool`, and `RemindersTool`. Brain supplies session inputs generically,
from contract declarations, never by a tool or action name, and it names no
task or reminder tool, action, or argument: timing reaches the planner and
the compiler as one opaque context, and task and reminder context through the
memory layer's and `core/context.py`'s generic reads. Neither tool reads a
clock, and nothing in V1-22 starts a thread, a timer, or any background or
scheduled work. Under V1-23-R1, nothing in Maria starts a timer or background
work either: the only scheduled work is the owner-installed task's one-minute
pass, which Task Scheduler starts and which ends after one pass, and the
in-session checks run only at the moments "CLI Boundary" and "GUI Boundary"
name. Under V1-24-R1 Brain likewise names no project tool or action: it reads
the project snapshot and the final check through the memory layer's public
methods, and file parts through the project file reader, and `ProjectsTool`
contains no SQL.

## Scope

This foundation intentionally excludes autonomous agents, voice, browser
automation, internet search, and user interfaces beyond the command line and
the optional desktop GUI of Stage V1-21; the closed-app reminder message box
of V1-23-R1 is the one owner-facing surface beyond them, and it accepts no
command. File organization (V1-25-R1) moves at most five owner-approved files of one
folder of the organize root into five fixed category folders, on 64-bit Windows over
NTFS only, and never deletes, copies, overwrites, writes, retries, or rolls back.
Project assistance (V1-24-R1) is read-only: it writes, renames, and
moves no project file, searches or indexes nothing, offers no automatic file
context, keeps no file part in a conversation, sends no project name or
location to a model, moves no project, adds no third root, runs no Git or any
other process, supports no folder under cloud synchronization, holds no
activation setting across a send, and reads no project file off Windows.
Future
versions can add richer capabilities by extending the planner, tool manager, and
tool set without rewriting the foundation.

Explicit memory remains intentionally small and strict. Maria does not infer
facts from conversation, learn in the background, or provide embeddings,
semantic search, fuzzy matching, or ranking. A memory's fact can be replaced
and a memory can be forgotten by numeric ID only, through `update memory <id>:
<new fact>` and `forget memory <id>`; there is no selection by fact text, no
bulk or clear-all deletion, and no undo or revision history.
