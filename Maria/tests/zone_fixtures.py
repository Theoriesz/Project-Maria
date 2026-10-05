"""Synthetic time zones for Maria's timing tests.

Daylight-saving and rule-change tests must not depend on whatever time zone
data the interpreter happens to have, so they build their own: minimal TZif
version 2 files with no transitions, whose rules come entirely from the POSIX
TZ footer, written under a temporary directory that zoneinfo's search path
points at for one test alone. Every installation restores exactly the search
path it found and clears zoneinfo's cache, so no synthetic zone outlives its
test and none can shadow real data afterwards. A test that needs the real
IANA data asks ``real_zone``, which skips the test, with a reason, when no
data source provides the key.

No zone the owner actually uses is named here; the real keys below are only
well-known examples with daylight-saving rules worth testing.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
import zoneinfo
from pathlib import Path


# A central European style rule: +01:00, and +02:00 from the last Sunday of
# March at 02:00 to the last Sunday of October at 03:00, so in 2027 the
# local hour from 02:00 on 28 March does not exist and the local hour from
# 02:00 on 31 October happens twice.
CENTRAL_RULES = "CET-1CEST,M3.5.0,M10.5.0/3"

# A thirty-minute change: +10:30, and +11:00 from the first Sunday of October
# at 02:00 to the first Sunday of April at 02:00.
HALF_HOUR_RULES = "<+1030>-10:30<+11>-11,M10.1.0,M4.1.0"

# A fixed +03:00 with no daylight saving, used to change a zone's rules.
FIXED_PLUS_THREE_RULES = "<+03>-3"

# A fixed offset with seconds, which no canonical reminder time can carry.
SECONDS_OFFSET_RULES = "<+010030>-1:00:30"

SYNTHETIC_KEY = "Maria/Synthetic"


def tzif_bytes(footer: str) -> bytes:
    """Return minimal TZif version 2 bytes whose rules are only ``footer``."""

    def header() -> bytes:
        return b"TZif2" + b"\x00" * 15 + struct.pack(">6l", 0, 0, 0, 0, 1, 4)

    block = header() + struct.pack(">lBB", 0, 0, 0) + b"LMT\x00"
    return block + block + b"\n" + footer.encode("ascii") + b"\n"


class SyntheticZones:
    """Synthetic zone files on zoneinfo's search path for one test.

    The directory is temporary, the search path found before is put back
    exactly, and the cache is cleared on the way in and on the way out.
    """

    def __init__(self, test: unittest.TestCase, zones: dict[str, str]) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mqz"))
        test.addCleanup(shutil.rmtree, self.directory, True)
        for key, footer in zones.items():
            self.write(key, footer)
        original = zoneinfo.TZPATH
        zoneinfo.reset_tzpath([str(self.directory)])
        zoneinfo.ZoneInfo.clear_cache()
        test.addCleanup(self._restore, original)

    def write(self, key: str, footer: str) -> None:
        """Create or replace one zone's rules, clearing zoneinfo's cache."""
        path = self.directory.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(tzif_bytes(footer))
        zoneinfo.ZoneInfo.clear_cache()

    def corrupt(self, key: str) -> None:
        """Replace one zone's file with bytes zoneinfo cannot read."""
        path = self.directory.joinpath(*key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a time zone file")
        zoneinfo.ZoneInfo.clear_cache()

    def remove(self, key: str) -> None:
        """Remove one zone's file, so the key no longer loads."""
        self.directory.joinpath(*key.split("/")).unlink()
        zoneinfo.ZoneInfo.clear_cache()

    @staticmethod
    def _restore(original: tuple[str, ...]) -> None:
        """Put back exactly the search path found, and clear the cache."""
        zoneinfo.reset_tzpath(original)
        zoneinfo.ZoneInfo.clear_cache()


def real_zone(test: unittest.TestCase, key: str) -> zoneinfo.ZoneInfo:
    """Return a real IANA zone, or skip the test when no data provides it."""
    zoneinfo.ZoneInfo.clear_cache()
    try:
        return zoneinfo.ZoneInfo(key)
    except zoneinfo.ZoneInfoNotFoundError:
        test.skipTest(
            "Real IANA time zone data is not available on this interpreter: "
            "install the pinned tzdata from requirements-time.txt to run this "
            "test."
        )
        raise AssertionError("unreachable")
