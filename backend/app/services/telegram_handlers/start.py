"""Start and help command handlers + reply keyboard."""

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup

from backend.app.i18n import escape_md, get_language, t

router = Router()

NS = "telegram_ui"


def _reply_keyboard(lang: str) -> ReplyKeyboardMarkup:
    """Build the persistent reply keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t(lang, NS, "reply_kb.printers")),
                KeyboardButton(text=t(lang, NS, "reply_kb.queue")),
            ],
            [
                KeyboardButton(text=t(lang, NS, "reply_kb.library")),
                KeyboardButton(text=t(lang, NS, "reply_kb.stats")),
            ],
            [
                KeyboardButton(text=t(lang, NS, "reply_kb.help")),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Handle /start command — welcome message with main menu."""
    lang = await get_language()

    inline_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"\U0001f5a8 {t(lang, NS, 'start.btn_printers')}", callback_data="menu:printers"
                ),
                InlineKeyboardButton(text=f"\U0001f4cb {t(lang, NS, 'start.btn_queue')}", callback_data="menu:queue"),
            ],
            [
                InlineKeyboardButton(text=f"\U0001f4c2 {t(lang, NS, 'start.btn_library')}", callback_data="lib:start"),
                InlineKeyboardButton(text=f"\U0001f4ca {t(lang, NS, 'start.btn_stats')}", callback_data="menu:stats"),
            ],
            [
                InlineKeyboardButton(text=f"\u2139\ufe0f {t(lang, NS, 'start.btn_help')}", callback_data="menu:help"),
            ],
        ]
    )

    # Send reply keyboard first (Telegram persists it for the chat)
    await message.answer(
        "\U0001f44b *Bambuddy HE*",
        reply_markup=_reply_keyboard(lang),
    )

    # Then send inline menu
    await message.answer(
        escape_md(t(lang, NS, "start.welcome")),
        reply_markup=inline_keyboard,
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Handle /help command."""
    lang = await get_language()

    await message.answer(
        f"*\U0001f4d6 {escape_md(t(lang, NS, 'help.title'))}*\n\n"
        f"{escape_md(t(lang, NS, 'help.cmd_start'))}\n"
        f"{escape_md(t(lang, NS, 'help.cmd_status'))}\n"
        f"{escape_md(t(lang, NS, 'help.cmd_help'))}\n\n"
        f"*\U0001f5a8 {escape_md(t(lang, NS, 'help.printers_title'))}*\n"
        f"{escape_md(t(lang, NS, 'help.printers_desc'))}\n\n"
        f"*\U0001f4cb {escape_md(t(lang, NS, 'help.queue_title'))}*\n"
        f"{escape_md(t(lang, NS, 'help.queue_desc'))}",
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Handle /status — quick printer status overview."""
    from backend.app.services.telegram_handlers.printers import show_printer_list

    await show_printer_list(message)


# === Reply keyboard text handlers ===
# Match button text from ANY language — check all supported locales


def _matches_reply_button(text: str, key: str) -> bool:
    """Check if message text matches a reply keyboard button in any locale."""
    from backend.app.i18n import _load_translations

    for lang_code in ("en", "uk"):
        translations = _load_translations(NS, lang_code)
        reply_kb = translations.get("reply_kb", {})
        if reply_kb.get(key) == text:
            return True
    return False


@router.message(Command("camera"))
async def cmd_camera(message: Message, tg_chat=None) -> None:
    """Handle /camera — quick snapshot. If one printer, snapshot directly. If many, show picker."""
    lang = await get_language()
    from backend.app.services.telegram_handlers.common import get_printers_data, has_perm

    if not has_perm(tg_chat, "camera:view"):
        return

    printers = await get_printers_data()
    connected = [p for p in printers if p["connected"]]

    if not connected:
        await message.answer(escape_md(t(lang, NS, "camera.not_available")))
        return

    if len(connected) == 1:
        # Direct snapshot
        from sqlalchemy import select

        from backend.app.core.database import async_session
        from backend.app.models.printer import Printer
        from backend.app.services.camera import capture_camera_frame_bytes

        async with async_session() as db:
            result = await db.execute(select(Printer).where(Printer.id == connected[0]["id"]))
            printer = result.scalar_one_or_none()

        if printer:
            try:
                jpeg = await capture_camera_frame_bytes(printer.ip_address, printer.access_code, printer.model)
                if jpeg:
                    from aiogram.types import BufferedInputFile

                    await message.answer_photo(
                        photo=BufferedInputFile(jpeg, "snapshot.jpg"),
                        caption=f"\U0001f4f7 {escape_md(printer.name)}",
                    )
                    return
            except Exception:
                pass
        await message.answer(escape_md(t(lang, NS, "camera.failed")))
    else:
        # Show picker
        btns = []
        for p in connected:
            btns.append(
                [
                    InlineKeyboardButton(
                        text=f"\U0001f4f7 {p['name']}",
                        callback_data=f"action:camera:{p['id']}",
                    )
                ]
            )
        await message.answer(
            escape_md(t(lang, NS, "camera.select_printer")),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=btns),
        )


@router.message(F.text.func(lambda text: _matches_reply_button(text, "printers")))
async def reply_printers(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Printers button."""
    from backend.app.services.telegram_handlers.printers import show_printer_list

    await show_printer_list(message, tg_chat)


@router.message(F.text.func(lambda text: _matches_reply_button(text, "queue")))
async def reply_queue(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Queue button."""
    from backend.app.services.telegram_handlers.queue import render_queue

    await render_queue(message, tg_chat)


@router.message(F.text.func(lambda text: _matches_reply_button(text, "library")))
async def reply_library(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Library button."""
    lang = await get_language()
    from backend.app.services.telegram_handlers.common import has_perm

    if not (has_perm(tg_chat, "library:read") and has_perm(tg_chat, "printers:control")):
        return
    from backend.app.services.telegram_handlers.library_scene import _get_library_files

    # Send as inline message so edit_text works
    files, total = await _get_library_files(0)
    if not files:
        await message.answer(escape_md(t(lang, NS, "library.no_files")))
        return
    # Create a "fake" inline entry — send a new message with file list
    lines = [
        f"\U0001f4c2 *{escape_md(t(lang, NS, 'library.title'))}*",
        escape_md(t(lang, NS, "library.select_file")),
    ]
    from backend.app.services.telegram_handlers.pagination import build_page_nav

    btns = []
    for f in files:
        btns.append([InlineKeyboardButton(text=f"\U0001f4c4 {f.filename}", callback_data=f"lib:file:{f.id}")])
    nav = build_page_nav(total, 0, 8, "page:lib:", lang)
    if nav:
        btns.append(nav)
    btns.append([InlineKeyboardButton(text=f"\u274c {t(lang, NS, 'library.btn_cancel')}", callback_data="lib:cancel")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


@router.message(F.text.func(lambda text: _matches_reply_button(text, "stats")))
async def reply_stats(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Stats button."""
    from backend.app.services.telegram_handlers.stats import render_stats

    await render_stats(message, tg_chat)


@router.message(F.text.func(lambda text: _matches_reply_button(text, "help")))
async def reply_help(message: Message, **kwargs) -> None:
    """Handle reply keyboard: Help button."""
    await cmd_help(message)
