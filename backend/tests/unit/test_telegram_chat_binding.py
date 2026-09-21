"""A Telegram chat belongs to the bot it wrote to (m180) — the middleware's half.

Telegram's private chat id is the person's user id, the same number in every
bot they start, so a chat is identified by the PAIR (bot, chat id): the
middleware resolves the provider from the bot the update arrived on, looks the
row up by the pair, and registers a new row under that bot. The same person
writing to a second bot is a second chat, with its own authorization.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.notification import NotificationProvider
from backend.app.models.telegram_chat import TelegramChat
from backend.app.services import telegram_bot as tb
from backend.app.services.telegram_handlers import auth_middleware as mw
from backend.app.services.telegram_handlers.auth_middleware import TelegramAuthMiddleware

pytestmark = pytest.mark.unit


async def _bot_row(db_session, name: str, token: str = "1:A") -> NotificationProvider:
    row = NotificationProvider(name=name, provider_type="telegram", enabled=True, config=f'{{"bot_token": "{token}"}}')
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _fake_bot(account_id: int) -> MagicMock:
    bot = MagicMock()
    bot.id = account_id
    return bot


def _message_from(chat_id: int, bot_account_id: int):
    """A real aiogram ``Message`` (the middleware branches on ``isinstance``), bound to a bot."""
    from aiogram.types import Chat, Message

    message = Message.model_construct(message_id=1, date=0, chat=Chat.model_construct(id=chat_id, type="private"))
    return message.as_(_fake_bot(bot_account_id))


@pytest.fixture
def patched_session(test_engine):
    """The middleware opens its own session — point it at the test database."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


@pytest.fixture(autouse=True)
def quiet_notice_cache():
    """The "you are disabled" cache is module state — never carried between tests."""
    mw._notified_chats.clear()
    yield
    mw._notified_chats.clear()


def _language_and_reply():
    reply = AsyncMock()
    return (
        reply,
        patch.object(TelegramAuthMiddleware, "_reply", reply),
        patch("backend.app.services.telegram_handlers.auth_middleware.get_language", AsyncMock(return_value="en")),
    )


@pytest.mark.asyncio
async def test_registration_binds_the_chat_to_the_bot_it_wrote_to(db_session, monkeypatch):
    bot = await _bot_row(db_session, "Bot A")
    monkeypatch.setattr(tb, "_bot_ids", {5001: bot.id})

    with patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        chat = await TelegramAuthMiddleware._auto_register(db_session, MagicMock(), 4242, bot.id)

    assert chat.provider_id == bot.id
    assert chat.is_active is False


@pytest.mark.asyncio
async def test_an_update_from_an_unknown_bot_registers_nothing_and_says_nothing(
    db_session, patched_session, monkeypatch
):
    """A bot this process does not run: a session torn down, a provider removed."""
    await _bot_row(db_session, "Bot A")
    monkeypatch.setattr(tb, "_bot_ids", {})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang:
        await TelegramAuthMiddleware()(handler, _message_from(4242, 5001), {})

    handler.assert_not_awaited()
    reply.assert_not_awaited()
    assert (await db_session.execute(select(TelegramChat))).scalars().all() == []


@pytest.mark.asyncio
async def test_the_same_person_writing_to_a_second_bot_becomes_a_second_chat(db_session, patched_session, monkeypatch):
    """One chat id, two bots, two rows — and the first bot's row is untouched."""
    first = await _bot_row(db_session, "First bot", token="1:A")
    second = await _bot_row(db_session, "Second bot", token="2:B")
    db_session.add(TelegramChat(chat_id=77, provider_id=first.id, is_active=True, group_id=None))
    await db_session.commit()
    first_id, second_id = first.id, second.id

    monkeypatch.setattr(tb, "_bot_ids", {5001: first_id, 5002: second_id})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang, patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        await TelegramAuthMiddleware()(handler, _message_from(77, 5002), {})

    handler.assert_not_awaited(), "a freshly registered chat is disabled, so nothing is handled"
    reply.assert_awaited_once()
    db_session.expire_all()
    rows = (await db_session.execute(select(TelegramChat).order_by(TelegramChat.provider_id))).scalars().all()
    assert [(row.provider_id, row.chat_id, row.is_active) for row in rows] == [
        (first_id, 77, True),
        (second_id, 77, False),
    ]


