# Changelog

## Unreleased

- Added a typed, provider-independent local reasoning interface with typed
  failure classes and fail-closed provider configuration, disabled by
  default, with machine-specific settings confined to an untracked local
  override. A new `core/reasoning.py` defines frozen, bounded request and
  response values with no field for personal context, a provider protocol for
  the later local adapter, and one boundary that returns every provider
  outcome as either inert reply text or a typed failure: unavailable,
  timeout, refused, malformed output, truncated output, or input or output
  over budget. It contains no transport and no model, imports no network,
  threading, asynchronous, or subprocess module, and nothing in Maria calls
  it yet, so every command behaves exactly as before. The shipped
  `config/config.toml` gains a `[reasoning]` template that must stay exactly
  `enabled = false`. Active settings — a `loopback` or `private_lan`
  destination class, one IP address inside it, a model identifier, a hard
  deadline of 1 to 300 seconds, and input and output budgets of 1 to
  1,048,576 bytes — belong only in the private `config/config.local.toml`,
  which is now Git-ignored, which Maria never creates, and which only
  startup reads. When absent, reasoning stays disabled. When present, it must
  be a regular file holding exactly one `[reasoning]` table, which replaces
  the tracked one completely; a link, including a dangling one, a directory,
  a special entry, invalid TOML, any other section, an unknown or
  credential-shaped key, a wrong type, an out-of-range value, or an enabled
  block missing any field now stops startup with the usual sanitized
  failure message instead of being silently accepted. No refusal, log line,
  or representation carries the file's text, its location, the host, or the
  model identifier. No schema, permission, command, or version change was
  introduced.
- Gave Maria's database a schema version of its own, independent of the
  application version, and put an ordered, idempotent, atomic forward
  migration runner behind it. The version lives in the SQLite header rather
  than in a new table, so nothing was added to what the database stores, and
  it is a plain counter over Maria's schema history rather than a release
  number: it is never `APP_VERSION` and is never derived from it, because the
  application version stays frozen while the schema may advance. Startup now
  settles that version once, in the memory layer, and everything else follows
  from it instead of each call site assuming what it is looking at.
  An existing database with no marker keeps working and is classified
  explicitly rather than assumed to be current. Neither a missing marker nor a
  present one is taken as proof of what a database is: an unversioned file is
  identified by its structure, and a stamped file has its structure confirmed
  against the frozen schema for the version it claims before any migration
  statement runs, because a marker records which build wrote a database rather
  than what the database currently is. A database matching the published
  v0.2.0 shape is stamped as that baseline and nothing else runs; the earlier
  shape Maria created before explicit memories existed is stamped as the
  version it is and then migrated forward; a database already at the current
  version is validated read-only and left byte for byte unchanged. Every owner
  record, id, and timestamp is preserved either way.
  What a database is compared against is a fingerprint of everything it
  declares, not of the parts that happened to be easy to read. Every
  user-defined object counts — tables, explicit indexes, views, and triggers —
  so the exact baseline plus one unrelated view or trigger is another
  application's database and is refused. For each object, SQLite's own
  catalogue supplies the object type, column count, `WITHOUT ROWID` and
  `STRICT` flags, every column including the generated and hidden ones
  `table_info` omits, each index's uniqueness, origin, partial flag, key
  columns, sort direction and collation, and the full identity of every
  foreign key.
  What SQLite reports nowhere — a conflict clause, a collation on a column no
  index covers, a table option, an `AUTOINCREMENT` clause, a `CHECK`
  condition, a trigger body — comes from the statement that created the
  object, reduced to typed tokens rather than compared as text, raw or
  whitespace-stripped. Each token keeps what kind of thing it is, so
  whitespace, line breaks, comments, casing, and the choice of identifier
  quoting all stop mattering while things that merely read alike stay apart: a
  numeric `0` is not the string `'0'`, and a constraint named
  `"autoincrement"` is not the `AUTOINCREMENT` clause, which is now recognised
  only where SQLite defines it, immediately after `PRIMARY KEY`. `CHECK`
  conditions are lifted out and compared separately, so one written beside its
  column and the same one written for the table compare alike, and parentheses
  that merely wrap a condition are dropped. A weakened, retargeted, extra, or
  missing check is refused, and a check that exists only inside a comment or a
  string literal is not a check. A condition rewritten into a different but
  equivalent form is refused rather than guessed at.
  A file is treated as a new database only when it holds no object SQLite did
  not create itself, rather than merely no table Maria would recognise, so
  another application's database is never mistaken for a blank one to build
  inside.
  Only SQLite's own reserved objects are ignored, including a `sqlite_sequence`
  that has outlived the table that created it. A brand-new or genuinely empty
  file is created directly at the current schema in one step and is never
  mistaken for a migrated owner database; a file holding even one unrelated
  object is refused with no Maria table created, no marker stamped, and not
  one byte changed.
  Migrations are forward-only and validated as a set before the database is
  opened, so a gap, a repeat, or an unknown step is refused rather than found
  part-way up. Each step runs in one explicit transaction together with the
  version marker it writes, so a failure part-way through leaves the database
  whole at the previous version with no partial schema, and re-running startup
  resumes at exactly the step that failed and applies it once. Re-running
  startup on a current database now does nothing at all and leaves the file
  byte for byte unchanged.
  A database written by a newer version of Maria is refused rather than half
  worked against. The refusal happens before any write, runs no migration,
  creates no schema, and leaves the file and its directory byte for byte as
  they were found. It raises a typed failure whose owner-visible sentence is
  fixed and interpolates nothing, and the command line prints that sentence
  instead of the general startup message, so the owner is told what is wrong
  without any path, record, SQL, or SQLite error text travelling with it. A
  corrupt, locked, or unreadable database is never reported as a newer schema
  and keeps its existing controlled result.
  Nothing the initialization boundary writes to the operational log is derived
  from what went wrong. A successful run records the schema version it reached
  and each migration's step name and resulting version; a refusal records one
  fixed sentence carrying a stable event code and nothing else. No row value,
  note title or body, memory fact, interaction request or response, database
  path, SQL statement or parameter, SQLite message, or traceback is written on
  any path, including a corrupt or unreadable file and a database another
  process is holding, either of which previously produced an exception log
  carrying SQLite's own text and a traceback. The underlying exception is
  still attached to the raised error for a caller that needs it; it is simply
  never written down. Reading the schema version is recorded the same way, and
  both boundaries classify contention themselves rather than inheriting the
  shared conversion that logs on its way past.
