"""The fields a farm-wide stall is actually diagnosed from.

Triage of issue #21 — three A1 Minis and a P1S, "printing from the archive does
not start" — came down to a guess, because the bundle described the farm in 19
fields and the one that had stopped it was not among them. The symptom was a
stuck plate-clear gate.

⚠️ Everything added here is a bool or an int. The bundle promises it carries no
names, serials or IPs, and one test below enforces that on the new keys rather
than trusting the review that added them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADDED_PRINTER_FIELDS = {
    "archived",
    "awaiting_plate_clear",
    "require_plate_clear",
    "swap_mode_enabled",
    "stagger_interval_minutes",
}


async def _info(printer_factory, test_engine, monkeypatch):
    """Collect against the TEST database, without touching a printer.

    ⚠️ All three patches are required, and the first two were missing when this
    file was written. ``_collect_support_info`` opens its OWN ``async_session``,
    so without redirecting it the test reads the operator's real database; its
    ``diagnostics`` section then runs ``run_connection_diagnostic`` against
    those real printers over the network, and ``_check_port`` opens a TCP
    connection to each. A test has no business doing any of that.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.api.routes import support

    await printer_factory()
    monkeypatch.setattr(
        support, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )
    # Patched at its source: support.py imports it inside the function, so the
    # name does not exist on that module.
    monkeypatch.setattr(
        "backend.app.services.diagnostic_snapshot.collect_diagnostic_snapshot",
        AsyncMock(return_value={}),
    )
    with patch.object(support, "_check_port", AsyncMock(return_value=False)):
        return await support._collect_support_info()


async def test_the_queue_gates_are_reported(printer_factory, test_engine, monkeypatch):
    info = await _info(printer_factory, test_engine, monkeypatch)

    assert info["printers"], "no printers collected — the assertion below would be vacuous"
    for entry in info["printers"]:
        assert set(entry) >= ADDED_PRINTER_FIELDS, f"missing: {ADDED_PRINTER_FIELDS - set(entry)}"


async def test_no_new_field_carries_an_identifier(printer_factory, test_engine, monkeypatch):
    """The cheapest guard there is: a name, serial or IP would be a string, and
    none of these may be one."""
    info = await _info(printer_factory, test_engine, monkeypatch)

    for entry in info["printers"]:
        for key in ADDED_PRINTER_FIELDS:
            assert isinstance(entry[key], (bool, int)), f"{key} is {type(entry[key]).__name__}, not bool/int"


async def test_the_queue_is_broken_down_per_printer(printer_factory, test_engine, monkeypatch):
    """The three global counters cannot say WHICH printer is stuck."""
    info = await _info(printer_factory, test_engine, monkeypatch)

    assert isinstance(info["queue"]["per_printer"], list)
    for row in info["queue"]["per_printer"]:
        assert set(row) >= {"index", "queue_status", "paused", "pending"}
        assert not isinstance(row["index"], str), "per-printer rows key on the anonymous index, never a name"


async def test_the_auto_queue_is_counted(printer_factory, test_engine, monkeypatch):
    """It was invisible, so the bundle inherited the same undercount the queue
    UI used to have."""
    info = await _info(printer_factory, test_engine, monkeypatch)

    assert set(info["queue"]["auto_queue"]) >= {"pending", "assigned", "by_status"}
