"""Tests the self-clearing maintenance-due Telegram notification.

A maintenance-due notification carries one ✅ button per due item. Once a
button is pressed and the maintenance is recorded, the button is removed
from the message; when it was the last one, the whole notification is
deleted. ``_strip_notification_button`` does that pruning.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _markup(*callback_datas: str) -> InlineKeyboardMarkup:
    """An inline keyboard with one button per row (the notification layout)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"done {cd}", callback_data=cd)] for cd in callback_datas]
    )


def _callback(pressed: str, markup: InlineKeyboardMarkup | None):
    callback = MagicMock()
    callback.data = pressed
    if markup is None:
        callback.message = None
    else:
        callback.message = MagicMock()
        callback.message.reply_markup = markup
        callback.message.edit_reply_markup = AsyncMock()
        callback.message.delete = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_strip_removes_only_pressed_button_when_others_remain():
    """One of several buttons pressed → that button is dropped, the message
    is edited (not deleted), and the remaining buttons survive."""
    from backend.app.services.telegram_handlers.maintenance_handlers import _strip_notification_button

    markup = _markup("maint:done:1:5:n", "maint:done:2:5:n", "maint:done:3:5:n")
    callback = _callback("maint:done:2:5:n", markup)

    await _strip_notification_button(callback)

    callback.message.delete.assert_not_called()
    callback.message.edit_reply_markup.assert_awaited_once()
    new_markup = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    remaining = [b.callback_data for row in new_markup.inline_keyboard for b in row]
    assert remaining == ["maint:done:1:5:n", "maint:done:3:5:n"]


@pytest.mark.asyncio
async def test_strip_deletes_message_when_last_button_pressed():
    """The last outstanding button pressed → the whole notification is deleted."""
    from backend.app.services.telegram_handlers.maintenance_handlers import _strip_notification_button

    callback = _callback("maint:done:7:5:n", _markup("maint:done:7:5:n"))

    await _strip_notification_button(callback)

    callback.message.delete.assert_awaited_once()
    callback.message.edit_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_strip_noop_when_message_missing():
    """An inaccessible/absent message must not raise."""
    from backend.app.services.telegram_handlers.maintenance_handlers import _strip_notification_button

    callback = _callback("maint:done:1:5:n", None)
    await _strip_notification_button(callback)  # no exception


@pytest.mark.asyncio
async def test_strip_swallows_telegram_errors():
    """Telegram refuses to edit/delete messages older than 48 h — the DB
    write already succeeded, so the failure must be swallowed silently."""
    from backend.app.services.telegram_handlers.maintenance_handlers import _strip_notification_button

    callback = _callback("maint:done:9:5:n", _markup("maint:done:9:5:n"))
    callback.message.delete = AsyncMock(side_effect=RuntimeError("message to delete not found"))

    await _strip_notification_button(callback)  # no exception propagates
    callback.message.delete.assert_awaited_once()
