"""Telegram bot auth middleware - checks chat authorization on every update."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from backend.app.i18n import escape_md, get_language, t

logger = logging.getLogger(__name__)

NS = "telegram_ui"

# Cache to avoid spamming "disabled" / "pending" messages
_notified_chats: set[int] = set()


def clear_chat_cache() -> None:
    """Clear the notified chats cache (call when chats are updated)."""
    _notified_chats.clear()


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

        # Look up chat in DB
        from sqlalchemy import select

        from backend.app.core.database import async_session
        from backend.app.models.telegram_chat import TelegramChat

        async with async_session() as db:
            result = await db.execute(select(TelegramChat).where(TelegramChat.chat_id == chat_id))
            tg_chat = result.scalar_one_or_none()

            if tg_chat is None:
                # Check if we should auto-register
                should_register = await self._should_auto_register(db)
                if should_register:
                    tg_chat = await self._auto_register(db, event, chat_id)
                    lang = await get_language()
                    await self._reply(event, escape_md(t(lang, NS, "auth.registered")))
                    return  # Don't process further - chat is disabled
                # Unknown chat, silently ignore
                return

            if not tg_chat.is_active:
                if chat_id not in _notified_chats:
                    _notified_chats.add(chat_id)
                    lang = await get_language()
                    await self._reply(event, escape_md(t(lang, NS, "auth.disabled")))
                return

            if tg_chat.group_id is None:
                if chat_id not in _notified_chats:
                    _notified_chats.add(chat_id)
                    lang = await get_language()
                    await self._reply(event, escape_md(t(lang, NS, "auth.pending_setup")))
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
    async def _should_auto_register(db) -> bool:
        """Check if auto-registration is allowed (table empty OR registration open)."""
        from sqlalchemy import func, select

        from backend.app.models.settings import Settings
        from backend.app.models.telegram_chat import TelegramChat

        # Always allow if table is empty (first setup)
        count = (await db.execute(select(func.count(TelegramChat.id)))).scalar() or 0
        if count == 0:
            return True

        # Check setting
        result = await db.execute(select(Settings.value).where(Settings.key == "telegram_registration_open"))
        val = result.scalar_one_or_none()
        return val == "true"

    @staticmethod
    async def _auto_register(db, event: TelegramObject, chat_id: int):
        """Create a disabled TelegramChat record for auto-registration."""
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
            label=label,
            is_active=False,
            group_id=None,
            user_id=None,
        )
        db.add(tg_chat)
        await db.commit()
        await db.refresh(tg_chat)
        logger.info("Auto-registered Telegram chat %s (label=%s)", chat_id, label)

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
    async def _reply(event: TelegramObject, text: str) -> None:
        """Send a reply to the event."""
        try:
            if isinstance(event, Message):
                await event.answer(text)
            elif isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
        except Exception as e:
            logger.warning("Failed to reply to unauthorized chat: %s", e)
