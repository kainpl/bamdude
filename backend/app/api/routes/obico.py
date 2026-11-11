"""API routes for Obico AI failure detection."""

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.api_key_scope import key_printer_scope
from backend.app.core.auth import RequireAdmin, RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.obico_detection import obico_detection_service, pop_frame
from backend.app.services.obico_history import HistoryUnavailable, obico_history

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/obico", tags=["obico"])


class TestConnectionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    url: str | None = None
    # None means "use the saved token"; "" means "test with no token at all", which
    # is a distinct and useful case — it is how you check a server that runs
    # without ML_API_TOKEN.
    token: str | None = None
    secret_actions: dict[str, Literal["keep", "clear", "replace"]] | None = None


def _public_obico_error(message: str | None) -> str | None:
    """Keep actionable categories without serializing driver URLs or peer text."""
    if message is None:
        return None
    if isinstance(message, str):
        if message.startswith("Obico ML API rejected the token."):
            return "Obico ML API rejected the token. Verify the ML API token in Failure detection settings."
        match = re.match(r"ML API call failed for printer (\d+):", message)
        if match:
            return (
                f"ML API call failed for printer {match.group(1)}. Verify the ML API address and service availability."
            )
        if re.fullmatch(r"Failed to capture snapshot for printer \d+|Printer \d+ not found", message):
            return message
        if message.startswith("external_url setting is empty"):
            return "External URL is empty. Set Settings → General → External URL so the ML API can fetch snapshots."
        if message.startswith("Action dispatch failed:"):
            return "Failure detection could not dispatch the configured action."
    return "Obico failure detection is temporarily unavailable."


@router.get("/status")
async def get_status(
    _: User = RequirePermission(Permission.SETTINGS_READ),
    request: Request = None,
):
    """Scheduler status, per-printer classification, and recent detection history."""
    settings = await obico_detection_service._load_settings()
    status = obico_detection_service.get_status(
        settings["sensitivity"], active=bool(settings["enabled"] and settings["ml_url"])
    )
    scope = key_printer_scope(request)
    if scope is not None:
        status["last_error"] = None
    else:
        status["last_error"] = _public_obico_error(status.get("last_error"))
    status["per_printer"] = {
        pid: {**value, "error": _public_obico_error(value.get("error"))}
        for pid, value in status["per_printer"].items()
        if scope is None or int(pid) in scope
    }
    status["history"] = [ev for ev in status["history"] if scope is None or ev["printer_id"] in scope]
    states = {}
    if _ is None or _.has_permission(Permission.PRINTERS_READ.value):
        try:
            states = await obico_history.recent_states([ev["id"] for ev in status["history"] if "id" in ev])
        except HistoryUnavailable:
            pass
    else:
        status["history"] = []
    status["history"] = [{**ev, "state": states.get(ev.get("id"), "not_saved")} for ev in status["history"]]
    from backend.app.schemas.settings import opaque_service_url

    return {
        **status,
        "enabled": settings["enabled"],
        "ml_url": "" if opaque_service_url(settings["ml_url"]) else settings["ml_url"],
        "ml_url_configured": bool(settings["ml_url"]),
        "sensitivity": settings["sensitivity"],
        "action": settings["action"],
        "poll_interval": settings["poll_interval"],
        "external_url_configured": bool(settings["external_url"]),
        "save_detections": settings.get("save_detections", False),
        "storage": obico_history.status() if scope is None else None,
    }


@router.get("/printer-status")
async def get_printer_status(
    user: User | None = RequirePermission(Permission.PRINTERS_READ),
    request: Request = None,
):
    """Per-printer live classification, for the printer cards (#1546).

    Deliberately excludes configuration — ML URL, action, sensitivity, history —
    so somebody with ``printers:read`` and no ``settings:read`` can still see the
    badge. Widening the existing ``/status`` endpoint's permission instead would
    have handed the ML URL to every operator to save one route.
    """
    settings = await obico_detection_service._load_settings()
    enabled_printers = settings["enabled_printers"]
    # Error strings can embed configured URLs (the ML API base, the external
    # URL), so they stay behind settings:read with the rest of the configuration.
    can_see_error = user is None or user.has_permission(Permission.SETTINGS_READ.value)
    per_printer = obico_detection_service.get_per_printer()
    scope = key_printer_scope(request)
    if scope is not None:
        per_printer = {pid: entry for pid, entry in per_printer.items() if int(pid) in scope}
        enabled_printers = scope if enabled_printers is None else scope.intersection(enabled_printers)
    if not can_see_error:
        # The "error" CLASS is not configuration: an operator still has to know
        # their print is not being watched. Only the reason, which can name a
        # URL, is withheld (upstream #2952).
        per_printer = {pid: {**entry, "error": None} for pid, entry in per_printer.items()}
    else:
        per_printer = {
            pid: {**entry, "error": _public_obico_error(entry.get("error"))} for pid, entry in per_printer.items()
        }
    return {
        "enabled": settings["enabled"],
        # None means every printer is monitored.
        "monitored_printers": sorted(enabled_printers) if enabled_printers is not None else None,
        "per_printer": per_printer,
        "last_error": _public_obico_error(obico_detection_service._last_error)
        if can_see_error and scope is None
        else None,
    }


