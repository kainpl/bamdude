"""Farm diagnostics and explicitly confirmed, addressed structural repairs."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from backend.app.core import database
from backend.app.core.api_key_scope import key_printer_scope, narrowed_scope
from backend.app.core.auth import (
    RequestCredentials,
    authorize_api_key,
    finish_request_auth,
    is_api_key_token,
    request_credentials,
    require_permission,
    resolve_api_key_authority,
)
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.schemas.print_state_recovery import RecoveryApplyRequest, RecoveryKind, RecoveryPreviewRequest
from backend.app.services import print_state_recovery_preview as tokens, print_state_recovery_store as store

router = APIRouter(prefix="/system", tags=["system"])
READ_PERMISSIONS = (
    Permission.SYSTEM_READ,
    Permission.PRINTERS_READ,
    Permission.QUEUE_READ_ALL,
    Permission.ARCHIVES_READ_ALL,
)
READ = tuple(require_permission(p) for p in READ_PERMISSIONS)
WRITE_PERMISSIONS = {
    "align_terminal_item": (Permission.QUEUE_UPDATE_ALL, Permission.PRINTERS_CONTROL),
    "release_orphan_header": (Permission.QUEUE_UPDATE_ALL, Permission.PRINTERS_CONTROL),
    "fill_estimated_end_and_align": (
        Permission.QUEUE_UPDATE_ALL,
        Permission.PRINTERS_CONTROL,
        Permission.ARCHIVES_UPDATE_ALL,
    ),
    "delete_stale_item": (Permission.QUEUE_UPDATE_ALL, Permission.PRINTERS_CONTROL, Permission.QUEUE_DELETE_ALL),
    "close_interrupted_attempt": (Permission.QUEUE_UPDATE_ALL, Permission.PRINTERS_CONTROL),
    # The in-memory repair pauses a queue and sends nothing to the printer: the read rights and the queue's
    # update right (spec printer-runtime-stall r3 R6).
    "clear_terminal_runtime_context": (Permission.QUEUE_UPDATE_ALL,),
}
WRITE = {name: tuple(require_permission(p) for p in perms) for name, perms in WRITE_PERMISSIONS.items()}


async def read_access(creds):
    user = None
    for gate in READ:
        found = await creds.check(gate)
        user = found or user
    return user


async def write_access(creds, action):
    if action not in WRITE:
        raise HTTPException(422, "recovery_not_allowed")
    for gate in WRITE[action]:
        await creds.check(gate)


def key_credential(creds):
    key = creds.x_api_key
    if not key and creds.credentials and is_api_key_token(creds.credentials.credentials):
        key = creds.credentials.credentials
    return key


async def actor_identity(creds, user):
    key = key_credential(creds)
    if key:
        authority = await resolve_api_key_authority(creds.request, key)
        return f"api_key:{authority.key.id}", f"API key #{authority.key.id}"
    if user:
        return f"user:{user.id}", f"User #{user.id}"
    return "auth_disabled:local", "Local operator"


async def fresh_key_access(db, initial_key, granted_scope, printer_id, action):
    """Recheck authority on the writer's connection: credential hashing/last_used writes belong outside it."""
    key_id, key_hash, owner_id = initial_key
    key = await db.get(APIKey, key_id, populate_existing=True)
    if key is None or not key.enabled or key.key_hash != key_hash or key.user_id != owner_id:
        raise HTTPException(403, "recovery_changed")
    if key.expires_at is not None:
        expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(403, "recovery_changed")
    await authorize_api_key(db, key, [p.value for p in (*READ_PERMISSIONS, *WRITE_PERMISSIONS[action])])
    scope = narrowed_scope(granted_scope, key)
    if scope is not None and printer_id not in scope:
        raise HTTPException(403, "recovery_changed")


@router.get("/print-state-issues")
async def issues(
    request: Request,
    printer_id: int | None = Query(None, gt=0, le=2_147_483_647),
    cursor: str | None = Query(None, max_length=160),
    kind: RecoveryKind | None = Query(None),
    target_id: int | None = Query(None, ge=0, le=2_147_483_647),
    creds: RequestCredentials = Depends(request_credentials),
):
    await read_access(creds)
    if (kind is None) != (target_id is None) or kind is not None and printer_id is None:
        raise HTTPException(422, "invalid_recovery_target")
    allowed = {action for action, gates in WRITE.items() if all([await creds.allows(gate) for gate in gates])}
    try:
        async with database.async_session() as db:
            result = await store.list_issues(
                db,
                key_printer_scope(request),
                printer_id=printer_id,
                cursor=cursor,
                target=(kind, target_id) if kind is not None else None,
            )
        for issue in result["issues"]:
            issue["actions"] = [action for action in issue["actions"] if action in allowed]
        return result
    except ValueError as exc:
        raise HTTPException(422, "invalid_cursor") from exc


