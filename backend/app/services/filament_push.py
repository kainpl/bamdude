"""Cloud push of authored families (spec B §5). One interface per
ecosystem; Bambu ships active, Orca is designed-inactive (blocked on the
external write-scope / re-pairing / own-client_id dependency — flipping
the capability map activates the leg once it clears, no redesign).

Push publishes COPIES — the family stays canonical in BamDude. No
automatic re-push: an edit sets push_dirty and waits for an explicit
"Re-push", so BamDude never fights edits made in BS.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.local_preset import LocalPreset
from backend.app.models.user_filament import UserFilamentPreset
from backend.app.services.filament_authoring import AuthoringError

logger = logging.getLogger(__name__)

PUSH_CAPABLE = {"bambu": True, "orca": False}


async def _build_bambu_cloud(db: AsyncSession, user):
    """Patch-point for tests (same delegation as filament_preset_sync)."""
    from backend.app.services.filament_preset_sync import _build_bambu_cloud as build

    return await build(db, user)


def _extract_setting_id(data) -> str | None:
    if not isinstance(data, dict):
        return None
    return data.get("setting_id") or (data.get("data") or {}).get("setting_id")


async def push_family(db: AsyncSession, *, filament_id: str, ecosystem: str = "bambu", user=None) -> list[dict]:
    if not PUSH_CAPABLE.get(ecosystem):
        raise AuthoringError(f"push to {ecosystem} is not available yet")
    rows = (
        (
            await db.execute(
                select(UserFilamentPreset).where(
                    UserFilamentPreset.family_filament_id == filament_id,
                    UserFilamentPreset.source == "local",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise AuthoringError("family has no local presets to push")
    cloud = await _build_bambu_cloud(db, user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise AuthoringError("Bambu Cloud is not connected")
    results: list[dict] = []
    try:
        for row in rows:
            preset = await db.get(LocalPreset, row.local_preset_id) if row.local_preset_id else None
            name = preset.name if preset else row.name
            try:
                content = json.loads(preset.setting) if preset else {}
            except ValueError:
                content = {}
            content["filament_id"] = filament_id  # the payload always carries the family
            try:
                if row.pushed_cloud_id and not row.push_dirty:
                    results.append(
                        {"name": name, "status": "up_to_date", "setting_id": row.pushed_cloud_id, "detail": None}
                    )
                    continue
                if row.pushed_cloud_id:
                    data = await cloud.update_setting(row.pushed_cloud_id, name=name, setting=content)
                    # Bambu "update" is delete+recreate — a NEW id comes back.
                    row.pushed_cloud_id = _extract_setting_id(data) or row.pushed_cloud_id
                    status = "updated"
                else:
                    data = await cloud.create_setting("filament", name, "", content)  # roots have no base
                    row.pushed_cloud_id = _extract_setting_id(data)
                    status = "pushed"
                row.pushed_at = datetime.now(timezone.utc)
                row.push_dirty = False
                results.append({"name": name, "status": status, "setting_id": row.pushed_cloud_id, "detail": None})
            except Exception as e:  # noqa: BLE001 — best-effort per preset (spec §5)
                logger.info("push of %s failed: %s", name, e)
                results.append({"name": name, "status": "error", "setting_id": None, "detail": str(e)})
    finally:
        await cloud.close()
    await db.commit()
    return results
