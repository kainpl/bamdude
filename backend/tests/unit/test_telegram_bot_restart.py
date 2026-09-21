"""Regression tests for ``telegram_bot.start_telegram_bot`` / ``restart_telegram_bot``.

Reproduces the "old bot doesn't die, new one fails to start" failure mode
when a token change causes ``Bot.get_me()`` to raise. The module-level
handler routers (``start_router``, ``printers_router``, …) are singletons
imported into every ``Dispatcher`` instance; aiogram refuses to attach a
router whose ``_parent_router`` still points at an earlier dispatcher.

``stop_telegram_bot`` handles this on the happy path — it walks
``_dispatcher.sub_routers`` and clears ``_parent_router`` on each. But the
error branch of the original ``start_telegram_bot`` only nulled ``_bot`` /
``_dispatcher`` and returned. The routers it had just attached stayed bound
to the now-orphaned dispatcher, and the *next* start attempt exploded inside
``include_router`` with ``"Router is already attached to ..."``. Today the
start-failure branch and the dead-poller guard share ``stop``'s teardown
tail (grep ``_discard_bot_locked``).

These tests pin the contract: when start fails (invalid token / network
blip), the next start with a valid token must succeed. We mock the aiogram
``Bot`` so the test doesn't touch the real Telegram API.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_start_failure_detaches_routers_so_next_start_succeeds(monkeypatch):
    """Token-change scenario: start fails, then succeeds on retry.

    Without the fix this raises ``"Router is already attached"`` from
    ``include_router`` during the second start.
    """
    import aiogram

    from backend.app.services import telegram_bot as tb

    # Token-fetch always returns a non-empty string so we hit the Bot/Dispatcher path.
    monkeypatch.setattr(tb, "current_bot_provider", AsyncMock(return_value=(1, "123:AAfake")))

    # Force the global state to a known starting point (clean test).
    tb._bot = None
    tb._dispatcher = None
    tb._polling_task = None

    # Capture the real handler router singletons so we can inspect their _parent_router.
    from backend.app.services.telegram_handlers.start import router as start_router

    # First start: get_me() raises → start_telegram_bot bails in the except branch.
    # Build a Bot mock that fails get_me on the first call (invalid token), then
    # succeeds on the second (the user fixed it and triggered another restart).
    bot_instances: list[MagicMock] = []

    def _bot_factory(*args, **kwargs):  # noqa: ARG001
        m = MagicMock(spec=aiogram.Bot)
        m.session = MagicMock()
        m.session.close = AsyncMock()
        # First instance fails get_me(); subsequent succeed.
        if not bot_instances:
            m.get_me = AsyncMock(side_effect=Exception("Unauthorized: invalid token"))
        else:
            me = MagicMock()
            me.username = "bamdude_test_bot"
            me.full_name = "BamDude Test"
            m.get_me = AsyncMock(return_value=me)
        m.set_my_commands = AsyncMock()
        bot_instances.append(m)
        return m

    # Patch the Bot constructor inside telegram_bot's import namespace.
    with patch.object(tb, "Bot", side_effect=_bot_factory):
        # Suppress real polling — return a no-op task so we don't actually hit
        # Telegram. ``*_args`` because ``_run_polling`` is handed the dispatcher
        # and the bot it should poll with (see the lifecycle tests below).
        async def _noop(*_args):
            return None

        with patch.object(tb, "_run_polling", _noop):
            # First start: simulates the user pasting a bad token.
            await tb.start_telegram_bot()

            # The bot/dispatcher globals are cleared (the except branch ran).
            assert tb._bot is None, "Bot global should be cleared after start failure"
            assert tb._dispatcher is None, "Dispatcher global should be cleared after start failure"

            # THE BUG: the router singletons are still attached to the orphaned
            # dispatcher. Without the fix, _parent_router is non-None here.
            assert start_router._parent_router is None, (
                "start_router._parent_router must be None after a failed start — "
                "otherwise the next include_router() in start_telegram_bot crashes "
                'with "Router is already attached".'
            )

            # Second start: simulates the user correcting the token and re-saving.
            await tb.start_telegram_bot()

            # After the second start, polling task should be set and bot should be live.
            assert tb._bot is not None, "Second start with valid token must succeed"
            assert tb._dispatcher is not None, "Dispatcher must be re-initialized on retry"

    # Cleanup so other tests don't see leaked state.
    if tb._dispatcher:
        for sub in list(tb._dispatcher.sub_routers):
            sub._parent_router = None  # noqa: SLF001
    tb._bot = None
    tb._dispatcher = None
    tb._polling_task = None


@pytest.mark.asyncio
async def test_restart_with_token_change_clean_path(monkeypatch):
    """Happy path: token change with valid → valid → second start succeeds."""
    import aiogram

    from backend.app.services import telegram_bot as tb

    monkeypatch.setattr(tb, "current_bot_provider", AsyncMock(return_value=(1, "123:AAvalid")))

    tb._bot = None
    tb._dispatcher = None
    tb._polling_task = None

    def _bot_factory(*args, **kwargs):  # noqa: ARG001
        m = MagicMock(spec=aiogram.Bot)
        m.session = MagicMock()
        m.session.close = AsyncMock()
        me = MagicMock()
        me.username = "bamdude_test_bot"
        me.full_name = "BamDude Test"
        m.get_me = AsyncMock(return_value=me)
        m.set_my_commands = AsyncMock()
        return m

    with patch.object(tb, "Bot", side_effect=_bot_factory):

        async def _noop(*_args):
            return None

        with patch.object(tb, "_run_polling", _noop):
            await tb.start_telegram_bot()
            first_bot = tb._bot
            assert first_bot is not None

            await tb.restart_telegram_bot()
            second_bot = tb._bot
            assert second_bot is not None
            assert second_bot is not first_bot, "restart should produce a fresh Bot instance"

    if tb._dispatcher:
        for sub in list(tb._dispatcher.sub_routers):
            sub._parent_router = None  # noqa: SLF001
    tb._bot = None
    tb._dispatcher = None
    tb._polling_task = None


# ---------------------------------------------------------------------------
# Lifecycle races (#50): one poller per process, under any interleaving.
#
# The module keeps three references and rewrites them around network awaits,
# so two overlapping restarts could each create a polling task while only the
# last one stayed referenced. The orphan kept long-polling ``getUpdates`` —
# two pollers fighting over the same updates, and a shutdown with nothing to
# cancel. These tests pin the contract rather than the implementation: however
# start / stop / restart interleave, at most one poller is alive and the module
# is holding it.
# ---------------------------------------------------------------------------


class _TelegramHarness:
    """The network half of ``telegram_bot``, faked, plus a record of what it built.

    ``get_me`` is a real coroutine rather than an ``AsyncMock`` on purpose: an
    ``AsyncMock`` returns without ever yielding to the loop, so two concurrent
    starts would run one after the other and the race under test could not
    occur at all.
    """

    def __init__(self, tb):
        self.tb = tb
        self._real_dispatcher = tb.Dispatcher
        self.bots: list[MagicMock] = []
        self.dispatchers: list = []
        self.pollers: list[asyncio.Task] = []
        # Never set while a test body runs: the fake pollers stay alive so the
        # test can count them. Released in teardown.
        self.release = asyncio.Event()
        # When set to an Event, ``get_me`` parks on it — the "start is halfway
        # through the network round trip" moment.
        self.get_me_gate: asyncio.Event | None = None

    def make_bot(self, *args, **kwargs):
        import aiogram

        bot = MagicMock(spec=aiogram.Bot)
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        bot.set_my_commands = AsyncMock()
        me = MagicMock()
        me.username = "bamdude_test_bot"
        me.full_name = "BamDude Test"

        async def _get_me():
            if self.get_me_gate is not None:
                await self.get_me_gate.wait()
            else:
                await asyncio.sleep(0)
            return me

        bot.get_me = _get_me
        self.bots.append(bot)
        return bot

    def make_dispatcher(self, *args, **kwargs):
        dispatcher = self._real_dispatcher(*args, **kwargs)
        self.dispatchers.append(dispatcher)
        return dispatcher

    async def run_polling(self, *_args):
        """Stand-in for ``_run_polling`` that lives until it is cancelled.

        Takes ``*_args`` so the very same test runs against both the old
        zero-argument call site and the new ``(dispatcher, bot)`` one — which
        is what lets it show RED before the fix and GREEN after.
        """
        self.pollers.append(asyncio.current_task())
        await self.release.wait()

    @property
    def alive_pollers(self) -> list[asyncio.Task]:
        return [task for task in self.pollers if not task.done()]


@pytest.fixture
async def tg(monkeypatch):
    """``telegram_bot`` with its network faked and its globals reset.

    Teardown detaches the router singletons from every ``Dispatcher`` the test
    built: these tests leave the module mid-flight on purpose, and a router
    still pointing at an abandoned dispatcher makes the *next* test explode
    inside ``include_router``.
    """
    from backend.app.services import telegram_bot as tb

    harness = _TelegramHarness(tb)

    # A fresh lock per test. ``asyncio.Lock`` binds itself to the loop of the
    # first caller that has to *wait* on it, and pytest-asyncio gives every
    # test its own loop — so the module's own lock, contended once, would raise
    # "bound to a different event loop" in the next test. The app has exactly
    # one loop, which is why the module can keep one lock for its lifetime.
    monkeypatch.setattr(tb, "_lifecycle_lock", asyncio.Lock())
    monkeypatch.setattr(tb, "current_bot_provider", AsyncMock(return_value=(1, "123:AAfake")))
    monkeypatch.setattr(tb, "Bot", harness.make_bot)
    monkeypatch.setattr(tb, "Dispatcher", harness.make_dispatcher)
    monkeypatch.setattr(tb, "_run_polling", harness.run_polling)

    tb._bot = None
    tb._dispatcher = None
    tb._polling_task = None

    yield harness

    harness.release.set()
    if harness.get_me_gate is not None:
        harness.get_me_gate.set()
    # Cancel in rounds: one of these fakes ignores its first cancel on purpose,
    # so a single ``gather`` would wait on it for as long as it chooses to sleep.
    for _ in range(3):
        pending = [task for task in harness.pollers if not task.done()]
        if not pending:
            break
        for task in pending:
            task.cancel()
        await asyncio.wait(pending, timeout=1)
    for dispatcher in harness.dispatchers:
        tb._detach_sub_routers(dispatcher)
    tb._bot = None
    tb._dispatcher = None
    tb._polling_task = None


async def _settle() -> None:
    """Give every task that was created a chance to reach its first line."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_concurrent_restarts_leave_exactly_one_poller(tg):
    """Three restarts at once: one poller survives and the module holds it."""
    tb = tg.tb

    await asyncio.gather(
        tb.restart_telegram_bot(),
        tb.restart_telegram_bot(),
        tb.restart_telegram_bot(),
    )
    await _settle()

    alive = tg.alive_pollers
    assert len(alive) == 1, (
        f"{len(alive)} pollers still running after three concurrent restarts — "
        "each orphan keeps long-polling getUpdates and nothing can cancel it"
    )
    assert tb._polling_task is alive[0], "the module must reference the poller that is actually running"
    assert tb._bot is not None
    assert tb._dispatcher is not None
    for task in tg.pollers:
        assert task.done() or task is tb._polling_task, "a poller that nothing references is an orphan"

    # One poller alive is also what a restart that released the lock between
    # its two halves produces — it just gets there by letting one restart's
    # start be swallowed as "already polling" by another's. Counting the bots
    # tells the two shapes apart: three restarts that each hold the lock end
    # to end build three bots (this is the measured number), the lock-releasing
    # shape builds two.
    assert len(tg.bots) == 3, (
        f"three restarts built {len(tg.bots)} bots - each restart must hold the lock across BOTH "
        "halves, or a start slipping in between somebody else's stop and start does one of them "
        "for them"
    )
    assert tb._bot is tg.bots[-1], "the module must hold the bot the last restart built"


