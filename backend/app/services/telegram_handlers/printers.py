"""Printer list, details, and control handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from backend.app.i18n import t, get_language, escape_md
from backend.app.services.printer_manager import printer_manager

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

NS = "telegram_ui"

STATE_EMOJIS = {
    "IDLE": "\U0001f7e2",
    "RUNNING": "\U0001f535",
    "PAUSE": "\U0001f7e1",
    "FINISH": "\u2705",
    "FAILED": "\U0001f534",
}


def _state_emoji(state: str | None) -> str:
    return STATE_EMOJIS.get(state or "", "\u26aa")


class PrinterHoursState(StatesGroup):
    waiting_for_hours = State()


async def _get_total_hours(printer_id: int) -> float:
    """Get total print hours for a printer."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(
            select(Printer.runtime_seconds, Printer.print_hours_offset).where(Printer.id == printer_id)
        )
        row = result.one_or_none()
        if not row:
            return 0.0
        return (row[0] or 0) / 3600.0 + (row[1] or 0.0)


async def _get_next_queue_item(printer_id: int) -> str | None:
    """Get the name of the next pending queue item for this printer (or any matching model)."""
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(
            select(PrintQueueItem)
            .where(
                PrintQueueItem.status == "pending",
                PrintQueueItem.printer_id == printer_id,
            )
            .order_by(PrintQueueItem.position)
            .limit(1)
        )
        item = result.scalar_one_or_none()
        if item:
            return item.file_name or f"Job #{item.id}"
    return None


def _has_perm(tg_chat: TelegramChat | None, perm: str) -> bool:
    """Check permission, allowing all if no tg_chat (auth disabled)."""
    if tg_chat is None:
        return True
    return tg_chat.has_permission(perm)


async def _get_printers_data() -> list[dict]:
    """Get all printers with their status from printer_manager."""
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(select(Printer).where(Printer.is_active == True))  # noqa: E712
        printers = list(result.scalars().all())

    data = []
    for p in printers:
        status = printer_manager.get_status(p.id)
        temps = status.temperatures if status else {}
        data.append({
            "id": p.id,
            "name": p.name,
            "model": p.model,
            "connected": status.connected if status else False,
            "state": status.state if status else None,
            "progress": status.progress if status else 0,
            "current_file": (status.subtask_name or status.current_print) if status else None,
            "nozzle_temp": temps.get("nozzle"),
            "bed_temp": temps.get("bed"),
            "remaining_time": status.remaining_time if status else None,
            "plate_cleared": printer_manager.is_plate_cleared(p.id),
        })

    return data


def _format_time(lang: str, minutes: int | None) -> str:
    if not minutes:
        return "–"
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return t(lang, NS, "printers.time_hm", h=hours, m=mins)
    return t(lang, NS, "printers.time_m", m=mins)


async def _get_maintenance_counts(printer_id: int) -> tuple[int, int]:
    """Get (due_count, warning_count) for a printer. Returns (0,0) on error."""
    try:
        from backend.app.core.database import async_session
        from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types

        async with async_session() as db:
            await ensure_default_types(db)
            overview = await _get_printer_maintenance_internal(printer_id, db, commit=False)
            if overview:
                return overview.due_count, overview.warning_count
    except Exception:
        pass
    return 0, 0


