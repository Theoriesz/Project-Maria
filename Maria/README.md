# Maria

Maria is a modular personal AI assistant foundation written in Python.

Maria focuses on architecture, safety, and maintainability. It is not a
prototype of the final assistant. Its optional use of a local language model
is deliberately narrow and off by default, and nothing the model proposes runs
without the owner's approval.

## Requirements

- Python 3.11 or newer
- SQLite, included with Python

## Setup

From the `Maria` directory:

```text
python -m pip install -r requirements.txt
python main.py
```

## CLI Exit and Interruption Behavior

Normal CLI completion, including `exit`, `quit`, `q`, EOF, or Ctrl+C at the
main prompt, returns status 0. A startup failure prints a stable, sanitized
message to stderr and returns status 1; startup Ctrl+C prints an interruption
message and returns status 130. Startup failures do not print raw tracebacks or
exception details. Review the configuration and available logs when startup
fails.

Most startup failures share one general message. A database written by a newer
version of Maria is the exception: it is refused with its own fixed message,
still with status 1 and still without any path, record, or underlying error
text, because half-working against a schema this build does not understand
would be worse than stopping. Nothing is written to the database on that path.

At a confirmation prompt, `y` or `yes` approves and `n` or `no` cancels the
pending action, ignoring casing and surrounding whitespace. Blank or
unrecognized input re-prompts instead of approving or cancelling. EOF or
Ctrl+C cancels the pending action immediately, without executing it, even
after earlier invalid input. Ctrl+C returns to the main prompt; if input
remains closed after EOF, the next main prompt exits cleanly.

## Supported Commands

```text
calculate 2 + 2
list .
read file README.md
rename old.txt to new.txt
move source.txt to docs/source.txt
create note Idea: Keep the foundation simple.
notes
read note 1
retitle note 1: Clearer idea
edit note 1: Keep the foundation simple and reviewable.
delete note 1
enable note context 1
disable note context 1
remember My editor is VS Code
recall editor
memories
update memory 1: My editor is Neovim
forget memory 1
enable memory context 1
disable memory context 1
system
time
history
search history editor
read interaction 1
delete interaction 1
audit
read audit 1
delete audit 1
```

File rename and move operations, `delete interaction <id>`, the three note
lifecycle commands `retitle note <id>: <new title>`, `edit note <id>: <new
body>`, and `delete note <id>`, the two note context consent commands
`enable note context <id>` and `disable note context <id>`, the two memory
lifecycle commands `update memory <id>: <new fact>` and `forget memory <id>`,
the two memory context consent commands `enable memory context <id>` and
`disable memory context <id>`, and `delete audit <id>` require explicit
confirmation. `delete interaction <id>` targets at most one interaction row by
ID and removes it when found; it never cascades to notes or memories. Each note
lifecycle command likewise targets at most one note by positive whole-number ID
only, never by title, and never cascades into interaction history or explicit
memories. Each memory lifecycle command targets at most one memory by positive
whole-number ID only, never by fact text, and never cascades into interaction
history or notes. `delete audit <id>` targets at most one audit summary by
positive whole-number ID only and removes it together with its recorded
outcome; it never cascades into interaction history, notes, or memories.
The thirteen ID-only planner commands (the eleven `<id>` commands above, plus
`read interaction <id>` and `read audit <id>`) accept a whole number from 1 to
9223372036854775807, the largest integer SQLite stores, with any number of
leading zeroes; each answers a larger number with its invalid-ID reply before
anything is planned or confirmed. `read note <id or exact title>` uses its
separate, unchanged lookup path.
Retitling and editing are deliberately separate commands, so neither can
overwrite the other's field. Filesystem delete functionality remains
intentionally unimplemented.

With reasoning enabled, every action the local model proposes also needs your
approval before it runs, even when the matching command would not ask; see
"Model-Proposed Actions".

## Filesystem Access

Maria reads, lists, renames, and moves files only inside named filesystem roots
declared in `config/config.toml`. The shipped declaration is exactly two:

