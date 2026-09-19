# Changelog

## Unreleased

- Bound confirmation to the exact plan the owner approved. Confirming a
  sensitive action now executes the plan that was presented for approval
  instead of re-sending the original request text, so Maria no longer
  plans that request a second time after confirmation and can no longer
  execute a different action than the one it announced. The confirmation
  lifecycle moved out of the command line into a shared in-memory session
  contract that any present or future interface can drive without a
  terminal: it holds at most one pending plan together with the
  fingerprint captured when that plan was shown, consumes an approval
  exactly once so it cannot be replayed, and discards the pending plan as
  soon as another request arrives. A pending plan that was invalidated,
  already used, cancelled, or no longer matches its captured fingerprint
  executes nothing and returns one fixed refusal that repeats no request,
  argument, path, or stored content. The pending plan is process memory
  only and is never written to the database, a file, or configuration.
  Confirmation prompt wording, the accepted `y`/`yes` and `n`/`no`
  answers, cancellation behavior, CLI output, and exit codes are
  unchanged. Configuring logging a second time is now honoured by
  replacing Maria's file handler instead of being silently ignored. No
  schema, permission, or version change was introduced.
- Resolved permission for every step of a plan before executing any part
  of it. Maria previously decided one step's permission and ran that same
  step before looking at the next, so a plan whose later step was denied
  could already have executed an earlier one. Permission resolution is now
  a complete first phase and dispatch a separate second phase: a denial
  anywhere in a plan denies the whole plan and executes no tool at all, and
  a plan that needs confirmation executes nothing before the owner approves
  it, even when an earlier step would have been allowed on its own. A plan
  that both needs confirmation and contains a denial is denied outright
  rather than offered for approval. Confirming re-resolves the whole plan's
  permissions before dispatching anything, so a decision that changed since
  the plan was shown still applies and the consumed approval still cannot be
  replayed. Approved steps then run in plan order and at most once each.
  Stop-on-failure is unchanged and remains the documented semantics: the
  first unsuccessful result stops the plan, and Maria neither rolls back nor
  compensates for the steps that already succeeded. Every command Maria
  currently understands still plans to at most one step, so single-step
  responses, confirmation wording, recorded interaction rows, CLI output,
  and every permission decision are unchanged. No schema, permission, or
  version change was introduced.
- Gave every built-in action one independent declarative argument contract,
  enforced before any permission is evaluated. Registered action membership
  was never argument validation: nothing declared which arguments an action
  required, which were optional, what types they accepted, what bounds
  applied, or which fields were paths, and several tools quietly converted a
  malformed value with a bare string or integer conversion instead of
  refusing it. A new `core/action_contracts.py` now declares all 22 built-in
  tool actions by hand, independently of the loaded tool inventory, of
  `PermissionManager`, and of the planner, and a two-way consistency test
  proves the declared set and the loaded set match exactly. Validation runs
  as a complete first phase over a whole plan, so an unknown tool, an unknown
  action, or a malformed argument anywhere refuses the plan with zero
  permission checks, zero tool calls, and no pending confirmation; confirming
  an approved plan validates the same plan again before permissions are
  re-resolved. Missing, extra, wrong-type, and out-of-range fields each
  produce a distinct, content-safe refusal naming only the tool, action, and
  field, never the submitted value, and duplicate contract declarations are
  rejected rather than silently overwritten. Every field declares every bound
  that applies to its type: an integer field its numeric minimum and maximum,
  a text field whether blank content is accepted and its minimum and maximum
  length, and an absent bound is declared as absent rather than left open. A
  declaration whose metadata cannot apply to its type — a length bound on an
  integer, a numeric bound on text, a negative or reversed length pair, a
  missing blank policy — is rejected when the contract is built. Every text
  field rejects blank or whitespace-only content, matching what each tool
  already refused, and `calculator.calculate.expression` declares the
  established 200-character maximum, measured on the stripped text exactly as
  the calculator measures it. Nothing is coerced, parsed, truncated, clamped,
  defaulted, or repaired, and `bool` is no longer accepted where an integer is
  required. Path fields on `filesystem.read`,
  `filesystem.list`, `filesystem.rename`, and `filesystem.move` declare the
  configured safe-root class as classification metadata only; no path is
  resolved and no filesystem access happens during validation. `ToolManager`
  validates the same contracts at its own dispatch boundary, so a direct
  caller cannot recover the old converting behavior, and the tools keep their
  own checks as defence in depth. The deliberately tightened malformed paths
  are the calculator expression; every filesystem path, source, and
  destination; the history search query; the memory fact and search query;
  the note title, body, and read target; and the memory, memory-search, and
  notes listing limits, where a provided value must now be an actual integer
  between 1 and 100 rather than being converted or clamped. A provided blank
  `notes.create` title is no longer silently repaired to `Untitled`; only a
  genuinely absent title still takes that tool-owned default, exactly as an
  absent `filesystem.list` path still defaults to the safe root and an absent
  listing limit still defaults to 20. The contract never inserts a default
  into a plan's arguments, and `core/memory.py` is unchanged: its internal
  limit clamp remains as defence in depth below the new gates rather than
  being the acceptance policy. One user-visible consequence: `create note
  <title>:` with an empty body now reports the contract refusal naming the
  blank `body` field instead of the tool's `Note body is required.`. Every
  other valid deterministic command, its output, its recorded interaction row,
  and every permission decision are unchanged; `core/permissions.py`, both
  mandatory sets, and its tests are untouched. No schema, permission, or
  version change was introduced.