@router.get("/history")
async def get_history(
    request: Request,
    _: User | None = RequirePermission(Permission.PRINTERS_READ),
    printer_id: int | None = None,
    verdict: Literal["safe", "warning", "failure"] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
):
    """Scoped metadata; no frame bytes, geometry or global storage counters."""

    def utc(value):
        if value is None:
            return None
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    since, until = utc(since), utc(until)
    if since is not None and until is not None and since >= until:
        raise HTTPException(422, "History start must precede its end")
    # Bind a cursor to this credential without embedding it or recording it.
    audience = hashlib.sha256(
        (request.headers.get("authorization", "") + "\0" + request.headers.get("x-api-key", "")).encode()
    ).hexdigest()
    try:
        return await obico_history.page(
            scope=key_printer_scope(request),
            printer_id=printer_id,
            verdict=verdict,
            since=since,
            until=until,
            limit=limit,
            cursor=cursor,
            audience=audience,
        )
    except HistoryUnavailable as exc:
        raise HTTPException(503, "Obico history is temporarily unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "History changed or the cursor expired; reload the history") from exc


@router.get("/printers/{printer_id}/events/{event_id}")
async def get_event(
    printer_id: int,
    event_id: UUID,
    _: User | None = RequirePermission(Permission.PRINTERS_READ, Permission.CAMERA_VIEW),
):
    try:
        result = await obico_history.details(printer_id, str(event_id))
    except HistoryUnavailable as exc:
        raise HTTPException(503, "Obico history is temporarily unavailable") from exc
    if result is None:
        raise HTTPException(404, "Saved detection is no longer available")
    return result


@router.get("/printers/{printer_id}/events/{event_id}/image")
async def get_event_image(
    printer_id: int,
    event_id: UUID,
    _: User | None = RequirePermission(Permission.PRINTERS_READ, Permission.CAMERA_VIEW),
):
    try:
        data = await obico_history.details(printer_id, str(event_id), image=True)
    except HistoryUnavailable as exc:
        raise HTTPException(503, "Obico history is temporarily unavailable") from exc
    if data is None:
        raise HTTPException(404, "Saved detection is no longer available")
    # Complete bytes: no Windows file handle remains during HTTP transmission.
    return Response(
        data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/history")
async def clear_history(_: User | None = RequirePermission(Permission.SETTINGS_UPDATE)):
    try:
        return {"deleted": await obico_history.clear()}
    except HistoryUnavailable as exc:
        raise HTTPException(503, "Obico history is temporarily unavailable") from exc


@router.post("/test-connection", dependencies=[RequireAdmin()])
async def test_connection(
    req: TestConnectionRequest,
    _: User = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Ping the Obico ML API and check the token. Returns ok + raw body + auth_ok.

    A token omitted from the body falls back to the saved setting, so the operator
    can test an already-configured server without retyping a secret the form does
    not echo back.
    """
    actions = req.secret_actions or {}
    if any(key not in {"obico_ml_token", "obico_ml_url"} for key in actions):
        raise HTTPException(422, "Invalid secret action")
    for field, key in (("token", "obico_ml_token"), ("url", "obico_ml_url")):
        if field in req.model_fields_set and actions.get(key) in {"keep", "clear"}:
            raise HTTPException(422, "Secret keep/clear requires an omitted value")
    if req.url == "":
        return {"ok": False, "status_code": None, "body": None, "error": "URL is empty", "auth_ok": None}

    from backend.app.api.routes.settings import resolve_settings_credentials
    from backend.app.core.credential_config import lock_credential_config
    from backend.app.schemas.settings import AppSettingsUpdate

    changes = {"obico_ml_url": req.url} if req.url is not None else {}
    if req.token is not None:
        changes["obico_ml_token"] = req.token
    # Reuse URL admission validation before resolving or contacting the sender.
    try:
        AppSettingsUpdate(**changes)
    except ValidationError:
        raise HTTPException(422, "Invalid Obico destination") from None
    await lock_credential_config(db)
    merged = await resolve_settings_credentials(db, changes, req.secret_actions)
    await db.rollback()
    result = await obico_detection_service.test_connection(merged["obico_ml_url"], merged["obico_ml_token"])
    ok = result.get("ok") is True
    auth_ok = result.get("auth_ok") if type(result.get("auth_ok")) is bool else None
    status_code = result.get("status_code")
    if type(status_code) is not int or not 100 <= status_code <= 599:
        status_code = None
    return {
        "ok": ok,
        "status_code": status_code,
        "auth_ok": auth_ok,
        "body": "ok" if ok else None,
        "error": None
        if ok
        else "Obico ML API rejected the token. Verify the configured credential."
        if auth_ok is False
        else "Obico connection test failed. Verify the ML API address and service availability.",
    }


@router.get("/cached-frame/{nonce}")
async def cached_frame(nonce: str):
    """Serve a pre-captured JPEG to the Obico ML API.

    The detection loop captures a snapshot locally (where we control the timeout),
    stashes the bytes under a one-shot random nonce, then hands this URL to Obico's
    ML API. Obico's hardcoded 5s read timeout never races our snapshot pipeline.

    Unauthenticated: the unguessable 32-byte nonce is single-use and expires in
    seconds, so exposing this path doesn't widen the camera access surface.
    Whitelisted in ``main.py::PUBLIC_API_PATTERNS`` so the auth-middleware lets
    Obico's bearer-less GET through.
    """
    data = await pop_frame(nonce)
    if data is None:
        raise HTTPException(status_code=404, detail="Frame not found or expired")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )
