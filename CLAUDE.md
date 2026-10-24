# Maria — Development Contract for Claude

You are acting as a Senior Python Software Engineer and Software Architect on **Project AI:
Maria**, a long-term, modular personal AI assistant. Maria is a personal, single-owner
application: it serves exactly one owner, and multi-user support is out of scope. It is a
real software product, not a prototype. Read the existing code before writing new code.

`v0.1.0` is the immutable Foundation release baseline and `v0.2.0` is the current public
release — do not assume either matches the working tree. Verify the actual repository
state first: the active branch (`git branch --show-current`), the `## Unreleased` section
of `Maria/CHANGELOG.md`, and `git status`.

This file is the development contract. `Maria/docs/roadmap.md` defines the Maria v1.0
stages and their owner-approved amendments; where a stage amendment and this file
disagree, stop and ask rather than choosing one.

## Philosophy (in priority order)

Mirrors `Maria/PRINCIPLES.md`: **Simplicity > Maintainability > Readability > Modularity >
Reliability > Security.**

Never trade architecture quality for more features. If a request would add a feature at the
cost of clean architecture, say so before implementing it.

## Repository Layout

**Every path in this document is relative to the repository root.** The tracked project
content at the root is `README.md`, `CLAUDE.md`, and the `Maria/` application directory;
other entries (tooling, configuration, or future additions) may also be present, so check
rather than assume. **All application code lives in `Maria/`.**

| Path | Contents |
|---|---|
| `Maria/main.py` | entry point: selects the CLI, or the desktop GUI with `--gui`, and composes the runtime |
| `Maria/deliver_reminders.py` | the closed-app reminder-delivery entry point (V1-23-R1): one pass, then exit; not an interface, and builds no Brain |
| `Maria/core/` | `brain`, `planner`, `memory`, `permissions`, `tool_manager`, `logger`, `config`, `constants`, `action_contracts`, `audit`, `context`, `conversation`, `conversation_records`, `desktop_notice`, `failures`, `filesystem_roots`, `ollama_adapter`, `plan_compiler`, `project_file_access`, `project_files`, `project_maria_manifest`, `project_records`, `reasoning`, `reminder_delivery`, `runtime_paths`, `session`, `task_records`, `task_requests`, `time_resolution` |
| `Maria/tools/` | `audit`, `calculator`, `conversation_summaries`, `conversations`, `filesystem`, `history`, `memory`, `notes`, `projects`, `reminders`, `system`, `tasks` |
| `Maria/gui/` | the optional desktop GUI: `controller` (Qt-free), `session_worker` (the one worker thread), `window` (the only Qt module) |
| `Maria/tests/` | standard-library `unittest` suite |
| `Maria/docs/` | `architecture`, `api`, `database`, `planner`, `tools`, `coding_style`, `roadmap`, `vision`, and the `reminder-delivery-task.xml` task template (documentation, never loaded by Maria) |
| `Maria/config/config.toml` | TOML configuration |
| `Maria/prompts/` | starter prompt text |
| `Maria/requirements.txt` | the core and CLI requirements: standard library only |
| `Maria/requirements-gui.txt` | the optional GUI requirements: PyQt6 and its runtime dependencies, pinned |
| `Maria/requirements-time.txt` | the optional time zone data: the data-only `tzdata`, pinned with its hash |
| `Maria/README.md` | the authoritative project README |
| `Maria/PRINCIPLES.md`, `Maria/CHANGELOG.md` | principles and changelog |
| `Maria/database/`, `Maria/logs/` | git-ignored runtime artifacts |

Python imports are rooted at `Maria/` (for example `from core.logger import get_logger`),
so **any Maria runtime or test command that depends on those top-level imports must run with
`Maria/` as the working directory.**

`Maria/README.md` is the authoritative project README — cite that one. The
repository-root `README.md` is at most a short signpost to it; verify its current contents
before relying on it for anything.

**Live runtime files.** `Maria/database/maria.db` and `Maria/logs/maria.log` are the
application's real persistence and log, `Maria/logs/delivery.log` and its rotated backups
are the closed-app reminder pass's own log (V1-23-R1), and
`Maria/config/config.local.toml`, when present, is the owner's private, untracked
configuration override. Normal application access
through the designated persistence, logging, and configuration modules is expected and
correct. However, while developing: do not manually open, inspect, print, query, hash,
copy, or modify any of those live files, and make automated tests use temporary resources
instead.

## Interfaces: CLI and Desktop GUI

Maria has two interfaces over one application core. `Maria/main.py` selects one and
launches it through the existing runtime composition (`build_runtime_brain()` and the
startup handling both interfaces share): the CLI by default, and the desktop GUI only for an
explicit `--gui`. Without `--gui`, CLI startup and behavior are unchanged, and any other
argument is ignored as before.

- **CLI** — the first-class fallback, recovery, debugging, and power-user interface.
- **Desktop GUI** — PyQt6 with Qt Widgets (Stage V1-21, amendment V1-21-R1): the intended
  daily interface, and optional.

Under V1-23-R1 there is one owner-facing surface beyond the two interfaces: the closed-app
reminder message box, which the separate entry point `Maria/deliver_reminders.py` shows
while both are closed. It is not an interface and accepts no command. Both interfaces also
show due-reminder notices in session, through the session's delivery seam (see "The
reminder-delivery contract" below).

The GUI contract:

- **Optional dependency.** PyQt6 and its required runtime dependencies (PyQt6-Qt6 and
  PyQt6-sip) are the one approved exception to the standard-library rule. They are pinned
  in `Maria/requirements-gui.txt` and serve the GUI alone. Core functionality and the CLI
  must import, start, and work without Qt. If PyQt6 cannot be loaded, Qt cannot start, the
  window fails, or its session ends unexpectedly or cannot end cleanly, `--gui` prints a
  fixed message and exits with status 1, and CLI startup is unaffected. Qt's application
  object is created before Brain, so nothing has been configured or opened when Qt cannot
  start. On Windows, a process without a console first sees Qt's own error dialog for a
  platform that cannot start.
- **Qt stays in the GUI package.** Production Qt imports live only in `Maria/gui/`, and
  within it only in `gui/window.py`. `gui/controller.py` and `gui/session_worker.py` never
  import Qt, so the controller is tested without a window.
- **Presentation only.** The GUI is presentation over the existing `MariaSession` API. It
  never imports the memory layer, Tool Manager, permissions, a tool, the planner, Brain, or
  the plan compiler; never executes SQL; never constructs, mutates, or reinterprets a plan;
  and never reimplements approval policy. It reaches application behavior only through the
  session's `submit()`, `pending_approval`, `confirm()`, `cancel()`, and `end()`, and,
  under V1-23-R1, the session's delivery seam, `delivers_reminders`,
  `claim_due_reminders()`, and `record_reminder_notice()`, and, under V1-24-R1,
  `project_choices()` for the project selector: nine members in all. The GUI changes
  neither Brain nor the Session API; V1-23-R1 adds exactly the delivery seam to the
  Session API and V1-24-R1 adds exactly `project_choices()`, and the GUI still never
  reaches Brain.
