"""Queue handlers: list, detail, move, cancel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from backend.app.i18n import escape_md, get_language, t
from backend.app.services.telegram_handlers.common import NS, has_perm
from backend.app.services.telegram_handlers.pagination import build_page_nav

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

PAGE_SIZE = 5

QUEUE_STATUS_EMOJIS = {
    "pending": "\u23f3",
    "printing": "\U0001f535",
    "completed": "\u2705",
    "failed": "\U0001f534",
    "cancelled": "\u26d4",
    "skipped": "\u23e9",
}


async def render_queue(target, tg_chat: TelegramChat | None = None, offset: int = 0) -> None:
    """Render queue with paginated job list."""
    lang = await get_language()

    if not has_perm(tg_chat, "queue:read"):
        if isinstance(target, CallbackQuery):
            await target.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    from sqlalchemy import func, select

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer

    async with async_session() as db:
        # Total counts
        total_pending = (
            await db.execute(select(func.count(PrintQueueItem.id)).where(PrintQueueItem.status == "pending"))
        ).scalar() or 0
        total_printing = (
            await db.execute(select(func.count(PrintQueueItem.id)).where(PrintQueueItem.status == "printing"))
        ).scalar() or 0

        # Active items (pending + printing) for list
        total_active = (
            await db.execute(
                select(func.count(PrintQueueItem.id)).where(PrintQueueItem.status.in_(["pending", "printing"]))
            )
        ).scalar() or 0

        # Paginated items
        result = await db.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.status.in_(["pending", "printing"]))
            .order_by(PrintQueueItem.position)
            .offset(offset)
            .limit(PAGE_SIZE)
        )
        items = list(result.scalars().all())

        # Get printer names
        printer_ids = {i.queue_id for i in items if i.queue_id}
        printer_names = {}
        if printer_ids:
            printers = await db.execute(select(Printer).where(Printer.id.in_(printer_ids)))
            for p in printers.scalars():
                printer_names[p.id] = p.name

    lines = [
        f"\U0001f4cb *{escape_md(t(lang, NS, 'queue.title'))}*\n",
        f"\u23f3 {escape_md(t(lang, NS, 'queue.pending'))}: {total_pending}",
        f"\U0001f535 {escape_md(t(lang, NS, 'queue.printing'))}: {total_printing}",
    ]

    btns = []

    if items:
        lines.append("")
        for item in items:
            emoji = QUEUE_STATUS_EMOJIS.get(item.status, "\u2753")
            fname = escape_md(item.file_name or f"Job \\#{item.id}")
            printer_label = ""
            if item.queue_id and item.queue_id in printer_names:
                printer_label = f" → {escape_md(printer_names[item.queue_id])}"

            lines.append(f"{emoji} *{fname}*{printer_label}")

            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"{emoji} {item.file_name or f'Job #{item.id}'}",
                        callback_data=f"queue:detail:{item.id}",
                    )
                ]
            )

        # Pagination nav
        nav = build_page_nav(total_active, offset, PAGE_SIZE, "page:queue:", lang)
        if nav:
            btns.append(nav)
    else:
        lines.append(f"\n{escape_md(t(lang, NS, 'queue.empty'))}")

    # Add to queue button
    if has_perm(tg_chat, "queue:create"):
        btns.append(
            [
                InlineKeyboardButton(
                    text=f"\u2795 {t(lang, NS, 'queue_add.title')}",
                    callback_data="qadd:start",
                )
            ]
        )

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_main_menu')}",
                callback_data="menu:main",
            )
        ]
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=btns)
    text = "\n".join(lines)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "menu:queue")
async def cb_queue(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    await render_queue(callback, tg_chat)


@router.callback_query(F.data.startswith("page:queue:"))
async def cb_queue_page(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    offset = int(callback.data.split(":")[2])
    await render_queue(callback, tg_chat, offset)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    """No-op for page counter button."""
    await callback.answer()


@router.callback_query(F.data.startswith("queue:detail:"))
async def cb_queue_detail(
    callback: CallbackQuery, tg_chat: TelegramChat | None = None, item_id: int | None = None
) -> None:
    """Show queue item detail.

    ``item_id`` is parsed from ``callback.data`` when invoked directly as a
    callback handler; ``cb_queue_move`` passes it explicitly since
    ``CallbackQuery`` is a frozen model and ``data`` can't be rewritten.
    """
    lang = await get_language()
    await callback.answer()

    if item_id is None:
        item_id = int(callback.data.split(":")[2])

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer

    async with async_session() as db:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
        item = result.scalar_one_or_none()

        if not item:
            await callback.message.edit_text(escape_md("Item not found"))
            return

        printer_name = None
        if item.queue_id:
            p = await db.get(Printer, item.queue_id)
            printer_name = p.name if p else None

    emoji = QUEUE_STATUS_EMOJIS.get(item.status, "\u2753")
    status_label = t(lang, NS, f"queue.status_{item.status}", default=item.status)

    lines = [
        f"\U0001f4cb *{escape_md(t(lang, NS, 'queue.item_detail'))}*\n",
        f"\U0001f4c4 {escape_md(t(lang, NS, 'queue.file'))}: *{escape_md(item.file_name or '–')}*",
    ]

    if printer_name:
        lines.append(f"\U0001f5a8 {escape_md(t(lang, NS, 'queue.printer'))}: {escape_md(printer_name)}")

    lines.append(f"{emoji} {escape_md(t(lang, NS, 'queue.status'))}: {escape_md(status_label)}")
    lines.append(f"\U0001f522 {escape_md(t(lang, NS, 'queue.position'))}: {item.position}")

    if item.scheduled_time:
        lines.append(
            f"\U0001f4c5 {escape_md(t(lang, NS, 'queue.scheduled'))}: {escape_md(str(item.scheduled_time)[:16])}"
        )

    btns = []

    # Actions for pending items
    if item.status == "pending":
        action_row = []
        if has_perm(tg_chat, "queue:reorder"):
            action_row.append(
                InlineKeyboardButton(
                    text=f"\u2b06 {t(lang, NS, 'queue.btn_move_up')}",
                    callback_data=f"queue:move:{item.id}:up",
                )
            )
            action_row.append(
                InlineKeyboardButton(
                    text=f"\u2b07 {t(lang, NS, 'queue.btn_move_down')}",
                    callback_data=f"queue:move:{item.id}:down",
                )
            )
        if action_row:
            btns.append(action_row)

        if has_perm(tg_chat, "queue:delete_own") or has_perm(tg_chat, "queue:delete_all"):
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\u274c {t(lang, NS, 'queue.btn_cancel')}",
                        callback_data=f"queue:cancel:{item.id}",
                    )
                ]
            )

    btns.append(
        [
            InlineKeyboardButton(
                text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}",
                callback_data="menu:queue",
            )
        ]
    )

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("queue:move:"))
async def cb_queue_move(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Move queue item up or down."""
    lang = await get_language()

    if not has_perm(tg_chat, "queue:reorder"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    parts = callback.data.split(":")
    item_id = int(parts[2])
    direction = parts[3]  # "up" or "down"

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem

    async with async_session() as db:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
        item = result.scalar_one_or_none()
        if not item:
            await callback.answer("Not found", show_alert=True)
            return

        # Find adjacent item
        if direction == "up":
            adj = await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.status == "pending", PrintQueueItem.position < item.position)
                .order_by(PrintQueueItem.position.desc())
                .limit(1)
            )
        else:
            adj = await db.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.status == "pending", PrintQueueItem.position > item.position)
                .order_by(PrintQueueItem.position)
                .limit(1)
            )

        adjacent = adj.scalar_one_or_none()
        if adjacent:
            item.position, adjacent.position = adjacent.position, item.position
            await db.commit()
            await callback.answer(f"\u2705 {t(lang, NS, 'queue.move_ok')}")
        else:
            await callback.answer()

    # Refresh detail
    await cb_queue_detail(callback, tg_chat, item_id=item_id)


@router.callback_query(F.data.startswith("queue:cancel:"))
async def cb_queue_cancel(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    """Cancel a queue item."""
    lang = await get_language()

    if not (has_perm(tg_chat, "queue:delete_own") or has_perm(tg_chat, "queue:delete_all")):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    item_id = int(callback.data.split(":")[2])

    from sqlalchemy import select

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem

    async with async_session() as db:
        result = await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
        item = result.scalar_one_or_none()
        if item and item.status == "pending":
            item.status = "cancelled"
            await db.commit()
            await callback.answer(f"\u2705 {t(lang, NS, 'queue.cancel_ok')}")
        else:
            await callback.answer()

    await render_queue(callback, tg_chat)