## 0.2.0 - 2026-09-13

- Added read-only interaction-history browsing: `history`, `search history
  <query>`, and `read interaction <id>`, backed by a new `HistoryTool`
  (`list`, `search`, `read` actions) and two new `Memory` methods
  (`get_interaction`, `search_interactions`). `history` and `search history`
  show at most the newest 20 rows with a more-results indicator when older
  rows exist; `read interaction` displays the complete stored request,
  response, and timestamp but records only a fixed, content-free audit
  summary instead of duplicating that content into a new interaction row, via
  a new generic, optional `ToolExecutionResult.recorded_message` field that
  Brain consumes without any tool-specific branch. No schema, permission, or
  version change was introduced.
- Added interaction-history deletion: `delete interaction <id>`, backed by a
  new `HistoryTool` `delete` action and a new `Memory.delete_interaction`
  method (`DELETE FROM interactions WHERE id = ?`). The command accepts only
  a single positive ASCII-decimal ID, targets at most one row, and removes it
  when found. It requires mandatory explicit confirmation — `history.delete`
  is a new entry in `MANDATORY_CONFIRMATION_ACTIONS` alongside
  `filesystem.rename` and `filesystem.move` — with no preview of stored
  content before confirmation.
  Deletion commits independently, and Maria then separately attempts to
  record a short, content-free audit interaction (`Deleted interaction
  <id>.`); when that later recording succeeds, the total interaction row
  count may remain unchanged even though the selected historical content is
  gone, and if it fails, the deletion remains committed with no audit row
  guaranteed. There is no bulk, wildcard, range, or cascading deletion, no
  renumbering of remaining IDs, and no guarantee of secure erasure from
  SQLite file pages, backups, or external copies. No schema or version
  change was introduced.
- Added note lifecycle management: `retitle note <id>: <new title>`,
  `edit note <id>: <new body>`, and `delete note <id>`, backed by three new
  `NotesTool` actions (`retitle`, `edit`, `delete`) and three new `Memory`
  methods (`retitle_note`, `edit_note`, `delete_note`). Title editing and
  body editing are deliberately separate commands, so neither can overwrite
  the other's field, and each command accepts only a single positive
  ASCII-decimal ID — never a title — and affects at most one row. All three
  require mandatory explicit confirmation; `notes.retitle`, `notes.edit`, and
  `notes.delete` are new entries in `MANDATORY_CONFIRMATION_ACTIONS`
  alongside `filesystem.rename`, `filesystem.move`, and `history.delete`,
  with no preview of stored content before confirmation. A successful retitle
  or edit preserves `created_at` and refreshes `updated_at` with the current
  UTC timestamp, even when the new value matches the stored one; duplicate
  note titles remain allowed. Each mutation commits independently, and Maria
  then separately attempts to record a short, content-free response (`Retitled
  note <id>.`, `Edited note <id>.`, or `Deleted note <id>.`); if that later
  recording fails, the mutation remains committed with no audit row
  guaranteed. Because the original command is stored as the request half of
  interaction history, a retitle retains its new title and an edit retains
  its new body there, and later changing or deleting the note does not
  rewrite those independent interaction rows. There is no bulk, wildcard,
  clear-all, or automatic deletion, no cascade into interaction history or
  explicit memories, no renumbering of remaining note IDs, no undo or note
  revision history, and no guarantee of secure erasure from SQLite file
  pages, backups, or external copies. No schema or version change was
  introduced.
