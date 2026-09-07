"""The request session must be unwound before the request returns.

A handler that raises is the ordinary case, not the exotic one: every 404
and every validation failure goes through it. If the scope around the
session is left suspended when that happens, nothing closes it until the
garbage collector reaches it -- and asyncio runs an abandoned async
generator's finalizer as a detached task, so the ROLLBACK lands on a pooled
connection whenever that happens to be, on whatever request owns it by then.
"""

from __future__ import annotations

import gc
import inspect
import types
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.api.deps import get_session
from nova.core.errors import NotFoundError

pytestmark = pytest.mark.integration


def _fake_request(session_factory: async_sessionmaker[AsyncSession]) -> types.SimpleNamespace:
    """The only thing ``get_session`` reads off the request."""
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(session_factory=session_factory))
    )


class TestFailingRequests:
    async def test_the_session_is_closed_when_the_handler_raises(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Drive the dependency the way FastAPI does on the error path.

        FastAPI throws the handler's exception into the dependency. That must
        unwind the session scope here and now; leaving it to the garbage
        collector is what puts a stray ROLLBACK on someone else's connection.
        """
        scope = get_session(_fake_request(session_factory))
        session = await anext(scope)
        await session.execute(text("SELECT 1"))
        assert session.in_transaction()

        with pytest.raises(NotFoundError):
            await scope.athrow(NotFoundError("boom", code="boom"))

        # Not "eventually, once the collector runs" -- now.
        assert not session.in_transaction()

    async def test_a_404_leaves_no_suspended_scope_behind(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """The same property over HTTP, on the path real clients take.

        Checked by looking for the generator itself rather than for a
        symptom: an abandoned one stays suspended -- ``ag_frame`` is not
        None -- until the collector reaches it, and by then it is running on
        someone else's connection.
        """
        response = await client.get(f"/api/v1/devices/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404

        suspended = [
            obj
            for obj in gc.get_objects()
            if inspect.isasyncgen(obj)
            and obj.ag_frame is not None
            and obj.ag_code.co_name == "session_scope"
        ]
        assert suspended == []
