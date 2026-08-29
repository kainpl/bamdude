"""Auto-sync helpers for project print-plan rows.

Plan rows mirror the file→project links: every (project, library_file)
pair that ends up in the M2M pivot owns a corresponding plan row, and
detaching the file from a project drops just that row. Only ``.3mf``
files participate; other formats are ignored.

Since m044 a single library file can belong to N projects, so this
module key is **(project_id, library_file_id)** rather than
``library_file_id`` alone — a file in 3 projects has 3 plan rows with
independent ``copies`` and ``order_index``.

Totals are NOT cached here — they're computed in the read endpoint from
``file_metadata × copies``, so reslicing a 3MF flows through without
another sync pass.
"""

from __future__ import annotations

from sqlalchemy import delete, func, inspect as sqla_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile
from backend.app.models.project_print_plan import ProjectPrintPlanItem
from backend.app.services.project_parts import seed_project_parts_for_file


def _is_plan_eligible(file_type: str | None) -> bool:
    """Plan-eligible files are anything printable: sliced ``.gcode.3mf``
    (which ``library_helpers.detect_file_type`` collapses to ``"gcode"``),
    raw ``.gcode``, AND unsliced ``.3mf`` project packages.

    Pre-fix this only matched ``"3mf"``, so every sliced file in the
    library — the typical case after a slice-and-save flow — was filtered
    out of the plan. Re-attaching a folder to a project would correctly
    set the M2M pivot but skip the plan-row plant for these files,
    leaving operators with empty plans even after a clean re-link.

    STL / OBJ / STEP / STP are not directly printable → still excluded;
    those formats must be sliced first (which produces a ``.gcode``-typed
    sibling that DOES enter the plan).
    """
    if not file_type:
        return False
    return file_type.lower() in ("3mf", "gcode")


async def _next_order_index(db: AsyncSession, project_id: int) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(ProjectPrintPlanItem.order_index), -1)).where(
            ProjectPrintPlanItem.project_id == project_id
        )
    )
    return (result.scalar() or -1) + 1


def _plate_indices(file_metadata: dict | None) -> list[int]:
    """Sorted positive plate indices from ``file_metadata["plates"]``.

    Empty (no plate metadata — raw gcode or a file that hasn't been through
    the 3MF plate-metadata extraction — or a single plate) means "the whole
    file is one printable unit"; callers collapse that to the single
    ``plate_index=0`` row. Two or more distinct positive indices mean the
    file is a multi-plate 3MF, one plan row per plate.
    """
    if not file_metadata:
        return []
    plates = file_metadata.get("plates") or []
    indices = {
        int(p["index"]) for p in plates if isinstance(p, dict) and isinstance(p.get("index"), int) and p["index"] > 0
    }
    return sorted(indices)


async def sync_plan_for_file(
    db: AsyncSession,
    *,
    library_file_id: int,
    project_ids: list[int],
    file_type: str,
) -> None:
    """Reconcile plan rows for a single file with its target project list,
    at (project, file, plate) granularity.

    Diffs the existing rows against the desired ``project_ids`` set and
    inserts / deletes to make the project set match. Non-plan-eligible
    files just have all their rows removed. For every project that stays
    (or becomes) linked, the row's *plate* set is reconciled too: a
    single-plate file (or one with no plate metadata, e.g. raw gcode) owns
    exactly one row at ``plate_index=0``; an N>1-plate file owns one row
    per plate index. A legacy ``plate_index=0`` row on a file that turns
    out to be multi-plate is replaced by per-plate rows that inherit its
    ``copies``/``order_index`` (same rule m158's seed uses for existing
    DBs) — new plate rows otherwise start at ``copies=1``. Re-slicing
    (plate count changes on a file already tracked) drops rows for plates
    that vanished, plants ``copies=1`` rows for new plates, and leaves
    surviving plates' rows — and their ``copies`` — untouched.
    """
    existing_rows = (
        (await db.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == library_file_id)))
        .scalars()
        .all()
    )
    existing_project_ids = {row.project_id for row in existing_rows}

    if not _is_plan_eligible(file_type):
        if existing_rows:
            await db.execute(
                delete(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == library_file_id)
            )
        return

    desired = set(project_ids)

    # Drop rows for projects no longer linked.
    to_remove = existing_project_ids - desired
    if to_remove:
        await db.execute(
            delete(ProjectPrintPlanItem).where(
                ProjectPrintPlanItem.library_file_id == library_file_id,
                ProjectPrintPlanItem.project_id.in_(to_remove),
            )
        )

    file_metadata = (
        await db.execute(select(LibraryFile.file_metadata).where(LibraryFile.id == library_file_id))
    ).scalar_one_or_none()
    indices = _plate_indices(file_metadata)
    wanted_plates: set[int] = set(indices) if len(indices) > 1 else {0}

    rows_by_project: dict[int, list[ProjectPrintPlanItem]] = {}
    for row in existing_rows:
        rows_by_project.setdefault(row.project_id, []).append(row)

    # Reconcile the plate set for every project that stays (or becomes)
    # linked — including ones already in ``existing_project_ids`` that
    # never left, so a re-slice on an unchanged project list still fans
    # out into per-plate rows.
    for project_id in desired:
        project_rows = rows_by_project.get(project_id, [])
        existing_plates = {row.plate_index: row for row in project_rows}
        if existing_plates.keys() == wanted_plates:
            continue  # already in the wanted shape, nothing to do

        legacy_whole = existing_plates.get(0) if wanted_plates != {0} else None
        if legacy_whole is not None:
            # A whole-file row on a file that turned out multi-plate:
            # replace it with per-plate rows inheriting its copies/order.
            copies = legacy_whole.copies
            order_index = legacy_whole.order_index
            await db.delete(legacy_whole)
            for plate in sorted(wanted_plates):
                db.add(
                    ProjectPrintPlanItem(
                        project_id=project_id,
                        library_file_id=library_file_id,
                        copies=copies,
                        order_index=order_index,
                        plate_index=plate,
                    )
                )
            continue

        group_order_index = project_rows[0].order_index if project_rows else await _next_order_index(db, project_id)

        # Drop rows for plates that vanished (re-slice, or a multi-plate
        # file collapsing back to one plate).
        for plate, row in existing_plates.items():
            if plate not in wanted_plates:
                await db.delete(row)

        # Plant rows for newly-appeared plates; surviving plates' rows are
        # left untouched so their copies carry over.
        for plate in sorted(wanted_plates - existing_plates.keys()):
            db.add(
                ProjectPrintPlanItem(
                    project_id=project_id,
                    library_file_id=library_file_id,
                    copies=1,
                    order_index=group_order_index,
                    plate_index=plate,
                )
            )

    # Plant ledger target rows (m158) for every linked project — idempotent,
    # so re-linking an already-linked file adds nothing.
    await seed_project_parts_for_file(db, library_file_id=library_file_id, project_ids=list(desired))


