"""Per-(user, printer-model) saved PrintModal toggles.

Two surfaces:

* **Per-user** (``GET / PUT /print-option-preferences/{model}``) — used
  by the PrintModal itself. Always scoped to ``current_user.id``; the
  URL carries only the printer model.
* **Admin** (``/print-option-preferences/admin/...``) — used by the
  Settings → Print → Saved Profiles widget. Lists every saved
  preference across all users, lets an admin add/edit/delete on
  behalf of any user, and copy a preference from one user to another.
  Gated on ``USERS_READ`` for read and ``USERS_UPDATE`` for writes
  (admin-grade ops on other users' data).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.models.user import User
from backend.app.schemas.print_options_preference import (
    PrintOptionsPreferenceAdminEntry,
    PrintOptionsPreferenceCopy,
    PrintOptionsPreferenceData,
    PrintOptionsPreferenceResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/print-option-preferences", tags=["print-option-preferences"])


@router.get("/{printer_model}", response_model=PrintOptionsPreferenceResponse)
async def get_preference(
    printer_model: str,
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
    db: AsyncSession = Depends(get_db),
):
    """Return the saved preference for ``(current_user, printer_model)``.

    404 if no preference has been saved yet — the modal then falls back
    to its built-in defaults.
    """
    if current_user is None:
        # Auth disabled (shouldn't happen post-0.4.0 — auth is always on),
        # but handle gracefully so the modal doesn't 500.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No preference saved")

    result = await db.execute(
        select(PrintOptionsPreference)
        .where(PrintOptionsPreference.user_id == current_user.id)
        .where(PrintOptionsPreference.printer_model == printer_model)
    )
    pref = result.scalar_one_or_none()
    if pref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No preference saved")
    return pref


@router.put("/{printer_model}", response_model=PrintOptionsPreferenceResponse)
async def upsert_preference(
    printer_model: str,
    data: PrintOptionsPreferenceData,
    current_user: User | None = RequirePermission(Permission.QUEUE_CREATE),
    db: AsyncSession = Depends(get_db),
):
    """Insert or update the preference row for ``(current_user, printer_model)``."""
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    return await _upsert_preference(db, current_user.id, printer_model, data)


async def _upsert_preference(
    db: AsyncSession,
    user_id: int,
    printer_model: str,
    data: PrintOptionsPreferenceData,
) -> PrintOptionsPreference:
    """Shared upsert helper used by both the self-PUT and admin-PUT routes."""
    result = await db.execute(
        select(PrintOptionsPreference)
        .where(PrintOptionsPreference.user_id == user_id)
        .where(PrintOptionsPreference.printer_model == printer_model)
    )
    pref = result.scalar_one_or_none()

    payload = data.model_dump()
    if pref is None:
        pref = PrintOptionsPreference(
            user_id=user_id,
            printer_model=printer_model,
            options=payload,
        )
        db.add(pref)
    else:
        pref.options = payload
    await db.commit()
    await db.refresh(pref)
    return pref


# ─────────────────────── Admin ops (Settings widget) ───────────────────────


@router.get("/admin/list", response_model=list[PrintOptionsPreferenceAdminEntry])
async def admin_list_all_preferences(
    _: User | None = RequirePermission(Permission.USERS_READ),
    db: AsyncSession = Depends(get_db),
):
    """List every saved preference across all users, with username attached.

    Powers the Settings → Print → Saved Profiles widget. Read-only.
    """
    result = await db.execute(
        select(PrintOptionsPreference, User.username)
        .join(User, User.id == PrintOptionsPreference.user_id)
        .order_by(User.username, PrintOptionsPreference.printer_model)
    )
    entries: list[PrintOptionsPreferenceAdminEntry] = []
    for pref, username in result.all():
        entries.append(
            PrintOptionsPreferenceAdminEntry(
                user_id=pref.user_id,
                username=username,
                printer_model=pref.printer_model,
                options=pref.options,
                updated_at=pref.updated_at,
            )
        )
    return entries


@router.put(
    "/admin/{user_id}/{printer_model}",
    response_model=PrintOptionsPreferenceResponse,
)
async def admin_upsert_preference(
    user_id: int,
    printer_model: str,
    data: PrintOptionsPreferenceData,
    _: User | None = RequirePermission(Permission.USERS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Insert/update a preference on behalf of any user. Admin only."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found")
    return await _upsert_preference(db, user_id, printer_model, data)


@router.delete("/admin/{user_id}/{printer_model}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_preference(
    user_id: int,
    printer_model: str,
    _: User | None = RequirePermission(Permission.USERS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Delete the preference for ``(user_id, printer_model)``.

    404 when the row doesn't exist — caller can treat as already-gone.
    """
    result = await db.execute(
        select(PrintOptionsPreference)
        .where(PrintOptionsPreference.user_id == user_id)
        .where(PrintOptionsPreference.printer_model == printer_model)
    )
    pref = result.scalar_one_or_none()
    if pref is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preference not found")
    await db.delete(pref)
    await db.commit()


@router.post("/admin/copy", response_model=PrintOptionsPreferenceResponse)
async def admin_copy_preference(
    body: PrintOptionsPreferenceCopy,
    _: User | None = RequirePermission(Permission.USERS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Copy a preference's options blob from one user (+model) to another.

    Defaults the destination model to the source's model so the common
    case ("give operator B the same toggles operator A uses for their P1S")
    is one POST without repeating the model. The destination row is
    upserted — if the target user already has a preference for that
    model, it gets overwritten with the source payload.
    """
    src_result = await db.execute(
        select(PrintOptionsPreference)
        .where(PrintOptionsPreference.user_id == body.src_user_id)
        .where(PrintOptionsPreference.printer_model == body.src_printer_model)
    )
    src = src_result.scalar_one_or_none()
    if src is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source preference not found")

    dst_user = await db.get(User, body.dst_user_id)
    if dst_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination user not found")

    dst_model = body.dst_printer_model or body.src_printer_model
    # The shared helper already does upsert + commit + refresh.
    data = PrintOptionsPreferenceData.model_validate(src.options)
    return await _upsert_preference(db, body.dst_user_id, dst_model, data)
