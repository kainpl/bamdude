"""Server-only mirrors of PRIVATE cloud filament presets (spec A §3). The
browser never talks to the clouds (CSP) and the server owns the tokens, so
this loop is the sole writer of user_filament_presets rows with
source='cloud_*'.

Asymmetric resolution, per the verified contracts (vault
60-specs/bs-filament-preset-system §11):
- Bambu's LISTING is pre-enriched — filament_id comes straight off the row.
- Orca enriches nothing — a root carries filament_id in content, a child
  resolves by walking its `inherits` NAME against the orca system catalog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.user import User
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.utils import filament_catalog as catalog

logger = logging.getLogger(__name__)

SYNC_INTERVAL_S = 300  # 5 min (spec A §3)


@dataclass
class SyncOutcome:
    ok: bool
    upserted: int = 0
    deleted: int = 0
    families_added: int = 0
    detail: str = ""


async def _build_bambu_cloud(db: AsyncSession, user: User | None):
    """Patch-point for tests. Delegates to routes/cloud.py::build_authenticated_cloud."""
    from backend.app.api.routes.cloud import build_authenticated_cloud

    return await build_authenticated_cloud(db, user)


async def build_authenticated_service(db: AsyncSession, user: User | None):
    """Patch-point for tests; the ONE shared Orca refresh path (Task 7's goal).

    Delegates to the route-layer builder so the JIT-refreshed, rotated token
    pair is always persisted through the same code the routes use — Orca's
    refresh tokens are one-shot outside a ~60s grace window, so a second
    persistence path would eventually revoke the pairing.
    """
    from backend.app.api.routes.orca_cloud import _build_authenticated_service

    return await _build_authenticated_service(db, user)


def _alias_of(name: str) -> str:
    return name.split("@")[0].strip() if "@" in name else name.strip()


def _scalar(value):
    if isinstance(value, list):
        value = value[0] if value else None
    return value


def _int_or_none(value):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


async def _upsert_family(
    db: AsyncSession,
    *,
    filament_id: str,
    ecosystem: str,
    name: str,
    vendor: str | None,
    filament_type: str | None,
    origin: str,
) -> bool:
    """Ensure a custom-family row exists for a P-hash the system catalog lacks.
    Returns True when a new row was added."""
    if catalog.get_family(filament_id):
        return False
    existing = (
        (
            await db.execute(
                select(UserFilamentFamily).where(
                    UserFilamentFamily.ecosystem == ecosystem,
                    UserFilamentFamily.filament_id == filament_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing:
        existing.orphaned = False
        return False
    db.add(
        UserFilamentFamily(
            filament_id=filament_id,
            ecosystem=ecosystem,
            alias=_alias_of(name),
            vendor=vendor,
            filament_type=filament_type,
            origin=origin,
        )
    )
    return True


async def _reconcile(
    db: AsyncSession,
    *,
    owner_id: int | None,
    ecosystem: str,
    source: str,
    incoming: dict[str, dict],
) -> SyncOutcome:
    """incoming: cloud_id -> column dict. Upserts + deletes + family upkeep."""
    outcome = SyncOutcome(ok=True)
    existing_rows = (
        (
            await db.execute(
                select(UserFilamentPreset).where(
                    UserFilamentPreset.owner_user_id == owner_id,
                    UserFilamentPreset.ecosystem == ecosystem,
                    UserFilamentPreset.source == source,
                )
            )
        )
        .scalars()
        .all()
    )
    existing = {r.cloud_id: r for r in existing_rows}

    for cloud_id, cols in incoming.items():
        row = existing.get(cloud_id)
        if row is None:
            db.add(
                UserFilamentPreset(
                    owner_user_id=owner_id, ecosystem=ecosystem, source=source, cloud_id=cloud_id, **cols
                )
            )
        else:
            for key, value in cols.items():
                setattr(row, key, value)
        outcome.upserted += 1
        fid = cols.get("family_filament_id")
        if fid and await _upsert_family(
            db,
            filament_id=fid,
            ecosystem=ecosystem,
            name=cols["name"],
            vendor=cols.get("vendor"),
            filament_type=cols.get("filament_type"),
            origin=source,
        ):
            outcome.families_added += 1

    vanished = set(existing) - set(incoming)
    for cloud_id in vanished:
        await db.delete(existing[cloud_id])
        outcome.deleted += 1

    if vanished:
        # A family nobody mirrors anymore is marked orphaned, never deleted —
        # spools / filament_calibration rows may still reference the id.
        remaining_fids = {c.get("family_filament_id") for c in incoming.values()}
        user_fams = (
            (await db.execute(select(UserFilamentFamily).where(UserFilamentFamily.ecosystem == ecosystem)))
            .scalars()
            .all()
        )
        for fam in user_fams:
            if fam.origin == source and fam.filament_id not in remaining_fids:
                fam.orphaned = True

    await db.commit()
    return outcome


async def sync_bambu_presets_for_user(db: AsyncSession, user: User | None) -> SyncOutcome:
    cloud = await _build_bambu_cloud(db, user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        return SyncOutcome(ok=False, detail="bambu cloud not connected")
    try:
        listing = await cloud.get_slicer_settings()
    except Exception as e:  # noqa: BLE001 — sync must never crash the loop
        logger.info("bambu preset sync failed: %s", e)
        return SyncOutcome(ok=False, detail=str(e))
    finally:
        await cloud.close()

    private = ((listing.get("filament") or {}).get("private")) or []
    incoming: dict[str, dict] = {}
    for row in private:
        sid = row.get("setting_id")
        if not sid:
            continue
        temps = row.get("nozzle_temperature") or [None, None]
        incoming[sid] = {
            "name": row.get("name") or "",
            "family_filament_id": row.get("filament_id"),  # pre-resolved by the cloud
            "base_ref": row.get("base_id"),
            "vendor": row.get("filament_vendor"),
            "filament_type": row.get("filament_type"),
            "nozzle_temp_min": temps[0] if len(temps) > 0 else None,
            "nozzle_temp_max": temps[1] if len(temps) > 1 else None,
            "updated_time": row.get("update_time"),
        }
    owner_id = user.id if user is not None else None
    return await _reconcile(db, owner_id=owner_id, ecosystem="bambu", source="cloud_bambu", incoming=incoming)


def _orca_family_id(content: dict) -> str | None:
    fid = content.get("filament_id")
    if fid:
        return str(fid)
    inherits = content.get("inherits") or ""
    if inherits:
        preset = catalog.preset_by_name(str(inherits), "orca") or catalog.preset_by_name(str(inherits), "bambu")
        if preset:
            return preset.filament_id
    return None


async def sync_orca_presets_for_user(db: AsyncSession, user: User | None) -> SyncOutcome:
    try:
        svc = await build_authenticated_service(db, user)
    except Exception as e:  # noqa: BLE001 — includes HTTPException("not connected")
        return SyncOutcome(ok=False, detail=str(e))
    try:
        profiles = await svc.list_profiles()
    except Exception as e:  # noqa: BLE001
        logger.info("orca preset sync failed: %s", e)
        return SyncOutcome(ok=False, detail=str(e))
    finally:
        await svc.close()

    incoming: dict[str, dict] = {}
    for profile in profiles:
        content = profile.get("content") or {}
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = {}
        if (content.get("type") or "") != "filament":
            continue
        pid = str(profile.get("id") or "")
        if not pid:
            continue
        incoming[pid] = {
            "name": profile.get("name") or content.get("name") or "",
            "family_filament_id": _orca_family_id(content),
            "base_ref": content.get("inherits") or None,
            "vendor": _scalar(content.get("filament_vendor")),
            "filament_type": _scalar(content.get("filament_type")),
            "nozzle_temp_min": _int_or_none(_scalar(content.get("nozzle_temperature_range_low"))),
            "nozzle_temp_max": _int_or_none(_scalar(content.get("nozzle_temperature_range_high"))),
            "updated_time": profile.get("updated_time"),
        }
    owner_id = user.id if user is not None else None
    return await _reconcile(db, owner_id=owner_id, ecosystem="orca", source="cloud_orca", incoming=incoming)


# ---------------------------------------------------------------------------
# Local preset absorption (spec A §3): identity link rows for LocalPresets,
# written on their own CRUD path, not by the cloud loop.
# ---------------------------------------------------------------------------


async def absorb_local_preset(db: AsyncSession, preset) -> None:
    """Upsert the identity link row for one filament LocalPreset."""
    if preset.preset_type != "filament":
        return
    try:
        content = json.loads(preset.setting or "{}")
    except ValueError:
        content = {}
    fid = content.get("filament_id") or _orca_family_id(content)
    existing = (
        (await db.execute(select(UserFilamentPreset).where(UserFilamentPreset.local_preset_id == preset.id)))
        .scalars()
        .first()
    )
    cols = {
        "name": preset.name,
        "family_filament_id": str(fid) if fid else None,
        "base_ref": content.get("inherits") or None,
        "vendor": _scalar(content.get("filament_vendor")) or preset.filament_vendor,
        "filament_type": _scalar(content.get("filament_type")) or preset.filament_type,
        "nozzle_temp_min": preset.nozzle_temp_min,
        "nozzle_temp_max": preset.nozzle_temp_max,
    }
    if existing is None:
        db.add(
            UserFilamentPreset(
                owner_user_id=None,
                ecosystem="orca",
                source="local",
                cloud_id=None,
                local_preset_id=preset.id,
                **cols,
            )
        )
    else:
        for key, value in cols.items():
            setattr(existing, key, value)
    if fid:
        await _upsert_family(
            db,
            filament_id=str(fid),
            ecosystem="orca",
            name=preset.name,
            vendor=cols["vendor"],
            filament_type=cols["filament_type"],
            origin="local",
        )


async def drop_local_preset_row(db: AsyncSession, preset_id: int) -> None:
    row = (
        (await db.execute(select(UserFilamentPreset).where(UserFilamentPreset.local_preset_id == preset_id)))
        .scalars()
        .first()
    )
    if row is not None:
        await db.delete(row)


async def absorb_all_local_presets(db: AsyncSession) -> int:
    """Startup pass: absorb every pre-existing filament LocalPreset."""
    from backend.app.models.local_preset import LocalPreset

    presets = (await db.execute(select(LocalPreset).where(LocalPreset.preset_type == "filament"))).scalars().all()
    count = 0
    for preset in presets:
        await absorb_local_preset(db, preset)
        count += 1
    return count


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

_sync_wakeup = asyncio.Event()


def request_sync_soon() -> None:
    """On-demand trigger (slot dialog / spool form / manual button / connect)."""
    _sync_wakeup.set()


async def _sync_all_connected() -> None:
    from backend.app.core import database

    async with database.async_session() as db:
        users = (
            (
                await db.execute(
                    select(User).where((User.cloud_token.is_not(None)) | (User.orca_cloud_token.is_not(None)))
                )
            )
            .scalars()
            .all()
        )
        targets: list[User | None] = list(users) or [None]  # auth-disabled global scope
        for user in targets:
            if user is None or user.cloud_token:
                await sync_bambu_presets_for_user(db, user)
            if user is None or getattr(user, "orca_cloud_token", None):
                await sync_orca_presets_for_user(db, user)


async def filament_preset_sync_loop() -> None:
    """Lifespan task: every SYNC_INTERVAL_S, or sooner when poked."""
    while True:
        try:
            await asyncio.wait_for(_sync_wakeup.wait(), timeout=SYNC_INTERVAL_S)
        except TimeoutError:
            pass
        _sync_wakeup.clear()
        try:
            await _sync_all_connected()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("filament preset sync tick failed")
