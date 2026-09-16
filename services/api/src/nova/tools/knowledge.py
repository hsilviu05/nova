"""Tools for the thing NOVA already does best: remembering.

Memory is the one place a tool may write without asking, and the reason is
that NOVA already writes there on its own -- the extractor records what it
learned from every exchange in the background. Prompting for the explicit
"remember that I prefer Postgres" while the implicit version goes unremarked
would be theatre, not consent. What makes that acceptable is the rest of the
feature: every memory is listed, editable and deletable in the app, and every
tool write lands in the audit log.

Forgetting is different, and is behind the confirmation gate. The prompt
names the memory in the person's own words rather than by id, because "Forget
a3f1c…?" is not a question anyone can answer.

These tools own their sessions. A tool called mid-reply runs after the
request's unit of work has closed -- the same constraint that shapes
:class:`~nova.services.conversation.ChatStreamer`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.ai.base import EmbeddingProvider
from nova.core.config import AISettings
from nova.core.errors import NotFoundError
from nova.core.logging import get_logger
from nova.models.memory import CATEGORIES, Memory
from nova.repositories.memory import MemoryRepository
from nova.schemas.memory import MemoryUpdate
from nova.services.memory import MemoryService, looks_sensitive
from nova.tools.base import (
    Permission,
    Tool,
    ToolContext,
    ToolGroup,
    ToolResult,
    ToolSpec,
    narrow,
)
from nova.tools.errors import ToolError

logger = get_logger(__name__)

MemoryCategoryField = Field(
    description=f"One of: {', '.join(CATEGORIES)}.",
)


class MemorySearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description="What to look for, in plain language.",
        ),
    ]


class MemoryCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[
        str,
        Field(
            min_length=1,
            max_length=300,
            description=(
                "One short, self-contained sentence in the third person, e.g. "
                '"Prefers PostgreSQL for new projects." It will be read months '
                "later with none of this conversation around it."
            ),
        ),
    ]
    category: Annotated[str, MemoryCategoryField]
    importance: Annotated[
        float,
        Field(
            default=0.6, ge=0.0, le=1.0, description="How much this should shape future replies."
        ),
    ] = 0.6


class MemoryUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: Annotated[str, Field(description="Id from memory_search.")]
    content: Annotated[str | None, Field(default=None, min_length=1, max_length=300)] = None
    category: Annotated[str | None, MemoryCategoryField] = None
    importance: Annotated[float | None, Field(default=None, ge=0.0, le=1.0)] = None


class MemoryDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: Annotated[str, Field(description="Id from memory_search.")]


class _KnowledgeTool(Tool):
    """Shared session and embedding wiring for the memory tools."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embeddings: EmbeddingProvider,
        settings: AISettings,
    ) -> None:
        self._session_factory = session_factory
        self._embeddings = embeddings
        self._settings = settings

    def _service(self, session: AsyncSession) -> MemoryService:
        return MemoryService(
            memories=MemoryRepository(session),
            embeddings=self._embeddings,
            settings=self._settings,
        )

    @staticmethod
    def _memory_id(raw: str) -> uuid.UUID:
        """Parse an id the model supplied.

        A model that has not run a search will cheerfully invent one, so this
        is a real input path rather than a formality.
        """
        try:
            return uuid.UUID(raw)
        except ValueError:
            raise ToolError(
                "That is not a memory id. Use memory_search first and pass an id from its results.",
                code="memory_bad_id",
            ) from None

    async def _content_of(self, memory_id: str, owner_id: uuid.UUID) -> str | None:
        """The text of one memory, for a confirmation prompt."""
        try:
            parsed = uuid.UUID(memory_id)
        except ValueError:
            return None

        async with self._session_factory() as session:
            memory = await MemoryRepository(session).get_for_owner(parsed, owner_id)
            return memory.content if memory else None


