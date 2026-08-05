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

Version 0.1 uses only the Python standard library. Add dependencies only when
they solve a real maintenance problem and are approved for the project scope.

## Logging

Important actions should be logged. Logs should explain what happened without
storing unnecessary sensitive content.
