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

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.library import LibraryFile
from backend.app.models.project_print_plan import ProjectPrintPlanItem


def _is_plan_eligible(file_type: str | None) -> bool:
    """Plan-eligible files are sliced 3MFs. Everything else is ignored."""
    return bool(file_type) and file_type.lower() == "3mf"


async def _next_order_index(db: AsyncSession, project_id: int) -> int:
    result = await db.execute(
        select(func.coalesce(func.max(ProjectPrintPlanItem.order_index), -1)).where(
            ProjectPrintPlanItem.project_id == project_id
        )
    )
    return (result.scalar() or -1) + 1


async def ensure_plan_row(db: AsyncSession, *, library_file_id: int, project_id: int, file_type: str) -> None:
    """Create a plan row for (project, file) if it doesn't exist yet.

    No-op when the file isn't plan-eligible or the row already exists.
    Caller is responsible for committing.
    """
    if not _is_plan_eligible(file_type):
        return

    existing = await db.execute(
        select(ProjectPrintPlanItem.id).where(
            ProjectPrintPlanItem.library_file_id == library_file_id,
            ProjectPrintPlanItem.project_id == project_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return

    order_index = await _next_order_index(db, project_id)
    db.add(
        ProjectPrintPlanItem(
            project_id=project_id,
            library_file_id=library_file_id,
            copies=1,
            order_index=order_index,
        )
    )


async def remove_plan_row(db: AsyncSession, *, library_file_id: int, project_id: int) -> None:
    """Delete the plan row for one specific (project, file) pair."""
    await db.execute(
        delete(ProjectPrintPlanItem).where(
            ProjectPrintPlanItem.library_file_id == library_file_id,
            ProjectPrintPlanItem.project_id == project_id,
        )
    )


async def remove_all_plan_rows_for_file(db: AsyncSession, *, library_file_id: int) -> None:
    """Drop every plan row referencing this file (every project).

    Used when the file itself is being hard-deleted at the ORM level.
    The DB-level ``ON DELETE CASCADE`` on ``library_file_id`` handles
    the same cleanup for raw-SQL deletes; this helper exists for the
    in-session ``db.delete(file)`` path so SQLAlchemy doesn't trip on
    a stale reference.
    """
    await db.execute(delete(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == library_file_id))


async def sync_plan_for_file(
    db: AsyncSession,
    *,
    library_file_id: int,
    project_ids: list[int],
    file_type: str,
) -> None:
    """Reconcile plan rows for a single file with its target project list.

    Diffs the existing rows against the desired ``project_ids`` set and
    inserts / deletes to make them match. Non-plan-eligible files just
    have all their rows removed.
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

    # Add rows for newly-linked projects (one at a time so order_index
    # stays correct per project).
    for project_id in desired - existing_project_ids:
        order_index = await _next_order_index(db, project_id)
        db.add(
            ProjectPrintPlanItem(
                project_id=project_id,
                library_file_id=library_file_id,
                copies=1,
                order_index=order_index,
            )
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
