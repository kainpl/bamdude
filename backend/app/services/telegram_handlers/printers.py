"""Printer list, details, and hours editing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import (
    NS,
    format_time,
    get_maintenance_counts,
    get_next_queue_item,
    get_printers_data,
    get_total_hours,
    has_perm,
    state_emoji,
)

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()


class PrinterHoursState(StatesGroup):
    waiting_for_hours = State()


# === Printer list ===


async def show_printer_list(message_or_callback, tg_chat: TelegramChat | None = None) -> None:
    """Show printer list with inline buttons."""
    lang = await get_language()

    if not has_perm(tg_chat, "printers:read"):
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printers = await get_printers_data()
    title = escape_md(t(lang, NS, "printers.title"))

    if not printers:
        btns = []
        if has_perm(tg_chat, "printers:create"):
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\u2795 {t(lang, NS, 'printer_add.btn_add')}",
                        callback_data="printer_add:start",
                    )
                ]
            )
        text = f"\U0001f5a8 *{title}*\n\n{escape_md(t(lang, NS, 'printers.empty'))}"
        kb = InlineKeyboardMarkup(inline_keyboard=btns) if btns else None
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(text, reply_markup=kb)
        else:
            await message_or_callback.answer(text, reply_markup=kb)
        return

    lines = [f"\U0001f5a8 *{title}*\n"]
    buttons = []

    for p in printers:
        emoji = state_emoji(p["state"]) if p["connected"] else "\u26ab"
        label = (
            escape_md(t(lang, NS, f"states.{p['state']}"))
            if p["connected"] and p["state"]
            else escape_md(t(lang, NS, "printers.offline"))
        )
        name = escape_md(p["name"])

        model = escape_md(p["model"] or "") if p["model"] else ""
        line = f"{emoji} *{name}*"
        if model:
            line += f" \\[{model}\\]"
        line += f" – {label}"
        if p["state"] == "RUNNING" and p["progress"]:
            line += f" \\({p['progress']}%\\)"

        due, warning = await get_maintenance_counts(p["id"])
        if due > 0:
            line += f" \U0001f534\U0001f527{due}"
        elif warning > 0:
            line += f" \U0001f7e1\U0001f527{warning}"

        lines.append(line)
        btn_label = f"{emoji} {p['name']}"
        if p["model"]:
            btn_label += f" [{p['model']}]"
        buttons.append(
            InlineKeyboardButton(
                text=btn_label,
                callback_data=f"printer:{p['id']}",
            )
        )

    keyboard_rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    keyboard_rows.append(
        [
            InlineKeyboardButton(
                text=f"\U0001f504 {t(lang, NS, 'printers.btn_refresh')}",
                callback_data="menu:printers",
            )
        ]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    text = "\n".join(lines)

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await message_or_callback.answer(text, reply_markup=keyboard)


# === Printer detail ===


async def show_printer_detail(
    callback: CallbackQuery,
    printer_id: int,
    tg_chat: TelegramChat | None = None,
) -> None:
    """Show printer details with control buttons."""
    lang = await get_language()
    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)

    if not printer:
        await callback.answer(t(lang, NS, "printers.not_found"), show_alert=True)
        return

    emoji = state_emoji(printer["state"]) if printer["connected"] else "\u26ab"
    label = (
        escape_md(t(lang, NS, f"states.{printer['state']}"))
        if printer["connected"] and printer["state"]
        else escape_md(t(lang, NS, "printers.offline"))
    )
    name = escape_md(printer["name"])

    lines = [
        f"{emoji} *{name}*",
        f"{escape_md(t(lang, NS, 'printers.model'))}: {escape_md(printer['model'] or '–')}",
        f"{escape_md(t(lang, NS, 'printers.status'))}: {label}",
    ]

    if printer["connected"]:
        if printer["nozzle_temp"] is not None:
            nozzle_val = f"{printer['nozzle_temp']:.0f}°C"
            lines.append(f"\U0001f321 {escape_md(t(lang, NS, 'printers.nozzle'))}: {escape_md(nozzle_val)}")
        if printer["bed_temp"] is not None:
            bed_val = f"{printer['bed_temp']:.0f}°C"
            lines.append(f"\U0001f6cf {escape_md(t(lang, NS, 'printers.bed'))}: {escape_md(bed_val)}")

    total_hours = await get_total_hours(printer_id)
    lines.append(f"\u23f0 {escape_md(t(lang, NS, 'printers.total_hours'))}: {escape_md(f'{total_hours:.1f}')}")

    if printer["state"] == "RUNNING":
        lines.append(f"\n\U0001f4c4 {escape_md(printer['current_file'] or '–')}")
        lines.append(f"\U0001f4ca {escape_md(t(lang, NS, 'printers.progress'))}: {printer['progress']}%")
        lines.append(
            f"\u23f1 {escape_md(t(lang, NS, 'printers.remaining'))}: {escape_md(format_time(lang, printer['remaining_time']))}"
        )

    # Control buttons
    btns = []
    can_control = has_perm(tg_chat, "printers:control")

    if printer["connected"] and can_control:
        if printer["state"] == "RUNNING":
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\u23f8 {t(lang, NS, 'actions.btn_pause')}", callback_data=f"action:pause:{printer_id}"
                    ),
                    InlineKeyboardButton(
                        text=f"\u23f9 {t(lang, NS, 'actions.btn_stop')}", callback_data=f"action:stop:{printer_id}"
                    ),
                    InlineKeyboardButton(
                        text=f"\U0001f3ce\ufe0f {t(lang, NS, 'actions.btn_speed')}",
                        callback_data=f"action:speed:{printer_id}",
                    ),
                ]
            )
        elif printer["state"] == "PAUSE":
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\u25b6\ufe0f {t(lang, NS, 'actions.btn_resume')}",
                        callback_data=f"action:resume:{printer_id}",
                    ),
                    InlineKeyboardButton(
                        text=f"\u23f9 {t(lang, NS, 'actions.btn_stop')}", callback_data=f"action:stop:{printer_id}"
                    ),
                    InlineKeyboardButton(
                        text=f"\U0001f3ce\ufe0f {t(lang, NS, 'actions.btn_speed')}",
                        callback_data=f"action:speed:{printer_id}",
                    ),
                ]
            )

        btns.append(
            [
                InlineKeyboardButton(
                    text=f"\U0001f4a1 {t(lang, NS, 'actions.btn_light')}", callback_data=f"action:light:{printer_id}"
                ),
                InlineKeyboardButton(
                    text=f"\U0001f4f7 {t(lang, NS, 'actions.btn_camera')}", callback_data=f"action:camera:{printer_id}"
                ),
                InlineKeyboardButton(
                    text=f"\U0001f504 {t(lang, NS, 'printers.btn_refresh')}", callback_data=f"printer:{printer_id}"
                ),
            ]
        )
    elif printer["connected"]:
        row = [
            InlineKeyboardButton(
                text=f"\U0001f504 {t(lang, NS, 'printers.btn_refresh')}", callback_data=f"printer:{printer_id}"
            )
        ]
        if has_perm(tg_chat, "camera:view"):
            row.insert(
                0,
                InlineKeyboardButton(
                    text=f"\U0001f4f7 {t(lang, NS, 'actions.btn_camera')}", callback_data=f"action:camera:{printer_id}"
                ),
            )
        btns.append(row)

    # Clear plate
    if (
        printer["state"] in ("FINISH", "FAILED")
        and printer["awaiting_plate_clear"]
        and has_perm(tg_chat, "printers:clear_plate")
    ):
        next_job = await get_next_queue_item(printer_id)
        if next_job:
            lines.append(f"\n\U0001f4e5 {escape_md(t(lang, NS, 'printers.next_in_queue', name=next_job))}")
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\u2705 {t(lang, NS, 'printers.btn_clear_plate')}",
                        callback_data=f"action:clear_plate:{printer_id}",
                    )
                ]
            )

    # Maintenance
    maint_btns = []
    if has_perm(tg_chat, "maintenance:read"):
        maint_btns.append(
            InlineKeyboardButton(
                text=f"\U0001f527 {t(lang, NS, 'printers.btn_maintenance')}", callback_data=f"maint:list:{printer_id}"
            )
        )
    if has_perm(tg_chat, "maintenance:update"):
        maint_btns.append(
            InlineKeyboardButton(
                text=f"\u23f0 {t(lang, NS, 'printers.hours')}", callback_data=f"action:hours:{printer_id}"
            )
        )
    if maint_btns:
        btns.append(maint_btns)

    # Calibration
    if printer["connected"] and printer["state"] in ("IDLE", "FINISH") and can_control:
        btns.append(
            [
                InlineKeyboardButton(
                    text=f"\U0001f527 {t(lang, NS, 'printers.btn_calibration')}",
                    callback_data=f"calib:show:{printer_id}",
                )
            ]
        )

    btns.append(
        [InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data="menu:printers")]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=btns)
    await callback.message.edit_text("\n".join(lines), reply_markup=keyboard)


# === Callback handlers ===


@router.callback_query(F.data == "menu:printers")
async def cb_printer_list(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    await show_printer_list(callback, tg_chat)


@router.callback_query(F.data.startswith("printer:"))
async def cb_printer_detail(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    printer_id = int(callback.data.split(":")[1])
    await show_printer_detail(callback, printer_id, tg_chat)


# === Hours FSM ===


@router.callback_query(F.data.startswith("action:hours:"))
async def cb_edit_hours(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    if not has_perm(tg_chat, "maintenance:update"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    total_hours = await get_total_hours(printer_id)

    await callback.answer()
    await callback.message.answer(
        f"\u23f0 {escape_md(t(lang, NS, 'printers.total_hours'))}: *{escape_md(f'{total_hours:.1f}')}*\n\n"
        f"{escape_md(t(lang, NS, 'printers.enter_hours'))}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"\u274c {t(lang, NS, 'printers.btn_cancel')}", callback_data=f"cancel_hours:{printer_id}"
                    )
                ],
            ]
        ),
    )
    await state.set_state(PrinterHoursState.waiting_for_hours)
    await state.update_data(printer_id=printer_id)


@router.callback_query(F.data.startswith("cancel_hours:"))
async def cb_cancel_hours(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await get_language()
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(escape_md(t(lang, NS, "printers.hours_cancelled")))
    printer_id = int(callback.data.split(":")[1])
    await show_printer_detail(callback, printer_id)


@router.message(PrinterHoursState.waiting_for_hours)
async def msg_set_hours(message: Message, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    data = await state.get_data()
    printer_id = data.get("printer_id")

    if not printer_id:
        await state.clear()
        return

    text = message.text.strip().replace(",", ".") if message.text else ""
    try:
        new_hours = float(text)
        if new_hours < 0:
            raise ValueError
    except ValueError:
        await message.answer(escape_md(t(lang, NS, "printers.hours_invalid")))
        return

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.id == printer_id))
        printer = result.scalar_one_or_none()
        if not printer:
            await state.clear()
            return
        runtime_hours = (printer.runtime_seconds or 0) / 3600.0
        printer.print_hours_offset = max(0, new_hours - runtime_hours)
        await db.commit()

    await state.clear()
    await message.answer(f"\u2705 {escape_md(t(lang, NS, 'printers.hours_updated', hours=f'{new_hours:.1f}'))}")


# === Menu callbacks ===


@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery, **kwargs) -> None:
    await callback.answer()
    from backend.app.services.telegram_handlers.start import cmd_help

    await cmd_help(callback.message)


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, **kwargs) -> None:
    await callback.answer()
    from backend.app.services.telegram_handlers.start import cmd_start

    await cmd_start(callback.message)
