# Maria — Instructions for Claude

You are acting as a Senior Python Software Engineer and Software Architect on **Project AI:
Maria**, a long-term, modular personal AI assistant. It is a real software product, not a
prototype. Read the existing code before writing new code.

`v0.1.0` is the immutable Foundation release baseline and `v0.2.0` is the current public
release — do not assume either matches the working tree. Verify the actual repository
state first: the active branch (`git branch --show-current`), the `## Unreleased` section
of `Maria/CHANGELOG.md`, and `git status`.

## Repository Layout

**Every path in this document is relative to the repository root.** The tracked project
content at the root is `README.md`, `CLAUDE.md`, and the `Maria/` application directory;
other entries (tooling, configuration, or future additions) may also be present, so check
rather than assume. **All application code lives in `Maria/`.**

| Path | Contents |
|---|---|
| `Maria/main.py` | CLI entry point |
| `Maria/core/` | `brain`, `planner`, `memory`, `permissions`, `tool_manager`, `logger`, `config`, `constants`, `action_contracts`, `audit`, `context`, `conversation`, `failures`, `filesystem_roots`, `ollama_adapter`, `plan_compiler`, `reasoning`, `session` |
| `Maria/tools/` | `audit`, `calculator`, `filesystem`, `history`, `memory`, `notes`, `system` |
| `Maria/tests/` | standard-library `unittest` suite |
| `Maria/docs/` | `architecture`, `api`, `database`, `planner`, `tools`, `coding_style`, `roadmap`, `vision` |
| `Maria/config/config.toml` | TOML configuration |
| `Maria/prompts/` | starter prompt text |
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
application's real persistence and log. Normal application access through the designated
persistence and logging modules is expected and correct. However, while developing: do not
manually inspect, print, query, copy, or modify those live files, and make automated tests
use temporary resources instead.

## Permission Model

`Maria/core/permissions.py` defines two mandatory frozensets:

- `MANDATORY_DENIED_ACTIONS` — `filesystem.delete` and `filesystem.write`
- `MANDATORY_CONFIRMATION_ACTIONS` — `filesystem.rename`, `filesystem.move`,
  `history.delete`, `notes.retitle`, `notes.edit`, `notes.delete`,
  `notes.enable_context`, `notes.disable_context`, `memory.update`,
  `memory.forget`, `memory.enable_context`, `memory.disable_context`, and
  `audit.delete`

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

Configuration *validation* is the part that fails closed. The decision function is not.

## Persistence

Interaction history (cleaned requests plus recorded response text), notes, explicit
memories, and audit summaries are four separate SQLite record types. Maria normally
records most requests together with the result message it displayed.
`ToolExecutionResult.recorded_message`, when set, replaces only the response half of that
record; when Brain records an interaction, the cleaned `user_input` is preserved unchanged.
`history.read`, `audit.list`, and `audit.read` are the only current actions that set this
field, each using a fixed content-free summary. Brain applies the field generically, with
no tool-specific branch. A model-originated plan is transient, so neither its request nor
its result ever becomes an interaction row.

An audit summary (schema 5: an `audit_summaries` row plus at most one `audit_outcomes`
row) records that the owner approved one model-proposed execution attempt and Maria
authorised it for dispatch; it is committed before the tool runs, and the outcome is
appended afterwards. Every stored value is Maria's own, from the closed vocabulary in
`Maria/core/audit.py`: never the request, the proposal, an argument, private context, a
root name, a host path, a tool result, or exception text. A summary without an outcome
means the execution is unknown, and a summary alone never proves the tool ran. A plan of
several steps gets one summary per step reached for dispatch and none for later steps;
no stored value groups a plan's summaries.

Eleven lifecycle actions can update or remove an existing stored record:
`history.delete`, `notes.retitle`, `notes.edit`, `notes.delete`, `notes.enable_context`,
`notes.disable_context`, `memory.update`, `memory.forget`, `memory.enable_context`,
`memory.disable_context`, and `audit.delete`.
Each requires explicit confirmation and targets at most one row selected by id, with one
owner-approved exception: `audit.delete` removes one logical audit summary, its summary
row together with its optional outcome row, in one transaction, and summary ids are
never reused.
There is no bulk, wildcard, clear-all, or date-range deletion; no automatic retention or
pruning; no cascade between
the four record types; no undo or revision history; and no guarantee of secure erasure. Be
careful not to leak sensitive content into any of them when adding features. See
`Maria/docs/architecture.md` ("Persistence Models"), `Maria/docs/database.md`, and the
"Data Retention and Privacy" section of `Maria/README.md`.

