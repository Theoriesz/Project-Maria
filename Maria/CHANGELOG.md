# Changelog

## Unreleased

- Hardened calculator evaluation with bounded intermediate and final values,
  predictive oversized integer multiplication and power checks, and controlled
  rejection of booleans, complex numbers, and non-finite floats.
- Added focused calculator regression tests for supported boundary cases and
  oversized or unsupported numeric inputs.
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

## 0.1.0

- Created the initial Maria project foundation.
- Added core modules for brain, planner, memory, tool management, permissions,
  logging, configuration, and constants.
- Added built-in calculator, filesystem, notes, and system tools.
- Added TOML configuration, starter prompt, SQLite schema, and documentation.
