"""Telegram keeps its own grants and immutable optional linked-account attribution."""

import pytest
from fastapi import HTTPException
from sqlalchemy import delete

from backend.app.models.group import Group
from backend.app.models.notification import NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.models.user import User
from backend.app.services.domain_authority import capture_chat_writer, fresh, guard

pytestmark = pytest.mark.asyncio


async def actor(db, *, linked=True):
    group = Group(name="chat-writer", permissions=["queue:create", "library:read", "printers:control"])
    provider = NotificationProvider(name="chat-writer", provider_type="telegram", enabled=True, config="{}")
    user = User(username="chat-linked", role="user") if linked else None
    db.add_all([group, provider] + ([user] if user else []))
    await db.flush()
    chat = TelegramChat(
        chat_id=991,
        provider_id=provider.id,
        group_id=group.id,
        user_id=user.id if user else None,
        is_active=True,
        printer_ids=[12],
    )
    db.add(chat)
    await db.commit()
    authority = await capture_chat_writer(db, chat)
    await db.commit()
    return authority, chat, group, user


async def test_linked_user_does_not_supply_browser_or_group_authority(db_session):
    authority, _, _, user = await actor(db_session)
    user.session_generation += 1
    await db_session.commit()
    _, principal = await guard(db_session, authority, permissions=("queue:create",), printer_id=12)
    assert principal.user.id == user.id
    assert principal.capture.kind == "telegram_chat"
    assert principal.capture.linked_account.generation is None


async def test_chat_revoke_and_printer_scope_are_current(db_session):
    authority, chat, group, _ = await actor(db_session)
    with pytest.raises(HTTPException):
        await fresh(db_session, authority, permissions=("queue:create",), printer_id=13)
    group.permissions = ["library:read"]
    await db_session.commit()
    with pytest.raises(HTTPException):
        await guard(db_session, authority, permissions=("queue:create",), printer_id=12)
    await db_session.rollback()
    group.permissions = ["queue:create"]
    await db_session.commit()
    # Captured library-read prerequisite is rechecked by the later dispatch writer too.
    with pytest.raises(HTTPException):
        await fresh(db_session, authority, permissions=("queue:create",), printer_id=12)


async def test_recycled_linked_user_id_cannot_receive_old_work(db_session):
    authority, _, _, user = await actor(db_session)
    old_id = user.id
    await db_session.execute(delete(User).where(User.id == old_id))
    await db_session.commit()
    db_session.add(User(id=old_id, username="replacement", role="admin"))
    await db_session.commit()
    with pytest.raises(HTTPException):
        await guard(db_session, authority, permissions=("queue:create",))


async def test_unlinked_chat_preserves_chat_policy_without_fabricated_user(db_session):
    authority, _, _, _ = await actor(db_session, linked=False)
    _, principal = await guard(db_session, authority, permissions=("printers:control",), printer_id=12)
    assert principal is None