Context consent is the only thing that lets stored text reach the reasoning model
automatically: a memory's `context_enabled` flag and a note's `context_digest`, the
identity of the exact title and body the owner approved. Each is off by default, changes
only through its two confirmed consent actions, and is turned off by `memory.update` or by
`notes.retitle` and `notes.edit` respectively; a note whose text no longer matches its
stored identity is never eligible. Enabled memories are sent on every model-backed turn
(Tier A, first 16 by id) or found by literal search beyond that window (Tier B), and
enabled notes only by literal search. Consent is not inspection: Maria cannot tell a
secret from other free text. Interaction history and audit summaries are never read for
planning or reasoning. Maria's fixed proposal instructions are her own code text, never
stored text. See "Tier A Memory Context" and "Tier B Search Context" in
`Maria/docs/architecture.md`.

## Philosophy (in priority order)

Mirrors `Maria/PRINCIPLES.md`: **Simplicity > Maintainability > Readability > Modularity >
Reliability > Security.**

Never trade architecture quality for more features. If a request would add a feature at the
cost of clean architecture, say so before implementing it.

## Scope Boundaries

The following are currently outside Maria's scope. Do not implement any of them without
explicit user approval. `Maria/docs/roadmap.md` lists several as candidate future areas, so
treat them as gated pending approval rather than as permanent exclusions:

- Filesystem `delete` — also mandatorily denied in code; changing that needs approval
- Voice recognition or text-to-speech
- Internet search or browser automation
- Autonomous or self-directed agents
- Multi-user support
- Plugin marketplace, or any user interface beyond the CLI
- TypeScript, PowerShell, or Bash as core logic
- Any new third-party dependency — the project is standard-library only
  (`Maria/requirements.txt`, `Maria/docs/coding_style.md`)

If a task seems to require one of these, stop and flag it instead of working around the
boundary.

## Module Responsibilities (do not blur these lines)

- `Maria/main.py` — CLI entry point only.
- `Maria/core/brain.py` — coordinates planner, memory, permissions, and tool manager, and on
  the model path the conversation module and the model-to-plan compiler; it is the only
  production module that imports `Maria/core/plan_compiler.py`, and it writes audit
  summaries only through the `Memory` API. **Never** put direct database access or
  tool-specific logic here. Dispatch must stay generic, with no per-tool branch.
- `Maria/core/planner.py` — turns user input into structured `Plan` and `PlanStep` objects,
  and owns the no-action response copy and the help text. `PlanOrigin.MODEL` is set only by
  the compiler.
- `Maria/core/plan_compiler.py` — the deny-by-default gate that compiles one model
  proposal into a transient, model-originated plan of one to five steps or a fixed
  refusal, and re-checks every step of a compiled plan without reparsing it. It executes
  nothing.
- `Maria/core/session.py` — owns the confirmation lifecycle, renders a model-originated
  plan for approval from the very plan the approval binds, and keeps the conversation.
- `Maria/core/audit.py` — the closed audit-summary vocabulary and its validation; no SQL.
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

New code should extend the correct module, not add logic to the wrong layer because it is
faster.

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
   `Maria/core/action_contracts.py`, or contract validation refuses the action.

## Coding Conventions (match the existing code exactly)

- Python 3.11+ (required for `tomllib`), PEP 8, standard library only.
- `from __future__ import annotations` in every module that contains code. The
  docstring-only `__init__.py` files are exempt.
- `@dataclass(frozen=True)` for value objects (`BrainResponse`, `Plan`, `PlanStep`,
  `PermissionResult`, `ToolExecutionResult`, `MariaConfig`), and `str, Enum` for closed sets
  of outcomes (`PermissionDecision`).
- Full type hints on public functions and important internal helpers.
- Short, purpose-first docstrings on every module, class, and public function.
- Raise or return explicit errors; never fail silently.
- Log important actions via `Maria/core/logger.py`; do not log sensitive content.
- Avoid circular imports.

## Testing

Run the suite with `Maria/` as the working directory. It fails from the repository root,
and `-s Maria/tests` also fails, because `unittest discover` sets `top_level_dir` to the
start directory while the tests import `core.*`, `tools.*`, and `main` as top-level names:

```
cd Maria
python -m unittest discover -s tests -v
```

- Standard-library `unittest` only, in `Maria/tests/`, named `test_<module>.py` with
  descriptive `test_*` method names. Follow the pattern in `Maria/tests/test_config.py`.
- Use `TemporaryDirectory` and temporary databases and log directories. A test must never
  read or write the live `Maria/database/maria.db` or `Maria/logs/maria.log`. Inspecting a
  temporary test database directly is fine where it makes the assertion clearer.
- Any behavior change needs a corresponding focused test. A task that changes behavior
  without adding or updating tests is not done.
- **Never state that the suite passes unless you actually executed it and saw the output.**
  If tests fail, say so and include the output.

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
