"""API routes for OctoEverywhere AI failure detection (ported from upstream Bambuddy v1.3.0)."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.api_key_scope import key_printer_scope
from backend.app.core.auth import RequireAdmin, RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.octoeverywhere_detection import octoeverywhere_detection_service

router = APIRouter(prefix="/octoeverywhere", tags=["octoeverywhere"])


class TestConnectionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    # None means "test the saved key"; a draft is sent with ``secret_actions`` = replace.
    api_key: str | None = None
    confidence: Literal["lowest", "low", "medium", "high", "highest"] | None = None
    secret_actions: dict[Literal["octoeverywhere_api_key"], Literal["keep", "clear", "replace"]] | None = None


@router.get("/status")
async def get_status(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
    request: Request = None,
):
    """Scheduler status, print quality, and recent detection history."""
    settings = await octoeverywhere_detection_service._load_settings()
    status = octoeverywhere_detection_service.get_status()
    scope = key_printer_scope(request)
    history = [entry for entry in status["history"] if scope is None or entry["printer_id"] in scope]
    if _ is not None and not _.has_permission(Permission.PRINTERS_READ.value):
        history = []
    return {
        **status,
        "per_printer": {pid: entry for pid, entry in status["per_printer"].items() if scope is None or pid in scope},
        "history": history,
        "last_error": status["last_error"] if scope is None else None,
        "last_error_code": status["last_error_code"] if scope is None else None,
        "enabled": settings["enabled"],
        "api_key_configured": bool(settings["api_key"]),
        "confidence": settings["confidence"],
        "action": settings["action"],
        "poll_interval": settings["poll_interval"],
    }


@router.get("/printer-status")
async def get_printer_status(
    user: User | None = RequirePermission(Permission.PRINTERS_READ),
    request: Request = None,
):
    """Live printer-card status without exposing provider configuration."""
    settings = await octoeverywhere_detection_service._load_settings()
    enabled_printers = settings["enabled_printers"]
    can_see_error = user is None or user.has_permission(Permission.SETTINGS_READ.value)
    per_printer = octoeverywhere_detection_service.get_per_printer()
    if not can_see_error:
        per_printer = {pid: {**entry, "error": None, "error_code": None} for pid, entry in per_printer.items()}
    scope = key_printer_scope(request)
    if scope is not None:
        per_printer = {pid: entry for pid, entry in per_printer.items() if pid in scope}
        enabled_printers = scope if enabled_printers is None else scope.intersection(enabled_printers)
    return {
        "enabled": settings["enabled"],
        # None means every printer is monitored.
        "monitored_printers": sorted(enabled_printers) if enabled_printers is not None else None,
        "per_printer": per_printer,
        "last_error": octoeverywhere_detection_service._last_error if can_see_error and scope is None else None,
        "last_error_code": octoeverywhere_detection_service._last_error_code
        if can_see_error and scope is None
        else None,
    }


@router.post("/test-connection", dependencies=[RequireAdmin()])
async def test_connection(
    req: TestConnectionRequest,
    _: User = RequirePermission(Permission.SETTINGS_UPDATE),
    db: AsyncSession = Depends(get_db),
):
    """Validate the saved or the draft key by creating a context, without uploading a camera image."""
    actions = req.secret_actions or {}
    if "api_key" in req.model_fields_set and actions.get("octoeverywhere_api_key") in {"keep", "clear"}:
        raise HTTPException(422, "Secret keep/clear requires an omitted value")
    if req.api_key is not None and not req.api_key.strip():
        return await octoeverywhere_detection_service.test_connection("", req.confidence or "medium")

    from backend.app.api.routes.settings import resolve_settings_credentials
    from backend.app.core.credential_config import lock_credential_config

    changes = {"octoeverywhere_api_key": req.api_key.strip()} if req.api_key is not None else {}
    await lock_credential_config(db)
    merged = await resolve_settings_credentials(db, changes, req.secret_actions)
    await db.rollback()
    confidence = req.confidence or merged.get("octoeverywhere_confidence") or "medium"
    return await octoeverywhere_detection_service.test_connection(
        merged.get("octoeverywhere_api_key") or "", confidence
    )
