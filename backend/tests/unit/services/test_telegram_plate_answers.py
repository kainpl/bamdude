"""Telegram offers the same two answers as the card.

⚠️ And it must offer them in the case the card already handles: the keyboard was
built inside ``if next_job:``, so after the LAST print in a queue Telegram showed
no plate control at all — which is exactly when repeating is wanted. The buttons
belong to the armed gate, not to there being something queued behind.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import backend.app.models.printer_location  # noqa: F401

pytestmark = pytest.mark.unit

MOD = "backend.app.services.telegram_handlers.actions"


async def test_repeating_re_arms_and_releases_the_gate():
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    callback = MagicMock()
    callback.data = "action:repeat_print:5"
    callback.answer = AsyncMock()
    released = []

    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch(
            f"{MOD}.printer_manager.set_awaiting_plate_clear",
            MagicMock(side_effect=lambda p, v: released.append((p, v))),
        ),
        patch("backend.app.services.plate_hold.answer_by_repeating", AsyncMock(return_value=MagicMock(id=1))),
        patch("backend.app.core.database.async_session", MagicMock()),
        patch("backend.app.services.telegram_handlers.printers.show_printer_detail", AsyncMock()),
    ):
        await cb_repeat_print(callback)

    assert released == [(5, False)], "a re-armed row never dispatches while the gate is armed"
    callback.answer.assert_awaited()


async def test_repeating_without_permission_is_refused():
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    callback = MagicMock()
    callback.data = "action:repeat_print:5"
    callback.answer = AsyncMock()
    repeat = AsyncMock()

    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=False)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch("backend.app.services.plate_hold.answer_by_repeating", repeat),
    ):
        await cb_repeat_print(callback)

    repeat.assert_not_awaited()


async def test_nothing_waiting_says_so_and_leaves_the_gate_armed():
    """⚠️ The gate stays: the plate has not been dealt with, and dropping it
    would let the queue dispatch onto a bed nobody confirmed."""
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    callback = MagicMock()
    callback.data = "action:repeat_print:5"
    callback.answer = AsyncMock()
    released = []

    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch(
            f"{MOD}.printer_manager.set_awaiting_plate_clear",
            MagicMock(side_effect=lambda p, v: released.append((p, v))),
        ),
        patch("backend.app.services.plate_hold.answer_by_repeating", AsyncMock(return_value=None)),
        patch("backend.app.core.database.async_session", MagicMock()),
    ):
        await cb_repeat_print(callback)

    assert released == []
