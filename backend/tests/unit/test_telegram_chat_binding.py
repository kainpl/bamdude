"""A Telegram chat belongs to the bot it wrote to (m180) — the middleware's half.

Registration binds the new chat to the provider row the LIVE poller was built
from; a chat that writes to a different bot than it is bound to is re-bound on
contact; with no running bot there is nothing to bind to and nothing is
registered.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.notification import NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.services import telegram_bot as tb
from backend.app.services.telegram_handlers.auth_middleware import TelegramAuthMiddleware

pytestmark = pytest.mark.unit


async def _bot_row(db_session, name: str) -> NotificationProvider:
    row = NotificationProvider(name=name, provider_type="telegram", enabled=True, config='{"bot_token": "1:A"}')
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_registration_binds_the_chat_to_the_running_bot(db_session, monkeypatch):
    bot = await _bot_row(db_session, "Bot A")
    monkeypatch.setattr(tb, "_bots", {bot.id: MagicMock()})

    with patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        chat = await TelegramAuthMiddleware._auto_register(db_session, MagicMock(), 4242)

    assert chat is not None
    assert chat.provider_id == bot.id
    assert chat.is_active is False


@pytest.mark.asyncio
async def test_without_a_running_bot_nothing_is_registered(db_session, monkeypatch):
    monkeypatch.setattr(tb, "_bots", {})

    chat = await TelegramAuthMiddleware._auto_register(db_session, MagicMock(), 4242)

    assert chat is None
    assert (await db_session.execute(select(TelegramChat))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_chat_that_writes_to_another_bot_is_re_bound_to_it(db_session, monkeypatch):
    old = await _bot_row(db_session, "Old bot")
    new = await _bot_row(db_session, "New bot")
    chat = TelegramChat(chat_id=7, provider_id=old.id, is_active=True)
    db_session.add(chat)
    await db_session.commit()

    # Plain ints BEFORE expire_all(): an expired attribute read outside an
    # await is a MissingGreenlet, not a useful failure.
    chat_row_id, new_id = chat.id, new.id
    monkeypatch.setattr(tb, "_bots", {new_id: MagicMock()})
    await TelegramAuthMiddleware._rebind_to_running_bot(db_session, chat)

    db_session.expire_all()
    assert (await db_session.get(TelegramChat, chat_row_id)).provider_id == new_id


@pytest.mark.asyncio
async def test_the_same_bot_leaves_the_binding_alone(db_session, monkeypatch):
    bot = await _bot_row(db_session, "Bot")
    chat = TelegramChat(chat_id=8, provider_id=bot.id, is_active=True)
    db_session.add(chat)
    await db_session.commit()
    commit = AsyncMock(wraps=db_session.commit)

    monkeypatch.setattr(tb, "_bots", {bot.id: MagicMock()})
    monkeypatch.setattr(db_session, "commit", commit)
    await TelegramAuthMiddleware._rebind_to_running_bot(db_session, chat)

    commit.assert_not_awaited()
    assert chat.provider_id == bot.id


@pytest.mark.asyncio
async def test_no_running_bot_re_binds_nothing(db_session, monkeypatch):
    bot = await _bot_row(db_session, "Bot")
    chat = TelegramChat(chat_id=9, provider_id=bot.id, is_active=True)
    db_session.add(chat)
    await db_session.commit()

    monkeypatch.setattr(tb, "_bots", {})
    await TelegramAuthMiddleware._rebind_to_running_bot(db_session, chat)

    assert chat.provider_id == bot.id


# ---------------------------------------------------------------------------
# Through the middleware itself: the call sites, not only the helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_session(test_engine):
    """The middleware opens its own session — point it at the test database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


def _message_from(chat_id: int):
    """A real aiogram ``Message`` (the middleware branches on ``isinstance``), unvalidated."""
    from aiogram.types import Chat, Message

    return Message.model_construct(message_id=1, date=0, chat=Chat.model_construct(id=chat_id, type="private"))


@pytest.mark.asyncio
async def test_a_disabled_chat_writing_to_the_new_bot_is_moved_before_it_is_told_it_is_disabled(
    db_session, patched_session, monkeypatch
):
    """The re-bind sits ABOVE the is_active gate: a chat still waiting for activation
    follows the bot it wrote to, and only then hears that it is disabled."""
    old = await _bot_row(db_session, "Old bot")
    new = await _bot_row(db_session, "New bot")
    db_session.add(TelegramChat(chat_id=77, provider_id=old.id, is_active=False))
    await db_session.commit()
    new_id = new.id

    monkeypatch.setattr(tb, "_bots", {new_id: MagicMock()})
    handler = AsyncMock()
    reply = AsyncMock()
    with (
        patch.object(TelegramAuthMiddleware, "_reply", reply),
        patch("backend.app.services.telegram_handlers.auth_middleware.get_language", AsyncMock(return_value="en")),
    ):
        await TelegramAuthMiddleware()(handler, _message_from(77), {})

    handler.assert_not_awaited()
    reply.assert_awaited_once()
    db_session.expire_all()
    row = (await db_session.execute(select(TelegramChat).where(TelegramChat.chat_id == 77))).scalar_one()
    assert row.provider_id == new_id
    assert row.is_active is False


@pytest.mark.asyncio
async def test_with_no_running_bot_an_unknown_chat_is_neither_registered_nor_answered(
    db_session, patched_session, monkeypatch
):
    monkeypatch.setattr(tb, "_bots", {})
    handler = AsyncMock()
    reply = AsyncMock()
    with (
        patch.object(TelegramAuthMiddleware, "_reply", reply),
        patch("backend.app.services.telegram_handlers.auth_middleware.get_language", AsyncMock(return_value="en")),
    ):
        await TelegramAuthMiddleware()(handler, _message_from(78), {})

    handler.assert_not_awaited()
    reply.assert_not_awaited()
    assert (await db_session.execute(select(TelegramChat))).scalars().all() == []
