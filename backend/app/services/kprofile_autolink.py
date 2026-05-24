"""Auto-link engine: attach printer K-profiles to spools by filament_id.

Pure functions reused by triggers (spool save, calibration sync), the manual
relink endpoint, and the migration's runtime resolver. "B-simple" rule: the
engine only ever creates/deletes rows with ``auto_linked=True`` — manual
PA-tab links (``auto_linked=False``) are never touched and take precedence.

Matching is keyed on the spool's resolved filament_id across ALL printers and
ALL nozzles/extruders, one calibration per
``(printer, nozzle_diameter, nozzle_volume_type, extruder)`` combo, picked
active-first then newest (mirrors the sync ordering in calibration_service).
The chosen calibration is auto-activated when its combo has no active row yet.
"""

import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.filament_calibration import FilamentCalibration

logger = logging.getLogger(__name__)


async def select_matching_calibrations(
    *, db: AsyncSession, printer_id: int, filament_id: str
) -> list[FilamentCalibration]:
    """One calibration per ``(nozzle_diameter, vol_type, extruder)`` combo for
    the given printer + filament_id, picked active-first then newest."""
    rows = (
        (
            await db.execute(
                select(FilamentCalibration)
                .where(
                    FilamentCalibration.printer_id == printer_id,
                    FilamentCalibration.filament_id == filament_id,
                )
                .order_by(FilamentCalibration.is_active.desc(), FilamentCalibration.id.desc())
            )
        )
        .scalars()
        .all()
    )
    seen: set[tuple] = set()
    picked: list[FilamentCalibration] = []
    for r in rows:
        combo = (r.nozzle_diameter, r.nozzle_volume_type, r.extruder_id)
        if combo in seen:
            continue
        seen.add(combo)
        picked.append(r)
    return picked


async def _activate_if_none_active(*, db: AsyncSession, fc: FilamentCalibration) -> None:
    """Set ``fc.is_active=True`` only when its combo has no active row yet."""
    if fc.is_active:
        return
    has_active = (
        await db.execute(
            select(FilamentCalibration.id)
            .where(
                FilamentCalibration.printer_id == fc.printer_id,
                FilamentCalibration.filament_id == fc.filament_id,
                FilamentCalibration.nozzle_diameter == fc.nozzle_diameter,
                FilamentCalibration.nozzle_volume_type == fc.nozzle_volume_type,
                FilamentCalibration.extruder_id == fc.extruder_id,
                FilamentCalibration.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_active is None:
        fc.is_active = True


async def _apply_links(
    *,
    db: AsyncSession,
    existing_rows: list,
    filament_id: str | None,
    make_row: Callable[[FilamentCalibration], object],
) -> int:
    """Reconcile ``auto_linked`` rows against the calibrations that match
    ``filament_id`` across all printers. Shared by the local + Spoolman paths.

    ``existing_rows`` are the current link rows for this spool (each exposing
    ``auto_linked``, ``printer_id``, ``extruder``, ``filament_calibration``,
    ``filament_calibration_id``). ``make_row(fc)`` builds a new auto link.
    Returns the number of desired auto links after reconciliation.
    """
    manual_combos: set[tuple] = set()
    auto_rows: list = []
    for ln in existing_rows:
        fc = ln.filament_calibration
        if fc is None:
            continue
        combo = (ln.printer_id, fc.nozzle_diameter, fc.nozzle_volume_type, ln.extruder)
        if ln.auto_linked:
            auto_rows.append(ln)
        else:
            manual_combos.add(combo)

    desired: dict[tuple, FilamentCalibration] = {}
    fid = (filament_id or "").strip()
    if fid:
        from backend.app.models.printer import Printer

        printer_ids = (await db.execute(select(Printer.id))).scalars().all()
        for pid in printer_ids:
            for fc in await select_matching_calibrations(db=db, printer_id=pid, filament_id=fid):
                combo = (pid, fc.nozzle_diameter, fc.nozzle_volume_type, fc.extruder_id)
                if combo in manual_combos:
                    continue  # manual wins
                desired[combo] = fc

    # Repoint / remove existing auto rows.
    kept: set[tuple] = set()
    for ln in auto_rows:
        fc = ln.filament_calibration
        combo = (ln.printer_id, fc.nozzle_diameter, fc.nozzle_volume_type, ln.extruder) if fc else None
        if combo in desired:
            if desired[combo].id != ln.filament_calibration_id:
                ln.filament_calibration_id = desired[combo].id  # repoint to newest/active
            kept.add(combo)
        else:
            await db.delete(ln)

    # Create missing auto rows + auto-activate the chosen calibration.
    for combo, fc in desired.items():
        if combo not in kept:
            db.add(make_row(fc))
        await _activate_if_none_active(db=db, fc=fc)

    return len(desired)


async def autolink_spool(*, db: AsyncSession, spool) -> int:
    """Maintain ``auto_linked`` SpoolKProfile rows for one local spool across
    all printers. Returns the number of desired auto links."""
    from backend.app.models.spool_k_profile import SpoolKProfile

    existing = (await db.execute(select(SpoolKProfile).where(SpoolKProfile.spool_id == spool.id))).scalars().all()

    def _make(fc: FilamentCalibration) -> SpoolKProfile:
        return SpoolKProfile(
            spool_id=spool.id,
            printer_id=fc.printer_id,
            extruder=fc.extruder_id,
            filament_calibration_id=fc.id,
            auto_linked=True,
        )

    return await _apply_links(
        db=db, existing_rows=list(existing), filament_id=spool.resolved_filament_id, make_row=_make
    )


async def autolink_spoolman_spool(*, db: AsyncSession, spoolman_spool_id: int, resolved_filament_id: str | None) -> int:
    """Maintain ``auto_linked`` SpoolmanKProfile rows for one Spoolman spool."""
    from backend.app.models.spoolman_k_profile import SpoolmanKProfile

    existing = (
        (await db.execute(select(SpoolmanKProfile).where(SpoolmanKProfile.spoolman_spool_id == spoolman_spool_id)))
        .scalars()
        .all()
    )

    def _make(fc: FilamentCalibration) -> SpoolmanKProfile:
        return SpoolmanKProfile(
            spoolman_spool_id=spoolman_spool_id,
            printer_id=fc.printer_id,
            extruder=fc.extruder_id,
            filament_calibration_id=fc.id,
            auto_linked=True,
        )

    return await _apply_links(db=db, existing_rows=list(existing), filament_id=resolved_filament_id, make_row=_make)


async def propagate_calibration_to_spools(*, db: AsyncSession, printer_id: int, filament_ids: set[str]) -> None:
    """Re-link all local spools whose ``resolved_filament_id`` is in
    ``filament_ids`` (called after a printer's K-profiles sync).

    ``printer_id`` is accepted for symmetry / future scoping; matching already
    spans all printers via :func:`autolink_spool`. Spoolman spools are handled
    where their resolved id is stored (see the spool-save / Spoolman path).
    """
    from backend.app.models.spool import Spool

    fids = {f for f in filament_ids if f}
    if not fids:
        return
    spools = (await db.execute(select(Spool).where(Spool.resolved_filament_id.in_(fids)))).scalars().all()
    for sp in spools:
        await autolink_spool(db=db, spool=sp)
