import logging
import time
import uuid

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from backend.app.core.auth import authenticate_websocket_token
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
    started = time.monotonic()
    bootstrap_id = uuid.uuid4().hex[:12]
    # Authenticate BEFORE ws_manager.connect() so an unauth caller never enters
    # the broadcast set.
    authenticated, user_id = await authenticate_websocket_token(token or "")
    if not authenticated:
        logger.info("WebSocket connect refused: missing/invalid token")
        await websocket.close(code=_WS_CLOSE_UNAUTHORIZED)
        return

    logger.info("WebSocket client connecting...")
    # Tag the connection with the minting user (None for API-key callers) so the
    # manager can target per-user broadcasts. Auth already passed above.
    await ws_manager.connect(websocket, user_id)
    logger.info("WebSocket client connected")
    accepted_at = time.monotonic()

    try:
        # Send initial status of all printers
        initial_status_started = time.monotonic()
        statuses = printer_manager.get_all_statuses()
        for printer_id, state in statuses.items():
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
                "printers": len(statuses),
            },
        )
        ack_pending = True
        logger.info("Queued initial status for %s printers", len(statuses))
        logger.info(
            "WebSocket bootstrap timing: auth_and_accept=%.3fs initial_queue=%.3fs printers=%s id=%s",
            accepted_at - started,
            time.monotonic() - initial_status_started,
            len(statuses),
            bootstrap_id,
        )

        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_json()

            # Handle ping/pong for keepalive
            if data.get("type") == "ping":
                await ws_manager.send(websocket, {"type": "pong"})

            elif data.get("type") == "initial_status_applied" and ack_pending:
                if data.get("bootstrap_id") == bootstrap_id:
                    # Log once. Values from a browser are untrusted and never
                    # interpolated as arbitrary text, payloads or URLs.
                    client_ms = data.get("connect_ms")
                    client_ms = client_ms if type(client_ms) in (int, float) and 0 <= client_ms <= 300_000 else None
                    logger.info(
                        "WebSocket bootstrap applied: id=%s server_elapsed=%.3fs client_connect_ms=%s printers=%s",
                        bootstrap_id,
                        time.monotonic() - started,
                        client_ms,
                        len(statuses),
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
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)
    finally:
        await ws_manager.disconnect(websocket)
