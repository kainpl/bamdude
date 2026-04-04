"""Statistics handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from backend.app.i18n import t, get_language, escape_md
from backend.app.services.telegram_handlers.common import NS, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()


async def render_stats(target, tg_chat: TelegramChat | None = None) -> None:
    """Render statistics. target can be Message or CallbackQuery."""
    lang = await get_language()

    if not has_perm(tg_chat, "stats:read"):
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


@router.callback_query(F.data == "menu:stats")
async def cb_stats(callback: CallbackQuery, tg_chat: TelegramChat | None = None) -> None:
    await callback.answer()
    await render_stats(callback, tg_chat)