- **One worker, serialized.** The narrow concurrency exception covers GUI event delivery
  to the GUI thread plus exactly one non-daemon worker thread, started by
  `gui/session_worker.py`, the only production module that may import `threading`. That
  worker creates, uses, confirms, cancels, and ends the window's session, one operation at
  a time in the order the owner asked; widgets are touched only on the GUI thread. The
  exception grants no reminder scheduling, timers, autonomous or background work, or
  operation after the window has closed. Under V1-23-R1 the worker also runs the session's
  due-reminder check and one acknowledgment per notice it handed to the window, as "The
  reminder-delivery contract" below describes; for these two alone, V1-23-R1 supersedes
  "in the order the owner asked" and "grants no reminder scheduling". Neither runs on a
  timer, and the check never runs once the close was requested. Under V1-24-R1 the worker
  also reads the session's project choices at the same two points, after creating the
  session and after each operation that ends with a reply, never after an approval offer
  and never once the close was requested, and hands them to the window as a
  `ProjectList`; the read changes nothing. Only the worker's thread
  boundary catches failures of every kind, `BaseException` included, and it logs only
  fixed text or the exception type; no global exception hook is changed. A failure to
  create the session or of an operation stops the queue at once; `end()` is then
  attempted once whenever a session exists; an event that cannot be delivered is only
  logged; and only the requested close followed by an `end()` that succeeds counts as
  orderly.
- **Approval.** When an approval is offered, the worker keeps the exact `PendingApproval`
  object the session holds. Before confirming, it checks that this object is still the
  session's `pending_approval`, then calls the existing `confirm()`, which takes no
  argument; Cancel calls `cancel()`. Never add a parameter to `confirm()`. Fingerprint
  rejection, cancellation, recording, model calls, and audit summaries stay the core's
  own. Cancel dismisses a pending approval; nothing claims to stop an operation already
  running.
- **Display and clipboard.** Requests, replies, results, and approval text are plain text:
  never interpreted as HTML, no link is opened, no referenced file is loaded, and no
  displayed text is executed. Maria never writes the clipboard or exports private output
  automatically, and the result and approval displays cannot be selected or copied;
  pasting into the input works. Do not claim control over an operating-system clipboard
  history.
- **Lifecycle.** A request is accepted only while the window is idle, and an answer only
  for the offer it is showing, so duplicate submissions and stale approval callbacks are
  refused. The window and its session end together: closing while busy refuses new
  requests, shows that it is waiting, approves nothing the current operation produced,
  and ends the session before the window closes. Closing waits for the current operation
  and has no general GUI-wide deadline. Provider deadlines apply where configured, but a
  stalled operation can delay closing indefinitely. No thread is forcibly stopped, no
  hidden session remains, and while the window is open Ctrl+C in the launching terminal
  is ignored, so closing the window is the one orderly way out. If closing the window or
  delivering its last events fails, the launcher still asks the worker to end, waits for
  it, restores Ctrl+C, and reports the failure. Qt ends the process when a callback
  raises, so every window callback reaches the controller through a shield that logs a
  failure by type and marks the window failed. A failed window then closes as a
  failure: no later Send, Approve, or Cancel is accepted, queued ones included; the
  worker is asked to end the session before the window is touched again; and once the
  session has ended, the window is still asked to close even if clearing it fails.
  Status 0 needs an end the controller saw in order and no failed callback. The
  displayed history is presentation state: it is never replayed into the session's
  conversation or saved automatically. Display history is never populated automatically
  from stored records, except due-reminder notices from the session's delivery seam,
  shown as plain text (V1-23-R1). Explicit record-reading commands still display their
  results.
- **Deliberately small.** The first GUI has a read-only conversation and result area, a
  one-line input, Send, a short idle or busy status, and the complete core approval text
  with Approve and Cancel; every command, record commands included, is typed exactly as in
  the CLI. A mascot, eye animation, elaborate themes, taskbar or tray behavior, dedicated
  record browsers, and settings panels are deferred until separately approved. Under
  V1-24-R1 the window adds one project selector: a `Project` label, a non-editable list of
  plain-text items, `<id>: <name>` with ` (active)` on the active project or one
  `No projects` item, and Activate and Deactivate buttons that take no focus and are
  enabled only while the window is ready. They submit exactly `activate project <id>` or
  `deactivate project` through the same path as Send, so an activation is still offered
  for approval; the selector's items change no state, offer, or approval.

The reminder-delivery contract (V1-23-R1):

- **The closed-app pass.** The owner registers one per-user Task Scheduler task,
  `MariaReminderDelivery`, from `Maria/docs/reminder-delivery-task.xml`. While the owner
  is signed in it starts pythonw on `Maria/deliver_reminders.py` once a minute, with the
  interactive token and least privilege, never waking the computer and never overlapping
  its own scheduled runs (IgnoreNew), with no execution time limit. Each run is one
  pass that exits. Maria never registers, changes, runs, or deletes the task, never calls
  schtasks, and starts no process. A pass claims only when Windows reports S_OK with
  QUNS_ACCEPTS_NOTIFICATIONS, then shows its retained claims in one standard Windows
  message box. That box is the one owner-facing surface beyond the CLI and the GUI; no
  tray, taskbar, or toast exists.
- **One claim protocol.** Every surface, the pass, the CLI, and the GUI, claims a reminder
  before displaying it, one reminder at a time in its own write transaction, and commits
  the claim first. For each reminder and scheduling revision at most one claim is ever
  committed, and each outcome is written for its own claim only, identified by the
  reminder id and the revision. Nothing is retried, and disabling stops new claims only:
  an attempt already committed may still be displayed and recorded.
- **The session's delivery seam.** `MariaSession` takes an optional `delivery` keyword
  argument and offers `delivers_reminders`, `claim_due_reminders()`, and
  `record_reminder_notice(notice, displayed)`. Both methods contain every Exception, log
  only its type, never call Brain, a plan, a tool, Tool Manager, permissions, or a
  provider, and never touch a pending approval, the conversation, or the supplied-context
  fingerprints. `main.py` builds the seam from the runtime Brain's memory through an
  injectable factory; a caller that injects a Brain factory gets no seam unless it also
  injects a delivery factory.
- **In-session checks.** The CLI checks before every prompt, inside the prompt's own
  `try`, so Ctrl+C during the check or while a notice prints behaves exactly as at the
  prompt; it records a printed notice as displayed, or as not displayed when printing
  raises an Exception. The GUI's worker checks once after creating the session and after
  each operation that ends with a reply, never after an approval offer, never on a timer,
  and never once the close was requested. It hands the window a `DueReminderNotice` under
  a notice serial of its own; the controller appends the text without changing state and
  acknowledges that serial; and the worker records the notice as displayed only when that
  acknowledgment, queued in order with the owner's operations, reaches it. A notice never
  acknowledged stays unconfirmed. A notice is plain text and is never recorded, kept in a
  conversation, or sent to a model.
- **The box and the clipboard.** The box is plain text, and Maria calls no clipboard
  function and copies nothing automatically. The GUI's rule that its displays cannot be
  selected or copied does not extend to the box: Windows may let a person copy its text,
  which then follows the owner's clipboard history and sync settings, outside Maria's
  control.

## Module Responsibilities (do not blur these lines)

- `Maria/main.py` — the entry point and runtime composition point only. It selects the CLI
  or, for `--gui`, the desktop GUI; builds Brain through `build_runtime_brain()` for both;
  owns the CLI's terminal work; and is the one production module that names the local
  reasoning adapter. It imports the GUI only inside `run_gui()`, so CLI startup never loads
  Qt. Under V1-23-R1 it also builds the session's delivery seam through an injectable
  factory, and the CLI checks for due reminders before each prompt and prints them. Under
  V1-24-R1 it builds the window's projects seam the same way: a caller that injects a
  Brain factory gets none unless it also injects a projects factory.
- `Maria/deliver_reminders.py` — the closed-app reminder pass's entry point and
  composition point (V1-23-R1): one pass, an exit status from 0 to 5, and the entry
  boundary, which catches Exception only. It builds no Brain, never reads the private
  override, and imports neither `main.py`, the GUI, `core/config.py`, reasoning, nor the
  adapter.
- `Maria/core/runtime_paths.py` — the leaf that reads the tracked `config.toml`'s
  `[paths]` with the one shared path rule, which `core/config.py` also uses. It reads no
  other section and never the private override.
- `Maria/core/reminder_delivery.py` — the one claim protocol every surface uses:
  `claim_notice` and `record_notice`, the `ReminderNotice` value, the notice text, the
  overdue label, and the session's delivery seam. No SQL, `ctypes`, threading, sleep, or
  clock of its own: it receives the store, the instant, and the channel.
- `Maria/core/desktop_notice.py` — the only module that calls a Windows notice function:
  `available()`, `notification_state()` through SHQueryUserNotificationState, and
  `show()` through MessageBoxW with exactly MB_OK, MB_ICONINFORMATION, and MB_TOPMOST. It
  names no foreground or clipboard function.
- `Maria/core/brain.py` — coordinates planner, memory, permissions, and tool manager, and on
  the model path the conversation module and the model-to-plan compiler; it is the only
  production module that imports `Maria/core/plan_compiler.py`, and it writes audit
  summaries only through the `Memory` API. It supplies a planner plan's contract-declared
  session input generically, asking Tool Manager which argument takes it. It reads the
  current time exactly once per request, through its one clock seam, after the empty-request
  check and before planning, and hands that reference instant with the configured time
  zone key to the planner and the compiler; it holds no date logic, and confirmation reads
  no clock. **Never** put direct database access or tool-specific logic here. Dispatch must
  stay generic, with no per-tool branch. Under V1-24-R1 each model request first reads its
  one project snapshot (the activation, the active project, its eligible facts, and the
  recheck of facts supplied earlier) in one read transaction; Brain then settles the
  request's `@` references against that snapshot, reads each named file once through the
  project file reader, and hands the conversation module a final check of the activation.
  A request that names files is sent with proposal mode off and keeps nothing.
- `Maria/core/context.py`, `Maria/core/conversation.py`, `Maria/core/reasoning.py`, and
  `Maria/core/ollama_adapter.py` — under V1-24-R1, the active project's facts are a Tier A
  context source between memories and tasks, with their own framing, header, notices, and
  cleared notice, and a request may carry owner-named file parts, with their own framing,
  counted in its bytes, and sent as their own message just before the prompt. The
  conversation module runs the final check after every size check, immediately before the
  hand-off to the reasoning boundary, and keeps nothing of a file request.
- `Maria/core/project_records.py` — the project vocabulary (V1-24-R1): limits, the path
  grammar, the two roots and project locations, the activation states and serial rules,
  value objects, the fact approval digest, every fixed project text, and the file block's
  rendering; no SQL, I/O, or logging.
- `Maria/core/project_files.py` — the project file reader: the lexical phase, then, for
  each operation, the placeholder mode exposed and restored once, every component held and
  checked on its own handle, the bounded whole-file read, and listings from find data; also
  the `@` reference grammar, decoding, the display, and file parts. It calls Windows only
  through `core/project_file_access.py`, and a detected restore failure stops every later
  operation until Maria restarts.
- `Maria/core/project_file_access.py` — the only module that calls the nine Windows
  functions project file reading uses, through standard-library `ctypes` with every
  argument and result type pinned: kernel32's CreateFileW, GetFileInformationByHandle,
  GetFileInformationByHandleEx, GetFinalPathNameByHandleW, FindFirstFileExW,
  FindNextFileW, FindClose, and CloseHandle, and ntdll's
  RtlSetThreadPlaceholderCompatibilityMode, plus the binary, non-inheritable descriptor
  steps.
- `Maria/core/project_maria_manifest.py` — data only: Project Maria's approved path list
  and the files decided against, compared ignoring ASCII case and never read from the
  repository at run time.
- `Maria/tools/projects.py` — the projects tool's thirteen commands, and the only module
  that calls the memory layer's `activate_project` and `deactivate_project`.
- `Maria/core/planner.py` — turns user input into structured `Plan` and `PlanStep` objects,
  and owns the no-action response copy and the help text. `PlanOrigin.MODEL` is set only by
  the compiler. The same text, reference instant, and configured zone always give the same
  plan; only the task and reminder branches use the instant and the zone.
- `Maria/core/plan_compiler.py` — the deny-by-default gate that compiles one model
  proposal into a transient, model-originated plan of one to five steps or a fixed
  refusal, and re-checks every step of a compiled plan without reparsing the proposal. A
  creation step compiles only from the owner's own supported request, which it parses
  through `Maria/core/task_requests.py` and resolves through
  `Maria/core/time_resolution.py`; Maria builds the step from that extraction, never from
  the model's values. It executes nothing.
- `Maria/core/time_resolution.py` — the leaf for timing: the closed timing grammar, its
  resolution against a supplied reference instant and zone, gap and fold classification,
  zone loading through `zoneinfo`, and the rule-change check. It reads no clock, has no
  SQL, logs nothing, and imports only the standard library.
- `Maria/core/task_requests.py` — the four supported natural-language creation forms:
  segmentation, form matching, body and timing extraction, and the body guards. It reads no
  clock and logs nothing.
- `Maria/core/task_records.py` — the task and reminder vocabulary: limits, statuses,
  canonical formats, the stored-timing integrity rule, value objects, and fixed messages,
  and, under V1-23-R1, the delivery vocabulary (channels, outcomes, and the revision and
  claim rules); no SQL, I/O, or logging.
- `Maria/core/session.py` — owns the confirmation lifecycle for both interfaces, renders a
  model-originated plan for approval from the very plan the approval binds, and keeps the
  conversation. Under V1-23-R1 it also holds the delivery seam, which claims and records
  through the reminder-delivery subsystem, contains Exceptions, and never calls Brain.
  Under V1-24-R1 it also holds the projects seam, whose one read, `project_choices()`,
  contains Exceptions, logs only their type, and never calls Brain.
- `Maria/core/audit.py` — the closed audit-summary vocabulary and its validation; no SQL.
- `Maria/core/conversation_records.py` — the saved-conversation and conversation-summary
  vocabulary: limits, the canonical transcript, value objects, and fixed messages; no SQL,
  I/O, or logging.
- `Maria/core/memory.py` — the **only production application module** that may import
  `sqlite3` or execute SQL, and the only one that does. One generalized guard test in
  `Maria/tests/test_memory.py` enforces this boundary; keep it true. (Tests may connect
  to a temporary test database directly when necessary — never to the live database.)
  Under V1-24-R1 it owns the project tables (schema 9), the activation setting's serial
  rules, and the one-read project snapshot; only its `activate_project` and
  `deactivate_project` reach the SQL that writes the setting.
- `Maria/core/permissions.py` — decides `ALLOWED` / `REQUIRES_CONFIRMATION` / `DENIED`.
- `Maria/core/tool_manager.py` — discovers, loads, and dispatches tools.
- `Maria/core/logger.py` — application logging only. Under V1-23-R1 it also configures
  the closed-app pass's own `delivery.log`, which opens no file until its first line.
- `Maria/core/config.py` — loads and validates `Maria/config/config.toml` and the private
  override, which may hold only `[reasoning]` and `[time]`; a `[time]` section in the
  tracked file is refused. It takes its `[paths]` rule from `core/runtime_paths.py`.
- `Maria/core/constants.py` — shared identity, canonical paths, and default limits
  (`APP_NAME`, `APP_VERSION`, `PROJECT_ROOT`, `CONFIG_PATH`, `DATABASE_PATH`,
  `LOG_DIR`, `DEFAULT_LOG_FILE`, `SYSTEM_PROMPT_PATH`, `DEFAULT_TEXT_READ_LIMIT_BYTES`,
  and `DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS`, the SQLite busy-timeout constant
  `Memory._connect` passes to `sqlite3.connect()`), and, under V1-23-R1, the delivery log's
  file name and the delivery bounds (`DELIVERY_LOG_FILE_NAME`, `REMINDER_NOTICE_LIMIT`,
  and `REMINDER_OVERDUE_AFTER_SECONDS`). Reuse these rather than hardcoding a path.
- `Maria/tools/*.py` — one tool per file, thin adapters, no direct SQLite access; use the
  injected `Memory` object.
- `Maria/gui/controller.py` — the Qt-free controller: decides what the window shows and
  which owner actions are accepted, from its own state and the worker's events. Under
  V1-23-R1 it appends a due-reminder notice without changing state, then acknowledges it.
  Under V1-24-R1 it shows the project selector's items without changing state, and
  accepts Activate and Deactivate only while the window is ready.
- `Maria/gui/session_worker.py` — the one worker thread: owns the window's session and runs
  its operations in order, delivering results as events. The only production module that
  imports `threading`. Under V1-23-R1 it also runs the session's due-reminder check and
  records each notice the window acknowledges, and under V1-24-R1 it reads the session's
  project choices after creating the session and after each reply.
- `Maria/gui/window.py` — the Qt Widgets window and its launcher, the project selector
  included. The only production module that imports PyQt6; presentation only, touched only
  on the GUI thread.

New code should extend the correct module, not add logic to the wrong layer because it is
faster.

## Permission Model

`Maria/core/permissions.py` defines two mandatory frozensets:

- `MANDATORY_DENIED_ACTIONS` — `filesystem.delete` and `filesystem.write`
- `MANDATORY_CONFIRMATION_ACTIONS` — `filesystem.rename`, `filesystem.move`,
  `history.delete`, `notes.retitle`, `notes.edit`, `notes.delete`,
  `notes.enable_context`, `notes.disable_context`, `memory.update`,
  `memory.forget`, `memory.enable_context`, `memory.disable_context`,
  `audit.delete`, `conversations.delete`, `conversation_summaries.create`,
  `conversation_summaries.edit`, `conversation_summaries.delete`,
  `conversation_summaries.enable_context`, `conversation_summaries.disable_context`,
  `tasks.create`, `tasks.edit`, `tasks.reschedule`, `tasks.delete`,
  `tasks.enable_context`, `tasks.disable_context`, `reminders.create`, `reminders.edit`,
  `reminders.reschedule`, `reminders.delete`, `reminders.enable_context`,
  `reminders.disable_context`, `reminders.enable_delivery`,
  `reminders.disable_delivery`, `projects.retitle`, `projects.delete`,
  `projects.activate`, `projects.add_fact`, `projects.edit_fact`, and
  `projects.delete_fact` (thirty-nine in all; the two delivery actions under V1-23-R1,
  and the six project actions under V1-24-R1)

`PermissionManager.__init__` and `Maria/core/config.py` union these into whatever
configuration supplies, so **configuration can extend the mandatory baseline but can never
shrink it**. Malformed `[permissions]` arrays abort configuration loading rather than
weakening it. Precedence is denied, then requires-confirmation, then allowed. This is a
deliberate security boundary, not an oversight: do not loosen it without explicit user
approval.

Do **not** describe this model as globally "fail-closed". `PermissionManager.check()`
returns `ALLOWED` for any `tool.action` identifier present in neither set, and the test
suite deliberately locks that behavior in. Two consequences follow:

- A new tool action is **permitted by default**. Every new action therefore requires an
  explicit permission-risk assessment, and an entry in the denied or
  confirmation-required sets where the action is sensitive.
- Direct `ToolManager.execute_tool()` calls bypass Planner and permission checks entirely.
  That is an internal trusted-code boundary. Route all user-facing actions through
  Planner -> PermissionManager -> ToolManager -> tool.

The reminder-delivery subsystem is the sole exception to that pipeline, out of session
and, for its claim and outcome writes, in session (V1-23-R1). Those writes are not tool
actions: they pass through no permission decision, each targets one reminder row by its
identity, and the subsystem never uses ToolManager or model planning. Turning delivery on
or off is not part of that exception: `reminders.enable_delivery` and
`reminders.disable_delivery` are ordinary confirmation-required actions.

Both interfaces reach that route only through `MariaSession`: the CLI's `y`/`yes` and the
GUI's Approve both end in the session's own `confirm()`, and neither interface holds any
permission or approval policy of its own.

Configuration *validation* is the part that fails closed. The decision function is not.

### Model-originated plans

A local model's proposal has exactly one route to a tool, through Brain: the planner's
generic fallback -> the local model -> `Maria/core/plan_compiler.py` (its closed
allowlist must agree with the contract's explicit model eligibility, and every argument of
every action is checked) -> PermissionManager for every step -> exactly one owner approval
for the whole plan -> for each step in order, a committed audit summary -> ToolManager ->
tool. A proposal is one action, or a `steps` envelope of two to five, compiled all or
nothing; every plan of any origin holds at most five steps (`PLAN_MAX_STEPS`). Every
model-originated (`PlanOrigin.MODEL`) plan needs that one approval even when
PermissionManager answers `ALLOWED` for every step, and a plan with any `DENIED` action is
refused before any approval is offered. That approval requirement lives in Brain and is separate
from the two mandatory sets above, which it does not change. Only `calculator.calculate`,
`filesystem.list`, `system.current_time`, `tasks.create`, and `reminders.create` are
model-eligible; every new action contract must state its model eligibility explicitly.
The three reminder-delivery actions of V1-23-R1 and the thirteen project actions of
V1-24-R1 are all not model-eligible.

A model request that names project files with `@` (V1-24-R1) is sent with proposal mode
off: it carries no proposal instructions and no earlier exchange, and its reply is shown
as plain text only, never handed to the compiler, so text in a project file, or a reply
shaped like a proposal, can produce no plan, offer, permission decision, tool call, or
audit row. Nothing of such a request or its reply is kept.

A creation step (`tasks.create` or `reminders.create`) compiles only when the owner's
whole cleaned request is one to five items in the four supported forms of V1-22-R1
(`remind me <timing> to <body>`, `remind me to <body> <timing>`, `add a task to <body>`,
and `add a task to <body> by <date>`), parsed by `Maria/core/task_requests.py` with its
closed body guards. The proposal must hold exactly one creation step per item, in order,
whose `text` equals the item's body exactly and whose timing phrase equals the item's
own after canonical tokenization (ASCII letters lowercased, single spaces, digits as
written). Maria then builds each step from her own extraction, resolving the owner's
phrase at the request's one reference instant; no model value enters the plan, so the
model can add no body, time, or item. Raw argument checks run before the parse, and the
parse, the binding, and the resolution run before the existing step checks. This is a
declared, deterministic exception to "the compiler substitutes nothing": only the timing
value is replaced, by Maria's resolution of the owner's own words.

Approval binds to the exact immutable plan: the session fingerprints the plan it offers,
and confirming runs that same plan only while its fingerprint still matches. Maria never
re-plans, re-calls the model, or substitutes arguments after confirmation.

The approval text shows the exact compiled steps and then, before the turn's context
notices, the fixed `CONFIRMATION_NOTICES` text of every step PermissionManager resolved
as confirmation-required, each distinct notice once, in plan order (V1-22-R1, owner
decision A), so a reminder from the owner's own words says, before it is approved, that
Maria shows it at most once and only while reminder delivery is turned on and able to run
(V1-23-R1 wording). Brain asks `PermissionManager.confirmation_notice()` for each such step
generically. The notices are display only: never part of the plan, its arguments, or its
fingerprint, and never kept in the conversation, recorded, or sent to a model.

### Model-written summary drafts and session input

Model-written text has one other route into storage, and on it the model selects no tool,
action, or argument name: the planner's `summarize conversation` -> Brain asks the local
model once, through `Maria/core/conversation.py`, for a draft of exactly the session's
retained exchanges, with Maria's fixed prompt, no stored context, and no proposal
instructions -> PermissionManager -> the ordinary confirmation, which shows the whole
draft and pins its digest -> `conversation_summaries.create`, which stores exactly that
text. The draft is the contract-declared session input of that one argument
(`SessionInput.CONVERSATION_SUMMARY_DRAFT`); `conversations.save` declares the other, the
canonical transcript of exactly the retained exchanges, with no model involved. Brain
supplies session input only in `process`, for a planner's plan holding exactly
DETERMINISTIC as its own origin: never at confirmation, for a model-originated plan, or on
the conversational path. A plan that already carries the text, or holds any other step,
is refused, as is a session-input kind Brain does not name, and a contract that declares
a session input cannot be model-eligible. A task or reminder creation adds no route for
model-written text: what it stores is the owner's own text, extracted by Maria.

## Persistence

Interaction history (cleaned requests plus recorded response text), notes, explicit
memories, audit summaries, saved conversations, conversation summaries, tasks,
reminders, projects, and project facts are ten separate SQLite record types. Maria
normally records most requests
together with the result message it displayed. `ToolExecutionResult.recorded_message`,
when set, replaces only the response half of that record; when Brain records an
interaction, the cleaned `user_input` is preserved unchanged. `history.read`,
`audit.list`, `audit.read`, `conversations.list`, `conversations.read`,
`conversation_summaries.list`, `conversation_summaries.read`, `tasks.create`,
`tasks.list`, `tasks.due`, `tasks.read`, `tasks.edit`, `tasks.reschedule`,
`reminders.create`, `reminders.list`, `reminders.due`, `reminders.read`,
`reminders.edit`, `reminders.reschedule`, and the thirteen project actions of V1-24-R1
(`projects.list`, `projects.create`, `projects.read`, `projects.retitle`,
`projects.delete`, `projects.activate`, `projects.deactivate`, `projects.list_facts`,
`projects.add_fact`, `projects.edit_fact`, `projects.delete_fact`,
`projects.list_files`, and `projects.read_file`) are the only current actions that set
this field, thirty-two in all, each using a fixed content-free summary; a project
command's summary names ids only, never a name, location, path, fact, or file text,
while its request half keeps the command as typed, as a note command's does. Brain
applies the field generically, with
no tool-specific branch. A model-originated plan is transient, so neither its request nor
its result ever becomes an interaction row, and so is every outcome of
`summarize conversation`; a summary, task, or reminder edit keeps its new text in the
request half, as a note edit does. Both interfaces record identically, because recording
happens in Brain, never in an interface; what the GUI displays is never recorded by the
GUI itself. In-session reminder notices are never recorded as interactions, and the three
reminder-delivery actions of V1-23-R1 record ordinary content-free replies without
setting `recorded_message`. A model request that names project files with `@` is
transient: it records nothing, and nothing of it is kept (V1-24-R1).

A saved conversation (schema 6) is an exact, immutable copy of the exchanges a session
retained, one canonical compact-JSON transcript per explicit `save conversation`; it is
never edited, never becomes context, and is never read for planning or reasoning. A
conversation summary holds one text of at most 2,048 UTF-8 bytes that the owner approved
word for word, or later edited, and its own context consent.

Tasks and reminders (schema 7, V1-22-R1) are independent records: no foreign key,
trigger, index, cascade, or recurrence. A task holds one line of text
of at most 512 UTF-8 bytes, an optional calendar `due_date`, a status of `open` or
`completed`, UTC stamps, and its own context consent. A reminder holds the same text,
status, stamps, and consent, plus `remind_time`, its canonical RFC 9557 local time,
offset, and zone exactly as approved, and `remind_utc`, the saved instant, which must
equal that local time minus its offset on every write and read. `remind_utc` alone orders
reminders and selects due ones, and a later time zone rule change only adds a warning:
nothing is rewritten, rescheduled, or refused. A row Maria could not have written is
refused privately on read. Due and overdue are queries; only the reminder-delivery
subsystem below delivers anything, and Maria schedules, polls, and repeats nothing and
never delivers a task.

Reminder delivery state (schema 8, V1-23-R1) is part of the reminder row, not a ninth
record type: `schedule_revision`, an identity counter that starts at 1 and rises by one
exactly when an approved reschedule stores a different saved instant, and three claim
columns for the current revision only, `delivery_claimed_at`, `delivery_channel`
(desktop, cli, or gui), and `delivery_outcome` (displayed, failed, or NULL while
unconfirmed). A reschedule to a different instant starts a new revision and clears the
claim in the same statement; a reschedule to the same instant, an edit, completion,
reopening, consent changes, and turning delivery on or off keep both. Deleting a reminder
deletes its delivery state with its row. The one row of `reminder_delivery_settings` (id
1, enabled, changed_at) holds the delivery setting, which is off when the row is absent
or malformed; it is not a record type either. Tasks get no delivery columns. A row whose
delivery state Maria could not have written is refused privately on read, like any other
malformed field.

