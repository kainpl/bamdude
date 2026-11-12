"""Private inbox fanout belongs to browser sessions, never key/token owners."""

import asyncio
import json

import pytest

from backend.app.core.auth import (
    authenticate_websocket_authority,
    create_websocket_token,
)
from backend.app.core.auth_lifetime import FrozenAuthority, capture_long_lived
from backend.app.core.websocket import ConnectionManager, ws_manager
from backend.app.services import long_lived_tokens, notification_inbox
from backend.tests.auth_helpers import mint_test_authority

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Socket:
    def __init__(self):
        self.sent = []
        self.global_received = asyncio.Event()

    async def accept(self):
        pass

    async def send_text(self, data):
        message = json.loads(data)
        self.sent.append(message)
        if message.get("type") == "ck1_global":
            self.global_received.set()

    async def close(self, **kwargs):
        pass


async def test_ordinary_and_staged_inbox_exclude_key_and_long_lived_websockets(async_client, db_session):
    browser_response = await async_client.post("/api/v1/auth/ws-token")
    assert browser_response.status_code == 200, browser_response.text
    key_response = await async_client.post("/api/v1/api-keys/", json={"name": "status-only", "can_read_status": True})
    assert key_response.status_code == 200, key_response.text
    key_ws = await async_client.post(
        "/api/v1/auth/ws-token", headers={"Authorization": "", "X-API-Key": key_response.json()["key"]}
    )
    assert key_ws.status_code == 200, key_ws.text

    browser_authority = await mint_test_authority(db_session)
    created = await long_lived_tokens.create_token(
        db_session, user_id=browser_authority.user_id, name="camera", expires_in_days=1, authority=browser_authority
    )
    # LL-derived ephemerals retain their real token/owner binding. Even if a
    # server consumer mints a WS derivative, it cannot become a person's inbox.
    ll_ws = await create_websocket_token(authority=capture_long_lived(created.record))
    tokens = (browser_response.json()["token"], key_ws.json()["token"], ll_ws)
    sockets = [Socket() for _ in tokens]
    try:
        captures = []
        for token, socket, expected_kind in zip(tokens, sockets, ("session", "key", "long_lived"), strict=True):
            resolved = await authenticate_websocket_authority(token)
            assert resolved is not None
            capture, personal_id = resolved
            captures.append(capture)
            assert capture.kind == expected_kind
            assert personal_id == (capture.user_id if expected_kind == "session" else None)
            await ws_manager.connect(socket, personal_id, account_identity=capture.account_identity, authority=capture)
            assert ws_manager._authority_by_conn[socket] is capture
        assert len({capture.account_identity for capture in captures}) == 1

        recipients = await notification_inbox.deliver(
            db_session, event_type="print_failed", title="CK1 private print", message="private filename detail"
        )
        assert captures[0].user_id in recipients
        staged = await notification_inbox.stage_admin_event(
            db_session,
            event_type="credentials_reissue_required",
            episode_id="ck1-personal-channel",
            title="CK1 private reissue",
            message="private credential counts",
        )
        await db_session.commit()
        assert any(item.user_id == captures[0].user_id for item in staged.items)
        await notification_inbox.dispatch_staged(staged)

        # Same FIFO writer: receiving this final global event proves all earlier
        # per-user enqueues have drained, without a timing-based absence check.
        await ws_manager.broadcast_to_user(None, {"type": "ck1_global"})
        await asyncio.wait_for(asyncio.gather(*(socket.global_received.wait() for socket in sockets)), 5)
        titles = [
            [message["data"]["item"]["title"] for message in socket.sent if message.get("type") == "inbox_item"]
            for socket in sockets
        ]
        assert titles == [["CK1 private print", "CK1 private reissue"], [], []]
    finally:
        await asyncio.gather(*(ws_manager.disconnect(socket) for socket in sockets))
    assert all(socket not in ws_manager._authority_by_conn for socket in sockets)


@pytest.mark.parametrize("kind", ["key", "long_lived"])
async def test_manager_refuses_non_session_personal_tag_even_with_explicit_owner_id(kind):
    manager = ConnectionManager()
    socket = Socket()
    capture = FrozenAuthority(kind, "owner-identity", 7, key_identity="key", token_identity="ll")
    await manager.connect(socket, 7, account_identity=capture.account_identity, authority=capture)
    try:
        assert manager._authority_by_conn[socket] is capture
        assert manager._user_by_conn[socket] is None
        # Defense at delivery too: even an erroneous Python tag mutation cannot
        # turn a stored independent credential into a person's live channel.
        manager._user_by_conn[socket] = 7
        await manager.broadcast_to_user(7, {"type": "private_ordinary"})
        await manager.broadcast_to_user(
            7, {"type": "private_staged"}, expected_account_identity=capture.account_identity
        )
        await manager.broadcast({"type": "ck1_global"})
        await asyncio.wait_for(socket.global_received.wait(), 5)
        assert socket.sent == [{"type": "ck1_global"}]
    finally:
        await manager.shutdown()
    assert socket not in manager._authority_by_conn
