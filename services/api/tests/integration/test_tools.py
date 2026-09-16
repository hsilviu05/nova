"""The tool API, end to end.

The confirmation handshake is the centre of this file. NOVA will eventually
be able to remove containers and run commands, and the only thing standing
between "the model suggested it" and "it happened" is a token a person
obtained by being shown, in words, what would happen. Everything here is a
test of that seam or of the audit trail that proves it held.

A stub tool stands in for the destructive ones. Using a real ``docker rm``
would make these tests depend on Docker being installed, and the thing being
tested is the gate, not the container.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from nova.core.config import Settings
from nova.models.tool_invocation import ToolInvocation
from nova.services.tools import ToolService, tool_context
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
    narrow,
)
from nova.tools.errors import ToolInputError, ToolNotFoundError, ToolPermissionError
from nova.tools.registry import ToolRegistry
from tests.conftest import build_test_app

pytestmark = pytest.mark.integration


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=100)
    times: int = Field(default=1, ge=1, le=3)


class EchoTool(Tool):
    """A read-only tool with a real schema, so validation can be exercised."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="echo",
            description="Repeat some text back.",
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
            input_model=EchoInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, EchoInput)
        return ToolResult(
            content=" ".join([payload.text] * payload.times),
            data={"text": payload.text},
        )


class LeakyTool(Tool):
    """Returns something a tool should never hand back."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="leaky",
            description="Prints configuration.",
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(
            content="ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
            data={"env": {"GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz012345"}},
        )


class DemolishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, max_length=64)


class DemolishTool(Tool):
    """A destructive tool that records whether it actually ran."""

    def __init__(self) -> None:
        self.destroyed: list[str] = []

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="demolish",
            description="Removes something permanently.",
            group=ToolGroup.DOCKER,
            permission=Permission.DESTRUCTIVE,
            input_model=DemolishInput,
            confirmation_prompt="Permanently remove “{target}”?",
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, DemolishInput)
        self.destroyed.append(payload.target)
        return ToolResult(content=f"Removed {payload.target}.", data={"target": payload.target})


class ExplodingTool(Tool):
    """Raises something the service did not anticipate."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="exploding",
            description="Fails.",
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        raise RuntimeError("/Users/silviu/secrets/config.yaml could not be parsed")


@pytest.fixture
def demolish() -> DemolishTool:
    return DemolishTool()


@pytest.fixture
def registry(demolish: DemolishTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    registry.register(LeakyTool())
    registry.register(ExplodingTool())
    registry.register(demolish)
    return registry


@pytest.fixture
async def tools_client(
    settings: Settings,
    engine: Any,
    session_factory: Any,
    redis_client: Any,
    registry: ToolRegistry,
) -> Any:
    app = build_test_app(
        settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis_client,
        tool_registry=registry,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://nova.test") as client:
        yield client


async def _signed_in(client: AsyncClient) -> dict[str, str]:
    body = (
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"tools-{uuid.uuid4().hex[:8]}@example.com",
                "password": "correct-horse-battery-staple",
                "display_name": "Tester",
            },
        )
    ).json()
    return {"Authorization": f"Bearer {body['tokens']['access_token']}"}


class TestListing:
    async def test_requires_authentication(self, tools_client: AsyncClient) -> None:
        assert (await tools_client.get("/api/v1/tools")).status_code == 401

    async def test_lists_tools_with_their_schemas(self, tools_client: AsyncClient) -> None:
        headers = await _signed_in(tools_client)

        body = (await tools_client.get("/api/v1/tools", headers=headers)).json()
        echo = next(item for item in body["items"] if item["name"] == "echo")

        assert echo["permission"] == "read"
        assert echo["requires_confirmation"] is False
        # The schema is what the app renders a form from.
        assert echo["input_schema"]["properties"].keys() == {"text", "times"}

    async def test_the_app_is_told_which_tools_will_change_things(
        self, tools_client: AsyncClient
    ) -> None:
        """Unlike the model, the person holding the phone is entitled to know
        which button changes something before pressing it."""
        headers = await _signed_in(tools_client)

        body = (await tools_client.get("/api/v1/tools", headers=headers)).json()
        demolish = next(item for item in body["items"] if item["name"] == "demolish")

        assert demolish["permission"] == "destructive"
        assert demolish["requires_confirmation"] is True

    async def test_reports_whether_shell_is_on(self, tools_client: AsyncClient) -> None:
        headers = await _signed_in(tools_client)

        body = (await tools_client.get("/api/v1/tools", headers=headers)).json()
        assert body["shell_enabled"] is False


