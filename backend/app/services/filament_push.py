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
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.local_preset import LocalPreset
from backend.app.models.user_filament import UserFilamentPreset
from backend.app.services.filament_authoring import AuthoringError

logger = logging.getLogger(__name__)

PUSH_CAPABLE = {"bambu": True, "orca": True}


async def _build_bambu_cloud(db: AsyncSession, user):
    """Patch-point for tests (same delegation as filament_preset_sync)."""
    from backend.app.services.filament_preset_sync import _build_bambu_cloud as build

    return await build(db, user)


async def _build_orca_cloud(db: AsyncSession, user):
    """Patch-point for tests. Returns a token-hydrated, refreshed-if-needed
    OrcaCloudService, or None when no pairing is stored."""
    from fastapi import HTTPException

    from backend.app.api.routes.orca_cloud import _build_authenticated_service

    try:
        return await _build_authenticated_service(db, user)
    except HTTPException:
        return None


def _preset_blob(preset, row, filament_id: str) -> tuple[str, dict]:
    """(name, content) for one family preset — the same blob both clouds get:
    the local preset's setting with the family id injected."""
    name = preset.name if preset else row.name
    try:
        content = json.loads(preset.setting) if preset else {}
    except ValueError:
        content = {}
    if not isinstance(content, dict):
        content = {}
    content["filament_id"] = filament_id
    # The orca sync's incoming filter keys on this marker; a slicer-side
    # profile carries it anyway, so inject only when absent.
    content.setdefault("type", "filament")
    return name, content


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
    if ecosystem == "orca":
        return await _push_family_orca(db, rows, filament_id, user)
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


async def _push_family_orca(db: AsyncSession, rows, filament_id: str, user) -> list[dict]:
    """Orca leg of the family push. The anchor (``orca_pushed_updated_time``,
    the server timestamp from OUR last push) is the conflict detector: a cloud
    copy whose pull timestamp differs was edited over there, and the row comes
    back as ``status="conflict"`` WITHOUT a network push — resolution is the
    user's explicit call (``resolve_push_conflict``)."""
    from backend.app.services.orca_cloud import OrcaCloudConflict

    svc = await _build_orca_cloud(db, user)
    if svc is None:
        raise AuthoringError("Orca Cloud is not connected")
    results: list[dict] = []
    try:
        profiles = {str(p.get("id")): p for p in await svc.list_profiles() if p.get("id")}
        for row in rows:
            preset = await db.get(LocalPreset, row.local_preset_id) if row.local_preset_id else None
            name, content = _preset_blob(preset, row, filament_id)
            try:
                if row.orca_pushed_profile_id and row.orca_pushed_profile_id not in profiles:
                    # Deleted in the cloud — recreate under a FRESH id below
                    # (the old one is a tombstone for 30 days).
                    row.orca_pushed_profile_id = None
                    row.orca_pushed_updated_time = None
                if row.orca_pushed_profile_id and not row.orca_push_dirty:
                    results.append(
                        {"name": name, "status": "up_to_date", "profile_id": row.orca_pushed_profile_id, "detail": None}
                    )
                    continue
                if row.orca_pushed_profile_id:
                    server_time = profiles[row.orca_pushed_profile_id].get("updated_time")
                    if server_time != row.orca_pushed_updated_time:
                        results.append(
                            {
                                "name": name,
                                "status": "conflict",
                                "profile_id": row.orca_pushed_profile_id,
                                "server_updated_time": server_time,
                                "row_id": row.id,
                                "detail": None,
                            }
                        )
                        continue
                    meta = await svc.push_profile(
                        profile_id=row.orca_pushed_profile_id,
                        name=name,
                        content=content,
                        original_updated_time=row.orca_pushed_updated_time,
                    )
                    status = "updated"
                else:
                    profile_id = str(uuid.uuid4())
                    try:
                        meta = await svc.push_profile(profile_id=profile_id, name=name, content=content)
                    except OrcaCloudConflict as c:
                        if c.code != -3:  # anything but a tombstone collision is real
                            raise
                        profile_id = str(uuid.uuid4())
                        meta = await svc.push_profile(profile_id=profile_id, name=name, content=content)
                    row.orca_pushed_profile_id = profile_id
                    status = "pushed"
                row.orca_pushed_updated_time = meta.get("updated_time")
                row.orca_pushed_at = datetime.now(timezone.utc)
                row.orca_push_dirty = False
                results.append(
                    {"name": name, "status": status, "profile_id": row.orca_pushed_profile_id, "detail": None}
                )
            except Exception as e:  # noqa: BLE001 — best-effort per preset (spec §5)
                logger.info("orca push of %s failed: %s", name, e)
                results.append({"name": name, "status": "error", "profile_id": None, "detail": str(e)})
    finally:
        await svc.close()
    await db.commit()
    return results


