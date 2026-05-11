"""Regression tests for MarkdownV2 escaping in the Telegram printer-list flow.

The Telegram bot's default parse mode is MarkdownV2 (see ``telegram_bot.py``),
which treats ``.`` (among others) as a reserved character that must be
escaped with a leading backslash. ``status.progress`` is stored as ``float``
in ``PrinterState`` (cast from ``mc_percent`` in ``bambu_mqtt.py``), so an
f-string like ``f"{p['progress']}%"`` renders as ``"25.0%"`` — the unescaped
``.`` crashed every ``message.answer`` and ``edit_text`` that happened to
include a RUNNING printer in the list.

Reported in 0.4.4b3 (operator with one printer in mid-print): ``/status``,
the Printers reply-button, and the printer-detail open all raised
``TelegramBadRequest: can't parse entities: Character '.' is reserved``.

These tests pin the cast to ``int`` for the inline-list and the detail
view. They aren't a full integration test against the live Telegram API —
they assert the rendered text doesn't carry a ``25.0%``-shape token in the
progress segment, which is the failure mode that crashed users.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def fake_running_printer():
    """A single RUNNING printer with a float progress value."""
    return {
        "id": 1,
        "name": "Living Room",
        "model": "X1C",
        "connected": True,
        "state": "RUNNING",
        "progress": 25.7,  # float — the bait
        "current_file": "model.gcode.3mf",
        "nozzle_temp": 220.0,
        "bed_temp": 60.0,
        "remaining_time": 45,
        "awaiting_plate_clear": False,
        "speed_level": 2,
    }


@pytest.mark.asyncio
async def test_show_printer_list_int_casts_float_progress(fake_running_printer):
    """show_printer_list must not embed a raw float in the message text."""
    from backend.app.services.telegram_handlers import printers as printers_mod

    message = MagicMock()
    message.answer = AsyncMock()

    with (
        patch.object(printers_mod, "get_language", new=AsyncMock(return_value="en")),
        patch.object(printers_mod, "get_printers_data", new=AsyncMock(return_value=[fake_running_printer])),
        patch.object(printers_mod, "get_maintenance_counts", new=AsyncMock(return_value=(0, 0))),
    ):
        await printers_mod.show_printer_list(message)

    assert message.answer.await_count == 1
    text = message.answer.await_args.args[0]
    # The crash shape was "25.7%" with an unescaped dot before "7".
    # After the fix the percent renders integer-only.
    assert re.search(r"\d+\.\d+%", text) is None, f"Float-shaped progress leaked into MD text: {text!r}"
    assert "25%" in text or "26%" in text  # int(25.7) → 25; rounded display tolerant


@pytest.mark.asyncio
async def test_show_printer_detail_int_casts_float_progress(fake_running_printer):
    """show_printer_detail must not embed a raw float in the message text."""
    from backend.app.services.telegram_handlers import printers as printers_mod

    callback = MagicMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()

    with (
        patch.object(printers_mod, "get_language", new=AsyncMock(return_value="en")),
        patch.object(printers_mod, "get_printers_data", new=AsyncMock(return_value=[fake_running_printer])),
        patch.object(printers_mod, "get_total_hours", new=AsyncMock(return_value=12.3)),
        patch.object(printers_mod, "get_next_queue_item", new=AsyncMock(return_value=None)),
    ):
        await printers_mod.show_printer_detail(callback, printer_id=1)

    assert callback.message.edit_text.await_count == 1
    text = callback.message.edit_text.await_args.args[0]
    # Detail view's progress line: "📊 Progress: 25%" (int).
    # Crash shape would have been "25.7%" — assert it's not there.
    progress_match = re.search(r"Progress[^\n]*?(\d+(?:\\?\.\d+)?)%", text)
    assert progress_match is not None, f"Progress segment not found in detail text: {text!r}"
    progress_token = progress_match.group(1)
    assert "." not in progress_token, f"Float-shaped progress in detail text: {progress_token!r}"


@pytest.mark.asyncio
async def test_show_printer_list_no_unescaped_reserved_dots(fake_running_printer):
    """No unescaped reserved character (`.`) should remain in the rendered text.

    MarkdownV2 reserves ``_*[]()~`>#+-=|{}.!\\``. The list rendering uses
    pre-escaped paired markers (``*`` for bold, ``\\(`` / ``\\)`` for literal
    parens), so any *bare* ``.`` outside an escape sequence would crash.
    """
    from backend.app.services.telegram_handlers import printers as printers_mod

    message = MagicMock()
    message.answer = AsyncMock()

    with (
        patch.object(printers_mod, "get_language", new=AsyncMock(return_value="en")),
        patch.object(printers_mod, "get_printers_data", new=AsyncMock(return_value=[fake_running_printer])),
        patch.object(printers_mod, "get_maintenance_counts", new=AsyncMock(return_value=(0, 0))),
    ):
        await printers_mod.show_printer_list(message)

    text = message.answer.await_args.args[0]
    # Walk the text and confirm every `.` is preceded by `\` (escape) or
    # is part of an escaped sequence.
    for i, ch in enumerate(text):
        if ch == ".":
            preceding = text[i - 1] if i > 0 else ""
            assert preceding == "\\", (
                f"Unescaped '.' at offset {i} in rendered list text — would crash MarkdownV2 parser.\nText: {text!r}"
            )
