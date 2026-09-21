"""Regression tests for ``telegram_bot.start_telegram_bot`` / ``restart_telegram_bot``.

Reproduces the "old bot doesn't die, new one fails to start" failure mode
when a token change causes ``Bot.get_me()`` to raise. The module-level
handler routers (``start_router``, ``printers_router``, …) are singletons
imported into every ``Dispatcher`` instance; aiogram refuses to attach a
router whose ``_parent_router`` still points at an earlier dispatcher.

``stop_telegram_bot`` handles this on the happy path — it walks
``_dispatcher.sub_routers`` and clears ``_parent_router`` on each. But the
error branch of the original ``start_telegram_bot`` only nulled its bot and
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
    monkeypatch.setattr(tb, "current_bot_providers", AsyncMock(return_value=[(1, "123:AAfake")]))

    # Force the global state to a known starting point (clean test).
    tb._bots.clear()
    tb._bot_ids.clear()
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
        # and the bots it should poll with (see the lifecycle tests below).
        async def _noop(*_args):
            return None

        with patch.object(tb, "_run_polling", _noop):
            # First start: simulates the user pasting a bad token.
            await tb.start_telegram_bot()

            # The registry and the dispatcher are cleared (the except branch ran).
            assert tb._bots == {}, "the registry should be empty after a start failure"
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
            assert tb._bots, "Second start with valid token must succeed"
            assert tb._dispatcher is not None, "Dispatcher must be re-initialized on retry"

    # Cleanup so other tests don't see leaked state.
    if tb._dispatcher:
        for sub in list(tb._dispatcher.sub_routers):
            sub._parent_router = None  # noqa: SLF001
    tb._bots.clear()
    tb._bot_ids.clear()
    tb._dispatcher = None
    tb._polling_task = None


@pytest.mark.asyncio
async def test_restart_with_token_change_clean_path(monkeypatch):
    """Happy path: token change with valid → valid → second start succeeds."""
    import aiogram

    from backend.app.services import telegram_bot as tb

    monkeypatch.setattr(tb, "current_bot_providers", AsyncMock(return_value=[(1, "123:AAvalid")]))

    tb._bots.clear()
    tb._bot_ids.clear()
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
            first_bot = tb._bots[1]
            assert first_bot is not None

            await tb.restart_telegram_bot()
            second_bot = tb._bots[1]
            assert second_bot is not None
            assert second_bot is not first_bot, "restart should produce a fresh Bot instance"

    if tb._dispatcher:
        for sub in list(tb._dispatcher.sub_routers):
            sub._parent_router = None  # noqa: SLF001
    tb._bots.clear()
    tb._bot_ids.clear()
    tb._dispatcher = None
    tb._polling_task = None


# ---------------------------------------------------------------------------
# Lifecycle races (#50): one poller per process, under any interleaving.
#
# The module keeps a registry and rewrites it around network awaits,
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
        # The bots each polling session was handed, one list per session —
        # a session now holds every enabled provider's bot, not one.
        self.polled: list[tuple] = []
        # Tokens whose ``get_me`` raises: a bot that will not answer.
        self.bad_tokens: set[str] = set()
        # Never set while a test body runs: the fake pollers stay alive so the
        # test can count them. Released in teardown.
        self.release = asyncio.Event()
        # When set to an Event, ``get_me`` parks on it — the "start is halfway
        # through the network round trip" moment.
        self.get_me_gate: asyncio.Event | None = None

    def make_bot(self, *args, **kwargs):
        import aiogram

        token = kwargs.get("token", "")
        bot = MagicMock(spec=aiogram.Bot)
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        bot.set_my_commands = AsyncMock()
        bot.send_message = AsyncMock()
        bot.token = token
        me = MagicMock()
        # Distinct per bot, the way Telegram's own account ids are: the module
        # maps them back to provider ids so an update can name its bot.
        me.id = 1000 + len(self.bots)
        me.username = f"bamdude_test_bot_{me.id}"
        me.full_name = "BamDude Test"

        async def _get_me():
            if self.get_me_gate is not None:
                await self.get_me_gate.wait()
            else:
                await asyncio.sleep(0)
            if token in self.bad_tokens:
                raise RuntimeError("Unauthorized: invalid token")
            return me

        bot.get_me = _get_me
        bot.me_id = me.id
        self.bots.append(bot)
        return bot

    def make_dispatcher(self, *args, **kwargs):
        dispatcher = self._real_dispatcher(*args, **kwargs)
        self.dispatchers.append(dispatcher)
        return dispatcher

    async def run_polling(self, *args):
        """Stand-in for ``_run_polling`` that lives until it is cancelled.

        Takes ``*args`` so the very same test runs against every shape of the
        call site this module has had — which is what lets a test show RED
        before a change and GREEN after. The bots of each session are recorded
        so a test can assert the session holds all of them.
        """
        self.pollers.append(asyncio.current_task())
        self.polled.append(tuple(args[1]) if len(args) > 1 else ())
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
    monkeypatch.setattr(tb, "current_bot_providers", AsyncMock(return_value=[(1, "123:AAfake")]))
    monkeypatch.setattr(tb, "Bot", harness.make_bot)
    monkeypatch.setattr(tb, "Dispatcher", harness.make_dispatcher)
    monkeypatch.setattr(tb, "_run_polling", harness.run_polling)

    tb._bots.clear()
    tb._bot_ids.clear()
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
    tb._bots.clear()
    tb._bot_ids.clear()
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
    assert tb._bots
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
    assert tb._bots.get(1) is tg.bots[-1], "the module must hold the bot the last restart built"


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
    assert tb._bots.get(1) is tg.bots[-1]


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
        assert tb._bots, "a poller is running for a bot the module has already dropped"
        assert tb._dispatcher is not None
    else:
        assert tb._polling_task is None
        assert tb._bots == {}
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


# ---------------------------------------------------------------------------
# Several bots in one session.
#
# A telegram provider row IS a bot (its token), and Telegram serves one
# getUpdates consumer per token — different tokens are independent. So the
# process runs ONE polling session holding a bot per enabled provider, and a
# token that will not answer costs its own provider a bot and nothing else.
# ---------------------------------------------------------------------------


def _providers(*rows):
    """The reader's answer: ``(provider id, token)``, oldest provider first."""
    return AsyncMock(return_value=list(rows))


