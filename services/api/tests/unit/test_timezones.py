"""Timezone validation, in one place because two things must agree about it.

A name Python accepts but Postgres cannot resolve would pass validation on
the write path and then fail inside an ``AT TIME ZONE`` clause -- turning
every later analytics request for that account into a 500, long after the
request that caused it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nova.core.timezones import DEFAULT_TIMEZONE, is_valid, normalise
from nova.schemas.user import UserUpdate


class TestValidity:
    @pytest.mark.parametrize(
        "name",
        ["UTC", "Europe/Bucharest", "America/New_York", "Asia/Tokyo", "Etc/GMT+3"],
    )
    def test_real_iana_names_are_accepted(self, name: str) -> None:
        assert is_valid(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "Mars/Olympus",
            "europe/bucharest",  # the database is case sensitive
            "EST5EDT4",
            "",
            " UTC",
            "UTC; DROP TABLE users",
        ],
    )
    def test_anything_else_is_refused(self, name: str) -> None:
        assert is_valid(name) is False

    def test_the_lookup_is_cached(self) -> None:
        """``available_timezones`` walks the tz database directory, which is
        not something to do once per request."""
        from nova.core.timezones import _known

        assert _known() is _known()


class TestNormalising:
    def test_a_valid_name_is_returned_unchanged(self) -> None:
        assert normalise("Europe/Bucharest") == "Europe/Bucharest"

    @pytest.mark.parametrize("name", [None, "", "Mars/Olympus"])
    def test_anything_unusable_degrades_to_utc(self, name: str | None) -> None:
        """Used on the read path.

        A row written before a name was retired from tzdata -- or one from a
        hand-edited database -- should cost that account UTC timestamps, not
        break every query it has.
        """
        assert normalise(name) == DEFAULT_TIMEZONE


class TestTheSchema:
    def test_a_known_name_is_accepted(self) -> None:
        assert UserUpdate(timezone="Europe/Bucharest").timezone == "Europe/Bucharest"

    def test_omitting_it_is_not_the_same_as_clearing_it(self) -> None:
        assert UserUpdate().timezone is None
        assert UserUpdate(timezone=None).timezone is None

    def test_an_unknown_name_is_refused_with_an_example(self) -> None:
        """The message has to say what a correct value looks like; "invalid
        timezone" leaves the person guessing at the format."""
        with pytest.raises(ValidationError) as caught:
            UserUpdate(timezone="Mars/Olympus")

        assert "Europe/Bucharest" in str(caught.value)

    def test_a_name_longer_than_the_column_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            UserUpdate(timezone="A" * 65)
