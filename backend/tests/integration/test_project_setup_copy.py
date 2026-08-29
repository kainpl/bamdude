"""One copy routine for templates and duplication: the full setup travels.

Measured gap: both template flows copied descriptive fields + BOM only —
file links, folder links, the print plan, parts targets, attachments all
silently dropped (routes/projects.py:502-583, 1500-1575), while
_duplicate_project_tree carried everything except the parts targets.
"""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.library_project_links import library_file_projects, library_folder_projects
from backend.app.models.project import Project
from backend.app.models.project_bom import ProjectBOMItem
from backend.app.models.project_part import ProjectPart
from backend.app.models.project_print_plan import ProjectPrintPlanItem

pytestmark = pytest.mark.integration


async def _project_with_setup(db, *, is_template=False) -> Project:
    project = Project(name="Voron", is_template=is_template)
    db.add(project)
    await db.flush()
    file = LibraryFile(filename="frame.gcode.3mf", file_path="lib/frame.gcode.3mf", file_size=1, file_type="gcode")
    db.add(file)
    await db.flush()
    await db.execute(library_file_projects.insert(), [{"file_id": file.id, "project_id": project.id}])
    db.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=file.id, copies=3, order_index=0))
    db.add(ProjectPart(project_id=project.id, name="Bracket.stl", name_key="bracket.stl", target_qty=12))
    db.add(ProjectBOMItem(project_id=project.id, name="M3x8", quantity_needed=40, quantity_acquired=15))
    await db.commit()
    await db.refresh(project)
    return project


async def _setup_of(db, project_id):
    files = (
        (
            await db.execute(
                select(library_file_projects.c.file_id).where(library_file_projects.c.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    plan = (
        (await db.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.project_id == project_id)))
        .scalars()
        .all()
    )
    parts = (await db.execute(select(ProjectPart).where(ProjectPart.project_id == project_id))).scalars().all()
    bom = (await db.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == project_id))).scalars().all()
    return files, plan, parts, bom


@pytest.mark.asyncio
async def test_create_template_carries_the_full_setup(async_client, db_session):
    source = await _project_with_setup(db_session)

    resp = await async_client.post(f"/api/v1/projects/{source.id}/create-template")

    assert resp.status_code == 200, resp.text
    template_id = resp.json()["id"]
    files, plan, parts, bom = await _setup_of(db_session, template_id)
    assert len(files) == 1
    assert len(plan) == 1 and plan[0].copies == 3
    assert len(parts) == 1 and parts[0].target_qty == 12 and parts[0].name_key == "bracket.stl"
    assert len(bom) == 1 and bom[0].quantity_acquired == 0, "procurement progress never copies"


@pytest.mark.asyncio
async def test_from_template_carries_the_full_setup(async_client, db_session):
    template = await _project_with_setup(db_session, is_template=True)

    resp = await async_client.post(f"/api/v1/projects/from-template/{template.id}")

    assert resp.status_code == 200, resp.text
    files, plan, parts, bom = await _setup_of(db_session, resp.json()["id"])
    assert (len(files), len(plan), len(parts), len(bom)) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_duplicate_now_carries_parts_targets_too(async_client, db_session):
    source = await _project_with_setup(db_session)

    resp = await async_client.post(f"/api/v1/projects/{source.id}/duplicate", json={})

    assert resp.status_code == 200, resp.text
    _, _, parts, _ = await _setup_of(db_session, resp.json()["id"])
    assert len(parts) == 1 and parts[0].target_qty == 12


@pytest.mark.asyncio
async def test_the_source_keeps_everything(async_client, db_session):
    """Copy, never move."""
    source = await _project_with_setup(db_session)

    resp = await async_client.post(f"/api/v1/projects/{source.id}/create-template")
    assert resp.status_code == 200, resp.text

    files, plan, parts, bom = await _setup_of(db_session, source.id)
    assert (len(files), len(plan), len(parts), len(bom)) == (1, 1, 1, 1)