@router.post("/printers/{printer_id}/print-state/preview")
async def preview(
    printer_id: int, data: RecoveryPreviewRequest, creds: RequestCredentials = Depends(request_credentials)
):
    user = await read_access(creds)
    await write_access(creds, data.action)
    actor, _ = await actor_identity(creds, user)
    try:
        async with database.async_session() as db:
            return await store.make_preview(
                db,
                printer_id,
                data.kind,
                data.target_id,
                data.action,
                actor,
                confirmed_completed=data.confirmed_completed,
                note=data.note,
            )
    except LookupError as exc:
        raise HTTPException(404, "recovery_changed") from exc
    except ValueError as exc:
        raise HTTPException(409, "recovery_not_allowed") from exc


@router.post("/printers/{printer_id}/print-state/apply")
async def apply(printer_id: int, data: RecoveryApplyRequest, creds: RequestCredentials = Depends(request_credentials)):
    user = await read_access(creds)
    actor, label = await actor_identity(creds, user)
    credential = key_credential(creds)
    initial_key = None
    if credential:
        key = (await resolve_api_key_authority(creds.request, credential)).key
        initial_key = key.id, key.key_hash, key.user_id
    granted_scope = key_printer_scope(creds.request)
    try:
        payload = tokens.verify(data.preview_token)
        if payload.get("printer_id") != printer_id or payload.get("actor") != actor:
            raise ValueError("recovery_changed")
        await write_access(creds, payload.get("action"))

        async def recheck(action, db):
            # Authority cached before a contended guard must not outlive revocation.
            if initial_key is not None:
                await fresh_key_access(db, initial_key, granted_scope, printer_id, action)
                return
            await finish_request_auth(creds.request)
            fresh_user = await read_access(creds)
            await write_access(creds, action)
            fresh_actor, _ = await actor_identity(creds, fresh_user)
            if fresh_actor != actor:
                raise HTTPException(403, "recovery_changed")

        return await store.apply_preview(database.async_session, payload, actor, label, recheck=recheck)
    except ValueError as exc:
        code = str(exc)
        if code not in {"recovery_changed", "recovery_preview_expired", "recovery_not_allowed"}:
            code = "recovery_changed"
        raise HTTPException(409, code) from exc


@router.get("/printers/{printer_id}/print-state/history")
async def history(
    printer_id: int,
    before: str | None = Query(None, max_length=36),
    creds: RequestCredentials = Depends(request_credentials),
):
    await read_access(creds)
    # Keyset includes created_at and UUID; UUID alone is not chronological.
    async with database.async_session() as db:
        query = select(PrintStateRecoveryAction).where(PrintStateRecoveryAction.printer_id == printer_id)
        if before:
            from sqlalchemy import and_, or_

            prior = await db.get(PrintStateRecoveryAction, before)
            if prior is None or prior.printer_id != printer_id:
                raise HTTPException(404, "recovery_changed")
            query = query.where(
                or_(
                    PrintStateRecoveryAction.created_at < prior.created_at,
                    and_(
                        PrintStateRecoveryAction.created_at == prior.created_at, PrintStateRecoveryAction.id < prior.id
                    ),
                )
            )
        rows = list(
            (
                await db.scalars(
                    query.order_by(
                        PrintStateRecoveryAction.created_at.desc(), PrintStateRecoveryAction.id.desc()
                    ).limit(51)
                )
            ).all()
        )

        def phase_of(row):
            return row.result.get("phase") if isinstance(row.result, dict) else None

        # The pairs of in-memory repairs, read by the outcomes' own ids in one go, wherever the page boundary fell.
        wanted = [store.outcome_id(row.id) for row in rows[:50] if phase_of(row) == "intent"]
        outcomes = (
            {
                row.id: row
                for row in (
                    await db.scalars(select(PrintStateRecoveryAction).where(PrintStateRecoveryAction.id.in_(wanted)))
                ).all()
            }
            if wanted
            else {}
        )
        actions = []
        for row in rows[:50]:
            phase = phase_of(row)
            if phase == "outcome":
                continue  # shown inside its intent, wherever the page boundary fell
            action = {"created_at": row.created_at, "actor_label": row.actor_label, **row.result}
            if phase == "intent":
                # An intent with no outcome proves nothing, and one of an ended process is said to be so.
                outcome = outcomes.get(store.outcome_id(row.id))
                action["outcome"] = outcome.result if outcome is not None else None
                action["process_ended"] = outcome is None and row.result.get("incarnation") != tokens.incarnation()
            actions.append(action)
        return {"actions": actions, "next_cursor": rows[49].id if len(rows) > 50 else None}
