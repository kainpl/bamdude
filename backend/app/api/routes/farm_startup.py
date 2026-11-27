"""Authenticated observation and explicit, durable farm-startup acknowledgement."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.app.api.routes.print_state_recovery import actor_identity, key_credential
from backend.app.core.auth import RequestCredentials, finish_request_auth, request_credentials, require_permission
from backend.app.core.permissions import Permission
from backend.app.core.printer_scope import request_printer_scope
from backend.app.core.websocket import ws_manager
from backend.app.services.farm_startup_guard import FarmStartupRefused, farm_startup_guard

router = APIRouter(prefix="/system", tags=["system"])
_authenticated = require_permission()
_resume_permission = require_permission(Permission.FARM_RESUME)
_resume_lock = asyncio.Lock()


class FarmResumeRequest(BaseModel):
    expected_boot_id: str = Field(min_length=1, max_length=36)
    expected_hold_id: str = Field(min_length=1, max_length=36)


@router.get("/farm-startup")
async def status(creds: RequestCredentials = Depends(request_credentials)):
    await creds.check(require_permission(Permission.SYSTEM_READ) if key_credential(creds) else _authenticated)
    state = farm_startup_guard.status()
    state["can_resume"] = (
        state["can_resume"] and await creds.allows(_resume_permission) and request_printer_scope(creds.request) is None
    )
    return state


@router.post("/farm-startup/resume")
async def resume(data: FarmResumeRequest, creds: RequestCredentials = Depends(request_credentials)):
    await creds.check(_resume_permission)
    if request_printer_scope(creds.request) is not None:
        raise HTTPException(403, "farm_scope_required")
    async with _resume_lock:
        # A permission decision from before a contended acknowledgement must
        # not outlive revocation, including the key owner's current authority.
        await finish_request_auth(creds.request)
        user = await creds.check(_resume_permission)
        if request_printer_scope(creds.request) is not None:
            raise HTTPException(403, "farm_scope_required")
        actor, _ = await actor_identity(creds, user)
        try:
            result = farm_startup_guard.resume(data.expected_boot_id, data.expected_hold_id, actor=actor)
        except FarmStartupRefused as exc:
            raise HTTPException(503 if exc.reason == "storage_unavailable" else 409, exc.reason) from exc
    if result == "released":
        try:
            await ws_manager.broadcast({"type": "farm_startup_changed"})
        except Exception:
            logging.getLogger(__name__).exception("Farm startup: release invalidation failed")
        # The pause held every interrupted dispatch back; now each is checked once more,
        # by its own facts (post-rebuild I-R3). Nothing physical is sent from here.
        try:
            from backend.app.services.interrupted_dispatch import recover_after_resume

            await recover_after_resume()
        except Exception:
            logging.getLogger(__name__).exception("Farm startup: interrupted-dispatch recheck failed")
    return {"result": result, **farm_startup_guard.status()}
