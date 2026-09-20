"""Defects from Telegram — one tap per part, where the operator already is.

Reached three ways: the «Брак…» button on the completion message, and both
plate answers (Clear plate, Repeat) once they have succeeded. Every tap writes
ONE row absolutely through ``services/archive_defects`` (so answering parts one
at a time is exact and re-answering is idempotent), then the next part is
asked; «no defects, done» zeroes this and every remaining part; «other…» takes
a typed number through an FSM state, like the printer-hours edit.

Permission is the plate answer's own (``printers:clear_plate``) and the scope
is the archive's PRINTER — a chat that may not control that machine may not
grade its prints either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from backend.app.i18n import escape_md, get_language, t
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.services.archive_defects import DefectsWrite, record_defects
from backend.app.services.archive_parts import load_rows
from backend.app.services.telegram_handlers.common import NS, chat_allows_printer, has_perm

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

#: Buttons 0..MAX_BUTTONS inline; anything larger goes through «other…».
MAX_BUTTONS = 5


class DefectsState(StatesGroup):
    waiting_for_count = State()


async def _load(db, archive_id: int) -> tuple[PrintArchive | None, list[PrintArchivePart]]:
    archive = await db.get(PrintArchive, archive_id)
    if archive is None or archive.deleted_at is not None:
        return None, []
    return archive, await load_rows(db, archive_id)


def _keyboard(archive_id: int, row_id: int, quantity: int, lang: str) -> InlineKeyboardMarkup:
    numbers = [
        InlineKeyboardButton(text=str(n), callback_data=f"defects:{archive_id}:{row_id}:{n}")
        for n in range(0, min(quantity, MAX_BUTTONS) + 1)
    ]
    extra = []
    if quantity > MAX_BUTTONS:
        extra.append(
            InlineKeyboardButton(
                text=t(lang, NS, "defects.btn_other"), callback_data=f"defects_other:{archive_id}:{row_id}"
            )
        )
    extra.append(
        InlineKeyboardButton(
            text=f"✅ {t(lang, NS, 'defects.btn_none_rest')}",
            callback_data=f"defects_none:{archive_id}:{row_id}",
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[numbers, extra])


def _prompt_text(lang: str, archive: PrintArchive, row: PrintArchivePart | None) -> str:
    if row is None:
        return escape_md(
            t(
                lang,
                NS,
                "defects.prompt_flat",
                name=archive.print_name or archive.filename,
                quantity=archive.quantity or 0,
            )
        )
    return escape_md(t(lang, NS, "defects.prompt_part", name=row.name, quantity=row.quantity))


async def _ask(message: Message, lang: str, archive: PrintArchive, row: PrintArchivePart | None) -> None:
    row_id = row.id if row is not None else 0
    quantity = row.quantity if row is not None else int(archive.quantity or 0)
    await message.answer(_prompt_text(lang, archive, row), reply_markup=_keyboard(archive.id, row_id, quantity, lang))


async def start_defects_prompt(message: Message, archive_id: int, tg_chat: TelegramChat | None = None) -> None:
    """Ask about the first part (or the flat count). Silent when the print has nothing to grade."""
    from backend.app.core.database import async_session

    lang = await get_language()
    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
    if archive is None or archive.status != "completed" or not archive.quantity:
        return
    if not has_perm(tg_chat, "printers:clear_plate") or not chat_allows_printer(tg_chat, archive.printer_id):
        return
    await _ask(message, lang, archive, rows[0] if rows else None)


async def _allowed(
    callback: CallbackQuery, tg_chat: TelegramChat | None, archive: PrintArchive | None, lang: str
) -> bool:
    if archive is None:
        await callback.answer(t(lang, NS, "defects.gone"), show_alert=True)
        return False
    if not has_perm(tg_chat, "printers:clear_plate"):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return False
    if not chat_allows_printer(tg_chat, archive.printer_id):
        await callback.answer(t(lang, NS, "auth.not_in_scope"), show_alert=True)
        return False
    return True


def _actor_id(tg_chat: TelegramChat | None) -> int | None:
    """Who the ledger credits — the system user behind the chat, if it has one."""
    return tg_chat.user_id if tg_chat is not None else None


def _refused_line(lang: str, refused: list[int]) -> str:
    """The «the shelf could not follow» tail of the done message, or ``""``.

    Read off the LAST write, not accumulated across the taps — and that is
    complete, not a shortcut: ``part_stock.adjust_unfiled_print`` recomputes
    ``wanted`` for the whole archive against what the shelf is standing on every
    single time, so a part whose correction it refused is refused again on every
    later tap (nothing was written for it, so nothing about it changed), while a
    part that was corrected has a zero delta and drops out. The last result is
    therefore the standing list of parts the operator must fix by hand.
    """
    if not refused:
        return ""
    return "\n" + escape_md(t(lang, NS, "defects.ledger_refused", count=len(refused)))


async def _write_and_continue(
    callback: CallbackQuery, lang: str, archive_id: int, row_id: int, value: int, actor_id: int | None
) -> None:
    """Write one answer, edit the asked message to say so, and ask the next part."""
    from backend.app.core.database import async_session

    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
        if archive is None:
            await callback.answer(t(lang, NS, "defects.gone"), show_alert=True)
            return
        write = DefectsWrite(parts=((row_id, value),)) if row_id else DefectsWrite(flat=value)
        result = await record_defects(db, archive, write, actor_id=actor_id)
        await db.commit()
        by_id = {r.id: r for r in result.parts}
        answered = by_id.get(row_id)
        remaining = [r for r in result.parts if r.id > row_id] if row_id else []
        total_quantity = int(archive.quantity or 0)

    # ⚠️ ONE edit of the asked message, whichever way this goes. Editing it with
    # the per-part confirmation and then immediately again with the total was two
    # Bot API round-trips and a visible flicker, and the last part's own
    # confirmation never survived the second edit.
    if remaining:
        if answered is not None:
            await callback.message.edit_text(
                escape_md(
                    t(
                        lang,
                        NS,
                        "defects.recorded_part",
                        name=answered.name,
                        defective=answered.defective,
                        quantity=answered.quantity,
                    )
                )
            )
        await _ask(callback.message, lang, archive, remaining[0])
    else:
        await callback.message.edit_text(
            escape_md(t(lang, NS, "defects.done", defective=result.defective_count, quantity=total_quantity))
            + _refused_line(lang, result.ledger_refused)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("action:defects:"))
async def cb_defects_start(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    from backend.app.core.database import async_session

    lang = await get_language()
    archive_id = int(callback.data.split(":")[2])
    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
    if not await _allowed(callback, tg_chat, archive, lang):
        return
    # A fresh prompt ends any «other…» left waiting, or the number typed for
    # this one would be filed against the row that prompt was about.
    await state.clear()
    await callback.answer()
    await _ask(callback.message, lang, archive, rows[0] if rows else None)


@router.callback_query(F.data.startswith("defects:"))
async def cb_defects_set(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    from backend.app.core.database import async_session

    lang = await get_language()
    _, archive_id, row_id, value = callback.data.split(":")
    async with async_session() as db:
        archive, _rows = await _load(db, int(archive_id))
    if not await _allowed(callback, tg_chat, archive, lang):
        return
    # The tap IS the answer: a pending «other…» state must not survive it and
    # read the operator's next ordinary message as a count.
    await state.clear()
    await _write_and_continue(callback, lang, int(archive_id), int(row_id), int(value), _actor_id(tg_chat))


@router.callback_query(F.data.startswith("defects_none:"))
async def cb_defects_none(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """This part and every part after it: no defects. Ends the prompt."""
    from backend.app.core.database import async_session

    lang = await get_language()
    _, archive_id, row_id = callback.data.split(":")
    archive_id, row_id = int(archive_id), int(row_id)
    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
        if not await _allowed(callback, tg_chat, archive, lang):
            return
        await state.clear()
        rest = tuple((r.id, 0) for r in rows if r.id >= row_id) if row_id else ()
        write = DefectsWrite(parts=rest) if rest else DefectsWrite(flat=0)
        result = await record_defects(db, archive, write, actor_id=_actor_id(tg_chat))
        await db.commit()
        total_quantity = int(archive.quantity or 0)
    await callback.message.edit_text(
        escape_md(t(lang, NS, "defects.done", defective=result.defective_count, quantity=total_quantity))
        + _refused_line(lang, result.ledger_refused)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("defects_other:"))
async def cb_defects_other(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    from backend.app.core.database import async_session

    lang = await get_language()
    _, archive_id, row_id = callback.data.split(":")
    archive_id, row_id = int(archive_id), int(row_id)
    async with async_session() as db:
        archive, rows = await _load(db, archive_id)
    if not await _allowed(callback, tg_chat, archive, lang):
        return
    row = next((r for r in rows if r.id == row_id), None)
    maximum = row.quantity if row is not None else int(archive.quantity or 0)
    await callback.answer()
    await callback.message.answer(
        escape_md(t(lang, NS, "defects.enter_count", max=maximum)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"❌ {t(lang, NS, 'defects.btn_cancel')}",
                        callback_data=f"defects_cancel:{archive_id}",
                    )
                ]
            ]
        ),
    )
    await state.set_state(DefectsState.waiting_for_count)
    await state.update_data(archive_id=archive_id, row_id=row_id, maximum=maximum)


@router.callback_query(F.data.startswith("defects_cancel:"))
async def cb_defects_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await get_language()
    await state.clear()
    await callback.answer(t(lang, NS, "defects.cancelled"))


@router.message(DefectsState.waiting_for_count)
async def msg_defects_count(message: Message, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    from backend.app.core.database import async_session

    lang = await get_language()
    data = await state.get_data()
    archive_id, row_id = data.get("archive_id"), data.get("row_id", 0)
    if not archive_id:
        await state.clear()
        return
    text = (message.text or "").strip()
    # ``isdecimal`` and not ``isdigit``: "²" is a digit to Python and a
    # ValueError to ``int()``.
    if not text.isdecimal():
        await message.answer(escape_md(t(lang, NS, "defects.invalid", max=data.get("maximum", 0))))
        return
    value = int(text)
    async with async_session() as db:
        archive, rows = await _load(db, int(archive_id))
        if archive is None:
            await state.clear()
            await message.answer(escape_md(t(lang, NS, "defects.gone")))
            return
        if not has_perm(tg_chat, "printers:clear_plate"):
            await state.clear()
            await message.answer(escape_md(t(lang, NS, "auth.no_permission")))
            return
        if not chat_allows_printer(tg_chat, archive.printer_id):
            await state.clear()
            await message.answer(escape_md(t(lang, NS, "auth.not_in_scope")))
            return
        row = next((r for r in rows if r.id == int(row_id)), None) if row_id else None
        maximum = row.quantity if row is not None else int(archive.quantity or 0)
        if value > maximum:
            # The question promised "0 to {max}", so a bigger number is a typo,
            # not an instruction to clamp. The state stays: the next message is
            # still read as the answer for this part. (The writer clamps too —
            # that is the last line of defence, not this answer's behaviour.)
            await message.answer(escape_md(t(lang, NS, "defects.invalid", max=maximum)))
            return
        write = DefectsWrite(parts=((int(row_id), value),)) if row_id else DefectsWrite(flat=value)
        result = await record_defects(db, archive, write, actor_id=_actor_id(tg_chat))
        await db.commit()
        remaining = [r for r in result.parts if r.id > int(row_id)] if row_id else []
        total_quantity = int(archive.quantity or 0)
    await state.clear()
    if remaining:
        await _ask(message, lang, archive, remaining[0])
    else:
        await message.answer(
            escape_md(t(lang, NS, "defects.done", defective=result.defective_count, quantity=total_quantity))
            + _refused_line(lang, result.ledger_refused)
        )
