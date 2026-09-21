"""Telegram bot auth middleware - checks chat authorization on every update."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from backend.app.i18n import escape_md, get_language, t

logger = logging.getLogger(__name__)

NS = "telegram_ui"

# Cache to avoid spamming "disabled" / "pending" messages. Keyed by (bot
# account, chat) because the same person is a different chat in every bot
# they start — one bot having told them they are disabled says nothing about
# the other.
_notified_chats: set[tuple[int, int]] = set()


class TelegramAuthMiddleware(BaseMiddleware):
    """Middleware that checks if the chat is authorized before processing updates."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Extract chat_id from event
        chat_id = self._get_chat_id(event)
        if chat_id is None:
            return await handler(event, data)

        # WHICH bot this arrived on. A chat belongs to one bot (m180) and the
        # same person is a different chat in each bot they start, so the pair
        # is the identity — never the chat id alone.
        from backend.app.services.telegram_bot import provider_id_for_bot

        bot_account_id = getattr(getattr(event, "bot", None), "id", None)
        provider_id = provider_id_for_bot(bot_account_id) if bot_account_id is not None else None
        if provider_id is None:
            # An update from a bot this process does not run: a session being
            # torn down, or a bot removed between the long poll and here.
            logger.error("Telegram update from an unknown bot account %s - ignored", bot_account_id)
            return
        notified_key = (bot_account_id, chat_id)

        # Look up chat in DB
        from sqlalchemy import select

        from backend.app.core.database import async_session
        from backend.app.models.telegram_chat import TelegramChat

        async with async_session() as db:
            result = await db.execute(
                select(TelegramChat).where(
                    TelegramChat.provider_id == provider_id,
                    TelegramChat.chat_id == chat_id,
                )
            )
            tg_chat = result.scalar_one_or_none()

            if tg_chat is None:
                # Check if we should auto-register
                should_register = await self._should_auto_register(db, provider_id)
                if should_register:
                    tg_chat = await self._auto_register(db, event, chat_id, provider_id)
                    lang = await get_language()
                    await self._reply(event, t(lang, NS, "auth.registered"))
                    return  # Don't process further - chat is disabled
                # Unknown chat, silently ignore
                return

            if not tg_chat.is_active:
                if notified_key not in _notified_chats:
                    _notified_chats.add(notified_key)
                    lang = await get_language()
                    await self._reply(event, t(lang, NS, "auth.disabled"))
                return

            if tg_chat.group_id is None:
                if notified_key not in _notified_chats:
                    _notified_chats.add(notified_key)
                    lang = await get_language()
                    await self._reply(event, t(lang, NS, "auth.pending_setup"))
                return

        # Chat is authorized - attach to handler data and proceed
        data["tg_chat"] = tg_chat
        return await handler(event, data)

    @staticmethod
    def _get_chat_id(event: TelegramObject) -> int | None:
        if isinstance(event, Message) and event.chat:
            return event.chat.id
        if isinstance(event, CallbackQuery) and event.message and event.message.chat:
            return event.message.chat.id
        return None

    @staticmethod
    async def _should_auto_register(db, provider_id: int) -> bool:
        """Is auto-registration allowed — this bot has no chat yet, OR registration is open.

        "No chat yet" is asked per bot: a newly added bot is somebody's first
        setup even on a farm whose other bot has had chats for a year, and
        whoever presses Start on it first is the operator standing in front of
        it. The ``telegram_registration_open`` switch stays global — it is the
        operator's intent, not a property of one bot.
        """
        from sqlalchemy import func, select

        from backend.app.models.settings import Settings
        from backend.app.models.telegram_chat import TelegramChat

        count = (
            await db.execute(select(func.count(TelegramChat.id)).where(TelegramChat.provider_id == provider_id))
        ).scalar() or 0
        if count == 0:
            return True

        # Check setting
        result = await db.execute(select(Settings.value).where(Settings.key == "telegram_registration_open"))
        val = result.scalar_one_or_none()
        return val == "true"

    @staticmethod
    async def _auto_register(db, event: TelegramObject, chat_id: int, provider_id: int):
        """Create a disabled TelegramChat record for auto-registration.

        Bound to the bot the message arrived on (m180), which the caller
        resolved from ``event.bot.id``. A person writing to a second bot gets
        a second row: it is a different chat in Telegram, and each bot
        carries its own authorization, scope and notification opt-ins.
        """
        from backend.app.models.telegram_chat import TelegramChat

        # Extract label from Telegram
        label = None
        if isinstance(event, Message) and event.chat:
            chat = event.chat
            label = chat.title or chat.full_name or chat.username
        elif isinstance(event, CallbackQuery) and event.message and event.message.chat:
            chat = event.message.chat
            label = chat.title or chat.full_name or chat.username

        tg_chat = TelegramChat(
            chat_id=chat_id,
            provider_id=provider_id,
            label=label,
            is_active=False,
            group_id=None,
            user_id=None,
        )
        db.add(tg_chat)
        await db.commit()
        await db.refresh(tg_chat)
        logger.info("Auto-registered Telegram chat %s (label=%s, provider=%s)", chat_id, label, provider_id)

        # Notify frontend via WebSocket
        from backend.app.core.websocket import ws_manager

        await ws_manager.broadcast(
            {
                "type": "telegram_chat_registered",
                "data": {
                    "id": tg_chat.id,
                    "chat_id": tg_chat.chat_id,
                    "label": tg_chat.label,
                },
            }
        )

        return tg_chat

    @staticmethod
    async def _reply(event: TelegramObject, raw_text: str) -> None:
        """Send a reply to the event.

        Takes raw (un-escaped) text and escapes only for the path that uses
        the bot's MarkdownV2 default. CallbackQuery.answer renders the alert
        body as plain text — pre-escaping there would surface visible
        backslashes to the user.
        """
        try:
            if isinstance(event, Message):
                await event.answer(escape_md(raw_text))
            elif isinstance(event, CallbackQuery):
                await event.answer(raw_text, show_alert=True)
        except Exception as e:
            logger.warning("Failed to reply to unauthorized chat: %s", e)