- Replaced Maria's single configured safe directory with named filesystem
  roots, and put the things Maria must never reach behind one shared,
  permanent boundary. `config/config.toml` now declares roots in a
  `[filesystem]` section, and the shipped declaration is exactly two: an
  enabled `workspace` root at `~/MariaWorkspace` that may be organized, and a
  disabled, read-only `project_maria` root at Maria's repository root, which is
  reserved for a later capability and is inert today. A relative path resolves
  against the default enabled root, an absolute path is accepted only inside an
  enabled root, a `~` prefix expands first, and the current working directory is
  never consulted. A path beneath a disabled root is refused exactly like a path
  outside Maria's roots. A read-only root permits reading and listing only, and
  a rename or move whose source and destination fall in different roots is
  refused even when both roots may be organized. The roots themselves are a
  sealed, tuple-backed value model that cannot be mutated, extended, or
  subclassed after construction, and each root's canonical path is settled
  inside its constructor, so a non-canonical root path never exists. The whole
  `[filesystem]` section fails closed: a missing or malformed section, an
  unknown key, a missing or wrongly typed field, an unknown policy, a repeated
  name, a repeated or nested canonical path, a missing, unknown, or disabled
  default, a root path that is an ordinary file, a root name Maria has no
  semantics for, or a production root whose path, policy, or enabled flag is
  wrong are all refused rather than repaired. Configuration loading creates
  nothing; one controlled startup boundary creates a missing enabled organizing
  root, never creates a read-only or disabled one, and refuses a root inside an
  excluded location.
  Maria's live database file and its SQLite journal, write-ahead-log, and
  shared-memory sidecars, its live log directory and every rotation beneath it,
  and its active system prompt are now permanently unreachable by every
  filesystem capability, wherever configuration puts them. That is deliberately
  stronger than protecting the shipped layout: all three locations are
  configurable, so a valid configuration could otherwise place the live database,
  the live log, or the prompt inside an enabled workspace and have the generic
  filesystem commands hand them straight back out. The active locations are
  settled once when configuration loads and handed to both root preparation and
  the filesystem tool, so the two can never disagree about what is protected.
  The default `database/` and `logs/` directories, `.git` — as a directory or as
  a gitdir-pointer file — and the reserved, never-loaded
  `config/config.local.toml` remain unreachable as well, as do repository-scoped
  credential files, cache and environment directories, local smoke-test
  artifacts, assistant working state, and `.tmp` and `.bak` artifacts at any
  depth, with one exact exemption for an `.env.example` template as a final path
  component. Those name rules are scoped to Maria's own repository, so an owner's
  workspace may still hold `env`, `venv`, or `.claude` directories and `.tmp` or
  `.bak` files and use them normally. Separately, Maria's whole repository —
  tracked source, documentation, prompts, the shipped configuration, and
  untracked files beside them — is now unavailable to the generic read, list,
  rename, and move commands, and declaring a root around the repository does not
  defeat that.
  Every one of these rules is applied to two identities of a path: the spelling
  that was asked for and the canonical target it resolves to. Checking only the
  target would have let a protected name such as `Maria/logs` be reached by
  making it a junction pointing out of the repository, and checking only the
  spelling would have let an ordinary name be a link pointing in. Neither
  direction gets through now, and which root contains a path is still decided by
  the canonical target alone. Neither identity is ever repeated into a response,
  a log record, or result data.
  Renaming and moving now ask a wider question than reading does, because
  relocating a directory carries everything beneath it. A directory that merely
  contains the live database, one of its SQLite sidecars, or the system prompt
  cannot be renamed or moved, and neither can an ancestor of the live log
  directory; previously such a directory could be relocated, which left the
  protected files reachable at their new location because protection still
  pointed at the old one. The same widening applies to Maria's repository, so a
  broad root can no longer relocate the checkout by naming its parent. Reading
  and listing such a directory are deliberately unchanged: the parent still
  lists successfully with its protected children filtered out, and an ordinary
  directory with nothing protected inside it still renames and moves normally.
  The check covers both identities of both operands and runs before the policy
  check, the same-root check, and every existence check, so nothing is created,
  renamed, or moved on the way to refusing.
  A database sidecar is now protected by its canonical target as well as by its
  name. When a sidecar name already resolves elsewhere through a link, the
  location it points at is settled alongside it, so the content can no longer be
  reached under an alternate name. Each protected identity — the database, its
  sidecars, and the prompt — now also covers everything beneath it, so a name that
  turns out to be a directory rather than a file cannot hand its contents out one
  level down; under the ordinary file shape this changes nothing, because a file
  has no descendants. Ancestors are deliberately still not protected, so unrelated
  neighbours such as `maria.db.notes` stay usable and the directory holding the
  database is still listable with its protected children filtered out.
  Copying, deep-copying, and unpickling a settled protection value now preserve
  that value exactly instead of re-deriving it from the filesystem as it looks at
  the time. Previously a duplicate could come back describing different locations
  if a link had been repointed in between, which broke the value-object guarantee
  that copies compare and hash equal. Construction still settles the live
  filesystem once, and `_make` and `_replace` are still construction, so they
  continue to recompute the derived sidecar identities and continue to refuse a
  forged one.
  Root preparation now runs before logging is configured rather than after,
  because configuring logging creates the active log directory: a root
  declaration that names a protected runtime location has to be refused before
  anything is created. It still runs before the memory layer opens the database,
  and a refusal is still reported as the existing sanitized startup failure.
  Listing now settles every child before describing it: a child that is
  conclusively outside its root, permanently excluded, or inside the repository
  is omitted without ever being classified, while the parent listing still
  succeeds and its ordinary siblings stay visible. A directory whose every child
  was filtered still reports `Directory is empty.` An unexpected error while
  resolving or classifying a child is not hidden: the whole listing fails
  through the existing sanitized message instead.
  Three fixed refusals were added — `Path is permanently excluded from Maria's
  filesystem access.`, `This filesystem root is read-only.`, and `Source and
  destination must be inside the same filesystem root.` — and the existing
  outside-root message and the four action prefixes are unchanged. None of them
  interpolates anything, so no response reveals the submitted path, the owner's
  home or repository location, a root name, an excluded filename, raw operating
  system text, or an object repr. The four filesystem actions, the command
  grammar, the 128 KiB read limit, replacement decoding, the result-data and
  display-path contracts, and the deliberate log-versus-response asymmetry are
  all unchanged, the action-contract inventory remains exactly 22 actions with
  no write or delete contract, and the mandatory permission sets are untouched.
  No schema or version change was introduced.
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
- Introduced shared typed failures with a privacy-safe logging discipline
  and bounded the operational log's growth. A new `core/failures.py`
  declares a closed set of failure kinds and one immutable value object
  that carries a kind and nothing else: there is no field for a request, a
  response, an arguments mapping, an exception, its text, a path, an
  endpoint, a credential, or a header, and each kind's owner-visible
  sentence is fixed rather than interpolated, so no boundary can attach a
  private value by accident. `ToolManager` now uses it for an unexpected
  exception raised by a tool, which changes one user-visible string:
  `Tool execution failed: <raw exception text>` becomes `Tool execution
  failed.`. The matching operational record changed with it, from a
  formatted traceback to the validated tool name, the validated action
  name, and the typed failure code alone; the exception is no longer even
  bound to a name, so nothing on that path can format it into a response or
  a log. Maria's managed log handler now rotates instead of growing without
  limit, keeping at most a 1 MiB active `maria.log` plus three previous
  files, with no configuration field, CLI option, or environment variable
  to widen it; encoding, format, file name, level, propagation, and the
  existing handler-ownership rules are unchanged. There is deliberately no
  global redaction, scrubbing, or filtering layer, because the filesystem
  tool's operational log is deliberately tested to retain the absolute root
  path and filenames its sanitized response omits, and a blanket filter
  would break that contract while giving false confidence elsewhere; the
  discipline is instead that a module never hands a private value to the
  logger. Every tool keeps its own controlled error messages and specific
  diagnostics, and every other user-visible string, permission decision,
  and recorded interaction row is unchanged. No schema, permission, or
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
