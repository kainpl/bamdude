"""Print from Library scene — select file → select printer → confirm → start."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from backend.app.i18n import t, get_language, escape_md
from backend.app.services.telegram_handlers.common import NS, has_perm, get_printers_data
from backend.app.services.telegram_handlers.pagination import build_page_nav

if TYPE_CHECKING:
    from backend.app.models.telegram_chat import TelegramChat

router = Router()

PAGE_SIZE = 8


class LibraryPrintState(StatesGroup):
    selecting_file = State()
    selecting_printer = State()
    confirming = State()


async def _get_library_files(offset: int = 0, limit: int = PAGE_SIZE):
    """Get printable library files."""
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile
    from sqlalchemy import select, func

    async with async_session() as db:
        total = (await db.execute(
            select(func.count(LibraryFile.id)).where(LibraryFile.file_type == "3mf")
        )).scalar() or 0

        result = await db.execute(
            select(LibraryFile)
            .where(LibraryFile.file_type == "3mf")
            .order_by(LibraryFile.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        files = list(result.scalars().all())

    return files, total


@router.callback_query(F.data == "lib:start")
async def cb_library_start(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Entry point for Print from Library."""
    lang = await get_language()

    if not (has_perm(tg_chat, "library:read") and has_perm(tg_chat, "printers:control")):
        await callback.answer(t(lang, NS, "auth.no_permission"), show_alert=True)
        return

    await callback.answer()
    await state.set_state(LibraryPrintState.selecting_file)
    await state.update_data(offset=0)
    await _show_file_list(callback, lang, 0)


async def _show_file_list(callback: CallbackQuery, lang: str, offset: int) -> None:
    files, total = await _get_library_files(offset)

    if not files and offset == 0:
        await callback.message.edit_text(
            f"\U0001f4c2 *{escape_md(t(lang, NS, 'library.title'))}*\n\n"
            f"{escape_md(t(lang, NS, 'library.no_files'))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_main_menu')}", callback_data="menu:main")],
            ]),
        )
        return

    lines = [
        f"\U0001f4c2 *{escape_md(t(lang, NS, 'library.title'))}*",
        escape_md(t(lang, NS, "library.select_file")),
    ]

    btns = []
    for f in files:
        btns.append([InlineKeyboardButton(
            text=f"\U0001f4c4 {f.filename}",
            callback_data=f"lib:file:{f.id}",
        )])

    nav = build_page_nav(total, offset, PAGE_SIZE, "page:lib:", lang)
    if nav:
        btns.append(nav)

    btns.append([InlineKeyboardButton(
        text=f"\u274c {t(lang, NS, 'library.btn_cancel')}",
        callback_data="lib:cancel",
    )])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("page:lib:"))
async def cb_library_page(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    lang = await get_language()
    offset = int(callback.data.split(":")[2])
    await callback.answer()
    await state.update_data(offset=offset)
    await _show_file_list(callback, lang, offset)


@router.callback_query(F.data.startswith("lib:file:"))
async def cb_library_select_file(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """File selected — show printer list."""
    lang = await get_language()
    file_id = int(callback.data.split(":")[2])
    await callback.answer()

    # Get file info
    from backend.app.core.database import async_session
    from backend.app.models.library import LibraryFile
    from sqlalchemy import select

    async with async_session() as db:
        result = await db.execute(select(LibraryFile).where(LibraryFile.id == file_id))
        lib_file = result.scalar_one_or_none()

    if not lib_file:
        await callback.message.edit_text(escape_md("File not found"))
        await state.clear()
        return

    # Get sliced_for_model from metadata for printer filtering
    sliced_for_model = None
    if lib_file.file_metadata and isinstance(lib_file.file_metadata, dict):
        sliced_for_model = lib_file.file_metadata.get("sliced_for_model")

    await state.set_state(LibraryPrintState.selecting_printer)
    await state.update_data(file_id=file_id, file_name=lib_file.filename, sliced_for_model=sliced_for_model)

    # Show idle printers, filtered by model if available
    printers = await get_printers_data()
    idle_printers = [p for p in printers if p["connected"] and p["state"] in ("IDLE", "FINISH")]
    if sliced_for_model:
        compatible = [p for p in idle_printers if p["model"] and p["model"].upper() == sliced_for_model.upper()]
        # If compatible found, show only those; otherwise show all (user's choice)
        if compatible:
            idle_printers = compatible

    if not idle_printers:
        await callback.message.edit_text(
            f"\U0001f5a8 {escape_md(t(lang, NS, 'library.no_printers'))}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}", callback_data="lib:start")],
            ]),
        )
        return

    lines = [
        f"\U0001f4c4 *{escape_md(lib_file.filename)}*\n",
        escape_md(t(lang, NS, "library.select_printer")),
    ]

    btns = []
    for p in idle_printers:
        btns.append([InlineKeyboardButton(
            text=f"\U0001f5a8 {p['name']}",
            callback_data=f"lib:printer:{p['id']}",
        )])

    btns.append([InlineKeyboardButton(
        text=f"\u25c0\ufe0f {t(lang, NS, 'printers.btn_back')}",
        callback_data="lib:start",
    )])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
    )


