"""Telegram bot service using aiogram 3.x.

Manages bot lifecycle, polling, and provides send methods for notifications.
A telegram notification provider IS a bot (its token), and the process runs
one polling session holding a bot for EVERY enabled provider — see
``current_bot_providers`` and the registry comment below.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

logger = logging.getLogger(__name__)

# The registry: one polling session, one bot per enabled telegram provider.
#
# A telegram provider row IS a bot (its token), and Telegram serves one
# ``getUpdates`` consumer per token — different tokens are independent, so the
# number of bots a process can run is the number of providers, not one.
# aiogram polls them the way it was built to: ONE ``Dispatcher`` handed every
# bot (``start_polling(*bots)`` runs a task per bot), one router tree, and the
# handler receives the bot the update arrived on (``message.bot``), so replies
# go back through it without anybody looking a bot up. The FSM keys its state
# by ``bot_id`` already.
#
# Not a dispatcher per bot: the handler modules export module-level ``Router``
# singletons and aiogram refuses to attach one to two dispatchers, so that
# shape would mean turning every handler module into a router factory — for
# the single gain of restarting one bot without touching the others. Not our
# own ``getUpdates`` loop either: that is offset, backoff, allowed_updates and
# the startup/shutdown hooks re-implemented. The price of the shared session —
# any provider save reconnects every bot — is close to nil: Telegram re-serves
# updates nobody confirmed, so a command from the second the restart takes
# arrives after it, and a provider save is an operator's action, not a flow.
_bots: dict[int, Bot] = {}  # provider id -> its bot, oldest provider first
_bot_ids: dict[int, int] = {}  # Telegram bot account id (from get_me) -> provider id
_dispatcher: Dispatcher | None = None
_polling_task: asyncio.Task | None = None

# One lock over the whole lifecycle, because the registry above is FOUR
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


async def _discard_bot_locked(dispatcher: Dispatcher | None, bots: dict[int, Bot] | None) -> None:
    """Throw a session's bots away and leave the module holding nothing.

    Three paths end here and they need the same things done: the router
    singletons detached from the dispatcher that is going away (see
    ``_detach_sub_routers``), every bot's HTTP session closed, and the module
    registry cleared. Closing a session is best-effort — one of those paths is
    a start that just failed on one of these bots, and a raise here would
    strand the registry pointing at the wreck. Assumes ``_lifecycle_lock`` is
    held.
    """
    global _dispatcher, _polling_task

    if dispatcher is not None:
        _detach_sub_routers(dispatcher)
    for bot in (bots or {}).values():
        try:
            await bot.session.close()
        except Exception:
            logger.warning("Bot session close raised during teardown", exc_info=True)

    _bots.clear()
    _bot_ids.clear()
    _dispatcher = None
    _polling_task = None


def get_bot(provider_id: int) -> Bot | None:
    """The live bot of a telegram provider, or ``None`` when it is not running.

    Every send names the provider it speaks for: a chat belongs to one bot
    (m180) and no other bot can reach it — Telegram answers "chat not found"
    for a chat that never started that bot.
    """
    return _bots.get(provider_id)


async def current_bot_providers() -> list[tuple[int, str]]:
    """Every bot that should be running — ``(provider id, token)``, oldest provider first.

    One enabled telegram provider, one bot. The ``ORDER BY`` is not
    decoration: an unordered read is rowid order on SQLite but, on
    PostgreSQL, a heap scan whose order an UPDATE can move (the new tuple
    version lands wherever there is room), so a rename could reshuffle the
    answer between two restarts — and this list is compared against itself
    across a provider save to decide whether the poller must be rebuilt.

    A row whose config is not JSON, or carries no token, is not a bot and is
    left out rather than raising: the provider routes ask this on every save,
    and one damaged row must not turn all provider CRUD into a 500.
    """
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
            .order_by(NotificationProvider.id)
        )
        rows = result.scalars().all()

    import json

    providers: list[tuple[int, str]] = []
    for provider in rows:
        config = provider.config
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        token = config.get("bot_token") if isinstance(config, dict) else None
        token = token.strip() if isinstance(token, str) else ""
        if token:
            providers.append((provider.id, token))
    return providers


def running_bot_provider_ids() -> list[int]:
    """The providers the LIVE session polls for, oldest first; empty when no bot runs.

    Read from the registry rather than the provider table: the table may have
    changed since the session started, and what a chat wrote to is the bot
    that is actually up.
    """
    return list(_bots)


def provider_id_for_bot(bot_id: int) -> int | None:
    """Which provider a Telegram bot account id belongs to, or ``None`` if it is not ours.

    The identity an incoming update carries is ``event.bot.id`` — the bot
    account, not our row — so this is the one place that translates it. Built
    from ``get_me()`` at start, because nothing stores it: a token can be
    revoked and reissued for the same bot, and the account id is the
    firmware-side fact, not ours to persist.
    """
    return _bot_ids.get(bot_id)


async def _record_provider_error(provider_id: int, error: str) -> None:
    """Write a start failure onto the provider row, where the operator sees it.

    The same two columns ``NotificationService._update_provider_status``
    writes on a failed delivery. Written from here directly rather than
    through the service: the service imports this module, so importing it
    back would close a cycle. Best-effort — a bot that would not start must
    not also take the start of the others down.
    """
    try:
        from datetime import datetime, timezone

        from sqlalchemy import update

        from backend.app.core.database import async_session
        from backend.app.models.notification import NotificationProvider

        async with async_session() as db:
            await db.execute(
                update(NotificationProvider)
                .where(NotificationProvider.id == provider_id)
                .values(last_error=error, last_error_at=datetime.now(timezone.utc))
            )
            await db.commit()
    except Exception:
        logger.warning("Could not record the start error of provider %s", provider_id, exc_info=True)


async def start_telegram_bot() -> None:
    """Start the Telegram bot polling in background."""
    async with _lifecycle_lock:
        await _start_locked()


async def _start_locked() -> None:
    """Build every enabled provider's bot and the one poller. Assumes ``_lifecycle_lock`` is held."""
    global _dispatcher, _polling_task

    if _polling_task is not None and not _polling_task.done():
        # Idempotent: a session is already polling, and a second one would
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
        await _discard_bot_locked(_dispatcher, dict(_bots))

    providers = await current_bot_providers()
    if not providers:
        print("[TG-BOT] No Telegram bot token configured - bot not started")
        return
    print(f"[TG-BOT] {len(providers)} Telegram bot(s) configured")

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

    # One bot per provider. A token that will not answer takes its own
    # provider out of the session and nothing else: the others are different
    # bots with their own quotas, and a farm must not lose its working bot
    # because somebody pasted a stale token into a second provider.
    claimed_by: dict[str, int] = {}
    for provider_id, token in providers:
        owner = claimed_by.get(token)
        if owner is not None:
            # Two enabled rows behind one token are one bot, and Telegram
            # serves one getUpdates consumer per token — polling it twice is
            # a 409. m180 deduplicated what was already stored and the routes
            # refuse a new pair, so this is the backstop for a hand edit or a
            # restore: the older row polls, the younger says why it does not.
            message = f"This bot token is already polled by Telegram provider {owner}"
            logger.error("Telegram provider %s: %s", provider_id, message)
            await _record_provider_error(provider_id, message)
            continue

        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2))
        try:
            me = await bot.get_me()
            print(f"[TG-BOT] Started: @{me.username} ({me.full_name})")
            logger.info("Telegram bot started: @%s (%s) for provider %s", me.username, me.full_name, provider_id)
            await _register_commands(bot)
        except Exception as e:
            logger.error("Failed to start the Telegram bot of provider %s: %s", provider_id, e)
            await _record_provider_error(provider_id, str(e))
            try:
                await bot.session.close()
            except Exception:
                logger.warning("Bot session close raised after a failed start", exc_info=True)
            continue

        claimed_by[token] = provider_id
        _bots[provider_id] = bot
        _bot_ids[me.id] = provider_id

    if not _bots:
        # Not one bot answered. Throw the half-built session away, routers
        # included: without the detach the singletons stay bound to this
        # now-orphaned dispatcher and a follow-up start (e.g. the user fixes
        # the token) raises "Router is already attached" inside include_router.
        logger.error("No Telegram bot could be started")
        await _discard_bot_locked(_dispatcher, None)
        return

    # One polling session for all of them: aiogram runs a task per bot inside
    # it and hands each update's handler the bot it arrived on.
    print(f"[TG-BOT] Starting polling for {len(_bots)} bot(s)...")
    _polling_task = asyncio.create_task(_run_polling(_dispatcher, list(_bots.values())))


