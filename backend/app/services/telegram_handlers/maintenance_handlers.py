"""Maintenance list and mark-done handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import NS, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()


@router.callback_query(F.data.startswith("maint:list:"))
async def cb_maintenance_list(
    callback: CallbackQuery, tg_chat: TelegramChat | None = None, printer_id: int | None = None
) -> None:
    """Show maintenance items for a printer.

    ``printer_id`` is parsed from ``callback.data`` when invoked directly as
    a callback handler; other handlers (e.g. ``cb_maintenance_done``) pass it
    explicitly since ``CallbackQuery`` is a frozen model and ``data`` can't
    be rewritten to re-route through this handler.
    """
    lang = await get_language()

    if not has_perm(tg_chat, "maintenance:read"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    if printer_id is None:
        printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    from backend.app.api.routes.maintenance import _get_printer_maintenance_internal, ensure_default_types
    from backend.app.core.database import async_session

    async with async_session() as db:
        await ensure_default_types(db)
        overview = await _get_printer_maintenance_internal(printer_id, db, commit=True)

    if not overview or not overview.maintenance_items:
        await callback.message.edit_text(
            f"\U0001f527 *{escape_md(t(lang, NS, 'maintenance.title'))}*\n\n"
            f"{escape_md(t(lang, NS, 'maintenance.no_items'))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}",
                            callback_data=f"printer:{printer_id}",
                        )
                    ],
                ]
            ),
        )
        return

    lines = [
        f"\U0001f527 *{escape_md(t(lang, NS, 'maintenance.title'))}* – *{escape_md(overview.printer_name)}*",
        f"\u23f0 {escape_md(t(lang, NS, 'printers.total_hours'))}: {escape_md(f'{overview.total_print_hours:.1f}')}",
        "",
    ]

    btns = []
    can_update = has_perm(tg_chat, "maintenance:update")

    for item in overview.maintenance_items:
        if not item.enabled:
            continue

        if item.is_due:
            status = f"\U0001f534 {escape_md(t(lang, NS, 'maintenance.overdue'))}"
        elif item.is_warning:
            status = f"\U0001f7e1 {escape_md(t(lang, NS, 'maintenance.due_soon'))}"
        else:
            status = f"\U0001f7e2 {escape_md(t(lang, NS, 'maintenance.ok'))}"

        name = escape_md(item.maintenance_type_name)
        lines.append(f"{status} *{name}*")

        if item.interval_type == "days":
            if item.days_since_maintenance is not None:
                lines.append(
                    f"  {escape_md(t(lang, NS, 'maintenance.days_since', days=f'{item.days_since_maintenance:.0f}'))}"
                )
            if item.days_until_due is not None:
                lines.append(f"  {escape_md(t(lang, NS, 'maintenance.days_until', days=f'{item.days_until_due:.0f}'))}")
        else:
            lines.append(
                f"  {escape_md(t(lang, NS, 'maintenance.hours_since', hours=f'{item.hours_since_maintenance:.1f}'))}"
            )
            lines.append(f"  {escape_md(t(lang, NS, 'maintenance.hours_until', hours=f'{item.hours_until_due:.1f}'))}")

        lines.append("")

        if can_update and (item.is_due or item.is_warning):
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\u2705 {item.maintenance_type_name}",
                        callback_data=f"maint:done:{item.id}:{printer_id}",
                    )
                ]
            )

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data=f"printer:{printer_id}"
            )
        ]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("maint:done:"))
async def cb_maintenance_done(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Mark a maintenance item as done."""
    lang = await get_language()

    if not has_perm(tg_chat, "maintenance:update"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    item_id = int(parts[2])
    printer_id = int(parts[3])
    # A trailing ":n" means the press came from a maintenance-due
    # notification (not the in-bot list) — see notification_service.
    from_notification = len(parts) > 4 and parts[4] == "n"

    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.api.routes.maintenance import get_printer_total_hours
    from backend.app.core.database import async_session
    from backend.app.models import telegram_chat as tc_mod
    from backend.app.models.maintenance import MaintenanceHistory, PrinterMaintenance

    async with async_session() as db:
        # Find TelegramChat for this callback
        tg_chat_id = callback.message.chat.id if callback.message else None
        db_chat = None
        if tg_chat_id:
            chat_result = await db.execute(select(tc_mod.TelegramChat).where(tc_mod.TelegramChat.chat_id == tg_chat_id))
            db_chat = chat_result.scalar_one_or_none()
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

        history = MaintenanceHistory(
            printer_maintenance_id=item.id,
            performed_at=datetime.now(timezone.utc),
            hours_at_maintenance=current_hours,
            performed_by_chat_id=db_chat.id if db_chat else None,
            performed_by_user_id=db_chat.user_id if db_chat and db_chat.user_id else None,
        )
        db.add(history)

        item.last_performed_at = datetime.now(timezone.utc)
        item.last_performed_hours = current_hours

        await db.commit()

    await callback.answer(f"\u2705 {t(lang, NS, 'maintenance.done_ok')}")

    if from_notification:
        # The DB write succeeded \u2014 clear the just-handled button from the
        # notification so it can't be pressed again. When it was the last
        # outstanding button, drop the whole notification message.
        await _strip_notification_button(callback)
        return

    await cb_maintenance_list(callback, tg_chat, printer_id=printer_id)


async def _strip_notification_button(callback: CallbackQuery) -> None:
    """Remove the pressed button from a maintenance-due notification.

    Filters out every button whose ``callback_data`` matches the press. If
    no buttons remain (the usual case \u2014 a maintenance-due notification
    carries only "done" buttons) the message is deleted outright. Edit /
    delete failures are swallowed: the DB row is already saved and the
    operator saw the confirmation toast, so this is purely cosmetic, and
    Telegram refuses to edit / delete messages older than 48 h.
    """
    msg = callback.message
    if msg is None:
        return

    remaining: list[list[InlineKeyboardButton]] = []
    markup = msg.reply_markup
    if markup and markup.inline_keyboard:
        for row in markup.inline_keyboard:
            kept = [b for b in row if b.callback_data != callback.data]
            if kept:
                remaining.append(kept)

    try:
        if remaining:
            await msg.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=remaining))
        else:
            await msg.delete()
    except Exception:
        pass
