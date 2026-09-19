"""Reusable log assertions for Maria's privacy regressions.

A privacy regression is only convincing when the value it watches for could
not have reached the log by any route, so the check has to read the real file
a real handler wrote. This module supplies that one check, and nothing else:
it configures no logging, installs no handler, patches nothing, and holds no
state, so importing it from any test is free of side effects.

The file under inspection is always supplied by the caller and is expected to
be a temporary one. Maria's live log directory is refused outright rather than
merely not defaulted to, because a test that reads it has already gone wrong.
The refusal covers that directory itself and everything beneath it at any
depth, and it is settled on the fully resolved path, so a nested file, a
``".."`` traversal, or a symlink that lands inside the live directory is
refused rather than read.

Deliberately absent is any form of redaction. Nothing here rewrites, filters,
or scrubs a log. The discipline these assertions protect is that production
modules never hand a private value to the logger in the first place; a helper
that cleaned up afterwards would only hide the failure this is meant to catch.
"""

from __future__ import annotations

import unittest
from collections.abc import Iterable
from pathlib import Path

from core.constants import LOG_DIR


def assert_log_omits(
    test_case: unittest.TestCase,
    log_path: Path,
    sentinels: Iterable[str],
) -> None:
    """Assert a temporary log file contains none of the named sentinels.

    Each sentinel is a distinctive value the test put somewhere private: an
    exception's text, an argument's content, a request, a credential. A
    blank one is refused rather than searched for, because a value every log
    trivially contains would report success while proving nothing. The
    failure names only the sentinels that leaked and the file's bare name,
    never the surrounding log text and never an absolute path, so a failing
    run reports the leak without printing whatever else the log holds.
    """
    watched = tuple(sentinels)
    if not watched:
        raise ValueError("At least one sentinel is required.")
    if any(
        type(sentinel) is not str or not sentinel.strip()
        for sentinel in watched
    ):
        raise ValueError("Every sentinel must be a non-blank string.")

    # Containment is settled on the fully resolved path and before anything
    # is opened, so the live directory itself, a file directly inside it, a
    # descendant at any depth, a ".." traversal, and a symlink that lands
    # inside it are all refused rather than read.
    live_root = LOG_DIR.resolve()
    resolved = Path(log_path).resolve()
    if resolved == live_root or live_root in resolved.parents:
        raise ValueError(
            "Log assertions must target a temporary log file, "
            "never Maria's live log directory."
        )

    # Each sentinel is searched for exactly as supplied; the stripped check
    # above settles whether one is usable, never what is looked for.
    log_text = resolved.read_text(encoding="utf-8")
    leaked = [sentinel for sentinel in watched if sentinel in log_text]
    test_case.assertEqual(
        leaked,
        [],
        f"Private values reached {resolved.name}: {leaked}",
    )