| Name | Configured path | Policy | Enabled |
|---|---|---|---|
| `workspace` | `~/MariaWorkspace` | `organize` | yes |
| `project_maria` | `..` (Maria's repository root) | `read_only` | no |

`workspace` is the default root, so a relative path such as `inbox/order.txt`
resolves inside it. An absolute path is accepted only when it falls inside an
enabled root. Maria creates a missing enabled organizing root at startup; it
never creates a read-only root, and it never creates a disabled one.

`project_maria` is declared but disabled, reserved for a later capability. A path
beneath it is refused exactly like any path outside Maria's roots, and the refusal
names neither the path nor the root.

A `read_only` root permits `read` and `list` only. Rename and move are refused
there, and a rename or move whose source and destination are in different roots is
refused as well, even when both roots organize.

Some locations are permanently out of reach of every filesystem command:

- Maria's live database file and its SQLite `-journal`, `-wal`, and `-shm`
  sidecars, wherever `[paths] database` puts them
- Maria's live log directory and everything beneath it, including rotated logs,
  wherever `[paths] logs` puts it
- Maria's active system prompt, wherever `[paths] system_prompt` puts it
- the default `database/` and `logs/` directories inside Maria's own project
- the repository's `.git`, whether it is a directory or a gitdir-pointer file
- `config/config.local.toml`, the private local override described under
  "Local Reasoning Settings", which Maria never creates and reads only at
  startup
- inside Maria's repository, at any depth: `.env` and any `.env.*` file,
  `__pycache__`, `.venv`, `venv`, `env`, `.pytest_cache`, `.mypy_cache`,
  `.ruff_cache`, `maria_cli_test`, `.claude`, and any name ending `.tmp` or
  `.bak`

`.env.example` is the one exemption, and only as an exact final path component.

The first three follow the running configuration rather than Maria's default
layout, so pointing `[paths]` at somewhere inside your workspace keeps those
locations protected there too. Each covers itself and anything beneath it, which
matters only if one of those names turns out to be a directory rather than a file.
An unrelated file that merely sits beside the database or the prompt — such as
`maria.db.notes` — stays usable, and so does the directory holding them.

Renaming and moving go further than reading. A directory that *contains* the live
database, one of its sidecars, or the system prompt cannot be renamed or moved, and
neither can an ancestor of the live log directory, because relocating a directory
would carry those files along with it. Reading and listing such a directory still
work normally, with the protected entries filtered out; it is only the mutating
commands that refuse. An ordinary directory with nothing protected inside it
renames and moves as usual.

Separately, Maria's whole repository — its source, documentation, prompts, shipped
configuration, and any untracked file beside them — is unavailable to the generic
`read`, `list`, `rename`, and `move` commands. Declaring a root around the
repository does not change that.

Each rule is applied both to the path you asked for and to the path it actually
resolves to, so a `..` traversal, a symbolic link, or a Windows junction cannot
spell its way past them in either direction: a protected name that points outward
is still refused, and an ordinary name that points at something protected is
refused too. The repository-scoped name rules stay scoped to Maria's repository,
so your own workspace may hold an `env`, `venv`, or `.claude` directory and
`notes.tmp` or `archive.bak` files, and Maria can still use them.

A refused path produces one of four fixed messages that repeat no path, home
location, repository location, root name, or excluded filename:

```text
Path is outside Maria's configured safe root.
Path is permanently excluded from Maria's filesystem access.
This filesystem root is read-only.
Source and destination must be inside the same filesystem root.
```

Filesystem delete and write functionality remains intentionally unimplemented.

## Local Reasoning Settings

With the shipped configuration Maria uses no language model, and every command
works exactly as it always has. Reasoning is off until the owner enables it in
a private local override. Maria then answers input that matches no command
with the owner's local Ollama model, which may reply conversationally or
propose one of three allowed actions; a proposed action runs only if the owner
approves it. See "Local Ollama Adapter", "Conversation", and "Model-Proposed
Actions" below.

The tracked `config/config.toml` keeps reasoning disabled:

```toml
[reasoning]
enabled = false
```

That block must stay exactly that way. Startup is refused if the tracked file
enables reasoning or names any provider value, because such values are specific
to one machine. They belong only in `config/config.local.toml`, a private file
that Git ignores, that Maria never creates, and that no filesystem command can
read. When it is absent, the shipped disabled configuration applies. When it is
present, it must be a regular file — not a link, directory, or special entry —
holding exactly one `[reasoning]` table and nothing else, and that table
replaces the tracked one completely:

```toml
[reasoning]
enabled = true
transport = "loopback"          # or "private_lan"
host = "127.0.0.1"              # an IP address inside that class
model = "example-model:tag"
request_deadline_seconds = 60   # 1 to 300
max_input_bytes = 65536         # 1 to 1048576
max_output_bytes = 16384        # 1 to 1048576
```

An enabled block must name all six active fields; none has a default and none
is borrowed from the tracked file. `loopback` admits `127.0.0.0/8` and `::1`;
`private_lan` admits `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, and
`fc00::/7`. The host is written as an address, never a name, in its canonical
form. The model identifier is an opaque label of 1 to 128 ASCII letters,
digits, or `. _ : / @ + -`, starting with a letter or digit and never naming
a Windows drive, so `publisher/model:tag` is accepted while
`C:/models/local.gguf` is refused. There is no setting for a
port, endpoint URL, model path, credential, token, or header, and any
unknown key is refused. A missing, mistyped, out-of-range, or unknown value
stops startup with the usual sanitized failure message, and nothing about the
file, its location, or its values is printed or logged.

### Local Ollama Adapter

`core/ollama_adapter.py` is Maria's one reasoning adapter and the first code in
Maria's runtime that can use the network. It talks only to Ollama's native HTTP
API on the same machine: one `POST /api/chat` request, with streaming off, to
the fixed loopback address `127.0.0.1:11434`. The port and path are fixed in
the adapter and are not settings. The adapter refuses to exist unless the
local override names `transport = "loopback"` and `host = "127.0.0.1"`, and it
refuses a model identifier that names an Ollama cloud model, such as one
tagged `cloud` or ending `-cloud`. It never resolves a host name, uses a
proxy, follows a redirect, retries, or falls back to anything else, and it
never downloads a model, installs software, or starts a process.

It sends only the model identifier, Maria's fixed instructions, and the
conversation. The instructions come first, as the one system message, and
never contain your stored text: Maria's proposal instructions, which every
request carries (see "Model-Proposed Actions"), followed by her context
instructions when memories or notes you have enabled for context go with the
request. That stored text then follows as a separate, clearly marked data
message. Next come the current session's earlier conversational exchanges as
alternating user and assistant messages, and then the request as the final
user message. It takes back only the answer text. Any separate thinking text
and any tool calls in the reply are ignored after bounded JSON parsing; only
`message.content` is returned, as text, and the adapter never acts on it.
What Maria does with an answer that proposes an action is described under
"Model-Proposed Actions". One hard deadline covers connecting, sending, and
receiving the whole reply; the reply is size-bounded before it is parsed, and
the answer must fit within `max_output_bytes` or is discarded whole. Every
failure is one fixed, typed message, and no request, answer, address, model
identifier, or Ollama error text reaches Maria's log.

Maria constructs the adapter only when the private override enables reasoning,
and reasoning stays disabled by default. Before the adapter is ever used with a
real model, Ollama itself must be put in local-only mode, as Ollama documents
it, and restarted, so it cannot reach its cloud models. Maria's refusal of
cloud-tagged model names is a second line of defense, not proof that Ollama is
running local-only.

### Conversation

With reasoning enabled, Maria still tries every request as a command first,
exactly as before. Only input that matches no command, the input that would
otherwise get `I do not have an executable plan for that yet.`, goes to the
local model instead. Help, usage messages, and every command are unchanged.
Input that begins like a command is always that command, even when phrased
conversationally: `remember when we ...` stores a memory, and
`move on to ...` is read as a file move and asks for confirmation.

A reply is conversational text unless its first character, after only spaces,
tabs, carriage returns, and line feeds, is `{`: such a reply is a proposed
action, handled as described under "Model-Proposed Actions", and is never
shown or kept as conversation. Maria never treats a reply as a command or an
approval, and no reply can change a record or a file or confirm anything,
whatever it says; a proposed action runs only if you approve it. That includes
claims to have done something: a reply saying "I saved that" saves nothing.
Use the commands above to act.

Within one session the model sees Maria's fixed instructions, the current
request, the earlier conversational turns of that session, so a follow-up
question is understood, the explicit memories you have enabled for context,
and notes you have enabled for context that contain a word of your request
(see "Memory Context" and "Note Context and Search" below). It never sees a
command or its result, an earlier proposal or what became of it, a memory or
note you have not enabled, a file, an audit summary, the interaction history,
or anything from an earlier session. Maria keeps at most the last eight
exchanges, and at most 8 KiB of their text together with the current request,
Maria's fixed proposal instructions, and any context, or `max_input_bytes` if
that is smaller. The request and the instructions are fitted first, then any
context, then the newest exchanges; the instructions take 1,015 bytes and are
never left out, and the oldest exchanges are dropped first, whole. A request
too large to send beside the instructions, or a reply too large to keep beside
its request, is refused with a fixed message rather than cut short, and an
over-large request is never sent to the model. The conversation lives only in
memory and is discarded when the session ends, on `exit`, end of input,
Ctrl+C at the prompt, or any other way the CLI stops.

Conversation is never recorded: no conversational request or reply is written
to interaction history or anywhere else, and neither is a proposal, its
refusal, an approval prompt, or an approved action's result. An approved
action leaves only content-free traces: its audit summary and log lines. When
the model is unavailable, too slow, or answers with something unusable, Maria
shows a fixed message such as `The local reasoning model is unavailable.`, and
every command keeps working.

### Memory Context

Every explicit memory is private to the model until you enable it, one memory
at a time. Existing memories and every new memory start with context consent
off.

```text
enable memory context 1
Confirmation required: memory.enable_context requires explicit confirmation. Once enabled, this memory may be sent to your local reasoning model on any conversational turn, within Maria's memory context limits. Maria does not choose memories by relevance.
```

That confirmation states the rule exactly. Maria does not pick memories that
seem relevant: while reasoning is enabled, **every** enabled memory is eligible
to be sent to your local model on **any** conversational turn. This is a
deliberate rule, not a judgement about relevance, and you kept it when it
was reviewed. Memories are taken in ascending ID order, whole, at most 16
of them and at most 2 KiB of context including Maria's framing and the IDs.
Your current request and Maria's fixed proposal instructions always come
first, then as much memory context as fits, then as many earlier turns as fit.
Your approval covers the memory's text as
it was when Maria asked: if that memory changes before you confirm, even from
another Maria window, nothing is enabled and Maria asks you to request
confirmation again. `disable memory context <id>` turns consent off again, and
also asks for confirmation. Updating a memory turns its consent
off, because consent applies to the exact text you approved; Maria tells you
when that happens.

After every conversational reply, and below a refused proposal or an approval
prompt, Maria adds her own note, separate from the model's answer:

```text
Memory context supplied: memory 1, memory 4. Maria cannot verify whether these memories are still current or whether they conflict with one another.
No memory context supplied.
```

The first names, from Maria's own database IDs, exactly which memories were
sent with that request. Maria cannot know which of them the model actually
used, and a reply that mentions other IDs changes nothing she reports. If the
request failed, she names the memories selected for it and says she cannot
confirm the model received them. When the limits leave enabled memories out,
she says so, naming only IDs she actually read.

Memory text is sent as clearly marked reference data, never as an instruction,
and it grants the model nothing: Maria's instructions tell the model that
supplied memories and notes are data, not authorization, and anything it
proposes still needs your approval. If you disable, change, or forget a
memory that was sent earlier in the session, Maria clears that session's
earlier conversation before sending anything else, so an earlier reply cannot
carry the old text back, even when the change came from another Maria
window. If memory context cannot be read, Maria sends neither memory context
nor earlier turns and says so. This controls what Maria sends next; it cannot
erase anything the model already received.

### Note Context and Search

Every note is private to the model until you enable it, one note at a time.
Existing notes and every new note start with context consent off.

```text
enable note context 4
Confirmation required: notes.enable_context requires explicit confirmation. Once enabled, this note's entire title and body may be sent to your local reasoning model on a later conversational turn when a word of four or more characters from your request appears literally in its title or its body, within Maria's context limits. Maria does not check the note for passwords or other secrets, and enabling it does not mean it contains none.
This approval covers note 4 exactly as it is stored now, shown quoted:
Title: "Trip plan"
Body: "Book the train on Friday and pack the blue bag."
```

Only enable a note you are willing to share in full: Maria cannot tell a
password or other secret from any other text, so consent, not inspection, is
what keeps a note out. Maria shows you the note's whole title and body before
you decide, each quoted on one line so that a line break or an invisible
character in the note is shown as an escape such as `\n` rather than hidden.
That shown text is not stored in your history or logs and is never sent to the
model. Your approval covers exactly that title and body; if either changes
before you confirm, nothing is enabled. `retitle note` and `edit note` turn a
note's consent off and say so, and a note changed any other way is never sent
under its old approval. `disable note context <id>` turns consent off again,
and also asks for confirmation.

While reasoning is enabled, Maria searches the notes you enabled, and any
enabled memories beyond the 16 that are always eligible, for the words of your
current request. The search is literal: each word is casefolded, loses
punctuation at its ends, and counts only at 4 to 64 characters, and at most
the first 8 distinct words are used. A short request such as "Jo" or "AI"
searches nothing. A note matches when one of those words appears in its title
or in its body, never across the two, and only while both are still exactly
the text you approved; a note whose title or body is over 2 KiB is never
searched or named, because Maria cannot confirm it is the approved text without
reading all of it. Nothing is ranked, fuzzy, or chosen by the model. At most 8
matches are sent, memories before notes, each whole, and
everything sent is at most 24 records and 4,096 bytes together with your always
eligible memories.

```text
Found by search and supplied: memory 21, note 4. Maria cannot verify whether these records are still current or whether they conflict with one another.
Search matches not included because of the context limits: note 9.
Possible divergent versions: note 4 and note 7 have the same title and different bodies. Maria cannot tell which, if either, is current.
```

Maria names every record she sent by its kind and ID, names matches the limits
left out, and names notes that share a title but differ as possible divergent
versions; she never calls one of them wrong or stale. If a note or memory sent
earlier in the session is changed, disabled, deleted, or forgotten, Maria
clears that session's earlier conversation before sending anything else and
tells you the earlier copy was invalidated. She rechecks what she sent before
every conversational request, which sees the current state only: a record
changed and changed back in between looks unchanged.

### Model-Proposed Actions

Every request Maria sends to the local model carries her fixed instructions
saying which actions she accepts. When the model's reply opens with `{`, after
only spaces, tabs, carriage returns, and line feeds, Maria reads it as a
proposal of exactly one action. A reply that does not open that way, such as
one that puts a proposal after other words or inside a code fence, is ordinary
conversational text, and nothing in it runs. The model is asked once per
request, and only while reasoning is enabled; with the shipped configuration
no model proposes anything.

Only three actions can be proposed:

- `calculator.calculate`, with an `expression`
- `filesystem.list`, with no arguments to list the default root, or with a
  `path` relative to it
- `system.current_time`

A proposal must be exactly one JSON object holding only `tool`, `action`, and
`arguments`, at most 4,096 bytes, with nothing but whitespace around it, and
its arguments must meet the same rules as the matching command's. No text
argument may contain a control, formatting, surrogate, or line or paragraph
separator character, and a path must be plain relative text below the default
root: never absolute or just `.`, and with no drive, colon, leading `~`, part
made only of dots and spaces such as `..`, or Windows device name such as
`NUL`. When a listing runs, the usual root and exclusion rules apply to it as
to any `list` command. Anything else is refused with one fixed message, such
as `Maria does not accept that action from the reasoning model.`, followed by
Maria's notes about context. Nothing is repaired, and the model is not asked
again.

A proposal that passes is shown to you, and it runs only if you approve it
with `y` or `yes` at the usual confirmation prompt. That holds for all three
actions, even though the shipped `calculate`, `list`, and `time` commands run
without asking. Each proposal needs exactly one approval, whatever your
configuration says about confirming that action, and an action your
configuration denies is refused before any approval is asked for. For
example, if the model answers the request below by proposing a calculation,
Maria shows:

```text
What is six times seven?
Maria's local reasoning model proposed this action. It runs only if you approve it.
Tool: calculator
Action: calculate
Arguments: {"expression": "6 * 7"}

No memory context supplied.
```

Once you approve it, Maria runs it and shows:

```text
6 * 7 = 42

Recorded as audit summary 1.
```

The approval names the tool and the action and shows every argument exactly,
as one JSON object with the names sorted, and `{}` when there are none;
nothing is shortened. A quotation mark or backslash inside a text value is
preceded by a backslash, and every character outside printable ASCII, and
every space that leads, trails, or repeats, is written as an escape: an `e`
with an acute accent appears as `\u00e9`, and a doubled space as
`\u0020\u0020`. What you see is therefore plain, printable text that decodes
as JSON to exactly the value Maria will use.

Your approval covers exactly the plan you were shown. Nothing asks the model
again or plans the request again: just before the action runs, Maria checks
that same plan once more against the three allowed actions, their argument
rules, and your current permissions, and it runs at most once. Cancelling at
the prompt runs and records nothing. Maria's notes about context are shown
once, with the approval prompt or the refusal, and are not repeated when you
approve.

Immediately before an approved action runs, Maria commits its audit summary
(see "Audit Summaries"). If that summary cannot be recorded, for example
because the database stays busy, the action does not run, Maria says so, and
your approval is used up: make the request again if you still want it. After
the action runs, Maria adds whether it succeeded or failed to that summary and
follows the result with `Recorded as audit summary N.`; if that outcome cannot
be recorded, she says so instead, and the summary shows the outcome as
unknown. Nothing is retried. The request, the proposal, the refusal or
approval prompt, and the result never become conversation or interaction
history, so on a later turn the model sees neither the proposal nor its
result.

## Audit Summaries

An audit summary is Maria's record of one approved model-proposed action. It
holds only values Maria generates herself: the UTC time she authorized the
attempt; the action, such as `calculator.calculate`; that you approved it; a
target of `named filesystem root` for a directory listing or `none`
otherwise, never which root or path; and, once the action has run, whether it
succeeded or failed by the tool's own report. It never holds your request,
the model's reply, an argument such as an expression or a path, a root's
name, a host path, the result, or any memory or note.

- `audit` lists the newest 20 summaries, newest first, and says when more
  exist.
- `read audit <id>` shows one summary.
- `delete audit <id>` removes one summary together with its outcome, after
  confirmation.

```text
audit
2: 2026-10-01T09:47:02Z filesystem.list (target: named filesystem root). Approved by the owner. Outcome not recorded; execution is unknown.
1: 2026-10-01T09:46:17Z calculator.calculate (target: none). Approved by the owner. Outcome: succeeded.

read audit 1
Audit summary 1
Created at: 2026-10-01T09:46:17Z
Action: calculator.calculate
Target: none
Decision: approved by the owner
Outcome: succeeded

delete audit 1
Confirmation required: audit.delete requires explicit confirmation. Deleting an audit summary removes it from Maria's database. It does not undo the action, and Maria's operational log may still show that a model-proposed action ran.
```

A summary is committed before its action runs, so on its own it does not
prove the action ran. `Outcome not recorded; execution is unknown.` means
Maria may have stopped after recording the summary but before or during the
run, or could not record how it ended; she never reruns it. Command keywords
are case-insensitive, IDs are positive whole numbers, and `audit` must be the
complete command; `list audits` still lists a directory named `audits`.
There is no search, filtering, range, editing, or bulk deletion. No audit
command can be proposed by the model, and summaries are never sent to it.
Retention and deletion are described under "Data Retention and Privacy".

## Persistent Memory

Maria stores deliberately remembered facts in SQLite, so they remain available
after the application restarts. The supported syntax is strict:

- `remember <fact>` stores one fact.
- `recall <query>` performs a literal, case-insensitive substring search.
- `memories` lists the newest stored facts.
- `update memory <id>: <new fact>` replaces one stored fact.
- `forget memory <id>` removes one stored fact.
- `enable memory context <id>` and `disable memory context <id>` turn one
  memory's consent to be sent to the local model on or off; see "Memory
  Context" under "Local Reasoning Settings".

Command keywords are case-insensitive, and surrounding whitespace is removed
from facts and queries without changing their display casing. `remember` and
`recall` must start the request directly, while `memories` must be the complete
command. `update memory` and `forget memory` must likewise start the request
directly.

`update memory` and `forget memory` identify a memory only by its positive
whole-number ID, never by fact text, and each affects at most one memory. Both
require explicit confirmation. `update memory` splits its operand on the first
colon only, so a new fact may itself contain colons. An update replaces the
stored fact while preserving the memory's original creation time; because Maria
records no separate update time, an edited memory keeps its place in
newest-first output instead of moving to the top. A new fact identical to the
stored one, or differing only by letter casing, is still a successful update —
a casing-only change rewrites how the fact is displayed.

Stored facts stay unique after Unicode case folding, so updating one memory to
a fact another memory already holds is refused with a message naming only the
two numeric IDs, and neither memory is changed, merged, or removed. Folding can
make visually different text collide: `straße` and `STRASSE` count as the same
fact.

There is no way to update or forget a memory by its text, no fuzzy or
contextual selection, no bulk, wildcard, or clear-all deletion, no automatic
pruning, and no undo, recovery, or memory revision history. Neither command
cascades into interaction history or notes, and remaining memory IDs are never
renumbered. Conversational forms such as `please remember ...` are not memory
commands. With reasoning enabled they go to the local model instead, and
nothing it answers stores anything, whatever it says: storing a memory is not
an action the model may propose, so such a proposal is refused.

```text
remember My editor is VS Code
Remembered memory 1: My editor is VS Code

recall editor
1: My editor is VS Code

remember MY EDITOR IS VS CODE
Already remembered as memory 1: My editor is VS Code

memories
1: My editor is VS Code
```

After restarting Maria, the fact remains available:

```text
recall editor
1: My editor is VS Code
```

Memory commands and their responses also follow the interaction-history
behavior described below, so a fact can appear in both places.

## Data Retention and Privacy

Maria stores most non-empty requests it processes and their final response
text as persistent interaction history in its configured SQLite database,
located inside the project directory by default. Requests that go to the
local model, when reasoning is enabled, are the exception: no conversational
request or reply, proposal, refusal, approval prompt, or result of a
model-proposed action is ever stored as interaction history, and the
conversation is discarded when the session ends. Stored history can retain
file contents returned by `read file`, note titles and bodies, note creation,
retitle, and edit requests,
calculator expressions and results, explicit memory facts from both `remember`
and `update memory` requests and from their recall/list output, filesystem
paths, system information, and failure or denial
messages. Asking Maria to display a sensitive file or note can therefore copy
that returned content into interaction history.

Interaction history, notes, facts deliberately saved with `remember`, and the
audit summaries of approved model-proposed actions are four separate record
types in the same SQLite database, although the same text can appear in more
than one table. The configured operational log
(`logs/maria.log` by default) is a separate store for application events,
warnings, and failures rather than the interaction store. Avoiding full content
in an operational log message does not mean that content is absent from SQLite
interaction history.

Maria currently provides no application-level database encryption, automatic
retention period, or pruning. `history`, `search history <query>`, and
`read interaction <id>` provide read-only listing, searching, and reading of
interaction history; they do not modify or remove any row. `delete interaction
<id>` targets at most one interaction row by ID and removes it when found,
after mandatory explicit confirmation, with no cascade to notes or memories
and no content preview before confirmation. Deletion commits independently,
and Maria then separately attempts to record the short response `Deleted
interaction <id>.` as a new interaction-history row; when that later
recording succeeds, deleting one old row and adding that new row may leave
the total interaction-row count unchanged, and if it fails, the deletion
remains committed with no such row guaranteed. There is still no CLI
command to clear, bulk-delete, or prune interaction history. Retrieval limits
affect only how many
rows are read; they do not
remove stored rows, and restarting Maria does not clear them. Protection
depends on access to the machine, user account, and database file and on any
storage encryption in use.

`retitle note <id>: <new title>`, `edit note <id>: <new body>`, and `delete
note <id>` each target at most one note row and change or remove it when
found, after mandatory explicit
confirmation, with no content preview before confirmation. Retitle replaces
only the title, edit replaces only the body, and both preserve `created_at`
while refreshing `updated_at`; retitle and edit also turn the note's context
consent off, and say so only when it had been on. Delete removes the matching
row. An ID that matches no note changes or removes nothing and returns a
not-found result.
None of
them cascade into interaction history or explicit memories, renumber remaining
note IDs, or offer bulk, wildcard, clear-all, or automatic deletion, and there
is no undo, recovery, or note revision history. Each mutation commits
independently, and Maria then separately attempts to record a short,
content-free response (`Retitled note <id>.`, `Edited note <id>.`, or `Deleted
note <id>.`); if that later recording fails, the mutation remains committed
with no such history row guaranteed. The command text itself is still
recorded as the request half of interaction history, so a retitle command
retains its new title and an edit command retains its new body there, and a
prior `read note` may already have copied that note's content into
interaction history.
Afterwards changing or deleting the note does not rewrite or erase those
independent interaction rows; they can be removed separately with `delete
interaction <id>`. As elsewhere, Maria promises no secure erasure from SQLite
file pages, database or filesystem backups, Git history, or external copies.

`update memory <id>: <new fact>` and `forget memory <id>` each target at most
one explicit memory and change or remove it when found, after mandatory
explicit confirmation, with no content preview beforehand. An ID that matches
no memory changes or removes nothing and returns a not-found result. Neither
cascades into interaction history or notes, renumbers remaining memory IDs, or
offers bulk, wildcard, clear-all, or automatic deletion, and there is no undo,
recovery, or memory revision history. Each mutation commits independently, and
Maria then separately attempts to record a short, content-free response
(`Updated memory <id>.` or `Forgot memory <id>.`); if that later recording
fails, the mutation remains committed with no such history row guaranteed.
The command text itself is still recorded as the request half of interaction
history, so an update command retains its new fact there, and an earlier
`remember`, `recall`, or `memories` response may already have copied that fact
into interaction history. Forgetting or afterwards changing the memory does
not rewrite or erase those independent interaction rows; they can be removed
separately with `delete interaction <id>`. As elsewhere, Maria promises no
secure erasure from SQLite file pages, database or filesystem backups, Git
history, or external copies.

Audit summaries are kept until you delete them, with no automatic retention
period or pruning, and hold only the content-free values described under
"Audit Summaries". `audit` and `read audit <id>` only read them, and
interaction history records each only as the command you typed and a fixed
reply such as `Listed audit summaries.` or `Displayed audit summary <id>.`,
never what it showed. `delete audit <id>` targets at most one summary and
removes it together with its outcome, in one transaction, when found, after
mandatory explicit confirmation, with no content preview beforehand; an ID
that matches no summary removes nothing and returns a not-found result.
Deleting a summary does not undo its action, cascades into no other record,
and removes nothing from the operational log, and no other command changes or
removes a summary. There is no bulk, wildcard, clear-all, or automatic
deletion and no undo or recovery, and a deleted summary's ID is never reused.
As with the other deletions, it commits independently, and Maria then
separately attempts to record the short response `Deleted audit summary <id>.`
as interaction history. The operational log records a model-proposed action
only by its action name and audit summary ID, never its arguments or result,
and a directory listing, whether you or the model asked for it, is logged
without its path or any error text. As elsewhere, Maria promises no secure
erasure.

Maria's shipped runtime has no cloud integration and does not itself transmit
this history. Its only network-capable code is the local Ollama adapter
described under "Local Ollama Adapter": it can reach only `127.0.0.1:11434` on
the same machine, it is disabled by default, and when enabled it receives only
Maria's fixed instructions, the current session's conversation, the explicit
memories you have enabled for context, and notes you have enabled for context
that contain a word of the request, never stored history, an audit summary, a
memory or note you have not enabled, files, or the result of an action it
proposed.

`enable memory context <id>` and `disable memory context <id>` change one
memory's consent flag, and `enable note context <id>` and `disable note context
<id>` one note's, after mandatory confirmation; the recorded interaction names
only the ID, never the fact, title, or body. Consent is off for every memory
and every note until you turn it on, and enabling one does not check it for
secrets.
Configured network-mounted or synchronized paths and operating-system backup
or sync services remain outside Maria's boundary.

Avoid submitting or requesting display of passwords, API keys, access tokens,
private keys, recovery codes, confidential credentials, or other highly
sensitive secrets. Blank input and CLI-only exit or cancelled-confirmation
paths are not completed interaction records; detailed recording boundaries are
documented in `docs/architecture.md`.

## Tests

Run the standard-library test suite from the nested `Maria` application
directory:

```text
python -m unittest discover -s tests -v
```

Tests use temporary databases, log directories, homes, and filesystem roots, so
production runtime files are not required and no test creates or reads a real
`~/MariaWorkspace`. The suite covers the CLI, configuration, permissions,
calculator, memory, Logger, Tool Manager, notes, interaction history,
filesystem, filesystem roots and permanent exclusions, system information,
Brain, Planner, the reasoning interface, the local Ollama adapter,
model-proposed actions with their approval and audit summaries, the audit
commands, database schema upgrades, and clean startup wiring. The adapter's
tests run only against a local stub on a socket pair inside the test process,
and every other model reply is scripted, so the suite needs no Ollama, no
model, and no network.

## Project Layout

- `core/`: application coordination, planning, memory, permissions, logging,
  configuration, constants, the local reasoning interface and adapter, the
  compiler that checks model proposals, and the audit-summary vocabulary
- `tools/`: built-in tools
- `database/`: SQLite database
- `config/`: TOML configuration, including the named filesystem roots and the
  disabled reasoning template
- `prompts/`: starter prompt text
- `logs/`: runtime logs
- `docs/`: architecture and development documentation

## Scope

Maria intentionally excludes internet search, browser automation, voice,
text-to-speech, autonomous agents, multi-user support, and plugin marketplaces.
