"""Long-term memory: extraction, storage, retrieval.

Three collaborators, split by lifetime and by what can fail.

:class:`MemoryExtractor` turns an exchange into candidate memories with a
model call. It is the only part that can produce nonsense, so it is isolated
and returns nothing rather than raising: a bad extraction must never damage a
conversation that has already been answered successfully.

:class:`MemoryService` is request-scoped and owns everything a person does to
their own memories -- retrieval before a reply, and the list/edit/delete the
app exposes.

:class:`MemoryRecorder` owns its own sessions, because extraction runs *after*
a reply has been sent. A model call in the request path would add its latency
to every message for something the user never sees.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nova.ai.base import ChatMessage, ChatProvider, ChatRequest, EmbeddingProvider
from nova.ai.errors import AIProviderError
from nova.core.clock import utc_now
from nova.core.config import AISettings
from nova.core.errors import NotFoundError
from nova.core.logging import get_logger
from nova.models.memory import CATEGORIES, Memory
from nova.repositories.memory import MemoryRepository, ScoredMemory
from nova.schemas.memory import MemoryRead, MemoryUpdate

logger = get_logger(__name__)

# One exchange is one or two things worth keeping. A cap stops a rambling
# extraction from filling the store with near-restatements of one message.
MAX_MEMORIES_PER_EXCHANGE = 5

# A memory is a sentence. Anything longer is the model summarising the
# conversation rather than extracting a fact, and it would dominate the
# retrieval budget of every later reply.
MAX_MEMORY_LENGTH = 300

# Cosine distance below which a new memory is treated as one NOVA already
# has. Tight on purpose: merging two memories that were not the same thing
# loses information silently, while a near-duplicate merely wastes a row.
DEDUPLICATION_DISTANCE = 0.12

EXTRACTION_MAX_TOKENS = 700

EXTRACTION_PROMPT = f"""\
You extract durable facts about a person from a conversation, so a companion \
can remember them later.

Return a JSON array and nothing else. No prose, no code fences. Each element:

{{"content": "...", "category": "...", "importance": 0.0, "confidence": 0.0}}

- content: one short sentence in the third person, e.g. "Drinks coffee every \
morning before work". Self-contained: it will be read months later with none \
of this conversation around it. Never a question, never advice, never \
something the assistant said about itself.
- category: exactly one of {", ".join(CATEGORIES)}.
- importance: 0-1. How much this should shape future replies. A dietary \
restriction is high; a passing mention of the weather is not worth extracting \
at all.
- confidence: 0-1. How sure you are it is true and durable. Hedged, \
hypothetical, or one-off statements are low.

Extract only what is durable. Skip anything true of just this moment, \
anything the assistant introduced rather than the person, and anything you \
would be restating from an earlier turn.

Never extract credentials, passwords, card or account numbers, government \
identifiers, precise home addresses, or medical details -- even if freely \
offered, and even if it seems useful.

