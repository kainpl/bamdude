"""Integration tests for the per-project print plan (§project-plan).

Exercises the full lifecycle: folder-link cascade, direct file link, copies
update with min-1 guard, reorder, unlink cleanup, and computed totals
(filament + time + objects + cost).
"""

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_project(db_session, name: str = "Plan Test") -> int:
    """Create via the shared db_session so the async_client's override can see it.

    The override_get_db fixture in conftest doesn't auto-commit, so POSTs that
    only flush (like /projects/create) don't persist across the test — we seed
    the row directly on the shared engine instead.
    """
    from backend.app.models.project import Project

    project = Project(name=name, description="plan test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project.id


async def _create_folder(db_session, name: str = "Plan Folder") -> int:
    from backend.app.models.library import LibraryFolder

    folder = LibraryFolder(name=name)
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return folder.id


async def _add_library_file(
    db_session,
    *,
    folder_id: int | None = None,
    project_id: int | None = None,
    file_type: str = "3mf",
    filament_grams: float | None = 100.0,
    print_time_seconds: int | None = 1800,
    objects: int | None = 2,
) -> int:
    """Insert a LibraryFile directly (skipping the upload pipeline).

    ``project_id`` (legacy single-FK kwarg) is preserved for caller
    convenience: when set, the file is attached to that project via the
    m044 M2M pivot.
    """
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile
    from backend.app.models.project import Project

    metadata: dict = {}
    if filament_grams is not None:
        metadata["filament_used_grams"] = filament_grams
    if print_time_seconds is not None:
        metadata["print_time_seconds"] = print_time_seconds
    if objects is not None:
        metadata["printable_objects"] = {str(i): f"obj{i}" for i in range(objects)}

    f = LibraryFile(
        folder_id=folder_id,
        filename=f"test-{file_type}.{file_type}",
        file_path=f"/tmp/test.{file_type}",
        file_type=file_type,
        file_size=123,
        file_hash=None,
        file_metadata=metadata or None,
    )
    if project_id is not None:
        project = (await db_session.execute(select(Project).where(Project.id == project_id))).scalar_one()
        f.projects = [project]
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)
    return f.id


async def test_linking_folder_populates_plan_for_3mf_files(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)

    f_3mf_a = await _add_library_file(db_session, folder_id=folder_id, filament_grams=50.0, objects=3)
    f_3mf_b = await _add_library_file(db_session, folder_id=folder_id, filament_grams=80.0, objects=1)
    f_stl = await _add_library_file(db_session, folder_id=folder_id, file_type="stl", filament_grams=None, objects=None)

    resp = await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})
    assert resp.status_code == 200, resp.text

    plan_resp = await async_client.get(f"/api/v1/projects/{project_id}/print-plan")
    assert plan_resp.status_code == 200
    plan = plan_resp.json()

    plan_file_ids = [i["library_file_id"] for i in plan["items"]]
    assert f_3mf_a in plan_file_ids and f_3mf_b in plan_file_ids
    assert f_stl not in plan_file_ids, "non-3MF files must not enter the plan"

    # Totals account for default copies=1 on each row.
    # 50 + 80 = 130g, 3 + 1 = 4 objects.
    assert plan["totals_filament_grams"] == pytest.approx(130.0)
    assert plan["totals_objects"] == 4


async def test_update_copies_multiplies_totals(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    file_id = await _add_library_file(
        db_session, folder_id=folder_id, filament_grams=40.0, objects=2, print_time_seconds=600
    )
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})

    # Bump copies to 3
    patch_resp = await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": 3})
    assert patch_resp.status_code == 200, patch_resp.text
    item = patch_resp.json()
    assert item["copies"] == 3
    assert item["total_filament_grams"] == pytest.approx(120.0)
    assert item["total_objects"] == 6
    assert item["total_print_time_seconds"] == 1800

    # Totals roll up through the plan response too.
    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan["totals_filament_grams"] == pytest.approx(120.0)


async def test_copies_minimum_is_one(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    file_id = await _add_library_file(db_session, folder_id=folder_id)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})

    resp = await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": 0})
    assert resp.status_code == 400

    resp_neg = await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": -2})
    assert resp_neg.status_code == 400


