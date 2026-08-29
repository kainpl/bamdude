"""The project parts ledger: seeding on link, aggregation, target CRUD."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.library import LibraryFile
from backend.app.models.project import Project
from backend.app.models.project_part import ProjectPart
from backend.app.services.project_parts import seed_project_parts_for_file

pytestmark = pytest.mark.integration


async def _project(db) -> Project:
    project = Project(name="P")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _library_file(db, plates_objects: list[list[str]]) -> LibraryFile:
    file = LibraryFile(
        filename="f.gcode.3mf",
        file_path="lib/f.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={"plates": [{"index": i + 1, "objects": objs} for i, objs in enumerate(plates_objects)]},
    )
    db.add(file)
    await db.commit()
    await db.refresh(file)
    return file


async def _project_archive(db, project_id, parts: dict[str, int], status="completed"):
    archive = PrintArchive(
        printer_id=1,
        filename="a.3mf",
        print_name="A",
        file_path="x/a.3mf",
        file_size=1,
        status=status,
        project_id=project_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.flush()
    for name, qty in parts.items():
        db.add(
            PrintArchivePart(
                archive_id=archive.id,
                name=name,
                name_key=name.lower(),
                identify_ids=list(range(qty)),
                quantity=qty,
            )
        )
    await db.commit()
    return archive


@pytest.mark.asyncio
async def test_linking_a_file_seeds_zero_targets(db_session):
    project = await _project(db_session)
    file = await _library_file(db_session, [["part.stl_1", "part.stl_2", "lid"]])

    await seed_project_parts_for_file(db_session, file.id, [project.id])
    await db_session.commit()

    rows = {
        r.name_key: r
        for r in (await db_session.execute(select(ProjectPart).where(ProjectPart.project_id == project.id)))
        .scalars()
        .all()
    }
    assert set(rows) == {"part.stl", "lid"}
    assert all(r.target_qty == 0 for r in rows.values())


@pytest.mark.asyncio
async def test_seeding_twice_does_not_duplicate(db_session):
    project = await _project(db_session)
    file = await _library_file(db_session, [["lid"]])

    await seed_project_parts_for_file(db_session, file.id, [project.id])
    await seed_project_parts_for_file(db_session, file.id, [project.id])
    await db_session.commit()

    count = len(
        (await db_session.execute(select(ProjectPart).where(ProjectPart.project_id == project.id))).scalars().all()
    )
    assert count == 1


@pytest.mark.asyncio
async def test_ledger_aggregation(async_client, db_session):
    project = await _project(db_session)
    db_session.add(ProjectPart(project_id=project.id, name="lid", name_key="lid", target_qty=10))
    await db_session.commit()
    a = await _project_archive(db_session, project.id, {"lid": 4})
    rows = (
        (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.archive_id == a.id))).scalars().all()
    )
    rows[0].defective = 1
    await db_session.commit()
    await _project_archive(db_session, project.id, {"lid": 4}, status="printing")
    await _project_archive(db_session, project.id, {"stray": 2})  # history without a target

    resp = await async_client.get(f"/api/v1/projects/{project.id}/parts")

    assert resp.status_code == 200
    parts = {p["name_key"]: p for p in resp.json()["parts"]}
    lid = parts["lid"]
    assert (lid["printed"], lid["defective"], lid["usable"]) == (4, 1, 3)
    assert lid["in_progress"] == 4
    assert lid["remaining"] == 7
    assert parts["stray"]["target_qty"] is None
    assert parts["stray"]["printed"] == 2
    assert parts["stray"]["remaining"] is None


@pytest.mark.asyncio
async def test_target_upsert_and_delete(async_client, db_session):
    project = await _project(db_session)

    resp = await async_client.patch(
        f"/api/v1/projects/{project.id}/parts",
        json={"parts": [{"name_key": "lid", "name": "Lid", "target_qty": 5}]},
    )
    assert resp.status_code == 200

    resp = await async_client.get(f"/api/v1/projects/{project.id}/parts")
    assert resp.json()["parts"][0]["target_qty"] == 5

    resp = await async_client.delete(f"/api/v1/projects/{project.id}/parts", params={"name_key": "lid"})
    assert resp.status_code == 200
    rows = (await db_session.execute(select(ProjectPart).where(ProjectPart.project_id == project.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_target_upsert_rejects_an_oversized_name_key(async_client, db_session):
    """PG's name_key column is VARCHAR(512) — an unbounded string 500s instead
    of 422ing before it ever reaches the DB."""
    project = await _project(db_session)

    resp = await async_client.patch(
        f"/api/v1/projects/{project.id}/parts",
        json={"parts": [{"name_key": "x" * 600, "name": "Lid", "target_qty": 5}]},
    )
    assert resp.status_code == 422