class TestInvoking:
    async def test_a_read_tool_runs(self, tools_client: AsyncClient) -> None:
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "echo", "arguments": {"text": "hello", "times": 2}},
        )

        assert response.status_code == 200
        assert response.json()["content"] == "hello hello"

    async def test_an_unknown_tool_is_a_404(self, tools_client: AsyncClient) -> None:
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke", headers=headers, json={"name": "rm_rf", "arguments": {}}
        )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "tool_not_found"

    async def test_invalid_arguments_are_refused_with_the_reason(
        self, tools_client: AsyncClient
    ) -> None:
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "echo", "arguments": {"text": "", "times": 99}},
        )

        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "tool_invalid_input"
        # The fields are named so a model can correct itself.
        fields = {error["field"] for error in body["details"]["errors"]}
        assert fields == {"text", "times"}

    async def test_the_rejected_values_are_not_echoed_back(self, tools_client: AsyncClient) -> None:
        """A value reflected into an error is a value that can carry an
        injection, and occasionally a credential."""
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "echo", "arguments": {"text": "x" * 500}},
        )

        assert "xxxxx" not in response.text

    async def test_unknown_arguments_are_refused(self, tools_client: AsyncClient) -> None:
        """extra="forbid" on every input model.

        A tool that silently ignores an argument does something other than
        what was asked, and the caller has no way to tell.
        """
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "echo", "arguments": {"text": "hi", "sudo": True}},
        )

        assert response.status_code == 422

    async def test_an_unexpected_failure_discloses_nothing(self, tools_client: AsyncClient) -> None:
        """An exception's message carries paths, hostnames, and occasionally a
        credential from whatever it was talking to."""
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke", headers=headers, json={"name": "exploding", "arguments": {}}
        )

        assert response.status_code == 400
        assert "/Users/silviu" not in response.text
        assert response.json()["error"]["code"] == "tool_unexpected_error"

    async def test_secrets_in_output_never_reach_the_caller(
        self, tools_client: AsyncClient
    ) -> None:
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke", headers=headers, json={"name": "leaky", "arguments": {}}
        )

        body = response.text
        assert "sk-ant-api03" not in body
        assert "ghp_abcdefghijklmnopqrstuvwxyz" not in body
        # Redaction reaches the structured data too, which the app renders.
        assert "redacted" in body


