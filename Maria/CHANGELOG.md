# Changelog

## Unreleased

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
