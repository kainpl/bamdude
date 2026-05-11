"""Regression tests for ``telegram_bot.start_telegram_bot`` / ``restart_telegram_bot``.

Reproduces the "old bot doesn't die, new one fails to start" failure mode
when a token change causes ``Bot.get_me()`` to raise. The module-level
handler routers (``start_router``, ``printers_router``, …) are singletons
imported into every ``Dispatcher`` instance; aiogram refuses to attach a
router whose ``_parent_router`` still points at an earlier dispatcher.

``stop_telegram_bot`` handles this on the happy path — it walks
``_dispatcher.sub_routers`` and clears ``_parent_router`` on each. But the
error branch of ``start_telegram_bot`` (line 108 — ``except Exception``)
only nulls ``_bot`` / ``_dispatcher`` and returns. The routers it just
attached stay bound to the now-orphaned dispatcher, and the *next* start
attempt explodes inside ``include_router`` with
``"Router is already attached to ..."``.

These tests pin the contract: when start fails (invalid token / network
blip), the next start with a valid token must succeed. We mock the aiogram
``Bot`` so the test doesn't touch the real Telegram API.
"""

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
    monkeypatch.setattr(tb, "_get_bot_token", AsyncMock(return_value="123:AAfake"))

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
        # Suppress real polling — return a no-op task so we don't actually hit Telegram.
        async def _noop():
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

    monkeypatch.setattr(tb, "_get_bot_token", AsyncMock(return_value="123:AAvalid"))

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

        async def _noop():
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
