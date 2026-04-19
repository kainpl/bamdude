"""Printer action handlers: pause, stop, resume, light, clear plate, camera, speed."""

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

# Speed mode definitions
SPEED_MODES = {
    1: {"key": "speed.silent", "emoji": "\U0001f422"},
    2: {"key": "speed.standard", "emoji": "\u2699\ufe0f"},
    3: {"key": "speed.sport", "emoji": "\U0001f3ce\ufe0f"},
    4: {"key": "speed.ludicrous", "emoji": "\U0001f680"},
}


# === Camera ===


@router.callback_query(F.data.startswith("action:camera:"))
async def cb_camera_snapshot(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Capture and send a camera snapshot."""
    lang = await get_language()

    if not has_perm(tg_chat, "camera:view"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    await callback.answer(t(lang, NS, "camera.capturing"))

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()

    if not printer:
        await callback.message.answer(escape_md(t(lang, NS, "printers.not_found")))
        return

    # Try capture
    try:
        from backend.app.services.camera import capture_camera_frame_bytes

        jpeg_bytes = await capture_camera_frame_bytes(
            ip_address=printer.ip_address,
            access_code=printer.access_code,
            model=printer.model,
        )

        if jpeg_bytes:
            from aiogram.types import BufferedInputFile

            photo = BufferedInputFile(jpeg_bytes, filename="snapshot.jpg")
            await callback.message.answer_photo(
                photo=photo,
                caption=f"\U0001f4f7 {escape_md(printer.name)}",
            )
        else:
            await callback.message.answer(escape_md(t(lang, NS, "camera.failed")))
    except Exception:
        await callback.message.answer(escape_md(t(lang, NS, "camera.failed")))


# === Speed ===


@router.callback_query(F.data.startswith("action:speed:"))
async def cb_speed_menu(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Show speed mode selection."""
    lang = await get_language()

    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    current_speed = printer["speed_level"] if printer else 2
    name = escape_md(printer["name"]) if printer else f"#{printer_id}"

    text = f"\U0001f3ce\ufe0f *{escape_md(t(lang, NS, 'speed.title'))}* – *{name}*"

    btns = []
    for mode, info in SPEED_MODES.items():
        label = t(lang, NS, info["key"])
        check = " \u2705" if mode == current_speed else ""
        btns.append(
            [
                InlineKeyboardButton(
                    text=f"{info['emoji']} {label}{check}",
                    callback_data=f"speed:set:{printer_id}:{mode}",
                )
            ]
        )

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}",
                callback_data=f"printer:{printer_id}",
            )
        ]
    )

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


@router.callback_query(F.data.startswith("speed:set:"))
async def cb_speed_set(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Set print speed mode."""
    lang = await get_language()

    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    printer_id = int(parts[2])
    mode = int(parts[3])

    await ensure_fresh(printer_id)
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        await callback.answer(t(lang, NS, "printers.not_connected"), show_alert=True)
        return

    client.set_print_speed(mode)
    mode_label = t(lang, NS, SPEED_MODES[mode]["key"])
    await callback.answer(f"\u2705 {t(lang, NS, 'speed.set_ok', mode=mode_label)}")

    # Refresh speed menu
    callback.data = f"action:speed:{printer_id}"
    await cb_speed_menu(callback, tg_chat)


# === Clear plate ===


@router.callback_query(F.data.startswith("action:clear_plate:"))
async def cb_clear_plate(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Clear plate confirmation."""
    lang = await get_language()

    if not has_perm(tg_chat, "printers:clear_plate"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])

    try:
        printer_manager.set_plate_cleared(printer_id)
        await callback.answer(f"\u2705 {t(lang, NS, 'printers.clear_plate_ok')}")
    except Exception:
        await callback.answer(t(lang, NS, "printers.clear_plate_fail"), show_alert=True)
        return

    # Refresh printer detail
    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)


# === Generic actions (pause, stop, resume, light) - catch-all, must be last ===


@router.callback_query(F.data.startswith("action:"))
async def cb_printer_action(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Handle printer control actions."""
    lang = await get_language()

    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    printer_id = int(parts[2])

    await ensure_fresh(printer_id)

    if action == "pause":
        client = printer_manager.get_client(printer_id)
        if client:
            client.pause_print()
            await callback.answer(f"\u23f8 {t(lang, NS, 'actions.pause_ok')}")
        else:
            await callback.answer(t(lang, NS, "printers.not_connected"), show_alert=True)

    elif action == "resume":
        client = printer_manager.get_client(printer_id)
        if client:
            client.resume_print()
            await callback.answer(f"\u25b6\ufe0f {t(lang, NS, 'actions.resume_ok')}")
        else:
            await callback.answer(t(lang, NS, "printers.not_connected"), show_alert=True)

    elif action == "stop":
        success = printer_manager.stop_print(printer_id)
        if success:
            await callback.answer(f"\u23f9 {t(lang, NS, 'actions.stop_ok')}")
        else:
            await callback.answer(t(lang, NS, "actions.stop_fail"), show_alert=True)

    elif action == "light":
        client = printer_manager.get_client(printer_id)
        if client and client.state:
            new_state = not client.state.chamber_light
            client.set_chamber_light(new_state)
            light_msg = t(lang, NS, "actions.light_on") if new_state else t(lang, NS, "actions.light_off")
            await callback.answer(f"\U0001f4a1 {light_msg}")
        else:
            await callback.answer(t(lang, NS, "printers.not_connected"), show_alert=True)

    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)
