"""Actual WS route bootstrap after durable key-scope retirement, on one loop."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.api.routes import websocket as route
from backend.app.core import auth, database, websocket
from backend.app.core.auth_lifetime import capture_key, utcnow
from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.api_key import APIKey
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import live_authority as module, live_projection
from backend.app.services.bambu_mqtt import PrinterState
from backend.tests.auth_helpers import bind_test_key

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class Peer:
    """Controlled ASGI transport; the app and DB stay on the pytest event loop."""

    def __init__(self, app, *, headers=(), token="derived-child"):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.messages = []
        self.incoming.put_nowait({"type": "websocket.connect"})
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "scheme": "ws",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": f"token={token}".encode(),
            "root_path": "",
            "headers": list(headers),
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "subprotocols": [],
        }
        self.task = asyncio.create_task(app(scope, self.incoming.get, self.send))

    async def send(self, message):
        self.messages.append(message)
        await self.outgoing.put(message)

    async def bootstrap(self):
        statuses = []
        async with asyncio.timeout(5):
            assert (await self.outgoing.get())["type"] == "websocket.accept"
            while True:
                message = await self.outgoing.get()
                assert message["type"] == "websocket.send"
                payload = json.loads(message["text"])
                if payload["type"] == "initial_status_complete":
                    return statuses, payload
                assert payload["type"] == "printer_status"
                statuses.append(payload)

    async def disconnect(self):
        if not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(asyncio.shield(self.task), 5)


@pytest.mark.parametrize(
    "headers",
    [
        [(b"x-api-key", b"")],
        [(b"x-api-key", b"bd_garbage")],
        [(b"x-api-key", b""), (b"authorization", b"Bearer browser-jwt")],
        [(b"authorization", b"Bearer bd_garbage")],
        [(b"authorization", b"Bearer bb_retired")],
    ],
)
async def test_token_only_upgrade_never_rescues_a_selected_key_with_a_valid_query_token(
    async_client, monkeypatch, headers
):
    minted = await async_client.post("/api/v1/auth/ws-token")
    assert minted.status_code == 200, minted.text
    token = minted.json()["token"]
    assert await auth.authenticate_websocket_authority(token, details=True) is not None
    # Real derived proof above; the selected header must refuse before either
    # consuming/resolving it or registering a live viewer, even outside HTTP.
    verifier = AsyncMock(side_effect=AssertionError("query resolver must not run"))
    monkeypatch.setattr(route, "authenticate_websocket_authority", verifier)
    app = FastAPI()
    app.include_router(route.router)
    peer = Peer(app, headers=headers, token=token)
    await asyncio.wait_for(asyncio.shield(peer.task), 5)
    assert peer.messages == [{"type": "websocket.close", "code": 4401, "reason": ""}]
    verifier.assert_not_awaited()


async def test_real_route_reboots_a_narrowed_key_with_only_current_scoped_statuses(test_engine, monkeypatch):
    runtime = module.LiveAuthority()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    manager = websocket.ConnectionManager()
    monkeypatch.setattr(module, "live_authority", runtime)
    monkeypatch.setattr(websocket, "live_authority", runtime)
    monkeypatch.setattr(live_projection, "live_authority", runtime)
    monkeypatch.setattr(route, "live_authority", runtime)
    monkeypatch.setattr(route, "ws_manager", manager)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(auth, "async_session", factory)
    auth_runtime.open_admission()
    async with factory() as db:
        owner = User(username="route-bootstrap-owner", role="admin", is_active=True)
        printers = [
            Printer(name=f"scope-{n}", serial_number=f"SCOPE-{n}", ip_address="127.0.0.1", access_code="test")
            for n in range(2)
        ]
        db.add_all([owner, *printers])
        await db.flush()
        ids = [printer.id for printer in printers]
        key = APIKey(name="route-key", key_hash="unused-derived-proof", key_prefix="bd_test", printer_ids=ids)
        await bind_test_key(db, key, owner)
        db.add(key)
        await db.commit()
        capture, key_id = capture_key(key), key.id
        identities = {printer.id: printer.connector_identity for printer in printers}
        principal = SimpleNamespace(
            capture=capture,
            user=SimpleNamespace(id=owner.id, _auth_expires=utcnow() + timedelta(minutes=10)),
        )

    # Only the already-verified derived-token boundary is stubbed. Registration,
    # post-accept admission and periodic scope checks read real durable rows.
    verifier = AsyncMock(return_value=principal)
    monkeypatch.setattr(route, "authenticate_websocket_authority", verifier)
    states = {pid: PrinterState() for pid in ids}
    monkeypatch.setattr(route.printer_manager, "get_all_statuses", lambda: states)
    monkeypatch.setattr(route.printer_manager, "get_status", states.get)
    monkeypatch.setattr(route.printer_manager, "get_model", lambda _pid: "P1S")
    monkeypatch.setattr(route.printer_manager, "get_drying_targets", lambda _pid: {})
    monkeypatch.setattr(route.background_dispatch, "get_state", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(route.router)
    peers = []
    try:
        old = Peer(app)
        peers.append(old)
        statuses, marker = await old.bootstrap()
        assert [item["printer_id"] for item in statuses] == ids
        assert marker["printers"] == 2 and marker["bootstrap_id"]
        assert all(
            route.printer_manager.record_identity_snapshot()[pid] == identity for pid, identity in identities.items()
        )
        assert len(runtime.handles) == 1
        old_handle = next(iter(runtime.handles))
        async with factory() as db:
            await db.execute(update(APIKey).where(APIKey.id == key_id).values(printer_ids=[ids[0]]))
            await db.commit()
        await asyncio.wait_for(old_handle.stop.wait(), 5)
        assert old_handle.code == 1013
        await asyncio.wait_for(asyncio.shield(old.task), 5)
        assert old.messages[-1] == {"type": "websocket.close", "code": 1013, "reason": ""}

        fresh = Peer(app)
        peers.append(fresh)
        statuses, new_marker = await fresh.bootstrap()
        assert [item["printer_id"] for item in statuses] == [ids[0]]
        assert new_marker["printers"] == 1 and new_marker["bootstrap_id"] != marker["bootstrap_id"]
        assert next(iter(runtime.handles)).entitlements.printers == frozenset({ids[0]})
        # Real route's explicit status-request path must retain the same ceiling.
        await fresh.incoming.put(
            {"type": "websocket.receive", "text": json.dumps({"type": "get_status", "printer_id": ids[1]})}
        )
        await fresh.incoming.put({"type": "websocket.receive", "text": json.dumps({"type": "ping"})})
        async with asyncio.timeout(5):
            reply = await fresh.outgoing.get()
        assert json.loads(reply["text"]) == {"type": "pong"}
        assert not [
            message
            for message in fresh.messages
            if message["type"] == "websocket.send" and json.loads(message["text"]).get("printer_id") == ids[1]
        ]
        assert verifier.call_count == 2
        verifier.assert_awaited_with("derived-child", details=True)
    finally:
        for peer in peers:
            await peer.disconnect()
        await manager.shutdown()
        assert await runtime.shutdown(timeout=3)
        assert not runtime.handles and not runtime.tasks and not runtime.cleanups
