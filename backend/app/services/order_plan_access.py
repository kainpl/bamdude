"""Authority rechecked on the plan/publication's own database connection."""

from dataclasses import dataclass

from fastapi import HTTPException

from backend.app.core.api_key_scope import key_printer_scope, narrowed_scope
from backend.app.core.auth import authorize_api_key, library_name_scope
from backend.app.models.api_key import APIKey
from backend.app.models.user import User
from backend.app.services.swap_source_declarations import Actor, actor_for, fresh_api_key


@dataclass(frozen=True)
class PlanAccess:
    actor: Actor
    scope: str
    printers: frozenset[int] | None
    key_hash: str | None = None

    @property
    def identity(self):
        capture = self.actor.capture
        if capture is None:
            raise denied()
        return f"key:{capture.key_identity}" if capture.kind == "key" else f"user:{capture.account_identity}"


def denied():
    return HTTPException(403, {"code": "plan_authority_changed", "message": "Plan permissions changed. Read it again."})


async def capture(request, db, user):
    actor = await actor_for(request, db, user)
    key = await db.get(APIKey, actor.key_id) if actor.key_id else None
    return PlanAccess(
        actor, await library_name_scope(request, db, user), key_printer_scope(request), key.key_hash if key else None
    )


async def fresh(db, access: PlanAccess, *, edit=True, execute=False, receipts=False, delete=False, admin_cancel=False):
    actor = access.actor
    from backend.app.core.auth_lifetime import validate_captured

    if actor.capture is None:
        raise denied()
    principal = await validate_captured(db, actor.capture, required_permissions=("orders:read",))
    if actor.kind == "api_key":
        key = await fresh_api_key(db, actor)
        if key.key_hash != access.key_hash or key.user_id != actor.user_id:
            raise denied()
        await authorize_api_key(db, key, ["orders:read"] + (["queue:create"] if execute else []))
        if delete:
            await authorize_api_key(db, key, ["orders:delete"])
        if edit:
            await authorize_api_key(db, key, ["orders:update", "orders:file_prints"], require_any=True)
        if receipts:
            await authorize_api_key(db, key, ["queue:read_all", "queue:read_own"], require_any=True)
        if admin_cancel:
            await authorize_api_key(db, key, ["orders:update", "queue:update_all", "queue:read_all"])
        try:
            await authorize_api_key(db, key, ["library:read_all"])
            scope = "all"
        except HTTPException:
            try:
                await authorize_api_key(db, key, ["library:read_own"])
                scope = "own"
            except HTTPException:
                scope = "none"
        printers = narrowed_scope(access.printers, key)
        user = await db.get(User, actor.user_id) if actor.user_id else None
    else:
        user = principal.user
        if user is None or not user.is_active or not user.has_permission("orders:read"):
            raise denied()
        if edit and not (user.has_permission("orders:update") or user.has_permission("orders:file_prints")):
            raise denied()
        if execute and not user.has_permission("queue:create"):
            raise denied()
        if delete and not user.has_permission("orders:delete"):
            raise denied()
        if receipts and not (user.has_permission("queue:read_all") or user.has_permission("queue:read_own")):
            raise denied()
        if admin_cancel and not all(
            user.has_permission(p) for p in ("orders:update", "queue:update_all", "queue:read_all")
        ):
            raise denied()
        scope = (
            "all"
            if user.has_permission("library:read_all")
            else "own"
            if user.has_permission("library:read_own")
            else "none"
        )
        printers = access.printers
    # A later grant can only narrow what the request originally obtained.
    if access.scope == "none" or scope == "none":
        scope = "none"
    elif access.scope == "own":
        scope = "own"
    return user, scope, printers


async def guard(db, access):
    from backend.app.core.auth_lifetime import guard_accounts

    if access.actor.capture is None:
        raise denied()
    return await guard_accounts(db, [access.actor.capture])


def visible(file, user, scope):
    return file.deleted_at is None and (
        scope == "all" or scope == "own" and user is not None and file.created_by_id == user.id
    )
