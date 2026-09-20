"""Skipping a failed object from the bot.

The operator sees a part has come loose, from a notification, on a phone, away
from the machine. Until now the only control the bot offered was Stop — which
throws away the other nineteen parts on the plate.

⚠️ The gates are the point of this suite. Skipping is irreversible: the object
is excluded for the rest of the print and nothing brings it back. So the
confirmation must name what it is about to cancel, and an already-skipped
object must stay on screen rather than shifting the ones beside it.

Where a gate lives matters as much as what it says. The entry button asks only
what is true straight after a reconnect; every refusal that needs the 3MF read
is spoken on the screen behind it. Gating the button on ``skip_objects_supported``
made it vanish after a restart, which is the case the feature is for.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.telegram_handlers import skip_objects_scene as scene

pytestmark = pytest.mark.integration


class _Chat:
    def __init__(self, allowed: bool = True):
        self._allowed = allowed

    def has_permission(self, _perm: str) -> bool:
        return self._allowed

    def allows_printer(self, _printer_id) -> bool:
        # m157 printer scope: this double models an unscoped chat.
        return True


def _state(*, supported=True, objects=None, skipped=(), partskip=None):
    st = MagicMock()
    st.skip_objects_supported = supported
    st.printable_objects = objects if objects is not None else {1: {"name": "a"}, 2: {"name": "b"}}
    st.skipped_objects = list(skipped)
    st.print_option_support = {} if partskip is None else {"partskip": partskip}
    return st


def _manager(state) -> MagicMock:
    manager = MagicMock()
    manager.get_client.return_value = None if state is None else MagicMock(state=state)
    return manager


def _payload(objects: list[dict], *, approximate: bool = False) -> dict:
    return {
        "objects": objects,
        "total": len(objects),
        "skipped_count": sum(1 for o in objects if o["skipped"]),
        "is_printing": True,
        "bbox_all": None,
        "positions_approximate": approximate,
    }


def _obj(oid: int, name: str, *, skipped: bool = False) -> dict:
    return {"id": oid, "name": name, "x": 0.5, "y": 0.5, "norm": True, "skipped": skipped, "marker": {"x": 50, "y": 50}}


def _callback(data: str, *, photo: bool = True) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.photo = [MagicMock()] if photo else None
    cb.message.edit_text = AsyncMock()
    cb.message.edit_caption = AsyncMock()
    cb.message.edit_media = AsyncMock()
    cb.message.answer_photo = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


def _wire(monkeypatch, payload, *, picture=b"png", skip=None):
    monkeypatch.setattr(scene, "get_language", AsyncMock(return_value="en"))
    monkeypatch.setattr(scene, "_objects_payload", AsyncMock(return_value=payload))
    monkeypatch.setattr(scene, "_plate_picture", AsyncMock(return_value=picture))
    monkeypatch.setattr(scene, "_perform_skip", skip or AsyncMock())
    # A printer that CAN skip, so the refusal branch stays out of the way. The
    # tests about refusals override this after calling _wire — stated here so
    # that "no refusal" is a choice this file makes rather than a side effect
    # of get_client happening to return None.
    monkeypatch.setattr(scene, "printer_manager", _manager(_state()))


def _shown(callback) -> tuple[str, list]:
    """The caption and keyboard of whatever the handler put on screen."""
    for call in (callback.message.edit_media, callback.message.edit_caption, callback.message.edit_text):
        if call.await_args is None:
            continue
        kwargs = call.await_args.kwargs
        media = kwargs.get("media")
        text = getattr(media, "caption", None) or kwargs.get("caption") or kwargs.get("text")
        if text is None and call.await_args.args:
            text = call.await_args.args[0]
        return text or "", kwargs["reply_markup"].inline_keyboard
    raise AssertionError("nothing was rendered")


def _plain(text: str) -> str:
    """Undo MarkdownV2 escaping so a test can assert on what the operator reads.

    ``escape_md`` backslashes ``-``, ``.`` and friends, so a literal
    ``"left-bracket"`` never appears in the wire text."""
    return text.replace("\\", "")


def _callbacks(keyboard) -> list[str]:
    return [b.callback_data for row in keyboard for b in row]


# ── the entry button ────────────────────────────────────────────────────────


def test_the_button_survives_a_restart_with_no_objects_loaded(monkeypatch):
    """⚠️ The bug this suite was rewritten for.

    ``skip_objects_supported`` is written in three places — two branches of
    ``GET /print/objects`` and ``on_print_start``. A backend restarted while a
    print is running hits none of them, so the flag is False and the object
    dict empty until somebody opens the Skip dialog **in the web**. Gating the
    button on either hid it in exactly the case the feature exists for: the
    operator who is not at a computer. Measured on a live farm — restarted,
    no button."""
    monkeypatch.setattr(scene, "printer_manager", _manager(_state(supported=False, objects={})))

    assert scene.entry_button(7, "en") is not None


def test_no_button_when_the_printer_reported_it_cannot(monkeypatch):
    """``fun`` bit 49 said no — the machine itself, over telemetry, so this one
    IS true straight after a reconnect. Firmware's answer to a skip it cannot
    do is silence, which is the worst possible feedback."""
    monkeypatch.setattr(scene, "printer_manager", _manager(_state(partskip=False)))

    assert scene.entry_button(1, "en") is None


def test_no_button_when_the_printer_is_not_connected(monkeypatch):
    monkeypatch.setattr(scene, "printer_manager", _manager(None))

    assert scene.entry_button(1, "en") is None


def test_the_button_appears_when_the_action_would_work(monkeypatch):
    monkeypatch.setattr(scene, "printer_manager", _manager(_state()))

    button = scene.entry_button(7, "en")
    assert button is not None
    assert button.callback_data == "skipobj:show:7:0"


# ── refusals are explained on the screen, not by a missing button ───────────


async def test_a_plate_without_object_labels_says_so(monkeypatch):
    """Hiding explains nothing — and this is the case where somebody flips the
    wrong slicer switch for want of being told which one. Same call the web
    preview modal already made."""
    from backend.app.i18n import t

    _wire(monkeypatch, _payload([_obj(1, "a"), _obj(2, "b")]))
    monkeypatch.setattr(scene, "printer_manager", _manager(_state(supported=False)))

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat())

    caption, keyboard = _shown(cb)
    assert t("en", "telegram_ui", "skip_objects.unsupported")[:30] in _plain(caption)
    assert not any(b.callback_data.startswith("skipobj:pick:") for row in keyboard for b in row)


async def test_one_object_left_says_to_use_stop(monkeypatch):
    """Skipping the last one is Stop with extra steps and none of Stop's own
    confirmation — refused, but out loud, naming the control that does mean
    'end this print'."""
    from backend.app.i18n import t

    _wire(monkeypatch, _payload([_obj(1, "a", skipped=True), _obj(2, "b")]))
    monkeypatch.setattr(scene, "printer_manager", _manager(_state()))

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat())

    caption, keyboard = _shown(cb)
    assert t("en", "telegram_ui", "skip_objects.only_one_left")[:30] in _plain(caption)
    assert not any(b.callback_data.startswith("skipobj:pick:") for row in keyboard for b in row)


# ── the picker ──────────────────────────────────────────────────────────────


async def test_already_skipped_objects_are_shown_but_not_pressable(monkeypatch):
    """⚠️ They stay on screen deliberately.

    Dropping them would renumber nothing — the IDs are the slicer's — but it
    would move every remaining button, so the position the operator was about
    to press means a different part between one screen and the next."""
    _wire(monkeypatch, _payload([_obj(1, "left"), _obj(2, "right", skipped=True), _obj(3, "middle")]))

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat())

    _, keyboard = _shown(cb)
    data = _callbacks(keyboard)
    assert "skipobj:pick:1:1" in data
    assert "skipobj:pick:1:3" in data
    assert "skipobj:pick:1:2" not in data, "a skipped object was still pressable"
    assert any(b.callback_data == "noop" and "2" in b.text for row in keyboard for b in row)


async def test_the_screen_names_every_object_on_it(monkeypatch):
    """The picture carries numbers; the caption is what turns a number into a
    part. Without it the operator is matching shapes on a phone screen."""
    _wire(monkeypatch, _payload([_obj(1, "left-bracket"), _obj(2, "right-bracket")]))

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat())

    caption, _ = _shown(cb)
    assert "left-bracket" in _plain(caption)
    assert "right-bracket" in _plain(caption)


async def test_approximate_positions_are_stated_in_words(monkeypatch):
    """When every marker came from the grid fallback the picture is a legend,
    not a map. Saying nothing invites the operator to trust it."""
    from backend.app.i18n import t

    payload = _payload([_obj(1, "a"), _obj(2, "b")], approximate=True)
    _wire(monkeypatch, payload)

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat())

    caption, _ = _shown(cb)
    assert t("en", "telegram_ui", "skip_objects.approximate")[:20] in _plain(caption)


async def test_a_missing_top_view_costs_the_picture_not_the_feature(monkeypatch):
    """⚠️ Never drawn on the ¾ render instead — see plate_marker_render. The
    list still works; it just stops claiming to show where."""
    from backend.app.i18n import t

    _wire(monkeypatch, _payload([_obj(1, "a"), _obj(2, "b")]), picture=None)

    cb = _callback("skipobj:show:1:0", photo=False)
    await scene.cb_show(cb, tg_chat=_Chat())

    caption, keyboard = _shown(cb)
    assert t("en", "telegram_ui", "skip_objects.no_picture")[:20] in _plain(caption)
    assert "skipobj:pick:1:1" in _callbacks(keyboard)


async def test_a_long_plate_paginates(monkeypatch):
    """Rows of five, and a page that stops before the caption hits Telegram's
    1024-character cap."""
    _wire(monkeypatch, _payload([_obj(i, f"part-{i}") for i in range(1, 41)]))

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat())

    _, keyboard = _shown(cb)
    object_rows = [row for row in keyboard if any(b.callback_data.startswith("skipobj:pick:") for b in row)]
    assert all(len(row) <= 5 for row in object_rows)
    assert sum(len(row) for row in object_rows) == scene.PAGE_SIZE
    assert f"skipobj:show:1:{scene.PAGE_SIZE}" in _callbacks(keyboard), "no way to reach the rest"


async def test_a_second_page_shows_the_objects_the_first_did_not(monkeypatch):
    _wire(monkeypatch, _payload([_obj(i, f"part-{i}") for i in range(1, 41)]))

    cb = _callback(f"skipobj:show:1:{scene.PAGE_SIZE}")
    await scene.cb_show(cb, tg_chat=_Chat())

    _, keyboard = _shown(cb)
    assert f"skipobj:pick:1:{scene.PAGE_SIZE + 1}" in _callbacks(keyboard)
    assert "skipobj:pick:1:1" not in _callbacks(keyboard)


async def test_without_the_control_permission_nothing_is_shown(monkeypatch):
    _wire(monkeypatch, _payload([_obj(1, "a"), _obj(2, "b")]))

    cb = _callback("skipobj:show:1:0")
    await scene.cb_show(cb, tg_chat=_Chat(allowed=False))

    assert cb.answer.await_args.kwargs.get("show_alert") is True
    cb.message.edit_media.assert_not_awaited()
    cb.message.edit_caption.assert_not_awaited()


# ── the confirmation ────────────────────────────────────────────────────────


async def test_a_press_asks_for_confirmation_naming_the_object(monkeypatch):
    """⚠️ Warranted here and nowhere else in these controls: pause and resume
    are reversible, this is not. And the name is what the operator checks
    against — a bare "are you sure?" confirms only that a button was pressed."""
    skip = AsyncMock()
    _wire(monkeypatch, _payload([_obj(1, "left-bracket"), _obj(2, "right")]), skip=skip)

    cb = _callback("skipobj:pick:1:1")
    await scene.cb_pick(cb, tg_chat=_Chat())

    caption, keyboard = _shown(cb)
    assert "left\\-bracket" in caption or "left-bracket" in caption
    assert "skipobj:do:1:1" in _callbacks(keyboard)
    skip.assert_not_awaited(), "the press itself must not skip anything"


async def test_confirming_calls_skip_and_redraws_from_fresh_state(monkeypatch):
    """The redraw comes from the payload, not from patching the old screen —
    the printer is the authority on what is skipped, including objects skipped
    from the web while this dialog sat open."""
    skip = AsyncMock()
    after = _payload([_obj(1, "left", skipped=True), _obj(2, "right"), _obj(3, "middle")])
    _wire(monkeypatch, after, skip=skip)

    cb = _callback("skipobj:do:1:1")
    await scene.cb_do(cb, tg_chat=_Chat())

    skip.assert_awaited_once_with(1, 1)
    scene._objects_payload.assert_awaited()
    _, keyboard = _shown(cb)
    assert "skipobj:pick:1:1" not in _callbacks(keyboard)


async def test_a_refused_skip_is_reported_rather_than_swallowed(monkeypatch):
    """The endpoint raises for both of BS's gates and for a dead MQTT. Any of
    them means the part is still printing, which the operator must know."""
    from fastapi import HTTPException

    skip = AsyncMock(side_effect=HTTPException(409, "This printer does not support skipping objects"))
    _wire(monkeypatch, _payload([_obj(1, "left"), _obj(2, "right")]), skip=skip)

    cb = _callback("skipobj:do:1:1")
    await scene.cb_do(cb, tg_chat=_Chat())

    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_confirming_without_permission_does_not_skip(monkeypatch):
    """The gate is re-checked at the last step, not only at the first: the
    keyboard survives on screen long after permissions can change."""
    skip = AsyncMock()
    _wire(monkeypatch, _payload([_obj(1, "a"), _obj(2, "b")]), skip=skip)

    cb = _callback("skipobj:do:1:1")
    await scene.cb_do(cb, tg_chat=_Chat(allowed=False))

    skip.assert_not_awaited()


# ── leaving the picker ──────────────────────────────────────────────────────


async def test_back_from_a_photo_screen_deletes_it(monkeypatch):
    """⚠️ The picker is a photo, and Telegram refuses to edit text into one.
    A plain ``printer:{id}`` button lands in show_printer_detail, which ends in
    edit_text — so Back did nothing at all on the screens that work best. The
    printer card is still above, so removing the photo is the way out."""
    monkeypatch.setattr(scene, "get_language", AsyncMock(return_value="en"))
    cb = _callback("skipobj:back:1")
    cb.message.delete = AsyncMock()

    await scene.cb_back(cb, tg_chat=_Chat())

    cb.message.delete.assert_awaited_once()


async def test_back_from_a_text_screen_redraws_the_card(monkeypatch):
    """No top view → the picker edited the card itself, so there is nothing
    above to return to."""
    monkeypatch.setattr(scene, "get_language", AsyncMock(return_value="en"))
    detail = AsyncMock()
    monkeypatch.setattr("backend.app.services.telegram_handlers.printers.show_printer_detail", detail)

    cb = _callback("skipobj:back:1", photo=False)
    await scene.cb_back(cb, tg_chat=_Chat())

    detail.assert_awaited_once()
