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
| `Maria/core/` | `brain`, `planner`, `memory`, `permissions`, `tool_manager`, `logger`, `config`, `constants`, `action_contracts`, `audit`, `context`, `conversation`, `conversation_records`, `failures`, `filesystem_roots`, `ollama_adapter`, `plan_compiler`, `reasoning`, `session` |
| `Maria/tools/` | `audit`, `calculator`, `conversation_summaries`, `conversations`, `filesystem`, `history`, `memory`, `notes`, `system` |
| `Maria/gui/` | the optional desktop GUI: `controller` (Qt-free), `session_worker` (the one worker thread), `window` (the only Qt module) |
| `Maria/tests/` | standard-library `unittest` suite |
| `Maria/docs/` | `architecture`, `api`, `database`, `planner`, `tools`, `coding_style`, `roadmap`, `vision` |
| `Maria/config/config.toml` | TOML configuration |
| `Maria/prompts/` | starter prompt text |
| `Maria/requirements.txt` | the core and CLI requirements: standard library only |
| `Maria/requirements-gui.txt` | the optional GUI requirements: PyQt6 and its runtime dependencies, pinned |
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
application's real persistence and log, and `Maria/config/config.local.toml`, when present,
is the owner's private, untracked configuration override. Normal application access
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
  session's `submit()`, `pending_approval`, `confirm()`, `cancel()`, and `end()`. The GUI
  changes neither Brain nor the Session API.
- **One worker, serialized.** The narrow concurrency exception covers GUI event delivery
  to the GUI thread plus exactly one non-daemon worker thread, started by
  `gui/session_worker.py`, the only production module that may import `threading`. That
  worker creates, uses, confirms, cancels, and ends the window's session, one operation at
  a time in the order the owner asked; widgets are touched only on the GUI thread. The
  exception grants no reminder scheduling, timers, autonomous or background work, or
  operation after the window has closed. Only the worker's thread boundary catches
  failures of every kind, `BaseException` included, and it logs only fixed text or the
  exception type; no global exception hook is changed. A failure to create the session or
  of an operation stops the queue at once; `end()` is then attempted once whenever a
  session exists; an event that cannot be delivered is only logged; and only the
  requested close followed by an `end()` that succeeds counts as orderly.
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
  from stored records. Explicit record-reading commands still display their results.
- **Deliberately small.** The first GUI has a read-only conversation and result area, a
  one-line input, Send, a short idle or busy status, and the complete core approval text
  with Approve and Cancel; every command, record commands included, is typed exactly as in
  the CLI. A mascot, eye animation, elaborate themes, taskbar or tray behavior, dedicated
  record browsers, and settings panels are deferred until separately approved.

## Module Responsibilities (do not blur these lines)

- `Maria/main.py` — the entry point and runtime composition point only. It selects the CLI
  or, for `--gui`, the desktop GUI; builds Brain through `build_runtime_brain()` for both;
  owns the CLI's terminal work; and is the one production module that names the local
  reasoning adapter. It imports the GUI only inside `run_gui()`, so CLI startup never loads
  Qt.
- `Maria/core/brain.py` — coordinates planner, memory, permissions, and tool manager, and on
  the model path the conversation module and the model-to-plan compiler; it is the only
  production module that imports `Maria/core/plan_compiler.py`, and it writes audit
  summaries only through the `Memory` API. It supplies a planner plan's contract-declared
  session input generically, asking Tool Manager which argument takes it. **Never** put
  direct database access or tool-specific logic here. Dispatch must stay generic, with no
  per-tool branch.
- `Maria/core/planner.py` — turns user input into structured `Plan` and `PlanStep` objects,
  and owns the no-action response copy and the help text. `PlanOrigin.MODEL` is set only by
  the compiler.
- `Maria/core/plan_compiler.py` — the deny-by-default gate that compiles one model
  proposal into a transient, model-originated plan of one to five steps or a fixed
  refusal, and re-checks every step of a compiled plan without reparsing it. It executes
  nothing.
- `Maria/core/session.py` — owns the confirmation lifecycle for both interfaces, renders a
  model-originated plan for approval from the very plan the approval binds, and keeps the
  conversation.
- `Maria/core/audit.py` — the closed audit-summary vocabulary and its validation; no SQL.
- `Maria/core/conversation_records.py` — the saved-conversation and conversation-summary
  vocabulary: limits, the canonical transcript, value objects, and fixed messages; no SQL,
  I/O, or logging.
