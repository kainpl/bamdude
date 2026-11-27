"""Adapt the existing key/owner ceiling into a request-local scalar snapshot."""

from fastapi import HTTPException
from starlette.requests import Request

from backend.app.core.auth import (
    APIKeyValidationFailure,
    authorize_api_key,
    is_api_key_token,
    resolve_api_key_authority,
)
from backend.app.core.permissions import Permission
from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.services.queue_sources import current_epoch


def credential(scope) -> str:
    values: list[str] = []
    for name, value in scope["headers"]:
        if name.lower() == b"authorization":
            parts = value.decode("latin-1").split()
            if len(parts) != 2 or parts[0].lower() != "bearer":
                raise HTTPException(401)
            values.append(parts[1])
        elif name.lower() == b"x-api-key":
            values.append(value.decode("latin-1"))
    if not values or any(value != values[0] for value in values):
        raise HTTPException(401)
    if not is_api_key_token(values[0]):
        raise HTTPException(401)
    return values[0]


async def authenticate(scope) -> DiagnosticAccess:
    token = credential(scope)
    epoch = current_epoch()
    try:
        authority = await resolve_api_key_authority(Request(scope), token)
    except APIKeyValidationFailure as exc:
        raise HTTPException(503 if exc.reason == "overloaded" else 401) from None
    if not authority.mcp_allowed or not authority.owner_required:
        raise HTTPException(403)
    # The authority already owns its short validation/last_used transaction.
    # Supplying it avoids opening another session or resolving a user twice.
    await authorize_api_key(None, authority.key, [Permission.PRINTERS_READ.value], authority=authority)
    if epoch != current_epoch():
        raise HTTPException(503)
    key, owner = authority.key, authority.owner
    # N62 D5: the key's printers are its own list ∩ its owner's scope, frozen with the access.
    from backend.app.core.database import async_session
    from backend.app.core.printer_scope import key_scope

    async with async_session() as db:
        printers = await key_scope(db, key, owner)
    return DiagnosticAccess(
        key_id=key.id,
        owner_id=owner.id,
        permissions=authority.owner_permissions,
        is_admin=authority.owner_is_admin,
        printer_ids=printers,
        logs_allowed=authority.mcp_logs_allowed,
        epoch=epoch,
        key_identity=key.connector_identity,
        owner_identity=owner.account_identity,
    )
