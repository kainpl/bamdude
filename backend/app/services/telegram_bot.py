"""Telegram bot service using aiogram 3.x.

Manages bot lifecycle, polling, and provides send methods for notifications.
Bot token is read from the first Telegram notification provider in DB.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)

# Singleton
_bot: Bot | None = None
_dispatcher: Dispatcher | None = None
_polling_task: asyncio.Task | None = None

# One lock over the whole lifecycle, because the singleton above is THREE
# references and every operation rewrites all of them around network awaits —
# the token read, ``get_me()``, ``set_my_commands()``. Two of those operations
# overlapping is not hypothetical: saving a notification provider restarts the
# bot, and the UI saves from more than one place, so ``restart_telegram_bot``
# could be entered again while the first one was still between "I built a Bot"
# and "I stored the polling task". The second start then overwrote
# ``_polling_task`` while the first poller was still running: nothing referenced
# the old task any more, so shutdown had nothing to cancel, and the orphan sat
# inside a ``getUpdates`` long poll — two pollers competing for the same
# updates (Telegram refuses one of them with 409 Conflict, and which poller a
# button press reaches is a coin toss) and a container that would not come
# down. #50.
#
# So: start / stop / restart are serialised end to end, network awaits
# included, and ``restart`` holds the lock across both halves. The three public
# functions are thin wrappers; ``_start_locked`` / ``_stop_locked`` below do
# the work and assume the lock is held.
_lifecycle_lock = asyncio.Lock()


def _detach_sub_routers(dispatcher: Dispatcher) -> None:
    """Clear ``_parent_router`` on every sub-router of ``dispatcher``.

    Handler modules export module-level ``Router`` singletons (``start_router``,
    ``printers_router``, …). aiogram refuses to ``include_router`` a router
    whose ``_parent_router`` is non-None — so once a router has been attached
    to a dispatcher, **every** future ``Dispatcher`` instance needs a fresh
    re-attach, which means we must detach from the current one before letting
    that one go out of scope. The public setter rejects ``None``, so we clear
    the private attribute directly. Without this, restarting the bot after a
    token change crashes with ``"Router is already attached to <Dispatcher>"``.
    """
    for sub in list(dispatcher.sub_routers):
        sub._parent_router = None  # noqa: SLF001 - only way to detach
    dispatcher.sub_routers.clear()


async def _discard_bot_locked(dispatcher: Dispatcher | None, bot: Bot | None) -> None:
    """Throw a bot pair away and leave the module holding nothing.

    Three paths end here and they need the same three things done: the router
    singletons detached from the dispatcher that is going away (see
    ``_detach_sub_routers``), the HTTP session closed, and the module globals
    cleared. Closing the session is best-effort — one of those paths is a
    start that just failed on this very bot, and a raise here would strand the
    globals pointing at the wreck. Assumes ``_lifecycle_lock`` is held.
    """
    global _bot, _dispatcher, _polling_task

    if dispatcher is not None:
        _detach_sub_routers(dispatcher)
    if bot is not None:
        try:
            await bot.session.close()
        except Exception:
            logger.debug("Bot session close raised during teardown", exc_info=True)

    _bot = None
    _dispatcher = None
    _polling_task = None


def get_bot() -> Bot | None:
    """Get the active bot instance."""
    return _bot


async def _get_bot_token() -> str | None:
    """Read bot token from the first enabled Telegram notification provider."""
    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.notification import NotificationProvider

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
    async with _lifecycle_lock:
        await _start_locked()


async def _start_locked() -> None:
    """Build the bot and its poller. Assumes ``_lifecycle_lock`` is held."""
    global _bot, _dispatcher, _polling_task

    if _polling_task is not None and not _polling_task.done():
        # Idempotent: a poller is already running, and a second one would
        # compete with it for the same updates.
        logger.debug("Telegram bot is already polling - start request ignored")
        return

    if _dispatcher is not None:
        # No live poller, but a dispatcher is still here: the poller died on
        # its own (``_run_polling`` logs the exception and returns, so nothing
        # tore the rest down). Its dispatcher still holds the module-level
        # router singletons, and building a second dispatcher now would
        # ``include_router`` them a second time — "Router is already attached".
        # Clear the wreck first: the same teardown ``_stop_locked`` ends with,
        # minus a poller there is nothing left to cancel.
        logger.debug("Clearing a dead Telegram bot before starting a new one")
        await _discard_bot_locked(_dispatcher, _bot)

    token = await _get_bot_token()
    if not token:
        print("[TG-BOT] No Telegram bot token configured - bot not started")
        return
    print(f"[TG-BOT] Token found: {token[:10]}...")

    # Register handlers
    from backend.app.services.telegram_handlers.actions import router as actions_router
    from backend.app.services.telegram_handlers.auth_middleware import TelegramAuthMiddleware
    from backend.app.services.telegram_handlers.calibration import router as calibration_router
    from backend.app.services.telegram_handlers.defects import router as defects_router
    from backend.app.services.telegram_handlers.fallback import router as fallback_router
    from backend.app.services.telegram_handlers.library_scene import router as library_router
    from backend.app.services.telegram_handlers.library_upload_scene import router as library_upload_router
    from backend.app.services.telegram_handlers.maintenance_handlers import router as maintenance_router
    from backend.app.services.telegram_handlers.printer_add_scene import router as printer_add_router
    from backend.app.services.telegram_handlers.printers import router as printers_router
    from backend.app.services.telegram_handlers.queue import router as queue_router
    from backend.app.services.telegram_handlers.queue_scene import router as queue_scene_router
    from backend.app.services.telegram_handlers.skip_objects_scene import router as skip_objects_router
    from backend.app.services.telegram_handlers.start import router as start_router
    from backend.app.services.telegram_handlers.stats import router as stats_router

    _dispatcher = Dispatcher()
    _dispatcher.message.middleware(TelegramAuthMiddleware())
    _dispatcher.callback_query.middleware(TelegramAuthMiddleware())
    _dispatcher.include_router(start_router)
    _dispatcher.include_router(printers_router)
    _dispatcher.include_router(calibration_router)
    _dispatcher.include_router(maintenance_router)
    # ⚠️ ABOVE actions_router, whose last handler is the catch-all ``action:*``.
    # The completion message's «Брак…» button is ``action:defects:{archive_id}``,
    # so below it the catch-all would claim it, read the archive id as a printer
    # id and redraw that printer's detail. Same reason ``action:hours:`` lives in
    # printers_router, which is included earlier for exactly this.
    _dispatcher.include_router(defects_router)
    _dispatcher.include_router(actions_router)
    # Its own callbacks are ``skipobj:*``, so it collides with nothing above —
    # in particular not with actions_router's catch-all ``action:*``, which is
    # why the entry button is NOT called ``action:skip_objects``.
    _dispatcher.include_router(skip_objects_router)
    _dispatcher.include_router(queue_router)
    _dispatcher.include_router(stats_router)
    _dispatcher.include_router(library_router)
    # Receives documents and hands the stored file back to library_router's
    # ``lib:file:{id}`` picker, so it must sit above the fallback and may sit
    # anywhere below the scenes that claim free text.
    _dispatcher.include_router(library_upload_router)
    _dispatcher.include_router(queue_scene_router)
    _dispatcher.include_router(printer_add_router)
    # ⚠️ LAST, and it must stay last: it answers any message no handler above
    # it claimed. Anywhere earlier and it swallows the bot.
    _dispatcher.include_router(fallback_router)

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
        # Throw the half-built pair away, routers included: without the detach
        # the singletons stay bound to this now-orphaned dispatcher and a
        # follow-up start (e.g. the user fixes the token) raises "Router is
        # already attached to <Dispatcher>" inside include_router.
        await _discard_bot_locked(_dispatcher, _bot)
        return

    # Start polling in background
    print("[TG-BOT] Starting polling...")
    _polling_task = asyncio.create_task(_run_polling(_dispatcher, _bot))


async def _register_commands() -> None:
    """Register bot commands for the Telegram menu button."""
    from aiogram.types import BotCommand

    from backend.app.i18n import get_language, t

    lang = await get_language()
    NS = "telegram_ui"

    commands = [
        BotCommand(command="start", description=t(lang, NS, "commands.start")),
        BotCommand(command="status", description=t(lang, NS, "commands.status")),
        BotCommand(command="camera", description=t(lang, NS, "commands.camera")),
        BotCommand(command="cancel", description=t(lang, NS, "commands.cancel")),
        BotCommand(command="help", description=t(lang, NS, "commands.help")),
    ]

    try:
        await _bot.set_my_commands(commands)
        logger.info("Bot commands registered (%s)", lang)
    except Exception as e:
        logger.warning("Failed to register bot commands: %s", e)


async def _run_polling(dispatcher: Dispatcher, bot: Bot) -> None:
    """Run dispatcher polling (background task).

    Takes the pair it polls with as arguments instead of reading the module
    globals: the task outlives the call that created it, and by the time it
    first runs those globals can already belong to a newer bot — or be ``None``
    because a stop is under way. It polls what it was handed.

    Two things end it: cancelling the task, and ``dispatcher.stop_polling()``,
    which makes ``start_polling`` return. ``_stop_locked`` does both — the
    cancel first, which is why the ``stop_polling()`` after it usually reports
    "Polling is not started", and why that answer is tolerated there.
    """
    try:
        print("[TG-BOT] Polling started")
        await dispatcher.start_polling(bot, handle_signals=False)
    except asyncio.CancelledError:
        print("[TG-BOT] Polling cancelled")
    except Exception as e:
        print(f"[TG-BOT] Polling error: {e}")
        logger.error("Telegram bot polling error: %s", e)


async def stop_telegram_bot() -> None:
    """Stop the Telegram bot."""
    async with _lifecycle_lock:
        await _stop_locked()


async def _stop_locked() -> None:
    """Tear the bot down. Assumes ``_lifecycle_lock`` is held.

    Detaches the three module references BEFORE the first await and works on
    the local copies from there on. Everything below can yield, and whoever
    looks at this module while we are yielding must see "there is no bot", not
    a half-dismantled one — and must never be handed back a Bot we are in the
    middle of closing.
    """
    global _bot, _dispatcher, _polling_task

    task, dispatcher, bot = _polling_task, _dispatcher, _bot
    _polling_task = None
    _dispatcher = None
    _bot = None

    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # Two cancellations can surface here and they mean opposite things:
            # the poller finishing the cancel we just asked for (ours, swallow
            # it), and somebody cancelling *us* — a bounded shutdown giving up
            # on a poller that will not die. Swallowing the second would make
            # that bound silently useless, so let it travel on.
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

    if dispatcher is not None:
        # aiogram's stop_polling() raises RuntimeError("Polling is not started")
        # when polling already stopped - which is exactly our state after the
        # cancel above, or if an earlier TelegramNetworkError tore the poller
        # down on its own. Tolerate that case; any other RuntimeError still
        # surfaces.
        try:
            await dispatcher.stop_polling()
        except RuntimeError as e:
            if "not started" not in str(e).lower():
                raise
            logger.debug("stop_polling() reported polling already stopped - ignoring")

    # Detach the module-level router singletons so the next Dispatcher can
    # re-attach them, close the session, and leave the globals empty — the
    # same teardown a start that never got off the ground runs. The dispatcher
    # is safe to strip by now: aiogram emits its shutdown hooks INSIDE the
    # poller task (``start_polling`` awaits ``emit_shutdown`` in its own
    # ``finally``), and that task was awaited above, so nothing is still
    # walking the routers we are about to unhook.
    await _discard_bot_locked(dispatcher, bot)

    logger.info("Telegram bot stopped")


async def stop_telegram_bot_bounded(timeout: float = 15.0) -> None:
    """Stop the bot, giving up after ``timeout`` seconds instead of hanging.

    Shutdown runs against a clock: Docker and systemd hand the process a grace
    period and then kill it. A poller parked inside a ``getUpdates`` long poll
    must not be able to spend that budget on our behalf, so the shutdown path
    puts a bound on the wait and says so in the log rather than blocking the
    rest of the teardown.

    What the timeout abandons is the POLLER, not a wrapper task: on 3.12
    ``wait_for`` runs ``stop_telegram_bot`` inline in this task and cancels it
    in place, so the task left behind is the one ``_stop_locked`` was waiting
    on — the long poll that would not die. It dies with the process.

    ⚠️ ``_stop_locked`` clears the module globals before its first await, so
    after a timed-out stop the module already holds nothing while that poller
    is still alive. A later ``start`` would therefore see a clean slate and
    build a SECOND poller beside it — two of them competing for the same
    updates, which is #50 again. Unreachable today because the only caller is
    the lifespan handler at process exit; a second caller has to be a
    decision, not an accident.
    """
    try:
        await asyncio.wait_for(stop_telegram_bot(), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "Telegram bot did not stop within %ss - abandoning it and continuing shutdown",
            timeout,
        )


async def restart_telegram_bot() -> None:
    """Restart the Telegram bot (stop if running, then start with fresh config)."""
    logger.info("Restarting Telegram bot...")
    # One acquisition across both halves. Released in between, the module
    # briefly holds no bot at all — and a start let in there builds one that
    # this restart's own start then duplicates.
    async with _lifecycle_lock:
        await _stop_locked()
        await _start_locked()


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
