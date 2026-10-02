# Internal API

Maria exposes internal Python interfaces between modules. These are not
public network APIs.

## Brain

`MariaBrain(config, planner, memory, tool_manager, permissions, logger=None,
reasoning: ReasoningProvider | None = None)`

`reasoning` is the owner's reasoning provider, typed only through the
`ReasoningProvider` protocol, or `None` for a deterministic-only Brain. Brain
never imports or names an adapter, and it keeps no state between requests.

`MariaBrain.from_defaults(reasoning_factory: Callable[[ReasoningSettings],
ReasoningProvider | None] | None = None) -> MariaBrain`

Builds the runtime Brain from configuration. When a factory is given it is
called exactly once with the settled `config.reasoning`, before roots are
prepared, logging is configured, or the database is opened, so a provider
that refuses its settings stops startup with nothing created. Its result
becomes `reasoning`. Without a factory, Brain is deterministic only.

`MariaBrain.process(user_input: str, *conversation: ReasoningExchange,
supplied_context: frozenset[ContextFingerprint] = frozenset()) -> BrainResponse`

Plans one request and coordinates it from input to plan, whole-plan permission
resolution, tool execution, and interaction-history recording. A deterministic
plan holding a step that requires confirmation returns before execution, carrying on the
response the plan `ToolManager.prepare_confirmation()` returns for it: the same
plan, unless a tool pinned the exact state a step acts on, in which case the
approval binds to that pinned plan. Any previews that call returns follow the
confirmation request's text, each on its own lines, and go nowhere else. The deterministic planner always runs first. The request is
answered by the local model instead only when all three hold: a reasoning
provider was supplied, `config.reasoning.enabled` is true, and the plan is
exactly the planner's generic no-action fallback. It is then answered in the
context of `conversation`, the caller's earlier exchanges passed positionally,
oldest first, with `supplied_context`, the fingerprints of the memories and
notes supplied since that conversation began (see "Conversation"). Only then
does Brain read context: Tier A through
`Memory.list_context_memories(limit=17, max_fact_bytes=2048)`; the records
`supplied_context` names, only when there are any, through
`Memory.context_memories_by_id(ids, max_fact_bytes=2048)` and
`Memory.context_notes_by_id(ids, max_text_bytes=2048)`; and, only when
`context.search_terms(request)` yields terms, Tier B through
`Memory.search_context_notes(terms, limit=9, max_text_bytes=2048)` and, only
when Tier A's read returned 17 rows, `Memory.search_context_memories(terms,
after_id=<16th id>, limit=9, max_fact_bytes=2048)`. A read that raises
`RuntimeError`, or a Tier A read too malformed to start the search from, is
logged as the fixed line `Unable to read memory context.` and ends every read,
so the turn fails closed. The model may reply conversationally or propose one
action, which Brain compiles and then offers for the owner's approval or
refuses; see "Model-Originated Plans" below. With disabled settings the
fallback keeps its original response and interaction row even if a provider
was supplied, and no context read happens. The planner receives the cleaned
request text alone, and no interaction history is read. `supplied_context` is
the only keyword accepted; no keyword can approve anything.

`BrainResponse(text, needs_confirmation=False, plan=None, conversation=None,
supplied_context=frozenset())`

`conversation` is set by a completed conversational reply, to the session's
conversation as it stands after that reply, and to `()` by a model-backed
turn that had to clear the conversation, whatever that turn then produced,
including a refused proposal or a plan awaiting approval. `supplied_context`
travels with it: the fingerprints of every memory and note supplied since that
conversation was last cleared, at most 64. Every other response, including a
failed or refused conversational turn that cleared nothing and every proposal
turn that cleared nothing, leaves `conversation` `None`, so neither a proposal
nor the fingerprints of what it was given are ever kept.

A model-originated plan awaiting approval comes back with `needs_confirmation`
set, `plan` holding that very plan, and as `text` only Maria's notices about
the turn's context; the session renders the plan above them (see "Session").

`main.build_reasoning_provider(settings: ReasoningSettings) ->
ReasoningProvider | None` and `main.build_runtime_brain() -> MariaBrain`

The runtime composition point. `build_reasoning_provider` returns `None` for
disabled settings and otherwise constructs `OllamaReasoningProvider(settings)`,
whose own `ValueError` refuses settings it cannot honour.
`build_runtime_brain()` is the CLI's default startup and calls
`MariaBrain.from_defaults(reasoning_factory=build_reasoning_provider)`.

`MariaBrain.execute_confirmed_plan(plan: Plan) -> BrainResponse`

Runs a plan whose approval a caller has already bound, resuming the same
contract, permission, dispatch, and recording flow without retrieving context,
invoking Planner again, or asking a reasoning provider anything. It validates
the whole plan against the action contracts
and resolves its permissions again before dispatching anything, so a contract or
a decision that changed since the plan was shown still applies. That recheck is
not replanning: it revalidates and re-resolves the same immutable plan, and a
contract failure there dispatches nothing while the approval the session already
consumed stays spent. There is no raw-text confirmation parameter: an approved
request is never planned a second time. For a model-originated plan the recheck
also repeats the compiler's gates, and the plan runs only behind a committed
audit summary; see "Model-Originated Plans".

Both entry points share one internal helper, so for a deterministic plan
zero-step, denied, allowed, successful, and failed-result behavior is identical
on either path. A model-originated plan is the exception: through `process`
even an allowed one only waits for approval, and it runs only through
`execute_confirmed_plan`. The request half of a recorded interaction is the
plan's `original_request`, which Planner sets to the cleaned input it was given.

### Contract Validation, Permission Resolution, and Execution Phases

A deterministic plan with at least one step runs in three separate phases, and
no phase begins while an earlier one is incomplete. A model-originated plan
takes the path described in "Model-Originated Plans" instead.

Phase zero validates the plan. Brain checks every step in plan order through
`ToolManager.validate_action()` and stops at the first violation. Brain holds
no registry of its own, so this phase and the defensive pass inside dispatch
can only ever consult Tool Manager's one active registry. A violation
anywhere refuses the whole plan before `PermissionManager` is consulted at all:
`check()` is called zero times, no tool runs, no confirmation becomes pending,
and the violation's fixed message is returned and recorded through the ordinary
generic interaction path. Because the allowlist is positive, an identifier that
is mandatorily denied but is not a registered tool action, such as
`filesystem.write`, is refused here as an unknown action and never reaches the
permission layer; a direct `PermissionManager.check("filesystem", "write")`
still returns denied, and the two layers are defence in depth. The pass reads
the original `PlanStep` objects and their immutable arguments, so the plan's
fingerprint is unchanged by it.

Phase one resolves the plan. Brain walks the steps in their original order and
calls `PermissionManager.check(tool_name, action)` once for each, pairing every
step with the `PermissionResult` that came back. It runs no tool, calls no
planner, retrieves no context, and neither rebuilds the plan nor copies a step's
arguments. The resolution is a private, frozen, in-memory value: it is never
persisted, never logged, and never handed to another module, and it reports
which decisions came back without making any of them.

Phase two dispatches. It begins only once validation and resolution have
cleared the whole plan:

- If any step resolved to `denied`, the entire plan is denied, no tool runs, and
  no confirmation becomes pending. The earliest denied step in plan order
  supplies the reason, and the denial is recorded through the normal path. A
  plan whose first step needs confirmation and whose second is denied is denied
  outright rather than offered for approval.
- Otherwise, if any step resolved to `requires_confirmation` and the plan is not
  yet approved, one confirmation-required response is returned carrying the exact
  plan. Nothing executes and nothing is recorded. One approval covers that one
  complete plan and no other.
- Otherwise every step is dispatched in plan order, at most once each, receiving
  the immutable arguments object its `PlanStep` holds rather than a copy.

Stop-on-failure is unchanged: the first unsuccessful `ToolExecutionResult` stops
the plan, later steps do not run, and Maria neither rolls back nor compensates
for steps that already succeeded. There is no transaction, undo, or workflow
persistence across steps. The displayed and recorded text covers exactly the
prefix of steps that ran.

`PermissionManager` is consumed unchanged. Brain coordinates several ordinary
`check()` calls and never reinterprets an individual result, adds an argument,
or asks for a bulk decision, and `PermissionManager` remains unaware of
arguments and contracts. The approval every model-originated plan needs is a
separate requirement of Brain's, not a reinterpretation: a denied decision
still refuses that plan. Planner still emits at most one step for every
command Maria currently understands, so these guarantees are about plans that a
later stage may produce rather than about any command available today.

A confirmation request for a single-step plan keeps its existing wording. A plan
with more than one step is instead rendered as a deterministic, plan-ordered
list naming each step's tool action and the permission outcome it resolved to:

```text
Confirmation required for 2 planned actions:
1. calculator.calculate: allowed
2. filesystem.rename: requires confirmation
```

Neither form repeats an argument value, so no absolute host path can reach the
owner's interface, an interaction row, or a log through it. The rendering
carries no fingerprint and no canonical serialized plan content, and a pending
confirmation is not recorded to interaction history. The one addition is a
tool's preview from `ToolManager.prepare_confirmation()`: owner-visible text,
such as the exact title and body a `notes.enable_context` approval covers,
appended after the request on its own lines. It is shown only; it is not part
of the plan or its fingerprint, and it is never recorded, logged, retained in
the conversation, or sent to a model. With no preview the request is exactly
as before.

These renderings are Brain's, for deterministic plans. A model-originated plan
gets none: Brain returns only the turn's notices, and the session renders the
plan itself, naming its declared tool and action and showing every argument
value exactly (see "Session"). Those values are only what the model proposed,
never a path Maria resolved, and the compiler admits only plain relative text
as a path argument. That rendering is likewise never recorded, logged,
retained in the conversation, or sent to a model.

### Model-Originated Plans

A model-backed turn runs `converse(..., proposals=True)`, so the request
carries Maria's fixed proposal instructions and the provider is asked at most
once. A conversational outcome is accepted only when `is_conversational_plan`
holds, and then takes the ordinary zero-step path; anything else answers
`Maria could not use that conversational result.`, keeping any clearing the
turn made. A proposal attempt goes to `compile_proposal(request, proposal,
self.tool_manager.contracts, self.logger)`: the cleaned request, the reply
exactly as it arrived, and Tool Manager's one active registry. A
`ProposalRejection` answers with its fixed message and then the turn's
notices; nothing is recorded, repaired, or retried, and the model is asked
nothing more. Brain is the only production module that imports the compiler.
See "Model-Originated Execution and Audit" in `docs/architecture.md` for the
boundary as a whole.

A compiled plan has the `MODEL` origin. Brain routes every plan whose origin is
anything but exactly `DETERMINISTIC` this way, so a plan whose origin was
altered can only be refused here, never run down the deterministic path.
Whichever entry point it reaches, Brain checks it in this order:

1. Its shape. A plan that `is_model_plan` refuses is answered with `Maria
   refused that plan because it is not a well-formed plan compiled from one
   model proposal.`
2. `recheck_model_plan` against `self.tool_manager.contracts` as it stands
   now. A rejection is answered with its fixed message.
3. `PermissionManager.check()` for its one step. A denied action is answered
   with `Permission denied: <reason>` before any approval is offered or any
   audit summary is written.

Each of these refusals carries the plan, with the turn's notices after it when
it arises in `process`, and none is recorded. Every plan that passes needs
exactly one approval from the owner, including when its action is allowed
outright: the requirement is Brain's own, and no permission policy changes.
Through `process` the plan comes back with `needs_confirmation` set, as the
very same immutable plan, never passed to `ToolManager.prepare_confirmation()`,
and with only the turn's notices as text, which the session shows below its
rendering of the plan.

Through `execute_confirmed_plan` the same checks run again, without reparsing,
rebuilding, or substituting anything and without asking a provider, and then:

1. The audit summary is committed with
   `Memory.record_audit_summary(contract.identifier,
   AuditDecision.OWNER_APPROVED, audit_target(contract))`. If that raises an
   `Exception`, or returns anything but an exact positive `int`, nothing is
   dispatched and the answer is `Maria did not run the approved action because
   its audit summary could not be recorded.`; the approval stays spent.
2. The step is dispatched exactly once through `ToolManager.execute_tool()`,
   with the very arguments object the plan holds.
3. The outcome is appended through `Memory.record_audit_outcome()`:
   `succeeded` when the result is exactly a `ToolExecutionResult` whose
   `success` is `True`, and `failed` otherwise. Anything Tool Manager hands
   back that is not exactly a `ToolExecutionResult`, a subclass included, is
   neither shown nor trusted: it counts as the shared fixed failure, `Tool
   execution failed.`

The answer is the tool's message, a blank line, and `Recorded as audit summary
<id>.`, or, when the outcome could not be recorded, `Recorded as audit summary
<id>, but its outcome could not be recorded, so that summary shows the outcome
as unknown.` Nothing is retried, and nothing is recorded as interaction
history, because a `MODEL` plan is transient. The dispatch step itself refuses
any call not marked approved with `Maria runs a model-proposed action only
after you approve it.` Brain's log lines on this path, the compiler's included,
are fixed text naming at most the declared tool and action, a summary id, or a
rejection code, never the request, the reply, an argument, or a result, and
`filesystem.list`, the one model-eligible action that takes a path, logs only
`Listed a directory.` or `Directory listing failed.`

## Session

`MariaSession(brain: MariaBrain, logger: Logger | None = None)`

The interface-agnostic confirmation boundary. It needs no standard input,
standard output, or terminal, so the CLI and any later interface share one
contract. It holds at most one pending approval and owns the whole policy for
invalidation, replay prevention, and refusal.

`MariaSession.submit(user_input: str) -> BrainResponse`

Discards any approval still pending, then plans the request exactly once,
handing Brain the session's conversation and, when there are any, its
supplied-record fingerprints. A confirmation-required response becomes the
single pending approval. When its plan's origin is anything but exactly
`DETERMINISTIC`, the session first replaces the response's text with
`render_model_approval(plan)`, followed, after a blank line, by Brain's
notices when there are any, so the owner sees exactly the plan the approval
then binds; a plan that cannot be rendered, which includes any that is not
exactly a well-formed model-originated plan, raises `ValueError`, and nothing
becomes pending. A response carrying a
`conversation` replaces the session's conversation, held to the retention
limits again here, and replaces its fingerprints with the response's
`supplied_context`, or with nothing when the conversation is empty; a response
carrying more than 64 fingerprints clears both. Every other response leaves
both unchanged, so a turn that ended in a proposal changes them only when it
cleared them.

`MariaSession.conversation -> tuple[ReasoningExchange, ...]`

The session's completed conversational exchanges, oldest first: at most
eight, with at most 8 KiB of content. Empty for a new session. Each exchange
holds only the owner's request and the model's reply, never Maria's notices
about context.

`MariaSession.supplied_context -> frozenset[ContextFingerprint]`

The fingerprints of every memory and note supplied to the model since the
conversation was last cleared, at most 64: source, id, and a SHA-256 digest of
the exact text, a note's title and body together, never the text. Empty for a
new session and whenever the conversation is empty.

`MariaSession.end() -> None`

Ends the session: discards its conversation, its supplied-record fingerprints,
and any pending approval, writes nothing, and logs only `Session ended.`. Ending again changes nothing. The CLI
calls it on every way out of its main loop.

`MariaSession.confirm() -> BrainResponse`

Executes the pending plan exactly as it was presented. The approval is consumed
before execution is attempted, so neither a failing tool nor a raised exception
can leave it replayable.

`MariaSession.cancel() -> bool`

Discards the pending plan, planning and executing nothing, and returns whether
an approval was actually pending.

`MariaSession.pending_approval -> PendingApproval | None`

The plan awaiting approval, or `None` when the session is idle.

`PendingApproval` is a frozen value object holding exactly two fields: the
immutable `plan` and the `fingerprint` captured when that plan became pending.
It keeps nothing else, neither the text the owner was shown nor the notices
shown with it, so confirming never shows them again. It lives only in process
memory. It is never written to SQLite, a file, or configuration, never
serialized, and never logged, because the plan it wraps can contain note
bodies, memory facts, and file paths. Its `repr()` is exactly
`PendingApproval(<redacted>)`, showing neither the request, an argument, what
was shown, nor the fingerprint.

Confirmation first checks that the pending plan is still an exact `Plan` whose
origin is exactly `DETERMINISTIC`, or that `is_model_plan` still holds for it,
and only then compares its current `fingerprint` with the value captured when
it became pending, because the fingerprint reads the origin and every step. A
confirmation that binds no pending plan, whose plan fails that first check,
whose plan can no longer be fingerprinted at all, or whose plan no longer
matches executes nothing. A failure to fingerprint is never raised, the only
line logged is the fixed `Refused a confirmation that binds no pending plan.`,
and the owner gets one fixed message:

```text
No pending action matches that confirmation. Please make the request again.
```

That refusal repeats no request text, argument, canonical payload, stored
content, file path, fingerprint, or object representation. It reaches the owner
through the same interface wording as any other response. The same message
covers a second confirmation after success, a confirmation after cancellation,
and a confirmation after an intervening request, because all four mean the
approval binds no executable plan.

`render_model_approval(plan: Plan) -> str`

Renders a model-originated plan for the owner's approval as four lines,
`MODEL_APPROVAL_HEADER` and then the step's declared tool, its action, and
every argument through `approval_arguments`, from the plan alone and without
changing it. Nothing is truncated, omitted, or normalised:

```text
Maria's local reasoning model proposed this action. It runs only if you approve it.
Tool: filesystem
Action: list
Arguments: {"path": "reports/2026"}
```

A proposal with no arguments shows `Arguments: {}`. Anything but a plan
`is_model_plan` accepts, named by a tool and an action that are plain ASCII
identifiers of at most 64 characters, raises
`ValueError(MODEL_APPROVAL_MESSAGE)`: `Only a well-formed model-originated plan
can be shown for approval.`

`approval_arguments(arguments: Mapping[str, str | int]) -> str` and
`approval_quoted(text: str) -> str`

`approval_arguments` writes the arguments as one JSON object on one line, names
in sorted order, each name and text value through `approval_quoted` and each
`int` in full, so decoding it as JSON gives back exactly the arguments; any
other value type raises `ValueError(MODEL_APPROVAL_MESSAGE)`. `approval_quoted`
writes text between quotation marks, keeping every printable ASCII character
as itself except that the quotation mark and the backslash are each preceded
by a backslash. Every other character is written as a four-digit `\uXXXX`
escape in lowercase hexadecimal: each control character, DEL, each character
outside ASCII, a character beyond the Basic Multilingual Plane as its surrogate
pair, and every space that leads, trails, or stands in a run of two or more. A
single space between other characters stays a space, so a leading space, `a`,
two spaces, `b`, U+00E9, U+007F, and a trailing space are shown as
`"\u0020a\u0020\u0020b\u00e9\u007f\u0020"`. The result is printable ASCII and
one JSON string literal, so nothing in it can hide, reorder, or start a line,
and decoding it gives back exactly the original text. Text holding a surrogate
code point is refused rather than quoted, because a high and a low surrogate
kept as two separate characters would decode as the one character they
encode; no compiled plan can hold one, since the compiler refuses that
category. So both round trips hold for everything the two functions accept.
Anything but exact text, and text holding a surrogate, raises
`ValueError(MODEL_APPROVAL_MESSAGE)`, so a plan that cannot be shown exactly
is never pending. It is the approval's own quoter, separate from
`quoted_for_owner`.

## Conversation

`core/conversation.py` settles one conversational turn. It keeps no state; the
conversation belongs to the caller's session. It imports only the context
module, the shared failures, the logger, the planner's plan types, and the
reasoning interface, so it has no path to permissions, tool dispatch,
persistence, or the compiler. It parses no reply: in proposal mode it only
recognises a proposal attempt and hands it back untouched for Brain to
compile.

`GENERIC_FALLBACK_RESPONSE` and `is_generic_fallback(plan: object) -> bool`

The planner's exact generic no-action text, and the one test Brain uses to
recognise that fallback: true only for an exact `Plan` equal to
`Plan(plan.original_request, (), GENERIC_FALLBACK_RESPONSE)`, so a plan with a
step, another recording policy, other text, or a `Plan` subtype is never the
fallback. The planner itself is unchanged.

`JSON_WHITESPACE = " \t\r\n"` and `is_proposal_attempt(text: object) -> bool`

True only for exact text whose first character, once `JSON_WHITESPACE` and
nothing else is skipped, is `{`. Nothing is parsed, so a reply that starts
with `[`, or with a no-break space, a vertical tab, a form feed, or U+2028
before its `{`, stays conversational text. Whether an attempt is a valid
proposal is the compiler's question.

`is_conversational_plan(plan: object) -> bool`

True only for an exact `Plan` of `DETERMINISTIC` origin with an exact empty
tuple of steps and the `TRANSIENT` policy, which is all `transient_plan`
produces. Brain accepts nothing else as a conversational outcome.

`CONVERSATION_MAX_EXCHANGES = 8` and `CONVERSATION_MAX_BYTES = 8192`

The most completed exchanges a session keeps or a request carries, and the
hard content limit.

`conversation_budget(settings: ReasoningSettings) -> int`

`CONVERSATION_MAX_BYTES` or the configured `max_input_bytes`, whichever is
smaller; the hard limit alone for disabled settings.

`fitted_history(request, conversation, budget, context=(),
proposal_instructions=False)` and
`bounded_conversation(exchanges, budget=CONVERSATION_MAX_BYTES)`

`fitted_history` returns the newest whole exchanges that fit beside the
request, the rendered `context`, and any proposal instructions
`proposal_instructions` selects, all measured by `request_content_bytes`,
within the budget, at most eight. `bounded_conversation` keeps the
newest eight and then drops the oldest, whole, until their content fits the
budget. Neither ever truncates text. Anything but an exact tuple of exact
exchanges raises `ValueError(REASONING_HISTORY_MESSAGE)`.

`transient_plan(request: str, response: str) -> Plan`

`Plan(request, (), response, RecordingPolicy.TRANSIENT)`: the zero-step plan
every conversational outcome produces, so Brain's recording gate writes no row.
A proposal attempt produces no plan here; only the compiler builds its plan.

`with_notices(text, notices) -> str`

`text`, then a blank line and one notice per line; with no notices, `text`
exactly as it is.

`converse(provider, settings, request, conversation, logger=None, *,
supplied_context=frozenset(), enabled_memories_read=(),
supplied_memories_read=(), supplied_notes_read=(), searched_memories_read=(),
searched_notes_read=(), proposals=False) -> ConversationTurn`

Answers one request conversationally, or, in proposal mode, hands back a
proposal attempt. `proposals` must be exactly `True` or `False`, or
`ValueError(REASONING_PROPOSAL_INSTRUCTIONS_MESSAGE)` is raised; with it off
the turn is exactly what it was before proposal mode existed. Brain always
turns it on, so the request selects Maria's fixed proposal instructions (see
"Reasoning Interface"). `enabled_memories_read` is
what Brain's bounded Tier A read returned, or `None` when any context read
failed; the `supplied_*` reads are the rechecks of the records
`supplied_context` names, and the `searched_*` reads are Tier B's results. All
are settled first: a failed or malformed read sends no context and no earlier
exchange and clears the conversation; otherwise, when a fingerprint in
`supplied_context` is no longer among the enabled records rechecked with the
same text, the whole earlier conversation is cleared before anything is sent.
The request, together with the proposal instructions in proposal mode, then
comes first, and the instructions are never dropped to make it fit. Fitting
Tier A context comes second and fitting Tier B matches third, memories before
notes, in what is left, less the two-byte `INSTRUCTION_SEPARATOR` in proposal
mode; fitting earlier exchanges come fourth. A conversation that would then
carry more than 64 supplied records is cleared before sending, with
`CONTEXT_LIMIT_CLEARED_NOTICE`. In proposal mode a reply
`is_proposal_attempt` recognises is returned untouched as the turn's
`proposal`, with the turn's notices beside it: it is never kept as an
exchange, the fingerprints of what it was given are not kept, and the session
changes only if this turn already cleared it.

| Situation | Response text | Provider asked | Conversation |
| --- | --- | --- | --- |
| Request cannot be carried as text (for example a lone surrogate) | `CONVERSATION_UNSENDABLE_MESSAGE` | no | unchanged |
| Request, with the proposal instructions in proposal mode, over the budget | the fixed input-over-budget message | no | unchanged |
| Typed failure, crash, or malformed reply | that failure's fixed message | once | unchanged |
| Proposal attempt, in proposal mode | none here: the reply goes back as `proposal`, and Brain answers it | once | unchanged; nothing is kept |
| Request and reply together over the budget | `CONVERSATION_EXCHANGE_TOO_LARGE_MESSAGE` | once | unchanged |
| Reply that fits | the reply, verbatim | once | fitted history plus the new exchange, bounded |

"Unchanged" becomes "cleared" whenever the turn cleared the conversation.
Every response text is followed, after a blank line, by Maria's notices: the
cleared, recheck-limit, or unreadable notice when it applies; then `Memory
context supplied: memory <id>, ...` with the uncertainty sentence after a
reply, or `Memory context selected for this request: memory <id>, ...` with
the delivery caveat after a failure, when Tier A was sent; the Tier A omission
notice when a bound left enabled memories out; then `Found by search and
supplied: memory <id>, note <id>, ...` with its uncertainty sentence, or the
matching `Found by search and selected for this request: ...` after a failure,
when Tier B was sent; the search omission notice when a bound left matches
out; and `Possible divergent versions: note <id> and note <id> have the same
title and different bodies. ...` for selected notes that share a title. With
no context at all the one notice is `No memory context supplied.` A proposal
attempt carries the notices a reply would, in `ConversationTurn.notices`
rather than after a text; Brain shows them once, after a refusal or with the
approval request, and they are never kept with an approval or shown again
when it is confirmed.

`ConversationTurn(plan, conversation=None, supplied_context=None,
proposal=None, notices=())` carries exactly one outcome: `plan`, the transient
plan of a conversational outcome, whose response already ends with the
notices, or `proposal`, the exact `ReasoningResponse` of a proposal attempt,
with `notices` an exact tuple of Maria's notice texts. With either it carries,
together, the conversation and fingerprints the session should keep; both are
`None` when it keeps what it had, and setting only one raises `ValueError`. A
plan turn must have no `notices`, and a proposal turn can only keep what the
session had or clear it, with `()` and an empty frozenset; anything else
raises `ValueError`. Refusals log a fixed line such as `Conversational request
refused: over budget.`; the turn also logs `Memory context selected: <n>
item(s).` and `Search context selected: <n> item(s).`, a proposal attempt logs
`Model reply is a proposal attempt.`, and clearing logs a fixed line. No
request, reply, history, stored text, search term, or digest is ever logged.

## Context

`core/context.py` holds Tier A and Tier B context: the provenance values, the
selection rules, the search-term rule, the rendering a model receives, and the
fixed owner-visible notices. It imports no Maria module, reads nothing, stores
nothing, and logs nothing.

`CONTEXT_MAX_ITEMS = 16`, `CONTEXT_MAX_BYTES = 2048`, and
`CONTEXT_QUERY_LIMIT = 17`

At most 16 Tier A memories per request, at most 2 KiB of Tier A rendered on
its own (fixed instructions, header, ids, and quoted text), and the
one-extra-row limit Brain passes to the bounded read. `CONTEXT_MAX_BYTES` is
also the per-text size bound Brain passes to every context read, since a
longer text could never be sent.

`SEARCH_MAX_ITEMS = 8`, `SEARCH_QUERY_LIMIT = 9`, `SEARCH_MAX_TERMS = 8`,
`SEARCH_TERM_MIN_CHARACTERS = 4`, `SEARCH_TERM_MAX_CHARACTERS = 64`,
`COMBINED_CONTEXT_MAX_ITEMS = 24`, `COMBINED_CONTEXT_MAX_BYTES = 4096`, and
`SUPPLIED_CONTEXT_MAX_RECORDS = 64`

At most 8 Tier B items, 9 rows per source read, 8 search terms of 4 to 64
characters, 24 items and 4,096 fully rendered bytes across both tiers, and 64
supplied records a conversation may carry.

`ContextSource.MEMORY`, `ContextSource.NOTE`, `ContextTier.A`, `ContextTier.B`,
`ContextItem(source, record_id, text, title=None, tier=ContextTier.A)`, and
`ContextFingerprint(source, record_id, digest)`

A `ContextItem` is one stored record supplied to reasoning: an exact
`ContextSource`, the positive `int` id Maria read, non-empty, encodable text of
at most `CONTEXT_MAX_BYTES` — a memory's fact or a note's body — and a note's
title under the same bound, with `title` None for any other source. Only a
memory can be a Tier A item. Anything else raises
`ValueError(CONTEXT_ITEM_MESSAGE)`. `item.fingerprint` returns its
`ContextFingerprint`, whose `digest` is the SHA-256 of the exact text, or for a
note `note_digest(title, body)`: the SHA-256 of both as one compact JSON
array. Neither `repr()` shows text or digest. A later stage that makes another
record type eligible adds a `ContextSource` member.

`search_terms(request) -> tuple[str, ...]` and
`memory_search_after(memories) -> int | None`

`search_terms` casefolds the request, splits it on whitespace, strips
non-alphanumeric characters from both ends of each word, keeps words of 4 to 64
characters, drops repeats in first-seen order, and returns the first 8; text a
request cannot carry yields none. `memory_search_after` returns the id of the
16th memory Tier A considered when its read returned more, and None otherwise.

`select_search_context(enabled_items, memories, notes, byte_budget) ->
SearchSelection`

Takes the first 8 rows of each source's search, memories first, each in
ascending id order, whole, while fewer than 8 have been taken and the whole
rendered context, Tier A included, stays within `COMBINED_CONTEXT_MAX_BYTES`
and `byte_budget`. `SearchSelection` holds the `items` sent, the `omitted`
`(source, id)` pairs a read returned that did not fit, or memories too large to
supply, and `more`, the sources whose read returned a ninth row. A note search
never returns a note by id only, and `searched_notes_whole(notes)` raises
`ValueError(SEARCHED_NOTES_MESSAGE)` for one that does, so the conversation
module treats such a read as untrustworthy and fails closed rather than naming
the note.

`select_memory_context(memories, byte_budget) -> ContextSelection`

Takes the first 16 enabled memories in ascending id order, each whole, while
the rendered context stays within `CONTEXT_MAX_BYTES` and `byte_budget`; one
that does not fit is left out and the next is still considered. An oversized
memory, whose text the bounded read never returned, is left out the same way.
`ContextSelection` holds the `items` sent, the `omitted_ids` the read returned
but the limits kept out, and `more_enabled` when the read found more than 16.

`render_context_data(items) -> str`, `rendered_context_bytes(items) -> int`,
and `context_content_bytes(context) -> int`

The data block is the header `Owner memories (reference data):` and then one
line per Tier A memory, `memory <id>: <fact as one JSON string>`, then, when
Tier B is present, the header `Owner memories and notes found by search
(reference data):` and one line per match: `memory <id>: <fact as one JSON
string>` or `note <id>: {"title": ..., "body": ...}`. JSON escapes every quote
and control character, and Maria additionally escapes U+0085, U+2028, and
U+2029, the line separators JSON leaves as they are, so every separator
`str.splitlines()` recognises is escaped and no stored text can break out of
its line or pose as another record. The escapes are standard JSON, so the
decoded text is unchanged, and the rendered size counts the escaped bytes.
`context_instructions(items)` returns the fixed text sent ahead of it:
`CONTEXT_INSTRUCTIONS`, exactly the V1-15 text, when only Tier A is present,
and `SEARCH_CONTEXT_INSTRUCTIONS` otherwise; neither contains stored text.
`rendered_context_bytes` is their combined UTF-8 size, or `0` for no items.
When a request also selects the proposal instructions, they precede the
context instructions in the same system message, joined by
`INSTRUCTION_SEPARATOR` (see `system_instructions` under "Reasoning
Interface"), and neither they nor that separator count here.
`context_content_bytes` validates a request's context again — an exact tuple of
at most 24 exact items, still valid; at most 16 Tier A memories first,
rendering on their own within `CONTEXT_MAX_BYTES`; at most 8 Tier B items after
them, grouped by source; each group in strictly ascending id order and no
memory in both tiers; everything rendering within
`COMBINED_CONTEXT_MAX_BYTES` — and returns its size, raising
`ValueError(CONTEXT_MESSAGE)` otherwise.

`supplied_still_current(supplied, memories, notes=()) -> bool`

True when every supplied fingerprint names a record among the enabled rows a
recheck by id returned, with exactly the text it had; a missing, oversized, or
changed record is not current. Only supplied records are hashed.

`search_supplied_notice(items)`, `search_selected_notice(items)`,
`search_omission_notice(selection)`, and `divergent_notices(items)`

Tier B's owner-visible notices, built from sources and ids only.
`divergent_notices` names selected notes that share exactly a title while their
bodies differ, as possible divergent versions; it never claims a contradiction
or that either is stale.

`quoted_for_owner(text) -> str`

Returns stored text as one line of JSON the owner can read exactly: the
model's quoting, with every character `str.isprintable()` rejects — control,
format, and separator characters such as a newline, U+2028, U+202E, or a
zero-width space — escaped as well. The escapes are standard JSON, so the text
decodes unchanged; an invisible character is shown rather than hidden, and no
stored text can start a new line or pose as Maria's own words.

## Planner

`Planner.create_plan(user_input: str) -> Plan`

Returns a `Plan` containing zero or more `PlanStep` objects. The request text
is the only input; no stored record, interaction history, or memory context is
consulted, and any other argument raises `TypeError`.

`Plan` and `PlanStep` are immutable value objects, and both are hashable, so a
plan can be compared, stored in a set, or used as a dictionary key. Step order
is significant; argument insertion order is not.

`Plan(original_request, steps, response_if_no_action=None,
recording=RecordingPolicy.RECORD, origin=PlanOrigin.DETERMINISTIC)`

`PlanOrigin` says who produced a plan's steps: `DETERMINISTIC`, the default,
for every plan Maria's own code builds, the planner's included, or `MODEL`,
which only `core/plan_compiler.py` sets, on a plan compiled from one
reasoning-model proposal. The origin must be exactly a `PlanOrigin` member;
anything else, including a string equal to a member's value, raises
`TypeError("Plan origin must be a PlanOrigin.")`. A `MODEL` plan must be
`TRANSIENT`, with exactly one step and no canned response, and any other shape
raises `ValueError(MODEL_PLAN_SHAPE_MESSAGE)`, `A model-originated plan must be
transient, with exactly one step and no canned response.`, so
`dataclasses.replace` cannot produce one either. The origin takes part in
equality and hashing like every other field.

`is_model_plan(plan: object) -> bool`

True only for an exact `Plan` of `MODEL` origin and `TRANSIENT` policy, with no
canned response and an exact tuple holding exactly one exact `PlanStep` whose
tool and action are exact `str` and whose arguments are its `PlanArguments`.
Every field is read again rather than trusted from construction, because a
frozen dataclass can still be altered through `object.__setattr__`, so the
session, Brain, and the compiler ask it again before acting on a model plan.

`PlanStep.arguments` is a read-only `Mapping[str, Any]`. A supplied mapping is
copied into an immutable `PlanArguments` snapshot on construction, so mutating
the original mapping afterwards cannot change the step, and no accessor returns
the caller's object. Key lookup, `get`, `in`, `len`, iteration, `keys`,
`items`, `values`, and equality against a plain dictionary all work as usual;
every mutating operation raises.

The supported argument domain is `str` keys with `str` or `int` values, which
is exactly what the deterministic planner emits. Both supported value types are
themselves immutable, so a snapshot is deeply immutable without recursive
copying. Any other key or value type, including `bool`, `float`, `None`, and
any nested container, raises `TypeError` at construction naming the key and the
rejected type, never the value.

`Plan.fingerprint -> str`

Returns a stable lowercase SHA-256 hexadecimal digest of the plan's complete
logical content: `original_request`, every step in order with its `tool_name`,
`action`, and complete arguments, `response_if_no_action`, the recording
policy when it is not the default `RECORD`, and the origin when it is not the
default `DETERMINISTIC`. A default plan's serialization carries neither key,
so its digest is exactly what it was before either existed, while an otherwise
identical `TRANSIENT` plan has a different digest, and a `MODEL` plan's digest
differs from that of the same plan with the default origin. See
`docs/architecture.md`, "Interaction Recording Boundary", for what
the policy does. The digest comes
from a canonical JSON serialization with sorted mapping keys, fixed separators,
and ASCII escaping, encoded as UTF-8, so it is deterministic across processes
and independent of Python's randomized hashing. Structurally equal plans share
one fingerprint, argument insertion order cannot change it, step order and any
single differing character do change it, and string and integer values stay
distinguishable. Only the digest is exposed: the serialized content can include
note bodies, memory facts, and file paths, so it is never returned or logged.

## Memory

The memory layer owns all SQLite access. Other modules must use the public
`Memory` methods documented below rather than importing `sqlite3` or executing
SQL directly.

Every connection uses an explicit 5.0-second SQLite busy timeout
(`DEFAULT_DATABASE_BUSY_TIMEOUT_SECONDS`), which governs SQLite's built-in
busy waiting for `SQLITE_BUSY` contention. When statement execution or commit
ultimately raises a SQLite error whose numeric primary result code is
`SQLITE_BUSY` or `SQLITE_LOCKED` (including extended variants of either,
classified by that numeric code, not by matching exception text), every
method below raises `RuntimeError("Database is busy. Try again shortly.")`
instead of its listed operation-specific message; `SQLITE_LOCKED` is
classified the same way but is not guaranteed to use or exhaust the
busy-timeout window. A failure while opening the connection itself is not
covered by this fixed message and continues through that method's existing
operation-specific static `RuntimeError` message. Every other SQLite failure
likewise continues to raise that method's existing operation-specific
`RuntimeError` message, unchanged. The audit-summary methods classify
contention themselves, wherever it arises; see "Audit Summaries".

### Schema

`Memory.initialize() -> None`

Brings the configured database up to `DATABASE_SCHEMA_VERSION`, creating it
when it does not exist. It settles the database's own version first and does
it once, so no other method has to decide what shape of database it is looking
at: an unversioned file is identified by its structure, and a stamped file has
its structure confirmed against the frozen schema for the version it claims
before any migration statement runs. A file already at the current version is
validated read-only and left byte for byte unchanged, and every migration runs
in ascending order, one transaction per step, with the version marker written
inside that same transaction.

Raises `TypedFailureError` carrying `FailureKind.DATABASE_SCHEMA_TOO_NEW` when
the file is stamped above `DATABASE_SCHEMA_VERSION`, refusing before anything
is written. Every other refusal and failure - an unrecognised or drifted
structure, a marker that disagrees with the schema it names, a file holding
another application's objects, a half-created database, an invalid migration
set, a failed migration, and a corrupt or unreadable file - raises
`RuntimeError("Unable to initialize Maria database.")`. Contention keeps the
shared classification described above, but this method applies it itself
rather than inheriting it: a `SQLITE_BUSY` or `SQLITE_LOCKED` result raises
`RuntimeError("Database is busy. Try again shortly.")` wherever it arises,
including while the connection is being opened, because it is transient and
worth retrying rather than a broken database.

This method logs nothing derived from the failure:
a refusal records one fixed sentence carrying a stable event code, never an
exception, its text, a traceback, or the database path. `docs/database.md`
describes the classification, migration, refusal, and logging rules in full.

`Memory.schema_version() -> int`

Returns the schema version currently stamped on the database, which is what is
stored rather than what this build expects. An unstamped or absent database
returns `0`. This is the only way to read the marker from outside the memory
layer; no caller opens its own connection or issues its own pragma. SQLite
failures raise `RuntimeError("Unable to read database schema version.")`, and
contention raises `RuntimeError("Database is busy. Try again shortly.")`. Like
`initialize()`, this method classifies contention itself rather than
inheriting the shared conversion, and logs a fixed event code rather than an
exception.

### Interaction History

`Memory.record_interaction(user_input: str, response: str) -> int`

Stores the supplied request and response strings as full text, adds a UTC ISO
8601 `created_at` timestamp, and returns the inserted interaction ID. The method
does not strip, summarize, or validate its inputs as non-empty; Brain calls it
with the stripped request and final response text on recorded paths. SQLite
failures raise `RuntimeError("Unable to record interaction.")`.

Passing complete request and response text means tool output such as file or
note contents can become persistent interaction history. Callers must not treat
these rows as facts deliberately saved to explicit memory.

`Memory.get_recent_interactions(limit: int = 5) -> list[dict[str, Any]]`

Returns interaction dictionaries containing exactly `id`, `user_input`,
`response`, and `created_at`. Rows are ordered by `created_at` descending and
then `id` descending; an empty table returns an empty list. Requested integer
limits are clamped to a minimum of 1 and maximum of 50. SQLite failures raise
`RuntimeError("Unable to fetch recent interactions.")`.

"Recent" limits the query result and does not delete, expire, or prune older
rows. `Memory.delete_interaction`, documented below, targets at most one row
by ID and removes it when found; Maria still exposes no prune or bulk-delete
API method or CLI command.
Brain requests up to three rows for context; retrieval failure falls
back to empty context, while recording failure is logged without replacing
the user response.

`Memory.get_interaction(interaction_id: int) -> dict[str, Any] | None`

Returns one interaction by ID with exactly `id`, `user_input`, `response`,
and `created_at`, or `None` when the ID does not exist. SQLite failures raise
`RuntimeError("Unable to read interaction.")`.

`Memory.search_interactions(query: str, limit: int = 20) -> list[dict[str, Any]]`

Strips the query and raises `ValueError("Search query is required.")` when it
is empty. Matching uses SQLite's built-in `LOWER()` for ASCII
case-insensitive comparison — not full Unicode casefold equivalence, unlike
explicit-memory search — together with literal `instr()` substring matching
against both `user_input` and `response`; percent (`%`) and underscore (`_`)
are literal characters, not wildcards. Results contain `id`, `user_input`,
`response`, and `created_at`, ordered by `created_at` descending and then
`id` descending; no match returns an empty list. Requested integer limits are
clamped to the same 1-through-50 range as `get_recent_interactions`. SQLite
failures raise `RuntimeError("Unable to search interactions.")`.

`Memory.delete_interaction(interaction_id: int) -> bool`

Validates `interaction_id` directly, before opening a connection: raises
`ValueError("Interaction id must be a positive whole number.")` when it is
not an `int`, is a `bool`, or is less than 1. For a valid ID, executes
one parameterized `DELETE FROM interactions WHERE id = ?` statement without
first reading the row, and returns `True` when exactly one row was deleted or
`False` when no row matched. Deleted content is never read, logged, or
returned. SQLite failures raise `RuntimeError("Unable to delete
interaction.")`.

### Explicit Memories

`Memory.store_memory(fact: str) -> tuple[int, bool]`

Strips the fact and raises `ValueError` when it is empty. A new fact returns
`(memory_id, True)`. A case-insensitive exact duplicate returns the existing ID
as `(memory_id, False)` without changing the original fact or timestamp. SQLite
failures raise `RuntimeError("Unable to store memory.")`.

`Memory.get_memory(memory_id: int) -> dict[str, Any] | None`

Returns one explicit memory by ID with exactly `id`, `fact`, and `created_at`,
or `None` when the ID does not exist. SQLite failures raise
`RuntimeError("Unable to read memory.")`. This is an internal persistence
method, not a CLI command.

`Memory.search_memories(query: str, limit: int = 20) -> list[dict[str, Any]]`

Strips and validates the query, raising `ValueError` when it is empty. Results
use `casefold()` normalization and literal SQLite `instr()` substring matching.
They contain `id`, `fact`, and `created_at`, ordered by newest timestamp and
then descending ID; no match returns an empty list. SQLite failures raise
`RuntimeError("Unable to search memories.")`.

`Memory.list_memories(limit: int = 20) -> list[dict[str, Any]]`

Returns explicit memories with the same `id`, `fact`, and `created_at` shape.
Rows are ordered by newest timestamp and then descending ID. An empty table
returns an empty list. SQLite failures raise
`RuntimeError("Unable to list memories.")`.

Search and list default to 20 results. Direct API limits are clamped to a
minimum of 1 and a maximum of 100.

`Memory.update_memory(memory_id: int, new_fact: str) -> tuple[bool, int | None]`

Replaces one memory's fact. Before casefolding, generating anything, or opening
a connection, it requires `memory_id` to be an actual `int` (never a `bool`) of
at least `1`, raising `ValueError("Memory id must be a positive whole
number.")`, and requires `new_fact` to be an actual `str` that is non-blank
after stripping, raising `ValueError("New memory fact is required.")`. The
stripped fact is stored as `fact` and its `casefold()` form as
`normalized_fact`.

The return tuple keeps its released two-value shape and has exactly three
valid states:

- `(True, None)` — one row was updated.
- `(False, None)` — no row matched `memory_id`.
- `(False, conflicting_id)` — the target exists, but another memory already
  stores the same `normalized_fact`.

`Memory.update_memory_reporting_consent(memory_id, new_fact) -> MemoryUpdate` is
the same update — the same validation, statements, transaction, and outcomes —
returned as the frozen `MemoryUpdate(updated, conflicting_id,
context_consent_cleared)`. `context_consent_cleared` is `True` exactly when the
updated memory's context consent had been on and this update turned it off; it
is decided by the second statement below, inside the same transaction, never
by a separate read. `update_memory` returns `(result.updated,
result.conflicting_id)` from it.

For a valid ID it executes one parameterized `UPDATE memories SET fact = ?,
normalized_fact = ? WHERE id = ?` statement without first reading the row.
When that row matched, the same transaction runs `UPDATE memories SET
context_enabled = 0 WHERE id = ? AND context_enabled = 1`, because consent
covers the exact text the owner approved; a failure there rolls the new fact
back too. A conflicting or missing row changes nothing, consent included.
`created_at` is always preserved and no `updated_at` column exists, so an
updated memory keeps its position in newest-first results rather than moving to
the top — unlike a retitled or edited note. Writing a value identical to the
stored one, or one differing from it only by casing, still counts as a
successful update, because success is determined by the row matching
`WHERE id = ?` rather than by the value changing; a casing-only change rewrites
the displayed `fact` while leaving `normalized_fact` untouched, so it never
conflicts with itself.

A uniqueness conflict is detected from SQLite's own constraint rather than from
a pre-read. The raised `sqlite3.IntegrityError` is caught inside the active
connection context and classified by its exact extended result code,
`sqlite3.SQLITE_CONSTRAINT_UNIQUE`; any other constraint failure, including a
`CHECK` violation sharing the same primary result code, is re-raised and keeps
the method's static message. A confirmed conflict is resolved to the owning row
through one parameterized `SELECT id FROM memories WHERE normalized_fact = ?`
on the same open connection; `cursor.lastrowid` is never used for this. Neither
memory is changed, merged, or removed on the conflict path. If that lookup
finds no row — the conflicting memory was removed concurrently — the method
raises rather than mislabel the outcome as not-found, so the caller sees the
ordinary failure below instead of an invented conflicting ID.

Other SQLite failures are fixed failures (see "Fixed failures" below): they log
only `Failed to update memory.` and raise `RuntimeError("Unable to update
memory.")`, because SQLite's message can quote the old or the new fact.
Busy/locked contention raises `RuntimeError("Database is busy. Try again
shortly.")`.

`Memory.forget_memory(memory_id: int) -> bool`

Validates `memory_id` the same way, before opening a connection. For a valid ID,
executes one parameterized `DELETE FROM memories WHERE id = ?` statement without
first reading the row, and returns `True` when exactly one row was deleted or
`False` when no row matched. The forgotten fact is never read, logged, or
returned. Forgetting does not cascade to `interactions` or `notes` and does not
renumber remaining memory IDs. SQLite failures raise `RuntimeError("Unable to
forget memory.")`.

Both log only the numeric memory ID on success, after their transaction commits
(`Updated memory <id>.`, `Forgot memory <id>.`, and for an update that turned
consent off, `Turned off context consent for updated memory <id>.`), and log
nothing extra for a benign not-found or uniqueness-conflict result. Memory fact
text and `normalized_fact` never appear in log messages.

**Fixed failures.** SQLite's own error message can quote stored text: a
`TEXT` value that is not valid UTF-8 makes the decode error repeat it, and a
trigger's `RAISE` message can hold the text being written or removed. Every
`Memory` method that reads or writes interactions, memories, notes, or audit
summaries therefore fails in one fixed way: it catches only `sqlite3.Error`, logs one
fixed line at error level with no traceback, and raises its fixed
`RuntimeError` only after the `except` block has ended, so the failure has
neither a `__cause__` nor a `__context__` and nothing from SQLite travels with
it. SQL, transactions, rollback and closing, success logging, and the
recognised uniqueness conflicts are unchanged. Busy/locked contention is still
converted as before, logging and attaching SQLite's busy exception, whose
message is SQLite's own fixed text, except in the audit-summary methods, which
raise the same busy message with neither a cause nor a context and log only
their own fixed line. Only `initialize()` and `schema_version()`,
which never read a stored record's text, keep SQLite's exception as the cause,
and they log only fixed event codes; see `docs/database.md`.

`Memory.memory_fact_digest(memory_id: int) -> str | None`

Returns the identity of one memory's exact fact — the lowercase hexadecimal
SHA-256 of its UTF-8 text — or `None` when no row matched. The fact itself is
never returned or logged. It validates `memory_id` as above; on SQLite failure
it logs `Failed to read memory identity.` and raises the fixed failure
`RuntimeError("Unable to read memory.")`.

`Memory.enable_memory_context(memory_id: int, expected_fact_digest: str | None) ->
ContextConsentOutcome`

Turns one memory's context consent on, but only for the approved text.
`expected_fact_digest` is what `memory_fact_digest` returned when confirmation
was offered, or `None` when no such memory existed then; anything else raises
`ValueError("A memory fact identity must be a SHA-256 digest.")`. The check and
the change are one write transaction: `BEGIN IMMEDIATE` takes the write lock
before the fact is read, so no other connection can change it in between, and
`UPDATE memories SET context_enabled = 1 WHERE id = ? AND fact = ?` applies only
while the fact is still exactly the text that was checked. The outcome, from
`ContextConsentOutcome` in `core/context.py`, is `ENABLED`; `NOT_FOUND` when no
row matches; or `TARGET_CHANGED` when the text is not the approved text, when
no identity was supplied for an existing memory, or when the conditional UPDATE
matched nothing — and then nothing changes. The identity guarantee is exact
text, compared by SHA-256: text changed away and back to the approved text is
accepted, and a casing-only change is not. It logs `Enabled context consent for
memory <id>.` or `Refused context consent for memory <id>: approved text
changed.`, never the fact or the digest. On SQLite failure it logs `Failed to
change memory context consent.` and raises the fixed failure
`RuntimeError("Unable to change memory context consent.")`.

`Memory.disable_memory_context(memory_id: int) -> bool`

Turns one memory's context consent off with one parameterized `UPDATE memories
SET context_enabled = 0 WHERE id = ?`, returning `True` when a row matched —
including one already off — or `False` when none did. Turning consent off only
narrows what can be sent, so it needs no identity. It logs `Disabled context
consent for memory <id>.`, and fails exactly as `enable_memory_context` does.

`Memory.list_context_memories(limit: int, max_fact_bytes: int) ->
list[dict[str, Any]]`

The only read of Tier A context: enabled memories only, in ascending `id`
order, with `limit` clamped to 1 through 100, each as `{"id": int, "fact": str
| None, "oversized": bool}`. The bound is on each fact's UTF-8 bytes, whatever
the database file's text encoding: a fact longer than `max_fact_bytes` comes
back with `fact` `None` and `oversized` `True`. The size test runs first inside
SQLite on the stored bytes. In a UTF-8 database that test is exact, so an
oversized fact never reaches Python. In a UTF-16 database, which Maria accepts
when its schema matches, a fact is stored in at most twice its UTF-8 size, so
the test admits every fact stored in at most twice the bound, never more than
`max_fact_bytes` characters, and the memory layer then
measures each admitted fact's exact UTF-8 size before returning it. A
`max_fact_bytes` that is not a positive `int` raises `ValueError`. SQLite
failures are fixed failures: they log only `Failed to read memory context.`
and raise `RuntimeError("Unable to read memory context.")` with no cause or
context, because SQLite's message for a stored text it cannot decode quotes
that text. So does a returned fact that is not text — SQLite will store a BLOB in
the `TEXT` column — after logging the fixed line `Refused a memory context
read: a fact is not text.`; neither carries the value, and Brain treats either
as an unreadable read and fails closed. A non-text value beyond the stored-byte
test is never read and is reported as oversized like any other. `get_memory`,
`search_memories`, and `list_memories` never return the consent flag.

`Memory.context_memories_by_id(memory_ids: Sequence[int], max_fact_bytes: int)
-> list[dict[str, Any]]`

Rechecks memories already supplied to a conversation: returns, in the same
shape and under the same bounds and failures as `list_context_memories`, those
of the 1 to 64 distinct positive `int` ids named that are enabled now. Any
other id list raises `ValueError("Record ids must be distinct positive whole
numbers.")`.

`Memory.search_context_memories(terms: Sequence[str], after_id: int, limit:
int, max_fact_bytes: int) -> list[dict[str, Any]]`

Tier B's memory search: enabled memories with an id above `after_id` holding
one of the 1 to 8 casefolded terms, each non-empty and at most 64 characters,
as a literal substring, so `%` and `_` are literal; in ascending id order, at
most `limit` rows, clamped to 1 through 100, in the same shape and under the
same bounds and failures as `list_context_memories`. The limit bounds returned
rows, not rows examined. A fact within `max_fact_bytes` UTF-8 bytes is matched
against its current text, casefolded, by a matcher SQLite hands it only once
its stored size is within the bound; the matcher settles the exact UTF-8 size
itself before casefolding, so a stale `normalized_fact` can neither add nor
hide such a match. A fact too large to supply never leaves SQLite, so it cannot
be casefolded: it is matched, with `instr()`, against its stored
`normalized_fact` and comes back by id only. Maria writes that copy with every
fact she stores or updates, but cannot check it against a fact too large to
read, so a copy changed outside Maria can name an oversized memory that no
longer holds a term, or miss one that does; such a row still counts toward
`limit`. Invalid terms raise `ValueError("Search terms must be 1 to 8
non-empty texts.")`, which repeats none of them.

### Notes

`Memory.retitle_note(note_id: int, new_title: str) -> bool`

Validates both arguments directly, before generating a timestamp or opening a
connection: raises `ValueError("Note id must be a positive whole number.")`
when `note_id` is not an `int`, is a `bool`, or is less than 1, and
`ValueError("New note title is required.")` when `new_title` is not a `str` or
is blank after stripping. Non-string values are rejected rather than coerced.
For valid arguments, and in one transaction without first reading the row, it
turns the note's context consent off with `UPDATE notes SET context_digest =
NULL WHERE id = ? AND context_digest IS NOT NULL`, then executes one
parameterized `UPDATE notes SET title = ?, updated_at = ? WHERE id = ?`,
writing only the stripped title and a single current UTC timestamp; a failure
in the second rolls the first back. `body` and `created_at` are untouched.
Returns `True` when exactly one row was updated or `False` when no row
matched. A title identical to the stored one still returns `True`, still
refreshes `updated_at`, and still turns consent off, because the row matched;
duplicate titles across notes remain allowed. `updated_at` receives the current
timestamp with no guarantee that it is strictly later than the previous value.
SQLite failures are fixed failures that log only `Failed to retitle note.` and
raise `RuntimeError("Unable to retitle note.")`, and busy/locked contention
raises `RuntimeError("Database is busy. Try again
shortly.")`.

`Memory.retitle_note_reporting_consent(note_id: int, new_title: str) ->
NoteUpdate`

`retitle_note` itself, with the same validation, statements, and failures,
returning `NoteUpdate(updated, context_consent_cleared)`: whether a row
matched, and whether its consent had been on and was turned off, which also
logs `Turned off context consent for retitled note <id>.`.

`Memory.edit_note(note_id: int, new_body: str) -> bool` and
`Memory.edit_note_reporting_consent(note_id: int, new_body: str) -> NoteUpdate`

Behave identically to the retitle pair, except that they validate `new_body`
with `ValueError("New note body is required.")`, execute `UPDATE notes SET
body = ?, updated_at = ? WHERE id = ?`, leave `title` and `created_at`
untouched, log `Turned off context consent for edited note <id>.`, and on
SQLite failure log only `Failed to edit note.` and raise the fixed failure
`RuntimeError("Unable to edit note.")`.

`Memory.note_snapshot(note_id: int) -> NoteSnapshot | None` and
`Memory.note_text_digest(note_id: int) -> str | None`

`note_snapshot` reads the note's title and body in one `SELECT` and returns
`NoteSnapshot(title, body, text_digest)`, where `text_digest` is
`context.note_digest(title, body)` of exactly that text: the lowercase
hexadecimal SHA-256 of both as one compact JSON array, the identity an enable
approval is bound to. What an approval shows the owner and what it pins
therefore come from one read. `repr()` shows neither text nor identity.
`note_text_digest` returns that identity alone. Both return `None` when no such
note exists or its title or body is not non-empty text, and neither logs text.
Both raise `ValueError` for an invalid id, and on SQLite failure log the fixed
line `Failed to read note identity.`, with no traceback, and raise
`RuntimeError("Unable to read note.")` with no cause or context, because
SQLite's message
for a stored text it cannot decode quotes that text.

`Memory.enable_note_context(note_id: int, expected_text_digest: str | None) ->
ContextConsentOutcome` and `Memory.disable_note_context(note_id: int) -> bool`

The note counterparts of `enable_memory_context` and `disable_memory_context`.
Enabling runs one `BEGIN IMMEDIATE` transaction that re-reads the title and
body, compares their identity with the approved one, and only on a match runs
`UPDATE notes SET context_digest = ? WHERE id = ? AND title = ? AND body = ?`,
storing that identity as the consent; otherwise the outcome is `NOT_FOUND` or
`TARGET_CHANGED` and nothing changes. Disabling runs `UPDATE notes SET
context_digest = NULL WHERE id = ?`. They log `Enabled context consent for
note <id>.`, `Refused context consent for note <id>: approved text changed.`,
or `Disabled context consent for note <id>.`, never text or identity. On SQLite
failure they log the fixed line `Failed to change note context consent.`, with
no traceback, and raise `RuntimeError("Unable to change note context
consent.")` with no cause or context.

`Memory.search_context_notes(terms: Sequence[str], limit: int, max_text_bytes:
int) -> list[dict[str, Any]]` and `Memory.context_notes_by_id(note_ids:
Sequence[int], max_text_bytes: int) -> list[dict[str, Any]]`

Tier B's note search and the recheck of notes already supplied. Only notes
whose consent is set and whose title and body are text are considered, in
ascending id order, at most `limit` rows, clamped to 1 through 100; the limit
bounds rows returned, not rows examined. Each row is `{"id": int, "title": str
| None, "body": str | None, "oversized": bool}`.

The search considers a note only while both texts are within `max_text_bytes`
UTF-8 bytes and are still exactly the approved text, and it checks that inside
SQLite, row by row, before anything is matched: consent, type, and stored size
first, then an approval check that recomputes the identity from the current
title and body and compares it with `context_digest`. Only then is either text
handed to the matcher, which matches a note when a term occurs wholly within
its casefolded title or wholly within its casefolded body, never across the
two, after settling each text's exact UTF-8 size. A note changed outside Maria
is therefore never matched and never uses up `limit`, and a note with a text
too large to hash within the bound is never searched, returned, or named,
since its approved identity cannot be verified; every search row is whole,
with `oversized` `False`. The recheck returns a named note whole only while it
is the approved text, by id only with `oversized` `True` when a text is too
large, which the conversation treats as no longer current, and not at all when
its text is no longer the approved text. SQLite failures log the fixed line
`Failed to read note context.`, with no traceback, and raise
`RuntimeError("Unable to read note context.")` with no cause or context.
`get_note` and
`list_notes` never return the consent column.

`Memory.delete_note(note_id: int) -> bool`

Validates `note_id` the same way, before opening a connection. For a valid ID,
executes one parameterized `DELETE FROM notes WHERE id = ?` statement without
first reading the row, and returns `True` when exactly one row was deleted or
`False` when no row matched. Deleted title and body text is never read,
logged, or returned. Deletion does not cascade to `interactions` or
`memories` and does not renumber remaining note IDs. SQLite failures raise
`RuntimeError("Unable to delete note.")`.

All three log only the numeric note ID on success, after their transaction
commits, and log nothing extra for a benign not-found result. Note titles and
bodies never appear in log messages.

### Audit Summaries

The `audit_summaries` and `audit_outcomes` tables, added in schema version 5
by the `create_audit_summaries` migration (see `docs/database.md`), hold the
content-free summaries of approved model-proposed actions, in the vocabulary
described under "Audit Vocabulary". Brain alone writes them, through the first
two methods, and the audit tool alone reads and deletes them. Every method
validates its arguments before opening a connection, and an invalid summary id
raises `ValueError(AUDIT_SUMMARY_ID_MESSAGE)`.

`Memory.record_audit_summary(action: str, decision: AuditDecision, target:
AuditTarget) -> int`

Appends one `audit_summaries` row with Maria's own `audit_timestamp()` and
commits it before returning the new id, so a caller that dispatches only once
it holds the id can never run an action whose summary is not stored. `action`
must be one of `AUDITED_ACTIONS` and `decision` and `target` exact members, and
the generated timestamp is validated like every other field, or
`ValueError(AUDIT_SUMMARY_MESSAGE)` is raised. SQLite failing the insert or its
commit, an insert that added no row, and an id that is not an exact positive
`int` raise `RuntimeError("Unable to record audit summary.")`, and a database
still busy or locked once the busy timeout has passed raises
`RuntimeError("Database is busy. Try again shortly.")`. Success logs `Recorded
audit summary <id>.`.

`Memory.record_audit_outcome(summary_id: int, outcome: AuditOutcome) -> None`

Appends the one outcome of a summary that still exists, with `INSERT INTO
audit_outcomes (summary_id, outcome) SELECT ?, ? WHERE EXISTS (SELECT 1 FROM
audit_summaries WHERE id = ?)`, so no outcome can be stored for a deleted
summary, and the table's key refuses a second outcome for the same summary. An
outcome that is not exactly an `AuditOutcome` raises
`ValueError(AUDIT_SUMMARY_MESSAGE)`. A write that fails, is refused, or adds no
row, and an id larger than SQLite's largest integer, which no summary can
have, raise `RuntimeError("Unable to record audit outcome.")`, or the busy
message. Success logs `Recorded the outcome of audit summary <id>.`.

`Memory.list_audit_summaries(limit: int = 21) -> list[AuditSummary]` and
`Memory.get_audit_summary(summary_id: int) -> AuditSummary | None`

`list_audit_summaries` returns at most `limit` summaries, newest id first,
each with its outcome or `None`; `limit` must be an exact `int` from 1 to
`AUDIT_QUERY_LIMIT`, or `ValueError` is raised. `get_audit_summary` returns one
summary by id, or `None` when no summary has it; an id larger than SQLite's
largest integer is answered `None` without opening the database. Every stored
value is
validated against the audit vocabulary after it is read, and one outside it
fails the whole read without that value or any other being repeated or
logged. Failures raise `RuntimeError("Unable to list audit summaries.")` or
`RuntimeError("Unable to read audit summary.")`, or the busy message.

`Memory.delete_audit_summary(summary_id: int) -> bool`

Deletes one logical summary, its row and its outcome, in one `BEGIN IMMEDIATE`
transaction, deleting the outcome only when the summary was deleted. Returns
`True` when the summary existed, or `False`, changing nothing, when no summary
has that id, including an id larger than SQLite's largest integer, which is
answered without opening the database. `AUTOINCREMENT` keeps a deleted
summary's id from ever being
reused, so no later summary can take its place. Success logs `Deleted audit
summary <id>.`, and failures raise `RuntimeError("Unable to delete audit
summary.")` or the busy message.

Each `RuntimeError` the five raise is raised after any handler has ended, with
neither a cause nor a context, and is logged as one fixed line, `Failed to
<operation>: <reason>.`, whose reason is `database busy`, `database error`, `no
row was inserted`, `no valid id was returned`, or `a stored value is outside
the audit vocabulary`; an invalid argument's `ValueError` is raised before any
connection opens and logs nothing. No SQLite text, traceback, or stored value
is ever logged or raised. There is no update, search, filter, range, bulk
deletion, retention, or pruning.

## Tool Execution Result

`ToolExecutionResult(success: bool, message: str, data: Any = None, recorded_message: str | None = None)`

`message` is the text Brain displays to the user. `recorded_message` is an
optional override for what Brain persists to interaction history instead. It
defaults to `None`, and any result that does not opt in leaves it as `None`;
`None` makes Brain record `message` unchanged. An explicit empty string is
honored and persisted as an empty string rather than falling back to
`message`. Brain applies this generically and never branches on a tool's name
or action.

In the current implementation, three results supply a non-`None`
`recorded_message`, each a fixed label with at most an id: a successful
`history.read`, `audit.list`, or `audit.read` (see "History Tool" and "Audit
Tool"). Every other current tool result leaves the field as `None` and
therefore retains the existing message-as-recorded behavior.

## Memory Tool

`MemoryTool.execute(action: str, arguments: Mapping[str, Any]) -> ToolExecutionResult`

The memory tool supports `store`, `search`, `list`, `update`, `forget`,
`enable_context`, and `disable_context`.
Successful stores return data shaped as
`{"id": int, "fact": str, "created": bool}`. For a duplicate, `fact` is the
original stored display text. Successful searches and lists return a list of
memory dictionaries as `data`; no match or an empty table is a successful
result with `data=[]`. `store`, `search`, and `list` are unchanged.

`update` and `forget` each read an `id` argument and validate it the same way
as the history and notes tools (an actual `int`, not a `bool`, at least `1`),
returning `"Memory id must be a positive whole number."` for an invalid direct
value without calling `Memory`. `update` additionally reads a `fact` argument,
which must be an actual `str` that is non-blank after stripping; otherwise it
returns `"New memory fact is required."`, again without calling `Memory`. That
value is never coerced with `str(...)`, so `None`, numbers, booleans, and
arbitrary objects can never be persisted as memory text, and only the stripped
value is passed to `Memory`. The `store` action now rejects a non-`str` `fact`
the same way, returning `"Memory fact must be text."` instead of converting
it.

A missing row returns `"Memory not found: <id>."`. A uniqueness conflict
returns `"Memory <id> conflicts with existing memory <other id>."` — numeric
IDs only, never either memory's fact. Success returns `"Updated memory <id>."`
or `"Forgot memory <id>."` with `data={"id": <id>}` — never the old or new fact
— and leaves `recorded_message` as `None`, since those messages are already
short and content-free. When the updated memory's context consent had been on,
the update message continues `" Context consent for memory <id> was turned off
because its text changed."`; otherwise it is exactly as before.

Unsuccessful results, including the conflict result, carry no `data`. An
ordinary `Memory` failure is wrapped as `"Memory update failed: Unable to
update memory."` or `"Memory forget failed: Unable to forget memory."`;
busy/locked contention is wrapped the same way, for example `"Memory forget
failed: Database is busy. Try again shortly."`. `update` goes through
`Memory.update_memory_reporting_consent()`.

`enable_context` and `disable_context` read and validate an `id` the same way.
`MemoryTool.confirmation_binding(action, arguments)` pins an `enable_context`
approval: when the plan is offered for confirmation it adds
`{"fact_digest": Memory.memory_fact_digest(id)}`, and adds nothing for any other
action, an invalid id, a missing memory, or a read failure. `enable_context`
then calls `Memory.enable_memory_context(id, fact_digest)` and
`disable_context` calls `Memory.disable_memory_context(id)`. Success returns
`"Enabled context for memory <id>."` or `"Disabled context for memory <id>."`
with `data={"id": <id>, "context_enabled": <bool>}`; no matching row returns
`"Memory not found: <id>."`; and an enable whose memory is not the pinned text
returns, changing nothing, `"Context was not enabled for memory <id> because
it could not be confirmed unchanged since confirmation was requested. Request
confirmation again with: enable memory context <id>"`. None ever carries the
fact or the digest. A `Memory` failure is wrapped as `"Memory context change
failed: <message>"`.

`memory.store`, `memory.search`, and `memory.list` are allowed by default, while
`memory.update`, `memory.forget`, `memory.enable_context`, and
`memory.disable_context` always require explicit confirmation.

Expected validation and persistence failures are converted into unsuccessful
`ToolExecutionResult` values. The tool calls the shared `Memory` interface and
does not execute SQL.

## History Tool

`HistoryTool.execute(action: str, arguments: Mapping[str, Any]) -> ToolExecutionResult`

The history tool supports `list`, `search`, `read`, and `delete`. `list` and
`search` return at most the newest 20 rows, using the same limit-plus-one
probing pattern as notes and memories; their `data` is a redacted list of
`{"id", "user_input", "created_at"}` dictionaries that excludes `response`,
even when `search` matched inside a row's `response` text. `read` returns
the complete interaction row (including `response`) as `data`, since its
`message` already displays the full stored request and response.
Non-printable characters in a `list`/`search` display row are replaced with
a single space; the underlying stored data and `read`'s full display are
unaffected. `read` sets `recorded_message` to a fixed
`Displayed interaction <id>.` string so redisplaying old content does not
duplicate it into a new interaction row.

`delete` validates its `id` argument the same way as `read` (an actual
`int`, not a `bool`, at least `1`), returning `"Interaction id must be a
positive whole number."` for an invalid direct value without calling
`Memory`. A missing row returns `"Interaction not found: <id>."`. A
successful deletion returns `"Deleted interaction <id>."` with
`data={"id": <id>}` — never the deleted request or response text — and
leaves `recorded_message` as `None`, since that message is already short
and content-free. An ordinary `Memory` failure is wrapped as `"History
deletion failed: Unable to delete interaction."`.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.history` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `history.list`, `history.search`, and `history.read` are allowed by
default, while `history.delete` always requires explicit confirmation. The
tool contains no SQL; `core/memory.py` owns all database operations.

## Notes Tool

`NotesTool.execute(action: str, arguments: Mapping[str, Any]) -> ToolExecutionResult`

The notes tool supports `create`, `list`, `read`, `retitle`, `edit`, `delete`,
`enable_context`, and `disable_context`. `create`, `list`, and `read` are
unchanged.

`retitle`, `edit`, and `delete` each read an `id` argument and validate it the
same way as the history tool (an actual `int`, not a `bool`, at least `1`),
returning `"Note id must be a positive whole number."` for an invalid direct
value without calling `Memory`. `retitle` additionally reads a `title`
argument and `edit` a `body` argument, each of which must be an actual `str`
that is non-blank after stripping; otherwise they return `"New note title is
required."` or `"New note body is required."`, again without calling `Memory`.
Values are never coerced with `str(...)`, so `None`, numbers, booleans, and
arbitrary objects can never be persisted as note text. Only the stripped value
is passed to `Memory`.

A missing row returns `"Note not found: <id>."`. Success returns `"Retitled
note <id>."`, `"Edited note <id>."`, or `"Deleted note <id>."` with
`data={"id": <id>}` — never the note's title or body — and leaves
`recorded_message` as `None`, since those messages are already short and
content-free. An ordinary `Memory` failure is wrapped as `"Note retitle
failed: Unable to retitle note."`, `"Note edit failed: Unable to edit
note."`, or `"Note deletion failed: Unable to delete note."`; busy/locked
contention is wrapped the same way, for example `"Note deletion failed:
Database is busy. Try again shortly."`. `retitle` and `edit` go through
`retitle_note_reporting_consent` and `edit_note_reporting_consent`; when the
note's context consent had been on, their success message continues `Context
consent for note <id> was turned off because its title changed.` or `... its
body changed.`, and otherwise it is exactly as before.

`enable_context` and `disable_context` take an `id` validated the same way.
`NotesTool.confirmation_binding(action, arguments)` reads, for
`enable_context` only, one `Memory.note_snapshot` when confirmation is offered
and returns a `ConfirmationBinding`: its arguments pin `text_digest`, the
snapshot's identity, so the approved plan names the exact title and body, and
its preview shows the owner that same title and body in full, each quoted by
`context.quoted_for_owner`:

```text
This approval covers note <id> exactly as it is stored now, shown quoted:
Title: "<title>"
Body: "<body>"
```

The preview follows the confirmation request and its exposure notice and goes
nowhere else: the pending plan holds only the id and `text_digest`. The binding
adds nothing for any other action, an invalid id, a missing note, or a failed
read, and the owner then sees the request without a preview. Enabling then succeeds only while the note is still
exactly that text: `"Enabled context for note <id>."`; otherwise `"Context was
not enabled for note <id> because its title and body could not be confirmed
unchanged since confirmation was requested. Request confirmation again with:
enable note context <id>"`. Disabling returns `"Disabled context for note
<id>."`. A missing note returns `"Note not found: <id>."`, and a failure is
wrapped as `"Note context change failed: <message>"`. Results carry
`data={"id": <id>, "context_enabled": <bool>}` and never the title, body, or
identity.

The tool receives the shared `Memory` instance through dependency injection.
It is registered as `tools.notes` through Tool Manager's standard
`build_tool(...)` factory contract and travels through the normal permission
path; `notes.create`, `notes.list`, and `notes.read` are allowed by default,
while `notes.retitle`, `notes.edit`, `notes.delete`, `notes.enable_context`,
and `notes.disable_context` always require explicit confirmation. The tool
contains no SQL; `core/memory.py` owns all database operations.

## Audit Tool

`AuditTool.execute(action: str, arguments: Mapping[str, Any]) -> ToolExecutionResult`

The audit tool supports `list`, `read`, and `delete` over the audit summaries
Brain writes (see "Audit Summaries"). Planner routes `audit`, `read audit
<id>`, and `delete audit <id>` to them; `docs/planner.md` has the grammar.
Everything it shows comes from the audit vocabulary.

`list` reads `Memory.list_audit_summaries(21)` and shows at most the newest 20,
by id, one line each, with `data` the list of `AuditSummary` values shown:

```text
5: 2026-10-01T09:46:17Z calculator.calculate (target: none). Approved by the owner. Outcome: succeeded.
```

The target reads `none` or `named filesystem root`, and the line ends
`Outcome: succeeded.`, `Outcome: failed.`, or, when no outcome was recorded,
`Outcome not recorded; execution is unknown.` (`OUTCOME_UNKNOWN_TEXT`). When a
21st row exists, a final line says `Showing the newest 20 audit summaries.
More audit summaries exist.` An empty table answers `No audit summaries
found.` with `data=[]`.

`read` shows one summary as a labelled block, with `data` its `AuditSummary`:

```text
Audit summary 5
Created at: 2026-10-01T09:46:17Z
Action: calculator.calculate
Target: none
Decision: approved by the owner
Outcome: succeeded
```

Its last line is `Outcome: succeeded`, `Outcome: failed`, or `Outcome not
recorded; execution is unknown.` `delete` removes the summary and its outcome
together through `Memory.delete_audit_summary()` and returns `Deleted audit
summary <id>.` with `data={"id": <id>}`.

`read` and `delete` validate an `id` argument (an actual `int`, not a `bool`,
at least `1`), returning `Audit summary id must be a positive whole number.`
for an invalid direct value without calling `Memory`, and `Audit summary not
found: <id>.` when no summary has that id. A `Memory` failure returns the fixed
`Unable to list audit summaries.`, `Unable to read audit summary.`, or `Unable
to delete audit summary.`, never the underlying message. Every successful
`list` sets `recorded_message` to `Listed audit summaries.` and every
successful `read` to `Displayed audit summary <id>.`, so no summary's fields
and no listing are copied into interaction history; `delete` leaves it `None`,
since its message is already short and content-free.

The tool receives the shared `Memory` instance through dependency injection;
its `build_tool(memory, **_)` factory ignores `config` and `logger`. It is
registered as `tools.audit` through Tool Manager's standard `build_tool(...)`
factory contract and travels through the normal permission path: `audit.list`
and `audit.read` are allowed by default, while `audit.delete` always requires
explicit confirmation, by id only and with its fixed notice (see
"Permissions"), even when no summary has that id. None of the three may be
proposed by a reasoning model, and audit summaries never become model
context. The tool contains no SQL; `core/memory.py` owns all database
operations.

## Action Contracts

`ActionContractRegistry.validate(tool_name, action, arguments) -> ContractViolation | None`

`core/action_contracts.py` declares `BUILT_IN_ACTION_CONTRACTS`, the immutable
positive allowlist of all 29 built-in tool actions and their arguments. The
table is written by hand and is independent of Tool Manager's runtime
inventory, of `PermissionManager`, and of the planner; it is never generated
from a tool's `actions` set, read from configuration, or persisted. Declaring
the same tool action twice raises `ValueError` rather than silently keeping the
last declaration, and the registry refuses attribute assignment and deletion
after construction.

`ActionContract` declares one exact tool action; `ArgumentContract` declares one
field's `value_type` (exactly `str` or `int`), whether it is `required`, and
every bound that applies to that type: `minimum` and `maximum` for an int field,
`allow_blank`, `minimum_length`, and `maximum_length` for a str field, plus
`root_class` when the field is a path. `None` for a bound declares that no bound
exists rather than leaving it open, while `allow_blank` is mandatory on a str
field so a blank policy can never be omitted. Construction raises `ValueError`
for an unsupported declared type, a numeric bound on a str field, a length bound
or blank policy on an int field, a root class on an int field, a missing blank
policy on a str field, a negative length, and a `minimum_length` greater than a
`maximum_length`.

`ActionContract` also requires the keyword-only `model_eligibility`, a
`ModelEligibility` member, `MODEL_ELIGIBLE` or `NOT_MODEL_ELIGIBLE`, with no
default: omitting it raises `TypeError`, and anything but an exact member,
including a string equal to a member's value, raises `ValueError`. It records
whether a reasoning model may ever propose the action and decides nothing
else; `core/plan_compiler.py` compiles a proposal only when it and the
compiler's own allowlist agree. Three actions are `MODEL_ELIGIBLE`:
`calculator.calculate`, `filesystem.list`, and `system.current_time`. The
other 26, the three audit actions included, are `NOT_MODEL_ELIGIBLE`.

A present str value is measured on its stripped form: that is how every tool
establishes blankness and exactly how `CalculatorTool` measures the
200-character expression ceiling it applies after stripping. The stripped text
is used for the comparison only and is never written back, so a valid argument
reaches Tool Manager and the tool by exact object identity.

`validate` returns `None` when a call is valid, or one frozen
`ContractViolation` carrying only `kind`, `tool_name`, `action`, and `field`.
An `arguments` container that is not a `Mapping`, or a mapping carrying any
non-`str` key, is refused as `WRONG_TYPE` on the field name `arguments` rather
than raising, and neither the container nor a key is converted. A tool, action,
or field name is stored only when it reads as a short plain ASCII identifier;
anything else is replaced on construction by the fixed label `unknown_tool`,
`unknown_action`, or `unsupported_field`, so a path, a newline, or an oversized
name can never reach a response, a recorded row, or a log.
`ContractFailure` has six members: `UNKNOWN_TOOL`, `UNKNOWN_ACTION`,
`MISSING_REQUIRED_ARGUMENT`, `EXTRA_ARGUMENT`, `WRONG_TYPE`, and
`OUT_OF_RANGE`. `ContractViolation.message` renders one fixed template per kind
naming the tool, action, and field only; it never contains the submitted value,
its repr, an object address, or a host path.

Validation rejects and never repairs: no coercion, parsing, truncation,
clamping, default insertion, or case and whitespace normalization. Type
identity is settled first and is exact, so `bool` is not accepted for an `int`
field, a numeric string is not accepted for an `int` field, an `int` is not
accepted for a `str` field, and an arbitrary object is refused without
`str(value)` ever being called, so a side-effecting `__str__` is never reached.
A field order of type, then blank policy, then length or numeric bounds means a
malformed value never reaches a text operation. A blank, too-short, too-long,
or out-of-range value all report `OUT_OF_RANGE`.
The supplied mapping is read, never rebuilt or copied into a mutable mapping,
so a `Plan` fingerprint is unchanged by validation. `RootClass` has one member,
`NAMED_FILESYSTEM_ROOT`, and all six filesystem path fields declare it:
`read.path`, `list.path`, `rename.source`, `rename.destination`, `move.source`,
and `move.destination`. It is classification metadata and stays that: the
registry creates no `Path`, resolves nothing, selects no root, reads no
filesystem, consults no configuration or other ambient state, and decides no
permission. Which named root a path lands in, and whether it is reachable at
all, are settled later by `FilesystemRoots` and `FilesystemTool`.

## Model-to-Plan Compilation

`compile_proposal(request, response, contracts=BUILT_IN_ACTION_CONTRACTS, logger=None) -> Plan | ProposalRejection`

`core/plan_compiler.py` compiles one reasoning-model proposal into a
single-step `TRANSIENT` `Plan` of origin `MODEL` whose `original_request` is
`request`, or
returns the `ProposalRejection` saying why it refused. `request` must be exact,
non-empty text equal to its stripped form, `response` an exact
`ReasoningResponse`, and `contracts` an exact `ActionContractRegistry`;
anything else raises `ValueError` with a fixed message. `MODEL_ACTION_ALLOWLIST`
is the closed allowlist, `MODEL_PROPOSAL_MAX_BYTES` the 4,096-byte cap, and
`MODEL_PROPOSAL_MAX_NESTING` the eight-container nesting limit.
`ProposalRejection` has twelve members: `OVERSIZED`, `MALFORMED_OUTPUT`,
`MALFORMED_PROPOSAL`, `UNKNOWN_TOOL`, `UNKNOWN_ACTION`, `NOT_MODEL_ELIGIBLE`,
`MISSING_ARGUMENT`, `EXTRA_ARGUMENT`, `WRONG_TYPE`, `OUT_OF_RANGE`,
`UNSAFE_TEXT`, and `UNSAFE_PATH`; each has a stable `code` and a fixed
`message` that repeats nothing the model supplied. Brain is the only
production module that calls it, for a reply the conversation module
recognised as a proposal attempt, passing Tool Manager's active registry,
`tool_manager.contracts`; see "Model-Originated Plans" under "Brain" and
"Model-to-Plan Compilation Gate" in `docs/architecture.md`.

`recheck_model_plan(plan, contracts, logger=None) -> ProposalRejection | None`

Checks an already compiled plan again, exactly as it stands: its one step
against the closed allowlist, the model eligibility `contracts` declares now,
the argument contract, and the text and path checks, in that order, the same
gates that compiled it, returning the first refusal or `None`. Nothing is
parsed, rebuilt, repaired, defaulted, or substituted, and no provider is asked
anything. Brain asks it when it offers a compiled plan for approval and again
just before the approved plan runs. A registry that is not exactly an
`ActionContractRegistry` raises `ValueError(COMPILER_CONTRACTS_MESSAGE)`, and
anything `is_model_plan` refuses raises `ValueError(COMPILER_PLAN_MESSAGE)`:
`Only a well-formed model-originated plan can be checked again.` A refusal
logs `Refused a compiled model plan: code=<code>` and nothing else.

## Audit Vocabulary

`core/audit.py` holds everything an audit summary may say, and the memory
layer, Brain, and the audit tool all take its words from there. It reads
nothing, stores nothing, and logs nothing, and imports nothing from Maria but
`core/action_contracts.py`.

`AUDITED_ACTIONS`, `AUDIT_LIST_LIMIT = 20`, `AUDIT_QUERY_LIMIT = 21`, and
`AUDIT_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"`

The actions a summary may name, `calculator.calculate`, `filesystem.list`, and
`system.current_time`, which are exactly `MODEL_ACTION_ALLOWLIST` but declared
again here rather than imported; the most summaries a listing shows, and the
one more it reads to learn that more exist; and Maria's one timestamp form,
UTC to the second with a literal `Z`, such as `2026-10-01T09:46:17Z`.
`audit_timestamp()` returns the current time in that form.

`AuditDecision`, `AuditTarget`, and `AuditOutcome`

Closed `str` enums: the decision that authorised an attempt, `OWNER_APPROVED`
(`owner_approved`); the generalised class of what the action acted on, `NONE`
(`none`) or `NAMED_FILESYSTEM_ROOT` (`named_filesystem_root`); and what the
tool's own success flag reported after dispatch, `SUCCEEDED` (`succeeded`) or
`FAILED` (`failed`).

`AuditSummary(summary_id, created_at, action, decision, target, outcome=None)`

One stored summary with its outcome, as a frozen dataclass. `summary_id` must
be an exact `int` of at least 1, never a `bool`; `created_at` exact text in the
timestamp form, every field at its fixed width, naming a real calendar second,
so the answer is the same on every platform; `action` one of
`AUDITED_ACTIONS`; and `decision`, `target`, and any
`outcome` exact members, with `outcome` `None` when none was recorded, which
means the execution is unknown. Anything else raises
`ValueError(AUDIT_SUMMARY_MESSAGE)`, `An audit summary may hold only values
from Maria's audit vocabulary.`, which names no value.
`AuditSummary.from_stored(...)` builds one from stored values, accepting for
each enumerated field only exact text spelling one member.
`is_audit_timestamp`, `is_audited_action`, and `is_audit_summary_id` are the
field validators, and `AUDIT_SUMMARY_ID_MESSAGE` is `Audit summary id must be
a positive whole number.`

`audit_target(contract: ActionContract) -> AuditTarget`

`NAMED_FILESYSTEM_ROOT` when the contract declares an argument of root class
`NAMED_FILESYSTEM_ROOT`, and `NONE` otherwise. It is decided from the contract
alone, never from an argument value, so `filesystem.list` without a path is
still `named_filesystem_root`. A path argument of a root class with no audited
target raises `ValueError`, so such an action could never be audited, or run.

There is deliberately no field for the request, the proposal, an argument, a
fingerprint, private context, a root's name, a host path, a tool's result, or
an exception's text. A summary records that the owner approved one
model-proposed action and that Maria authorised that attempt for dispatch; by
itself it does not prove the tool ran.

## Filesystem Roots

`FilesystemRoot(name, path, policy, enabled)`

One named filesystem root. `name` must be an exact `str` reading as a non-empty
plain ASCII identifier of at most 32 characters. `path` must be the exact
concrete `Path` class for the platform and must be absolute; the constructor
settles and stores `path.resolve()`, so no non-canonical path can exist inside a
root. `policy` must be a `RootPolicy` member and `enabled` an exact `bool`; both
are checked by `type()`, so a string equal to a policy's value and an integer
standing in for a bool are both refused. A resolution failure becomes the single
fixed `ValueError` `A filesystem root path could not be settled as a canonical
absolute path.`, which repeats nothing it was handed.

`RootPolicy` is a closed set with two members: `ORGANIZE`, which permits all four
filesystem actions, and `READ_ONLY`, which permits `read` and `list` only.

`root.contains(path)` reports whether a canonical path lies at or beneath the
root.

`FilesystemRoots(roots, default_name)`

Every declared root plus the name of the one relative text resolves against. The
constructor snapshots its iterable into a tuple, so a list mutated afterwards
cannot change a settled value, and the roots are retained as that tuple rather
than as a dictionary or a proxy over private mutable storage. Construction
refuses an empty declaration, a member that is not exactly a `FilesystemRoot`, a
repeated name, a root that shares or contains another's canonical path, and a
default that is unknown or disabled. Because overlap is refused, at most one root
can contain any canonical path.

`roots.default_root` returns the default root, `roots.enabled_roots` the enabled
ones in declaration order, `roots.by_name(name)` one root or `None`, and
`roots.containing(path)` the single enabled root holding a canonical path or
`None`. A disabled root is never considered by `containing`, so a path beneath one
reports exactly as a path outside Maria's available roots.

`roots.resolve_path(value) -> ResolvedPath | None`

Settles submitted path text against the enabled roots: `~` expands first, text
that is still relative is taken against the default enabled root, and an absolute
path is taken as given. The result is a `ResolvedPath` carrying the governing
root, the absolute pre-resolution `candidate` spelling — lexically collapsed, with
no link followed — and the canonical `target`, or `None` when the target lies
outside every enabled root. `None` is content-free: it reports only that no root
applies, never which path was submitted or which root was nearest. An unexpected
`OSError` from canonicalisation propagates so the caller reports it through its own
sanitized failure path.

`ResolvedPath(root, candidate, target)`

The two identities a protection rule needs, in one sealed value. A protected name
that links outward escapes a target-only rule, and an innocuous name that links
inward escapes a candidate-only rule, so both are carried and both are checked.
Containment is decided by `target` alone. Neither path is ever rendered into
owner-visible text, a log record, or result data.

`FilesystemProtections(database_path, log_dir, prompt_path)`

The runtime locations the active configuration chose, settled canonically in the
constructor. Each must be the exact concrete platform `Path` and absolute;
anything else raises a fixed `ValueError` that repeats neither the path nor the
underlying error. `core/config.py` builds it from `[paths]` and stores it on
`MariaConfig`; `core/filesystem_roots.py` never imports `MariaConfig`.

`database_sidecars` is a fourth, derived field holding every identity the
`-journal`, `-wal`, and `-shm` files can be reached by: the lexical name beside the
database, plus the canonical target whenever that name already resolves elsewhere
through a link. It is recomputed from the settled database path on every
*construction*, so it cannot be replaced independently or forged through `_make` or
`_replace`, and resolving once here keeps `protects` a pure path comparison. Like
any path-based rule it describes the filesystem as it stood when the value was
settled; no path check can close the window between validating a path and acting
on it.

Construction and duplication are different operations. Constructing settles the
live filesystem once; `__copy__` and `__deepcopy__` return the value itself and
`__reduce__` serialises the four settled fields to a private rebuild function, so
copying, deep-copying, and unpickling preserve the exact snapshot and perform no
filesystem access. The rebuild function is not a public alternate constructor: it
takes one positional snapshot, names the concrete type itself so no subtype can be
reconstructed through it, and refuses a snapshot of the wrong shape with a fixed
message. Pickle input is trusted input, as it is everywhere in Python.

`protections.protects(path)` reports whether one canonical path is an active
runtime location. Each protected identity covers itself and everything beneath it,
so the log directory's rotations are covered without naming rotations and a
database, sidecar, or prompt name occupied by a directory cannot leak through a
child path; under the ordinary file shape the descendant half is free, because a
file has no descendants. An *ancestor* is deliberately not protected, so an
unrelated neighbour stays usable and the database's parent directory stays
listable.

`protections.intersects_mutation(*paths) -> bool`

The wider question `rename` and `move` ask. A path intersects when it is directly
protected, when it lies inside a protected location, or when a protected location
lies inside *it* — so a directory containing the live database, one of its
sidecars, or the prompt, and any ancestor of the log directory, are all refused as
mutation operands. The comparison is pure path arithmetic, so a destination is
judged identically whether or not it exists yet. `protects` and `excludes` keep
their narrower meaning, which is what leaves an ordinary parent listable.

`protections.excludes(*paths) -> bool`

The complete universal boundary — the fixed source-layout half and the active
runtime half — applied to every identity it is handed. This is what a filesystem
capability applies. Callers pass both a candidate and a target where a submitted
path has two, and a single path where it has one, such as an already-canonical
declared root. The value is sealed exactly as the roots are: no instance
dictionary, refused assignment and deletion, no `object.__setattr__` route, no
subclassing, `_make` and `_replace` validated, structural equality and hashing,
and `copy`, `deepcopy`, and `pickle` preserving exact values.

Both value objects are sealed the way `Failure` is. Neither has an instance
dictionary, ordinary assignment and deletion are refused, `object.__setattr__`
can neither replace a settled field nor add an undeclared one, subclassing raises
`TypeError`, and `_make` and `_replace` are routed back through the validating
constructor. Equality and hashing are structural, so a root stays findable in the
set or mapping it was put into, and `copy`, `deepcopy`, and `pickle` preserve
exact values.

`is_universally_excluded(path) -> bool`

The layout-fixed *half* of the universal boundary, not the whole of it. It
classifies a path only: no file is opened, no directory is read, and no metadata
is consulted, so the answer does not depend on the path existing. It covers
`Maria/database/`, `Maria/logs/`, the repository's `.git` as either a directory or
a gitdir-pointer file, `Maria/config/config.local.toml`, and, for paths proven to
be inside the repository, credential components (`.env` and any `.env.*`, with an
exact final-component exemption for `.env.example`), the cache and environment
directories `__pycache__`, `.venv`, `venv`, `env`, `.pytest_cache`,
`.mypy_cache`, `.ruff_cache`, `maria_cli_test`, and `.claude`, and any component
ending `.tmp` or `.bak`. Component comparison is case-folded.

It knows nothing about the runtime locations configuration chose, so a capability
must never apply it alone: ask a settled `FilesystemProtections` through
`excludes` instead.

`prepare_roots(filesystem_roots, protections) -> None`

The one boundary allowed to create a filesystem root, called once by
`MariaBrain.from_defaults` with the same settled protections the filesystem tool
receives. It runs before `configure_logging` — which creates the active log
directory — and before the memory layer opens the database, so a root naming a
protected runtime location is refused before anything is created. An enabled
organizing root is created when missing, parents included; an enabled read-only
root is never created and must already be a directory; a disabled root is neither
created nor required to exist; and every root, enabled or not, is checked against
the protections first. Every refusal raises `ValueError` with one of the module's
fixed messages, so a startup failure never carries the configured path, the
owner's home location, or an underlying `OSError`'s text.

## Shared Failures

`Failure(kind: FailureKind)`

An immutable typed failure carrying a kind and nothing else. `FailureKind`
is a closed set with nine members:

| Kind | `.code` | `.message` |
| --- | --- | --- |
| `TOOL_EXECUTION_FAILED` | `tool_execution_failed` | `Tool execution failed.` |
| `DATABASE_SCHEMA_TOO_NEW` | `database_schema_too_new` | `This database was written by a newer version of Maria and cannot be opened by this one.` |
| `REASONING_UNAVAILABLE` | `reasoning_unavailable` | `The local reasoning model is unavailable.` |
| `REASONING_TIMEOUT` | `reasoning_timeout` | `The local reasoning model did not respond in time.` |
| `REASONING_REFUSED` | `reasoning_refused` | `The local reasoning model refused the request.` |
| `REASONING_MALFORMED_OUTPUT` | `reasoning_malformed_output` | `The local reasoning model returned an unusable response.` |
| `REASONING_OUTPUT_TRUNCATED` | `reasoning_output_truncated` | `The local reasoning model returned an incomplete response.` |
| `REASONING_INPUT_OVER_BUDGET` | `reasoning_input_over_budget` | `The request is too large for the local reasoning model.` |
| `REASONING_OUTPUT_OVER_BUDGET` | `reasoning_output_over_budget` | `The local reasoning model's response was too large and was discarded.` |

The seven reasoning kinds are what `request_reasoning` returns in place of a
reply; see "Reasoning Interface".

`.code` returns the kind's stable identifier, which is what an operational log
record may name. `.message` returns the fixed owner-visible sentence for the
kind. The kind is checked by exact type, so a plain string equal to a member's
value raises `ValueError` rather than reaching the message table.

The value object has no field for a request, a response, an arguments
mapping, an exception, its text, a path, an endpoint, a credential, or a
header, and a message interpolates nothing, so neither a caller's value nor
an underlying failure's text can travel with it. Constructing one logs
nothing and touches no file. `ContractFailure` in `core/action_contracts.py`
is separate and unchanged: it is the action-contract vocabulary rather than
this shared spine.

`TypedFailureError(failure: Failure)`

The exception a boundary raises when it has to stop rather than return a
value. It carries one `Failure` and derives everything it shows from that
failure's kind: `args` holds the failure alone, `str()` is the kind's fixed
owner-visible sentence, and `repr()` names the kind. The failure is checked by
exact type, so anything else raises `ValueError`. It derives from `Exception`
rather than a more specific built-in, so an existing `except RuntimeError` or
`except sqlite3.Error` handler cannot silently absorb a refusal the owner is
meant to see. `Memory.initialize()` raises it for a database written by a
newer build, and `run_cli()` prints its message.

## Reasoning Interface

`core/reasoning.py` is the provider-independent contract for Maria's one
owner-controlled local model. It contains no transport, no model, and no
provider; the conversation module is the one caller that asks a provider
anything. Its own imports include no network, threading, asynchronous, or
subprocess module, and it imports nothing from Maria except `core.context`,
`core.failures`, and `core.logger`, so it has no path to planning,
permissions, tool dispatch, or persistence.

`ReasoningRequest(prompt: str, history: tuple[ReasoningExchange, ...] = (),
context: tuple[ContextItem, ...] = (), proposal_instructions: bool = False)`
and `ReasoningResponse(text: str)`

Frozen dataclasses. The prompt and the response text must each be an exact,
non-empty, UTF-8-encodable `str` of at most 1,048,576 UTF-8 bytes
(`REASONING_MAXIMUM_INPUT_BYTES` and `REASONING_MAXIMUM_OUTPUT_BYTES`);
anything else raises `ValueError` with a fixed message that never repeats the
value. Sizes are bytes, not tokens, so no runtime's tokenizer is assumed.
Because every encodable character takes at least one UTF-8 byte, text with
more characters than a limit is refused without being encoded, so text of any
length is never copied just to be measured.
`history` holds the earlier exchanges of the current conversation, oldest
first, and is empty by default. It must be an exact `tuple` of exact
`ReasoningExchange` values, and the prompt with every exchange's text, the
rendered context, and any selected instructions must together be at most
`REASONING_MAXIMUM_INPUT_BYTES`, or `ValueError(REASONING_HISTORY_MESSAGE)`
is raised. `context` holds the records Maria selected, Tier A memories and
then Tier B search matches, empty by default, and must satisfy
`context_content_bytes` (see "Context"), or
`ValueError(CONTEXT_MESSAGE)` is raised. `proposal_instructions` selects
Maria's fixed `PROPOSAL_INSTRUCTIONS` and carries no text of its own; it must
be exactly `True` or `False`, or
`ValueError(REASONING_PROPOSAL_INSTRUCTIONS_MESSAGE)` is raised, and it is
`False` by default, which leaves a request exactly what it was before the flag
existed. There is no field for a system prompt or a template a caller could
fill, raw interaction history, a credential, a header, an endpoint, a port, or
a model path.
`repr()`, `str()`, and formatting show `<redacted>` rather than any text.

`ReasoningExchange(user: str, assistant: str)`

One completed turn of the conversation: what the owner asked and the reply
the model gave. A frozen dataclass; `user` must be exact, non-empty, encodable
text within `REASONING_MAXIMUM_INPUT_BYTES`, and `assistant` within
`REASONING_MAXIMUM_OUTPUT_BYTES`, or `ValueError(REASONING_EXCHANGE_MESSAGE)`
is raised. It is two strings and nothing more, redacted from every
representation.

`PROPOSAL_INSTRUCTIONS`, `INSTRUCTION_SEPARATOR = "\n\n"`,
`proposal_instruction_bytes(with_context: bool) -> int`, and
`system_instructions(context, proposal_instructions) -> str | None`

`PROPOSAL_INSTRUCTIONS` is Maria's fixed text, 1,015 ASCII bytes, inviting the
model to answer a request for an allowed action with exactly one proposal: one
bare JSON object of `tool`, `action`, and `arguments`. It lists exactly the
compiler's allowlist, calls earlier conversation and supplied memories or
notes data rather than authorization, and asks for a conversational reply
that does not start with `{` when no allowed action fits. It never contains
stored text, request text, or anything a model wrote. `system_instructions`
returns the one system message a request carries: `None` with neither the
flag nor context, the context instructions alone with context only,
`PROPOSAL_INSTRUCTIONS` alone with the flag only, and with both,
`PROPOSAL_INSTRUCTIONS`, `INSTRUCTION_SEPARATOR`, and the context
instructions, in that order. `proposal_instruction_bytes` is what the
instructions add to a request's size: 1,015 bytes, plus 2 for the separator
when the request also carries context, whose framing is counted with the
context.

`request_content_bytes(prompt, history, maximum_bytes, context=(),
proposal_instructions=False) -> int` and
`history_content_bytes(history, maximum_bytes) -> int`

The one definition of a request's size: the UTF-8 length of the prompt, plus
the UTF-8 length of every exchange's `user` and `assistant` text, plus the
fully rendered context, which is nothing when the context is empty, plus, when
`proposal_instructions` is true, `proposal_instruction_bytes`: the fixed
instructions and, with context, the separator that joins them to the context
framing. No role name, message structure, other separator, model identifier,
or transport framing counts. Every text is read again rather than trusted, and
counting stops as soon as the total passes `maximum_bytes`, so the result is
then only some value above the bound. A prompt that is not exact, encodable,
non-empty text raises `ValueError(REASONING_PROMPT_MESSAGE)`; a flag that is
not exactly `True` or `False`, checked next, raises
`ValueError(REASONING_PROPOSAL_INSTRUCTIONS_MESSAGE)`; a history that is
not an exact tuple of exact exchanges holding such text raises
`ValueError(REASONING_HISTORY_MESSAGE)`. Request construction,
`request_reasoning`, the adapter, and the conversation module all measure with
it.

`ReasoningTransport`

A `str` enum naming where the one reasoning host may be, not how Maria
reaches it: `LOOPBACK` (`loopback`: `127.0.0.0/8`, `::1`) and `PRIVATE_LAN`
(`private_lan`: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `fc00::/7`).
It selects no protocol, port, runtime, or library.

`ReasoningSettings(enabled, transport=None, host=None, model=None,
request_deadline_seconds=None, max_input_bytes=None, max_output_bytes=None)`

The validated provider settings, as a frozen dataclass. Disabled settings
must carry no other value; `DISABLED_REASONING` is the one disabled instance
and the default of `MariaConfig.reasoning`. Enabled settings require every
field, each checked by exact type with no default:

| Field | Accepted |
| --- | --- |
| `transport` | a `ReasoningTransport` member |
| `host` | an IP address literal, in canonical form, inside the transport's networks; no host name, port, scope, or IPv4-mapped IPv6 address |
| `model` | an opaque label of 1 to 128 ASCII letters, digits, or `. _ : / @ + -`, starting with a letter or digit; a leading single letter and colon is refused, so no Windows drive-qualified path such as `C:/models/local.gguf` or `C:models` is accepted, while `publisher/model:tag` is |
| `request_deadline_seconds` | an `int` from 1 to 300 |
| `max_input_bytes` | an `int` from 1 to 1,048,576 |
| `max_output_bytes` | an `int` from 1 to 1,048,576 |

`bool` is refused wherever an `int` is required. Each violation raises
`ValueError` with a fixed per-field message. Once enabled, the settings are
machine-specific: `repr()` and `str()` show only
`ReasoningSettings(enabled=True, <redacted>)`.

`ReasoningProvider`

The protocol the local adapter, `OllamaReasoningProvider` (see "Local Ollama
Adapter"), implements: `respond(request) ->
ReasoningResponse`, or raise `TypedFailureError` carrying one of the seven
`REASONING_FAILURE_KINDS`. A provider enforces its own hard deadline and
output budget, reports a timeout, an oversized reply, or a cut-off reply as
that typed failure rather than returning partial text, and never puts a
prompt, a reply, its host, its model identifier, or a runtime's own error
text into an exception or a log line.

`request_reasoning(provider, request, settings, logger=None) ->
ReasoningResponse | Failure`

The one boundary through which a provider is asked anything. Settings and
the request must be exact types, or `ValueError` is raised before the
provider is reached. Then, in order:

1. Disabled settings return `REASONING_UNAVAILABLE` without asking the
   provider.
2. A request whose prompt, history, context, and any selected instructions
   together, measured again by `request_content_bytes`, exceed
   `max_input_bytes` returns `REASONING_INPUT_OVER_BUDGET` without asking the
   provider. A prompt, history, context, or flag tampered into anything a
   request cannot hold raises `ValueError`.
3. The provider is asked exactly once. A `TypedFailureError` of exactly that
   type, carrying a reasoning kind, returns that kind. Any other typed failure,
   a derived or tampered carrier, and every other `Exception` return
   `REASONING_UNAVAILABLE`; the exception is never bound to a name, so neither
   it nor its text can reach the result or the log. `KeyboardInterrupt` and
   `SystemExit` propagate.
4. A result that is not exactly a `ReasoningResponse` holding non-empty,
   encodable, exact `str` text returns `REASONING_MALFORMED_OUTPUT`. The text is
   read again rather than trusted from construction.
5. Text over `max_output_bytes` returns `REASONING_OUTPUT_OVER_BUDGET`; it is
   discarded whole, never truncated. Text with more characters than the budget
   is judged over it before any encoding, even when it would also fail to
   encode, so a reply enlarged after construction is never copied to be
   measured. The request check in step 2 is bounded the same way.
6. Otherwise a fresh `ReasoningResponse` holding the same text is returned.

The reply is untrusted, inert text: nothing in this module parses, repairs,
or acts on it. Beyond it, the conversation module only checks, in proposal
mode and without parsing, whether a reply opens with `{` after JSON
whitespace, and Brain hands such a proposal attempt to the compiler; see
"Model-Originated Plans". The log records
`Reasoning request completed.` or
`Reasoning request failed: code=<code>`, and nothing else.

### Reasoning configuration and the local override

`load_config(config_path=CONFIG_PATH, local_override_path=None) ->
MariaConfig`

`MariaConfig.reasoning` holds the settled `ReasoningSettings`. It is the last
field and defaults to `DISABLED_REASONING`, so direct constructions that do
not name it keep working.

The tracked file's `[reasoning]` table is optional. When absent, reasoning is
disabled. When present, it must be a table containing exactly
`enabled = false`: a tracked file that enables reasoning, or names any other
key, raises `ValueError`. Active settings belong only in the untracked
override.

`local_override_path` is `None` by default, so an ordinary caller loads the
tracked file alone and never reads the owner's private settings.
`MariaBrain.from_defaults()` is the one caller that passes `LOCAL_CONFIG_PATH`
(`config/config.local.toml`, which Git ignores). The tracked file is settled
in full first. Then:

- When the override's directory entry does not exist, the tracked
  configuration applies unchanged. Presence is decided with `os.lstat`, so a
  dangling symbolic link is present, not absent.
- A present entry must be a regular file. A symbolic link, including a
  dangling one, a directory, and any special entry are refused before the file
  is opened. Where the platform supports it, the open neither follows a link
  nor waits on a special file, and the opened descriptor must still be the
  same regular file.
- The file must be at most 65,536 bytes of UTF-8 TOML containing exactly one
  `[reasoning]` table and no other table or key.
- That table replaces the tracked one completely: a disabled table must say
  only `enabled = false`, and an enabled table must name all six active
  fields. Nothing is merged and no missing field is borrowed. The override
  can never affect permissions, roots, paths, logging, or any other section.

Unknown keys are refused inside `[reasoning]` only; the existing rule that
unknown keys in `[app]`, `[paths]`, `[logging]`, and `[permissions]` are
ignored is unchanged. Every reasoning and override failure raises `ValueError`
with a fixed message that names no path, host, model, file content, or parser
or operating-system error text, and is raised with neither a cause nor a
context, because Python's TOML error keeps the whole document it was parsing.
Loading logs nothing.

## Local Ollama Adapter

`core/ollama_adapter.py` holds the one `ReasoningProvider` implementation: the
owner's local Ollama runtime, reached over its native HTTP API (decision
V1-13-R1). It is the only production module that imports a network module
(`socket`), and it imports nothing from Maria except `core.failures` and
`core.reasoning`. Only `main.build_reasoning_provider` constructs it, for
enabled settings alone, and Brain receives it only as a `ReasoningProvider`.

`OLLAMA_ADDRESS = ("127.0.0.1", 11434)` and `OLLAMA_CHAT_PATH = "/api/chat"`

The one fixed destination. Neither is configurable, and no setting names a
port or endpoint.

`OllamaReasoningProvider(settings: ReasoningSettings)`

Settles everything the adapter uses at construction. `settings` must be an
exact `ReasoningSettings`, or `ValueError` is raised with
`REASONING_SETTINGS_TYPE_MESSAGE`. The settings are rebuilt, which re-runs
their own validation, so values altered after construction raise that field's
fixed message; the adapter keeps its own copies, so later changes cannot reach
it. Disabled settings, any transport but `loopback`, and any host but
`127.0.0.1` raise `ValueError(OLLAMA_DESTINATION_MESSAGE)`. A model identifier
whose tag, after the last colon of its final path segment and ignoring any
`@` digest, is `cloud` or ends `-cloud`, compared without regard to case,
raises `ValueError(OLLAMA_CLOUD_MODEL_MESSAGE)`. Both messages are fixed and
name no host, port, path, or model. `repr()` is
`OllamaReasoningProvider(<redacted>)`.

`OllamaReasoningProvider.respond(request: ReasoningRequest) ->
ReasoningResponse`

Asks the runtime one question over one new connection and returns the
complete answer, or raises `TypedFailureError`. Anything but an exact
`ReasoningRequest` raises `ValueError(REASONING_REQUEST_TYPE_MESSAGE)`, a
prompt altered into anything but non-empty, encodable text raises
`ValueError(REASONING_PROMPT_MESSAGE)`, a history altered into anything
but an exact tuple of exact exchanges raises
`ValueError(REASONING_HISTORY_MESSAGE)`, a context altered into anything
invalid raises `ValueError(CONTEXT_MESSAGE)`, and a proposal-instructions flag
altered into anything but exactly `True` or `False` raises
`ValueError(REASONING_PROPOSAL_INSTRUCTIONS_MESSAGE)`. Before anything is
serialized, the prompt, history, rendered context, and any selected proposal
instructions are measured together against
`max_input_bytes` with `request_content_bytes`. The request is `POST /api/chat HTTP/1.1` with `Host`,
`Content-Type`, `Content-Length`, and `Connection: close` headers only, and
the JSON body `{"model": <model>, "messages": [...], "stream": false}`, where
the messages are each exchange's `{"role": "user", "content": <user>}` then
`{"role": "assistant", "content": <assistant>}`, oldest first, and finally
`{"role": "user", "content": <prompt>}`. With the proposal-instructions flag
off, the body with no history and no context is exactly
`{"model": <model>, "messages": [{"role": "user", "content": <prompt>}],
"stream": false}`, byte for byte as before; with no context the body is
byte for byte what V1-14 sent; and with Tier A context alone it is byte for
byte what V1-15 sent. When `system_instructions(context,
proposal_instructions)` returns text, the messages begin with `{"role":
"system", "content": <that text>}`: `PROPOSAL_INSTRUCTIONS` when the flag is
on and the context instructions when the request carries context, joined by
`INSTRUCTION_SEPARATOR` when there are both. Context then follows as
`{"role": "user", "content": <rendered data block>}`, before the
conversation; the context instructions are `CONTEXT_INSTRUCTIONS` unless a
Tier B item is present, when they are `SEARCH_CONTEXT_INSTRUCTIONS`, and no
stored text is ever placed in the system message. Brain's production path
always turns the flag on.

| Outcome | Result |
| --- | --- |
| Prompt, history, rendered context, and any selected proposal instructions together over `max_input_bytes` | `REASONING_INPUT_OVER_BUDGET`, before any connection |
| Deadline passed while connecting, sending, or receiving | `REASONING_TIMEOUT` |
| Connection refused or failed before any reply byte, or the connection closed with no reply | `REASONING_UNAVAILABLE` |
| Any status but `200`, including a missing model (`404`) and every redirect, which is never followed | `REASONING_UNAVAILABLE` |
| Reply ended or failed partway, `done` false, or `done_reason` `"length"` | `REASONING_OUTPUT_TRUNCATED` |
| Status line and headers over 16 KiB, bad status line or header field, missing or ambiguous framing, bad chunk framing, a body that is not UTF-8 JSON, or any unexpected shape | `REASONING_MALFORMED_OUTPUT` |
| Body over the envelope, declared or received | `REASONING_OUTPUT_OVER_BUDGET` |
| `message.content` over `max_output_bytes` | `REASONING_OUTPUT_OVER_BUDGET` |
| Complete reply | a `ReasoningResponse` holding exactly `message.content` |

The reply must be one JSON object whose `done` is exactly `true` and whose
`message` has `role` `"assistant"` and non-empty `content` text. A
`done_reason` is optional; when present it must be text. The framing must be
exactly one `Content-Length` of digits or `Transfer-Encoding: chunked`, never
both and never neither. A JSON document that is cut off inside a complete
frame is malformed, not truncated, because the frame says it is whole.

The deadline is absolute: `request_deadline_seconds` measured on the
monotonic clock from the start of `respond`. Every socket wait receives only
the time that remains. The status line and headers are bounded by
`MAXIMUM_RESPONSE_HEADER_BYTES` (16,384). The whole body, chunk framing and
trailers included, is bounded by `RESPONSE_ENVELOPE_OVERHEAD_BYTES` (65,536)
plus `JSON_ESCAPE_EXPANSION` (6) times `max_output_bytes`, and a declared
length or chunk size over what remains is refused before it is read. Every
failure is raised outside any exception handler, so it has neither a cause
nor a context, and the connection is closed on every path. The adapter logs
nothing; used through `request_reasoning`, the log records only the outcome
and failure code.

## Tool Manager

`ToolManager.prepare_confirmation(plan: Plan) -> PreparedConfirmation`

Returns `PreparedConfirmation(plan, previews)`: the plan to present for
approval and the owner-visible previews to show beside it. Brain calls it once,
when a deterministic plan is about to wait for the owner's answer, so the approved plan — and
the fingerprint the session binds approval to — already names the exact state
each step acts on. A tool may provide an optional `confirmation_binding(action,
arguments)` that returns either a mapping of arguments to add or a
`ConfirmationBinding(arguments, preview=None)`, whose `preview` is text for the
owner read in the same read as the state its arguments pin; Tool Manager adds
the arguments to that step only and returns the previews, in plan order,
outside the plan. A preview never enters the plan or its fingerprint.
`ConfirmationBinding` refuses anything but a mapping and optional text with
`ValueError`, and neither value object's `repr()` shows its contents. A binding
can never replace an existing argument (that raises `ValueError`), change a
step's tool or action, or add or remove a step. A binder that raises is
contained, logged as the fixed line `Confirmation binding failed:
<tool>.<action>`, and adds nothing, so a tool relying on a binding must treat
an unbound step as unapproved. A plan no tool binds is returned as the same
object. The contract check at confirmation validates whatever was added. A
model-originated plan is approved exactly as the compiler built it, so Brain
never passes one here, and a plan whose origin is not `DETERMINISTIC` raises
`ValueError("A model-originated plan is approved exactly as compiled and is
never prepared for confirmation.")`.

`ToolManager.execute_tool(tool_name, action, arguments) -> ToolExecutionResult`

Tools must expose `name`, `description`, `actions`, and an `execute` method.
`arguments` is typed as a read-only `Mapping[str, Any]`, so a tool receives a
plan's immutable arguments directly rather than a mutable copy. Tool Manager
forwards the mapping unchanged and never rebuilds it, and no built-in tool
mutates it.

`execute_tool` validates the action contracts first and unconditionally, so a
direct caller cannot reach a tool with an unsupported action or a malformed
argument. A violation returns an unsuccessful `ToolExecutionResult` carrying
the violation's fixed message, and no tool is invoked. The registry used is
injected through the optional `contracts` constructor argument and defaults to
`BUILT_IN_ACTION_CONTRACTS`. Brain validates a whole plan earlier through
`validate_action()`, against that same active registry, so that ordering holds
across a plan; this pass is the defensive one no caller can skip. For a
model-originated plan Brain hands that same registry, `contracts`, to
`compile_proposal` and `recheck_model_plan` instead, so the model path's
eligibility and contract checks use no other registry either.

`ToolManager.validate_action(tool_name, action, arguments) -> ContractViolation | None`

Reports the first violation for one call without executing anything.

`ToolManager.loaded_action_identifiers() -> frozenset[str]`

Returns a read-only snapshot of every loaded tool action, used to check the
declared registry against the tools actually loaded. The registry is never
populated from this snapshot.

An unexpected exception raised by a tool is contained at this boundary
rather than crashing Maria, and neither the exception nor its text reaches
the owner or the log. The result is an unsuccessful `ToolExecutionResult`
carrying the shared failure's fixed `Tool execution failed.` message, no
`data`, and no `recorded_message`, while the log records the validated tool
name, the validated action name, and the typed failure code only. The
exception is never bound to a name, so no traceback, `str()`, or `repr()`
of it can be formatted into either. Contract refusals, the not-loaded and
unsupported-action messages, and every tool's own controlled errors are
unaffected and keep their existing specific text.

## Permissions

`PermissionManager.check(tool_name, action) -> PermissionResult`

Permission results are one of `allowed`, `requires_confirmation`, or `denied`.
Decisions follow denied, confirmation-required, then allowed precedence. A
confirmation-required reason is `"<tool>.<action> requires explicit
confirmation."`, followed, for an action listed in `CONFIRMATION_NOTICES`, by
that action's fixed notice. `memory.enable_context` has one stating that an
enabled memory may be sent to the local reasoning model on any conversational
turn, within Maria's memory context limits, and that Maria does not choose
memories by relevance. `notes.enable_context` has one stating that the note's
entire title and body may be sent on a later conversational turn when a word
of four or more characters from the request appears literally in its title or
its body, within Maria's context limits, and that Maria does not check the
note for passwords or other secrets. `audit.delete` has one stating that
deleting an audit summary removes it from Maria's database, that this does
not undo the action, and that Maria's operational log may still show that a
model-proposed action ran.

`load_config` requires a present `[permissions]` value to be a TOML table and
validates `require_confirmation` and `deny` as arrays of lowercase
`tool.action` identifiers. Each identifier contains exactly one period, and
its tool and action segments start with a letter and otherwise contain only
lowercase ASCII letters, digits, or underscores. Duplicate identifiers within
one array are rejected, while syntactically valid identifiers for future tools
are retained. Malformed permission values raise `ValueError`.

Missing permission arrays and explicitly empty arrays use the mandatory secure
baseline: `audit.delete`, `filesystem.rename`, `filesystem.move`, `history.delete`,
`notes.retitle`, `notes.edit`, `notes.delete`, `notes.enable_context`,
`notes.disable_context`, `memory.update`, `memory.forget`,
`memory.enable_context`, and `memory.disable_context` require confirmation,
while `filesystem.delete` and `filesystem.write` are denied.
Configuration can add restrictions but cannot remove this baseline.
`PermissionManager` applies the same mandatory policy when constructed
directly, and denial wins if an action also requires confirmation.

The one approval every model-originated plan needs is Brain's own requirement,
not an entry here: `PermissionManager` decides `calculator.calculate`,
`filesystem.list`, and `system.current_time` exactly as before, allowing them
by default, and a configuration that denies one makes Brain refuse a proposal
of it before any approval is offered.