async def show_printer_list(message_or_callback, tg_chat: TelegramChat | None = None) -> None:
    """Show printer list with inline buttons."""
    lang = await get_language()

    if not _has_perm(tg_chat, "printers:read"):
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printers = await _get_printers_data()
    title = escape_md(t(lang, NS, "printers.title"))

    if not printers:
        text = f"\U0001f5a8 *{title}*\n\n{escape_md(t(lang, NS, 'printers.empty'))}"
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(text)
        else:
            await message_or_callback.answer(text)
        return

    lines = [f"\U0001f5a8 *{title}*\n"]
    buttons = []

    for p in printers:
        emoji = _state_emoji(p["state"]) if p["connected"] else "\u26ab"
        label = (
            escape_md(t(lang, NS, f"states.{p['state']}"))
            if p["connected"] and p["state"]
            else escape_md(t(lang, NS, "printers.offline"))
        )
        name = escape_md(p["name"])

        line = f"{emoji} *{name}* – {label}"
        if p["state"] == "RUNNING" and p["progress"]:
            line += f" \\({p['progress']}%\\)"

        # Maintenance indicator
        due, warning = await _get_maintenance_counts(p["id"])
        if due > 0:
            line += f" \U0001f534\U0001f527{due}"
        elif warning > 0:
            line += f" \U0001f7e1\U0001f527{warning}"

        lines.append(line)

        buttons.append(
            InlineKeyboardButton(
                text=f"{emoji} {p['name']}",
                callback_data=f"printer:{p['id']}",
            )
        )

    keyboard_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    keyboard_rows.append([InlineKeyboardButton(
        text=f"\U0001f504 {t(lang, NS, 'printers.btn_refresh')}", callback_data="menu:printers",
    )])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    text = "\n".join(lines)

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await message_or_callback.answer(text, reply_markup=keyboard)


async def _show_printer_detail(
    callback: CallbackQuery, printer_id: int, tg_chat: TelegramChat | None = None,
) -> None:
    """Show printer details with control buttons."""
    lang = await get_language()
    printers = await _get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)

    if not printer:
        await callback.answer(t(lang, NS, "printers.not_found"), show_alert=True)
        return

    emoji = _state_emoji(printer["state"]) if printer["connected"] else "\u26ab"
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
            nozzle_val = f"{printer['nozzle_temp']:.0f}\u00b0C"
            lines.append(f"\U0001f321 {escape_md(t(lang, NS, 'printers.nozzle'))}: {escape_md(nozzle_val)}")
        if printer["bed_temp"] is not None:
            bed_val = f"{printer['bed_temp']:.0f}\u00b0C"
            lines.append(f"\U0001f6cf {escape_md(t(lang, NS, 'printers.bed'))}: {escape_md(bed_val)}")

    # Total hours
    total_hours = await _get_total_hours(printer_id)
    lines.append(f"\u23f0 {escape_md(t(lang, NS, 'printers.total_hours'))}: {escape_md(f'{total_hours:.1f}')}")

    if printer["state"] == "RUNNING":
        lines.append(f"\n\U0001f4c4 {escape_md(printer['current_file'] or '–')}")
        lines.append(f"\U0001f4ca {escape_md(t(lang, NS, 'printers.progress'))}: {printer['progress']}%")
        lines.append(f"\u23f1 {escape_md(t(lang, NS, 'printers.remaining'))}: {escape_md(_format_time(lang, printer['remaining_time']))}")

    # Control buttons — only shown if chat has printers:control permission
    btns = []
    can_control = _has_perm(tg_chat, "printers:control")

    if printer["connected"] and can_control:
        if printer["state"] == "RUNNING":
            btns.append([
                InlineKeyboardButton(text=f"\u23f8 {t(lang, NS, 'actions.btn_pause')}", callback_data=f"action:pause:{printer_id}"),
                InlineKeyboardButton(text=f"\u23f9 {t(lang, NS, 'actions.btn_stop')}", callback_data=f"action:stop:{printer_id}"),
            ])
        elif printer["state"] == "PAUSE":
            btns.append([
                InlineKeyboardButton(text=f"\u25b6\ufe0f {t(lang, NS, 'actions.btn_resume')}", callback_data=f"action:resume:{printer_id}"),
                InlineKeyboardButton(text=f"\u23f9 {t(lang, NS, 'actions.btn_stop')}", callback_data=f"action:stop:{printer_id}"),
            ])

        btns.append([
            InlineKeyboardButton(text=f"\U0001f4a1 {t(lang, NS, 'actions.btn_light')}", callback_data=f"action:light:{printer_id}"),
            InlineKeyboardButton(text=f"\U0001f504 {t(lang, NS, 'printers.btn_refresh')}", callback_data=f"printer:{printer_id}"),
        ])
    elif printer["connected"]:
        # Read-only: only refresh button
        btns.append([
            InlineKeyboardButton(text=f"\U0001f504 {t(lang, NS, 'printers.btn_refresh')}", callback_data=f"printer:{printer_id}"),
        ])

    # Clear plate button — FINISH/FAILED + not cleared + has next queue item
    if (
        printer["state"] in ("FINISH", "FAILED")
        and not printer["plate_cleared"]
        and _has_perm(tg_chat, "printers:clear_plate")
    ):
        next_job = await _get_next_queue_item(printer_id)
        if next_job:
            lines.append(f"\n\U0001f4e5 {escape_md(t(lang, NS, 'printers.next_in_queue', name=next_job))}")
            btns.append([InlineKeyboardButton(
                text=f"\u2705 {t(lang, NS, 'printers.btn_clear_plate')}",
                callback_data=f"action:clear_plate:{printer_id}",
            )])

    # Maintenance buttons — needs maintenance:read/update
    maint_btns = []
    if _has_perm(tg_chat, "maintenance:read"):
        maint_btns.append(
            InlineKeyboardButton(text=f"\U0001f527 {t(lang, NS, 'printers.btn_maintenance')}", callback_data=f"maint:list:{printer_id}")
        )
    if _has_perm(tg_chat, "maintenance:update"):
        maint_btns.append(
            InlineKeyboardButton(text=f"\u23f0 {t(lang, NS, 'printers.hours')}", callback_data=f"action:hours:{printer_id}")
        )
    if maint_btns:
        btns.append(maint_btns)

    # Calibration button — needs printers:control, printer must be connected and idle
    if printer["connected"] and printer["state"] in ("IDLE", "FINISH") and can_control:
        btns.append([InlineKeyboardButton(
            text=f"\U0001f527 {t(lang, NS, 'printers.btn_calibration')}",
            callback_data=f"calib:show:{printer_id}",
        )])

    btns.append([InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data="menu:printers")])

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
    await _show_printer_detail(callback, printer_id, tg_chat)