async def test_concurrent_starts_build_one_bot_and_one_poller(tg):
    """Two starts at once: the second is a no-op, not a second bot."""
    tb = tg.tb

    await asyncio.gather(tb.start_telegram_bot(), tb.start_telegram_bot())
    await _settle()

    assert len(tg.bots) == 1, f"two concurrent starts built {len(tg.bots)} Bot instances — start must be idempotent"
    alive = tg.alive_pollers
    assert len(alive) == 1, f"expected one live poller, found {len(alive)}"
    assert tb._polling_task is alive[0]


async def test_a_poller_that_died_on_its_own_is_cleared_before_the_next_start(tg):
    """A dead poller leaves a dispatcher behind, and the routers are still bolted to it.

    ``_run_polling`` swallows its own exceptions, so a network failure ends the
    task quietly and the module keeps holding the dispatcher it polled with.
    The handler routers are module-level singletons attached to that
    dispatcher; a start that builds a second one without clearing the first
    raises ``"Router is already attached"`` inside ``include_router``.
    """
    tb = tg.tb

    async def _dies_immediately(*_args):
        raise RuntimeError("polling died: connection reset")

    with patch.object(tb, "_run_polling", _dies_immediately):
        await tb.start_telegram_bot()
        await _settle()

    dead_task = tb._polling_task
    dead_dispatcher = tb._dispatcher
    assert dead_task is not None and dead_task.done(), "the first poller must have ended on its own"
    assert dead_task.exception() is not None, "this test is about a poller that died, not one that stopped"
    assert dead_dispatcher is not None and dead_dispatcher.sub_routers, (
        "the dead poller's dispatcher must still be holding the router singletons"
    )
    attached = len(dead_dispatcher.sub_routers)

    await tb.start_telegram_bot()
    await _settle()

    assert not dead_dispatcher.sub_routers, "the routers must be detached from the dispatcher that died"
    assert tb._dispatcher is not None and tb._dispatcher is not dead_dispatcher
    assert len(tb._dispatcher.sub_routers) == attached, "the new dispatcher must hold each handler router exactly once"
    alive = tg.alive_pollers
    assert len(alive) == 1, f"expected exactly one live poller after the restart, found {len(alive)}"
    assert tb._polling_task is alive[0]
    assert tb._bot is tg.bots[-1]


