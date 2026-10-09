# Logs

Maria writes application logs here at runtime.

The default log file is `maria.log`. The closed-app reminder pass,
`deliver_reminders.py`, writes its own `delivery.log` here instead, and only
once it has something to record; Maria never reads it back. Each file rotates
at 1 MiB and keeps at most three earlier files, and neither holds reminder
text. Runtime log files are ignored by Git, but this directory is kept so a
fresh checkout has the expected project structure.
