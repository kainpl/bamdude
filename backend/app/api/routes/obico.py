"""API routes for Obico AI failure detection."""

import logging

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from backend.app.core.auth import RequirePermission
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.obico_detection import obico_detection_service, pop_frame

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/obico", tags=["obico"])


class TestConnectionRequest(BaseModel):
    url: str
    # None means "use the saved token"; "" means "test with no token at all", which
    # is a distinct and useful case — it is how you check a server that runs
    # without ML_API_TOKEN.
    token: str | None = None


@router.get("/status")
async def get_status(
    _: User = RequirePermission(Permission.SETTINGS_READ),
):
    """Scheduler status, per-printer classification, and recent detection history."""
    settings = await obico_detection_service._load_settings()
    status = obico_detection_service.get_status(settings["sensitivity"])
    return {
        **status,
        "enabled": settings["enabled"],
        "ml_url": settings["ml_url"],
        "sensitivity": settings["sensitivity"],
        "action": settings["action"],
        "poll_interval": settings["poll_interval"],
        "external_url_configured": bool(settings["external_url"]),
    }


@router.get("/printer-status")
async def get_printer_status(
    user: User | None = RequirePermission(Permission.PRINTERS_READ),
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
    return {
        "enabled": settings["enabled"],
        # None means every printer is monitored.
        "monitored_printers": sorted(enabled_printers) if enabled_printers is not None else None,
        "per_printer": obico_detection_service.get_per_printer(),
        "last_error": obico_detection_service._last_error if can_see_error else None,
    }


@router.post("/test-connection")
async def test_connection(
    req: TestConnectionRequest,
    _: User = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Ping the Obico ML API and check the token. Returns ok + raw body + auth_ok.

    A token omitted from the body falls back to the saved setting, so the operator
    can test an already-configured server without retyping a secret the form does
    not echo back.
    """
    if not req.url:
        return {"ok": False, "status_code": None, "body": None, "error": "URL is empty", "auth_ok": None}

    token = req.token
    if token is None:
        from sqlalchemy import select

        from backend.app.core.database import async_session
        from backend.app.models.settings import Settings

        async with async_session() as db:
            row = (await db.execute(select(Settings).where(Settings.key == "obico_ml_token"))).scalar_one_or_none()
            token = row.value if row else ""
    return await obico_detection_service.test_connection(req.url, token)


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