- `Maria/core/memory.py` — the **only production application module** that may import
  `sqlite3` or execute SQL, and the only one that does. One generalized guard test in
  `Maria/tests/test_memory.py` enforces this boundary; keep it true. (Tests may connect
  to a temporary test database directly when necessary — never to the live database.)
- `Maria/core/permissions.py` — decides `ALLOWED` / `REQUIRES_CONFIRMATION` / `DENIED`.
- `Maria/core/tool_manager.py` — discovers, loads, and dispatches tools.
- `Maria/core/logger.py` — application logging only.
- `Maria/core/config.py` — loads and validates `Maria/config/config.toml`.
- `Maria/core/constants.py` — shared identity, canonical paths, and default limits
  (`APP_NAME`, `APP_VERSION`, `PROJECT_ROOT`, `CONFIG_PATH`, `DATABASE_PATH`,
  `LOG_DIR`, `DEFAULT_LOG_FILE`, `SYSTEM_PROMPT_PATH`, `DEFAULT_TEXT_READ_LIMIT_BYTES`,
  and `DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS`, the SQLite busy-timeout constant
  `Memory._connect` passes to `sqlite3.connect()`). Reuse these rather than hardcoding a
  path.
- `Maria/tools/*.py` — one tool per file, thin adapters, no direct SQLite access; use the
  injected `Memory` object.
- `Maria/gui/controller.py` — the Qt-free controller: decides what the window shows and
  which owner actions are accepted, from its own state and the worker's events.
- `Maria/gui/session_worker.py` — the one worker thread: owns the window's session and runs
  its operations in order, delivering results as events. The only production module that
  imports `threading`.
- `Maria/gui/window.py` — the Qt Widgets window and its launcher. The only production
  module that imports PyQt6; presentation only, touched only on the GUI thread.

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
  `conversation_summaries.enable_context`, and
  `conversation_summaries.disable_context`

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
`filesystem.list`, and `system.current_time` are model-eligible; every new action
contract must state its model eligibility explicitly.

Approval binds to the exact immutable plan: the session fingerprints the plan it offers,
and confirming runs that same plan only while its fingerprint still matches. Maria never
re-plans, re-calls the model, or substitutes arguments after confirmation.

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
a session input cannot be model-eligible.

## Persistence

Interaction history (cleaned requests plus recorded response text), notes, explicit
memories, audit summaries, saved conversations, and conversation summaries are six
separate SQLite record types. Maria normally records most requests together with the
result message it displayed. `ToolExecutionResult.recorded_message`, when set, replaces
only the response half of that record; when Brain records an interaction, the cleaned
`user_input` is preserved unchanged. `history.read`, `audit.list`, `audit.read`,
`conversations.list`, `conversations.read`, `conversation_summaries.list`, and
`conversation_summaries.read` are the only current actions that set this field, each
using a fixed content-free summary. Brain applies the field generically, with no
tool-specific branch. A model-originated plan is transient, so neither its request nor its
result ever becomes an interaction row, and so is every outcome of
`summarize conversation`; a summary edit keeps its new text in the request half, as a
note edit does. Both interfaces record identically, because recording happens in Brain,
never in an interface; what the GUI displays is never recorded by the GUI itself.

A saved conversation (schema 6) is an exact, immutable copy of the exchanges a session
retained, one canonical compact-JSON transcript per explicit `save conversation`; it is
never edited, never becomes context, and is never read for planning or reasoning. A
conversation summary holds one text of at most 2,048 UTF-8 bytes that the owner approved
word for word, or later edited, and its own context consent.

An audit summary (schema 5: an `audit_summaries` row plus at most one `audit_outcomes`
row) records that the owner approved one model-proposed execution attempt and Maria
authorised it for dispatch; it is committed before the tool runs, and the outcome is
appended afterwards. Every stored value is Maria's own, from the closed vocabulary in
`Maria/core/audit.py`: never the request, the proposal, an argument, private context, a
root name, a host path, a tool result, or exception text. A summary without an outcome
means the execution is unknown, and a summary alone never proves the tool ran. A plan of
several steps gets one summary per step reached for dispatch and none for later steps;
no stored value groups a plan's summaries.

