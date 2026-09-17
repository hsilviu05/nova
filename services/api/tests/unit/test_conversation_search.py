"""The pure parts of conversation search: LIKE escaping and excerpts."""

from __future__ import annotations

from nova.repositories.conversation import _escape_like
from nova.services.conversation import excerpt


class TestEscapeLike:
    def test_plain_text_is_unchanged(self) -> None:
        assert _escape_like("postgres") == "postgres"

    def test_wildcards_and_backslashes_are_escaped(self) -> None:
        assert _escape_like("100%_done\\") == "100\\%\\_done\\\\"


class TestExcerpt:
    def test_short_text_is_returned_whole(self) -> None:
        assert excerpt("Which database?", "database") == "Which database?"

    def test_whitespace_is_collapsed(self) -> None:
        assert excerpt("a\n\n    b\tc", "b") == "a b c"

    def test_long_text_is_windowed_around_the_match(self) -> None:
        text = "x " * 200 + "NEEDLE" + " y" * 200
        piece = excerpt(text, "needle", width=60)
        assert "NEEDLE" in piece
        assert piece.startswith("…") and piece.endswith("…")
        assert len(piece) <= 62

    def test_a_match_at_the_start_is_not_cut_off(self) -> None:
        text = "NEEDLE at the very start " + "filler " * 50
        piece = excerpt(text, "needle", width=40)
        assert piece.startswith("NEEDLE")
        assert piece.endswith("…")

    def test_no_match_falls_back_to_the_head(self) -> None:
        text = "word " * 100
        piece = excerpt(text, "absent", width=20)
        assert piece.endswith("…") and len(piece) <= 21
