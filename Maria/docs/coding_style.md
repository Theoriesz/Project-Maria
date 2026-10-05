# Coding Style

Maria's code should be written for long-term readability.

## Python

- Follow PEP 8.
- Use descriptive names.
- Prefer small functions with one clear responsibility.
- Use type hints for public functions and important internal helpers.
- Add docstrings to modules, classes, and public functions.
- Raise or return clear errors instead of failing silently.
- Avoid circular imports.

## Dependencies

Maria's core and command line use only the Python standard library, and
`requirements.txt` stays free of third-party packages. There are two approved
exceptions, both optional and pinned. The desktop GUI uses PyQt6 and its
required runtime dependencies, pinned in `requirements-gui.txt` and imported
only by `gui/window.py`. Dated tasks and timed reminders may use the
data-only `tzdata` package, pinned in `requirements-time.txt` (amendment
V1-22-R1, decision D1a): no Maria module imports it, standard-library
`zoneinfo` loads it on its own when a key is not found in time zone files on
its search path, and `core/time_resolution.py` is the only module that
imports `zoneinfo`. Maria never installs either. Add dependencies only when
they solve a real maintenance problem and are approved for the project scope.

## Logging

Important actions should be logged. Logs should explain what happened without
storing unnecessary sensitive content.
