"""Run-bound Telegram answers to the completion card.

The two operations intentionally call the shared service, rather than their
old printer-wide helpers.  A stale button must be harmless: a newer completed
run may be waiting on the same printer by the time it is tapped.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

MOD = "backend.app.services.telegram_handlers.actions"


class _SessionContext:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *_args):
        return None


def _callback(data: str) -> MagicMock:
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    return callback


def _allowed():
    return (
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch(f"{MOD}.deny_out_of_scope", AsyncMock(return_value=False)),
        patch("backend.app.core.database.async_session", MagicMock(return_value=_SessionContext())),
        patch("backend.app.services.telegram_handlers.printers.show_printer_detail", AsyncMock()),
    )


@pytest.mark.parametrize(
    ("handler_name", "callback_data", "action"),
    [
        ("cb_clear_plate", "action:clear_plate:5:77", "clear"),
        ("cb_repeat_print", "action:repeat_print:5:77", "repeat"),
    ],
)
async def test_completion_buttons_answer_the_named_run(handler_name, callback_data, action):
    from backend.app.services.telegram_handlers import actions

    answer_run = AsyncMock()
    p1, p2, p3, p4, p5 = _allowed()
    with p1, p2, p3, p4, p5, patch("backend.app.services.plate_answers.answer_plate_run", answer_run):
        await getattr(actions, handler_name)(_callback(callback_data))

    assert answer_run.await_args.kwargs["printer_id"] == 5
    assert answer_run.await_args.kwargs["expected_archive_id"] == 77
    assert answer_run.await_args.kwargs["action"] == action


@pytest.mark.parametrize("handler_name", ["cb_clear_plate", "cb_repeat_print"])
async def test_legacy_printer_wide_button_is_refused(handler_name):
    from backend.app.services.telegram_handlers import actions

    callback = _callback("action:clear_plate:5" if handler_name == "cb_clear_plate" else "action:repeat_print:5")
    answer_run = AsyncMock()
    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=True)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch("backend.app.services.plate_answers.answer_plate_run", answer_run),
    ):
        await getattr(actions, handler_name)(callback)

    answer_run.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_stale_completion_button_does_not_refresh_or_answer_another_run():
    from backend.app.services.plate_hold import StalePlateAnswer
    from backend.app.services.telegram_handlers.actions import cb_clear_plate

    callback = _callback("action:clear_plate:5:77")
    detail = AsyncMock()
    p1, p2, p3, p4, _p5 = _allowed()
    with (
        p1,
        p2,
        p3,
        p4,
        patch("backend.app.services.telegram_handlers.printers.show_printer_detail", detail),
        patch("backend.app.services.plate_answers.answer_plate_run", AsyncMock(side_effect=StalePlateAnswer())),
    ):
        await cb_clear_plate(callback)

    detail.assert_not_awaited()
    assert callback.answer.await_args.kwargs["show_alert"] is True


async def test_missing_permission_never_calls_the_answer_service():
    from backend.app.services.telegram_handlers.actions import cb_repeat_print

    answer_run = AsyncMock()
    with (
        patch(f"{MOD}.has_perm", MagicMock(return_value=False)),
        patch(f"{MOD}.get_language", AsyncMock(return_value="en")),
        patch("backend.app.services.plate_answers.answer_plate_run", answer_run),
    ):
        await cb_repeat_print(_callback("action:repeat_print:5:77"))

    answer_run.assert_not_awaited()
