"""Start and help command handlers + reply keyboard."""

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from backend.app.i18n import t, get_language, escape_md

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
                KeyboardButton(text=t(lang, NS, "reply_kb.stats")),
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

    inline_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"\U0001f5a8 {t(lang, NS, 'start.btn_printers')}", callback_data="menu:printers"),
            InlineKeyboardButton(text=f"\U0001f4cb {t(lang, NS, 'start.btn_queue')}", callback_data="menu:queue"),
        ],
        [
            InlineKeyboardButton(text=f"\U0001f4ca {t(lang, NS, 'start.btn_stats')}", callback_data="menu:stats"),
            InlineKeyboardButton(text=f"\u2139\ufe0f {t(lang, NS, 'start.btn_help')}", callback_data="menu:help"),
        ],
    ])

    # Send reply keyboard first (Telegram persists it for the chat)
    await message.answer(
        f"\U0001f44b *Bambuddy HE*",
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


@router.message(F.text.func(lambda text: _matches_reply_button(text, "printers")))
async def reply_printers(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Printers button."""
    from backend.app.services.telegram_handlers.printers import show_printer_list
    await show_printer_list(message, tg_chat)


@router.message(F.text.func(lambda text: _matches_reply_button(text, "queue")))
async def reply_queue(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Queue button."""
    from backend.app.services.telegram_handlers.printers import render_queue
    await render_queue(message, tg_chat)


@router.message(F.text.func(lambda text: _matches_reply_button(text, "stats")))
async def reply_stats(message: Message, tg_chat=None) -> None:
    """Handle reply keyboard: Stats button."""
    from backend.app.services.telegram_handlers.printers import render_stats
    await render_stats(message, tg_chat)


@router.message(F.text.func(lambda text: _matches_reply_button(text, "help")))
async def reply_help(message: Message, **kwargs) -> None:
    """Handle reply keyboard: Help button."""
    await cmd_help(message)
