"""Timezone validation.

One place, because two things depend on agreeing about it: the schema that
accepts a timezone from a client, and the SQL that buckets telemetry into
local hours. A name Python accepts but Postgres does not would pass
validation and then fail at query time.
"""

from __future__ import annotations

import functools
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

DEFAULT_TIMEZONE = "UTC"


@functools.lru_cache(maxsize=1)
def _known() -> frozenset[str]:
    """The tzdata names available to this process.

    Cached: ``available_timezones()`` walks the tz database directory, which
    is not something to do per request.
    """
    return frozenset(available_timezones())


def is_valid(name: str) -> bool:
    """Whether ``name`` is an IANA timezone this process can resolve."""
    if name not in _known():
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - defensive
        return False
    return True


def normalise(name: str | None) -> str:
    """Return a usable timezone name, falling back to UTC.

    Used on the read path, where a row written before a name was retired --
    or by a hand-edited database -- should degrade to UTC rather than break
    every analytics query for that account.
    """
    if name and is_valid(name):
        return name
    return DEFAULT_TIMEZONE