@pytest.mark.asyncio
async def test_writing_to_the_same_bot_twice_is_one_row(db_session, patched_session, monkeypatch):
    bot = await _bot_row(db_session, "Bot")
    bot_id = bot.id
    monkeypatch.setattr(tb, "_bot_ids", {5001: bot_id})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang, patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        await TelegramAuthMiddleware()(handler, _message_from(88, 5001), {})
        await TelegramAuthMiddleware()(handler, _message_from(88, 5001), {})

    db_session.expire_all()
    rows = (await db_session.execute(select(TelegramChat))).scalars().all()
    assert len(rows) == 1 and rows[0].provider_id == bot_id


@pytest.mark.asyncio
async def test_a_new_bot_registers_its_first_chat_even_when_another_bot_has_chats(
    db_session, patched_session, monkeypatch
):
    """ "First setup" is asked per bot: a bot with no chats yet has nobody to ask.

    Registration is CLOSED here, which is the point — the global switch stays
    the operator's intent, and the empty-table allowance that makes the very
    first setup possible now follows each bot.
    """
    from backend.app.models.settings import Settings

    old = await _bot_row(db_session, "Old bot", token="1:A")
    new = await _bot_row(db_session, "New bot", token="2:B")
    db_session.add(TelegramChat(chat_id=10, provider_id=old.id, is_active=True))
    db_session.add(Settings(key="telegram_registration_open", value="false"))
    await db_session.commit()
    new_id = new.id

    monkeypatch.setattr(tb, "_bot_ids", {5002: new_id})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang, patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()):
        await TelegramAuthMiddleware()(handler, _message_from(99, 5002), {})

    reply.assert_awaited_once()
    db_session.expire_all()
    rows = (await db_session.execute(select(TelegramChat).where(TelegramChat.provider_id == new_id))).scalars().all()
    assert [row.chat_id for row in rows] == [99]


@pytest.mark.asyncio
async def test_a_closed_registration_still_turns_an_unknown_chat_away(db_session, patched_session, monkeypatch):
    from backend.app.models.settings import Settings

    bot = await _bot_row(db_session, "Bot")
    db_session.add(TelegramChat(chat_id=10, provider_id=bot.id, is_active=True))
    db_session.add(Settings(key="telegram_registration_open", value="false"))
    await db_session.commit()
    bot_id = bot.id

    monkeypatch.setattr(tb, "_bot_ids", {5001: bot_id})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang:
        await TelegramAuthMiddleware()(handler, _message_from(4242, 5001), {})

    handler.assert_not_awaited()
    reply.assert_not_awaited(), "an unknown chat is ignored, not answered"
    db_session.expire_all()
    assert (
        await db_session.execute(select(TelegramChat).where(TelegramChat.chat_id == 4242))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_the_disabled_notice_is_remembered_per_bot(db_session, patched_session, monkeypatch):
    """Told once by each bot, not once for the person: they are different chats."""
    first = await _bot_row(db_session, "First bot", token="1:A")
    second = await _bot_row(db_session, "Second bot", token="2:B")
    db_session.add(TelegramChat(chat_id=55, provider_id=first.id, is_active=False))
    db_session.add(TelegramChat(chat_id=55, provider_id=second.id, is_active=False))
    await db_session.commit()

    monkeypatch.setattr(tb, "_bot_ids", {5001: first.id, 5002: second.id})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang:
        await TelegramAuthMiddleware()(handler, _message_from(55, 5001), {})
        await TelegramAuthMiddleware()(handler, _message_from(55, 5001), {})
        await TelegramAuthMiddleware()(handler, _message_from(55, 5002), {})

    assert reply.await_count == 2, "once per bot, and the repeat on the first bot stays silent"
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_active_chat_of_its_own_bot_reaches_the_handler(db_session, patched_session, monkeypatch):
    from backend.app.models.group import Group

    bot = await _bot_row(db_session, "Bot")
    group = Group(name="Ops", permissions=["printers:control"])
    db_session.add(group)
    await db_session.flush()
    db_session.add(TelegramChat(chat_id=66, provider_id=bot.id, is_active=True, group_id=group.id))
    await db_session.commit()

    monkeypatch.setattr(tb, "_bot_ids", {5001: bot.id})
    handler = AsyncMock()
    reply, p_reply, p_lang = _language_and_reply()

    with p_reply, p_lang:
        await TelegramAuthMiddleware()(handler, _message_from(66, 5001), {})

    handler.assert_awaited_once()
    assert handler.await_args.args[1]["tg_chat"].chat_id == 66
    reply.assert_not_awaited()
