import asyncio
import logging
import math
import time
import uuid

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from backend.app.core.auth import authenticate_websocket_authority, selected_api_key
from backend.app.core.websocket import ws_manager
from backend.app.services.background_dispatch import background_dispatch
from backend.app.services.live_authority import LiveDescriptor, live_authority, monotonic_expiry
from backend.app.services.printer_manager import printer_manager, printer_state_to_dict

logger = logging.getLogger(__name__)
router = APIRouter()

# 4401 mirrors the "unauthorised" application close code convention for
# WebSockets (private-use range 4000-4999 per RFC 6455). The SPA distinguishes
# it from a network drop and stops reconnecting until a fresh login remounts it.
_WS_CLOSE_UNAUTHORIZED = 4401


def _bounded_client_ms(value: object) -> int | float | None:
    """Accept only finite, bounded client-reported timing values."""
    if type(value) not in (int, float) or not 0 <= value <= 300_000:
        return None
    return value if math.isfinite(value) else None


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
    """WebSocket endpoint for real-time updates.

    Auth gate (upstream Bambuddy GHSA-r2qv follow-up): the HTTP auth middleware
    only runs on the "http" scope, so it never intercepts the WebSocket upgrade.
    Without this check any client that could reach the HTTP port would land in
    ``ws_manager.active_connections`` and receive every ``printer_status`` /
    ``print_*`` / ``archive_*`` / ``inventory_*`` broadcast (broadcasts walk the
    connection list blindly). Auth is always-on in BamDude, so a valid
    ``?token=`` (minted by ``POST /api/v1/auth/ws-token`` behind
    ``Permission.WEBSOCKET_CONNECT``) is required before ``accept()`` — an
    unauthenticated caller is closed with 4401 and never joins the fan-out.
    """
    started = time.monotonic()
    bootstrap_id = uuid.uuid4().hex[:12]
    # Upgrade bypasses the HTTP gate. This door accepts only the minted query
    # credential; a selected key cannot borrow it or open a raw-key WS door.
    if selected_api_key(websocket) is not None:
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return
    # Authenticate BEFORE ws_manager.connect() so an unauth caller never enters
    # the broadcast set.
    try:
        authenticated = await authenticate_websocket_authority(token or "", details=True)
    except HTTPException as exc:
        await websocket.close(code=4403 if exc.status_code == 403 else 1013)
        return
    if authenticated is None:
        logger.info("WebSocket connect refused: missing/invalid token")
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return

    logger.info("WebSocket client connecting...")
    # Preserve the exact credential capture for lifetime ownership; only a
    # browser session is tagged for a person's per-user channel.
    authority = authenticated.capture
    user_id = authenticated.user.id if authority.kind == "session" else None
    route_task = asyncio.current_task()
    live_authority.own(route_task)
    try:
        handle = live_authority.register(
            LiveDescriptor(
                authority,
                ("websocket:connect",),
                expires=monotonic_expiry(authenticated.user._auth_expires),
            )
        )
    except HTTPException:
        # A capacity/restore refusal is retryable to an actual browser, whose
        # upgrade API otherwise hides a pre-accept close code behind 1006.
        await websocket.accept()
        async with asyncio.timeout(5):
            await websocket.close(code=1013)
        return
    try:
        await ws_manager.connect(
            websocket, user_id, account_identity=authority.account_identity, authority=authority, live_handle=handle
        )
        logger.info("WebSocket client connected")
        accepted_at = time.monotonic()
        # Send initial status of all printers
        initial_status_started = time.monotonic()
        statuses = printer_manager.get_all_statuses()
        source_identities = printer_manager.record_identity_snapshot()
        visible_statuses = {
            pid: state
            for pid, state in statuses.items()
            if handle.entitlements is not None
            and handle.entitlements.allows("printers:read")
            and handle.entitlements.printer_visible(pid)
        }
        for printer_id, state in visible_statuses.items():
            await ws_manager.send(
                websocket,
                {
                    "type": "printer_status",
                    "printer_id": printer_id,
                    "data": printer_state_to_dict(
                        state,
                        printer_id,
                        printer_manager.get_model(printer_id),
                        printer_manager.get_drying_targets(printer_id),
                    ),
                },
                source_printer_identities=source_identities,
            )

        dispatch_state = await background_dispatch.get_state()
        if (dispatch_state.get("dispatched", 0) + dispatch_state.get("processing", 0)) > 0:
            await ws_manager.send(
                websocket,
                {
                    "type": "background_dispatch",
                    "data": dispatch_state,
                },
            )
        await ws_manager.send(
            websocket,
            {
                "type": "initial_status_complete",
                "bootstrap_id": bootstrap_id,
                "printers": len(visible_statuses),
            },
        )
        ack_pending = True
        logger.info("Queued initial status for %s printers", len(visible_statuses))
        logger.info(
            "WebSocket bootstrap timing: auth_and_accept=%.3fs initial_queue=%.3fs printers=%s id=%s",
            accepted_at - started,
            time.monotonic() - initial_status_started,
            len(visible_statuses),
            bootstrap_id,
        )

        # Keep connection alive and handle incoming messages
        while True:
            data = await ws_manager.receive(websocket)

            # Handle ping/pong for keepalive
            if data.get("type") == "ping":
                await ws_manager.send(websocket, {"type": "pong"})

            elif data.get("type") == "initial_status_applied" and ack_pending:
                if data.get("bootstrap_id") == bootstrap_id:
                    # Log once. Values from a browser are untrusted and never
                    # interpolated as arbitrary text, payloads or URLs.
                    client_ms = _bounded_client_ms(data.get("connect_ms"))
                    logger.info(
                        "WebSocket bootstrap applied: id=%s server_elapsed=%.3fs client_connect_ms=%s token_ms=%s socket_open_ms=%s first_status_ms=%s marker_to_cache_ms=%s printers=%s",
                        bootstrap_id,
                        time.monotonic() - started,
                        client_ms,
                        _bounded_client_ms(data.get("token_ms")),
                        _bounded_client_ms(data.get("socket_open_ms")),
                        _bounded_client_ms(data.get("first_status_ms")),
                        _bounded_client_ms(data.get("marker_to_cache_ms")),
                        len(visible_statuses),
                    )
                    ack_pending = False

            # Handle status request
            elif data.get("type") == "get_status":
                printer_id = data.get("printer_id")
                if printer_id:
                    state = printer_manager.get_status(printer_id)
                    if state:
                        await ws_manager.send(
                            websocket,
                            {
                                "type": "printer_status",
                                "printer_id": printer_id,
                                "data": printer_state_to_dict(
                                    state,
                                    printer_id,
                                    printer_manager.get_model(printer_id),
                                    printer_manager.get_drying_targets(printer_id),
                                ),
                            },
                        )

    except WebSocketDisconnect as exc:
        logger.info("WebSocket client disconnected (code=%s)", exc.code)
    except HTTPException:
        # Admission failures already select the sole writer's retryable close.
        pass
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)
    finally:
        await ws_manager.disconnect(websocket)
        # accept itself can fail before the manager has an outbox.
        live_authority.release(handle)