@router.callback_query(F.data.startswith("action:hours:"))
async def cb_edit_hours(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Prompt user to enter new total hours — requires maintenance:update."""
    lang = await get_language()

    if not _has_perm(tg_chat, "maintenance:update"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    total_hours = await _get_total_hours(printer_id)

    await callback.answer()
    await callback.message.answer(
        f"\u23f0 {escape_md(t(lang, NS, 'printers.total_hours'))}: *{escape_md(f'{total_hours:.1f}')}*\n\n"
        f"{escape_md(t(lang, NS, 'printers.enter_hours'))}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"\u274c {t(lang, NS, 'printers.btn_cancel')}", callback_data=f"cancel_hours:{printer_id}")],
        ]),
    )
    await state.set_state(PrinterHoursState.waiting_for_hours)
    await state.update_data(printer_id=printer_id)


@router.callback_query(F.data.startswith("cancel_hours:"))
async def cb_cancel_hours(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel hours editing."""
    lang = await get_language()
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(escape_md(t(lang, NS, "printers.hours_cancelled")))
    printer_id = int(callback.data.split(":")[1])
    await _show_printer_detail(callback, printer_id)


@router.message(PrinterHoursState.waiting_for_hours)
async def msg_set_hours(message: Message, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Receive new hours value from user."""
    lang = await get_language()
    data = await state.get_data()
    printer_id = data.get("printer_id")

    if not printer_id:
        await state.clear()
        return

    # Parse number
    text = message.text.strip().replace(",", ".") if message.text else ""
    try:
        new_hours = float(text)
        if new_hours < 0:
            raise ValueError
    except ValueError:
        await message.answer(escape_md(t(lang, NS, "printers.hours_invalid")))
        return

    # Apply — same logic as the API endpoint
    from backend.app.core.database import async_session
    from backend.app.models.printer import Printer
    from sqlalchemy import select

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
    await message.answer(
        f"\u2705 {escape_md(t(lang, NS, 'printers.hours_updated', hours=f'{new_hours:.1f}'))}"
    )


@router.callback_query(F.data.startswith("action:clear_plate:"))
async def cb_clear_plate(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Clear plate confirmation — requires printers:clear_plate."""
    lang = await get_language()

    if not _has_perm(tg_chat, "printers:clear_plate"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])

    try:
        printer_manager.set_plate_cleared(printer_id)
        await callback.answer(f"\u2705 {t(lang, NS, 'printers.clear_plate_ok')}")
    except Exception:
        await callback.answer(t(lang, NS, "printers.clear_plate_fail"), show_alert=True)
        return

    await _show_printer_detail(callback, printer_id, tg_chat)


@router.callback_query(F.data.startswith("action:"))
async def cb_printer_action(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Handle printer control actions — requires printers:control permission."""
    lang = await get_language()

    if not _has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    printer_id = int(parts[2])

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

    await _show_printer_detail(callback, printer_id, tg_chat)


# === Calibration handlers ===

CALIBRATION_TYPES = {
    "bed_leveling": {"label_key": "calibration.bed_leveling", "bit": 1},
    "vibration": {"label_key": "calibration.vibration", "bit": 2},
    "motor_noise": {"label_key": "calibration.motor_noise", "bit": 3},
    "nozzle_offset": {"label_key": "calibration.nozzle_offset", "bit": 4},
    "high_temp_heatbed": {"label_key": "calibration.high_temp_heatbed", "bit": 5},
}


def _get_available_calibrations(model: str | None) -> list[str]:
    """Return list of calibration type keys available for the printer model."""
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
    lang: str, printer_id: int, printer_name: str, model: str | None, selected: set[str],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build calibration selection screen text and keyboard."""
    available = _get_available_calibrations(model)

    lines = [
        f"\U0001f527 *{escape_md(t(lang, NS, 'calibration.title'))}* – *{escape_md(printer_name)}*",
        escape_md(t(lang, NS, "calibration.select")),
        "",
    ]

    btns = []
    for cal_type in available:
        label = t(lang, NS, CALIBRATION_TYPES[cal_type]["label_key"])
        checked = cal_type in selected
        prefix = "\u2705" if checked else "\u2b1c"
        btns.append([InlineKeyboardButton(
            text=f"{prefix} {label}",
            callback_data=f"calib:toggle:{printer_id}:{cal_type}",
        )])

    btns.append([
        InlineKeyboardButton(
            text=f"\u25b6\ufe0f {t(lang, NS, 'calibration.btn_start')}",
            callback_data=f"calib:start:{printer_id}",
        ),
        InlineKeyboardButton(
            text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}",
            callback_data=f"printer:{printer_id}",
        ),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=btns)


# In-memory calibration selection per chat (chat_id -> set of selected types)
_calib_selections: dict[int, set[str]] = {}


@router.callback_query(F.data.startswith("calib:show:"))
async def cb_calibration_show(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Show calibration selection screen."""
    lang = await get_language()
    if not _has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    # Get printer info for model
    printers = await _get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    model = printer["model"] if printer else None
    name = printer["name"] if printer else f"#{printer_id}"

    chat_id = callback.message.chat.id
    _calib_selections[chat_id] = set()

    text, keyboard = _render_calibration_screen(lang, printer_id, name, model, set())
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("calib:toggle:"))
async def cb_calibration_toggle(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Toggle a calibration option on/off."""
    lang = await get_language()
    parts = callback.data.split(":")
    printer_id = int(parts[2])
    cal_type = parts[3]

    chat_id = callback.message.chat.id
    selected = _calib_selections.get(chat_id, set())

    if cal_type in selected:
        selected.discard(cal_type)
    else:
        selected.add(cal_type)
    _calib_selections[chat_id] = selected

    await callback.answer()

    printers = await _get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    model = printer["model"] if printer else None
    name = printer["name"] if printer else f"#{printer_id}"

    text, keyboard = _render_calibration_screen(lang, printer_id, name, model, selected)
    await callback.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("calib:start:"))
async def cb_calibration_start(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Execute selected calibrations."""
    lang = await get_language()
    if not _has_perm(tg_chat, "printers:control"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    chat_id = callback.message.chat.id
    selected = _calib_selections.pop(chat_id, set())

    if not selected:
        await callback.answer(t(lang, NS, "calibration.none_selected"), show_alert=True)
        return

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

    await _show_printer_detail(callback, printer_id, tg_chat)


# === Maintenance handlers ===

@router.callback_query(F.data.startswith("maint:list:"))
async def cb_maintenance_list(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Show maintenance items for a printer."""
    lang = await get_language()

    if not _has_perm(tg_chat, "maintenance:read"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    from backend.app.core.database import async_session
    from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types

    async with async_session() as db:
        await ensure_default_types(db)
        overview = await _get_printer_maintenance_internal(printer_id, db, commit=True)

    if not overview or not overview.maintenance_items:
        await callback.message.edit_text(
            f"\U0001f527 *{escape_md(t(lang, NS, 'maintenance.title'))}*\n\n"
            f"{escape_md(t(lang, NS, 'maintenance.no_items'))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data=f"printer:{printer_id}")],
            ]),
        )
        return

    lines = [
        f"\U0001f527 *{escape_md(t(lang, NS, 'maintenance.title'))}* – *{escape_md(overview.printer_name)}*",
        f"\u23f0 {escape_md(t(lang, NS, 'printers.total_hours'))}: {escape_md(f'{overview.total_print_hours:.1f}')}",
        "",
    ]

    btns = []
    can_update = _has_perm(tg_chat, "maintenance:update")

    for item in overview.maintenance_items:
        if not item.enabled:
            continue

        # Status emoji
        if item.is_due:
            status = f"\U0001f534 {escape_md(t(lang, NS, 'maintenance.overdue'))}"
        elif item.is_warning:
            status = f"\U0001f7e1 {escape_md(t(lang, NS, 'maintenance.due_soon'))}"
        else:
            status = f"\U0001f7e2 {escape_md(t(lang, NS, 'maintenance.ok'))}"

        name = escape_md(item.maintenance_type_name)
        lines.append(f"{status} *{name}*")

        # Show progress info
        if item.interval_type == "days":
            if item.days_since_maintenance is not None:
                lines.append(f"  {escape_md(t(lang, NS, 'maintenance.days_since', days=f'{item.days_since_maintenance:.0f}'))}")
            if item.days_until_due is not None:
                lines.append(f"  {escape_md(t(lang, NS, 'maintenance.days_until', days=f'{item.days_until_due:.0f}'))}")
        else:
            lines.append(f"  {escape_md(t(lang, NS, 'maintenance.hours_since', hours=f'{item.hours_since_maintenance:.1f}'))}")
            lines.append(f"  {escape_md(t(lang, NS, 'maintenance.hours_until', hours=f'{item.hours_until_due:.1f}'))}")

        lines.append("")

        # "Done" button for due/warning items
        if can_update and (item.is_due or item.is_warning):
            btns.append([InlineKeyboardButton(
                text=f"\u2705 {item.maintenance_type_name}",
                callback_data=f"maint:done:{item.id}:{printer_id}",
            )])

    btns.append([InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data=f"printer:{printer_id}")])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("maint:done:"))
