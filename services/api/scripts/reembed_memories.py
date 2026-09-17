"""Re-embed every memory with the configured embedder.

Run after changing NOVA_AI__EMBEDDING_PROVIDER or NOVA_AI__EMBEDDING_MODEL,
with the same environment the API runs with:

    cd services/api
    python scripts/reembed_memories.py --dry-run   # what would change
    python scripts/reembed_memories.py             # do it

Until it has run, memories written by the previous embedder are invisible to
retrieval -- the dashboard's memory card counts them as stale. The pass is
one transaction: if the model server fails halfway, nothing has changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from nova.ai.errors import AIProviderError
from nova.ai.registry import build_embedding_provider
from nova.core.config import get_settings
from nova.db.session import create_engine, create_session_factory
from nova.services.reembed import DEFAULT_BATCH_SIZE, reembed_all


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dry-run", action="store_true", help="count and embed, but commit nothing"
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    settings = get_settings()
    embeddings = build_embedding_provider(settings.ai)
    engine = create_engine(settings.database)
    started = time.perf_counter()

    def progress(done: int, total: int) -> None:
        print(f"\r  re-embedded {done}/{total}", end="", flush=True)

    try:
        print(f"embedder: {embeddings.name} ({embeddings.dimensions} wide)")
        report = await reembed_all(
            create_session_factory(engine),
            embeddings,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            progress=progress,
        )
    except AIProviderError as error:
        print(f"\nthe embedder is not reachable, nothing was changed: {error}")
        return 2
    finally:
        await embeddings.aclose()
        await engine.dispose()

    if report.stale_before:
        print()
    print("before:", ", ".join(f"{name}: {count}" for name, count in sorted(report.before.items())))
    verb = "would re-embed" if report.dry_run else "re-embedded"
    print(f"{verb} {report.reembedded} in {time.perf_counter() - started:.1f}s")
    if report.dry_run and report.reembedded:
        print("dry run: rolled back")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