- Added explicit-memory lifecycle management: `update memory <id>: <new fact>`
  and `forget memory <id>`, backed by two new `MemoryTool` actions (`update`,
  `forget`) and two new `Memory` methods (`update_memory`, `forget_memory`).
  Each command accepts only a single positive ASCII-decimal ID — never fact
  text — and affects at most one row; `update memory` splits its operand on the
  first colon only, so later colons stay literal inside the new fact. Both
  require mandatory explicit confirmation; `memory.update` and `memory.forget`
  are new entries in `MANDATORY_CONFIRMATION_ACTIONS` alongside
  `filesystem.rename`, `filesystem.move`, `history.delete`, `notes.retitle`,
  `notes.edit`, and `notes.delete`, bringing that baseline to eight actions,
  with no preview of stored content before confirmation. `remember`, `recall`,
  and `memories` are unchanged. A successful update rewrites only `fact` and
  `normalized_fact` and preserves `created_at`; no `memories.updated_at` column
  was added, so an edited memory keeps its place in newest-first results
  instead of moving to the top. A new fact equal to the stored one, or
  differing from it only by casing, still counts as a successful update.
  Because `normalized_fact` remains unique over `str.casefold()`, an update
  whose casefolded fact already belongs to a different memory returns the
  controlled, content-free result `Memory <id> conflicts with existing memory
  <other id>.` rather than a raw constraint error; the conflict is classified
  by SQLite's exact extended `SQLITE_CONSTRAINT_UNIQUE` result code, so other
  constraint failures keep their existing static message, and neither memory is
  changed, merged, or removed. Neither mutation reads its target row first.
  Each mutation commits independently, and Maria then separately attempts to
  record a short, content-free response (`Updated memory <id>.` or `Forgot
  memory <id>.`); if that later recording fails, the mutation remains committed
  with no audit row guaranteed. Because the original command is stored as the
  request half of interaction history, an update command retains its new fact
  there, and an earlier `remember`, `recall`, or `memories` response may
  already have copied a fact into its own interaction row; forgetting the
  memory afterwards does not rewrite or remove those independent rows. There is
  no mutation by fact text, no fuzzy or contextual selection, no bulk,
  wildcard, clear-all, or automatic deletion, no cascade into interaction
  history or notes, no renumbering of remaining memory IDs, no undo or memory
  revision history, and no guarantee of secure erasure from SQLite file pages,
  backups, or external copies. No schema or version change was introduced.
- Made active command guidance version-neutral: CLI startup and help text,
  unsupported-command responses, and the oversized-file response no longer
  label the command set as v0.1; command grammar, filesystem limits,
  permissions, versions, and operational behavior are unchanged.

## 0.1.2 - 2026-09-09

- Made filesystem responses show paths relative to the configured safe root using
  forward slashes, with the safe root itself shown as `.`, instead of absolute host
  paths; operating-system failure reasons are now derived from the numeric `errno`,
  Maria's two fixed path-validation messages remain supported, and raw
  exception-supplied text and filename fields such as `strerror`, `filename`, and
  `filename2` never reach response text. The improvement is prospective and does
  not rewrite existing interaction-history rows, and a path a user typed
  explicitly can still be retained as request text. Path resolution, safe-root
  containment, symlink handling, collision behavior, move parent-directory
  creation, read size limits, UTF-8 replacement decoding, confirmation policy,
  command grammar, tool result `data`, and operational logging are all unchanged.
- Made the existing newest-20 display limit visible for `notes`, `memories`, and
  `recall <query>`: when more than 20 rows exist, only the newest 20 are shown
  and one line is appended stating that more results exist. Output for 20 or
  fewer results, ordering, storage, schema, permissions, and command grammar are
  all unchanged.