async def _register_commands(bot: Bot) -> None:
    """Register bot commands for the Telegram menu button — per bot, they are its own."""
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
        await bot.set_my_commands(commands)
        logger.info("Bot commands registered (%s)", lang)
    except Exception as e:
        logger.warning("Failed to register bot commands: %s", e)


async def _run_polling(dispatcher: Dispatcher, bots: list[Bot]) -> None:
    """Run the one polling session (background task).

    Takes what it polls with as arguments instead of reading the module
    registry: the task outlives the call that created it, and by the time it
    first runs the registry can already belong to a newer session — or be
    empty because a stop is under way. It polls what it was handed.

    ``start_polling`` runs a long-poll task per bot inside this one task and
    dispatches every update through the same router tree, handing the handler
    the bot it arrived on. Two things end it: cancelling this task, and
    ``dispatcher.stop_polling()``, which ends every bot of the session at
    once. ``_stop_locked`` does both — the cancel first, which is why the
    ``stop_polling()`` after it usually reports "Polling is not started", and
    why that answer is tolerated there.
    """
    try:
        print("[TG-BOT] Polling started")
        await dispatcher.start_polling(*bots, handle_signals=False)
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
    """Tear the session down. Assumes ``_lifecycle_lock`` is held.

    Detaches the module's references BEFORE the first await and works on the
    local copies from there on. Everything below can yield, and whoever looks
    at this module while we are yielding must see "there is no bot", not a
    half-dismantled one — and must never be handed back a Bot we are in the
    middle of closing.
    """
    global _dispatcher, _polling_task

    task, dispatcher, bots = _polling_task, _dispatcher, dict(_bots)
    _polling_task = None
    _dispatcher = None
    _bots.clear()
    _bot_ids.clear()

    # Completion drafts are bound to a provider+chat+operator. A stopped bot
    # cannot safely resume its ForceReply prompts after a restart, so remove
    # their addresses before another bot/session accepts updates.
    from backend.app.services.telegram_handlers.defects import clear_completion_drafts

    clear_completion_drafts(set(bots))

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
    await _discard_bot_locked(dispatcher, bots)

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

    ⚠️ ``_stop_locked`` clears the registry before its first await, so after a
    timed-out stop the module already holds nothing while that poller is still
    alive — and the cancel re-raise skips ``_discard_bot_locked``, so the
    handler routers stay attached to the abandoned dispatcher. A later
    ``start`` would therefore see a clean slate, crash in ``include_router``
    ("Router is already attached"), and had it got past that, build a SECOND
    session beside the live one — two pollers per token competing for the same
    updates, which is #50 again. Unreachable today because the only caller is the
    lifespan handler at process exit; a second caller has to be a decision,
    not an accident.
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


async def send_message(provider_id: int, chat_id: str | int, text: str, **kwargs) -> bool:
    """Send a text message as the bot of ``provider_id``.

    ``False`` when that provider's bot is not running — the caller then has
    the direct HTTP route, which needs no poller.
    """
    bot = _bots.get(provider_id)
    if bot is None:
        return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


async def send_photo(provider_id: int, chat_id: str | int, photo: bytes, caption: str | None = None, **kwargs) -> bool:
    """Send a photo as the bot of ``provider_id`` (``False`` when it is not running)."""
    bot = _bots.get(provider_id)
    if bot is None:
        return False
    try:
        from aiogram.types import BufferedInputFile

        file = BufferedInputFile(photo, filename="photo.jpg")
        await bot.send_photo(chat_id=chat_id, photo=file, caption=caption, **kwargs)
        return True
    except Exception as e:
        logger.error("Failed to send Telegram photo: %s", e)
        return False
