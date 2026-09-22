"""One side of the two-process PostgreSQL archive-file lock barrier."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def _run(role: str, marker_dir: Path, file_path: str) -> None:
    from backend.app.core.database import async_session
    from backend.app.services.archive_write_scope import archive_file_reference_scope

    # The parent scenario initialized the scratch schema before starting either
    # worker. Re-running migrations in both independent processes would test a
    # different race and obscure the file-reference barrier.
    if role == "probe":
        (marker_dir / "probe-attempted").write_text("attempted", encoding="utf-8")
    async with async_session() as db, archive_file_reference_scope(db, file_path):
        if role == "hold":
            (marker_dir / "holder-ready").write_text("ready", encoding="utf-8")
            while not (marker_dir / "release-holder").exists():
                await asyncio.sleep(0.02)
        elif role == "probe":
            (marker_dir / "probe-entered").write_text("entered", encoding="utf-8")
        else:
            raise ValueError(f"unknown file-scope worker role: {role}")


def main() -> None:
    role, marker_dir, file_path = sys.argv[1:]
    asyncio.run(_run(role, Path(marker_dir), file_path))


if __name__ == "__main__":
    main()