class TestConfirmation:
    async def test_a_destructive_tool_does_not_run_on_the_first_call(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "demolish", "arguments": {"target": "nova-api"}},
        )

        assert response.status_code == 409
        assert demolish.destroyed == []

    async def test_the_prompt_says_what_would_happen(self, tools_client: AsyncClient) -> None:
        headers = await _signed_in(tools_client)

        body = (
            await tools_client.post(
                "/api/v1/tools/invoke",
                headers=headers,
                json={"name": "demolish", "arguments": {"target": "nova-api"}},
            )
        ).json()["error"]

        assert body["code"] == "tool_confirmation_required"
        assert "nova-api" in body["details"]["prompt"]
        assert body["details"]["confirmation_token"]

    async def test_confirming_runs_it(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        headers = await _signed_in(tools_client)
        request = {"name": "demolish", "arguments": {"target": "nova-api"}}

        first = (
            await tools_client.post("/api/v1/tools/invoke", headers=headers, json=request)
        ).json()
        token = first["error"]["details"]["confirmation_token"]

        response = await tools_client.post(
            "/api/v1/tools/invoke", headers=headers, json={**request, "confirmation_token": token}
        )

        assert response.status_code == 200
        assert demolish.destroyed == ["nova-api"]

    async def test_a_token_is_single_use(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        """Approving an action once is not approving it repeatedly."""
        headers = await _signed_in(tools_client)
        request = {"name": "demolish", "arguments": {"target": "nova-api"}}

        first = (
            await tools_client.post("/api/v1/tools/invoke", headers=headers, json=request)
        ).json()
        token = first["error"]["details"]["confirmation_token"]
        confirmed = {**request, "confirmation_token": token}

        await tools_client.post("/api/v1/tools/invoke", headers=headers, json=confirmed)
        replay = await tools_client.post("/api/v1/tools/invoke", headers=headers, json=confirmed)

        assert replay.status_code == 403
        assert replay.json()["error"]["code"] == "tool_confirmation_invalid"
        assert demolish.destroyed == ["nova-api"]

    async def test_a_token_does_not_authorise_different_arguments(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        """Otherwise approving "remove nova-test" would approve removing
        anything at all -- which is the whole attack."""
        headers = await _signed_in(tools_client)

        first = (
            await tools_client.post(
                "/api/v1/tools/invoke",
                headers=headers,
                json={"name": "demolish", "arguments": {"target": "nova-test"}},
            )
        ).json()
        token = first["error"]["details"]["confirmation_token"]

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={
                "name": "demolish",
                "arguments": {"target": "nova-production"},
                "confirmation_token": token,
            },
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "tool_confirmation_mismatch"
        assert demolish.destroyed == []

    async def test_a_mismatched_token_is_discarded_rather_than_returned(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        """A valid token presented for a different call is not trustworthy.

        Either the client is wrong or somebody is trying to redirect an
        approval; in both cases the approval is spent, and asking the person
        again costs one tap.
        """
        headers = await _signed_in(tools_client)
        request = {"name": "demolish", "arguments": {"target": "nova-test"}}

        first = (
            await tools_client.post("/api/v1/tools/invoke", headers=headers, json=request)
        ).json()
        token = first["error"]["details"]["confirmation_token"]

        await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={**request, "arguments": {"target": "elsewhere"}, "confirmation_token": token},
        )
        # The original, correct call now fails too: the token went with the
        # mismatch.
        retry = await tools_client.post(
            "/api/v1/tools/invoke", headers=headers, json={**request, "confirmation_token": token}
        )

        assert retry.status_code == 403
        assert retry.json()["error"]["code"] == "tool_confirmation_invalid"
        assert demolish.destroyed == []

    async def test_a_token_cannot_be_used_by_another_account(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        """Tokens are stored under the user id, so one cannot be replayed
        against a different account on the same NOVA."""
        mine = await _signed_in(tools_client)
        theirs = await _signed_in(tools_client)
        request = {"name": "demolish", "arguments": {"target": "nova-api"}}

        first = (await tools_client.post("/api/v1/tools/invoke", headers=mine, json=request)).json()
        token = first["error"]["details"]["confirmation_token"]

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=theirs,
            json={**request, "confirmation_token": token},
        )

        assert response.status_code == 403
        assert demolish.destroyed == []

    async def test_an_invented_token_is_refused(
        self, tools_client: AsyncClient, demolish: DemolishTool
    ) -> None:
        headers = await _signed_in(tools_client)

        response = await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={
                "name": "demolish",
                "arguments": {"target": "nova-api"},
                "confirmation_token": "not-a-real-token",
            },
        )

        assert response.status_code == 403
        assert demolish.destroyed == []


class TestAuditLog:
    async def test_a_successful_call_is_recorded(
        self, tools_client: AsyncClient, session_factory: Any
    ) -> None:
        headers = await _signed_in(tools_client)

        await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "echo", "arguments": {"text": "hello"}},
        )

        rows = await _invocations(session_factory)
        assert [(row.tool_name, row.status) for row in rows] == [("echo", "succeeded")]
        assert rows[0].initiated_by_model is False
        assert rows[0].request_id

    async def test_a_confirmed_call_is_marked_as_confirmed(
        self, tools_client: AsyncClient, session_factory: Any
    ) -> None:
        headers = await _signed_in(tools_client)
        request = {"name": "demolish", "arguments": {"target": "nova-api"}}

        first = (
            await tools_client.post("/api/v1/tools/invoke", headers=headers, json=request)
        ).json()
        await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={**request, "confirmation_token": first["error"]["details"]["confirmation_token"]},
        )

        rows = await _invocations(session_factory)
        succeeded = [row for row in rows if row.status == "succeeded"]
        assert len(succeeded) == 1
        assert succeeded[0].confirmed is True
        assert succeeded[0].permission == "destructive"

    async def test_arguments_are_stored_redacted(
        self, tools_client: AsyncClient, session_factory: Any
    ) -> None:
        """Arguments are a place credentials end up, particularly for the
        shell tool. The audit log must not become the leak."""
        headers = await _signed_in(tools_client)

        await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={
                "name": "echo",
                "arguments": {"text": "token=ghp_abcdefghijklmnopqrstuvwxyz0123"},
            },
        )

        rows = await _invocations(session_factory)
        assert "ghp_abcdef" not in str(rows[0].arguments)

    async def test_the_activity_feed_shows_what_happened(self, tools_client: AsyncClient) -> None:
        headers = await _signed_in(tools_client)
        await tools_client.post(
            "/api/v1/tools/invoke",
            headers=headers,
            json={"name": "echo", "arguments": {"text": "hello"}},
        )

        body = (await tools_client.get("/api/v1/system/activity", headers=headers)).json()

        assert body["items"][0]["tool_name"] == "echo"
        assert body["items"][0]["status"] == "succeeded"

    async def test_the_feed_is_scoped_to_the_caller(self, tools_client: AsyncClient) -> None:
        mine = await _signed_in(tools_client)
        theirs = await _signed_in(tools_client)

        await tools_client.post(
            "/api/v1/tools/invoke",
            headers=theirs,
            json={"name": "echo", "arguments": {"text": "not yours"}},
        )

        body = (await tools_client.get("/api/v1/system/activity", headers=mine)).json()
        assert body["items"] == []


