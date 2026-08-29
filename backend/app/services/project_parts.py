"""Project parts ledger seeding — target rows planted when files join a project.

Runs inside ``sync_plan_for_file`` (the choke point every link path funnels
through: direct link, folder inherit, folder re-link). Reads plate object
names from ``LibraryFile.file_metadata`` — no file I/O. Unlinking never
deletes ledger rows: targets belong to the project, not the file.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFile
from backend.app.models.project_part import ProjectPart
from backend.app.services.part_names import canonicalize, name_key

logger = logging.getLogger(__name__)


async def seed_project_parts_for_file(db: AsyncSession, library_file_id: int, project_ids: list[int]) -> None:
    if not project_ids:
        return
    file = (await db.execute(select(LibraryFile).where(LibraryFile.id == library_file_id))).scalar_one_or_none()
    if file is None:
        return
    plates = ((file.file_metadata or {}).get("plates")) or []
    names: dict[str, str] = {}
    for plate in plates:
        plate_objs = [o for o in (plate.get("objects") or []) if o]
        for raw in plate_objs:
            canon = canonicalize(raw, plate_objs)
            names.setdefault(name_key(canon), canon)
    if not names:
        return
    for project_id in project_ids:
        existing = {
            key
            for (key,) in (
                await db.execute(select(ProjectPart.name_key).where(ProjectPart.project_id == project_id))
            ).all()
        }
        for key, canon in names.items():
            if key not in existing:
                db.add(ProjectPart(project_id=project_id, name=canon, name_key=key, target_qty=0))
