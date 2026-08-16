"""The print-control keyboard, built once and shown wherever it is needed.

Asked for by @vladyslav_biletskyi, in their own words: *"I pull /camera, I see
on the picture that it is printing into thin air. And then I have to switch on
the VPN, go to the URL and stop the print, instead of just hitting stop from
the bot."*

Nothing was missing but the **path**. The bot could already pause, resume and
stop — ``actions.py`` has handled all three for a long time — and the buttons
were built on the printer card. The camera snapshot simply went out with no
``reply_markup`` at all, so somebody who had just seen the problem with their
own eyes was in a dead end.

⚠️ **One builder, deliberately.** These buttons are gated by printer state and
by permission, and a second copy would drift from the first the next time
either gate changes. That is the whole reason this module exists rather than a
few lines copied under the photo.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton

from backend.app.i18n import t
from backend.app.services.telegram_handlers.common import NS, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

PRINTING_STATES = ("RUNNING", "PAUSE")


def print_control_rows(
    printer: dict,
    tg_chat: TelegramChat | None,
    lang: str,
    *,
    include_skip: bool = True,
) -> list[list[InlineKeyboardButton]]:
    """Pause / resume / stop / speed for one printer, plus Skip an object.

    ``printer`` is one entry of :func:`common.get_printers_data` — read fresh
    by the caller, never carried over from an older message. The keyboard
    describes the printer *now*: a card rendered five minutes ago and a
    snapshot taken this second must not offer the same buttons.

    Returns ``[]`` when the chat may not control this printer or the printer is
    not printing, so a caller can append the result unconditionally.
    """
    if not printer.get("connected") or not has_perm(tg_chat, "printers:control"):
        return []

    printer_id = printer["id"]
    state = printer.get("state")
    rows: list[list[InlineKeyboardButton]] = []

    if state == "RUNNING":
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⏸ {t(lang, NS, 'actions.btn_pause')}", callback_data=f"action:pause:{printer_id}"
                ),
                _stop_button(printer_id, lang),
                InlineKeyboardButton(
                    text=f"\U0001f3ce️ {t(lang, NS, 'actions.btn_speed')}",
                    callback_data=f"action:speed:{printer_id}",
                ),
            ]
        )
    elif state == "PAUSE":
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"▶️ {t(lang, NS, 'actions.btn_resume')}",
                    callback_data=f"action:resume:{printer_id}",
                ),
                _stop_button(printer_id, lang),
                InlineKeyboardButton(
                    text=f"\U0001f3ce️ {t(lang, NS, 'actions.btn_speed')}",
                    callback_data=f"action:speed:{printer_id}",
                ),
            ]
        )

    if include_skip and state in PRINTING_STATES:
        # The pairing this was built for: saw the defect on camera → drop that
        # one part instead of the whole plate. Whether THIS plate can be
        # skipped is answered on the screen behind the button, not here — the
        # flag that would answer it is empty until the objects load.
        from backend.app.services.telegram_handlers.skip_objects_scene import entry_button

        skip = entry_button(printer_id, lang)
        if skip:
            rows.append([skip])

    return rows


def _stop_button(printer_id: int, lang: str) -> InlineKeyboardButton:
    """Stop, routed through a confirmation.

    ⚠️ ``action:stop_ask:`` — NOT ``action:stop:``, which is still the handler
    that actually stops. Stop ends the print and discards every part on the
    plate; it is the most destructive control the bot has, and it lives on a
    phone screen next to Pause. Skipping a single object already asks, so stop
    not asking was the wrong way round.

    Confirmed everywhere rather than only under the camera photo: one button
    that behaves differently depending on which screen it is on is exactly the
    drift this module exists to prevent.
    """
    return InlineKeyboardButton(
        text=f"⏹ {t(lang, NS, 'actions.btn_stop')}", callback_data=f"action:stop_ask:{printer_id}"
    )


def stop_confirm_rows(printer_id: int, lang: str, back: str) -> list[list[InlineKeyboardButton]]:
    """The two buttons the stop question is asked with.

    Swapped in over whatever message is on screen, which is why the question
    lives in the button label rather than in the text: the camera path's
    message is a PHOTO, and its caption cannot become a question without
    re-uploading the image. Both screens already name the printer above the
    keyboard, so the label does not have to.

    ``back`` is the callback that restores the ordinary keyboard.
    """
    return [
        [
            InlineKeyboardButton(
                text=f"⏹ {t(lang, NS, 'actions.stop_confirm')}",
                callback_data=f"action:stop:{printer_id}",
            ),
            InlineKeyboardButton(text=f"❌ {t(lang, NS, 'printers.btn_cancel')}", callback_data=back),
        ]
    ]