async def test_stop_racing_a_start_leaves_no_orphan(tg):
    """A stop arriving while a start is inside ``get_me()``.

    Either side may win — zero pollers (stop won) or one (start won) — but the
    state has to be coherent: a live poller means a live bot, and no poller is
    left running behind the module's back.
    """
    tb = tg.tb
    tg.get_me_gate = asyncio.Event()

    start = asyncio.create_task(tb.start_telegram_bot())
    await _settle()  # parks inside get_me()

    stop = asyncio.create_task(tb.stop_telegram_bot())
    await _settle()

    tg.get_me_gate.set()
    await asyncio.gather(start, stop)
    await _settle()

    alive = tg.alive_pollers
    assert len(alive) <= 1, f"{len(alive)} pollers alive after a stop raced a start"
    if alive:
        assert tb._polling_task is alive[0]
        assert tb._bot is not None, "a poller is running for a bot the module has already dropped"
        assert tb._dispatcher is not None
    else:
        assert tb._polling_task is None
        assert tb._bot is None
    for task in tg.pollers:
        assert task.done() or task is tb._polling_task


def _stubborn_poller(tg, started: asyncio.Event):
    """A poller that ignores the first cancel for far longer than any shutdown.

    Stands in for a ``getUpdates`` long poll that does not come back. The
    second cancel — the one the timeout delivers — does end it, so nothing is
    left running after the test.
    """

    async def _run(*_args):
        tg.pollers.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(3600)

    return _run


async def test_stop_can_be_bounded_when_the_poller_ignores_cancellation(tg):
    """``stop`` must stay interruptible, or putting a clock on it changes nothing."""
    tb = tg.tb
    started = asyncio.Event()

    with patch.object(tb, "_run_polling", _stubborn_poller(tg, started)):
        await tb.start_telegram_bot()
        await started.wait()

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(tb.stop_telegram_bot(), 0.5)


async def test_bounded_stop_logs_instead_of_hanging(tg, caplog):
    """The shutdown path gives up out loud rather than spending the grace period."""
    tb = tg.tb
    started = asyncio.Event()

    with patch.object(tb, "_run_polling", _stubborn_poller(tg, started)):
        await tb.start_telegram_bot()
        await started.wait()

        with caplog.at_level(logging.WARNING, logger="backend.app.services.telegram_bot"):
            await tb.stop_telegram_bot_bounded(timeout=0.5)

    assert any("did not stop" in record.getMessage() for record in caplog.records), (
        f"the abandoned stop must be logged, got: {caplog.text!r}"
    )
