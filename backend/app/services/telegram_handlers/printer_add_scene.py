"""Add printer scene — enter IP → auto-detect → enter access code → confirm."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import NS, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()


class AddPrinterState(StatesGroup):
    entering_ip = State()
    entering_access_code = State()
    confirming = State()


async def _probe_printer_ip(ip: str) -> dict | None:
    """Probe an IP for a Bambu printer. Returns {serial, name, model} or None."""

    from backend.app.services.discovery import subnet_scanner

    try:
        # Check ports (990 FTP + 8883 MQTT)
        ftp_open = await subnet_scanner._check_port(ip, 990, 2.0)
        if not ftp_open:
            return None
        mqtt_open = await subnet_scanner._check_port(ip, 8883, 2.0)
        if not mqtt_open:
            return None

        # Get printer info via SSDP unicast
        serial, name, model = await subnet_scanner._get_printer_info_ssdp(ip, 3.0)

        return {
            "serial": serial or f"unknown-{ip.replace('.', '-')}",
            "name": name or f"Printer at {ip}",
            "model": model,
        }
    except Exception:
        return None


@router.callback_query(F.data == "printer_add:start")
async def cb_add_printer_start(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Start add printer scene."""
    lang = await get_language()

    if not has_perm(tg_chat, "printers:create"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    await callback.answer()
    await state.set_state(AddPrinterState.entering_ip)

    await callback.message.answer(
        f"\U0001f5a8 *{escape_md(t(lang, NS, 'printer_add.title'))}*\n\n"
        f"{escape_md(t(lang, NS, 'printer_add.enter_ip'))}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"\u274c {t(lang, NS, 'printers.btn_cancel')}", callback_data="printer_add:cancel"
                    )
                ],
            ]
        ),
    )


@router.message(AddPrinterState.entering_ip)
async def msg_ip(message: Message, state: FSMContext, **kwargs) -> None:
    """User entered IP — probe the network."""
    lang = await get_language()
    ip = message.text.strip() if message.text else ""

    import re

    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        await message.answer(escape_md(t(lang, NS, "printer_add.ip_invalid")))
        return

    # Probe
    await message.answer(escape_md(t(lang, NS, "printer_add.probing")))

    info = await _probe_printer_ip(ip)

    if not info:
        await message.answer(
            escape_md(t(lang, NS, "printer_add.not_found")),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"\u274c {t(lang, NS, 'printers.btn_cancel')}", callback_data="printer_add:cancel"
                        )
                    ],
                ]
            ),
        )
        return

    # Found! Store info and ask for access code
    await state.update_data(
        ip_address=ip,
        serial_number=info["serial"],
        name=info["name"],
        model=info["model"],
    )
    await state.set_state(AddPrinterState.entering_access_code)

    model_str = escape_md(info["model"] or "?")
    name_str = escape_md(info["name"])
    serial_str = escape_md(info["serial"])

    await message.answer(
        f"\u2705 {escape_md(t(lang, NS, 'printer_add.found'))}\n\n"
        f"{escape_md(t(lang, NS, 'printer_add.field_name'))}: *{name_str}*\n"
        f"{escape_md(t(lang, NS, 'printers.model'))}: *{model_str}*\n"
        f"Serial: *{serial_str}*\n\n"
        f"{escape_md(t(lang, NS, 'printer_add.enter_access_code'))}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"\u274c {t(lang, NS, 'printers.btn_cancel')}", callback_data="printer_add:cancel"
                    )
                ],
            ]
        ),
    )


@router.message(AddPrinterState.entering_access_code)
async def msg_access_code(message: Message, state: FSMContext, **kwargs) -> None:
    """User entered access code — show confirmation."""
    lang = await get_language()
    code = message.text.strip() if message.text else ""

    if not code or len(code) > 20:
        await message.answer(escape_md(t(lang, NS, "printer_add.access_code_invalid")))
        return

    await state.update_data(access_code=code)
    await state.set_state(AddPrinterState.confirming)

    data = await state.get_data()

    text = (
        f"\U0001f5a8 *{escape_md(t(lang, NS, 'printer_add.confirm_title'))}*\n\n"
        f"{escape_md(t(lang, NS, 'printer_add.field_name'))}: *{escape_md(data['name'])}*\n"
        f"IP: *{escape_md(data['ip_address'])}*\n"
        f"{escape_md(t(lang, NS, 'printers.model'))}: *{escape_md(data.get('model') or '?')}*\n"
        f"Serial: *{escape_md(data['serial_number'])}*"
    )

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"\u2705 {t(lang, NS, 'printer_add.btn_add')}", callback_data="printer_add:confirm"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"\u274c {t(lang, NS, 'printers.btn_cancel')}", callback_data="printer_add:cancel"
                    )
                ],
            ]
        ),
    )


@router.callback_query(F.data == "printer_add:confirm")
async def cb_confirm(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Create the printer."""
    lang = await get_language()
    data = await state.get_data()
    await state.clear()

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer

    try:
        async with async_session() as db:
            # Check duplicate serial
            existing = await db.execute(select(Printer).where(Printer.serial_number == data["serial_number"]))
            if existing.scalar_one_or_none():
                await callback.answer(t(lang, NS, "printer_add.duplicate_serial"), show_alert=True)
                return

            printer = Printer(
                name=data["name"],
                ip_address=data["ip_address"],
                access_code=data["access_code"],
                serial_number=data["serial_number"],
                model=data.get("model"),
            )
            db.add(printer)
            await db.commit()

            # Connect
            from backend.app.services.printer_manager import printer_manager

            await printer_manager.connect_printer(printer)

        await callback.answer(f"\u2705 {t(lang, NS, 'printer_add.success')}")
    except Exception:
        await callback.answer(t(lang, NS, "printer_add.failed"), show_alert=True)

    from backend.app.services.telegram_handlers.printers import show_printer_list

    await show_printer_list(callback, tg_chat)


@router.callback_query(F.data == "printer_add:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    """Cancel add printer."""
    lang = await get_language()
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(escape_md(t(lang, NS, "printers.hours_cancelled")))
