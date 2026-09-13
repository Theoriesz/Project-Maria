# Principles

Maria should be built as a long-term software product.

## Priorities

1. Simplicity
2. Maintainability
3. Readability
4. Modularity
5. Reliability
6. Security

## Development Rules

- Every module should have one clear responsibility.
- Prefer explicit interfaces over hidden coupling.
- Keep behavior small until the architecture can support more.
- Log important actions.
- Require confirmation before potentially destructive operations.
- Avoid unnecessary third-party dependencies.
- Do not add out-of-scope features.

## User Control

Maria should assist the user without taking uncontrolled action. The assistant
must be transparent about what it can do, what it cannot do, and when it needs
confirmation.