@router.callback_query(F.data.startswith("lib:printer:"))
async def cb_library_select_printer(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Printer selected — show confirmation."""
    lang = await get_language()
    printer_id = int(callback.data.split(":")[2])
    await callback.answer()

    data = await state.get_data()
    file_name = data.get("file_name", "?")
    file_id = data.get("file_id")

    printers = await get_printers_data()
    printer = next((p for p in printers if p["id"] == printer_id), None)
    printer_name = printer["name"] if printer else f"#{printer_id}"

    await state.set_state(LibraryPrintState.confirming)
    await state.update_data(printer_id=printer_id, printer_name=printer_name)

    text = (
        f"\u2705 *{escape_md(t(lang, NS, 'library.confirm_title'))}*\n\n"
        f"\U0001f4c4 {escape_md(t(lang, NS, 'library.confirm_file'))}: *{escape_md(file_name)}*\n"
        f"\U0001f5a8 {escape_md(t(lang, NS, 'library.confirm_printer'))}: *{escape_md(printer_name)}*"
    )

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"\u25b6\ufe0f {t(lang, NS, 'library.btn_print_now')}", callback_data="lib:print_now")],
        [InlineKeyboardButton(text=f"\U0001f4cb {t(lang, NS, 'library.btn_add_queue')}", callback_data="lib:add_queue")],
        [InlineKeyboardButton(text=f"\u274c {t(lang, NS, 'library.btn_cancel')}", callback_data="lib:cancel")],
    ]))


@router.callback_query(F.data == "lib:print_now")
async def cb_library_print_now(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Print now — dispatch via background_dispatch (FTP upload + start)."""
    lang = await get_language()

    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name")
    printer_id = data.get("printer_id")
    printer_name = data.get("printer_name")

    await state.clear()

    if not file_id or not printer_id:
        await callback.answer(t(lang, NS, "library.failed"), show_alert=True)
        return

    try:
        from backend.app.services.background_dispatch import background_dispatch

        await background_dispatch.dispatch_print_library_file(
            file_id=file_id,
            filename=file_name,
            printer_id=printer_id,
            printer_name=printer_name or f"#{printer_id}",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )

        await callback.answer(f"\u2705 {t(lang, NS, 'library.print_dispatched')}")
    except Exception:
        await callback.answer(t(lang, NS, "library.failed"), show_alert=True)

    from backend.app.services.telegram_handlers.start import cmd_start
    await cmd_start(callback.message)


@router.callback_query(F.data == "lib:add_queue")
async def cb_library_add_queue(callback: CallbackQuery, state: FSMContext, tg_chat: TelegramChat | None = None) -> None:
    """Add to queue instead of printing now."""
    lang = await get_language()

    data = await state.get_data()
    file_id = data.get("file_id")
    file_name = data.get("file_name")
    printer_id = data.get("printer_id")

    await state.clear()

    if not file_id or not printer_id:
        await callback.answer(t(lang, NS, "library.failed"), show_alert=True)
        return

    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from sqlalchemy import select, func

    try:
        async with async_session() as db:
            max_pos = (await db.execute(select(func.max(PrintQueueItem.position)))).scalar() or 0
            item = PrintQueueItem(
                printer_id=printer_id,
                library_file_id=file_id,
                file_name=file_name,
                status="pending",
                position=max_pos + 1,
            )
            db.add(item)
            await db.commit()

        await callback.answer(f"\u2705 {t(lang, NS, 'library.queued')}")
    except Exception:
        await callback.answer(t(lang, NS, "library.failed"), show_alert=True)

    # Return to main menu
    from backend.app.services.telegram_handlers.start import cmd_start
    await cmd_start(callback.message)


@router.callback_query(F.data == "lib:cancel")
async def cb_library_cancel(callback: CallbackQuery, state: FSMContext, **kwargs) -> None:
    """Cancel library scene."""
    await state.clear()
    await callback.answer()
    from backend.app.services.telegram_handlers.start import cmd_start
    await cmd_start(callback.message)
