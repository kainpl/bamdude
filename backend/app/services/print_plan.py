"""Auto-sync helpers for project print-plan rows.

Plan rows mirror ``library_files.project_id``: whenever a file ends up
attached to a project (via folder cascade, direct file update, or bulk
move) a row appears here; when detached, the row is removed. Only
``.3mf`` files participate.

Totals are NOT cached here — they're computed in the read endpoint from
``file_metadata × copies``, so reslicing a 3MF flows through without
another sync pass.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Create a plan row for (project, file) if one doesn't exist yet.

    No-op when the file isn't plan-eligible or the row already exists.
    Caller is responsible for committing.
    """
    if not _is_plan_eligible(file_type):
        return

    existing = await db.execute(
        select(ProjectPrintPlanItem.id).where(ProjectPrintPlanItem.library_file_id == library_file_id)
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


async def remove_plan_row(db: AsyncSession, *, library_file_id: int) -> None:
    """Delete any plan row for the given library file. Commit is caller's job."""
    await db.execute(delete(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == library_file_id))


async def sync_plan_for_folder(db: AsyncSession, *, folder_id: int, new_project_id: int | None) -> None:
    """Reconcile plan rows for every file in a folder after its project link changed.

    - ``new_project_id is None`` → delete all rows for those files.
    - Otherwise → move/create rows pointing at the new project, preserving
      existing ``copies`` and ``order_index`` when the row already existed
      for a different project (shouldn't normally happen since a file is
      1:1 with a project, but this keeps us safe if somebody hand-edits).
    """
    files = (
        await db.execute(select(LibraryFile.id, LibraryFile.file_type).where(LibraryFile.folder_id == folder_id))
    ).all()

    if not files:
        return

    file_ids_eligible = [fid for fid, ftype in files if _is_plan_eligible(ftype)]

    if new_project_id is None:
        if file_ids_eligible:
            await db.execute(
                delete(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id.in_(file_ids_eligible))
            )
        return

    # For each eligible file: either update the row's project_id (stale
    # link) or create a fresh row at the next order index.
    for fid in file_ids_eligible:
        existing = (
            await db.execute(select(ProjectPrintPlanItem).where(ProjectPrintPlanItem.library_file_id == fid))
        ).scalar_one_or_none()
        if existing is not None:
            if existing.project_id != new_project_id:
                existing.project_id = new_project_id
            continue
        order_index = await _next_order_index(db, new_project_id)
        db.add(
            ProjectPrintPlanItem(
                project_id=new_project_id,
                library_file_id=fid,
                copies=1,
                order_index=order_index,
            )
        )
