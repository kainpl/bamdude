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
    """Insert a LibraryFile directly (skipping the upload pipeline)."""
    from backend.app.models.library import LibraryFile

    metadata: dict = {}
    if filament_grams is not None:
        metadata["filament_used_grams"] = filament_grams
    if print_time_seconds is not None:
        metadata["print_time_seconds"] = print_time_seconds
    if objects is not None:
        metadata["printable_objects"] = {str(i): f"obj{i}" for i in range(objects)}

    f = LibraryFile(
        folder_id=folder_id,
        project_id=project_id,
        filename=f"test-{file_type}.{file_type}",
        file_path=f"/tmp/test.{file_type}",
        file_type=file_type,
        file_size=123,
        file_hash=None,
        file_metadata=metadata or None,
    )
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

    resp = await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})
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
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

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
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

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
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

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
    resp = await async_client.put(f"/api/v1/library/files/{file_id}", json={"project_id": project_id})
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
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert len(plan["items"]) == 1

    # Unlink the file via project_id=0
    unlink = await async_client.put(f"/api/v1/library/files/{file_id}", json={"project_id": 0})
    assert unlink.status_code == 200

    plan_after = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan_after["items"] == []


async def test_unlinking_folder_clears_plan_rows(async_client: AsyncClient, db_session):
    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    await _add_library_file(db_session, folder_id=folder_id)
    await _add_library_file(db_session, folder_id=folder_id)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert len(plan["items"]) == 2

    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": 0})
    plan_after = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan_after["items"] == []


async def test_cost_uses_default_filament_cost_setting(async_client: AsyncClient, db_session):
    # Explicit cost/kg via settings endpoint — plan response should echo and multiply.
    await async_client.put("/api/v1/settings/", json={"default_filament_cost": 30.0})

    project_id = await _create_project(db_session)
    folder_id = await _create_folder(db_session)
    # 200g × 30/kg = 6.0 per copy. Two copies = 12.0.
    file_id = await _add_library_file(db_session, folder_id=folder_id, filament_grams=200.0)
    await async_client.put(f"/api/v1/library/folders/{folder_id}", json={"project_id": project_id})

    await async_client.patch(f"/api/v1/projects/{project_id}/print-plan/{file_id}", json={"copies": 2})

    plan = (await async_client.get(f"/api/v1/projects/{project_id}/print-plan")).json()
    assert plan["default_filament_cost_per_kg"] == pytest.approx(30.0)
    assert plan["items"][0]["cost_per_copy"] == pytest.approx(6.0)
    assert plan["items"][0]["total_cost"] == pytest.approx(12.0)
    assert plan["totals_cost"] == pytest.approx(12.0)
