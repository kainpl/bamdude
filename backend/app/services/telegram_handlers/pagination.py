"""Reusable pagination utility for Telegram bot inline keyboards."""

from aiogram.types import InlineKeyboardButton

NS = "telegram_ui"


def build_page_nav(
    total: int,
    offset: int,
    page_size: int,
    callback_prefix: str,
    lang: str,
) -> list[InlineKeyboardButton]:
    """Build navigation row: [<< Prev] [2/5] [Next >>].

    Args:
        total: total number of items
        offset: current offset (0-based)
        page_size: items per page
        callback_prefix: e.g. "page:queue:" — offset is appended
        lang: language code for i18n

    Returns:
        List of buttons for one keyboard row, or empty list if only 1 page.
    """
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = offset // page_size + 1

    if total_pages <= 1:
        return []

    buttons = []

    if current_page > 1:
        prev_offset = max(0, offset - page_size)
        buttons.append(
            InlineKeyboardButton(
                text="\u25c0\ufe0f",
                callback_data=f"{callback_prefix}{prev_offset}",
            )
        )

    buttons.append(
        InlineKeyboardButton(
            text=f"{current_page}/{total_pages}",
            callback_data="noop",
        )
    )

    if current_page < total_pages:
        next_offset = offset + page_size
        buttons.append(
            InlineKeyboardButton(
                text="\u25b6\ufe0f",
                callback_data=f"{callback_prefix}{next_offset}",
            )
        )

    return buttons
