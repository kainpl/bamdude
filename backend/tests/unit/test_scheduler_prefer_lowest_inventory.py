"""Tests for the inventory-weight "Prefer Lowest Remaining Filament" sort (#1508).

Covers the two-tier sort key + slot tiebreaker (pure), the inventory-remain
override builder (internal-inventory mode via DB, Spoolman mode via a mocked
client), and the integration through _match_filaments_to_slots.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select  # noqa: F401  (kept for parity with sibling tests)

from backend.app.models.printer import Printer
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.print_scheduler import PrintScheduler


def _loaded(gtid, ams_id, tray_id, *, remain=-1, type_="PLA", color="FF0000", idx="", external=False):
    return {
        "global_tray_id": gtid,
        "ams_id": ams_id,
        "tray_id": tray_id,
        "type": type_,
        "color": color,
        "tray_info_idx": idx,
        "remain": remain,
        "is_external": external,
    }


# ── Pure: slot priority + sort key ──────────────────────────────────────────


def test_slot_priority_bands():
    assert PrintScheduler._slot_priority(0, 0) == 0
    assert PrintScheduler._slot_priority(0, 3) == 3
    assert PrintScheduler._slot_priority(1, 0) == 4
    assert PrintScheduler._slot_priority(128, 0) == 1000  # AMS-HT band
    assert PrintScheduler._slot_priority(-1, 0) == 10_000  # VT/external band
    assert PrintScheduler._slot_priority(None, None) == 10_000
    # Regular AMS < AMS-HT < external on ties
    assert PrintScheduler._slot_priority(7, 3) < PrintScheduler._slot_priority(128, 0)
    assert PrintScheduler._slot_priority(128, 0) < PrintScheduler._slot_priority(-1, 0)


def test_sort_key_inventory_tier_beats_mqtt():
    """An inventory-tracked spool (even with MORE grams) sorts before an
    MQTT-only spool, because the user opted into tracking it."""
    overrides = {10: 500.0}  # gtid 10 tracked, 500 g remaining
    tracked = _loaded(10, 0, 0, remain=5)  # tiny MQTT remain, but tracked
    mqtt_only = _loaded(11, 0, 1, remain=2)  # lower MQTT %, but untracked
    k_tracked = PrintScheduler._prefer_lowest_sort_key(tracked, overrides)
    k_mqtt = PrintScheduler._prefer_lowest_sort_key(mqtt_only, overrides)
    assert k_tracked[0] == 0 and k_mqtt[0] == 1
    assert k_tracked < k_mqtt  # tier 0 always before tier 1


def test_sort_key_ascending_within_inventory_tier():
    overrides = {10: 800.0, 11: 120.0}
    a = PrintScheduler._prefer_lowest_sort_key(_loaded(10, 0, 0), overrides)
    b = PrintScheduler._prefer_lowest_sort_key(_loaded(11, 0, 1), overrides)
    assert b < a  # 120 g sorts before 800 g


def test_sort_key_mqtt_unknown_maps_to_101():
    k = PrintScheduler._prefer_lowest_sort_key(_loaded(10, 0, 0, remain=-1), None)
    assert k == (1, 101.0, 0)


# ── Integration through _match_filaments_to_slots ───────────────────────────


def test_match_prefers_lower_inventory_remaining():
    """Two trays of the same filament; the one with less inventory-tracked
    remaining wins when prefer_lowest is on."""
    scheduler = PrintScheduler()
    required = [{"slot_id": 1, "type": "PLA", "color": "FF0000", "tray_info_idx": ""}]
    loaded = [
        _loaded(0, 0, 0, remain=90),  # MQTT says lots left
        _loaded(1, 0, 1, remain=90),
    ]
    overrides = {0: 700.0, 1: 150.0}  # tray gtid=1 has less inventory remaining

    mapping = scheduler._match_filaments_to_slots(
        required, loaded, prefer_lowest=True, inventory_remain_overrides=overrides
    )
    assert mapping == [1]  # slot 1 → global_tray_id 1 (the lower-inventory tray)


def test_match_without_prefer_lowest_keeps_slot_order():
    scheduler = PrintScheduler()
    required = [{"slot_id": 1, "type": "PLA", "color": "FF0000", "tray_info_idx": ""}]
    loaded = [_loaded(0, 0, 0, remain=10), _loaded(1, 0, 1, remain=90)]
    mapping = scheduler._match_filaments_to_slots(required, loaded, prefer_lowest=False)
    assert mapping == [0]  # first matching tray, no remain-based reorder


# ── _build_inventory_remain_overrides — internal mode (DB) ──────────────────


async def _make_printer(db):
    p = Printer(name="P", serial_number="SN-PL-1", ip_address="10.0.0.1", access_code="0000", model="P1S")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@pytest.mark.asyncio
async def test_build_overrides_internal_mode(db_session):
    scheduler = PrintScheduler()
    printer = await _make_printer(db_session)
    spool = Spool(material="PLA", label_weight=1000, weight_used=850)  # 150 g remaining
    db_session.add(spool)
    await db_session.commit()
    db_session.add(SpoolAssignment(printer_id=printer.id, ams_id=0, tray_id=2, spool_id=spool.id))
    await db_session.commit()

    loaded = [_loaded(2, 0, 2), _loaded(3, 0, 3)]  # gtid 2 bound, gtid 3 unbound
    with patch.object(PrintScheduler, "_is_spoolman_mode", new=AsyncMock(return_value=False)):
        overrides = await scheduler._build_inventory_remain_overrides(db_session, printer.id, loaded)

    assert overrides == {2: 150.0}  # bound slot only; unbound absent


@pytest.mark.asyncio
async def test_build_overrides_skips_external(db_session):
    scheduler = PrintScheduler()
    printer = await _make_printer(db_session)
    loaded = [_loaded(254, 254, 0, external=True)]
    with patch.object(PrintScheduler, "_is_spoolman_mode", new=AsyncMock(return_value=False)):
        overrides = await scheduler._build_inventory_remain_overrides(db_session, printer.id, loaded)
    assert overrides == {}


@pytest.mark.asyncio
async def test_build_overrides_spoolman_mode(db_session):
    scheduler = PrintScheduler()
    printer = await _make_printer(db_session)
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=1, spoolman_spool_id=77))
    await db_session.commit()

    fake_client = AsyncMock()
    fake_client.get_spool = AsyncMock(return_value={"remaining_weight": 230.0})
    loaded = [_loaded(1, 0, 1)]
    with (
        patch.object(PrintScheduler, "_is_spoolman_mode", new=AsyncMock(return_value=True)),
        patch("backend.app.services.spoolman.get_spoolman_client", new=AsyncMock(return_value=fake_client)),
    ):
        overrides = await scheduler._build_inventory_remain_overrides(db_session, printer.id, loaded)

    assert overrides == {1: 230.0}
    fake_client.get_spool.assert_awaited_once_with(77)
