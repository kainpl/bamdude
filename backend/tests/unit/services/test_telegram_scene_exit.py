"""Leaving a wizard must also end it.

``start.py``'s router is included FIRST (``telegram_bot.py``), so its
reply-keyboard and ``/start`` handlers outrank the message handler a scene has
open on its own state. The menu appeared, the FSM state stayed set, and the
next unrelated thing the operator typed was swallowed as scene input — an IP
address for a printer they were no longer adding.

There was also no way to say "stop": every scene's Cancel is a button inside
that scene's own message, which is exactly what an operator no longer has once
they have navigated away.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.telegram_handlers import start as start_handlers


class _State:
    """Enough FSMContext for these handlers: a state and a clear()."""

    def __init__(self, current: str | None = None):
        self.current = current
        self.cleared = False

    async def get_state(self):
        return self.current

    async def clear(self):
        self.current = None
        self.cleared = True


def _message() -> SimpleNamespace:
    return SimpleNamespace(answer=AsyncMock(), chat=SimpleNamespace(id=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["reply_printers", "reply_queue", "reply_stats", "reply_help"])
async def test_every_menu_button_ends_an_open_scene(handler_name):
    state = _State("AddPrinterState:entering_ip")
    handler = getattr(start_handlers, handler_name)

    with (
        patch("backend.app.services.telegram_handlers.printers.show_printer_list", new=AsyncMock()),
        patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()),
        patch("backend.app.services.telegram_handlers.stats.render_stats", new=AsyncMock()),
        patch.object(start_handlers, "cmd_help", new=AsyncMock()),
    ):
        await handler(_message(), state=state)

    assert state.cleared, f"{handler_name} left the scene running"


@pytest.mark.asyncio
async def test_start_ends_an_open_scene_too():
    state = _State("LibraryPrintState:selecting_file")

    await start_handlers.cmd_start(_message(), state=state)

    assert state.cleared


@pytest.mark.asyncio
async def test_start_still_works_when_a_scene_calls_it_directly():
    """⚠️ The scenes finish by calling ``cmd_start(callback.message)`` with no
    state — they have already cleared their own. The parameter must stay
    optional or every completed wizard raises on its last line."""
    await start_handlers.cmd_start(_message())


@pytest.mark.asyncio
async def test_cancel_ends_the_scene_and_says_so():
    state = _State("AddPrinterState:entering_access_code")
    message = _message()

    await start_handlers.cmd_cancel(message, state=state)

    assert state.cleared
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_answers_even_with_nothing_to_cancel():
    """Silence would read as the bot being stuck — the very thing this fixes."""
    message = _message()

    await start_handlers.cmd_cancel(message, state=_State(None))

    message.answer.assert_awaited_once()
