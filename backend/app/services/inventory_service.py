"""Spool list/update logic extracted from `api/routes/inventory.py` (behavior-preserving)
so it can be called directly — not only through HTTP — by the cloud portal's remote-op
registry (see docs/superpowers/sdd/2026-08-28-cloud-portal-phase2-remote-inventory-agent).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.app.models.spool import Spool
from backend.app.schemas.spool import SpoolUpdate
from backend.app.services.location_service import prepare_internal_spool_payload


class SpoolNotFoundError(Exception):
    """No spool with the given id."""


async def list_spools(db: AsyncSession, *, include_archived: bool = False) -> list[Spool]:
    """List all spools, excluding archived by default."""
    query = select(Spool).options(selectinload(Spool.k_profiles))
    if not include_archived:
        query = query.where(Spool.archived_at.is_(None))
    query = query.order_by(Spool.material, Spool.brand, Spool.color_name)
    result = await db.execute(query)
    return list(result.scalars().all())


async def update_spool(db: AsyncSession, spool_id: int, spool_data: SpoolUpdate) -> Spool:
    """Update a spool.

    Raises SpoolNotFoundError when spool_id doesn't exist. Lets
    prepare_internal_spool_payload's ValueError propagate — the caller maps both
    to the appropriate response (route -> HTTP 404 / 400).
    """
    # Lazy import: _validate_family_id/_safe_autolink have other callers still in
    # inventory.py, and that module imports this one at module level to call
    # list_spools/update_spool — a top-level import here would be circular.
    from backend.app.api.routes.inventory import _safe_autolink, _validate_family_id

    result = await db.execute(select(Spool).where(Spool.id == spool_id))
    spool = result.scalar_one_or_none()
    if not spool:
        raise SpoolNotFoundError(spool_id)

    update_data = spool_data.model_dump(exclude_unset=True)
    update_data = await prepare_internal_spool_payload(db, update_data, set(spool_data.model_fields_set))
    # Auto-lock weight when user explicitly sets weight_used
    if "weight_used" in update_data and "weight_locked" not in update_data:
        update_data["weight_locked"] = True

    await _validate_family_id(db, update_data.get("filament_family_id"))
    for field, value in update_data.items():
        setattr(spool, field, value)

    await db.commit()
    # Re-link when the family / resolved filament_id changed (or on any save —
    # cheap and keeps links current with the spool's current preset).
    if "filament_family_id" in update_data or "slicer_filament" in update_data:
        await _safe_autolink(db, spool)
    result = await db.execute(select(Spool).options(selectinload(Spool.k_profiles)).where(Spool.id == spool_id))
    return result.scalar_one()
