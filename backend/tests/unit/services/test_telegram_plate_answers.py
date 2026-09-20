"""Telegram offers the same two answers as the card.

⚠️ And it must offer them in the case the card already handles: the keyboard was
built inside ``if next_job:``, so after the LAST print in a queue Telegram showed
no plate control at all — which is exactly when repeating is wanted. The buttons
belong to the armed gate, not to there being something queued behind.
"""

from contextlib import ExitStack
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
        # The session is a fake here, so the defects hook's own lookup is faked too.
        patch("backend.app.services.plate_hold.waiting_archive", AsyncMock(return_value=None)),
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
        patch("backend.app.services.plate_hold.waiting_archive", AsyncMock(return_value=None)),
        patch("backend.app.core.database.async_session", MagicMock()),
    ):
        await cb_repeat_print(callback)

    assert released == []


# === The defects prompt hangs off the answer, and only off a successful one ===


def _answer_callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    return callback


def _finished(**kwargs) -> MagicMock:
    """A waiting archive the prompt should be offered for."""
    archive = MagicMock()
    archive.id = kwargs.get("id", 77)
    archive.status = kwargs.get("status", "completed")
    archive.quantity = kwargs.get("quantity", 2)
    return archive


def _plate_patches(*, waiting, prompt, clearing=None, repeating=None):
    """The patches both answers need, plus whichever half the test drives."""
    patches = [
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch(f"{MOD}.printer_manager.set_awaiting_plate_clear", MagicMock()),
        patch("backend.app.services.plate_hold.waiting_archive", waiting),
        patch("backend.app.core.database.async_session", MagicMock()),
        patch("backend.app.services.telegram_handlers.printers.show_printer_detail", AsyncMock()),
        patch("backend.app.services.telegram_handlers.defects.start_defects_prompt", prompt),
    ]
    if clearing is not None:
        patches.append(patch("backend.app.services.plate_hold.answer_by_clearing", clearing))
    if repeating is not None:
        patches.append(patch("backend.app.services.plate_hold.answer_by_repeating", repeating))
    return patches


async def test_the_waiting_print_is_read_before_clearing_deletes_its_row():
    from backend.app.services.telegram_handlers.actions import cb_clear_plate

    order: list[str] = []

    async def _waiting(_db, _printer_id):
        order.append("resolved")
        return _finished()

    async def _clearing(_db, _printer_id):
        order.append("answered")

    prompt = AsyncMock()
    callback = _answer_callback("action:clear_plate:5")
    with ExitStack() as stack:
        for p in _plate_patches(waiting=_waiting, prompt=prompt, clearing=_clearing):
            stack.enter_context(p)
        await cb_clear_plate(callback)

    assert order == ["resolved", "answered"], "clearing deletes the row — read what it was about first"
    prompt.assert_awaited_once()
    assert prompt.await_args.args[1] == 77


async def test_repeating_offers_the_prompt_for_the_print_that_just_finished():
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    order: list[str] = []

    async def _waiting(_db, _printer_id):
        order.append("resolved")
        return _finished(id=91)

    async def _repeating(_db, _printer_id):
        order.append("answered")
        return MagicMock(id=1)

    prompt = AsyncMock()
    callback = _answer_callback("action:repeat_print:5")
    with ExitStack() as stack:
        for p in _plate_patches(waiting=_waiting, prompt=prompt, repeating=_repeating):
            stack.enter_context(p)
        await cb_repeat_print(callback)

    assert order == ["resolved", "answered"], "repeating re-arms the row — read the old print first"
    prompt.assert_awaited_once()
    assert prompt.await_args.args[1] == 91


async def test_a_refused_repeat_asks_nothing_about_defects():
    """``RepeatNotPossible``: nothing was answered, so there is nothing to grade."""
    from backend.app.services.plate_hold import RepeatNotPossible
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    prompt = AsyncMock()
    callback = _answer_callback("action:repeat_print:5")
    with ExitStack() as stack:
        for p in _plate_patches(
            waiting=AsyncMock(return_value=_finished()),
            prompt=prompt,
            repeating=AsyncMock(side_effect=RepeatNotPossible("nothing to repeat")),
        ):
            stack.enter_context(p)
        await cb_repeat_print(callback)

    prompt.assert_not_awaited()


async def test_nothing_to_repeat_asks_nothing_about_defects():
    """The gate stays armed and the question is still open — no prompt."""
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    prompt = AsyncMock()
    callback = _answer_callback("action:repeat_print:5")
    with ExitStack() as stack:
        for p in _plate_patches(
            waiting=AsyncMock(return_value=_finished()),
            prompt=prompt,
            repeating=AsyncMock(return_value=None),
        ):
            stack.enter_context(p)
        await cb_repeat_print(callback)

    prompt.assert_not_awaited()


async def test_a_failed_clear_asks_nothing_about_defects():
    from backend.app.services.telegram_handlers.actions import cb_clear_plate

    prompt = AsyncMock()
    callback = _answer_callback("action:clear_plate:5")
    with ExitStack() as stack:
        for p in _plate_patches(
            waiting=AsyncMock(return_value=_finished()),
            prompt=prompt,
            clearing=AsyncMock(side_effect=RuntimeError("database is locked")),
        ):
            stack.enter_context(p)
        await cb_clear_plate(callback)

    prompt.assert_not_awaited()


async def test_an_unfinished_waiting_print_is_not_offered_for_grading():
    """A row can wait over a failed or empty print — there is nothing good to count."""
    from backend.app.services.telegram_handlers.actions import cb_clear_plate

    prompt = AsyncMock()
    callback = _answer_callback("action:clear_plate:5")
    with ExitStack() as stack:
        for p in _plate_patches(
            waiting=AsyncMock(return_value=_finished(status="failed")),
            prompt=prompt,
            clearing=AsyncMock(),
        ):
            stack.enter_context(p)
        await cb_clear_plate(callback)
    prompt.assert_not_awaited()

    prompt = AsyncMock()
    with ExitStack() as stack:
        for p in _plate_patches(
            waiting=AsyncMock(return_value=_finished(quantity=0)),
            prompt=prompt,
            clearing=AsyncMock(),
        ):
            stack.enter_context(p)
        await cb_clear_plate(_answer_callback("action:clear_plate:5"))
    prompt.assert_not_awaited()
