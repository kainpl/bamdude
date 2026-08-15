"""A wizard that stopped existing must say so, not fail and not go quiet.

FSM state lives in aiogram's in-memory storage, so restarting the backend wipes
every open wizard while its messages stay on the operator's screen. This is not
about keeping the state — a wizard is seconds of interaction and restarts are
rare. It is about the loss being indistinguishable from something else:

- pressing a button of a dead wizard answered **"failed"**, which claims the
  action was refused rather than never attempted;
- typing into a dead wizard produced **nothing at all**, which is exactly what
  a bot that is down looks like.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.telegram_handlers.common import scene_expired
from backend.app.services.telegram_handlers.fallback import msg_unclaimed


class _State:
    def __init__(self, current: str | None = None):
        self.current = current
        self.cleared = False

    async def get_state(self):
        return self.current

    async def clear(self):
        self.current = None
        self.cleared = True


def _callback():
    return SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))


@pytest.mark.asyncio
async def test_it_tells_the_operator_the_step_is_gone():
    callback = _callback()

    await scene_expired(callback, "en")

    assert callback.answer.await_args.kwargs.get("show_alert") is True
    # And the stale message is rewritten, so the dead buttons stop inviting a
    # second press.
    callback.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_message_too_old_to_edit_still_gets_the_alert():
    """Telegram refuses edits to old messages. The alert is the part that
    matters, so the edit failing must not swallow it."""
    callback = _callback()
    callback.message.edit_text.side_effect = OSError("message is too old")

    await scene_expired(callback, "en")  # must not raise

    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unclaimed_message_gets_an_answer():
    message = SimpleNamespace(answer=AsyncMock())

    await msg_unclaimed(message, state=_State(None))

    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_reaching_the_fallback_with_a_state_set_clears_it():
    """⚠️ Stale by definition: the scene handler is bound to that state and
    would have run. Leaving it set keeps eating messages."""
    message = SimpleNamespace(answer=AsyncMock())
    state = _State("AddPrinterState:entering_ip")

    await msg_unclaimed(message, state=state)

    assert state.cleared


def test_the_fallback_router_is_registered_last():
    """The whole design. Anywhere earlier and it swallows the bot."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "app" / "services" / "telegram_bot.py").read_text(encoding="utf-8")
    includes = [line for line in source.splitlines() if "include_router(" in line and "def " not in line]

    assert includes, "router registration moved"
    assert "fallback_router" in includes[-1], f"fallback is not last: {includes[-1].strip()}"


@pytest.mark.asyncio
async def test_a_dead_library_wizard_says_expired_rather_than_failed():
    from backend.app.services.telegram_handlers.library_scene import cb_library_print_now

    callback = _callback()
    state = _State("LibraryPrintState:confirming")
    state.get_data = AsyncMock(return_value={})  # what a restart leaves behind

    with patch("backend.app.services.telegram_handlers.library_scene.scene_expired", new=AsyncMock()) as expired:
        await cb_library_print_now(callback, state)

    expired.assert_awaited_once()
