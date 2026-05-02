"""Archive trash endpoints (#1008 follow-up).

Permission model:

* **Trash settings** (``/archives/trash/settings``) require
  :attr:`Permission.ARCHIVES_PURGE` — admin-only.
* **Per-user trash** (list / restore / hard-delete / empty) is gated by the
  existing :attr:`Permission.ARCHIVES_DELETE_ALL` /
  :attr:`Permission.ARCHIVES_DELETE_OWN` ownership pair, so a regular user
  sees their own trashed archives and an admin sees everyone's.

The earlier per-row "auto-purge by activity age" feature was removed in
0.4.2 — see ``services/archive_cleanup_service.py`` for the replacement
disk-reclaim path that prunes 3MF bytes per design chain instead of
destroying whole archive rows.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission, require_ownership_permission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.user import User
from backend.app.schemas.archive_purge import (
    ArchiveEmptyTrashResponse,
    ArchiveTrashItem,
    ArchiveTrashListResponse,
    ArchiveTrashSettings,
)
from backend.app.services.archive_purge import (
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    archive_purge_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/archives", tags=["archives-purge"])


# ===================== Trash list + per-item ops =====================


@router.get("/trash", response_model=ArchiveTrashListResponse)
async def list_archive_trash(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_DELETE_ALL,
            Permission.ARCHIVES_DELETE_OWN,
        )
    ),
):
    """List trashed archives.

    Admins see everyone's trashed archives; regular users see only those they
    created.
    """
    user, can_modify_all = auth_result
    retention_days = await archive_purge_service.get_retention_days(db)

    base_conditions = [PrintArchive.deleted_at.isnot(None)]
    if not can_modify_all:
        if user is None:
            raise HTTPException(status_code=403, detail="Authentication required")
        base_conditions.append(PrintArchive.created_by_id == user.id)

    total_result = await db.execute(select(func.count(PrintArchive.id)).where(*base_conditions))
    total = int(total_result.scalar() or 0)

    rows_result = await db.execute(
        select(PrintArchive, User.username)
        .outerjoin(User, PrintArchive.created_by_id == User.id)
        .where(*base_conditions)
        .order_by(PrintArchive.deleted_at.desc())
        .limit(limit)
        .offset(offset)
    )

    items: list[ArchiveTrashItem] = []
    for archive, username in rows_result.all():
        assert archive.deleted_at is not None
        auto_purge_at = archive.deleted_at + timedelta(days=retention_days)
        items.append(
            ArchiveTrashItem(
                id=archive.id,
                filename=archive.filename,
                print_name=archive.print_name,
                file_size=archive.file_size,
                thumbnail_path=archive.thumbnail_path,
                printer_id=archive.printer_id,
                project_id=archive.project_id,
                status=archive.status,
                created_by_id=archive.created_by_id,
                created_by_username=username,
                deleted_at=archive.deleted_at,
                auto_purge_at=auto_purge_at,
            )
        )

    return ArchiveTrashListResponse(items=items, total=total, retention_days=retention_days)


async def _load_trashed_archive(
    db: AsyncSession,
    archive_id: int,
    user: User | None,
    can_modify_all: bool,
) -> PrintArchive:
    result = await db.execute(
        select(PrintArchive).where(
            PrintArchive.id == archive_id,
            PrintArchive.deleted_at.isnot(None),
        )
    )
    archive = result.scalar_one_or_none()
    if archive is None:
        raise HTTPException(status_code=404, detail="Trashed archive not found")
    if not can_modify_all:
        if user is None or archive.created_by_id != user.id:
            raise HTTPException(status_code=403, detail="You can only manage your own trashed archives")
    return archive


@router.post("/trash/{archive_id}/restore")
async def restore_archive_from_trash(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_DELETE_ALL,
            Permission.ARCHIVES_DELETE_OWN,
        )
    ),
):
    user, can_modify_all = auth_result
    archive = await _load_trashed_archive(db, archive_id, user, can_modify_all)
    await archive_purge_service.restore(db, archive)
    return {"status": "success", "id": archive.id}


@router.delete("/trash/{archive_id}")
async def hard_delete_archive_from_trash(
    archive_id: int,
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_DELETE_ALL,
            Permission.ARCHIVES_DELETE_OWN,
        )
    ),
):
    """Permanently delete a trashed archive + its on-disk files. Irreversible."""
    user, can_modify_all = auth_result
    archive = await _load_trashed_archive(db, archive_id, user, can_modify_all)
    if not await archive_purge_service.hard_delete_now(archive.id):
        raise HTTPException(status_code=404, detail="Archive vanished during delete")
    return {"status": "success"}


@router.delete("/trash", response_model=ArchiveEmptyTrashResponse)
async def empty_archive_trash(
    db: AsyncSession = Depends(get_db),
    auth_result: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.ARCHIVES_DELETE_ALL,
            Permission.ARCHIVES_DELETE_OWN,
        )
    ),
):
    """Permanently delete every trashed archive in the caller's scope."""
    user, can_modify_all = auth_result
    conditions = [PrintArchive.deleted_at.isnot(None)]
    if not can_modify_all:
        if user is None:
            raise HTTPException(status_code=403, detail="Authentication required")
        conditions.append(PrintArchive.created_by_id == user.id)

    rows_result = await db.execute(select(PrintArchive.id).where(*conditions))
    ids = [row[0] for row in rows_result.all()]
    deleted = 0
    for archive_id in ids:
        if await archive_purge_service.hard_delete_now(archive_id):
            deleted += 1
    return ArchiveEmptyTrashResponse(deleted=deleted)


# ===================== Trash retention settings (admin only) =====================


@router.get("/trash/settings", response_model=ArchiveTrashSettings)
async def get_archive_trash_settings(
    db: AsyncSession = Depends(get_db),
    _: User = RequirePermission(Permission.ARCHIVES_PURGE),
):
    retention = await archive_purge_service.get_retention_days(db)
    return ArchiveTrashSettings(retention_days=retention)


@router.put("/trash/settings", response_model=ArchiveTrashSettings)
async def update_archive_trash_settings(
    body: ArchiveTrashSettings,
    db: AsyncSession = Depends(get_db),
    _: User = RequirePermission(Permission.ARCHIVES_PURGE),
):
    if body.retention_days < MIN_RETENTION_DAYS or body.retention_days > MAX_RETENTION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"retention_days must be between {MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}",
        )
    saved = await archive_purge_service.set_retention_days(db, body.retention_days)
    return ArchiveTrashSettings(retention_days=saved)
