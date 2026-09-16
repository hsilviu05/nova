"""The memory tools.

These are the ones a person actually notices. "Remember that I prefer
PostgreSQL", "what do you know about me", "forget that" -- all three are
tool calls, and all three have to behave exactly as the app's own memory
screen does, because they write to the same rows.

Memory is also the one place a tool may write without asking, so the limits
on that permission are tested here rather than taken on trust: the sensitive
filter runs on the explicit path too, and forgetting is still behind the
confirmation gate.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from nova.ai.registry import build_embedding_provider
from nova.core.config import Settings
from nova.models.memory import Memory
from nova.models.user import User
from nova.tools.base import Permission, ToolContext
from nova.tools.errors import ToolError
from nova.tools.knowledge import build_knowledge_tools

pytestmark = pytest.mark.integration


@pytest.fixture
async def owner(session_factory: Any) -> uuid.UUID:
    async with session_factory() as session:
        user = User(
            email=f"memtool-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="x",
            display_name="Tester",
        )
        session.add(user)
        await session.commit()
        return user.id


@pytest.fixture
def tools(settings: Settings, session_factory: Any) -> dict[str, Any]:
    built = build_knowledge_tools(
        session_factory=session_factory,
        embeddings=build_embedding_provider(settings.ai),
        settings=settings.ai,
    )
    return {tool.spec.name: tool for tool in built}


def _context(owner: uuid.UUID) -> ToolContext:
    return ToolContext(user_id=owner, initiated_by_model=True)


async def _run(tool: Any, owner: uuid.UUID, **arguments: Any) -> Any:
    return await tool.execute(tool.spec.input_model(**arguments), _context(owner))


async def _memories(session_factory: Any, owner: uuid.UUID) -> list[Memory]:
    async with session_factory() as session:
        result = await session.execute(select(Memory).where(Memory.user_id == owner))
        return list(result.scalars().all())


class TestPermissions:
    def test_searching_is_a_read(self, tools: dict[str, Any]) -> None:
        assert tools["memory_search"].spec.permission is Permission.READ

    def test_remembering_runs_without_asking(self, tools: dict[str, Any]) -> None:
        """NOVA already writes memories on its own from ordinary conversation.

        Prompting for the explicit "remember this" while the implicit version
        goes unremarked would be theatre, not consent.
        """
        assert tools["memory_create"].spec.permission is Permission.WRITE
        assert tools["memory_create"].spec.permission.needs_confirmation is False

    def test_forgetting_needs_confirmation(self, tools: dict[str, Any]) -> None:
        assert tools["memory_delete"].spec.permission is Permission.DESTRUCTIVE


class TestRemembering:
    async def test_a_stated_preference_is_stored(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        result = await _run(
            tools["memory_create"],
            owner,
            content="Prefers PostgreSQL for new projects",
            category="preference",
        )

        assert "Remembered" in result.content
        stored = await _memories(session_factory, owner)
        assert [memory.content for memory in stored] == ["Prefers PostgreSQL for new projects"]

    async def test_a_stated_memory_is_fully_confident(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        """Somebody saying it outright is the strongest evidence there is --
        unlike an inference from a passing remark, which is what the
        background extractor produces."""
        await _run(
            tools["memory_create"],
            owner,
            content="Works in Bucharest",
            category="fact",
        )

        stored = await _memories(session_factory, owner)
        assert stored[0].confidence == 1.0

    async def test_an_invented_category_is_refused_with_the_real_ones(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        with pytest.raises(ToolError) as caught:
            await _run(tools["memory_create"], owner, content="Something", category="vibes")

        assert caught.value.code == "memory_bad_category"
        assert "preference" in caught.value.message

    async def test_a_credential_is_never_stored(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        """The case this exists for: a model talked into remembering a secret.

        The same filter the background extractor is held to, applied to the
        explicit path as well.
        """
        result = await _run(
            tools["memory_create"],
            owner,
            content="Their database password is hunter2thing",
            category="fact",
        )

        assert result.is_error is True
        assert await _memories(session_factory, owner) == []


class TestSearching:
    async def test_finds_what_was_remembered_and_returns_ids(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        """The ids are the point: memory_update and memory_delete need one,
        and a model without them invents one."""
        await _run(
            tools["memory_create"],
            owner,
            content="Prefers PostgreSQL for new projects",
            category="preference",
        )

        result = await _run(tools["memory_search"], owner, query="database preference")

        assert result.data["memories"]
        assert uuid.UUID(result.data["memories"][0]["id"])

    async def test_nothing_remembered_says_so(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        result = await _run(tools["memory_search"], owner, query="quantum tuba repair")

        assert result.data["memories"] == []
        assert "Nothing remembered" in result.content

    async def test_one_account_cannot_search_another(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        """The retrieved text goes straight into a prompt, so this is a
        correctness boundary rather than an optimisation."""
        await _run(
            tools["memory_create"],
            owner,
            content="Prefers PostgreSQL for new projects",
            category="preference",
        )

        async with session_factory() as session:
            stranger = User(
                email=f"stranger-{uuid.uuid4().hex[:8]}@example.com",
                password_hash="x",
                display_name="Stranger",
            )
            session.add(stranger)
            await session.commit()
            stranger_id = stranger.id

        result = await _run(tools["memory_search"], stranger_id, query="database preference")
        assert result.data["memories"] == []


class TestCorrecting:
    async def test_editing_the_text_re_embeds_it(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        """Skipping that would leave a memory that reads correctly on screen
        but is still retrieved by whatever it used to say."""
        created = await _run(
            tools["memory_create"],
            owner,
            content="Prefers MySQL for new projects",
            category="preference",
        )
        before = (await _memories(session_factory, owner))[0].embedding

        await _run(
            tools["memory_update"],
            owner,
            memory_id=created.data["id"],
            content="Prefers PostgreSQL for new projects",
        )

        after = (await _memories(session_factory, owner))[0]
        assert after.content == "Prefers PostgreSQL for new projects"
        assert list(after.embedding) != list(before)

    async def test_an_invented_id_is_refused_with_advice(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        """A model that has not run a search will cheerfully invent one."""
        with pytest.raises(ToolError) as caught:
            await _run(tools["memory_update"], owner, memory_id="the-one-about-coffee", content="x")

        assert caught.value.code == "memory_bad_id"
        assert "memory_search" in caught.value.message

    async def test_a_well_formed_id_for_nothing_is_a_clear_miss(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        with pytest.raises(ToolError) as caught:
            await _run(
                tools["memory_update"],
                owner,
                memory_id=str(uuid.uuid4()),
                content="x",
            )

        assert caught.value.code == "memory_not_found"

    async def test_updating_nothing_is_refused(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        created = await _run(
            tools["memory_create"], owner, content="Something true", category="fact"
        )

        with pytest.raises(ToolError) as caught:
            await _run(tools["memory_update"], owner, memory_id=created.data["id"])

        assert caught.value.code == "memory_no_change"


class TestForgetting:
    async def test_the_confirmation_names_the_memory_in_plain_words(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        """Nobody can consent to forgetting "a3f1c8…"."""
        created = await _run(
            tools["memory_create"],
            owner,
            content="Prefers PostgreSQL for new projects",
            category="preference",
        )
        tool = tools["memory_delete"]

        prompt = await tool.describe(
            tool.spec.input_model(memory_id=created.data["id"]), _context(owner)
        )

        assert prompt is not None
        assert "Prefers PostgreSQL for new projects" in prompt

    async def test_the_confirmation_cannot_read_another_account(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        """Otherwise the prompt becomes a way to read somebody else's
        memories one id at a time."""
        created = await _run(
            tools["memory_create"], owner, content="A private thing", category="fact"
        )
        tool = tools["memory_delete"]

        prompt = await tool.describe(
            tool.spec.input_model(memory_id=created.data["id"]), _context(uuid.uuid4())
        )

        assert prompt is None

    async def test_an_id_that_is_not_an_id_produces_no_prompt(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        """A model can invent an id. Describing it must not raise: the
        refusal belongs to ``execute``, which says what to do about it, and
        a failure here would turn that into a 500 instead."""
        tool = tools["memory_delete"]

        prompt = await tool.describe(
            tool.spec.input_model(memory_id="the one about postgres"), _context(owner)
        )

        assert prompt is None

    async def test_an_id_that_is_not_an_id_is_refused_with_advice(
        self, tools: dict[str, Any], owner: uuid.UUID
    ) -> None:
        tool = tools["memory_delete"]

        with pytest.raises(ToolError) as caught:
            await _run(tool, owner, memory_id="the one about postgres")

        assert caught.value.code == "memory_bad_id"
        assert "memory_search" in str(caught.value)

    async def test_forgetting_removes_the_row(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        created = await _run(
            tools["memory_create"], owner, content="Something to forget", category="fact"
        )

        await _run(tools["memory_delete"], owner, memory_id=created.data["id"])

        assert await _memories(session_factory, owner) == []

    async def test_forgetting_someone_elses_memory_is_a_miss(
        self, tools: dict[str, Any], owner: uuid.UUID, session_factory: Any
    ) -> None:
        created = await _run(tools["memory_create"], owner, content="Not yours", category="fact")

        with pytest.raises(ToolError) as caught:
            await _run(tools["memory_delete"], uuid.uuid4(), memory_id=created.data["id"])

        assert caught.value.code == "memory_not_found"
        assert len(await _memories(session_factory, owner)) == 1