async def resolve_push_conflict(db: AsyncSession, *, row_id: int, action: str, user=None) -> dict:
    """The two explicit answers to an Orca push conflict (user's call,
    2026-08-24): ``force`` overwrites the cloud copy with ours;``adopt``
    takes the cloud content into the LocalPreset. Adopt honestly marks the
    Bambu copy dirty — its cloud twin now lags the content it mirrors."""
    row = await db.get(UserFilamentPreset, row_id)
    if row is None or not row.orca_pushed_profile_id:
        raise AuthoringError("unknown preset row or nothing pushed to Orca Cloud")
    preset = await db.get(LocalPreset, row.local_preset_id) if row.local_preset_id else None
    svc = await _build_orca_cloud(db, user)
    if svc is None:
        raise AuthoringError("Orca Cloud is not connected")
    try:
        if action == "force":
            name, content = _preset_blob(preset, row, row.family_filament_id or "")
            meta = await svc.force_push_profile(profile_id=row.orca_pushed_profile_id, name=name, content=content)
            row.orca_pushed_updated_time = meta.get("updated_time")
            row.orca_pushed_at = datetime.now(timezone.utc)
            row.orca_push_dirty = False
            await db.commit()
            return {"status": "overwritten", "profile_id": row.orca_pushed_profile_id}
        if action == "adopt":
            profiles = {str(p.get("id")): p for p in await svc.list_profiles() if p.get("id")}
            profile = profiles.get(row.orca_pushed_profile_id)
            if profile is None:
                raise AuthoringError("the cloud profile is gone — push the family again instead")
            content = profile.get("content")
            if not isinstance(content, dict):
                raise AuthoringError("the cloud profile carries no usable content")
            if preset is not None:
                preset.setting = json.dumps(content)
                from backend.app.services.filament_preset_sync import absorb_local_preset

                await absorb_local_preset(db, preset)
            row.orca_pushed_updated_time = profile.get("updated_time")
            row.orca_push_dirty = False
            if row.pushed_cloud_id:
                row.push_dirty = True  # the Bambu copy now lags — say so honestly
            await db.commit()
            return {"status": "adopted", "profile_id": row.orca_pushed_profile_id}
        raise AuthoringError(f"unknown conflict action {action!r}")
    finally:
        await svc.close()


async def push_blobs(db: AsyncSession, *, blobs: list[dict], user=None, ecosystem: str = "bambu") -> list[dict]:
    """Push freshly-authored content straight to the cloud (cloud-only
    creation — no LocalPreset exists). No pushed_cloud_id bookkeeping either:
    the sync mirrors these back as ordinary cloud presets, which is exactly
    what a cloud-born preset is."""
    if not PUSH_CAPABLE.get(ecosystem):
        raise AuthoringError(f"push to {ecosystem} is not available yet")
    if not blobs:
        raise AuthoringError("nothing to push")
    if ecosystem == "orca":
        return await _push_blobs_orca(db, blobs, user)
    cloud = await _build_bambu_cloud(db, user)
    if cloud is None or not cloud.is_authenticated:
        if cloud is not None:
            await cloud.close()
        raise AuthoringError("Bambu Cloud is not connected")
    results: list[dict] = []
    try:
        for blob in blobs:
            name = blob.get("name") or ""
            try:
                data = await cloud.create_setting("filament", name, "", blob)  # roots have no base
                results.append(
                    {"name": name, "status": "pushed", "setting_id": _extract_setting_id(data), "detail": None}
                )
            except Exception as e:  # noqa: BLE001 — best-effort per preset (spec §5)
                logger.info("push of %s failed: %s", name, e)
                results.append({"name": name, "status": "error", "setting_id": None, "detail": str(e)})
    finally:
        await cloud.close()
    return results


async def _push_blobs_orca(db: AsyncSession, blobs: list[dict], user) -> list[dict]:
    """Cloud-only creation, Orca flavour: mint a uuid per blob, no
    bookkeeping — the sync mirrors these back as ordinary cloud profiles."""
    svc = await _build_orca_cloud(db, user)
    if svc is None:
        raise AuthoringError("Orca Cloud is not connected")
    results: list[dict] = []
    try:
        for blob in blobs:
            name = blob.get("name") or ""
            try:
                meta = await svc.push_profile(profile_id=str(uuid.uuid4()), name=name, content=blob)
                results.append({"name": name, "status": "pushed", "profile_id": meta.get("id"), "detail": None})
            except Exception as e:  # noqa: BLE001 — best-effort per preset (spec §5)
                logger.info("orca push of %s failed: %s", name, e)
                results.append({"name": name, "status": "error", "profile_id": None, "detail": str(e)})
    finally:
        await svc.close()
    return results