- Made SQLite busy/locked contention bounded and explicit: every database
  connection now sets a documented 5.0-second SQLite busy timeout instead of
  relying on the implicit default, governing SQLite's own bounded waiting for
  `SQLITE_BUSY` contention, with no added Python retry loop, sleep, backoff,
  or whole-operation replay. `SQLITE_BUSY` and `SQLITE_LOCKED`, including
  extended variants of either, are classified by SQLite's numeric primary
  result code rather than by exception text, and Maria now returns a fixed
  `Database is busy. Try again shortly.` message if SQLite ultimately reports
  either code, instead of that operation's generic failure text. Every other
  SQLite failure keeps its existing operation-specific message. No Python
  retry loop, schema, transaction, permission, command, or data-contract
  change was introduced.

## 0.1.1 - 2026-09-06

- Made `create note` require an explicit first-colon separator between title
  and body; `create note : <body>` still creates an explicitly untitled
  (`Untitled`) note, and `create note <text>` with no colon now returns a
  controlled `Use: create note <title>: <body>` response instead of silently
  creating an Untitled note.
- Widened the CLI confirmation prompt to accept `y` or `yes` to approve and
  `n` or `no` to cancel, case- and whitespace-insensitively, with blank or
  unrecognized input re-prompting instead of cancelling; EOF and Ctrl+C still
  cancel immediately and safely, including after earlier invalid input.
- Added `sys` and `sys info` as aliases for the `system` overview command, and
  `time now` as an alias for the `time` current-time command; `status` and
  `current time` continue to work unchanged.
- Replaced the dedicated `time` command's single ISO-8601 line with a
  readable, timezone-aware four-line report (`Date`, `Time`, `UTC offset`,
  `ISO-8601`), and replaced the `system` overview's `Current time` line with
  a compact reading of the same fields. Both are derived from a single
  timezone-aware datetime capture per command and remain locale-independent.

## 0.1.0 - 2026-08-15

- Created the initial Maria project foundation.
- Added core modules for brain, planner, memory, tool management, permissions,
  logging, configuration, and constants.
- Added built-in calculator, filesystem, notes, and system tools.
- Added TOML configuration, starter prompt, SQLite interactions and notes
  schema, and documentation.
- Stopped tracking generated runtime database and log files, and added ignore
  rules for SQLite sidecars, logs, caches, local environments, and temporary
  CLI artifacts.
- Added explicit persistent memory storage and the strict `remember`, `recall`,
  and `memories` CLI commands.
- Added an additive, idempotent `memories` SQLite schema that preserves existing
  interactions and notes.
- Made case-insensitive exact duplicates idempotent while preserving the
  original memory ID, fact casing, and creation timestamp.
- Added literal, case-insensitive substring retrieval with deterministic
  newest-first ordering.
- Integrated the memory tool through the normal Planner, permission, Tool
  Manager, and Memory architecture.
- Added focused automated tests for persistence, Planner parsing, memory-tool
  behavior, and Brain execution flow.
- Hardened calculator evaluation with bounded intermediate and final values,
  predictive oversized integer multiplication and power checks, and controlled
  rejection of booleans, complex numbers, and non-finite floats.
- Added focused calculator regression tests for supported boundary cases and
  oversized or unsupported numeric inputs.
- Made permission configuration fail closed with strict lowercase
  `tool.action` array validation and duplicate rejection.
- Enforced mandatory confirmation for filesystem rename and move, mandatory
  denial for filesystem delete and write, and added focused configuration and
  permission regression tests.
- Added controlled, sanitized CLI startup failure messages and process
  statuses: `1` for ordinary startup failures and `130` for startup Ctrl+C.
- Made confirmation-prompt EOF and Ctrl+C safely cancel pending actions, and
  added focused CLI and module-entrypoint regression tests.
- Added broad standard-library v0.1 regression suites for Logger, Tool Manager,
  notes, filesystem, system information, Brain, Planner, and clean startup
  integration, using only temporary runtime resources and preserving existing
  production behavior.
- Expanded documentation of persistent request/response interaction history,
  separate notes, explicit memories, and operational logs; content that may be
  retained or duplicated; local-by-default configurable storage; the absence
  of application-level encryption, automatic pruning, and in-app deletion
  controls; the shipped runtime's lack of built-in network upload; and
  sensitive-content guidance. Runtime behavior is unchanged.
