"""Durable, indexed per-model firmware archive.

Wraps the firmware-check download path so both the single-printer flow and the
bulk flow share one cache. Files live under ``<data_dir>/firmware/``; a
``FirmwareCacheEntry`` row indexes each by ``(model, version)`` — so a cached
version is reusable even after the vendor removes its ``download_url`` from the
site (we no longer need the URL to locate the file).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from backend.app.core.config import _data_dir
from backend.app.core.database import async_session
from backend.app.models.firmware import FirmwareCacheEntry
from backend.app.services.firmware_check import get_firmware_service

logger = logging.getLogger(__name__)


def _firmware_dir() -> Path:
    d = _data_dir / "firmware"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class StoredFirmware:
    model: str
    version: str
    filename: str
    path: Path
    sha256: str
    size_bytes: int
    source_url: str | None
    release_notes: str | None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


async def _find_cached(model: str, version: str) -> StoredFirmware | None:
    """Return the indexed StoredFirmware for (model, version), or None.

    A row whose backing file has gone missing is treated as a miss.
    """
    async with async_session() as db:
        row = (
            await db.execute(
                select(FirmwareCacheEntry).where(
                    FirmwareCacheEntry.model == model, FirmwareCacheEntry.version == version
                )
            )
        ).scalar_one_or_none()
    if not row:
        return None
    path = _firmware_dir() / row.filename
    if not path.exists():
        logger.warning("Firmware index row %s/%s points at a missing file %s", model, version, path)
        return None
    return StoredFirmware(
        model=row.model,
        version=row.version,
        filename=row.filename,
        path=path,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        source_url=row.source_url,
        release_notes=row.release_notes,
    )


async def _record(sf: StoredFirmware) -> None:
    async with async_session() as db:
        existing = (
            await db.execute(
                select(FirmwareCacheEntry).where(
                    FirmwareCacheEntry.model == sf.model, FirmwareCacheEntry.version == sf.version
                )
            )
        ).scalar_one_or_none()
        if existing:
            return
        db.add(
            FirmwareCacheEntry(
                model=sf.model,
                version=sf.version,
                filename=sf.filename,
                sha256=sf.sha256,
                size_bytes=sf.size_bytes,
                source_url=sf.source_url,
                release_notes=sf.release_notes,
            )
        )
        await db.commit()


async def get_or_download(model: str, version: str, *, progress_callback=None) -> StoredFirmware | None:
    """Return the firmware for (model, version), downloading + indexing on a miss.

    Reuse is keyed by ``(model, version)``, NOT the URL, so a cached version
    survives the vendor dropping it. sha256 is verified on a cache hit; a corrupt
    file is re-downloaded.
    """
    cached = await _find_cached(model, version)
    if cached:
        if _sha256_of(cached.path) == cached.sha256:
            return cached
        logger.warning("Cached firmware %s/%s failed sha256; re-downloading", model, version)

    svc = get_firmware_service()
    info = await svc.get_version_info(model, version)
    if not info or not info.download_url:
        logger.warning("No download URL for %s/%s and not cached", model, version)
        return None

    path = await svc.download_firmware(model, version=version, progress_callback=progress_callback)
    if not path or not path.exists():
        return None
    sf = StoredFirmware(
        model=model,
        version=version,
        filename=path.name,
        path=path,
        sha256=_sha256_of(path),
        size_bytes=path.stat().st_size,
        source_url=info.download_url,
        release_notes=info.release_notes,
    )
    await _record(sf)
    return sf


async def list_cached(model: str) -> list[StoredFirmware]:
    """All indexed firmware versions for a model whose file is still on disk."""
    async with async_session() as db:
        rows = (await db.execute(select(FirmwareCacheEntry).where(FirmwareCacheEntry.model == model))).scalars().all()
    out: list[StoredFirmware] = []
    for r in rows:
        p = _firmware_dir() / r.filename
        if p.exists():
            out.append(
                StoredFirmware(
                    model=r.model,
                    version=r.version,
                    filename=r.filename,
                    path=p,
                    sha256=r.sha256,
                    size_bytes=r.size_bytes,
                    source_url=r.source_url,
                    release_notes=r.release_notes,
                )
            )
    return out
