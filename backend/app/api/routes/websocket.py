import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from backend.app.core.auth import resolve_websocket_token_user, verify_websocket_token
from backend.app.core.websocket import ws_manager
from backend.app.services.background_dispatch import background_dispatch
from backend.app.services.printer_manager import printer_manager, printer_state_to_dict

logger = logging.getLogger(__name__)
router = APIRouter()

# 4401 mirrors the "unauthorised" application close code convention for
# WebSockets (private-use range 4000-4999 per RFC 6455). The SPA distinguishes
# it from a network drop and refetches a token instead of retrying the old one.
_WS_CLOSE_UNAUTHORIZED = 4401


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
    # Authenticate BEFORE ws_manager.connect() so an unauth caller never enters
    # the broadcast set.
    if not await verify_websocket_token(token or ""):
        logger.info("WebSocket connect refused: missing/invalid token")
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return

    logger.info("WebSocket client connecting...")
    # Tag the connection with the minting user (None for API-key callers) so the
    # manager can target per-user broadcasts. Auth already passed above.
    user_id = await resolve_websocket_token_user(token or "")
    await ws_manager.connect(websocket, user_id)
    logger.info("WebSocket client connected")

    try:
        # Send initial status of all printers
        statuses = printer_manager.get_all_statuses()
        for printer_id, state in statuses.items():
            await websocket.send_json(
                {
                    "type": "printer_status",
                    "printer_id": printer_id,
                    "data": printer_state_to_dict(
                        state,
                        printer_id,
                        printer_manager.get_model(printer_id),
                        printer_manager.get_drying_targets(printer_id),
                    ),
                }
            )

        dispatch_state = await background_dispatch.get_state()
        if (dispatch_state.get("dispatched", 0) + dispatch_state.get("processing", 0)) > 0:
            await websocket.send_json(
                {
                    "type": "background_dispatch",
                    "data": dispatch_state,
                }
            )
        logger.info("Sent initial status for %s printers", len(statuses))

        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_json()

            # Handle ping/pong for keepalive
            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

            # Handle status request
            elif data.get("type") == "get_status":
                printer_id = data.get("printer_id")
                if printer_id:
                    state = printer_manager.get_status(printer_id)
                    if state:
                        await websocket.send_json(
                            {
                                "type": "printer_status",
                                "printer_id": printer_id,
                                "data": printer_state_to_dict(
                                    state,
                                    printer_id,
                                    printer_manager.get_model(printer_id),
                                    printer_manager.get_drying_targets(printer_id),
                                ),
                            }
                        )

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected normally")
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)
        await ws_manager.disconnect(websocket)
