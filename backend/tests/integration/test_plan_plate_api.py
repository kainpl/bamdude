"""Per-plate plan API: plate rows carry their own metadata slice and counts.

Covers Task 4 of the project-templates-and-plate-plan plan: the print-plan
GET response emits one row per plate (fed from that plate's own entry in
``file_metadata["plates"]``), printed counts are scoped per (project,
library_file, plate_index), and the per-file PATCH route is replaced by a
per-item-id one.
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _three_plate_setup(db_session):
    """Project + a 3-plate library file, plan rows planted via
    ``sync_plan_for_file`` (Task 3) exactly as production code plants them.

    Plates 1..3 carry filament_used_grams 10/20/30 (and distinct
    print_time_seconds/object_count) in their own ``file_metadata["plates"]``
    entries, matching the real per-plate dict shape written by
    ``services/archive.py``'s extractor.
    """
    from backend.app.models.library import LibraryFile
    from backend.app.models.project import Project
    from backend.app.services.print_plan import sync_plan_for_file

    project = Project(name="Plate Plan API Test", description="")
    db_session.add(project)
    await db_session.flush()

    file = LibraryFile(
        filename="three-plate.gcode.3mf",
        file_path="/tmp/three-plate.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_hash=None,
        file_metadata={
            "plates": [
                {"index": 1, "filament_used_grams": 10.0, "print_time_seconds": 600, "object_count": 1},
                {"index": 2, "filament_used_grams": 20.0, "print_time_seconds": 1200, "object_count": 2},
                {"index": 3, "filament_used_grams": 30.0, "print_time_seconds": 1800, "object_count": 3},
            ]
        },
    )
    db_session.add(file)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(file)

    await sync_plan_for_file(db_session, library_file_id=file.id, project_ids=[project.id], file_type="gcode")
    await db_session.commit()

    return project, file


async def _completed_archive(db_session, project_id: int, library_file_id: int, *, plate_index: int):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        project_id=project_id,
        library_file_id=library_file_id,
        plate_index=plate_index,
        filename="x.3mf",
        file_path="/tmp/x.3mf",
        file_size=1,
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def test_plan_returns_one_item_per_plate_with_plate_metadata(async_client: AsyncClient, db_session):
    project, file = await _three_plate_setup(db_session)

    resp = await async_client.get(f"/api/v1/projects/{project.id}/print-plan")

    items = [i for i in resp.json()["items"] if i["library_file_id"] == file.id]
    assert [i["plate_index"] for i in items] == [1, 2, 3]
    assert items[1]["filament_grams"] == 20.0
    assert resp.json()["totals_filament_grams"] == 60.0  # copies=1 each


async def test_printed_count_is_per_plate(async_client: AsyncClient, db_session):
    project, file = await _three_plate_setup(db_session)
    await _completed_archive(db_session, project.id, file.id, plate_index=2)
    await _completed_archive(db_session, project.id, file.id, plate_index=2)

    resp = await async_client.get(f"/api/v1/projects/{project.id}/print-plan")

    by_plate = {i["plate_index"]: i for i in resp.json()["items"] if i["library_file_id"] == file.id}
    assert by_plate[2]["printed_count"] == 2
    assert by_plate[1]["printed_count"] == 0


async def test_patch_by_item_id(async_client: AsyncClient, db_session):
    project, file = await _three_plate_setup(db_session)
    resp = await async_client.get(f"/api/v1/projects/{project.id}/print-plan")
    item = resp.json()["items"][0]

    resp = await async_client.patch(f"/api/v1/projects/{project.id}/print-plan/items/{item['id']}", json={"copies": 5})

    assert resp.status_code == 200
    assert resp.json()["copies"] == 5
    assert resp.json()["plate_index"] == item["plate_index"]


async def test_the_old_per_file_patch_route_is_gone(async_client: AsyncClient, db_session):
    project, file = await _three_plate_setup(db_session)

    resp = await async_client.patch(f"/api/v1/projects/{project.id}/print-plan/{file.id}", json={"copies": 5})

    assert resp.status_code in (404, 405)


async def test_a_stale_plate_row_degrades_gracefully(async_client: AsyncClient, db_session):
    """A plan row can outlive its own plate's metadata — e.g. a row was
    planted directly by a writer other than ``sync_plan_for_file`` (the
    setup-copy helper, m158's seed), or the file was re-sliced down to
    fewer plates without the row ever being reconciled. The plate-metadata
    lookup in ``_build_plan_item_response`` must degrade to None fields
    (not raise, not fall back to the whole-file numbers) and the plan's
    totals must not crash summing a None into the running total."""
    from backend.app.models.library import LibraryFile
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    project = Project(name="Stale Plate Test", description="")
    db_session.add(project)
    await db_session.flush()

    file = LibraryFile(
        filename="stale-plate.gcode.3mf",
        file_path="/tmp/stale-plate.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_hash=None,
        # Only plates 1 and 2 have metadata — no entry for index 3.
        file_metadata={
            "plates": [
                {"index": 1, "filament_used_grams": 10.0, "print_time_seconds": 600, "object_count": 1},
                {"index": 2, "filament_used_grams": 20.0, "print_time_seconds": 1200, "object_count": 2},
            ]
        },
    )
    db_session.add(file)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(file)

    # Plant a plate_index=3 row directly — bypassing sync_plan_for_file,
    # which would never plant a plate absent from the metadata.
    db_session.add(
        ProjectPrintPlanItem(project_id=project.id, library_file_id=file.id, copies=1, order_index=0, plate_index=3)
    )
    await db_session.commit()

    resp = await async_client.get(f"/api/v1/projects/{project.id}/print-plan")

    assert resp.status_code == 200
    body = resp.json()
    item = next(i for i in body["items"] if i["library_file_id"] == file.id and i["plate_index"] == 3)
    assert item["filament_grams"] is None
    assert item["print_time_seconds"] is None
    assert item["object_count"] is None
    # Totals must not crash on the None contribution.
    assert body["totals_filament_grams"] == 0.0
    assert body["totals_print_time_seconds"] == 0
    assert body["totals_objects"] == 0


async def test_patch_is_scoped_to_the_owning_project(async_client: AsyncClient, db_session):
    """A plan item id is only valid under the project that owns it — a
    request that gets the id right but the project wrong must 404, not
    silently patch (or leak) another project's row."""
    from backend.app.models.project import Project

    project_a, file_a = await _three_plate_setup(db_session)

    project_b = Project(name="Other Project", description="")
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_b)

    plan_a = (await async_client.get(f"/api/v1/projects/{project_a.id}/print-plan")).json()
    item = plan_a["items"][0]

    resp = await async_client.patch(
        f"/api/v1/projects/{project_b.id}/print-plan/items/{item['id']}", json={"copies": 2}
    )

    assert resp.status_code == 404

    plan_a_after = (await async_client.get(f"/api/v1/projects/{project_a.id}/print-plan")).json()
    item_after = next(i for i in plan_a_after["items"] if i["id"] == item["id"])
    assert item_after["copies"] == item["copies"]
