"""Tests for the virtual-printer system-default print options fallback (#1235).

When a slicer sends a file to a virtual printer but omits the print-option
flags, the queue item falls back to the per-model system row
(``PrintOptionsPreference`` with ``user_id IS NULL``). Precedence is
slicer-sent value → system row → model column default.
"""

from __future__ import annotations

import pytest

from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.services.virtual_printer.manager import (
    VirtualPrinterInstance,
    _resolve_print_option,
)

# ───────────────────────── precedence (pure function) ─────────────────────────


def test_slicer_value_wins_over_system_and_default():
    assert _resolve_print_option({"timelapse": True}, {"timelapse": False}, "timelapse", "timelapse", False) is True
    # int 0/1 shape coerces too
    assert _resolve_print_option({"timelapse": 0}, {"timelapse": True}, "timelapse", "timelapse", True) is False


def test_system_value_wins_when_slicer_silent():
    assert _resolve_print_option(None, {"timelapse": True}, "timelapse", "timelapse", False) is True
    assert _resolve_print_option({}, {"flow_cali": False}, "flow_cali", "flow_cali", True) is False


def test_column_default_when_both_silent():
    assert _resolve_print_option(None, None, "bed_leveling", "bed_levelling", True) is True
    assert _resolve_print_option({}, {}, "layer_inspect", "layer_inspect", False) is False


def test_slicer_and_pref_key_naming_differ():
    # Slicer sends single-L "bed_leveling"; system row stores double-L key.
    assert _resolve_print_option(None, {"bed_levelling": False}, "bed_leveling", "bed_levelling", True) is False


# ────────────────────── _load_system_print_options (DB) ──────────────────────


@pytest.mark.asyncio
async def test_load_returns_none_without_printer_id(db_session):
    assert await VirtualPrinterInstance._load_system_print_options(db_session, None) is None


@pytest.mark.asyncio
async def test_load_returns_none_without_system_row(db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    assert await VirtualPrinterInstance._load_system_print_options(db_session, printer.id) is None


@pytest.mark.asyncio
async def test_load_returns_print_options_for_model(db_session, printer_factory):
    printer = await printer_factory(model="P1S")
    db_session.add(
        PrintOptionsPreference(
            user_id=None,
            printer_model="P1S",
            options={"print_options": {"timelapse": True, "flow_cali": False}, "swap_macros": {}},
        )
    )
    await db_session.commit()

    opts = await VirtualPrinterInstance._load_system_print_options(db_session, printer.id)
    assert opts == {"timelapse": True, "flow_cali": False}


@pytest.mark.asyncio
async def test_load_ignores_other_models_system_row(db_session, printer_factory):
    printer = await printer_factory(model="X1C")
    db_session.add(
        PrintOptionsPreference(user_id=None, printer_model="P1S", options={"print_options": {"timelapse": True}})
    )
    await db_session.commit()
    # Printer is X1C; the only system row is for P1S → no match.
    assert await VirtualPrinterInstance._load_system_print_options(db_session, printer.id) is None