Projects and project facts (schema 9, V1-24-R1) are two record types, with no foreign
key, trigger, index, or cascade. A project holds its name, one line of 1 to 64 UTF-8
bytes with no colon, its root, `workspace` or `project_maria`, its folder within that
root, empty for the root's top and always empty for Project Maria, and UTC stamps.
Locations never overlap, compared ignoring case, and at most one Project Maria exists. A
project fact holds its project's id, one line of at most 512 UTF-8 bytes the owner
approved word for word, its approval digest, the SHA-256 of the compact JSON
`[project_id, text]`, and UTC stamps. The activation setting is at most one row of
`project_activation` (id 1, the active project's id or NULL, a serial, changed_at); it is
not a record type. It is in one of four states: never activated, with no row; active;
inactive; and unreadable, any row Maria could not have written, which is reported and
never taken for never activated. Only `activate project` and `deactivate project` change
it, and each change raises the serial by one, never past SQLite's largest integer: at
that serial an activation is refused and a deactivation keeps the serial. A project is
deleted only while it is inactive and holds no facts, facts are deleted one at a time,
and nothing cascades. A project or fact row Maria could not have written is refused
privately on read.

An audit summary (schema 5: an `audit_summaries` row plus at most one `audit_outcomes`
row) records that the owner approved one model-proposed execution attempt and Maria
authorised it for dispatch; it is committed before the tool runs, and the outcome is
appended afterwards. Every stored value is Maria's own, from the closed vocabulary in
`Maria/core/audit.py`: never the request, the proposal, an argument, private context, a
root name, a host path, a tool result, or exception text. A summary without an outcome
means the execution is unknown, and a summary alone never proves the tool ran. A plan of
several steps gets one summary per step reached for dispatch and none for later steps;
no stored value groups a plan's summaries.

Thirty-four lifecycle actions can update or remove an existing stored record. These thirty
require explicit confirmation: `history.delete`, `notes.retitle`, `notes.edit`,
`notes.delete`, `notes.enable_context`, `notes.disable_context`, `memory.update`,
`memory.forget`, `memory.enable_context`, `memory.disable_context`, `audit.delete`,
`conversations.delete`, `conversation_summaries.edit`, `conversation_summaries.delete`,
`conversation_summaries.enable_context`, `conversation_summaries.disable_context`,
`tasks.edit`, `tasks.reschedule`, `tasks.delete`, `tasks.enable_context`,
`tasks.disable_context`, `reminders.edit`, `reminders.reschedule`, `reminders.delete`,
`reminders.enable_context`, `reminders.disable_context`, `projects.retitle`,
`projects.delete`, `projects.edit_fact`, and `projects.delete_fact`. The four complete and
reopen actions, `tasks.complete`, `tasks.reopen`, `reminders.complete`, and
`reminders.reopen`, are allowed under the owner's explicit permission-risk decision
(V1-22-R1, D7a): each changes one row's status only, complete stamps the time of that
completion, reopen clears it, and an earlier completion time is never restored. Every
lifecycle action targets at most one row selected by id, with one owner-approved
exception: `audit.delete` removes one logical audit summary, its summary row together
with its optional outcome row, in one transaction, and summary ids are never reused.
Saved-conversation, conversation-summary, task, reminder, project, and project-fact ids
are never reused either.
The reminder-delivery subsystem's claim and outcome writes each target one reminder row by
its id and revision, and `reminders.enable_delivery` and `reminders.disable_delivery` each
write the one setting row; none of them is a lifecycle action of a record (V1-23-R1).
Likewise `projects.activate` and `projects.deactivate` each write the one activation
setting row and are not lifecycle actions of a record; deactivation, which only narrows
what can reach a model, needs no confirmation (V1-24-R1).
There is no bulk, wildcard, clear-all, or date-range deletion; no automatic retention or
pruning; no cascade between
the ten record types; no undo or revision history (a reminder's scheduling revision is
an identity counter that keeps no earlier time, text, or claim, not a history); and no
guarantee of secure erasure. Be
careful not to leak sensitive content into any of them when adding features. See
`Maria/docs/architecture.md` ("Persistence Models"), `Maria/docs/database.md`, and the
"Data Retention and Privacy" section of `Maria/README.md`.

### Context consent

Context consent is the only thing that lets stored text reach the reasoning model
automatically: a memory's `context_enabled` flag, and the `context_digest` of a note, a
conversation summary, a task, or a reminder, the identity of the exact text the owner
approved, a task's or reminder's timing included. Each is off by default, changes only
through its two confirmed consent actions, and is turned off by `memory.update`, by
`notes.retitle` and `notes.edit`, by `conversation_summaries.edit`, by `tasks.edit` and
`tasks.reschedule`, or by `reminders.edit` and `reminders.reschedule` respectively; a
record whose text or timing no longer matches its stored identity is never eligible.
Enabled memories are sent on every model-backed turn (Tier A, first 16 by id) or found
by literal search beyond that window (Tier B), and enabled notes and conversation
summaries only by literal search. Enabled open tasks, then reminders, are eligible for
Tier A after memories, by ascending id and only within Tier A's unchanged bounds, so an
eligible one is not always sent; a completed one is not eligible until it is reopened,
and neither is one whose stamps break the record rules. A task or reminder Tier A read
admits a row's values only as typed text, or NULL where allowed, within small
stored-byte bounds before any of them reaches Python. A project fact (V1-24-R1) has no
consent flag of its own: its consent is the owner's confirmed addition or edit, which
shows its exact text, together with the confirmed activation of its project. Only the
active project's facts whose stored project id and text still match their approval digest
are eligible, for Tier A only, after memories and before tasks, by ascending id and within
Tier A's unchanged bounds; facts of every other project are never read for context, and
project names and locations are never sent to a model. A supplied fact is current only
while it belongs to the active project with its approved digest, and a conversation that
carried a fact no longer current is cleared with a fixed notice. The digest is an
integrity check against changes made outside Maria's confirmed commands, not
authentication. Project file parts reach the model only as owner-named parts of a single
request, outside Tier A and Tier B, and are never kept in a conversation; a model request
carrying a fact or a file part is sent only if the activation setting, read once more
immediately before the hand-off, still holds the token of the request's snapshot.
Consent is not inspection: Maria
cannot tell a secret from other free text. Interaction history, saved conversations, and
audit summaries are never read for planning or reasoning. A reminder's delivery state
(V1-23-R1) is never part of its consent identity, never turns consent on or off, and is
never read for context, planning, or reasoning or sent to a model. Maria's fixed proposal
instructions and summary prompt are her own code text, never stored text. See "Tier A
Memory Context" and "Tier B Search Context" in `Maria/docs/architecture.md`.

## Scope Boundaries

The following are currently outside Maria's scope. Do not implement any of them without
explicit user approval. `Maria/docs/roadmap.md` lists several as candidate future areas, so
treat them as gated pending approval rather than as permanent exclusions:

- Filesystem `delete` — also mandatorily denied in code; changing that needs approval
- Voice recognition or text-to-speech
- Internet search or browser automation
- Autonomous or self-directed agents, and background work of any kind (the GUI's one
  worker runs only foreground, owner-initiated session operations; the one bounded
  exception is the owner-installed closed-app reminder pass of V1-23-R1, limited to its
  five reminder operations)
- Multi-user support
- Plugin marketplace, or any user interface beyond the CLI and the desktop GUI (no web
  server, browser interface, or remote access; the V1-23-R1 closed-app reminder message
  box is the one owner-facing surface beyond them), and any GUI feature beyond the
  deliberately small first GUI described under "Interfaces"
- TypeScript, PowerShell, or Bash as core logic
- Any new third-party dependency beyond the approved GUI and time zone data exceptions
  (see "Dependencies")
- Any reminder delivery or notification beyond V1-23-R1's narrow subsystem: no wake
  timers or waking the computer; no resident process, service, startup entry, tray icon,
  toast, or AppUserModelID registration; no recurrence, snooze, or delivery of tasks; no
  retry after a claim or cancellation of a claimed attempt; no automatic installation or
  removal of the task; and no timer in any interface
- Any project capability beyond V1-24-R1's: writing, renaming, or moving project files
  (Project Maria's root stays read-only); recursive search or indexing; automatic file
  context; keeping file parts in a conversation; sending project names or locations to a
  model; moving a project; a third root; Git or any other process; support for folders
  under cloud synchronization, or detecting cloud providers or the files they manage;
  holding the activation setting across a send; project file reading off Windows; and any
  automatic activation of a project, of Project Maria above all

If a task seems to require one of these, stop and flag it instead of working around the
boundary.

## Dependencies

Maria's core and CLI are standard-library only: `Maria/requirements.txt` stays free of
third-party packages, and `Maria/docs/coding_style.md` records the same rule. The one
owner-approved exception (V1-21-R1) is PyQt6 and its required runtime dependencies,
PyQt6-Qt6 and PyQt6-sip, for the optional desktop GUI alone, pinned to the exact tested
versions in `Maria/requirements-gui.txt`. No other GUI framework, Qt add-on, or helper
package is admitted, and nothing outside `Maria/gui/` may depend on Qt. The second
owner-approved exception (V1-22-R1) is the data-only `tzdata` package, optional and
pinned with its hash in `Maria/requirements-time.txt`. No Maria module imports it:
standard-library `zoneinfo` loads it only when no configured time zone files on its
search path provide a key. Only resolving new task or reminder timing and `due tasks`
need time zone data; everything else, the CLI included, works without it, and nothing
falls back to UTC, a fixed offset, or the operating system's zone. Maria never installs
dependencies automatically; a missing PyQt6 or missing time zone data produces a message,
never an installation.

## Tool Integration Contract

Adding a tool requires all five steps. Skipping any one leaves the tool unreachable:

1. Expose a module-level factory `build_tool(config=..., memory=..., logger=...)` returning
   an object that satisfies the `Tool` Protocol in `Maria/core/tool_manager.py`: `name`,
   `description`, `actions: frozenset[str]`, and
   `execute(action, arguments) -> ToolExecutionResult`.
2. Register the module in `BUILT_IN_TOOL_MODULES` in `Maria/core/tool_manager.py`, or it is
   never imported.
3. Add a routing branch in `Maria/core/planner.py`, or no user input can reach it.
4. Assess every new action against the permission model above, and add explicit denied or
   confirmation-required entries where the action is sensitive.
5. Declare every new action's argument contract, with an explicit `model_eligibility`, in
   `Maria/core/action_contracts.py`, or contract validation refuses the action. An
   argument whose text only Maria may supply from the session also declares its
   `SessionInput`.

A new tool needs no GUI work: the GUI reaches every command by its typed text, exactly as
the CLI does.

## Coding Conventions (match the existing code exactly)

- Python 3.11+ (required for `tomllib`), PEP 8, standard library only — except
  `Maria/gui/window.py`, which alone imports PyQt6, and the optional `tzdata` data, which
  only standard-library `zoneinfo` loads (see "Dependencies"). Only
  `Maria/core/time_resolution.py` imports `zoneinfo`.
- `from __future__ import annotations` in every module that contains code. The
  docstring-only `__init__.py` files are exempt.
- `@dataclass(frozen=True)` for value objects (`BrainResponse`, `Plan`, `PlanStep`,
  `PermissionResult`, `ToolExecutionResult`, `MariaConfig`), and `str, Enum` for closed sets
  of outcomes (`PermissionDecision`).
- Full type hints on public functions and important internal helpers.
- Short, purpose-first docstrings on every module, class, and public function.
- Raise or return explicit errors; never fail silently.
- Log important actions via `Maria/core/logger.py`; do not log sensitive content. The GUI
  never configures logging itself.
- Only `Maria/core/desktop_notice.py` calls a Windows notice function, through
  standard-library `ctypes`: MessageBoxW and SHQueryUserNotificationState only, with
  exactly MB_OK, MB_ICONINFORMATION, and MB_TOPMOST. It calls no foreground or clipboard
  function (V1-23-R1).
- Only `Maria/core/project_file_access.py`, one additional `ctypes` module, calls the nine
  Windows functions project file reading uses, with every argument and result type
  pinned: CreateFileW, GetFileInformationByHandle, GetFileInformationByHandleEx,
  GetFinalPathNameByHandleW, FindFirstFileExW, FindNextFileW, FindClose, CloseHandle,
  and RtlSetThreadPlaceholderCompatibilityMode (V1-24-R1). No other production module
  names them.
- No production module starts a process, runs a shell command, or evaluates, compiles,
  or executes code; registered-tool dispatch through ToolManager's fixed module list is
  the one dynamic import (V1-24-R1).
- Avoid circular imports.

## Testing

Run the suite with `Maria/` as the working directory. It fails from the repository root,
and `-s Maria/tests` also fails, because `unittest discover` sets `top_level_dir` to the
start directory while the tests import `core.*`, `tools.*`, `gui.*`, and `main` as
top-level names:

```
cd Maria
python -m unittest discover -s tests -v
```

- Standard-library `unittest` only, in `Maria/tests/`, named `test_<module>.py` with
  descriptive `test_*` method names. Follow the pattern in `Maria/tests/test_config.py`.
- Use `TemporaryDirectory` and temporary databases and log directories. A test must never
  read or write the live `Maria/database/maria.db`, `Maria/logs/maria.log`, or
  `Maria/logs/delivery.log`, or read the private `Maria/config/config.local.toml`.
  Inspecting a temporary test database directly is fine where it makes the assertion
  clearer.
- Reminder-delivery tests (V1-23-R1) never call the real MessageBoxW or
  SHQueryUserNotificationState and never register, run, end, or delete an
  operating-system task; every clock the delivery code reads is injected,
  and nothing sleeps.
- Project file tests (V1-24-R1) make real Windows calls only on temporary folders the
  test creates, where they try junctions, hard links, share modes, short-name aliases
  (skipped when the volume keeps none), and symbolic links (skipped without the privilege
  to make one); every other reader test goes through the fake access of
  `Maria/tests/project_fakes.py`, which can make each Windows call fail in turn. Project
  storage tests run in UTF-8, UTF-16le, and UTF-16be databases. The Project Maria
  manifest tests that look at files run only in an isolated copy that holds every
  explicitly approved application path, new unstaged files included, and no private,
  runtime, or Git file; nothing is staged or committed to build it.
- Any behavior change needs a corresponding focused test. A task that changes behavior
  without adding or updating tests is not done.
- **Never state that the suite passes unless you actually executed it and saw the output.**
  If tests fail, say so and include the output.
- GUI tests: the controller and worker tests never import Qt. Qt-dependent tests import
  PyQt6 only inside their own test module, run on Qt's offscreen platform, and skip
  explicitly, with a reason, when PyQt6 or that platform is unavailable. A skipped or
  offscreen test is never reported as visual verification; a real-window check is reported
  separately. Run the suite both without PyQt6 and with the pinned PyQt6. Never force a Qt
  platform that cannot start in an automated test or probe: on Windows, Qt then shows a
  modal error dialog on the owner's desktop whenever the process has no console. Never
  leave Maria's Qt message handler installed through interpreter shutdown, which crashes
  the process as Qt logs at exit: put back exactly the handler installed before it.
- Time zone tests: daylight-saving and rule-change tests use synthetic zones built in the
  test, loaded through `ZoneInfo.from_file` or through `zoneinfo.reset_tzpath()` pointed
  at a temporary directory with the zoneinfo cache cleared, and always restore the search
  path and clear the cache afterwards. A test that needs real IANA data skips explicitly,
  with a reason, when no data source provides it. Never write a zone the owner actually
  uses into a tracked file. Run the suite both without and with the pinned `tzdata`, as
  well as without and with the pinned PyQt6.
- Agent-run verification: run Maria imports, tests, probes, and the CLI only in a fresh
  isolated copy outside the checkout of the tracked files and every explicitly approved
  new application path, new unstaged files included, with bytecode writing disabled
  (`python -B`), synthetic data, and temporary resources; never against the real checkout,
  the private override, the live database, or the live log. Prove the copy excludes them
  before running, and never stage or commit anything to build it. Install PyQt6 and
  `tzdata` only into isolated virtual environments
  outside the checkout, never globally.

## Documentation

- Update the relevant `Maria/docs/` file whenever you change behavior it describes — do not
  let `Maria/docs/architecture.md` or `Maria/README.md` drift from the actual code.
- `Maria/CHANGELOG.md` follows Keep-a-Changelog style with an `## Unreleased` section at the
  top. Add an entry there for any user-visible or architectural change.
- `Maria/docs/roadmap.md` is reserved for future version planning. Do not implement roadmap
  items without explicit approval.

## Git and Safety

- The published tags are `v0.1.0`, `v0.1.1`, `v0.1.2`, and `v0.2.0`. All four are
  immutable: never retag, move, delete, amend, recreate one against another commit,
  force-push, or otherwise rewrite published history.
- They are **annotated** tags, so `git rev-parse v0.1.0` returns the tag object rather
  than the commit. Use `git rev-parse "v0.1.0^{commit}"` or `git rev-list -n 1 v0.1.0`.
- Do not `add`, `commit`, `push`, or `tag` without explicit user permission.
- This repository is public. Never commit secrets, credentials, personal email addresses,
  real names, or absolute machine paths.

## Release and Version Policy

The active effort is **Maria v1.0**; `Maria/docs/roadmap.md` defines its stages. Until it
ships, the public release surface and the version literals are frozen:

- `v0.2.0` remains the latest public release until `v1.0.0`.
- Focused development branches and pull requests continue normally.
- No intermediate public `0.x` release branch, annotated tag, or GitHub Release may be
  created during the Maria v1.0 effort.
- `APP_VERSION` in `Maria/core/constants.py` and the shipped `Maria/config/config.toml`
  version both stay `0.2.0` through Stage V1-31. No feature, documentation, maintenance,
  milestone, audit, or ordinary fix branch may change either value before Stage V1-32.
- Only Stage V1-32, the owner-approved `release/v1.0.0` branch, may move `APP_VERSION`,
  the shipped configuration version, and the corresponding hardcoded test expectations
  from `0.2.0` to `1.0.0`.
- `release/v1.0.0` may be created only after the Stage V1-31 completion audit passes.
- The `v1.0.0` annotated tag may be created only after the release-preparation pull
  request is merged and the resulting `main` merge commit is independently verified.
- The GitHub Release may be created only after that annotated tag is pushed and
  independently verified.

## Working Style

- Do not guess on a large architectural decision. Explain the uncertainty, present one or
  two options, and wait for approval. If you see a better implementation that still respects
  these principles, explain your reasoning before applying it.
- Prefer small, reviewable changes over large speculative rewrites, and optimize for
  long-term maintainability over short-term convenience.
- Report honestly: if a step was skipped or a test failed, say so plainly.
