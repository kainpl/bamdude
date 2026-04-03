"""Telegram bot service using aiogram 3.x.

Manages bot lifecycle, polling, and provides send methods for notifications.
Bot token is read from the first Telegram notification provider in DB.
"""

import asyncio
import logging
from io import BytesIO

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)

# Singleton
_bot: Bot | None = None
_dispatcher: Dispatcher | None = None
_polling_task: asyncio.Task | None = None


def get_bot() -> Bot | None:
    """Get the active bot instance."""
    return _bot


def get_dispatcher() -> Dispatcher | None:
    """Get the dispatcher instance."""
    return _dispatcher


async def _get_bot_token() -> str | None:
    """Read bot token from the first enabled Telegram notification provider."""
    from backend.app.core.database import async_session
    from backend.app.models.notification import NotificationProvider

    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(
            select(NotificationProvider)
            .where(
                NotificationProvider.provider_type == "telegram",
                NotificationProvider.enabled == True,  # noqa: E712
            )
            .limit(1)
        )
        provider = result.scalar_one_or_none()

    if not provider or not provider.config:
        return None

    import json

    config = provider.config
    if isinstance(config, str):
        config = json.loads(config)
    return config.get("bot_token")


async def start_telegram_bot() -> None:
    """Start the Telegram bot polling in background."""
    global _bot, _dispatcher, _polling_task

    token = await _get_bot_token()
    if not token:
        print("[TG-BOT] No Telegram bot token configured — bot not started")
        return
    print(f"[TG-BOT] Token found: {token[:10]}...")

    # Register handlers
    from backend.app.services.telegram_handlers.start import router as start_router
    from backend.app.services.telegram_handlers.printers import router as printers_router
    from backend.app.services.telegram_handlers.auth_middleware import TelegramAuthMiddleware

    _dispatcher = Dispatcher()
    _dispatcher.message.middleware(TelegramAuthMiddleware())
    _dispatcher.callback_query.middleware(TelegramAuthMiddleware())
    _dispatcher.include_router(start_router)
    _dispatcher.include_router(printers_router)

    _bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))

    # Verify token & register commands
    try:
        me = await _bot.get_me()
        print(f"[TG-BOT] Started: @{me.username} ({me.full_name})")
        logger.info("Telegram bot started: @%s (%s)", me.username, me.full_name)

        # Register bot commands (menu button in Telegram)
        await _register_commands()
    except Exception as e:
        logger.error("Failed to start Telegram bot: %s", e)
        _bot = None
        _dispatcher = None
        return

    # Start polling in background
    print("[TG-BOT] Starting polling...")
    _polling_task = asyncio.create_task(_run_polling())


async def _register_commands() -> None:
    """Register bot commands for the Telegram menu button."""
    from aiogram.types import BotCommand

    from backend.app.i18n import t, get_language

    lang = await get_language()
    NS = "telegram_ui"

    commands = [
        BotCommand(command="start", description=t(lang, NS, "commands.start")),
        BotCommand(command="status", description=t(lang, NS, "commands.status")),
        BotCommand(command="help", description=t(lang, NS, "commands.help")),
    ]

    try:
        await _bot.set_my_commands(commands)
        logger.info("Bot commands registered (%s)", lang)
    except Exception as e:
        logger.warning("Failed to register bot commands: %s", e)


async def _run_polling() -> None:
    """Run dispatcher polling (background task)."""
    try:
        print("[TG-BOT] Polling started")
        await _dispatcher.start_polling(_bot, handle_signals=False)
    except asyncio.CancelledError:
        print("[TG-BOT] Polling cancelled")
    except Exception as e:
        print(f"[TG-BOT] Polling error: {e}")
        logger.error("Telegram bot polling error: %s", e)


async def stop_telegram_bot() -> None:
    """Stop the Telegram bot."""
    global _bot, _dispatcher, _polling_task

    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await _polling_task
        except asyncio.CancelledError:
            pass

    if _dispatcher:
        await _dispatcher.stop_polling()
        _dispatcher = None

    if _bot:
        await _bot.session.close()
        _bot = None

    _polling_task = None
    logger.info("Telegram bot stopped")


async def send_message(chat_id: str | int, text: str, **kwargs) -> bool:
    """Send a text message via the bot."""
    if not _bot:
        return False
    try:
        await _bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


async def send_photo(chat_id: str | int, photo: bytes, caption: str | None = None, **kwargs) -> bool:
    """Send a photo via the bot."""
    if not _bot:
        return False
    try:
        from aiogram.types import BufferedInputFile

        file = BufferedInputFile(photo, filename="photo.jpg")
        await _bot.send_photo(chat_id=chat_id, photo=file, caption=caption, **kwargs)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram photo: %s", e)
        return False