async def cb_maintenance_done(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Mark a maintenance item as done."""
    lang = await get_language()

    if not _has_perm(tg_chat, "maintenance:update"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    item_id = int(parts[2])
    printer_id = int(parts[3])

    from backend.app.core.database import async_session
    from backend.app.models.maintenance import PrinterMaintenance, MaintenanceHistory
    from backend.app.api.routes.maintenance import get_printer_total_hours
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from datetime import datetime, timezone

    async with async_session() as db:
        result = await db.execute(
            select(PrinterMaintenance)
            .options(selectinload(PrinterMaintenance.maintenance_type))
            .where(PrinterMaintenance.id == item_id)
        )
        item = result.scalar_one_or_none()

        if not item:
            await callback.answer("Item not found", show_alert=True)
            return

        current_hours = await get_printer_total_hours(db, item.printer_id)

        # Create history entry
        history = MaintenanceHistory(
            printer_maintenance_id=item.id,
            performed_at=datetime.now(timezone.utc),
            hours_at_maintenance=current_hours,
        )
        db.add(history)

        # Reset counter
        item.last_performed_at = datetime.now(timezone.utc)
        item.last_performed_hours = current_hours

        await db.commit()

    await callback.answer(f"\u2705 {t(lang, NS, 'maintenance.done_ok')}")

    # Refresh the maintenance list
    callback.data = f"maint:list:{printer_id}"
    await cb_maintenance_list(callback, tg_chat)


# === Shared render functions (used by both callbacks and reply keyboard) ===

async def render_queue(target, tg_chat: TelegramChat | None = None) -> None:
    """Render queue overview. target can be Message or CallbackQuery."""
    lang = await get_language()

    if not _has_perm(tg_chat, "queue:read"):
        if isinstance(target, CallbackQuery):
            await target.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from sqlalchemy import select, func

    async with async_session() as db:
        pending = (await db.execute(
            select(func.count(PrintQueueItem.id)).where(PrintQueueItem.status == "pending")
        )).scalar() or 0
        printing = (await db.execute(
            select(func.count(PrintQueueItem.id)).where(PrintQueueItem.status == "printing")
        )).scalar() or 0

    text = (
        f"\U0001f4cb *{escape_md(t(lang, NS, 'queue.title'))}*\n\n"
        f"\u23f3 {escape_md(t(lang, NS, 'queue.pending'))}: {pending}\n"
        f"\U0001f535 {escape_md(t(lang, NS, 'queue.printing'))}: {printing}"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_main_menu')}", callback_data="menu:main")],
    ])

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


async def render_stats(target, tg_chat: TelegramChat | None = None) -> None:
    """Render statistics. target can be Message or CallbackQuery."""
    lang = await get_language()

    if not _has_perm(tg_chat, "stats:read"):
        if isinstance(target, CallbackQuery):
            await target.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from sqlalchemy import select, func

    async with async_session() as db:
        total = (await db.execute(select(func.count(PrintArchive.id)))).scalar() or 0
        completed = (await db.execute(
            select(func.count(PrintArchive.id)).where(PrintArchive.status == "completed")
        )).scalar() or 0
        failed = (await db.execute(
            select(func.count(PrintArchive.id)).where(PrintArchive.status.in_(["failed", "aborted", "cancelled"]))
        )).scalar() or 0

    success_rate = round(completed / (completed + failed) * 100) if (completed + failed) > 0 else 0

    text = (
        f"\U0001f4ca *{escape_md(t(lang, NS, 'stats.title'))}*\n\n"
        f"{escape_md(t(lang, NS, 'stats.total'))}: {total}\n"
        f"\u2705 {escape_md(t(lang, NS, 'stats.success'))}: {completed}\n"
        f"\u274c {escape_md(t(lang, NS, 'stats.failed'))}: {failed}\n"
        f"\U0001f4c8 {escape_md(t(lang, NS, 'stats.success_rate'))}: {success_rate}%"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_main_menu')}", callback_data="menu:main")],
    ])

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


# === Menu callbacks ===

@router.callback_query(F.data == "menu:help")
async def cb_help(callback: CallbackQuery, **kwargs) -> None:
    await callback.answer()
    from backend.app.services.telegram_handlers.start import cmd_help
    await cmd_help(callback.message)


@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    await render_stats(callback, tg_chat)


@router.callback_query(F.data == "menu:main")
async def cb_main_menu(callback: CallbackQuery, **kwargs) -> None:
    await callback.answer()
    from backend.app.services.telegram_handlers.start import cmd_start
    await cmd_start(callback.message)


@router.callback_query(F.data == "menu:queue")
async def cb_queue(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    await render_queue(callback, tg_chat)
