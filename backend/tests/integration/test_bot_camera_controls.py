"""Print controls under the camera snapshot.

Asked for by @vladyslav_biletskyi: *"I pull /camera, I see on the picture that
it is printing into thin air. And then I have to switch on the VPN, go to the
URL and stop the print, instead of just hitting stop from the bot."*

None of the actions were missing — the bot has handled pause, resume and stop
for a long time. The snapshot simply went out with no keyboard, so the person
who had just seen the problem had nowhere to press. These tests pin the path,
and the two things that were decided rather than inherited: one builder for
both screens, and stop asking first.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.telegram_handlers import actions, print_controls

pytestmark = pytest.mark.integration


class _Chat:
    def __init__(self, allowed: bool = True):
        self._allowed = allowed

    def has_permission(self, _perm: str) -> bool:
        return self._allowed


def _printer(state="RUNNING", *, connected=True, pid=3):
    return {"id": pid, "name": "P1S-02", "state": state, "connected": connected}


def _no_skip(monkeypatch):
    """Silence the skip button — it needs a live MQTT client of its own."""
    manager = MagicMock()
    manager.get_client.return_value = None
    monkeypatch.setattr("backend.app.services.telegram_handlers.skip_objects_scene.printer_manager", manager)


def _data(keyboard) -> list[str]:
    return [b.callback_data for row in keyboard for b in row]


# ── one builder, two screens ────────────────────────────────────────────────


def test_a_running_printer_offers_pause_stop_and_speed(monkeypatch):
    _no_skip(monkeypatch)

    rows = print_controls.print_control_rows(_printer("RUNNING"), _Chat(), "en")

    assert _data(rows) == ["action:pause:3", "action:stop_ask:3", "action:speed:3"]


def test_a_paused_printer_offers_resume_instead(monkeypatch):
    _no_skip(monkeypatch)

    rows = print_controls.print_control_rows(_printer("PAUSE"), _Chat(), "en")

    assert "action:resume:3" in _data(rows)
    assert "action:pause:3" not in _data(rows)


def test_an_idle_printer_gets_nothing(monkeypatch):
    """Returned empty rather than a keyboard of dead buttons, so a caller can
    append the result unconditionally."""
    _no_skip(monkeypatch)

    assert print_controls.print_control_rows(_printer("IDLE"), _Chat(), "en") == []


def test_a_viewer_gets_nothing(monkeypatch):
    """⚠️ Same gate as the printer card. A chat allowed to watch the camera
    must not gain Stop by way of the picture."""
    _no_skip(monkeypatch)

    assert print_controls.print_control_rows(_printer("RUNNING"), _Chat(allowed=False), "en") == []


def test_an_offline_printer_gets_nothing(monkeypatch):
    _no_skip(monkeypatch)

    assert print_controls.print_control_rows(_printer("RUNNING", connected=False), _Chat(), "en") == []


def test_the_skip_button_rides_along_when_the_printer_can_skip(monkeypatch):
    """The pairing this was built for: saw the defect on camera, drop that one
    part instead of the whole plate."""
    state = MagicMock()
    state.print_option_support = {}
    manager = MagicMock()
    manager.get_client.return_value = MagicMock(state=state)
    monkeypatch.setattr("backend.app.services.telegram_handlers.skip_objects_scene.printer_manager", manager)

    rows = print_controls.print_control_rows(_printer("RUNNING"), _Chat(), "en")

    assert "skipobj:show:3:0" in _data(rows)


# ── the snapshot carries them ───────────────────────────────────────────────


async def test_the_snapshot_keyboard_is_built_from_fresh_state(monkeypatch):
    """⚠️ Read beside the snapshot, not inherited from the card that launched
    it — that card may be minutes old, and the keyboard describes now."""
    _no_skip(monkeypatch)
    printers = AsyncMock(return_value=[_printer("RUNNING")])
    monkeypatch.setattr(actions, "get_printers_data", printers)

    markup = await actions.camera_controls(3, _Chat(), "en")

    printers.assert_awaited_once()
    assert "action:stop_ask:3" in _data(markup.inline_keyboard)


async def test_an_idle_printer_photo_stays_a_plain_photo(monkeypatch):
    """None, not an empty keyboard — Telegram would render a stray blank."""
    _no_skip(monkeypatch)
    monkeypatch.setattr(actions, "get_printers_data", AsyncMock(return_value=[_printer("IDLE")]))

    assert await actions.camera_controls(3, _Chat(), "en") is None


# ── stop asks first ─────────────────────────────────────────────────────────


def _callback(data: str, *, photo: bool):
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.photo = [MagicMock()] if photo else None
    cb.message.edit_reply_markup = AsyncMock()
    cb.message.edit_text = AsyncMock()
    return cb


async def test_pressing_stop_only_asks(monkeypatch):
    """⚠️ Stop ends the print and discards every part on the plate — the most
    destructive control the bot has, sitting next to Pause on a phone. Skipping
    one object already asked; this not asking was the wrong way round."""
    monkeypatch.setattr(actions, "get_language", AsyncMock(return_value="en"))
    stop = MagicMock()
    monkeypatch.setattr(actions.printer_manager, "stop_print", stop)

    cb = _callback("action:stop_ask:3", photo=True)
    await actions.cb_stop_ask(cb, tg_chat=_Chat())

    stop.assert_not_called()
    keyboard = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert "action:stop:3" in _data(keyboard), "no way to confirm"


async def test_the_question_is_asked_by_swapping_only_the_keyboard(monkeypatch):
    """The camera message is a PHOTO: its caption cannot become a question
    without re-uploading the image, and edit_text is refused outright."""
    monkeypatch.setattr(actions, "get_language", AsyncMock(return_value="en"))

    cb = _callback("action:stop_ask:3", photo=True)
    await actions.cb_stop_ask(cb, tg_chat=_Chat())

    cb.message.edit_text.assert_not_awaited()
    cb.message.edit_reply_markup.assert_awaited_once()


async def test_cancelling_from_a_photo_restores_the_controls(monkeypatch):
    monkeypatch.setattr(actions, "get_language", AsyncMock(return_value="en"))

    cb = _callback("action:stop_ask:3", photo=True)
    await actions.cb_stop_ask(cb, tg_chat=_Chat())

    keyboard = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert "action:controls:3" in _data(keyboard)


async def test_cancelling_from_the_card_redraws_the_card(monkeypatch):
    """There the full card has more than these buttons — light, camera, back —
    so only a redraw puts them all back."""
    monkeypatch.setattr(actions, "get_language", AsyncMock(return_value="en"))

    cb = _callback("action:stop_ask:3", photo=False)
    await actions.cb_stop_ask(cb, tg_chat=_Chat())

    keyboard = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"].inline_keyboard
    assert "printer:3" in _data(keyboard)


async def test_a_viewer_cannot_even_open_the_question(monkeypatch):
    monkeypatch.setattr(actions, "get_language", AsyncMock(return_value="en"))

    cb = _callback("action:stop_ask:3", photo=True)
    await actions.cb_stop_ask(cb, tg_chat=_Chat(allowed=False))

    cb.message.edit_reply_markup.assert_not_awaited()
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_an_action_under_a_photo_refreshes_the_keyboard_not_the_text(monkeypatch):
    """⚠️ ``show_printer_detail`` ends in edit_text, which Telegram refuses on a
    photo — so pause from under a snapshot used to work and then report a
    failure. The new state shows up in the keyboard instead."""
    _no_skip(monkeypatch)
    monkeypatch.setattr(actions, "get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(actions, "ensure_fresh", AsyncMock(return_value=True))
    monkeypatch.setattr(actions, "get_printers_data", AsyncMock(return_value=[_printer("PAUSE")]))
    client = MagicMock()
    monkeypatch.setattr(actions.printer_manager, "get_client", lambda _pid: client)

    cb = _callback("action:pause:3", photo=True)
    await actions.cb_printer_action(cb, tg_chat=_Chat())

    client.pause_print.assert_called_once()
    cb.message.edit_text.assert_not_awaited()
    cb.message.edit_reply_markup.assert_awaited()
