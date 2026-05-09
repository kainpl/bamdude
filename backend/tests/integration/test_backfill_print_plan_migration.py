"""Regression test for m048_backfill_print_plan_from_pivots.

Reproduces the on-disk state for users who uploaded files into project
folders pre-fix (the helper-call paths in `library.py` only land for new
uploads going forward) — files have ``library_file_projects`` M2M rows
but no matching ``project_print_plan_items`` rows. The migration's
``upgrade()`` must plant the missing plan rows from the pivot table.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


async def test_m048_backfills_missing_plan_rows_from_pivots(db_session):
    """Existing M2M pair without a plan row → migration creates one."""
    from backend.app.migrations import m048_backfill_print_plan_from_pivots
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    # Seed: project + folder linked + 3MF file linked via M2M but with NO
    # plan row (the pre-fix state every operator's DB is in).
    project = Project(name="Backfill Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Backfill Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.flush()

    f = LibraryFile(
        folder_id=folder.id,
        filename="orphan-from-upload.gcode.3mf",
        file_path="/tmp/orphan.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    f.projects = [project]  # M2M link present (this is what move/patch sets)
    db_session.add(f)
    await db_session.commit()

    # Pre-migration: zero plan rows.
    pre = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert pre == [], "Pre-migration must have no plan rows (test setup invariant)"

    # Run the migration's upgrade() against the live connection.
    conn = await db_session.connection()
    await m048_backfill_print_plan_from_pivots.upgrade(conn)
    await db_session.commit()

    # Post-migration: exactly one plan row, copies=1 default, order_index=0
    # (first row for this project).
    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].project_id == project.id
    assert rows[0].copies == 1
    assert rows[0].order_index == 0


async def test_m048_backfills_sliced_gcode_3mf_files(db_session):
    """Sliced ``.gcode.3mf`` files (file_type='gcode' per detect_file_type)
    must be backfilled — the typical case after a slice-and-save flow that
    the user actually has in their library."""
    from backend.app.migrations import m048_backfill_print_plan_from_pivots
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    project = Project(name="Sliced Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Sliced Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.flush()

    f = LibraryFile(
        folder_id=folder.id,
        filename="benchy.gcode.3mf",
        file_path="/tmp/benchy.gcode.3mf",
        file_type="gcode",  # detect_file_type collapses .gcode.3mf to "gcode"
        file_size=1,
        file_hash=None,
    )
    f.projects = [project]
    db_session.add(f)
    await db_session.commit()

    conn = await db_session.connection()
    await m048_backfill_print_plan_from_pivots.upgrade(conn)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].project_id == project.id


async def test_m048_skips_non_3mf_files(db_session):
    """STL / image / .gcode-without-3mf shouldn't get a plan row even if M2M
    links exist — only 3MFs are plan-eligible."""
    from backend.app.migrations import m048_backfill_print_plan_from_pivots
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    project = Project(name="Non-3MF Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Non-3MF Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.flush()

    stl = LibraryFile(
        folder_id=folder.id,
        filename="model.stl",
        file_path="/tmp/model.stl",
        file_type="stl",
        file_size=1,
        file_hash=None,
    )
    stl.projects = [project]
    db_session.add(stl)
    await db_session.commit()

    conn = await db_session.connection()
    await m048_backfill_print_plan_from_pivots.upgrade(conn)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == stl.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_m048_skips_trashed_files(db_session):
    """Trashed (deleted_at IS NOT NULL) files shouldn't be backfilled — they
    aren't part of the live print plan."""
    from datetime import datetime, timezone

    from backend.app.migrations import m048_backfill_print_plan_from_pivots
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    project = Project(name="Trashed Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Trashed Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.flush()

    f = LibraryFile(
        folder_id=folder.id,
        filename="deleted.gcode.3mf",
        file_path="/tmp/deleted.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
        deleted_at=datetime.now(timezone.utc),
    )
    f.projects = [project]
    db_session.add(f)
    await db_session.commit()

    conn = await db_session.connection()
    await m048_backfill_print_plan_from_pivots.upgrade(conn)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert rows == []


async def test_m048_idempotent_when_plan_row_already_exists(db_session):
    """If a plan row already exists (curated by the operator or planted by
    the live helper), the migration must not duplicate or overwrite it —
    ``WHERE NOT EXISTS`` guard."""
    from backend.app.migrations import m048_backfill_print_plan_from_pivots
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    project = Project(name="Idempotent Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Idempotent Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.flush()

    f = LibraryFile(
        folder_id=folder.id,
        filename="curated.gcode.3mf",
        file_path="/tmp/curated.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    f.projects = [project]
    db_session.add(f)
    await db_session.flush()

    # Pre-existing plan row with curated copies=5.
    db_session.add(
        ProjectPrintPlanItem(
            project_id=project.id,
            library_file_id=f.id,
            copies=5,
            order_index=42,
        )
    )
    await db_session.commit()

    conn = await db_session.connection()
    await m048_backfill_print_plan_from_pivots.upgrade(conn)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == f.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    # Curated values preserved — migration didn't overwrite.
    assert rows[0].copies == 5
    assert rows[0].order_index == 42


async def test_m048_appends_after_existing_max_order_index(db_session):
    """Backfilled rows append after the project's existing max(order_index)
    so curated rows keep their position."""
    from backend.app.migrations import m048_backfill_print_plan_from_pivots
    from backend.app.models.library import LibraryFile, LibraryFolder
    from backend.app.models.project import Project
    from backend.app.models.project_print_plan import ProjectPrintPlanItem

    project = Project(name="Order Test", description="")
    db_session.add(project)
    await db_session.flush()
    folder = LibraryFolder(name="Order Folder")
    folder.projects = [project]
    db_session.add(folder)
    await db_session.flush()

    # Curated file at order_index=10 (already has plan row).
    curated = LibraryFile(
        folder_id=folder.id,
        filename="curated.gcode.3mf",
        file_path="/tmp/curated.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    curated.projects = [project]
    db_session.add(curated)
    await db_session.flush()
    db_session.add(
        ProjectPrintPlanItem(
            project_id=project.id,
            library_file_id=curated.id,
            copies=2,
            order_index=10,
        )
    )

    # Orphan file with M2M link but no plan row.
    orphan = LibraryFile(
        folder_id=folder.id,
        filename="orphan.gcode.3mf",
        file_path="/tmp/orphan.gcode.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=None,
    )
    orphan.projects = [project]
    db_session.add(orphan)
    await db_session.commit()

    conn = await db_session.connection()
    await m048_backfill_print_plan_from_pivots.upgrade(conn)
    await db_session.commit()

    orphan_row = (
        await db_session.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == orphan.id))
    ).scalar_one()
    # 10 + 1 = 11 (appended after curated's max).
    assert orphan_row.order_index == 11
