"""Printer calibration selection handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.printer_manager import printer_manager
from backend.app.services.telegram_handlers.common import NS, ensure_fresh, get_printers_data, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

CALIBRATION_TYPES = {
    "bed_leveling": {"label_key": "calibration.bed_leveling", "bit": 1},
    "vibration": {"label_key": "calibration.vibration", "bit": 2},
    "motor_noise": {"label_key": "calibration.motor_noise", "bit": 3},
    "nozzle_offset": {"label_key": "calibration.nozzle_offset", "bit": 4},
    "high_temp_heatbed": {"label_key": "calibration.high_temp_heatbed", "bit": 5},
}


def _get_available_calibrations(model: str | None) -> list[str]:
    m = (model or "").upper()
    is_h2d = "H2D" in m
    is_h2 = m.startswith("H2")
    is_x1e = m == "X1E"
    is_p2s = m == "P2S"

    available = ["bed_leveling"]
    if not is_p2s:
        available.append("vibration")
    available.append("motor_noise")
    if is_h2d:
        available.append("nozzle_offset")
    if is_h2 or is_x1e:
        available.append("high_temp_heatbed")
    return available


def _render_calibration_screen(
    lang: str,
    printer_id: int,
    printer_name: str,
    model: str | None,
    selected: set[str],
) -> tuple[str, InlineKeyboardMarkup]:
    available = _get_available_calibrations(model)

    lines = [
        f"\U0001f527 *{escape_md(t(lang, NS, 'calibration.title'))}* – *{escape_md(printer_name)}*",
        escape_md(t(lang, NS, "calibration.select")),
        "",
    ]

    btns = []
    for cal_type in available:
        label = t(lang, NS, CALIBRATION_TYPES[cal_type]["label_key"])
        prefix = "\u2705" if cal_type in selected else "\u2b1c"
        btns.append(
            [
                InlineKeyboardButton(
                    text=f"{prefix} {label}",
                    callback_data=f"calib:toggle:{printer_id}:{cal_type}",
                )
            ]
        )

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u25b6\ufe0f {t(lang, NS, 'calibration.btn_start')}", callback_data=f"calib:start:{printer_id}"
            ),
            InlineKeyboardButton(
                text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data=f"printer:{printer_id}"
            ),
        ]
    )

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=btns)


_calib_selections: dict[int, set[str]] = {}


@router.callback_query(F.data.startswith("calib:show:"))
async def cb_calibration_show(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    model = printer["model"] if printer else None
    name = printer["name"] if printer else f"#{printer_id}"

    _calib_selections[callback.message.chat.id] = set()
    text, keyboard = _render_calibration_screen(lang, printer_id, name, model, set())
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("calib:toggle:"))
async def cb_calibration_toggle(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    parts = callback.data.split(":")
    printer_id = int(parts[2])
    cal_type = parts[3]

    chat_id = callback.message.chat.id
    selected = _calib_selections.get(chat_id, set())
    selected.symmetric_difference_update({cal_type})
    _calib_selections[chat_id] = selected

    await callback.answer()

    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    text, keyboard = _render_calibration_screen(
        lang,
        printer_id,
        printer["name"] if printer else f"#{printer_id}",
        printer["model"] if printer else None,
        selected,
    )
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("calib:start:"))
async def cb_calibration_start(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    selected = _calib_selections.pop(callback.message.chat.id, set())

    if not selected:
        await callback.answer(t(lang, NS, "calibration.none_selected"), show_alert=True)
        return

    await ensure_fresh(printer_id)
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        await callback.answer(t(lang, NS, "printers.not_connected"), show_alert=True)
        return

    success = client.start_calibration(
        bed_leveling="bed_leveling" in selected,
        vibration="vibration" in selected,
        motor_noise="motor_noise" in selected,
        nozzle_offset="nozzle_offset" in selected,
        high_temp_heatbed="high_temp_heatbed" in selected,
    )

    if success:
        await callback.answer(f"\u2705 {t(lang, NS, 'calibration.started')}")
    else:
        await callback.answer(t(lang, NS, "calibration.failed"), show_alert=True)

    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)
