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


async def camera_controls(printer_id: int, tg_chat: TelegramChat | None, lang: str) -> InlineKeyboardMarkup | None:
    """The control keyboard to hang under a camera snapshot, or ``None``.

    ``None`` rather than an empty keyboard: an idle printer, or a chat that may
    only look, gets a plain photo exactly as before.
    """
    from backend.app.services.telegram_handlers.print_controls import print_control_rows

    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    if not printer:
        return None

    rows = print_control_rows(printer, tg_chat, lang)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
                # The point of the whole feature: whoever just SAW the problem
                # can act on it here instead of reaching for a VPN and the web.
                # State is read now, beside the snapshot — not inherited from
                # the card that launched it, which may be minutes stale.
                reply_markup=await camera_controls(printer_id, tg_chat, lang),
            )
        else:
            await callback.message.answer(escape_md(t(lang, NS, "camera.failed")))
    except Exception:
        await callback.message.answer(escape_md(t(lang, NS, "camera.failed")))


# === Speed ===


@router.callback_query(F.data.startswith("action:speed:"))
async def cb_speed_menu(
    callback: CallbackQuery, tg_chat: TelegramChat | None = None, printer_id: int | None = None
) -> None:
    """Show speed mode selection.

    ``printer_id`` is parsed from ``callback.data`` when invoked directly as
    a callback handler; ``cb_speed_set`` passes it explicitly since
    ``CallbackQuery`` is a frozen model and ``data`` can't be rewritten.
    """
    lang = await get_language()

    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    if printer_id is None:
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

    markup = InlineKeyboardMarkup(inline_keyboard=btns)
    if callback.message.photo:
        # From under a camera snapshot: the speed menu is text, and a photo
        # message cannot become one. It arrives as its own message below the
        # picture, which also leaves the picture on screen where it is useful.
        await callback.message.answer(text, reply_markup=markup)
        return
    await callback.message.edit_text(text, reply_markup=markup)


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
    await cb_speed_menu(callback, tg_chat, printer_id=printer_id)


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
        printer_manager.set_awaiting_plate_clear(printer_id, False)
        # \u26a0\ufe0f The same answer as the card's Clear plate, so the held row goes here
        # too. Without this, clearing from Telegram leaks a row that nothing
        # else will ever remove.
        from backend.app.core.database import async_session
        from backend.app.services.plate_hold import answer_by_clearing

        async with async_session() as _db:
            await answer_by_clearing(_db, printer_id)
        await callback.answer(f"\u2705 {t(lang, NS, 'printers.clear_plate_ok')}")
    except Exception:
        await callback.answer(t(lang, NS, "printers.clear_plate_fail"), show_alert=True)
        return

    # Refresh printer detail
    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)


@router.callback_query(F.data.startswith("action:repeat_print:"))
async def cb_repeat_print(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Print the job that just finished again — Telegram's half of the pair.

    Same permission as clearing: they are two answers to one question.
    """
    lang = await get_language()

    if not has_perm(tg_chat, "printers:clear_plate"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])

    from backend.app.core.database import async_session
    from backend.app.services.plate_hold import answer_by_repeating

    try:
        async with async_session() as _db:
            row = await answer_by_repeating(_db, printer_id)
        if row is None:
            # ⚠️ The gate stays armed: the plate has not been dealt with, and
            # dropping it would let the queue dispatch onto a bed nobody cleared.
            await callback.answer(t(lang, NS, "printers.repeat_print_none"), show_alert=True)
            return
        # Releasing the gate is part of the answer — while it is armed
        # ``_is_printer_idle`` is False and the re-armed row would never go out.
        printer_manager.set_awaiting_plate_clear(printer_id, False)
        await callback.answer(f"✅ {t(lang, NS, 'printers.repeat_print_ok')}")
    except Exception:
        await callback.answer(t(lang, NS, "printers.clear_plate_fail"), show_alert=True)
        return

    # Refresh printer detail
    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)


# === Stop, which asks first ===


@router.callback_query(F.data.startswith("action:stop_ask:"))
async def cb_stop_ask(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Swap the keyboard for the stop question.

    ⚠️ Registered ABOVE ``cb_printer_action``, whose filter is the catch-all
    ``action:``. aiogram takes handlers in registration order within a router,
    so moving this below it would send the question to the catch-all, which
    matches no branch and silently just redraws.

    Only the keyboard changes. The message may be a photo — under a camera
    snapshot it always is — and a caption cannot become a question without
    re-uploading the image. Both screens already name the printer above.
    """
    lang = await get_language()
    if not has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    from backend.app.services.telegram_handlers.print_controls import stop_confirm_rows

    printer_id = int(callback.data.split(":")[2])
    back = f"action:controls:{printer_id}" if callback.message.photo else f"printer:{printer_id}"

    await callback.answer()
    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=stop_confirm_rows(printer_id, lang, back))
    )


@router.callback_query(F.data.startswith("action:controls:"))
async def cb_restore_controls(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Put the ordinary controls back under a photo.

    Used to cancel the stop question and to refresh after an action, because a
    photo message cannot be redrawn through ``show_printer_detail`` — that ends
    in ``edit_text``, which Telegram refuses on a photo.
    """
    lang = await get_language()
    printer_id = int(callback.data.split(":")[2])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=await camera_controls(printer_id, tg_chat, lang))


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

    if callback.message.photo:
        # Launched from under a camera snapshot. ``show_printer_detail`` ends in
        # edit_text, which Telegram refuses on a photo — so every one of these
        # actions would have worked and then reported a failure. Refresh the
        # keyboard instead: the new state (paused → resume) shows up there.
        await callback.message.edit_reply_markup(reply_markup=await camera_controls(printer_id, tg_chat, lang))
        return

    from backend.app.services.telegram_handlers.printers import show_printer_detail

    await show_printer_detail(callback, printer_id, tg_chat)