async def test_every_enabled_provider_gets_its_own_bot_in_one_session(tg, monkeypatch):
    tb = tg.tb
    monkeypatch.setattr(tb, "current_bot_providers", _providers((1, "111:A"), (2, "222:B"), (3, "333:C")))

    await tb.start_telegram_bot()
    await _settle()

    assert len(tg.alive_pollers) == 1, "one session, not one per bot"
    assert len(tg.bots) == 3
    assert list(tb._bots) == [1, 2, 3], "keyed by provider, oldest first"
    assert tg.polled[-1] == tuple(tg.bots), "the session was handed every bot"
    # An update names its bot by the Telegram account id; the module maps it back.
    assert {bot.me_id: tb.provider_id_for_bot(bot.me_id) for bot in tg.bots} == {
        tg.bots[0].me_id: 1,
        tg.bots[1].me_id: 2,
        tg.bots[2].me_id: 3,
    }
    assert tb.running_bot_provider_ids() == [1, 2, 3]


async def test_a_token_that_will_not_answer_takes_only_its_own_provider_out(tg, monkeypatch):
    """A stale token pasted into one provider must not cost the farm its working bot."""
    tb = tg.tb
    monkeypatch.setattr(tb, "current_bot_providers", _providers((1, "111:A"), (2, "222:BAD"), (3, "333:C")))
    recorded = AsyncMock()
    monkeypatch.setattr(tb, "_record_provider_error", recorded)
    tg.bad_tokens.add("222:BAD")

    await tb.start_telegram_bot()
    await _settle()

    assert list(tb._bots) == [1, 3]
    assert len(tg.alive_pollers) == 1
    assert tg.polled[-1] == (tg.bots[0], tg.bots[2])
    assert recorded.await_args.args[0] == 2, "the failure is written where the operator sees it"
    tg.bots[1].session.close.assert_awaited(), "the bot that never started does not keep a session"


async def test_two_providers_behind_one_token_start_one_bot(tg, monkeypatch):
    """The backstop: one token is one bot, and polling it twice is a 409 from Telegram."""
    tb = tg.tb
    monkeypatch.setattr(tb, "current_bot_providers", _providers((1, "111:SAME"), (2, "111:SAME"), (3, "333:C")))
    recorded = AsyncMock()
    monkeypatch.setattr(tb, "_record_provider_error", recorded)

    await tb.start_telegram_bot()
    await _settle()

    assert list(tb._bots) == [1, 3], "the older row polls the shared token"
    assert recorded.await_args.args[0] == 2
    assert "already polled" in recorded.await_args.args[1]


async def test_no_bot_at_all_starts_no_session(tg, monkeypatch):
    tb = tg.tb
    monkeypatch.setattr(tb, "current_bot_providers", _providers((1, "111:BAD"), (2, "222:BAD2")))
    monkeypatch.setattr(tb, "_record_provider_error", AsyncMock())
    tg.bad_tokens.update({"111:BAD", "222:BAD2"})

    await tb.start_telegram_bot()
    await _settle()

    assert tb._bots == {} and tb._bot_ids == {}
    assert tg.alive_pollers == []
    assert tb._dispatcher is None, "the routers are detached, so the next start can attach them"


async def test_a_send_goes_through_the_bot_of_the_provider_it_names(tg, monkeypatch):
    tb = tg.tb
    monkeypatch.setattr(tb, "current_bot_providers", _providers((1, "111:A"), (2, "222:B")))

    await tb.start_telegram_bot()
    await _settle()

    assert await tb.send_message(2, 4242, "hi") is True
    tg.bots[1].send_message.assert_awaited_once()
    tg.bots[0].send_message.assert_not_awaited()
    assert await tb.send_message(99, 4242, "hi") is False, "a provider with no live bot cannot send"


async def test_stopping_closes_every_bot_of_the_session(tg, monkeypatch):
    tb = tg.tb
    monkeypatch.setattr(tb, "current_bot_providers", _providers((1, "111:A"), (2, "222:B")))

    await tb.start_telegram_bot()
    await _settle()
    await tb.stop_telegram_bot()

    for bot in tg.bots:
        bot.session.close.assert_awaited()
    assert tb._bots == {} and tb._bot_ids == {}
