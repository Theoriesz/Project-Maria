# Maria

Maria is a modular personal AI assistant foundation written in Python.

Maria focuses on architecture, safety, and maintainability. It is not a
prototype of the final assistant and does not include advanced AI features.

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
```

File rename and move operations, `delete interaction <id>`, the three note
lifecycle commands `retitle note <id>: <new title>`, `edit note <id>: <new
body>`, and `delete note <id>`, the two memory lifecycle commands `update
memory <id>: <new fact>` and `forget memory <id>`, and the two memory context
consent commands `enable memory context <id>` and `disable memory context
<id>` require explicit confirmation. `delete interaction <id>` targets at most one interaction row by
ID and removes it when found; it never cascades to notes or memories. Each note
lifecycle command likewise targets at most one note by positive whole-number ID
only, never by title, and never cascades into interaction history or explicit
memories. Each memory lifecycle command targets at most one memory by positive
whole-number ID only, never by fact text, and never cascades into interaction
history or notes.
Retitling and editing are deliberately separate commands, so neither can
overwrite the other's field. Filesystem delete functionality remains
intentionally unimplemented.

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
a private local override. Maria then answers input that matches no command with
a conversational reply from the owner's local Ollama model; see "Conversation"
and "Local Ollama Adapter" below.

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

It sends only the model identifier and the conversation: the current session's
earlier conversational exchanges as alternating user and assistant messages,
then the request as the final user message. When you have enabled memories
for context, they come first: Maria's fixed instructions as the system
message, which never contain a memory, and then the memories as a separate,
clearly marked data message. With no memory enabled the request is exactly
what it was before memory context existed. It takes back only the answer
text. Any separate thinking text and any tool
calls in the reply are ignored after bounded JSON parsing; only
`message.content` is returned, and an answer is only ever text, never an
action. One hard deadline covers connecting, sending, and receiving
the whole reply; the reply is size-bounded before it is parsed, and the answer
must fit within `max_output_bytes` or is discarded whole. Every failure is one
fixed, typed message, and no request, answer, address, model identifier, or
Ollama error text reaches Maria's log.

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

A reply is text only. Maria never treats it as a command, an approval, or an
action, and it cannot run a tool, change a record, or confirm anything,
whatever it says. That includes claims to have done something: a reply saying
"I saved that" saves nothing. Use the commands above to act.

Within one session the model sees the current request, the earlier
conversational turns of that session, so a follow-up question is understood,
and the explicit memories you have enabled for context (see "Memory Context"
below). It never sees a command or its result, a memory you have not enabled,
a note, a file, the interaction history, or anything from an earlier session.
Maria keeps at most the last eight exchanges, and at most 8 KiB of their text
together with the current request and any memory context, or
`max_input_bytes` if that is smaller. The oldest exchanges
are dropped first, whole. A request too large on its own, or a reply too large
to keep beside its request, is refused with a fixed message rather than cut
short. The conversation lives only in memory and is discarded when the session
ends, on `exit`, end of input, Ctrl+C at the prompt, or any other way the CLI
stops.

Conversation is never recorded: no conversational request or reply is written
to interaction history or anywhere else. When the model is unavailable, too
slow, or answers with something unusable, Maria shows a fixed message such as
`The local reasoning model is unavailable.`, and every command keeps working.

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
deliberate, transitional rule, not a judgement about relevance, and a later
stage revisits it. Memories are taken in ascending ID order, whole, at most 16
of them and at most 2 KiB of context including Maria's framing and the IDs.
Your current request always comes first, then as much memory context as fits,
then as many earlier turns as fit. `disable memory context <id>` turns consent
off again, and also asks for confirmation. Updating a memory turns its consent
off, because consent applies to the exact text you approved; Maria tells you
when that happens.

After every conversational reply Maria adds her own note, separate from the
model's answer:

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
and it gives the model no ability to act. If you disable, change, or forget a
memory that was sent earlier in the session, Maria clears that session's
earlier conversation before sending anything else, so an earlier reply cannot
carry the old text back, even when the change came from another Maria
window. If memory context cannot be read, Maria sends neither memory context
nor earlier turns and says so. This controls what Maria sends next; it cannot
erase anything the model already received.

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
commands. With reasoning enabled they receive a conversational reply instead,
and that reply stores nothing, whatever it says.

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
located inside the project directory by default. Conversational requests and
replies, when reasoning is enabled, are the exception: they are never stored,
and the conversation is discarded when the session ends. This can retain file contents
returned by `read file`, note titles and bodies, note creation, retitle, and
edit requests,
calculator expressions and results, explicit memory facts from both `remember`
and `update memory` requests and from their recall/list output, filesystem
paths, system information, and failure or denial
messages. Asking Maria to display a sensitive file or note can therefore copy
that returned content into interaction history.

Interaction history, notes, and facts deliberately saved with `remember` are
three separate record types in the same SQLite database, although the same text
can appear in more than one table. The configured operational log
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
recording succeeds, deleting one old row and adding that audit row may leave
the total interaction-row count unchanged, and if it fails, the deletion
remains committed with no audit row guaranteed. There is still no CLI
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
while refreshing `updated_at`; delete removes the matching row. An ID that
matches no note changes or removes nothing and returns a not-found result.
None of
them cascade into interaction history or explicit memories, renumber remaining
note IDs, or offer bulk, wildcard, clear-all, or automatic deletion, and there
is no undo, recovery, or note revision history. Each mutation commits
independently, and Maria then separately attempts to record a short,
content-free response (`Retitled note <id>.`, `Edited note <id>.`, or `Deleted
note <id>.`); if that later recording fails, the mutation remains committed
with no audit row guaranteed. The command text itself is still recorded as the
request half of interaction history, so a retitle command retains its new
title and an edit command retains its new body there, and a prior `read note`
may already have copied that note's content into interaction history.
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
fails, the mutation remains committed with no audit row guaranteed. The command
text itself is still recorded as the request half of interaction history, so an
update command retains its new fact there, and an earlier `remember`, `recall`,
or `memories` response may already have copied that fact into interaction
history. Forgetting or afterwards changing the memory does not rewrite or erase
those independent interaction rows; they can be removed separately with `delete
interaction <id>`. As elsewhere, Maria promises no secure erasure from SQLite
file pages, database or filesystem backups, Git history, or external copies.

Maria's shipped runtime has no cloud integration and does not itself transmit
this history. Its only network-capable code is the local Ollama adapter
described under "Local Ollama Adapter": it can reach only `127.0.0.1:11434` on
the same machine, it is disabled by default, and when enabled it receives only
the current session's conversation and the explicit memories you have enabled
for context, never stored history, notes, other memories, or files.

`enable memory context <id>` and `disable memory context <id>` change one
memory's consent flag, after mandatory confirmation; the recorded interaction
names only the ID, never the fact. Consent is off for every memory until you
turn it on.
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
Brain, Planner, the reasoning interface, the local Ollama adapter, and clean
startup wiring. The adapter's tests run only against a local stub on a socket
pair inside the test process, so the suite needs no Ollama, no model, and no
network.

## Project Layout

- `core/`: application coordination, planning, memory, permissions, logging,
  configuration, constants, and the local reasoning interface and adapter
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
