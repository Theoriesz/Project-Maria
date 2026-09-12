# Roadmap

## Status

- **v0.1.2** is the current released baseline.
- Stages 1, 2, 3, and 4 (interaction-history browsing, single-interaction
  deletion, note lifecycle management, and explicit-memory lifecycle
  management) are implemented in the current `Unreleased` development state for
  v0.2.0.
- `APP_VERSION` (`core/constants.py`) and the shipped `config/config.toml` version
  remain `0.1.2` throughout v0.2.0 feature development.
- `0.2.0` is assigned only during the separate `release/v0.2.0` preparation stage
  (Stage 6), after every feature branch below has merged.

## v0.2.0 — Personal Data Control

### Release promise

Maria v0.2.0 gives the owner transparent, positive-numeric-ID-based control over three
separate record types:

- **Interaction history** gains list, search, read-by-id, and single-row deletion.
  Interaction history is never edited, because it is a historical record.
- **Notes** gain separate title editing, body editing, and single-row deletion.
- **Explicit memories** gain fact editing and single-row forgetting.
- Read operations require no confirmation.
- Every persistent update or deletion requires mandatory explicit confirmation that
  configuration can extend but never remove.

All new record IDs referenced by v0.2.0 commands are **positive decimal integers with a
minimum value of 1**.

## Planned Development Order

### Step 0 — Roadmap definition

- Branch: `docs/define-v0.2.0-data-control`
- Documentation only. No runtime behavior.

### Stage 1 — Interaction-history browsing

- Branch: `feature/interaction-history-browsing`
- Commands:
  - `history`
  - `search history <query>`
  - `read interaction <id>`
- `HistoryTool` actions during this stage are **exactly** `list`, `search`, and `read`.
  `delete` is **not** an available Stage 1 action.
- IDs are positive decimal integers with a minimum value of 1.
- `history` and `search history` show newest-first results with a default visible limit
  of 20 and a limit-plus-one more-results indication, matching the existing
  notes/memories convention.
- `read interaction` displays the full stored request, response, and timestamp.
- Stage 1 introduces a generic optional `recorded_message` contract on
  `ToolExecutionResult` so displayed response text may differ from the response
  persisted in interaction history.
- Brain must consume that field generically and must never branch on the history tool
  name or action.
- `read interaction` persists only a fixed, content-free audit summary rather than
  recursively storing the historical content it displayed.
- Stage 1 has no schema change and no mandatory-confirmation change.

### Stage 2 — Single-interaction deletion

- Branch: `feature/interaction-history-deletion`
- Command: `delete interaction <id>`
- Action: `history.delete`
- Stage 2 adds `delete` to `HistoryTool`; it does not exist during Stage 1.
- Positive numeric ID only. Exactly one row per command.
- Mandatory confirmation. No preview of stored content during confirmation.
- No bulk, wildcard, date-range, cascading, or inferred deletion.
- The selected interaction row is removed, but Maria records a new short audit
  interaction for the deletion command itself. The total row count may therefore remain
  unchanged even though the selected historical content is gone.

### Stage 3 — Note lifecycle management

- Branch: `feature/note-lifecycle-management`
- Commands:
  - `retitle note <id>: <new title>`
  - `edit note <id>: <new body>`
  - `delete note <id>`
- Actions: `notes.retitle`, `notes.edit`, `notes.delete`
- Positive numeric IDs only.
- Retitle and edit are deliberately separate commands to avoid accidental overwrites.
- `rename note` is not used, because it collides with the existing filesystem `rename`
  command prefix.
- A successful title or body edit preserves `created_at` and refreshes `updated_at`.
- Duplicate note titles remain allowed.
- Every action requires mandatory confirmation.
- No schema change.

### Stage 4 — Explicit-memory lifecycle management

- Branch: `feature/memory-lifecycle-management`
- Commands:
  - `update memory <id>: <new fact>`
  - `forget memory <id>`
- Actions: `memory.update`, `memory.forget`
- Positive numeric IDs only.
- A successful update preserves `created_at`. `memories.updated_at` is **not** added in
  v0.2.0, so editing a memory does not move it to the top of newest-first results.