Sixteen lifecycle actions can update or remove an existing stored record:
`history.delete`, `notes.retitle`, `notes.edit`, `notes.delete`, `notes.enable_context`,
`notes.disable_context`, `memory.update`, `memory.forget`, `memory.enable_context`,
`memory.disable_context`, `audit.delete`, `conversations.delete`,
`conversation_summaries.edit`, `conversation_summaries.delete`,
`conversation_summaries.enable_context`, and `conversation_summaries.disable_context`.
Each requires explicit confirmation and targets at most one row selected by id, with one
owner-approved exception: `audit.delete` removes one logical audit summary, its summary
row together with its optional outcome row, in one transaction, and summary ids are
never reused. Saved-conversation and conversation-summary ids are never reused either.
There is no bulk, wildcard, clear-all, or date-range deletion; no automatic retention or
pruning; no cascade between
the six record types; no undo or revision history; and no guarantee of secure erasure. Be
careful not to leak sensitive content into any of them when adding features. See
`Maria/docs/architecture.md` ("Persistence Models"), `Maria/docs/database.md`, and the
"Data Retention and Privacy" section of `Maria/README.md`.

### Context consent

Context consent is the only thing that lets stored text reach the reasoning model
automatically: a memory's `context_enabled` flag, and the `context_digest` of a note or a
conversation summary, the identity of the exact text the owner approved. Each is off by
default, changes only through its two confirmed consent actions, and is turned off by
`memory.update`, by `notes.retitle` and `notes.edit`, or by `conversation_summaries.edit`
respectively; a note or summary whose text no longer matches its stored identity is never
eligible. Enabled memories are sent on every model-backed turn (Tier A, first 16 by id)
or found by literal search beyond that window (Tier B), and enabled notes and conversation
summaries only by literal search. Consent is not inspection: Maria cannot tell a secret
from other free text. Interaction history, saved conversations, and audit summaries are
never read for planning or reasoning. Maria's fixed proposal instructions and summary
prompt are her own code text, never stored text. See "Tier A Memory Context" and
"Tier B Search Context" in `Maria/docs/architecture.md`.

## Scope Boundaries

The following are currently outside Maria's scope. Do not implement any of them without
explicit user approval. `Maria/docs/roadmap.md` lists several as candidate future areas, so
treat them as gated pending approval rather than as permanent exclusions:

- Filesystem `delete` — also mandatorily denied in code; changing that needs approval
- Voice recognition or text-to-speech
- Internet search or browser automation
- Autonomous or self-directed agents, and background work of any kind (the GUI's one
  worker runs only foreground, owner-initiated session operations)
- Multi-user support
- Plugin marketplace, or any user interface beyond the CLI and the desktop GUI (no web
  server, browser interface, or remote access), and any GUI feature beyond the deliberately
  small first GUI described under "Interfaces"
- TypeScript, PowerShell, or Bash as core logic
- Any new third-party dependency beyond the one approved GUI exception (see
  "Dependencies")

If a task seems to require one of these, stop and flag it instead of working around the
boundary.

## Dependencies

Maria's core and CLI are standard-library only: `Maria/requirements.txt` stays free of
third-party packages, and `Maria/docs/coding_style.md` records the same rule. The one
owner-approved exception (V1-21-R1) is PyQt6 and its required runtime dependencies,
PyQt6-Qt6 and PyQt6-sip, for the optional desktop GUI alone, pinned to the exact tested
versions in `Maria/requirements-gui.txt`. No other GUI framework, Qt add-on, or helper
package is admitted, and nothing outside `Maria/gui/` may depend on Qt. Maria never
installs dependencies automatically; a missing PyQt6 produces a message, never an
installation.

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
  `Maria/gui/window.py`, which alone imports PyQt6 (see "Dependencies").
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
  read or write the live `Maria/database/maria.db` or `Maria/logs/maria.log`, or read the
  private `Maria/config/config.local.toml`. Inspecting a temporary test database directly
  is fine where it makes the assertion clearer.
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
- Agent-run verification: run Maria imports, tests, probes, and the CLI only in a fresh
  isolated copy of the tracked files outside the checkout, with bytecode writing disabled
  (`python -B`), synthetic data, and temporary resources; never against the real checkout,
  the private override, the live database, or the live log. Prove the copy excludes them
  before running. Install PyQt6 only into an isolated virtual environment outside the
  checkout, never globally.

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
