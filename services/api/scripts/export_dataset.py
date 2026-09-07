#!/usr/bin/env python3
"""Export a device's history for the ML pipeline.

    python scripts/export_dataset.py --device-id <uuid> [--out ../../ml/data]

Writes ``<device>.csv`` and ``<device>.manifest.json``. The CSV is what
``python -m nova_ml.train`` consumes; the manifest is for the person reading
it. Exit 1 if the device is unknown or unclaimed, 2 if there is nothing to
export -- the ML pipeline would refuse an empty file at its gates anyway,
and it is better to hear that here.

The logic lives in ``nova.services.dataset_export`` so the test suite and
the type checker cover it. This file is the command line and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from nova.core.config import get_settings
from nova.db.session import create_engine, create_session_factory
from nova.services.dataset_export import (
    DeviceNotExportableError,
    EmptyDatasetError,
    export_dataset,
)


async def _run(device_id: uuid.UUID, out: Path) -> int:
    settings = get_settings()
    engine = create_engine(settings.database)
    try:
        async with create_session_factory(engine)() as session:
            try:
                export = await export_dataset(session, device_id)
            except DeviceNotExportableError as error:
                print(error, file=sys.stderr)
                return 1
            except EmptyDatasetError as error:
                print(error, file=sys.stderr)
                return 2
    finally:
        await engine.dispose()

    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / f"{device_id}.csv"
    manifest_path = out / f"{device_id}.manifest.json"
    csv_path.write_text(export.csv_text, encoding="utf-8")
    manifest_path.write_text(export.manifest.to_json(), encoding="utf-8")

    m = export.manifest
    print(f"wrote {csv_path}")
    print(f"      {m.rows} rows ({m.telemetry_rows} telemetry, {m.message_rows} messages)")
    print(f"      {m.first_at} -> {m.last_at}, timezone {m.timezone}")
    print(f"      sha256 {m.sha256}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--device-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "ml" / "data",
        help="directory for the CSV and manifest (default: ml/data)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.device_id, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