async def inherit_folder_projects(
    db: AsyncSession,
    library_file: LibraryFile,
    folder,
) -> None:
    """On file creation in a project-tagged folder, inherit the folder's
    projects into the file's M2M and plant matching plan rows.

    Symmetrical with the move / patch flows: a freshly-uploaded ``.3mf``
    that lands in a folder linked to N projects must show up in those
    projects' print plans without an extra UI step. Callers SHOULD
    ``selectinload(LibraryFolder.projects)`` to avoid a roundtrip, but
    this helper is defensive — when ``.projects`` is unloaded (e.g. a
    just-created folder via ``db.add()`` + ``db.flush()`` with no
    refresh) we refresh it ourselves so the lazy-load doesn't trip
    ``MissingGreenlet`` outside an async-engine session boundary. The
    refresh costs one extra ``SELECT … library_folder_projects`` only
    when the caller didn't pre-load the relation.

    Pre-fix, every upload / zip-extract / sliced-output / MakerWorld import
    bypassed this path entirely — the file row was created with empty
    M2M and ``project_print_plan_items`` stayed empty no matter how many
    files the user dropped into the project's folder. Bug class lurked
    since the m044 single-FK → M2M conversion (the old ``library_files
    .project_id`` column inheritance was lost in that refactor; m048
    backfills retroactive plan rows).

    Caller is responsible for committing.
    """
    if folder is None:
        return
    # Defensive load: if the caller forgot to selectinload .projects,
    # refresh the relation here instead of triggering a sync lazy-load
    # that would raise MissingGreenlet under aiosqlite/asyncpg.
    folder_state = sqla_inspect(folder)
    if "projects" in folder_state.unloaded:
        await db.refresh(folder, ["projects"])
    folder_projects = list(folder.projects or [])
    if not folder_projects:
        return

    if library_file.id is None:
        # Creation paths usually call us after ``db.flush`` so the PK
        # exists; if not, flush now so the next refresh works.
        await db.flush()

    # Eager-load the file's ``.projects`` collection before assignment.
    # On a freshly-flushed persistent row the relation is unloaded, and
    # assigning to an unloaded collection forces a lazy-load comparison —
    # which raises ``MissingGreenlet`` outside an async-engine session
    # boundary. Refreshing with the relation loaded short-circuits that.
    await db.refresh(library_file, ["projects"])

    # Mirror the move path: set the M2M, then sync plan rows in-step.
    library_file.projects = list(folder_projects)
    await sync_plan_for_file(
        db,
        library_file_id=library_file.id,
        project_ids=[p.id for p in folder_projects],
        file_type=library_file.file_type or "",
    )


async def sync_plan_for_folder(db: AsyncSession, *, folder_id: int, project_ids: list[int]) -> None:
    """Reconcile plan rows for every file in a folder after the folder's
    project list changed.

    For each eligible file in the folder, calls :func:`sync_plan_for_file`
    so every (project, file) pair lines up with the folder's new project
    list. Empty ``project_ids`` removes every plan row for the folder's
    eligible files.
    """
    files = (
        (
            await db.execute(
                select(LibraryFile)
                .where(LibraryFile.folder_id == folder_id)
                .options(selectinload(LibraryFile.projects))
            )
        )
        .scalars()
        .all()
    )

    if not files:
        return

    for file in files:
        if not _is_plan_eligible(file.file_type):
            continue
        await sync_plan_for_file(
            db,
            library_file_id=file.id,
            project_ids=project_ids,
            file_type=file.file_type,
        )
