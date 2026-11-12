"""Immutable cloud credential snapshots and final, caller-owned publication fences.

Cloud generations belong to a provider credential, independently of browser sessions.
A completed network call is never permission to overwrite a newer credential or cache.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from backend.app.core.auth_lifetime import AccountCapture, guard_accounts, validate_captured
from backend.app.models.settings import Settings
from backend.app.models.user import User

_open = True
_epoch = uuid.uuid4().hex


def close_admission() -> None:
    """Restore/shutdown fence: old network completions can no longer publish."""
    global _open, _epoch
    _open, _epoch = False, uuid.uuid4().hex


def open_admission() -> None:
    """Lifespan startup only, after restored identity initialization has finished."""
    global _open, _epoch
    _open, _epoch = True, uuid.uuid4().hex


def changed() -> HTTPException:
    return HTTPException(409, {"code": "cloud_credentials_changed"})


@dataclass(frozen=True)
class CredentialSnapshot:
    user_id: int | None
    account_identity: str
    provider: str
    generation: int
    storage_epoch: str | None
    runtime_epoch: str
    authority: object | None = None

    @property
    def namespace(self) -> tuple:
        return (self.account_identity, self.provider, self.generation, self.storage_epoch, self.runtime_epoch)


def _generation(provider: str):
    if provider not in ("bambu", "orca"):
        raise ValueError("Unknown cloud provider")
    return getattr(User, f"{provider}_cloud_generation")


async def _storage_epoch(db) -> str | None:
    return await db.scalar(select(Settings.value).where(Settings.key == "connector_storage_epoch"))


async def snapshot(db, user: User | None, provider: str, *, allow_global: bool = False) -> CredentialSnapshot:
    if not _open:
        raise HTTPException(503, "Cloud credential admission is closed")
    column = _generation(provider)
    epoch = _epoch
    if user is None:
        # No authenticated request is allowed to infer this mode from an absent owner.
        if not allow_global:
            raise HTTPException(401, "Cloud credentials require an active owner")
        value = await db.scalar(select(Settings.value).where(Settings.key == f"{provider}_cloud_generation"))
        generation = int(value or "0")
        identity, user_id = "configured-global", None
    else:
        row = (
            await db.execute(
                select(User.account_identity, column).where(
                    User.id == user.id, User.account_identity == user.account_identity, User.is_active.is_(True)
                )
            )
        ).one_or_none()
        if row is None:
            raise changed()
        identity, generation = row
        user_id = user.id
    actor = (
        (getattr(user, "_cloud_authority", None) or getattr(user, "_auth_capture", None)) if user is not None else None
    )
    result = CredentialSnapshot(user_id, identity, provider, generation, await _storage_epoch(db), epoch, actor)
    if not _open or epoch != _epoch:
        raise changed()
    return result


async def guard_snapshot(db, captured: CredentialSnapshot):
    with db.no_autoflush:
        return await _guard_snapshot(db, captured)


async def _guard_snapshot(db, captured: CredentialSnapshot):
    """Take lifetime first, then compare credential/restore generation in this writer."""
    if not _open or captured.runtime_epoch != _epoch:
        raise changed()
    if captured.user_id is not None:
        actor = captured.authority or AccountCapture(captured.account_identity, None, captured.user_id)
        proof = await guard_accounts(db, [actor])
        if captured.authority is not None:
            await validate_captured(
                db,
                captured.authority,
                required_permissions=("cloud:auth" if captured.provider == "bambu" else "orca_cloud:auth",),
                proof=proof,
            )
        user = await db.scalar(
            select(User)
            .where(User.id == captured.user_id, User.account_identity == captured.account_identity)
            .options(selectinload(User.groups))
            .execution_options(populate_existing=True)
        )
        permission = "cloud:auth" if captured.provider == "bambu" else "orca_cloud:auth"
        if user is None or not user.is_active or not user.has_permission(permission):
            raise changed()
        generation = getattr(user, f"{captured.provider}_cloud_generation")
    else:
        from backend.app.core.credential_config import lock_credential_config

        await lock_credential_config(db)
        key = f"{captured.provider}_cloud_generation"
        row = await db.scalar(select(Settings).where(Settings.key == key).with_for_update())
        generation = int(row.value or "0") if row else 0
        user = None
    if generation != captured.generation or await _storage_epoch(db) != captured.storage_epoch:
        raise changed()
    return user


async def write(
    db, captured: CredentialSnapshot, values: dict, *, commit: bool = True, replace_binding: bool = False
) -> CredentialSnapshot:
    """CAS a provider replacement/clear/refresh; callers must capture BEFORE network I/O."""
    await guard_snapshot(db, captured)
    next_generation = captured.generation + 1
    if captured.user_id is not None:
        column = _generation(captured.provider)
        result = await db.execute(
            update(User)
            .where(
                User.id == captured.user_id,
                User.account_identity == captured.account_identity,
                column == captured.generation,
            )
            .values(**values, **{column.key: next_generation})
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise changed()
        if replace_binding:
            from backend.app.models.user_filament import UserFilamentPreset

            owner_column = getattr(UserFilamentPreset, f"{captured.provider}_pushed_owner_identity")
            cleared = (
                {"pushed_cloud_id": None, "pushed_at": None, "push_dirty": False, "bambu_pushed_owner_identity": None}
                if captured.provider == "bambu"
                else {
                    "orca_pushed_profile_id": None,
                    "orca_pushed_at": None,
                    "orca_push_dirty": False,
                    "orca_pushed_updated_time": None,
                    "orca_pushed_owner_identity": None,
                }
            )
            await db.execute(
                update(UserFilamentPreset)
                .where(owner_column == captured.account_identity)
                .values(**cleared)
                .execution_options(synchronize_session=False)
            )
            await db.execute(
                delete(UserFilamentPreset).where(
                    UserFilamentPreset.owner_user_id == captured.user_id,
                    UserFilamentPreset.source == f"cloud_{captured.provider}",
                )
            )
    else:
        # Only explicit configured-global captures reach this branch.
        for key, value in {**values, f"{captured.provider}_cloud_generation": str(next_generation)}.items():
            row = await db.scalar(select(Settings).where(Settings.key == key))
            if row is None:
                db.add(Settings(key=key, value=value))
            else:
                row.value = value
    if commit:
        await db.commit()
    return replace(captured, generation=next_generation)


async def current(db, captured: CredentialSnapshot) -> bool:
    """Read-only cache publication check; never keeps a principal lock through network I/O."""
    if not _open or captured.runtime_epoch != _epoch:
        return False
    if captured.authority is not None:
        try:
            await validate_captured(
                db,
                captured.authority,
                required_permissions=("cloud:auth" if captured.provider == "bambu" else "orca_cloud:auth",),
            )
        except HTTPException as exc:
            if exc.status_code == 503:
                raise
            return False
    if captured.user_id is None:
        value = await db.scalar(select(Settings.value).where(Settings.key == f"{captured.provider}_cloud_generation"))
        generation = int(value or "0")
    else:
        generation = await db.scalar(
            select(_generation(captured.provider)).where(
                User.id == captured.user_id,
                User.account_identity == captured.account_identity,
                User.is_active.is_(True),
            )
        )
    return generation == captured.generation and await _storage_epoch(db) == captured.storage_epoch