class MemorySearchTool(_KnowledgeTool):
    """Find what NOVA already knows."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_search",
            description=(
                "Search what NOVA remembers about this person, by meaning "
                "rather than keyword. Returns each memory with its id, so the "
                "id can be passed to memory_update or memory_delete. Use this "
                "before claiming NOVA does or does not remember something."
            ),
            group=ToolGroup.KNOWLEDGE,
            permission=Permission.READ,
            input_model=MemorySearchInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, MemorySearchInput)

        async with self._session_factory() as session:
            matches = await self._service(session).retrieve(context.user_id, payload.query)
            await session.commit()

        if not matches:
            return ToolResult(
                content="Nothing remembered that relates to that.",
                data={"memories": []},
            )

        memories = [
            {
                "id": str(match.memory.id),
                "content": match.memory.content,
                "category": match.memory.category,
                "importance": match.memory.importance,
                "similarity": round(match.similarity, 3),
            }
            for match in matches
        ]
        lines = [f"[{m['id']}] ({m['category']}) {m['content']}" for m in memories]
        return ToolResult(content="\n".join(lines), data={"memories": memories})


class MemoryCreateTool(_KnowledgeTool):
    """Remember something on purpose."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_create",
            description=(
                "Record something durable about this person, when they ask you "
                "to remember it or state a lasting preference. Not for passing "
                "remarks, and never for credentials, card numbers, addresses or "
                "medical details, even if offered."
            ),
            group=ToolGroup.KNOWLEDGE,
            permission=Permission.WRITE,
            input_model=MemoryCreateInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, MemoryCreateInput)

        category = payload.category.strip().lower()
        if category not in CATEGORIES:
            raise ToolError(
                f"{payload.category!r} is not a memory category. One of: {', '.join(CATEGORIES)}.",
                code="memory_bad_category",
            )

        # The same filter the background extractor is held to. A model that
        # has been talked into storing a password is exactly the case this
        # exists for, so it runs on the explicit path too.
        content = " ".join(payload.content.split())
        if looks_sensitive(content):
            logger.warning("memory_tool_rejected_sensitive", user_id=str(context.user_id))
            return ToolResult.failure(
                "That looks like a credential or an identifier, and NOVA does not store those.",
                code="memory_sensitive",
            )

        vectors = await self._embeddings.embed([content])
        async with self._session_factory() as session:
            memory = Memory(
                user_id=context.user_id,
                content=content,
                category=category,
                importance=payload.importance,
                # Stated outright by the person, which is the strongest
                # evidence there is -- unlike an inference from a passing
                # remark, which is what the extractor produces.
                confidence=1.0,
                embedding=vectors[0],
                embedding_provider=self._embeddings.name,
                source_conversation_id=context.conversation_id,
            )
            MemoryRepository(session).add(memory)
            await session.commit()
            memory_id = str(memory.id)

        logger.info("memory_created_by_tool", memory_id=memory_id)
        return ToolResult(
            content=f"Remembered: {content}",
            data={"id": memory_id, "content": content, "category": category},
        )


class MemoryUpdateTool(_KnowledgeTool):
    """Correct something NOVA believes."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_update",
            description=(
                "Correct a memory NOVA already holds. Use this when the person "
                "says something you remember is wrong or out of date, rather "
                "than storing a second, contradictory memory."
            ),
            group=ToolGroup.KNOWLEDGE,
            permission=Permission.WRITE,
            input_model=MemoryUpdateInput,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, MemoryUpdateInput)
        memory_id = self._memory_id(payload.memory_id)

        if payload.content is None and payload.category is None and payload.importance is None:
            raise ToolError("Nothing to change.", code="memory_no_change")

        update = MemoryUpdate.model_validate(
            {
                "content": payload.content,
                "category": payload.category.strip().lower() if payload.category else None,
                "importance": payload.importance,
            }
        )

        async with self._session_factory() as session:
            try:
                updated = await self._service(session).update(memory_id, context.user_id, update)
            except NotFoundError:
                raise ToolError(
                    "No such memory. Run memory_search to find the right id.",
                    code="memory_not_found",
                ) from None
            await session.commit()

        return ToolResult(
            content=f"Updated: {updated.content}",
            data={"id": str(updated.id), "content": updated.content, "category": updated.category},
        )


class MemoryDeleteTool(_KnowledgeTool):
    """Forget something, once the person has confirmed which something."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="memory_delete",
            description=(
                "Forget one memory permanently. Use when the person asks you to "
                "forget something. Search first so the right memory is named."
            ),
            group=ToolGroup.KNOWLEDGE,
            permission=Permission.DESTRUCTIVE,
            input_model=MemoryDeleteInput,
            confirmation_prompt="Forget this memory permanently?",
        )

    async def describe(self, arguments: BaseModel, context: ToolContext) -> str | None:
        """Name the memory in the person's own words.

        Without this the prompt would quote a UUID, which nobody can agree
        to. Scoped to the caller, so the confirmation cannot be turned into a
        way to read somebody else's memories one id at a time.
        """
        payload = narrow(arguments, MemoryDeleteInput)
        content = await self._content_of(payload.memory_id, context.user_id)
        return f"Forget this permanently: “{content}”" if content else None

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        payload = narrow(arguments, MemoryDeleteInput)
        memory_id = self._memory_id(payload.memory_id)

        async with self._session_factory() as session:
            try:
                await self._service(session).delete(memory_id, context.user_id)
            except NotFoundError:
                raise ToolError(
                    "No such memory. Run memory_search to find the right id.",
                    code="memory_not_found",
                ) from None
            await session.commit()

        return ToolResult(content="Forgotten.", data={"id": str(memory_id), "deleted": True})


def build_knowledge_tools(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingProvider,
    settings: AISettings,
) -> list[Tool]:
    return [
        MemorySearchTool(session_factory=session_factory, embeddings=embeddings, settings=settings),
        MemoryCreateTool(session_factory=session_factory, embeddings=embeddings, settings=settings),
        MemoryUpdateTool(session_factory=session_factory, embeddings=embeddings, settings=settings),
        MemoryDeleteTool(session_factory=session_factory, embeddings=embeddings, settings=settings),
    ]
