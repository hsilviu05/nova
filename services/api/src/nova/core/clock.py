"""Time source.

Every timestamp in NOVA is timezone-aware UTC. Centralising ``utc_now`` keeps
that invariant in one place and gives tests a single seam to freeze.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
