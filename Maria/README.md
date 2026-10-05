# Maria

Maria is a modular personal AI assistant foundation written in Python.

Maria focuses on architecture, safety, and maintainability. It is not a
prototype of the final assistant. Its optional use of a local language model
is deliberately narrow and off by default, and nothing the model proposes runs
without the owner's approval.

## Requirements

- Python 3.11 or newer
- SQLite, included with Python
- Optional, for the desktop GUI only: PyQt6 and its runtime dependencies,
  pinned in `requirements-gui.txt`. The command line never needs them.
- Optional, for dated tasks and timed reminders on a Python without time zone
  data of its own, such as the Windows builds: the data-only `tzdata` package,
  pinned in `requirements-time.txt`. Maria never imports or installs it.

## Setup

From the `Maria` directory:

```text
python -m pip install -r requirements.txt
python main.py
```

To use the optional desktop window as well, install its requirements, ideally
into a virtual environment rather than a global interpreter, and start Maria
with `--gui`:

```text
python -m pip install -r requirements-gui.txt
python main.py --gui
```

Dated tasks and timed reminders need your time zone, set in the private
override described under "Tasks and Reminders". Where Python has no time zone
data of its own, install the pinned data package too, again ideally into a
virtual environment:

```text
python -m pip install -r requirements-time.txt
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

## Desktop GUI

`python main.py --gui` opens a small desktop window over the same Maria: the
same commands, approvals, records, permissions, and audit summaries as the
command line, through the same session. Without `--gui`, Maria starts the
command line exactly as before. The window is deliberately simple:

- a read-only conversation area showing `You:` and `Maria:` lines, starting
  with the same banner the command line prints;
- a one-line input with Send, or press Enter; every command, record commands
  included, is typed exactly as on the command line;
- a short status line: `Ready`, `Working...`, `Waiting for your approval`, or
  a closing message;
- while an action waits for approval, the complete approval text Maria
  produced, with Approve and Cancel.

The window follows the command line's rules. Blank input is ignored, and
`exit`, `quit`, or `q` closes the window. A request holding a line break is
refused, because the command line reads one line per request, and stays in the
input so you can fix it. One request or answer is handled at a time: Send and
Enter do nothing while Maria works or waits for your answer, though you can
keep typing, so a second click or key press is never sent twice, and an answer
counts only for the approval showing.
Approve confirms exactly the plan the session offered, through the same check
the command line uses, so a changed plan is refused rather than run. Cancel
dismisses the pending approval, prints `Action cancelled.`, and records
nothing; it cannot stop something that is already running. Approve and Cancel
never take keyboard focus, so only a deliberate click answers an approval.

The window stays responsive while Maria works, because one worker thread runs
your requests one at a time, in the foreground and only when you ask. Closing
the window while Maria is busy says that closing waits for the current
request; it never approves anything that request produces, and the window
closes once the session has ended. Closing waits for the current operation and
has no general GUI-wide deadline. Provider deadlines apply where configured,
but a stalled operation can delay closing indefinitely. Ctrl+C in the terminal
you started Maria from is ignored while the window is open; close the window
instead. A running plan is not interrupted in the window: it finishes, or
stops at a failure, on its own.

Everything in the window is plain text. Replies are never treated as HTML,
links are not opened, and referenced files are not loaded. The conversation
and approval areas cannot be selected or copied from, Maria never writes to
the clipboard, and pasting into the input works. Maria cannot control your
operating system's clipboard history. The window title is always `Maria`.

If PyQt6 cannot be loaded, or Qt cannot open windows on this system, `--gui`
prints `Maria: The desktop GUI is unavailable. Install the optional GUI
requirements, or start Maria without --gui to use the command line.` and
returns status 1 before anything is configured or opened. On Windows, when
Maria was started without a console window, Qt first shows its own error
dialog in that case. Startup failures read exactly as on the command line,
and a window that fails, or whose session ends unexpectedly or cannot end
cleanly, prints a fixed message and returns status 1. A window that fails
stops taking requests and answers at once, so nothing it was showing can
still be approved; it ends its session and closes. The command line keeps
working either way.

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
save conversation
conversations
read conversation 1
delete conversation 1
summarize conversation
summaries
read summary 1
edit summary 1: We agreed to book the train on Friday.
delete summary 1
enable summary context 1
disable summary context 1
add task Buy milk
add task due friday: File the tax return
tasks
completed tasks
due tasks
read task 1
complete task 1
reopen task 1
edit task 1: Buy oat milk
reschedule task 1: 2026-10-20
delete task 1
enable task context 1
disable task context 1
add reminder tomorrow 09:00: Call the dentist
add reminder in 20 minutes: Take the bread out
reminders
completed reminders
due reminders
read reminder 1
complete reminder 1
reopen reminder 1
edit reminder 1: Call the dentist about Tuesday
reschedule reminder 1: monday 08:30
delete reminder 1
enable reminder context 1
disable reminder context 1
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
`disable memory context <id>`, `delete audit <id>`, `delete conversation <id>`,
`summarize conversation`, whose confirmation shows the whole draft, the two
summary lifecycle commands `edit summary <id>: <new text>` and
`delete summary <id>`, the two summary context consent commands
`enable summary context <id>` and `disable summary context <id>`, `add task`,
`add reminder`, and for tasks and reminders alike the `edit`, `reschedule`,
`delete`, `enable ... context`, and `disable ... context` commands require
explicit confirmation. `delete interaction <id>` targets at most one
interaction row by ID and removes it when found; it never cascades to notes or
memories. Each note
lifecycle command likewise targets at most one note by positive whole-number ID
only, never by title, and never cascades into interaction history or explicit
memories. Each memory lifecycle command targets at most one memory by positive
whole-number ID only, never by fact text, and never cascades into interaction
history or notes. `delete audit <id>` targets at most one audit summary by
positive whole-number ID only and removes it together with its recorded
outcome; it never cascades into interaction history, notes, or memories.
`delete conversation <id>` and `delete summary <id>` likewise each target at
most one saved conversation or summary by ID and never cascade, and so does
every task and reminder command that names an ID.
The thirty-six ID-only planner commands (the twenty-six `<id>` commands above
that require confirmation, plus `read interaction <id>`, `read audit <id>`,
`read conversation <id>`, `read summary <id>`, and the `read`, `complete`, and
`reopen` commands for tasks and reminders) accept a whole number from
1 to 9223372036854775807, the largest integer SQLite stores, with any number of
leading zeroes; each answers a larger number with its invalid-ID reply before
anything is planned or confirmed. `read note <id or exact title>` uses its
separate, unchanged lookup path.
Retitling and editing are deliberately separate commands, so neither can
overwrite the other's field. Filesystem delete functionality remains
intentionally unimplemented.

With reasoning enabled, every action the local model proposes also needs your
approval before it runs, even when the matching command would not ask; see
"Model-Proposed Actions".

## Tasks and Reminders

A task is one line of your text with an optional due date, a calendar date
with no time. A reminder is one line of text with one moment: the local time,
offset, and time zone you approved, and the UTC instant they name. Both are
`open` or `completed`. Every task and reminder command works without a
language model.

Maria never delivers, notifies, schedules, repeats, or polls anything. A
reminder is never shown to you unasked: `due reminders` lists the open
reminders whose moment has come, and `due tasks` the open tasks due today or
earlier, only when you type them. A reminder does not repeat.

### Your time zone

Dates and times are read in one IANA time zone, which you set in the private
`config/config.local.toml` described under "Local Reasoning Settings":

```toml
[time]
zone = "Area/City"   # your own IANA key, such as Europe/Berlin
```

The tracked configuration may not hold `[time]`, and a key that is not IANA key
syntax stops startup. Whether the key's data can be loaded is checked only when
a date or time is needed. Data comes from time zone files on Python's zoneinfo
search path or, when those lack the key, from the optional pinned `tzdata`
package (see "Requirements"). Maria never falls back to UTC, a fixed offset,
or your computer's own zone: without a usable zone she refuses `due tasks` and
any new date or time with a fixed message and stores nothing. Every other task
and reminder command, including adding an undated task, works without it.

### Dates and times

`<date>` is `today`, `tomorrow`, a weekday such as `friday` (the next one, so
on a Friday `friday` is a week later), `YYYY-MM-DD`, or `in <N> days`.
`<time>` is `H:MM` or `HH:MM`. A reminder takes a date and a time, optionally
followed by an offset such as `+01:00`, or `in <N> minutes` or
`in <N> hours`:

```text
add reminder tomorrow 09:00: Call the dentist
add reminder 2026-10-25 02:30 +01:00: Check the clocks
add reminder in 90 minutes: Take the bread out
```

Maria reads the clock once per request, before planning, and resolves every
date and time of that request against that one moment, in your zone. Calendar
words and `in <N> days` keep the wall time you gave; `in <N> minutes` and
`in <N> hours` count real elapsed time from the current minute. A new date
before today, a new reminder time that is not after the current moment, and a
date more than ten years away are refused, and nothing is stored. Existing
overdue tasks and reminders stay fully usable.

On a daylight-saving change, a local time that does not exist is refused,
never shifted. A local time that happens twice is refused unless you add the
offset of the occurrence you mean, such as `02:30 +01:00`. An offset is
accepted only when it matches your zone at that local time: on an ordinary
time it changes nothing, on a repeated one it picks the occurrence, and any
other offset is refused.

### Adding and changing them

Adding, editing, rescheduling, deleting, and turning consent on or off ask for
confirmation, and the confirmation shows exactly what would be stored before
anything is:

```text
add reminder tomorrow 09:00: Call the dentist
Confirmation required: reminders.create requires explicit confirmation. Maria will not notify you when this reminder is due, and it does not repeat: type due reminders to see the reminders whose time has come.
This approval adds a reminder, shown quoted:
Text: "Call the dentist"
Time: Monday 2026-10-05 at 09:00, Europe/Berlin (UTC+02:00), which is 2026-10-05 07:00 UTC
Repeats: never
```

Approving stores exactly the values shown, even when that moment has passed
by then. `complete task <id>` and `reopen task <id>`, and the reminder forms,
change the status only and need no confirmation: completing stamps the time
of that completion, reopening clears it, and completing again stamps a new
time. `reschedule task <id>: none` removes a due date. `tasks` and `reminders`
list the open ones, `completed tasks` and `completed reminders` the completed
ones, each at most 20 and saying when more exist; open tasks come by due date,
undated last, and open reminders by their saved instant. `list tasks` and
`list reminders` still list directories.

A reminder always keeps the time zone it was set in, and is always shown and
ordered by its saved values; changing your configured zone later changes
nothing stored. If that zone's rules have since changed so that the saved
local time no longer has its saved offset, Maria adds a fixed warning and the
reminder stays at its saved instant: reschedule it to use the current rules.
When the saved zone cannot be loaded at all, she adds a note that she cannot
check it. Neither ever refuses or rewrites anything.

### Context consent

A task or reminder reaches your local model only after you confirm
`enable task context <id>` or `enable reminder context <id>`, which shows its
exact text and timing, and only while it is open. Enabled open tasks and
reminders are eligible on every conversational turn, after your enabled
memories and within the same limits of 16 items and 2 KiB, tasks before
reminders, each by ID and whole; Maria names those sent, and any left out.
Editing or rescheduling turns consent off and says so, and completing,
deleting, or disabling one that was sent clears the session's earlier
conversation, as for a memory. Reopening makes it eligible again. Maria does
not check a task or reminder for secrets.

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
propose one of five allowed actions, or a plan of two to five of them; a
proposed action or plan runs only if the owner approves it. See "Local Ollama Adapter", "Conversation", and "Model-Proposed
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
holding a `[reasoning]` table, a `[time]` table (see "Tasks and Reminders"), or
both, and nothing else. A `[reasoning]` table replaces the tracked one
completely:

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
action or plan of actions, handled as described under "Model-Proposed
Actions", and is never
shown or kept as conversation. Maria never treats a reply as a command or an
approval, and no reply can change a record or a file or confirm anything,
whatever it says; a proposed action runs only if you approve it. That includes
claims to have done something: a reply saying "I saved that" saves nothing.
Use the commands above to act.

Within one session the model sees Maria's fixed instructions, the current
request, the earlier conversational turns of that session, so a follow-up
question is understood, the explicit memories you have enabled for context,
the open tasks and reminders you have enabled for context, and notes and
conversation summaries you have enabled for context that contain a word of
your request (see "Memory Context", "Note Context and Search", "Saved
Conversations and Summaries", and "Tasks and Reminders"). It never sees a
command or its result, an earlier proposal or what became of it, a memory,
note, summary, task, or reminder you have not enabled, a saved conversation, a
file, an audit summary, the interaction history, or anything from an earlier
session. Maria keeps at most
the last eight exchanges, and at most 8 KiB of their text together with the
current request,
Maria's fixed proposal instructions, and any context, or `max_input_bytes` if
that is smaller. The request and the instructions are fitted first, then any
context, then the newest exchanges; the instructions take 2,045 bytes and are
never left out, and the oldest exchanges are dropped first, whole. A request
too large to send beside the instructions, or a reply too large to keep beside
its request, is refused with a fixed message rather than cut short, and an
over-large request is never sent to the model. The conversation lives only in
memory and is discarded when the session ends, on `exit`, end of input,
Ctrl+C at the prompt, or any other way the CLI stops, or when the desktop
window closes, unless you keep it first with `save conversation`.

Conversation is never recorded on its own: no conversational request or reply
is written to interaction history or anywhere else unless you save it, and
neither is a proposal, its refusal, an approval prompt, or an approved action's
result. An approved
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
Open tasks and reminders you enable follow the memories within the same
limits; see "Tasks and Reminders".
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
supplied records are data, not authorization, and anything it proposes still
needs your approval. If you disable, change, or forget a
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

While reasoning is enabled, Maria searches the notes and conversation
summaries you enabled, and any enabled memories beyond the 16 that are always
eligible, for the words of your current request. The search is literal: each
word is casefolded, loses punctuation at its ends, and counts only at 4 to 64
characters, and at most
the first 8 distinct words are used. A short request such as "Jo" or "AI"
searches nothing. A note matches when one of those words appears in its title
or in its body, never across the two, and only while both are still exactly
the text you approved; a note whose title or body is over 2 KiB is never
searched or named, because Maria cannot confirm it is the approved text without
reading all of it. Nothing is ranked, fuzzy, or chosen by the model. At most 8
matches are sent, memories first, then notes, then summaries, each whole, and
everything sent is at most 24 records and 4,096 bytes together with your always
eligible memories.

```text
Found by search and supplied: memory 21, note 4. Maria cannot verify whether these records are still current or whether they conflict with one another.
Search matches not included because of the context limits: note 9.
Possible divergent versions: note 4 and note 7 have the same title and different bodies. Maria cannot tell which, if either, is current.
```

Maria names every record she sent by its kind and ID, names matches the limits
left out, and names notes that share a title but differ as possible divergent
versions; she never calls one of them wrong or stale. If a memory, note, or
summary sent earlier in the session is changed, disabled, deleted, or
forgotten, Maria clears that session's earlier conversation before sending
anything else and tells you the earlier copy was invalidated. She rechecks what
she sent before every conversational request, which sees the current state
only: a record changed and changed back in between looks unchanged.

### Saved Conversations and Summaries

A conversation is kept only when you ask. `save conversation` stores the
exchanges the session holds right now, exactly as they are, as one saved
conversation; it reads no other record, asks the model nothing, and leaves the
conversation as it was:

```text
save conversation
Saved this session's 4 exchanges as saved conversation 7. It is an exact copy: later changes to memories or notes do not change it.
```

The copy keeps whatever the exchanges said, including text the model repeated
from a memory or note you have since changed or forgotten. Saving again stores
another copy, and with nothing held, including after the session has ended,
Maria answers `There is no conversation in this session to save.`
`conversations` lists the newest 20 saved conversations by ID, date, and
number of exchanges, `read conversation <id>` shows one with every turn quoted
on its own line, and `delete conversation <id>` removes one after
confirmation. A saved conversation is never edited and is never sent to the
model.

`summarize conversation` asks the local model, once, to summarize every
exchange the session holds. Maria sends her fixed request and those exchanges
and nothing else: no memory, note, or summary context and no action
instructions. If the exchanges with that request do not fit `max_input_bytes`,
or a record sent earlier in the conversation has since changed, nothing is
sent; in the second case Maria also clears the conversation. The draft is shown
in full, escaped as a note is, and it is stored exactly as shown only if you
approve it:

```text
summarize conversation
Confirmation required: conversation_summaries.create requires explicit confirmation. Storing this summary keeps text your local reasoning model wrote, which can repeat anything said in this session, including text from memories or notes it was given. Its context consent starts off, and Maria does not check it for passwords or other secrets.
This approval stores exactly this draft as a new conversation summary, shown quoted:
"We planned the trip: book the train on Friday and pack the blue bag."
```

Once you approve it, Maria shows
`Saved as conversation summary 1. Its context consent is off.` Approving asks
the model nothing, and any other request, a cancel, or ending the session
discards the draft. A draft that is empty, or
longer than 2,048 bytes once its outer spaces and line breaks are removed, is
discarded with a fixed message, and a reply that looks like an action is only
text. Nothing about a summary request is kept in the conversation or in
interaction history.

`summaries` lists the newest 20 summaries with their consent, `read summary
<id>` shows one, `edit summary <id>: <new text>` replaces its text and
`delete summary <id>` removes it, each after confirmation. A summary reaches
the model only after you confirm `enable summary context <id>`, which shows
its exact stored text, as for a note:

```text
enable summary context 1
Confirmation required: conversation_summaries.enable_context requires explicit confirmation. Once enabled, this summary's entire text may be sent to your local reasoning model on a later conversational turn when a word of four or more characters from your request appears literally in it, within Maria's context limits. Maria does not check the summary for passwords or other secrets, and enabling it does not mean it contains none.
This approval covers conversation summary 1 exactly as it is stored now, shown quoted:
"We planned the trip: book the train on Friday and pack the blue bag."
```

An enabled summary is then searched like an enabled note and named as
`summary 1` when it is sent. `disable summary context <id>` turns consent off,
editing a summary turns it off and says so, and a summary changed any other way
is never sent under its old approval.

### Model-Proposed Actions

Every request Maria sends to the local model carries her fixed instructions
saying which actions she accepts. When the model's reply opens with `{`, after
only spaces, tabs, carriage returns, and line feeds, Maria reads it as a
proposal: of one action, or of a short plan of two to five actions run in
order. A reply that does not open that way, such as one that puts a proposal
after other words or inside a code fence, or one that opens with `[`, is
ordinary conversational text, and nothing in it runs. The model is asked once per
request, and only while reasoning is enabled; with the shipped configuration
no model proposes anything.

Only five actions can be proposed:

- `calculator.calculate`, with an `expression`
- `filesystem.list`, with no arguments to list the default root, or with a
  `path` relative to it
- `system.current_time`
- `tasks.create` and `reminders.create`, only for a request in one of the
  four forms described below

A proposal must be exactly one JSON object, at most 4,096 bytes however many
actions it holds, with nothing but whitespace around it. For one action it
holds only `tool`, `action`, and `arguments`; for two to five it holds only
`steps`, a list of such objects in the order they are to run. Every action
must be one of the five, and its arguments must meet the same rules as the
matching command's. Every argument is taken literally: a step cannot use an
earlier step's result, and text such as `$1` is just text. No text
argument may contain a control, formatting, surrogate, or line or paragraph
separator character, and a path must be plain relative text below the default
root: never absolute or just `.`, and with no drive, colon, leading `~`, part
made only of dots and spaces such as `..`, or Windows device name such as
`NUL`. When a listing runs, the usual root and exclusion rules apply to it as
to any `list` command. Anything else is refused with one fixed message, such
as `Maria does not accept that action from the reasoning model.`, followed by
Maria's notes about context. A plan is refused whole if any of its actions is,
and nothing in it runs. Nothing is repaired, and the model is not asked again.

A proposal that passes is shown to you, and it runs only if you approve it
with `y` or `yes` at the usual confirmation prompt, or with Approve in the
desktop window. That holds for all five
actions, even though the shipped `calculate`, `list`, and `time` commands run
without asking. Each proposal needs exactly one approval, for the whole plan
when it holds several steps, whatever your configuration says about
confirming those actions, and a plan with any action your configuration
denies is refused before any approval is asked for. For
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
that same plan once more against the five allowed actions, their argument
rules, and your current permissions, and it runs at most once. Cancelling at
the prompt, or with Cancel in the desktop window, runs and records nothing.
Maria's notes about context are shown
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

A plan of several steps is shown whole, every step numbered with its tool,
action, and exact arguments:

```text
Maria's local reasoning model proposed this plan of 3 actions. They run in order, and only if you approve the whole plan. Maria stops if a step fails, an audit write fails, or the run is interrupted.
Step 1 of 3
Tool: system
Action: current_time
Arguments: {}
Step 2 of 3
Tool: filesystem
Action: list
Arguments: {"path": "missing"}
Step 3 of 3
Tool: calculator
Action: calculate
Arguments: {"expression": "2 + 2"}

No memory context supplied.
```

Once you approve it, the steps run in order, each behind its own audit
summary, committed just before that step runs. Maria goes on to the next step
only when the step before it succeeded and its outcome was recorded; she stops
at the first step that fails, at a step whose audit summary cannot be
recorded, which then does not run, and after a step whose outcome cannot be
recorded. The report says exactly what happened to every step:

```text
Step 1 of 3, system.current_time: completed.
Date: 2031-02-03
Time: 04:05:06
UTC offset: +00:00
ISO-8601: 2031-02-03T04:05:06+00:00
Recorded as audit summary 1.

Step 2 of 3, filesystem.list: failed.
Not a directory: missing
Recorded as audit summary 2.

Step 3 of 3, calculator.calculate: not run.

Maria stopped at step 2 and did not run step 3. Nothing that ran was undone.
```

Nothing is rolled back, retried, or resumed, and a step that failed or was
interrupted may still have done part of its work. Pressing Ctrl+C while a plan
of several steps runs stops it: Maria reports a step that had not started as
not run, a step interrupted inside its tool as having an unknown outcome, and
a step whose tool had already answered with that answer. If the interruption
came while an audit summary or outcome was being written, she says she could
not confirm whether it was recorded, because it may already have been. A plan
of one step stops at Ctrl+C exactly as before, and if Maria's process ends
abruptly no report can be shown at all. A step that was never reached has no
audit summary, and the audit listing does not group one plan's summaries
together. In the desktop window Ctrl+C does not reach a running plan, so the
plan finishes or stops on its own and its report says how.

### Tasks and Reminders in Your Own Words

With reasoning enabled, you can also ask for a task or reminder in one of four
closed English forms, or for two to five of them joined by `and` or `, and`:

```text
remind me tomorrow at 09:00 to call mom
remind me to stretch in 20 minutes
add a task to buy milk
add a task to file taxes by friday
```

A reminder's time is `today at`, `tomorrow at`, `on <weekday> at`,
`on YYYY-MM-DD at`, or `in <N> days at`, then a time and an optional offset,
or `in <N> minutes` or `in <N> hours`; a task's date is `today`, `tomorrow`, a
weekday, or `YYYY-MM-DD`. Keywords may be in any case.

The model never decides whether you asked. Maria reads your request herself:
a creation is accepted only when your whole request is exactly those forms,
and the proposal must hold one step for each of your items, in order, with its
text copied exactly and its time or date phrase as you wrote it, apart from
letter case and extra spaces. Maria then
builds each step from her own reading and resolves the time herself, so
nothing the model wrote is stored, and the approval shows the exact values:

```text
remind me tomorrow at 9:00 to call mom
Maria's local reasoning model proposed this action. It runs only if you approve it.
Tool: reminders
Action: create
Arguments: {"repeat": "never", "text": "call mom", "when": "2026-10-05T09:00:00+02:00[Europe/Berlin]"}

Maria will not notify you when this reminder is due, and it does not repeat: type due reminders to see the reminders whose time has come.
No memory context supplied.
```

Below the proposed steps, an approval shows the same fixed notice a typed
command's confirmation would, once for each kind of step that carries one,
however many such steps the plan holds; a plan without such a step shows
only the steps and the context notice. The notice is for you alone: it is not
kept in the conversation, recorded, or sent to the model.

A question, a negation, a quotation, an example, a polite wrapper such as
`please remind me ...`, or a passing mention is not a request, and nothing is
added. A request that begins like a form but has a missing or vague time, such
as `remind me tomorrow` or `remind me tomorrow morning to ...`, or whose text
holds a second request, a retraction or hedge such as `just kidding` or
`maybe`, or a time word, is refused with a request to say it again, and so is
a proposal that does not match your request exactly. Nothing is guessed or
repaired. Each item runs behind its own audit summary, and, as for any
model-proposed action, the request is not recorded as interaction history.

## Audit Summaries

An audit summary is Maria's record of one approved model-proposed action. It
holds only values Maria generates herself: the UTC time she authorized the
attempt; the action, such as `calculator.calculate`; that you approved it; a
target of `named filesystem root` for a directory listing or `none`
otherwise, never which root or path; and, once the action has run, whether it
succeeded or failed by the tool's own report. It never holds your request,
the model's reply, an argument such as an expression or a path, a root's
name, a host path, the result, or any memory or note. A plan of several steps
leaves one summary for each step Maria reached, and none for a step after it
stopped; nothing in the summaries says which of them belonged to one plan.

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
command can be proposed by the model, and audit summaries are never sent to
it.
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
model-proposed action, and no `summarize conversation` request, draft, or
outcome, is ever stored as interaction history, and the conversation is
discarded when the session ends unless you save it. Stored history can retain
file contents returned by `read file`, note titles and bodies, note creation,
retitle, and edit requests,
calculator expressions and results, explicit memory facts from both `remember`
and `update memory` requests and from their recall/list output, filesystem
paths, system information, and failure or denial
messages. Asking Maria to display a sensitive file or note can therefore copy
that returned content into interaction history.

Interaction history, notes, facts deliberately saved with `remember`, the
audit summaries of approved model-proposed actions, saved conversations,
conversation summaries, tasks, and reminders are eight separate record types
in the same SQLite database, although the same text can appear in more than
one table. The
configured operational log (`logs/maria.log` by default) is a separate store
for application events,
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

Saved conversations and conversation summaries are kept until you delete them,
with no automatic retention period or pruning. A saved conversation holds
exactly the exchanges you saved, which can include anything you typed and
anything the model repeated, from a memory, a note, or elsewhere, and a summary
holds model-written text you approved or later edited; Maria checks neither for
secrets. `conversations`, `read conversation <id>`, `summaries`, and
`read summary <id>` only read them, and interaction history records each as
the command you typed and a fixed reply such as `Listed saved conversations.`
or `Displayed conversation summary <id>.`, never what it showed. A save is
recorded as the command and its fixed reply, never the transcript.
`delete conversation <id>` and `delete summary <id>` each remove at most one
row by ID, after mandatory confirmation, with no content preview beforehand,
cascade into no other record, and never reuse a deleted ID; there is no bulk,
wildcard, clear-all, or automatic deletion and no undo or recovery. An edit
command's request half keeps its new text in interaction history, as a note
edit's does. Deleting a saved conversation or summary does not remove a copy
the same text may have in another record, and Maria promises no secure
erasure.

Tasks and reminders are kept until you delete them, with no automatic
retention period or pruning; completing one keeps it. A listing, a due query,
or a reading is recorded only as the command you typed and a fixed reply such
as `Listed open tasks.` or `Displayed reminder <id>.`, and adding, editing, or
rescheduling records a fixed reply such as `Added task <id>.`, never the text
or time shown; as for a note edit, the command you typed keeps its text in the
request half. A task or reminder created from a model proposal is not recorded
as interaction history at all. `delete task <id>` and `delete reminder <id>`
each remove at most one row by ID, after mandatory confirmation, cascade into
no other record, and never reuse a deleted ID; there is no bulk, wildcard,
clear-all, date-range, or automatic deletion and no undo or recovery, and
Maria promises no secure erasure. Tasks and reminders are never linked to each
other or to any other record. The operational log names a task or reminder
only by its ID, never its text, date, time, zone, or consent identity.

Maria's shipped runtime has no cloud integration and does not itself transmit
this history. Its only network-capable code is the local Ollama adapter
described under "Local Ollama Adapter": it can reach only `127.0.0.1:11434` on
the same machine, it is disabled by default, and when enabled it receives only
Maria's fixed instructions, the current session's conversation, the explicit
memories and the open tasks and reminders you have enabled for context, notes
and conversation summaries you have enabled for context that contain a word of
the request, and, when you ask for a summary, the session's exchanges with
Maria's fixed summary request; it never receives stored history, a saved
conversation, an audit summary, a memory, note, summary, task, or reminder you
have not enabled, files, or the result of an action it proposed.

`enable memory context <id>` and `disable memory context <id>` change one
memory's consent flag, `enable note context <id>` and `disable note context
<id>` one note's, `enable summary context <id>` and
`disable summary context <id>` one summary's, and the task and reminder forms
one task's or reminder's, after mandatory confirmation; the recorded
interaction names only the ID, never the fact, title, body, text, or timing.
Consent is off for every memory, note, summary, task, and reminder until you
turn it on, and enabling one does not check it for secrets.
Configured network-mounted or synchronized paths and operating-system backup
or sync services remain outside Maria's boundary.

Avoid submitting or requesting display of passwords, API keys, access tokens,
private keys, recovery codes, confidential credentials, or other highly
sensitive secrets. Blank input and CLI-only exit or cancelled-confirmation
paths are not completed interaction records; detailed recording boundaries are
documented in `docs/architecture.md`.

The desktop window records exactly what the command line records, because both
go through the same session and Brain. What the window displays is only
displayed: it is held in the window while it is open, never saved by the
window, never sent back to Maria as conversation, and gone when the window
closes, and a new window starts empty apart from its banner. Display history
is never populated automatically from stored records. Explicit record-reading
commands still display their results. The window title never shows a request
or a reply, the window keeps no recent-item list, and Maria shows no crash
dialog. Anything you copy elsewhere, or an operating system's own clipboard
history, is outside Maria's control; Maria itself never writes to the
clipboard.

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
model-proposed actions and plans with their approval, audit summaries, and
reports, the audit
commands, saved conversations and conversation summaries with their drafting,
approval, and context consent, tasks and reminders with their dates, times,
time zones, request forms, approvals, and context consent, database schema
upgrades, and clean startup wiring. The adapter's
tests run only against a local stub on a socket pair inside the test process,
and every other model reply is scripted, so the suite needs no Ollama, no
model, and no network.

The suite also covers the desktop GUI. Its controller and worker tests, and
the tests that run the same scripts through the command line and the GUI and
compare everything they showed and stored, need no Qt. The window's own tests
need PyQt6 and run on Qt's offscreen platform, so no window appears; without
PyQt6 they are skipped, each with its reason. Passing offscreen tests are not
a visual check.

Daylight-saving and rule-change tests build their own time zones inside the
test, so they never depend on the computer's time zone data. The few tests
that need real IANA data skip, each with its reason, when no data source
provides it; run the suite with the pinned `tzdata` installed as well to
include them.

## Project Layout

- `core/`: application coordination, planning, memory, permissions, logging,
  configuration, constants, the local reasoning interface and adapter, the
  compiler that checks model proposals, the audit-summary, conversation-record,
  and task vocabularies, the four task and reminder request forms, and date and
  time resolution
- `tools/`: built-in tools
- `gui/`: the optional desktop window, its Qt-free controller, and its one
  session worker
- `requirements.txt`, `requirements-gui.txt`, and `requirements-time.txt`: the
  standard-library-only core requirements, the optional GUI requirements, and
  the optional time zone data
- `database/`: SQLite database
- `config/`: TOML configuration, including the named filesystem roots and the
  disabled reasoning template
- `prompts/`: starter prompt text
- `logs/`: runtime logs
- `docs/`: architecture and development documentation

## Scope

Maria intentionally excludes internet search, browser automation, voice,
text-to-speech, autonomous agents, multi-user support, and plugin marketplaces.
