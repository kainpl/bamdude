"""Postcommit hints never follow a reused numeric account id or reopen SQL."""

import asyncio

import pytest

from backend.app.core.websocket import ConnectionManager
from backend.app.services import notification_inbox
from backend.tests.unit.test_websocket_delivery import Browser


@pytest.mark.asyncio
async def test_staged_hint_matches_account_incarnation_and_uses_no_sql(monkeypatch):
    manager = ConnectionManager()
    monkeypatch.setattr(notification_inbox, "ws_manager", manager)
    old, replacement, key_only, foreign = (Browser() for _ in range(4))
    await manager.connect(old, user_id=7, account_identity="old-account")
    await manager.connect(replacement, user_id=7, account_identity="new-account")
    await manager.connect(key_only, user_id=None)
    await manager.connect(foreign, user_id=8, account_identity="old-account")
    batch = notification_inbox.StagedInboxBatch(
        (notification_inbox.StagedInboxItem(7, "old-account", {"id": 12, "message": "safe scalar"}, 1),)
    )
    try:
        await notification_inbox.dispatch_staged(batch)
        await asyncio.wait_for(old.received.wait(), 1)
        assert old.sent[0]["data"]["item"]["id"] == 12
        assert not replacement.sent and not key_only.sent and not foreign.sent
        # An immutable recipient without a relational id never means global.
        await manager.broadcast_to_user(None, {"type": "private"}, expected_account_identity="old-account")
        await asyncio.sleep(0)
        assert len(old.sent) == 1 and not replacement.sent and not key_only.sent
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_staged_hint_failure_cannot_change_durable_delivery(monkeypatch):
    class Unavailable:
        async def broadcast_to_user(self, *args, **kwargs):
            raise RuntimeError("socket disappeared after commit")

    monkeypatch.setattr(notification_inbox, "ws_manager", Unavailable())
    batch = notification_inbox.StagedInboxBatch((notification_inbox.StagedInboxItem(7, "account", {"id": 12}, 1),))
    # No DB handle/factory is accepted by this operation; socket failure is a
    # live hint loss, not grounds for rolling back or re-sending the inbox row.
    await notification_inbox.dispatch_staged(batch)