class TestRefusalsAreAudited:
    """Refusals, tested one layer below the endpoint.

    A log of successes answers "what happened". A log that includes refusals
    answers "what was *attempted*", which is the question anyone asks after
    something goes wrong -- so both paths have to write a row.

    Exercised against the service rather than through HTTP because of the
    test fixtures, not the design. Every test runs inside one transaction on
    one connection, with application commits turned into savepoint releases,
    so a handler that raises rolls the request's savepoint back and takes the
    audit row with it. In production the audit session takes its own
    connection from the pool and commits independently, which is the whole
    reason ToolService holds a session *factory*. Testing through the
    endpoint here would assert the fixture's behaviour rather than NOVA's.
    """

    async def test_an_unknown_tool_is_recorded(
        self, registry: ToolRegistry, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        owner_id = await _a_user(session_factory)
        service = ToolService(
            registry=registry,
            settings=settings.tools,
            redis=redis_client,
            session_factory=session_factory,
        )

        with pytest.raises(ToolNotFoundError):
            await service.invoke("rm_rf", {}, tool_context(user_id=owner_id))

        rows = await _invocations(session_factory)
        assert [(row.tool_name, row.status, row.error_code) for row in rows] == [
            ("rm_rf", "refused", "tool_not_found")
        ]

    async def test_invalid_input_is_recorded(
        self, registry: ToolRegistry, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        owner_id = await _a_user(session_factory)
        service = ToolService(
            registry=registry,
            settings=settings.tools,
            redis=redis_client,
            session_factory=session_factory,
        )

        with pytest.raises(ToolInputError):
            await service.invoke("echo", {}, tool_context(user_id=owner_id))

        rows = await _invocations(session_factory)
        assert rows[0].status == "refused"
        assert rows[0].error_code == "tool_invalid_input"

    async def test_a_model_cannot_run_a_destructive_tool(
        self,
        registry: ToolRegistry,
        settings: Settings,
        redis_client: Any,
        session_factory: Any,
        demolish: DemolishTool,
    ) -> None:
        """The check that stops the chat path, asserted where it lives.

        A model never receives a confirmation token, so this is the branch
        that refuses it. It is in the service rather than only in the chat
        loop so that a second caller cannot reintroduce the hole.
        """
        owner_id = await _a_user(session_factory)
        service = ToolService(
            registry=registry,
            settings=settings.tools,
            redis=redis_client,
            session_factory=session_factory,
        )

        with pytest.raises(ToolPermissionError) as caught:
            await service.invoke(
                "demolish",
                {"target": "nova-api"},
                tool_context(user_id=owner_id, initiated_by_model=True),
            )

        assert caught.value.code == "tool_needs_confirmation"
        assert demolish.destroyed == []

    async def test_the_refusal_records_that_the_model_asked(
        self, registry: ToolRegistry, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        owner_id = await _a_user(session_factory)
        service = ToolService(
            registry=registry,
            settings=settings.tools,
            redis=redis_client,
            session_factory=session_factory,
        )

        with pytest.raises(ToolPermissionError):
            await service.invoke(
                "demolish",
                {"target": "nova-api"},
                tool_context(user_id=owner_id, initiated_by_model=True),
            )

        rows = await _invocations(session_factory)
        assert rows[0].initiated_by_model is True
        assert rows[0].confirmed is False
        assert rows[0].status == "refused"


async def _a_user(session_factory: Any) -> uuid.UUID:
    """A committed user row for the service-level tests to own rows under."""
    from nova.models.user import User

    async with session_factory() as session:
        user = User(
            email=f"svc-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            display_name="Tester",
        )
        session.add(user)
        await session.commit()
        return user.id


async def _invocations(session_factory: Any) -> list[ToolInvocation]:
    async with session_factory() as session:
        result = await session.execute(select(ToolInvocation).order_by(ToolInvocation.created_at))
        return list(result.scalars().all())


class SlowTool(Tool):
    """Hangs somewhere the tool's own deadline does not reach."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="slow",
            description="Never answers.",
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        import asyncio

        await asyncio.sleep(60)
        raise AssertionError("should have been given up on")  # pragma: no cover


class RefusingTool(Tool):
    """Raises a ToolError, the way a real tool reports a bad request."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="refusing",
            description="Says no.",
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        from nova.tools.errors import ToolError

        raise ToolError("That repository is not a repository.", code="tool_not_a_repository")


class FailingResultTool(Tool):
    """Returns a failure rather than raising one."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="failing_result",
            description="Reports a failure in its result.",
            group=ToolGroup.SYSTEM,
            permission=Permission.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult.failure("Docker is not running.", code="docker_unavailable")


def _service(
    registry: ToolRegistry, settings: Settings, redis: Any, session_factory: Any
) -> ToolService:
    return ToolService(
        registry=registry,
        settings=settings.tools,
        redis=redis,
        session_factory=session_factory,
    )


class TestFailuresAreAudited:
    """Every way a tool can fail writes a row, with a distinguishable code.

    "It didn't work" is not an answer anyone can act on. A tool that hung, a
    tool that refused the request, and a tool that reported a dead daemon are
    three different problems, and the log has to tell them apart afterwards.
    """

    async def test_a_tool_that_hangs_is_given_up_on_and_recorded(
        self, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        """The outer net.

        Tools that shell out enforce their own, shorter deadline. This
        catches one that hangs somewhere else -- a socket with no timeout of
        its own -- because a hung tool holds a chat turn open indefinitely.
        """
        from nova.tools.errors import ToolTimeoutError

        registry = ToolRegistry()
        registry.register(SlowTool())
        owner_id = await _a_user(session_factory)

        quick = settings.model_copy(deep=True)
        quick.tools.command_timeout_seconds = 0.05

        service = _service(registry, quick, redis_client, session_factory)

        with pytest.raises(ToolTimeoutError):
            await service.invoke("slow", {}, tool_context(user_id=owner_id))

        rows = await _invocations(session_factory)
        assert [(row.tool_name, row.status, row.error_code) for row in rows] == [
            ("slow", "timed_out", "tool_timeout")
        ]

    async def test_a_tool_that_refuses_is_recorded_with_its_own_code(
        self, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        from nova.tools.errors import ToolError

        registry = ToolRegistry()
        registry.register(RefusingTool())
        owner_id = await _a_user(session_factory)

        service = _service(registry, settings, redis_client, session_factory)

        with pytest.raises(ToolError):
            await service.invoke("refusing", {}, tool_context(user_id=owner_id))

        rows = await _invocations(session_factory)
        assert rows[0].status == "failed"
        # The tool's own code, not a generic one: this is what makes the log
        # answerable afterwards.
        assert rows[0].error_code == "tool_not_a_repository"

    async def test_a_failure_returned_rather_than_raised_is_still_recorded_as_one(
        self, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        """ "Docker is not running" comes back as a result, not an exception,
        because it is an answer. It is still a failed invocation."""
        registry = ToolRegistry()
        registry.register(FailingResultTool())
        owner_id = await _a_user(session_factory)

        service = _service(registry, settings, redis_client, session_factory)
        invocation = await service.invoke("failing_result", {}, tool_context(user_id=owner_id))

        assert invocation.result.is_error is True

        rows = await _invocations(session_factory)
        assert rows[0].status == "failed"
        assert rows[0].error_code == "docker_unavailable"

    async def test_an_unexpected_exception_does_not_reach_the_caller_intact(
        self, registry: ToolRegistry, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        """Its message can carry paths, hostnames, and occasionally a
        credential from whatever the tool was talking to."""
        from nova.tools.errors import ToolError

        owner_id = await _a_user(session_factory)
        service = _service(registry, settings, redis_client, session_factory)

        with pytest.raises(ToolError) as caught:
            await service.invoke("exploding", {}, tool_context(user_id=owner_id))

        assert caught.value.code == "tool_unexpected_error"
        assert "/Users/silviu/secrets" not in str(caught.value)

        rows = await _invocations(session_factory)
        assert rows[0].error_code == "tool_unexpected_error"


class TestWhenTheAuditLogItselfFails:
    async def test_a_failed_audit_write_does_not_fail_the_tool(
        self, registry: ToolRegistry, settings: Settings, redis_client: Any, session_factory: Any
    ) -> None:
        """Deliberate, and the trade-off is worth stating.

        Losing the database means losing the audit trail either way. Refusing
        every read-only tool as well would turn a logging outage into a
        total outage, and NOVA would stop being able to say what is wrong
        with the machine at exactly the moment somebody asks.
        """
        owner_id = await _a_user(session_factory)

        class BrokenFactory:
            def __call__(self) -> Any:
                raise RuntimeError("the pool is exhausted")

        service = ToolService(
            registry=registry,
            settings=settings.tools,
            redis=redis_client,
            session_factory=BrokenFactory(),  # type: ignore[arg-type]
        )

        invocation = await service.invoke(
            "echo", {"text": "still works"}, tool_context(user_id=owner_id)
        )

        assert invocation.result.content == "still works"


class TestWhenRedisIsDown:
    async def test_a_confirmation_cannot_be_consumed_and_the_tool_does_not_run(
        self,
        registry: ToolRegistry,
        settings: Settings,
        session_factory: Any,
        demolish: DemolishTool,
    ) -> None:
        """This fails closed, unlike the rate limiter.

        Losing Redis costs abuse protection there. Here it would mean running
        a destructive command nobody approved, so the answer is to refuse.
        """
        from redis.exceptions import ConnectionError as RedisConnectionError

        owner_id = await _a_user(session_factory)

        class DeadRedis:
            async def getdel(self, key: str) -> Any:
                raise RedisConnectionError("connection refused")

        service = ToolService(
            registry=registry,
            settings=settings.tools,
            redis=DeadRedis(),  # type: ignore[arg-type]
            session_factory=session_factory,
        )

        with pytest.raises(ToolPermissionError) as caught:
            await service.invoke(
                "demolish",
                {"target": "nova-db"},
                tool_context(user_id=owner_id),
                confirmation_token="whatever",
            )

        assert caught.value.code == "tool_confirmation_unavailable"
        assert demolish.destroyed == []


class TestRedactionThroughStructures:
    def test_a_credential_inside_a_list_is_redacted(self) -> None:
        """Arguments and tool ``data`` are JSON-ish, and the app renders
        ``data`` directly -- so a secret nested in a list is as much of a
        leak as one in the text."""
        from nova.services.tools import _redact_structure

        redacted = _redact_structure(
            {
                "args": ["--token", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"],
                "count": 3,
                "nested": [{"key": "ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz01"}],
            }
        )

        assert "ghp_abcdef" not in str(redacted)
        assert "sk-ant-api03" not in str(redacted)
        # Non-strings pass through untouched rather than being stringified.
        assert redacted["count"] == 3
