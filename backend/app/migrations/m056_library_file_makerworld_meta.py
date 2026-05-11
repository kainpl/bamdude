"""Detailed MakerWorld metadata table for library files (1:1 with cascade).

CREATE TABLE + best-effort backfill that re-fetches design/instance
metadata from MakerWorld for every previously-imported library file
(``source_type = 'makerworld'``) so the new sidebar/detail UI works on
upgrade — not just on imports going forward.

The backfill is **strictly best-effort**:

- A row that we can't enrich (parse failure, MakerWorld outage,
  upstream model removed) is skipped silently. The next time the
  operator visits that file we can show a "metadata unavailable" UI.
- Total network outage doesn't break the migration — each row's
  failures are swallowed; the migration completes either way.
- Re-running the migration (DEBUG=true) re-tries failed rows because
  the loop only writes rows that don't have a meta entry yet.

Cover images are downloaded via the shared
``services/makerworld_meta.download_covers`` helper that the live import
flow also uses, so both paths produce identical on-disk layout under
``<archive_dir>/library/makerworld-covers/<library_file_id>-{cover,variant}.<ext>``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from sqlalchemy import select, text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 56
name = "library_file_makerworld_meta"

# https://makerworld.com/.../models/{N}#profileId-{P}  — both ids may be
# absent in the legacy whole-model imports (no #profileId fragment).
_URL_RE = re.compile(r"/models/(\d+)(?:#profileId-(\d+))?")


async def upgrade(conn):
    if await table_exists(conn, "library_file_makerworld_meta"):
        return

    if is_postgres():
        await conn.execute(
            text(
                """
                CREATE TABLE library_file_makerworld_meta (
                    id SERIAL PRIMARY KEY,
                    library_file_id INTEGER NOT NULL UNIQUE REFERENCES library_files(id) ON DELETE CASCADE,
                    title VARCHAR(500),
                    description TEXT,
                    author_name VARCHAR(255),
                    author_profile_url VARCHAR(500),
                    license VARCHAR(64),
                    original_design_id INTEGER,
                    variant_title VARCHAR(500),
                    variant_description TEXT,
                    variant_url VARCHAR(500),
                    profile_id INTEGER,
                    cover_path VARCHAR(500),
                    variant_cover_path VARCHAR(500),
                    sliced_for VARCHAR(64),
                    compatible_models JSONB,
                    needs_ams BOOLEAN,
                    material_count INTEGER,
                    materials JSONB,
                    model_id_alphanumeric VARCHAR(64),
                    raw_payload JSONB,
                    imported_at TIMESTAMP NOT NULL DEFAULT now()
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX ix_library_file_makerworld_meta_profile_id ON library_file_makerworld_meta(profile_id)")
        )
    else:
        await conn.execute(
            text(
                """
                CREATE TABLE library_file_makerworld_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_file_id INTEGER NOT NULL UNIQUE REFERENCES library_files(id) ON DELETE CASCADE,
                    title VARCHAR(500),
                    description TEXT,
                    author_name VARCHAR(255),
                    author_profile_url VARCHAR(500),
                    license VARCHAR(64),
                    original_design_id INTEGER,
                    variant_title VARCHAR(500),
                    variant_description TEXT,
                    variant_url VARCHAR(500),
                    profile_id INTEGER,
                    cover_path VARCHAR(500),
                    variant_cover_path VARCHAR(500),
                    sliced_for VARCHAR(64),
                    compatible_models JSON,
                    needs_ams BOOLEAN,
                    material_count INTEGER,
                    materials JSON,
                    model_id_alphanumeric VARCHAR(64),
                    raw_payload JSON,
                    imported_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX ix_library_file_makerworld_meta_profile_id ON library_file_makerworld_meta(profile_id)")
        )


async def seed(session_factory):
    """Best-effort enrichment of existing MakerWorld imports.

    Pulls every ``library_files`` row with ``source_type='makerworld'``
    and no meta row, parses model/profile ids out of its ``source_url``,
    re-fetches design + instances from MakerWorld, downloads covers, and
    INSERTs a meta row. Per-row failures are logged + swallowed.
    """
    # Late imports keep migration discovery dependency-light.
    from backend.app.models.library import LibraryFile
    from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta
    from backend.app.services.makerworld import MakerWorldError, MakerWorldService
    from backend.app.services.makerworld_meta import build_meta_dict, download_covers

    async with session_factory() as session:
        result = await session.execute(
            select(LibraryFile.id, LibraryFile.source_url).where(
                LibraryFile.source_type == "makerworld",
                LibraryFile.deleted_at.is_(None),
            )
        )
        candidates = result.all()
        if not candidates:
            logger.info("m056: no MakerWorld imports to backfill")
            return

        # Skip rows that already have meta (idempotent re-run safety).
        already_q = await session.execute(select(LibraryFileMakerworldMeta.library_file_id))
        already_ids = {row[0] for row in already_q.all()}

        pending = [(fid, url) for fid, url in candidates if fid not in already_ids and url]
        if not pending:
            logger.info("m056: %d MakerWorld imports already enriched; nothing to do", len(candidates))
            return

        logger.info("m056: backfilling MakerWorld metadata for %d library files", len(pending))

        service = MakerWorldService()  # anonymous — get_design / get_design_instances are public
        try:
            enriched = 0
            for library_file_id, source_url in pending:
                match = _URL_RE.search(source_url or "")
                if not match:
                    logger.debug("m056: %s has unparseable source_url=%r — skipping", library_file_id, source_url)
                    continue
                model_id = int(match.group(1))
                profile_id = int(match.group(2)) if match.group(2) else None

                try:
                    design = await service.get_design(model_id)
                    envelope = await service.get_design_instances(model_id)
                except MakerWorldError as exc:
                    logger.info("m056: MakerWorld unreachable for model_id=%s: %s — skipping", model_id, exc)
                    continue
                except Exception as exc:
                    logger.warning("m056: unexpected error fetching model_id=%s: %s — skipping", model_id, exc)
                    continue

                instances = envelope.get("hits") or []
                if not isinstance(instances, list):
                    instances = []

                # Resolve covers — model-level always present, variant-level
                # only when we can locate the specific instance.
                cover_url = design.get("coverUrl") if isinstance(design.get("coverUrl"), str) else None
                variant_cover_url: str | None = None
                if profile_id is not None:
                    for inst in instances:
                        if isinstance(inst, dict) and inst.get("profileId") == profile_id:
                            cov = inst.get("cover")
                            if isinstance(cov, str):
                                variant_cover_url = cov
                            break

                meta_dict = build_meta_dict(
                    library_file_id=library_file_id,
                    design=design,
                    instances=instances,
                    profile_id=profile_id,
                    variant_url=source_url,
                    model_id_alphanumeric=(design.get("modelId") if isinstance(design.get("modelId"), str) else None),
                )

                try:
                    cover_rel, variant_cover_rel = await download_covers(
                        service,
                        library_file_id=library_file_id,
                        cover_url=cover_url,
                        variant_cover_url=variant_cover_url,
                    )
                except Exception as exc:
                    logger.warning("m056: cover download crashed for lf=%s: %s", library_file_id, exc)
                    cover_rel, variant_cover_rel = None, None

                meta = LibraryFileMakerworldMeta(
                    **meta_dict,
                    cover_path=cover_rel,
                    variant_cover_path=variant_cover_rel,
                )
                session.add(meta)
                try:
                    await session.commit()
                    enriched += 1
                except Exception as exc:
                    logger.warning("m056: failed to commit meta for lf=%s: %s", library_file_id, exc)
                    await session.rollback()

                # Polite delay between MakerWorld API calls — backfill is
                # not time-critical, and we want to be a good citizen.
                await asyncio.sleep(0.25)

            logger.info("m056: backfilled %d/%d rows", enriched, len(pending))
        finally:
            await service.close()
