# Maria — Instructions for Claude

You are acting as a Senior Python Software Engineer and Software Architect on **Project AI:
Maria**, a long-term, modular personal AI assistant. It is a real software product, not a
prototype. Read the existing code before writing new code.

`v0.1.0` is the immutable Foundation release baseline — do not assume it is still the
current development version. Verify the actual repository state first: the active branch
(`git branch --show-current`), the `## Unreleased` section of `Maria/CHANGELOG.md`, and
`git status`.

## Repository Layout

**Every path in this document is relative to the repository root.** The tracked project
content at the root is `README.md`, `CLAUDE.md`, and the `Maria/` application directory;
other entries (tooling, configuration, or future additions) may also be present, so check
rather than assume. **All application code lives in `Maria/`.**

| Path | Contents |
|---|---|
| `Maria/main.py` | CLI entry point |
| `Maria/core/` | `brain`, `planner`, `memory`, `permissions`, `tool_manager`, `logger`, `config`, `constants` |
| `Maria/tools/` | `calculator`, `filesystem`, `memory`, `notes`, `system` |
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
- `MANDATORY_CONFIRMATION_ACTIONS` — `filesystem.rename` and `filesystem.move`

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

Configuration *validation* is the part that fails closed. The decision function is not.

## Persistence

Interaction history (cleaned requests plus full response text), notes, and explicit
memories are three separate SQLite record types. Maria records most requests and their
complete responses, and provides no forget, prune, or delete path. Be careful not to leak
sensitive content into any of them when adding features. See `Maria/docs/architecture.md`
("Persistence Models"), `Maria/docs/database.md`, and the "Data Retention and Privacy"
section of `Maria/README.md`.

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
- `Maria/core/brain.py` — coordinates planner, memory, permissions, and tool manager.
  **Never** put direct database access or tool-specific logic here. Dispatch must stay
  generic, with no per-tool branch.
- `Maria/core/planner.py` — turns user input into structured `Plan` and `PlanStep` objects,
  and owns the no-action response copy and the help text.
- `Maria/core/memory.py` — the **only production application module** that may import
  `sqlite3` or execute SQL. Guard tests enforce this; keep it true. (Tests may connect to a
  temporary test database directly when necessary — never to the live database.)
- `Maria/core/permissions.py` — decides `ALLOWED` / `REQUIRES_CONFIRMATION` / `DENIED`.
- `Maria/core/tool_manager.py` — discovers, loads, and dispatches tools.
- `Maria/core/logger.py` — application logging only.
- `Maria/core/config.py` — loads and validates `Maria/config/config.toml`.
- `Maria/core/constants.py` — shared identity and canonical paths (`APP_NAME`,
  `APP_VERSION`, `PROJECT_ROOT`, `CONFIG_PATH`, `DATABASE_PATH`, `LOG_DIR`,
  `DEFAULT_LOG_FILE`, `SYSTEM_PROMPT_PATH`, `DEFAULT_TEXT_READ_LIMIT_BYTES`). Reuse these
  rather than hardcoding a path.
- `Maria/tools/*.py` — one tool per file, thin adapters, no direct SQLite access; use the
  injected `Memory` object.

New code should extend the correct module, not add logic to the wrong layer because it is
faster.

## Tool Integration Contract

Adding a tool requires all four steps. Skipping any one leaves the tool unreachable:

1. Expose a module-level factory `build_tool(config=..., memory=..., logger=...)` returning
   an object that satisfies the `Tool` Protocol in `Maria/core/tool_manager.py`: `name`,
   `description`, `actions: frozenset[str]`, and
   `execute(action, arguments) -> ToolExecutionResult`.
2. Register the module in `BUILT_IN_TOOL_MODULES` in `Maria/core/tool_manager.py`, or it is
   never imported.
3. Add a routing branch in `Maria/core/planner.py`, or no user input can reach it.
4. Assess every new action against the permission model above, and add explicit denied or
   confirmation-required entries where the action is sensitive.

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

- The `v0.1.0` tag is immutable. Never retag, amend, force-push, or rewrite published
  history.
- It is an **annotated** tag, so `git rev-parse v0.1.0` returns the tag object rather than
  the commit. Use `git rev-parse "v0.1.0^{commit}"` or `git rev-list -n 1 v0.1.0`.
- Do not `add`, `commit`, `push`, or `tag` without explicit user permission.
- This repository is public. Never commit secrets, credentials, personal email addresses,
  real names, or absolute machine paths.

## Working Style

- Do not guess on a large architectural decision. Explain the uncertainty, present one or
  two options, and wait for approval. If you see a better implementation that still respects
  these principles, explain your reasoning before applying it.
- Prefer small, reviewable changes over large speculative rewrites, and optimize for
  long-term maintainability over short-term convenience.
- Report honestly: if a step was skipped or a test failed, say so plainly.