- Casefold-based fact uniqueness is preserved; conflicts are handled through controlled
  results, not exceptions surfaced to the user.
- Both actions require mandatory confirmation.
- No schema change.

### Stage 5 — Read-only cross-feature and release-planning audit

- Runs only after all four feature PRs (Stages 1–4) are merged into a clean `main`.
- This stage has no branch or PR of its own unless the audit discovers a correction.
- Any discovered correction receives a separate, focused fix branch.
- Functional fixes are not placed into `release/v0.2.0`.

### Stage 6 — Release preparation

- Branch: `release/v0.2.0`
- Version bump, changelog finalization, tests, compilation, PR, verified merge commit,
  annotated tag, and GitHub Release.
- The `v0.2.0` tag is created only after the release-preparation PR is merged and the
  resulting `main` merge commit is independently verified.

## Permission Baseline

Existing mandatory-confirmation actions (unchanged): `filesystem.rename`,
`filesystem.move`.

Six new mandatory-confirmation actions are planned for v0.2.0: `history.delete`,
`notes.retitle`, `notes.edit`, `notes.delete`, `memory.update`, `memory.forget`.

The final mandatory-confirmation baseline is therefore **eight actions**, not nine.
Configuration may add further restrictions on top of this baseline but may never remove
any part of it.

Read-only `history.list`, `history.search`, and `history.read` remain allowed and do not
need entries in the mandatory confirmation set.

## Search Policy

Planned v0.2.0 history search is:

- literal substring search across stored `user_input` and `response`;
- newest-first;
- implemented using SQLite-side `LOWER()` comparison, to avoid a schema change;
- **not** advertised as full Unicode casefold equivalence (memories' search, which uses
  a precomputed casefolded column, remains the more precise comparison);
- not fuzzy, semantic, ranked, or AI-based.

No character truncation is added to history list/search previews in v0.2.0. That can be
reconsidered from real usage later.

## Retention and Deletion Boundaries

- Interactions, notes, and explicit memories are separate record types.
- Deleting one record never cascades into another table.
- Deleting a note or forgetting a memory does not erase copies of that content that may
  already exist in interaction history.
- Users may separately delete the relevant interaction rows by numeric ID.
- v0.2.0 does not promise secure erasure from filesystem backups, Git history, database
  backups, or external copies.
- No bulk deletion or automatic retention exists in v0.2.0.

## Architecture Requirements

- Brain remains generic.
- Memory remains the only production module that owns SQL/`sqlite3`.
- Planner owns deterministic command parsing.
- Every user-facing action follows
  `Planner -> PermissionManager -> ToolManager -> tool`.
- Tools remain thin adapters over public `Memory` methods.
- All six new persistent mutations are mandatory-confirmation actions.
- No schema migration is required.
- Standard library only.
- No WAL, background service, production thread, networking, asynchronous behavior, UI,
  AI model, agentic behavior, or third-party dependency.

## Deferred from v0.2.0

The following are gated future candidates, not commitments, and are not part of v0.2.0:

- bulk clear/delete
- automatic retention or pruning
- date-range deletion
- export/import
- backup and restore
- encryption at rest
- pagination tokens
- filesystem delete
- UI and voice
- AI-model integration
- internet and external services
- tasks and project management
- autonomous behavior
- self-development
- Claude Code skills
- third-party dependencies

## Completion Criteria

v0.2.0 is complete when:

- all planned read and lifecycle commands are implemented;
- ID validation accepts positive integers only, with a minimum value of 1;
- mandatory confirmation covers exactly the six new mutations;
- no bulk or cascade deletion path exists;
- the generic `recorded_message` design ships with no history-specific Brain branch;
- no SQL exists outside `Memory`;
- no schema migration was required;
- existing v0.1.2 behavior is preserved;
- tests use only temporary database and log resources;
- retention and privacy documentation is accurate for the shipped behavior;
- the full test suite and compilation pass;
- release preparation and the annotated tag are handled separately, with the merge
  commit independently verified before tagging.
