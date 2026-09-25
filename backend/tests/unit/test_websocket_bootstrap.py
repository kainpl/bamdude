"""Exercise the actual ASGI WebSocket route with synthetic printer states."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.routes import websocket as route
from backend.app.core.websocket import ConnectionManager
from backend.app.services.bambu_mqtt import PrinterState


@pytest.mark.parametrize("printer_count", [50, 300])
def test_fleet_bootstrap_ack_and_ping(caplog, monkeypatch, printer_count):
    manager = ConnectionManager()
    states = {pid: PrinterState() for pid in range(1, printer_count + 1)}
    monkeypatch.setattr(route, "ws_manager", manager)
    monkeypatch.setattr(route, "authenticate_websocket_token", AsyncMock(return_value=(True, 7)))
    monkeypatch.setattr(route.printer_manager, "get_all_statuses", lambda: states)
    monkeypatch.setattr(route.printer_manager, "get_model", lambda pid: "P1S")
    monkeypatch.setattr(route.printer_manager, "get_drying_targets", lambda pid: {})
    monkeypatch.setattr(route.background_dispatch, "get_state", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(route.router)
    with caplog.at_level("INFO"), TestClient(app) as client, client.websocket_connect("/ws?token=synthetic") as ws:
        for pid in states:
            message = ws.receive_json()
            assert message["type"] == "printer_status" and message["printer_id"] == pid
        marker = ws.receive_json()
        assert marker["type"] == "initial_status_complete" and marker["printers"] == printer_count
        ack = {"type": "initial_status_applied", "bootstrap_id": marker["bootstrap_id"], "connect_ms": 15}
        if printer_count == 300:
            ack.update({"token_ms": 4, "socket_open_ms": 3, "first_status_ms": 2, "marker_to_cache_ms": 1})
        ws.send_json({"type": "initial_status_applied", "bootstrap_id": "wrong", "connect_ms": 1})
        ws.send_json(ack)
        ws.send_json(ack)
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    assert "WebSocket bootstrap applied:" in caplog.text
    assert caplog.text.count("WebSocket bootstrap applied:") == 1
    if printer_count == 300:
        assert "token_ms=4 socket_open_ms=3 first_status_ms=2 marker_to_cache_ms=1" in caplog.text
    else:
        assert "token_ms=None socket_open_ms=None first_status_ms=None marker_to_cache_ms=None" in caplog.text
    assert not manager.active_connections and not manager._outboxes


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, 300_001, 10**1000, "12", True])
def test_bootstrap_ack_rejects_untrusted_timing_values(caplog, monkeypatch, invalid):
    manager = ConnectionManager()
    monkeypatch.setattr(route, "ws_manager", manager)
    monkeypatch.setattr(route, "authenticate_websocket_token", AsyncMock(return_value=(True, 7)))
    monkeypatch.setattr(route.printer_manager, "get_all_statuses", lambda: {})
    monkeypatch.setattr(route.background_dispatch, "get_state", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(route.router)
    with caplog.at_level("INFO"), TestClient(app) as client, client.websocket_connect("/ws?token=synthetic") as ws:
        marker = ws.receive_json()
        assert marker["type"] == "initial_status_complete"
        ws.send_json(
            {
                "type": "initial_status_applied",
                "bootstrap_id": marker["bootstrap_id"],
                "connect_ms": invalid,
                "token_ms": invalid,
                "socket_open_ms": invalid,
                "first_status_ms": invalid,
                "marker_to_cache_ms": invalid,
            }
        )
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
    assert (
        "client_connect_ms=None token_ms=None socket_open_ms=None first_status_ms=None marker_to_cache_ms=None"
        in caplog.text
    )


def test_legacy_client_can_disconnect_without_ack(caplog, monkeypatch):
    manager = ConnectionManager()
    monkeypatch.setattr(route, "ws_manager", manager)
    monkeypatch.setattr(route, "authenticate_websocket_token", AsyncMock(return_value=(True, 7)))
    monkeypatch.setattr(route.printer_manager, "get_all_statuses", lambda: {})
    monkeypatch.setattr(route.background_dispatch, "get_state", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(route.router)
    with caplog.at_level("INFO"), TestClient(app) as client, client.websocket_connect("/ws?token=synthetic") as ws:
        assert ws.receive_json()["type"] == "initial_status_complete"
    assert "WebSocket bootstrap applied:" not in caplog.text
    assert not manager.active_connections and not manager._outboxes


def test_invalid_token_never_enters_fanout(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    manager = ConnectionManager()
    monkeypatch.setattr(route, "ws_manager", manager)
    monkeypatch.setattr(route, "authenticate_websocket_token", AsyncMock(return_value=(False, None)))
    app = FastAPI()
    app.include_router(route.router)
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws"):
        pass
    assert exc.value.code == 4401
    assert not manager._outboxes
