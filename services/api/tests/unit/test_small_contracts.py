"""The small pieces the larger tests happen to skip.

Each of these is a property, a validator, or a one-line branch that nothing
else drives directly. They are cheap to get wrong and expensive to notice:
``Permission.rank`` ordering backwards, a blank display name reaching the
database, a refresh token that reports itself usable after revocation.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from nova.ai.base import ChatCompletion
from nova.ai.offline import OfflineEmbeddingProvider
from nova.core.clock import utc_now
from nova.core.config import ProjectTarget
from nova.models.refresh_token import RefreshToken
from nova.schemas.auth import RegisterRequest
from nova.tools.base import Permission
from nova.tools.process import CommandResult


class TestPermissionOrdering:
    def test_the_ranks_are_ordered_by_how_much_damage_is_possible(self) -> None:
        """Compared numerically wherever "at least this permission" is
        decided, so the order is the policy."""
        assert Permission.READ.rank < Permission.WRITE.rank < Permission.DESTRUCTIVE.rank

    @pytest.mark.parametrize(
        ("permission", "expected"),
        [
            (Permission.READ, False),
            (Permission.WRITE, False),
            (Permission.DESTRUCTIVE, True),
        ],
    )
    def test_only_destructive_needs_a_person(self, permission: Permission, expected: bool) -> None:
        """WRITE covers NOVA's own memories and nothing else, which is why
        it does not need a confirmation. Anything that reaches outside is
        DESTRUCTIVE by definition."""
        assert permission.needs_confirmation is expected


class TestChatCompletionRefusal:
    def test_a_refusal_is_recognised_by_its_stop_reason(self) -> None:
        """The chat loop branches on this to decide whether to say something
        rather than render an empty reply."""
        assert ChatCompletion(text="", model="m", stop_reason="refusal").was_refused is True

    @pytest.mark.parametrize("reason", ["end_turn", "max_tokens", "tool_use", None])
    def test_anything_else_is_an_ordinary_reply(self, reason: str | None) -> None:
        assert ChatCompletion(text="hi", model="m", stop_reason=reason).was_refused is False


class TestOfflineEmbeddings:
    def test_the_width_is_the_one_it_was_built_with(self) -> None:
        """It has to match the memories column, and the registry refuses any
        other value -- but the provider itself is told, not assumed."""
        assert OfflineEmbeddingProvider(dimensions=1536).dimensions == 1536
        assert OfflineEmbeddingProvider(dimensions=8).dimensions == 8

    async def test_closing_it_does_nothing_and_does_not_fail(self) -> None:
        """It holds no client. The lifespan closes whatever provider is
        configured without knowing which one it got."""
        assert await OfflineEmbeddingProvider().aclose() is None

    async def test_the_same_text_always_embeds_the_same_way(self) -> None:
        """Deterministic, so a test that stores and retrieves gets a stable
        answer. Adequate for exercising the plumbing, useless for judging
        retrieval quality."""
        provider = OfflineEmbeddingProvider(dimensions=32)

        first, second = await provider.embed(["hello", "hello"])

        assert first == second
        assert len(first) == 32


class TestCommandOutput:
    def test_stderr_is_labelled_when_both_streams_have_something(self) -> None:
        """Otherwise a warning on stderr reads as part of the output, and
        "error" in the middle of a listing changes what NOVA says about it."""
        result = CommandResult(
            exit_code=0, stdout="on branch main\n", stderr="warning: stale index\n", truncated=False
        )

        assert result.output == "on branch main\n[stderr]\nwarning: stale index"

    def test_one_stream_alone_needs_no_label(self) -> None:
        assert (
            CommandResult(exit_code=0, stdout="fine\n", stderr="", truncated=False).output == "fine"
        )
        assert (
            CommandResult(exit_code=1, stdout="", stderr="nope\n", truncated=False).output == "nope"
        )

    def test_whitespace_alone_on_stderr_is_not_output(self) -> None:
        result = CommandResult(exit_code=0, stdout="fine\n", stderr="  \n", truncated=False)

        assert result.output == "fine"


class TestRefreshTokenState:
    def _token(self, **kwargs: object) -> RefreshToken:
        defaults = {
            "user_id": uuid.uuid4(),
            "token_hash": hashlib.sha256(b"x").hexdigest(),
            "expires_at": utc_now() + timedelta(days=30),
        }
        return RefreshToken(**{**defaults, **kwargs})  # type: ignore[arg-type]

    def test_a_live_token_is_usable(self) -> None:
        assert self._token().is_usable is True

    def test_a_revoked_token_is_not(self) -> None:
        """Rotation revokes; so does a reuse detection sweeping the family.
        Either way the row stays, and this is what makes it inert."""
        assert self._token(revoked_at=utc_now()).is_usable is False

    def test_an_expired_token_is_not(self) -> None:
        assert self._token(expires_at=utc_now() - timedelta(seconds=1)).is_usable is False

    def test_a_token_expiring_exactly_now_is_not(self) -> None:
        """The boundary is closed on the expiry side: a token is valid
        *until* its expiry, not through it."""
        assert self._token(expires_at=utc_now()).is_usable is False


class TestRegistrationValidation:
    @pytest.mark.parametrize("name", ["   ", "\t", "\n", "\u00a0"])
    def test_a_display_name_of_only_whitespace_is_refused(self, name: str) -> None:
        """min_length alone would accept it: three spaces is three
        characters, and it renders as an empty label on the home screen."""
        with pytest.raises(ValidationError) as caught:
            RegisterRequest(
                email="a@example.com", password="correct-horse-battery-staple", display_name=name
            )

        assert "must not be blank" in str(caught.value)

    def test_a_name_with_padding_is_accepted_and_kept(self) -> None:
        request = RegisterRequest(
            email="a@example.com", password="correct-horse-battery-staple", display_name="  Ada  "
        )

        assert request.display_name is not None
        assert "Ada" in request.display_name


class TestProjectTargets:
    @pytest.mark.parametrize(
        "url",
        ["192.168.1.50:8000", "ftp://host/", "//host", "localhost:8000", ""],
    )
    def test_a_base_url_with_no_scheme_is_refused(self, url: str) -> None:
        """The model picks a project by name and never supplies a URL, so
        this is the only place an address is checked at all."""
        with pytest.raises(ValidationError) as caught:
            ProjectTarget(name="SnapWorth", base_url=url)

        assert "must start with http" in str(caught.value)

    def test_a_trailing_slash_is_dropped(self) -> None:
        """The health path is appended, and two slashes is a 404 on enough
        servers to be worth normalising here."""
        target = ProjectTarget(name="SnapWorth", base_url="http://192.168.1.50:8000/")

        assert target.base_url == "http://192.168.1.50:8000"


class TestErrorEnvelopes:
    """The request id is threaded through both the header and the body.

    A client correlates a failure by the header; a person reading a
    screenshot of the error correlates it by the body. Both have to be
    there -- and neither may be the string "None" when the failure happened
    before the request context existed.
    """

    def _request(self, request_id: str | None) -> Any:
        import types

        return types.SimpleNamespace(state=types.SimpleNamespace(request_id=request_id))

    def test_a_response_built_before_the_request_context_carries_no_id(self) -> None:
        from nova.middleware.errors import _envelope

        response = _envelope(self._request(None), status_code=400, code="bad", message="No.")

        assert response.status_code == 400
        assert "x-request-id" not in {key.lower() for key in response.headers}

    def test_a_request_id_is_echoed_in_the_header(self) -> None:
        from nova.middleware.errors import _envelope

        response = _envelope(self._request("abc-123"), status_code=400, code="bad", message="No.")

        assert response.headers["x-request-id"] == "abc-123"


class TestTitleClipping:
    def test_a_long_title_is_cut_at_a_word_boundary(self) -> None:
        from nova.services.conversation import derive_title

        title = derive_title("the quick brown fox " * 20)

        assert title.endswith("…")
        assert not title.rstrip("…").endswith(" ")

    def test_a_single_long_word_is_cut_where_it_has_to_be(self) -> None:
        """There is no boundary to cut at, and returning the whole thing
        would put a 400-character label in a list row."""
        from nova.services.conversation import TITLE_MAX_LENGTH, derive_title

        title = derive_title("x" * 500)

        assert title == "x" * TITLE_MAX_LENGTH + "…"

    def test_a_short_title_is_left_alone(self) -> None:
        from nova.services.conversation import derive_title

        assert derive_title("what is on the disk") == "what is on the disk"


class TestBranchPositionParsing:
    def test_a_branch_ahead_only(self) -> None:
        from nova.tools.git import _parse_status

        parsed = _parse_status("# branch.head main\n# branch.ab +5 -0\n")

        assert (parsed["ahead"], parsed["behind"]) == (5, 0)

    def test_a_branch_behind_only(self) -> None:
        """The ``-N`` token is read on its own pass through the loop, so
        reaching it means the ``+N`` branch did not short-circuit."""
        from nova.tools.git import _parse_status

        parsed = _parse_status("# branch.head main\n# branch.ab +0 -9\n")

        assert (parsed["ahead"], parsed["behind"]) == (0, 9)

    def test_a_token_that_is_neither_is_ignored(self) -> None:
        from nova.tools.git import _parse_status

        parsed = _parse_status("# branch.head main\n# branch.ab +1 -2 something\n")

        assert (parsed["ahead"], parsed["behind"]) == (1, 2)
