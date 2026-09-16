"""Running tools: the gate everything goes through.

A tool implements behaviour. This decides whether that behaviour is allowed
to happen, and records that it did. Splitting it this way means the rules are
in one readable place rather than repeated -- and occasionally forgotten --
across a dozen tools.

In order, every invocation:

1. **resolves the tool**, or is refused as unknown;
2. **validates the arguments** against the tool's own input model;
3. **checks the permission**, which for a ``DESTRUCTIVE`` tool means a
   confirmation token a person obtained by being shown what would happen;
4. **runs it**, with the tool's own timeout;
5. **cleans the output** -- secrets redacted, control sequences stripped,
   impersonation defanged, size capped;
6. **records it**, whatever the outcome.

Step 6 runs on every path, including refusals. A log that only shows what
succeeded cannot answer the question anyone actually asks after an incident,
which is what was attempted.

Confirmations live in Redis rather than Postgres. They are short-lived by
design, they are answered within seconds or not at all, and losing them in a
restart is correct behaviour: a confirmation is an answer to a question asked
a moment ago, and one that outlived the process it was asked in is stale.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.core.config import ToolSettings
from nova.core.logging import get_logger
from nova.models.tool_invocation import ToolInvocation
from nova.repositories.tool_invocation import ToolInvocationRepository
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from nova.tools.errors import (
    ToolConfirmationRequired,
    ToolError,
    ToolInputError,
    ToolPermissionError,
    ToolTimeoutError,
)
from nova.tools.registry import ToolRegistry
from nova.tools.safety import clean_tool_output, redact_secrets

logger = get_logger(__name__)

_CONFIRMATION_NAMESPACE = "tool-confirmation"

# Arguments this long are not arguments. Capped before storage so the audit
# log cannot be used to write bulk data into the database.
_MAX_STORED_ARGUMENT_CHARS = 2000


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """A destructive call waiting for a person to agree to it."""

    token: str
    tool_name: str
    prompt: str
    arguments: dict[str, Any]
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class Invocation:
    """A finished tool call, as the API and the chat loop both see it."""

    tool_name: str
    result: ToolResult
    duration_ms: int


class ToolService:
    """Executes tools on behalf of one signed-in person.

    Owns a session factory rather than a session: a tool called from the
    middle of a streamed reply runs after the request's unit of work has
    closed, so the audit write needs its own short transaction. The same
    constraint that shapes :class:`~nova.services.conversation.ChatStreamer`.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        settings: ToolSettings,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._redis = redis
        self._session_factory = session_factory

    # -- what the app can see ---------------------------------------------

    def specs(self) -> list[ToolSpec]:
        return self._registry.specs()

    def definitions_for_model(self) -> tuple[Any, ...]:
        """The tools the model is allowed to see.

        Destructive tools are included: the model needs to know they exist to
        propose one, and proposing is the whole interaction. What it cannot
        do is run one -- :meth:`invoke` refuses without a confirmation token,
        and the model has no way to obtain one.
        """
        return tuple(spec.as_definition() for spec in self._registry.specs())

    # -- running ----------------------------------------------------------

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        confirmation_token: str | None = None,
    ) -> Invocation:
        """Run one tool.

        Raises:
            ToolNotFoundError: no such tool on this machine.
            ToolInputError: the arguments do not fit the tool's schema.
            ToolConfirmationRequired: the tool is destructive and no valid
                confirmation was presented. The error carries the token to
                present next, in ``details``.
            ToolPermissionError: the model asked for something it may not run.
            ToolTimeoutError: the tool overran its budget.
        """
        started = time.perf_counter()

        try:
            tool = self._registry.get(name)
        except ToolError as exc:
            await self._record(
                name=name,
                group="unknown",
                permission=Permission.READ,
                context=context,
                arguments=arguments,
                status="refused",
                error_code=exc.code,
                duration_ms=0,
                confirmed=False,
            )
            raise

        spec = tool.spec
        try:
            payload = self._validate(spec, arguments)
            confirmed = await self._authorise(
                tool, spec, payload, context, confirmation_token=confirmation_token
            )
        except ToolError as exc:
            await self._record(
                name=spec.name,
                group=spec.group.value,
                permission=spec.permission,
                context=context,
                arguments=arguments,
                status="refused",
                error_code=exc.code,
                duration_ms=self._elapsed(started),
                confirmed=False,
            )
            raise

        status = "succeeded"
        error_code: str | None = None

        try:
            raw = await asyncio.wait_for(
                tool.execute(payload, context),
                timeout=self._settings.command_timeout_seconds + 5,
            )
        except TimeoutError:
            # The outer net. Tools that shell out enforce their own, shorter
            # deadline; this catches one that hangs somewhere else, such as a
            # socket with no timeout of its own.
            await self._record(
                name=spec.name,
                group=spec.group.value,
                permission=spec.permission,
                context=context,
                arguments=arguments,
                status="timed_out",
                error_code="tool_timeout",
                duration_ms=self._elapsed(started),
                confirmed=confirmed,
            )
            raise ToolTimeoutError() from None
        except ToolError as exc:
            await self._record(
                name=spec.name,
                group=spec.group.value,
                permission=spec.permission,
                context=context,
                arguments=arguments,
                status="failed",
                error_code=exc.code,
                duration_ms=self._elapsed(started),
                confirmed=confirmed,
            )
            raise
        except Exception as exc:
            # Detail is logged, not returned: an unexpected exception's
            # message can carry paths, hostnames, and occasionally a
            # credential from whatever it was talking to.
            logger.exception("tool_unexpected_failure", tool=spec.name, error=type(exc).__name__)
            await self._record(
                name=spec.name,
                group=spec.group.value,
                permission=spec.permission,
                context=context,
                arguments=arguments,
                status="failed",
                error_code="tool_unexpected_error",
                duration_ms=self._elapsed(started),
                confirmed=confirmed,
            )
            raise ToolError(
                f"{spec.name} failed unexpectedly.", code="tool_unexpected_error"
            ) from exc

        result = self._clean(raw)
        if result.is_error:
            status = "failed"
            error_code = str(result.data.get("error") or "tool_failed")

        duration_ms = self._elapsed(started)
        await self._record(
            name=spec.name,
            group=spec.group.value,
            permission=spec.permission,
            context=context,
            arguments=arguments,
            status=status,
            error_code=error_code,
            duration_ms=duration_ms,
            confirmed=confirmed,
        )
        return Invocation(tool_name=spec.name, result=result, duration_ms=duration_ms)

    # -- confirmation ------------------------------------------------------

    async def propose(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> PendingConfirmation:
        """Describe what a destructive call would do and mint a token for it.

        The token is bound to the tool, the arguments, *and* the person: it
        is stored under a key that includes the user id, so a token cannot be
        replayed against another account, and it cannot be edited into
        approval for a different command than the one that was shown.
        """
        tool = self._registry.get(name)
        spec = tool.spec
        payload = self._validate(spec, arguments)

        prompt = await self._prompt_for(tool, spec, payload, context, arguments)
        token = secrets.token_urlsafe(24)

        await self._redis.setex(
            self._confirmation_key(context.user_id, token),
            self._settings.confirmation_ttl_seconds,
            json.dumps({"tool": spec.name, "arguments": arguments}, sort_keys=True),
        )
        logger.info("tool_confirmation_issued", tool=spec.name, user_id=str(context.user_id))

        return PendingConfirmation(
            token=token,
            tool_name=spec.name,
            prompt=prompt,
            arguments=arguments,
            expires_in_seconds=self._settings.confirmation_ttl_seconds,
        )

    # -- internals ---------------------------------------------------------

    def _validate(self, spec: ToolSpec, arguments: dict[str, Any]) -> BaseModel:
        """Coerce arguments into the tool's input model.

        The error detail lists which fields were wrong and why, without
        echoing the values. A model that sent a bad argument can correct
        itself from that, and a value reflected back into a prompt is a value
        that could carry an injection.
        """
        try:
            return spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            problems = [
                {
                    "field": ".".join(str(part) for part in error["loc"]) or "(root)",
                    "problem": error["msg"],
                }
                for error in exc.errors()
            ]
            raise ToolInputError(
                f"{spec.name} was called with arguments it cannot accept.",
                details={"errors": problems},
            ) from exc

    async def _authorise(
        self,
        tool: Tool,
        spec: ToolSpec,
        payload: BaseModel,
        context: ToolContext,
        *,
        confirmation_token: str | None,
    ) -> bool:
        """Decide whether this call may run. Returns whether it was confirmed.

        Raises:
            ToolPermissionError: for a model asking to run something
                destructive directly. A model never receives a confirmation
                token, so this is the path that stops it -- the check is here
                and not only in the chat loop, because a second caller should
                not be able to reintroduce the hole.
            ToolConfirmationRequired: for a person who has not yet agreed.
        """
        if not spec.permission.needs_confirmation:
            return False

        if context.initiated_by_model and confirmation_token is None:
            raise ToolPermissionError(
                f"{spec.name} changes things, so it needs your confirmation before it runs.",
                code="tool_needs_confirmation",
                details={"tool": spec.name},
            )

        if confirmation_token is None:
            pending = await self.propose(spec.name, payload.model_dump(mode="json"), context)
            raise ToolConfirmationRequired(
                pending.prompt,
                details={
                    "tool": pending.tool_name,
                    "confirmation_token": pending.token,
                    "prompt": pending.prompt,
                    "expires_in_seconds": pending.expires_in_seconds,
                },
            )

        await self._consume(spec.name, payload, context, confirmation_token)
        return True

    async def _consume(
        self, name: str, payload: BaseModel, context: ToolContext, token: str
    ) -> None:
        """Spend a confirmation token, or refuse.

        Single use: the token is deleted as it is read, so a replay finds
        nothing. Approving an action once is not approving it repeatedly.
        """
        key = self._confirmation_key(context.user_id, token)
        try:
            stored = await self._redis.getdel(key)
        except RedisError as exc:
            # Unlike the rate limiter, this does not fail open. Losing Redis
            # costs abuse protection there; here it would mean running a
            # destructive command nobody approved.
            logger.error("confirmation_store_unavailable", error=str(exc))
            raise ToolPermissionError(
                "Confirmations are unavailable right now, so NOVA will not run this.",
                code="tool_confirmation_unavailable",
            ) from exc

        if stored is None:
            raise ToolPermissionError(
                "That confirmation has expired or was already used. Ask again.",
                code="tool_confirmation_invalid",
            )

        record = json.loads(stored)
        # The arguments are compared, not just the name. Otherwise a
        # confirmation for "remove container nova-test" would authorise
        # removing any container at all.
        #
        # Note that the token is already spent by the GETDEL above, and a
        # mismatch does not put it back. That is the safe reading: a request
        # carrying a valid token for a *different* call is either a client
        # bug or an attempt to redirect an approval, and in both cases the
        # approval is no longer worth trusting. Asking the person again costs
        # one tap. Both sides are compared after validation through the same
        # input model, so an honest client cannot lose a token to a
        # serialisation difference -- only to actually asking for something
        # else.
        if record.get("tool") != name or record.get("arguments") != payload.model_dump(mode="json"):
            logger.warning("confirmation_mismatch", tool=name, user_id=str(context.user_id))
            raise ToolPermissionError(
                "That confirmation was for a different action, so NOVA has discarded it. "
                "Ask again.",
                code="tool_confirmation_mismatch",
            )

    async def _prompt_for(
        self,
        tool: Tool,
        spec: ToolSpec,
        payload: BaseModel,
        context: ToolContext,
        arguments: dict[str, Any],
    ) -> str:
        """The sentence the person is shown before approving.

        A tool that cannot describe its own call falls back to the template.
        Failure here must not block the confirmation: being unable to write a
        nicer prompt is not a reason to run without one.
        """
        try:
            described = await tool.describe(payload, context)
        except Exception:  # pragma: no cover - defensive
            logger.warning("tool_describe_failed", tool=spec.name)
            described = None

        return redact_secrets(described or spec.describe_call(arguments))

    def _clean(self, result: ToolResult) -> ToolResult:
        """Make a result safe to store, show, and send to a model."""
        content, truncated = clean_tool_output(
            result.content, max_bytes=self._settings.max_output_bytes
        )
        return ToolResult(
            content=content,
            data=_redact_structure(result.data),
            is_error=result.is_error,
            truncated=result.truncated or truncated,
        )

    async def _record(
        self,
        *,
        name: str,
        group: str,
        permission: Permission,
        context: ToolContext,
        arguments: dict[str, Any],
        status: str,
        error_code: str | None,
        duration_ms: int,
        confirmed: bool,
    ) -> None:
        """Write the audit row. Never raises.

        An audit failure must not turn a successful tool call into an error
        the caller sees -- the work happened, and reporting otherwise would
        be a lie. It is logged loudly instead, because a silent gap in the
        log is worse than a noisy one.
        """
        try:
            async with self._session_factory() as session:
                ToolInvocationRepository(session).add(
                    ToolInvocation(
                        user_id=context.user_id,
                        conversation_id=context.conversation_id,
                        tool_name=name[:64],
                        tool_group=group[:24],
                        permission=permission.value,
                        status=status,
                        error_code=error_code[:64] if error_code else None,
                        initiated_by_model=context.initiated_by_model,
                        confirmed=confirmed,
                        arguments=_redact_structure(arguments),
                        duration_ms=max(duration_ms, 0),
                        request_id=context.request_id,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("tool_audit_write_failed", tool=name, status=status)

        logger.info(
            "tool_invoked",
            tool=name,
            status=status,
            permission=permission.value,
            by_model=context.initiated_by_model,
            confirmed=confirmed,
            duration_ms=duration_ms,
            error_code=error_code,
        )

    @staticmethod
    def _confirmation_key(user_id: Any, token: str) -> str:
        return f"{_CONFIRMATION_NAMESPACE}:{user_id}:{token}"

    @staticmethod
    def _elapsed(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)


def _redact_structure(value: Any) -> Any:
    """Apply secret redaction through a nested structure.

    Tool ``data`` and stored arguments are JSON-ish, and a credential in
    either is as much of a leak as one in the text -- more, because the app
    renders ``data`` directly.
    """
    if isinstance(value, str):
        return redact_secrets(value[:_MAX_STORED_ARGUMENT_CHARS])
    if isinstance(value, dict):
        return {str(key): _redact_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    return value


def tool_context(
    *,
    user_id: uuid.UUID,
    request_id: str | None = None,
    conversation_id: uuid.UUID | None = None,
    initiated_by_model: bool = False,
) -> ToolContext:
    """Build a context, so callers do not have to know the field order."""
    return ToolContext(
        user_id=user_id,
        request_id=request_id,
        conversation_id=conversation_id,
        initiated_by_model=initiated_by_model,
    )