async def test_reorder_updates_order_index(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    a = await _add_library_file(db_session, folder_id=folder_id)
    b = await _add_library_file(db_session, folder_id=folder_id)
    c = await _add_library_file(db_session, folder_id=folder_id)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})

    plan_before = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    original_order = [i["library_file_id"] for i in plan_before["items"]]
    assert original_order == [a, b, c]  # creation order (backfill path agnostic)

    # Send reversed order
    reorder_resp = await async_client.post(
        f"/api/v1/projects/{project_id}/print-plan/reorder",
        json={"library_file_ids": [c, b, a]},
    )
    assert reorder_resp.status_code == 200
    new_order = [i["library_file_id"] for i in reorder_resp.json()["items"]]
    assert new_order == [c, b, a]


async def test_direct_file_link_populates_plan(async_client: AsyncClient, db_session):
    """Linking a single file to a project (without linking its folder) still
    populates a plan row — this is the path used when the user clicks the
    per-file Link button from the File Manager card/row."""
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    file_id = await _add_library_file(db_session, folder_id=folder_id, filament_grams=42.0, objects=2)

    # Folder stays unlinked — only the file gets a project.
    resp = await async_client.put(f"/api/v1/library/files/{file_id}", json={"project_ids": [project_id]})
    assert resp.status_code == 200, resp.text

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert len(plan["items"]) == 1
    assert plan["items"][0]["library_file_id"] == file_id
    assert plan["items"][0]["copies"] == 1
    assert plan["items"][0]["total_filament_grams"] == pytest.approx(42.0)


async def test_unlinking_file_removes_plan_row(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    file_id = await _add_library_file(db_session, folder_id=folder_id)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert len(plan["items"]) == 1

    # Unlink the file via project_ids=[]
    unlink = await async_client.put(f"/api/v1/library/files/{file_id}", json={"project_ids": []})
    assert unlink.status_code == 200

    plan_after = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan_after["items"] == []


async def test_unlinking_folder_clears_plan_rows(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    await _add_library_file(db_session, folder_id=folder_id)
    await _add_library_file(db_session, folder_id=folder_id)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert len(plan["items"]) == 2

    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": []})
    plan_after = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan_after["items"] == []


async def test_cost_uses_default_filament_cost_setting(async_client: AsyncClient, db_session):
    # Explicit cost/kg via settings endpoint — plan response should echo and multiply.
    await async_client.put("/api/v1/settings/", json={"default_filament_cost": 30.0})

    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    # 200g × 30/kg = 6.0 per copy. Two copies = 12.0.
    file_id = await _add_library_file(db_session, folder_id=folder_id, filament_grams=200.0)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})

    await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": 2})

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan["default_filament_cost_per_kg"] == pytest.approx(30.0)
    assert plan["items"][0]["cost_per_copy"] == pytest.approx(6.0)
    assert plan["items"][0]["total_cost"] == pytest.approx(12.0)
    assert plan["totals_cost"] == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# m044 M2M behaviour: a single library file can belong to multiple projects.
# Each (project, library_file) pair gets its own plan row so copies / order
# per project remain independent.
# ---------------------------------------------------------------------------


async def test_file_linked_to_two_projects_has_independent_plan_rows(async_client: AsyncClient, db_session):
    p1 = await _create_project(db_session, name="Project A")
    p2 = await _create_project(db_session, name="Project B")
    file_id = await _add_library_file(db_session, filament_grams=60.0, objects=2)

    # Attach the file to both projects in one update.
    resp = await async_client.put(f"/api/v1/library/files/{file_id}", json={"project_ids": [p1, p2]})
    assert resp.status_code == 200, resp.text

    # Each project sees the file with its own default-1 copies.
    plan1 = (await async_client.get(f"/api/v1/projects/{p1}/print-plan")).json()
    plan2 = (await async_client.get(f"/api/v1/projects/{p2}/print-plan")).json()
    assert [i["library_file_id"] for i in plan1["items"]] == [file_id]
    assert [i["library_file_id"] for i in plan2["items"]] == [file_id]

    # Bump copies on project A only — project B's plan row stays at 1.
    await async_client.patch(f"/api/v1/projects/{p1}/print-plan/{file_id}", json={"copies": 4})
    plan1 = (await async_client.get(f"/api/v1/projects/{p1}/print-plan")).json()
    plan2 = (await async_client.get(f"/api/v1/projects/{p2}/print-plan")).json()
    assert plan1["items"][0]["copies"] == 4
    assert plan2["items"][0]["copies"] == 1
    assert plan1["totals_filament_grams"] == pytest.approx(240.0)
    assert plan2["totals_filament_grams"] == pytest.approx(60.0)