If there is nothing durable, return []. That is the common case and is a \
correct answer.\
"""

# Belt and braces on the instruction above. The model is asked not to extract
# secrets; this is what happens when it does anyway. Deliberately narrow --
# these match shapes that are never a legitimate memory, so a false positive
# costs nothing.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 13-19 digits, optionally grouped: card and account numbers. Phone
    # numbers are shorter and do not trip this.
    re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
    # A labelled secret followed by its value. The separator is required, so
    # "keeps forgetting their password" is still a storable memory while
    # "password is hunter2" is not.
    re.compile(
        r"\b(?:password|passcode|api[ _-]?key|secret|token|pin)\b\s*(?:is|=|:)\s*\S{4,}",
        re.IGNORECASE,
    ),
    # A long unbroken token: a key or credential pasted verbatim. Hyphens are
    # excluded so an ordinary hyphenated phrase does not match.
    re.compile(r"\b[A-Za-z0-9_]{32,}\b"),
)


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """One thing the extractor proposes remembering."""

    content: str
    category: str
    importance: float
    confidence: float


class MemoryExtractor:
    """Proposes memories from an exchange, using the chat provider."""

    def __init__(self, *, provider: ChatProvider, settings: AISettings) -> None:
        self._provider = provider
        self._settings = settings

    async def extract(self, *, user_text: str, assistant_text: str) -> list[MemoryCandidate]:
        """Return candidates from one exchange, or an empty list.

        Never raises. Extraction is a background improvement to a reply the
        person has already received, so every failure mode -- an unreachable
        provider, unparseable output, a model that answered conversationally
        instead of with JSON -- degrades to remembering nothing.
        """
        if not user_text.strip():
            return []

        transcript = f"Person: {user_text.strip()}"
        if assistant_text.strip():
            transcript += f"\nCompanion: {assistant_text.strip()}"

        try:
            completion = await self._provider.complete(
                ChatRequest(
                    system=EXTRACTION_PROMPT,
                    messages=[ChatMessage(role="user", content=transcript)],
                    max_tokens=EXTRACTION_MAX_TOKENS,
                )
            )
        except AIProviderError as exc:
            logger.warning("memory_extraction_failed", error=exc.code)
            return []

        candidates = parse_candidates(completion.text)
        return [c for c in candidates if c.confidence >= self._settings.memory_min_confidence]


class MemoryService:
    """Request-scoped memory operations for one signed-in person."""

    def __init__(
        self,
        *,
        memories: MemoryRepository,
        embeddings: EmbeddingProvider,
        settings: AISettings,
    ) -> None:
        self._memories = memories
        self._embeddings = embeddings
        self._settings = settings

    # -- retrieval --------------------------------------------------------

    async def retrieve(self, owner_id: uuid.UUID, query: str) -> list[ScoredMemory]:
        """Memories relevant to ``query``, nearest first.

        Recall is recorded here rather than by the caller, so a memory that
        actually influenced a reply is distinguishable from one that has sat
        untouched since it was written.
        """
        limit = self._settings.memory_retrieval_limit
        if limit <= 0 or not query.strip():
            return []

        embedding = await self._embed(query)
        matches = await self._memories.search(
            owner_id,
            embedding,
            limit=limit,
            max_distance=self._settings.memory_max_distance,
        )

        if matches:
            await self._memories.record_recall([m.memory.id for m in matches], at=utc_now())

        return matches

    async def context_for(self, owner_id: uuid.UUID, query: str) -> str | None:
        """The retrieved memories, rendered for a system prompt."""
        return render_context(await self.retrieve(owner_id, query))

    # -- what the app shows -----------------------------------------------

    async def list_memories(
        self,
        owner_id: uuid.UUID,
        *,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRead]:
        rows = await self._memories.list_for_owner(
            owner_id, category=category, limit=limit, offset=offset
        )
        return [MemoryRead.model_validate(row) for row in rows]

    async def count(self, owner_id: uuid.UUID, *, category: str | None = None) -> int:
        return await self._memories.count_for_owner(owner_id, category=category)

    async def update(
        self, memory_id: uuid.UUID, owner_id: uuid.UUID, payload: MemoryUpdate
    ) -> MemoryRead:
        """Correct something NOVA believes.

        Editing the text re-embeds it. Skipping that would leave a memory
        that reads correctly on the screen but is still retrieved by whatever
        it used to say.
        """
        memory = await self._require_owned(memory_id, owner_id)

        if payload.content is not None and payload.content != memory.content:
            memory.content = payload.content
            memory.embedding = await self._embed(payload.content)
            memory.embedding_provider = self._embeddings.name
            # A person's own correction is the strongest evidence available.
            memory.confidence = 1.0
        if payload.category is not None:
            memory.category = payload.category
        if payload.importance is not None:
            memory.importance = payload.importance

        await self._memories.session.flush()
        logger.info("memory_updated", memory_id=str(memory_id))
        return MemoryRead.model_validate(memory)

    async def delete(self, memory_id: uuid.UUID, owner_id: uuid.UUID) -> None:
        memory = await self._require_owned(memory_id, owner_id)
        await self._memories.delete(memory)
        logger.info("memory_deleted", memory_id=str(memory_id))

    async def delete_all(self, owner_id: uuid.UUID) -> int:
        """Forget everything about someone, on request.

        Separate from deleting the account: a person may want NOVA to stop
        knowing things about them without giving up the device.
        """
        removed = await self._memories.delete_for_owner(owner_id)
        logger.info("memories_cleared", user_id=str(owner_id), count=removed)
        return removed

    # -- internals --------------------------------------------------------

    async def _require_owned(self, memory_id: uuid.UUID, owner_id: uuid.UUID) -> Memory:
        memory = await self._memories.get_for_owner(memory_id, owner_id)
        if memory is None:
            raise NotFoundError("Memory not found.", code="memory_not_found")
        return memory

    async def _embed(self, text: str) -> list[float]:
        vectors = await self._embeddings.embed([text])
        return vectors[0]


class MemoryRecorder:
    """Extracts and stores memories after a reply has been delivered.

    Owns its sessions for the same reason :class:`ChatStreamer` does: this
    runs as a background task once the response is finished, and the
    request's session is closed by then.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        extractor: MemoryExtractor,
        embeddings: EmbeddingProvider,
        settings: AISettings,
    ) -> None:
        self._session_factory = session_factory
        self._extractor = extractor
        self._embeddings = embeddings
        self._settings = settings

    async def record(
        self,
        *,
        owner_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        user_text: str,
        assistant_text: str,
    ) -> list[Memory]:
        """Store whatever this exchange revealed. Never raises.

        A failure here means NOVA learned nothing from one exchange. That is
        a worse companion, not a broken one, and it must not surface to
        somebody who has already had their answer.
        """
        if not self._settings.memory_extraction_enabled:
            return []

        try:
            return await self._record(
                owner_id=owner_id,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception:  # pragma: no cover - defensive; nothing above raises
            logger.exception("memory_recording_failed", user_id=str(owner_id))
            return []

    async def _record(
        self,
        *,
        owner_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        user_text: str,
        assistant_text: str,
    ) -> list[Memory]:
        candidates = await self._extractor.extract(
            user_text=user_text, assistant_text=assistant_text
        )
        if not candidates:
            return []

        vectors = await self._embeddings.embed([c.content for c in candidates])

        stored: list[Memory] = []
        async with self._session_factory() as session:
            repository = MemoryRepository(session)

            for candidate, embedding in zip(candidates, vectors, strict=True):
                existing = await repository.find_similar(
                    owner_id, embedding, threshold=DEDUPLICATION_DISTANCE
                )
                if existing is not None:
                    # Hearing something again is evidence for it, so the
                    # stronger of the two readings wins -- but the wording is
                    # left alone, because the owner may have edited it.
                    existing.confidence = max(existing.confidence, candidate.confidence)
                    existing.importance = max(existing.importance, candidate.importance)
                    continue

                memory = Memory(
                    user_id=owner_id,
                    content=candidate.content,
                    category=candidate.category,
                    importance=candidate.importance,
                    confidence=candidate.confidence,
                    embedding=embedding,
                    embedding_provider=self._embeddings.name,
                    source_conversation_id=conversation_id,
                )
                repository.add(memory)
                stored.append(memory)

            await session.commit()

        logger.info(
            "memories_extracted",
            user_id=str(owner_id),
            proposed=len(candidates),
            stored=len(stored),
        )
        return stored


def render_context(matches: list[ScoredMemory]) -> str | None:
    """Render retrieved memories for the volatile half of a system prompt.

    Categories are included because they change how a line should be read:
    "(routine) Runs at six" is a pattern, "(event) Ran a marathon in April"
    happened once. ``None`` when there is nothing, so no empty heading is
    sent.
    """
    if not matches:
        return None

    lines = "\n".join(f"- ({m.memory.category}) {m.memory.content}" for m in matches)
    return (
        "What you remember about this person, from earlier conversations:\n"
        f"{lines}\n\n"
        "Use these only when they are relevant. Do not list them back, do not "
        "announce that you remembered something, and do not treat them as "
        "more current than what is being said now."
    )


def parse_candidates(raw: str) -> list[MemoryCandidate]:
    """Parse the extractor's output, discarding anything malformed.

    Written to survive the model ignoring its instructions: prose around the
    array, a code fence, a single object instead of a list, missing or
    out-of-range scores, an invented category. Individual bad elements are
    dropped rather than failing the batch, because one unusable candidate
    should not cost the good ones alongside it.
    """
    payload = _extract_json_array(raw)
    if payload is None:
        return []

    candidates: list[MemoryCandidate] = []
    for element in payload:
        candidate = _as_candidate(element)
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= MAX_MEMORIES_PER_EXCHANGE:
            break

    return candidates


def _extract_json_array(raw: str) -> list[object] | None:
    """Find the JSON array in the model's reply, if there is one."""
    text = raw.strip()
    if not text:
        return None

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end <= start:
        return None

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.warning("memory_extraction_unparseable", length=len(raw))
        return None

    return parsed if isinstance(parsed, list) else None


def _as_candidate(element: object) -> MemoryCandidate | None:
    """Validate one element, or reject it."""
    if not isinstance(element, dict):
        return None

    content = element.get("content")
    if not isinstance(content, str):
        return None

    content = " ".join(content.split())
    if not content or len(content) > MAX_MEMORY_LENGTH:
        return None
    if looks_sensitive(content):
        logger.warning("memory_rejected_sensitive")
        return None

    category = element.get("category")
    if not isinstance(category, str) or category.lower() not in CATEGORIES:
        return None

    importance = _as_score(element.get("importance"))
    confidence = _as_score(element.get("confidence"))
    if importance is None or confidence is None:
        return None

    return MemoryCandidate(
        content=content,
        category=category.lower(),
        importance=importance,
        confidence=confidence,
    )


def _as_score(value: object) -> float | None:
    """Coerce a score to 0-1, or reject it.

    Out-of-range numbers are clamped rather than dropped -- a model returning
    ``0.95`` on a 0-10 scale is confused about the scale, not about the
    memory. A non-number is rejected: guessing a score is worse than not
    storing the row.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0.0, min(1.0, float(value)))


def looks_sensitive(content: str) -> bool:
    """Whether this looks like something that should never be stored.

    A blunt filter, and knowingly incomplete: it catches shapes, not meaning.
    The instruction in the extraction prompt is the primary defence; this is
    what stands when the model ignores it.
    """
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)
