"""Copying a project copies its SETUP and not its HISTORY — and copies, never moves.

Users asked to start a new run of an existing project without carrying its
past. The split the endpoint promises:

* copied — every descriptive column, the BOM part list, the linked library
  files and folders, the print plan (per-file copies + order), the uploaded
  attachments on disk;
* not copied — archives and queue items, and BOM ``quantity_acquired``, which
  is procurement progress rather than a part list;
* forced — ``status="active"``, whatever the source was.

⚠️ **"Copy, not move" is the assertion most worth keeping.** The library links
are many-to-many, so attaching a file to the new project by reassignment
instead of insertion would silently strip it from the source — and the source
would look fine until someone opened it.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.project import Project
from backend.app.models.project_bom import ProjectBOMItem
from backend.app.models.project_print_plan import ProjectPrintPlanItem

pytestmark = pytest.mark.asyncio


async def _seed(db_session, *, status: str = "completed", with_child: bool = False) -> dict:
    """A source project wearing one of everything the copy has to handle."""
    project = Project(
        name="Voron Build",
        description="a description",
        color="#ff0000",
        status=status,
        target_count=4,
        target_parts_count=40,
        notes="<p>rich notes</p>",
        tags="voron,build",
        priority="high",
        budget=250.0,
        url="https://example.invalid/voron",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        ProjectBOMItem(
            project_id=project.id,
            name="M3x8 screw",
            quantity_needed=40,
            quantity_acquired=17,  # progress — must NOT come across
            unit_price=0.1,
            sort_order=1,
        )
    )

    # ⚠️ The M2M link is set at construction, never assigned to a row that is
    # already persisted: `folder.projects = [...]` on a loaded row has to read
    # the current collection to diff it, and that lazy load is un-awaited IO
    # (MissingGreenlet). A fresh object's collection is empty by definition.
    folder = LibraryFolder(name="Voron parts", projects=[project])
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)

    lib = LibraryFile(
        folder_id=folder.id,
        filename="frame.3mf",
        file_path="/tmp/frame.3mf",
        file_type="3mf",
        file_size=123,
        projects=[project],
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)

    db_session.add(ProjectPrintPlanItem(project_id=project.id, library_file_id=lib.id, copies=3, order_index=7))

    # History: an archive on the source. The copy must not inherit it.
    db_session.add(
        PrintArchive(
            project_id=project.id,
            filename="frame.gcode.3mf",
            file_path="archive/x/frame.gcode.3mf",
            file_size=1,
            print_name="frame",
            status="completed",
        )
    )

    child_id = None
    if with_child:
        child = Project(name="Frame", description="sub", parent_id=project.id, status="active")
        db_session.add(child)
        await db_session.commit()
        await db_session.refresh(child)
        child_id = child.id
        db_session.add(ProjectBOMItem(project_id=child.id, name="rail", quantity_needed=2, quantity_acquired=2))

    await db_session.commit()
    return {"project_id": project.id, "file_id": lib.id, "folder_id": folder.id, "child_id": child_id}


async def _duplicate(client: AsyncClient, project_id: int, **body) -> dict:
    resp = await client.post(f"/api/v1/projects/{project_id}/duplicate", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestWhatComesAcross:
    async def test_settings_are_copied_and_status_is_forced_active(self, async_client, db_session):
        seeded = await _seed(db_session, status="completed")

        copy = await _duplicate(async_client, seeded["project_id"])

        assert copy["id"] != seeded["project_id"]
        assert copy["name"] == "Voron Build (Copy)"
        assert copy["status"] == "active", "a duplicate is new work about to start"
        for field, expected in (
            ("description", "a description"),
            ("color", "#ff0000"),
            ("target_count", 4),
            ("target_parts_count", 40),
            ("notes", "<p>rich notes</p>"),
            ("tags", "voron,build"),
            ("priority", "high"),
            ("budget", 250.0),
            ("url", "https://example.invalid/voron"),
        ):
            assert copy[field] == expected, field

    async def test_the_part_list_comes_but_the_purchasing_progress_does_not(self, async_client, db_session):
        seeded = await _seed(db_session)

        copy = await _duplicate(async_client, seeded["project_id"])

        db_session.expire_all()
        items = (
            (await db_session.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == copy["id"])))
            .scalars()
            .all()
        )
        assert [i.name for i in items] == ["M3x8 screw"]
        assert items[0].quantity_needed == 40
        assert items[0].quantity_acquired == 0, "17 already bought belongs to the source, not to a fresh run"

    async def test_the_print_plan_keeps_copies_and_order(self, async_client, db_session):
        seeded = await _seed(db_session)

        copy = await _duplicate(async_client, seeded["project_id"])

        db_session.expire_all()
        plan = (
            (
                await db_session.execute(
                    select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.project_id == copy["id"])
                )
            )
            .scalars()
            .all()
        )
        assert len(plan) == 1
        assert plan[0].library_file_id == seeded["file_id"]
        assert (plan[0].copies, plan[0].order_index) == (3, 7)

    async def test_history_does_not_come(self, async_client, db_session):
        seeded = await _seed(db_session)

        copy = await _duplicate(async_client, seeded["project_id"])

        db_session.expire_all()
        n = await db_session.scalar(
            select(func.count()).select_from(PrintArchive).where(PrintArchive.project_id == copy["id"])
        )
        assert n == 0, "the copy inherited the source's print history"
        assert copy["stats"]["total_archives"] == 0
        assert copy["stats"]["completed_prints"] == 0
        # ...while the source still has it.
        src_archives = await db_session.scalar(
            select(func.count()).select_from(PrintArchive).where(PrintArchive.project_id == seeded["project_id"])
        )
        assert src_archives == 1


class TestItIsACopyNotAMove:
    async def test_the_source_keeps_its_library_links(self, async_client, db_session):
        seeded = await _seed(db_session)

        copy = await _duplicate(async_client, seeded["project_id"])

        db_session.expire_all()
        lib = (
            await db_session.execute(
                select(LibraryFile).where(LibraryFile.id == seeded["file_id"]).execution_options(populate_existing=True)
            )
        ).scalar_one()
        await db_session.refresh(lib, ["projects"])
        linked = {p.id for p in lib.projects}
        assert seeded["project_id"] in linked, "the file was MOVED off the source instead of copied"
        assert copy["id"] in linked

    async def test_the_source_keeps_its_folder_links_and_its_own_bom(self, async_client, db_session):
        seeded = await _seed(db_session)

        await _duplicate(async_client, seeded["project_id"])

        db_session.expire_all()
        folder = (
            await db_session.execute(select(LibraryFolder).where(LibraryFolder.id == seeded["folder_id"]))
        ).scalar_one()
        await db_session.refresh(folder, ["projects"])
        assert seeded["project_id"] in {p.id for p in folder.projects}

        src_bom = (
            (await db_session.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == seeded["project_id"])))
            .scalars()
            .all()
        )
        assert src_bom[0].quantity_acquired == 17, "resetting the copy's progress reached back into the source"

    async def test_a_second_copy_gets_its_own_name(self, async_client, db_session):
        seeded = await _seed(db_session)

        first = await _duplicate(async_client, seeded["project_id"])
        second = await _duplicate(async_client, seeded["project_id"])

        assert first["name"] == "Voron Build (Copy)"
        assert second["name"] == "Voron Build (Copy 2)"

    async def test_an_explicit_name_wins(self, async_client, db_session):
        seeded = await _seed(db_session)

        copy = await _duplicate(async_client, seeded["project_id"], name="Voron for Ihor")

        assert copy["name"] == "Voron for Ihor"


class TestSubProjects:
    async def test_children_are_left_alone_by_default(self, async_client, db_session):
        seeded = await _seed(db_session, with_child=True)

        copy = await _duplicate(async_client, seeded["project_id"])

        assert copy["children"] == []
        db_session.expire_all()
        n = await db_session.scalar(select(func.count()).select_from(Project).where(Project.name == "Frame"))
        assert n == 1, "the child was duplicated without being asked for"

    async def test_the_tree_comes_when_asked(self, async_client, db_session):
        seeded = await _seed(db_session, with_child=True)

        copy = await _duplicate(async_client, seeded["project_id"], include_children=True)

        assert [c["name"] for c in copy["children"]] == ["Frame"]
        db_session.expire_all()
        new_child = (await db_session.execute(select(Project).where(Project.parent_id == copy["id"]))).scalar_one()
        assert new_child.id != seeded["child_id"]
        # ...and the child's own belongings came with it, reset the same way.
        child_bom = (
            (await db_session.execute(select(ProjectBOMItem).where(ProjectBOMItem.project_id == new_child.id)))
            .scalars()
            .all()
        )
        assert [(b.name, b.quantity_acquired) for b in child_bom] == [("rail", 0)]

    async def test_the_copy_is_a_sibling_of_its_source(self, async_client, db_session):
        """Not a child of it — that would nest a duplicate under the original."""
        seeded = await _seed(db_session, with_child=True)
        child_id = seeded["child_id"]

        copy = await _duplicate(async_client, child_id)

        assert copy["parent_id"] == seeded["project_id"]


async def test_duplicating_a_missing_project_is_404(async_client):
    resp = await async_client.post("/api/v1/projects/999999/duplicate", json={})
    assert resp.status_code == 404