async def test_per_project_unlink_keeps_other_project_plan_intact(async_client: AsyncClient, db_session):
    p1 = await _create_project(db_session, name="Project A")
    p2 = await _create_project(db_session, name="Project B")
    file_id = await _add_library_file(db_session, filament_grams=50.0)
    await async_client.put(f"/api/v1/library/files/{file_id}", json={"project_ids": [p1, p2]})

    # DELETE the per-(file, project) link for project A only.
    unlink = await async_client.delete(f"/api/v1/library/files/{file_id}/projects/{p1}")
    assert unlink.status_code in (200, 204), unlink.text

    plan1 = (await async_client.get(f"/api/v1/projects/{p1}/print-plan")).json()
    plan2 = (await async_client.get(f"/api/v1/projects/{p2}/print-plan")).json()
    assert plan1["items"] == []
    assert [i["library_file_id"] for i in plan2["items"]] == [file_id]


async def test_folder_linked_to_two_projects_creates_plan_rows_in_both(async_client: AsyncClient, db_session):
    p1 = await _create_project(db_session, name="Folder A")
    p2 = await _create_project(db_session, name="Folder B")
    folder_id = await _create_folder(db_session)
    fa = await _add_library_file(db_session, folder_id=folder_id, filament_grams=10.0)
    fb = await _add_library_file(db_session, folder_id=folder_id, filament_grams=20.0)

    resp = await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [p1, p2]})
    assert resp.status_code == 200, resp.text

    plan1 = (await async_client.get(f"/api/v1/projects/{p1}/print-plan")).json()
    plan2 = (await async_client.get(f"/api/v1/projects/{p2}/print-plan")).json()
    assert sorted(i["library_file_id"] for i in plan1["items"]) == sorted([fa, fb])
    assert sorted(i["library_file_id"] for i in plan2["items"]) == sorted([fa, fb])

    # Per-project folder unlink purges only that project's rows.
    unlink = await async_client.delete(f"/api/v1/library/folders/{folder_id}/projects/{p1}")
    assert unlink.status_code in (200, 204), unlink.text
    plan1_after = (await async_client.get(f"/api/v1/projects/{p1}/print-plan")).json()
    plan2_after = (await async_client.get(f"/api/v1/projects/{p2}/print-plan")).json()
    assert plan1_after["items"] == []
    assert sorted(i["library_file_id"] for i in plan2_after["items"]) == sorted([fa, fb])


async def test_plan_progress_counts_completed_archives(async_client: AsyncClient, db_session):
    """``printed_count`` reflects completed archives for that (project, file) pair;
    ``remaining_count`` = max(0, copies - printed)."""
    from backend.app.models.archive import PrintArchive

    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    file_id = await _add_library_file(db_session, folder_id=folder_id)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_ids": [project_id]})
    await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": 5})

    # Seed 2× completed + 1× failed for this (project, file). Only the
    # completed ones should count towards printed.
    for status in ("completed", "completed", "failed"):
        archive = PrintArchive(
            project_id=project_id,
            library_file_id=file_id,
            filename="x.3mf",
            file_path="/tmp/x.3mf",
            file_size=1,
            status=status,
        )
        db_session.add(archive)
    # An unrelated completed archive in another project must NOT bleed in.
    other_project = await _create_project(db_session, name="Other")
    db_session.add(
        PrintArchive(
            project_id=other_project,
            library_file_id=file_id,
            filename="x.3mf",
            file_path="/tmp/x.3mf",
            file_size=1,
            status="completed",
        )
    )
    await db_session.commit()

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    item = plan["items"][0]
    assert item["copies"] == 5
    assert item["printed_count"] == 2
    assert item["remaining_count"] == 3

    # If printed exceeds copies (over-prints), remaining clamps to 0.
    db_session.add(
        PrintArchive(
            project_id=project_id,
            library_file_id=file_id,
            filename="x.3mf",
            file_path="/tmp/x.3mf",
            file_size=1,
            status="completed",
        )
    )
    await db_session.commit()
    await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": 1})
    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    item = plan["items"][0]
    assert item["printed_count"] == 3
    assert item["remaining_count"] == 0


async def test_inherit_folder_projects_helper_creates_plan_row(db_session):
    """Pin the helper contract: ``inherit_folder_projects`` assigns the M2M
    AND plants matching plan rows in one call.

    Pre-fix every upload / extract-zip / sliced-output / MakerWorld-import
    path bypassed this — the file row was created with empty M2M and
    ``project_print_plan_items`` stayed empty no matter how many 3MFs the
    user dropped into the project's folder.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem
    from backend.app.services.print_plan import inherit_folder_projects

    # Seed: project + folder linked to it.
    project = Project(name="Inherit Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Inherit Folder")
    folder.projects = [project]  # set before add so SQLAlchemy doesn't lazy-load
    db_session.add(folder)
    await db_session.commit()

    # Re-fetch with .projects eager-loaded — the helper's contract requires
    # this, and the production paths call it from selectinload-loaded folder
    # rows.
    folder = (
        await db_session.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder.id).options(selectinload(LibraryFolder.projects))
        )
    ).scalar_one()

    # Create a fresh file row in the folder — empty projects M2M, no plan
    # row, just like the upload path produced pre-fix.
    f = LibraryFile(
        folder_id=folder.id,
        filename="auto-inherit.gcode.3mf",
        file_path="/tmp/auto-inherit.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    db_session.add(f)
    await db_session.flush()

    # Pre-helper assertion: zero plan rows for this file.
    pre = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert pre == []

    # Run the helper.
    await inherit_folder_projects(db_session, f, folder)
    await db_session.commit()

    # Post: file is in the project's M2M, and a plan row exists.
    await db_session.refresh(f, ["projects"])
    assert [p.id for p in f.projects] == [project.id]

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].project_id == project.id
    assert rows[0].copies == 1


async def test_inherit_folder_projects_skips_non_plan_eligible(db_session):
    """An ``.stl`` (or any non-3mf) file dropped in a project folder must not
    plant a plan row — only 3MFs are plan-eligible. The M2M is also left
    untouched (folder→file project inherit is plan-driven; STLs aren't part
    of the print plan)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem
    from backend.app.services.print_plan import inherit_folder_projects

    project = Project(name="STL Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="STL Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.commit()

    folder = (
        await db_session.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder.id).options(selectinload(LibraryFolder.projects))
        )
    ).scalar_one()

    f = LibraryFile(
        folder_id=folder.id,
        filename="model.stl",
        file_path="/tmp/model.stl",
        file_type="stl",
        file_size=1,
        file_hash=None,
    )
    db_session.add(f)
    await db_session.flush()

    await inherit_folder_projects(db_session, f, folder)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_inherit_folder_projects_no_op_when_folder_has_no_projects(db_session):
    """Folder with empty .projects list → helper is a clean no-op (no M2M
    write, no plan row, no error)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project_print_plan import ProjectPrintPlanItem
    from backend.app.services.print_plan import inherit_folder_projects

    folder = LibraryFolder(name="Plain Folder")
    db_session.add(folder)
    await db_session.commit()
    folder = (
        await db_session.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder.id).options(selectinload(LibraryFolder.projects))
        )
    ).scalar_one()
    assert list(folder.projects) == []

    f = LibraryFile(
        folder_id=folder.id,
        filename="orphan.gcode.3mf",
        file_path="/tmp/orphan.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    db_session.add(f)
    await db_session.flush()

    await inherit_folder_projects(db_session, f, folder)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_inherit_folder_projects_idempotent_on_second_call(db_session):
    """Calling the helper twice on the same (file, folder) pair must not
    create duplicate plan rows — covers the case where a route accidentally
    fires the inherit twice (e.g. on retry)."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem
    from backend.app.services.print_plan import inherit_folder_projects

    project = Project(name="Idempotent Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Idempotent Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.commit()
    folder = (
        await db_session.execute(
            select(LibraryFolder).where(LibraryFolder.id == folder.id).options(selectinload(LibraryFolder.projects))
        )
    ).scalar_one()

    f = LibraryFile(
        folder_id=folder.id,
        filename="idempotent.gcode.3mf",
        file_path="/tmp/idempotent.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    db_session.add(f)
    await db_session.flush()

    await inherit_folder_projects(db_session, f, folder)
    await db_session.commit()

    # Second call — sync should detect the existing row and not duplicate.
    await inherit_folder_projects(db_session, f, folder)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
