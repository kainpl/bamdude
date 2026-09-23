"""Main-only preview publication. The service never sees a DB or final path."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.library import LibraryFile
from backend.app.services.preview_artifacts import disk, owned
from backend.app.services.preview_runtime import preview_attempt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Source:
    id: int
    filename: str
    file_path: str
    file_hash: str | None
    thumbnail_path: str | None
    fs_modified_at: datetime | None

    @classmethod
    def capture(cls, row: LibraryFile):
        return cls(**{name: getattr(row, name) for name in cls.__dataclass_fields__})


def _publish(source: Path, target: Path):
    temporary = target.with_suffix(".part")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 128 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


async def attach(db: AsyncSession, source: Source, png: Path) -> bool:
    """Own a fresh short transaction; never commit the caller's pending work."""
    from backend.app.api.routes.library import get_library_thumbnails_dir, to_relative_path

    final = get_library_thumbnails_dir() / f"{uuid.uuid4().hex}.png"
    relative = to_relative_path(final)
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    await disk(_publish, png, final)

    async def finish():
        committed = False
        try:
            async with factory() as session:
                result = await session.execute(
                    update(LibraryFile)
                    .where(
                        LibraryFile.id == source.id,
                        LibraryFile.deleted_at.is_(None),
                        LibraryFile.file_path == source.file_path,
                        LibraryFile.file_hash == source.file_hash,
                        LibraryFile.fs_modified_at == source.fs_modified_at,
                        LibraryFile.thumbnail_path == source.thumbnail_path,
                    )
                    .values(thumbnail_path=relative)
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
                committed = result.rowcount == 1
        except Exception:
            logger.warning("Preview attachment uncertain for library file %s", source.id)
        # Includes trash: a concurrently trashed row is still a durable owner.
        try:
            async with factory() as session:
                referenced = await session.scalar(
                    select(LibraryFile.id).where(LibraryFile.thumbnail_path == relative).limit(1)
                )
            if referenced is None:
                await disk(final.unlink, True)
        except Exception:
            logger.warning("Retaining preview with unknown durable reference for library file %s", source.id)
        return committed

    return await owned(finish())


async def generate(db: AsyncSession, source: Source) -> bool:
    from backend.app.api.routes.library import to_absolute_path

    path = to_absolute_path(source.file_path)
    if path is None:
        return False
    async with preview_attempt({"mesh": (path, Path(source.filename).suffix.lower().lstrip("."))}) as result:
        png = result.files.get("preview")
        if not png or result.outcome != "ok":
            return False
        # Snapshot digest binds this result to the DB source, not merely its name.
        if source.file_hash and result.input_digests.get("mesh") != source.file_hash:
            return False
        return await attach(db, source, png)


async def sliced_preview(
    db: AsyncSession, content: bytes, *, model_bytes: bytes | None = None, source: Source | None = None
) -> bytes:
    from backend.app.api.routes.library import to_absolute_path

    inputs = {"sliced": (content, "3mf")}
    if source and model_bytes is not None and source.filename.lower().endswith(".stl"):
        # The immutable bytes are exactly what went to the slicer. Never render
        # a newly replaced library path for an already finished old slice.
        digest = await disk(lambda: hashlib.sha256(model_bytes).hexdigest())
        if source.file_hash == digest:
            inputs["mesh"] = (model_bytes, "stl")
            if source.thumbnail_path:
                png = to_absolute_path(source.thumbnail_path)
                if png and await disk(png.is_file):
                    inputs["source_png"] = (png, "png")
        else:
            source = None
    async with preview_attempt(inputs) as result:
        if source and "preview" in result.files:
            try:
                await attach(db, source, result.files["preview"])
            except Exception:
                logger.warning("Source preview attachment skipped for library file %s", source.id)
        output = result.files.get("output") or result.files.get("checkpoint")
        try:
            return await disk(output.read_bytes) if output else content
        except OSError:
            return content
