"""The spools of an unplugged AMS stay assigned (owner decision Р9, option B; upstream ad62d1063).

Once an unplugged unit leaves the printer's AMS list, its trays are no longer found. "Tray not found"
used to mean "slot empty" and unlinked at once: unplugging an AMS for ten minutes to change a nozzle
would have cost every one of its spools its slot, and the next print from them would have been charged
to no spool. An assignment whose UNIT is absent is kept. It feeds nothing and routes nothing while the
unit is away; when the unit is back the same spools are on it, and a different reel is caught by the
ordinary checks.

Not the "one empty report" hold (``slot_unlink_hold``): an AMS that disappears whole is not a slot
whose presence bit reads 0, and a hold opened for such a slot is not confirmed by the unit going away.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app import main as main_module
from backend.app.services import slot_unlink_hold
from backend.tests.integration.test_slot_unlink_hold import (  # noqa: F401 — fixtures
    EVERYTHING_EMPTY,
    EVERYTHING_LOADED,
    _assigned_trays,
    _empty,
    _loaded,
    _unit_of_four,
    clock,
    main_db,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Pushes:
    """AMS lists through the real callback; ``units`` is the whole list the printer state holds."""

    def __init__(self, printer_id: int):
        self.printer_id = printer_id
        self.stamp = 0.0
        self.broadcast = AsyncMock()

    async def push(self, units: list[dict], *, state: str = "IDLE") -> None:
        self.stamp += 1.0
        status = SimpleNamespace(
            state=state,
            raw_data={"ams": units},
            connected=True,
            connection_generation=1,
            last_ams_update=self.stamp,
            tray_now=255,
            last_loaded_tray=-1,
            tray_change_log=[],
        )
        with (
            patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
            patch.object(main_module.printer_manager, "get_status", return_value=status),
            patch.object(main_module.printer_manager, "get_client", return_value=None),
            patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
            patch.object(main_module.ws_manager, "broadcast", new=self.broadcast),
            patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
        ):
            await main_module.on_ams_change(self.printer_id, units)

    def unlinked(self) -> list[int]:
        return sorted(
            call.args[0]["tray_id"]
            for call in self.broadcast.await_args_list
            if call.args[0].get("type") == "spool_assignment_changed"
        )


def _ams(trays: list[dict], unit_id: int = 0) -> dict:
    return {"id": unit_id, "tray": trays}


async def test_an_unplugged_ams_keeps_all_four_spools(db_session, main_db, printer_factory, clock):  # noqa: F811
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)

    await pushes.push([])  # the unit has left the list
    clock.now += slot_unlink_hold.HOLD_SECONDS * 4
    await pushes.push([])

    assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
    assert pushes.unlinked() == []
    assert not slot_unlink_hold.has_open(printer.id)  # nothing is waiting to be unlinked either


async def test_replugged_with_the_same_reels_nothing_changed(db_session, main_db, printer_factory, clock):  # noqa: F811
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)

    await pushes.push([])
    clock.now += 600
    await pushes.push([_ams(EVERYTHING_LOADED)])

    assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
    assert pushes.unlinked() == []


async def test_another_unit_present_does_not_make_the_absent_one_empty(db_session, main_db, printer_factory, clock):  # noqa: F811
    """AMS 0 unplugged, AMS 1 still there: the rows of AMS 0 stay."""
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)

    await pushes.push([_ams(EVERYTHING_LOADED, unit_id=1)])
    clock.now += slot_unlink_hold.HOLD_SECONDS * 4
    await pushes.push([_ams(EVERYTHING_LOADED, unit_id=1)])

    assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]


async def test_a_hold_opened_before_the_unit_left_is_not_confirmed_by_its_absence(
    db_session,
    main_db,  # noqa: F811
    printer_factory,
    clock,  # noqa: F811
):
    """Pulling the plug can show the slots empty for a push before the unit goes: that opens the
    25 s hold. The unit then being gone is not "still empty" -- the hold is dropped, the rows stay."""
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)

    await pushes.push([_ams(EVERYTHING_EMPTY)])
    assert slot_unlink_hold.has_open(printer.id)
    clock.now += slot_unlink_hold.HOLD_SECONDS + 1
    await pushes.push([])

    assert await _assigned_trays(db_session, printer.id) == [0, 1, 2, 3]
    assert not slot_unlink_hold.has_open(printer.id)


async def test_a_reel_taken_out_of_a_present_unit_is_still_unlinked(db_session, main_db, printer_factory, clock):  # noqa: F811
    """Unchanged: the unit is there and its slot stays empty past the window."""
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)
    one_out = [_ams([_empty(0), _loaded(1), _loaded(2), _loaded(3)])]

    await pushes.push(one_out)
    clock.now += slot_unlink_hold.HOLD_SECONDS + 1
    await pushes.push(one_out)

    assert await _assigned_trays(db_session, printer.id) == [1, 2, 3]
    assert pushes.unlinked() == [0]


async def test_a_tray_missing_from_a_present_unit_is_unlinked_as_before(db_session, main_db, printer_factory, clock):  # noqa: F811
    """Unchanged: the unit reports, and has no such tray."""
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)

    await pushes.push([_ams([_loaded(0), _loaded(1), _loaded(2)])])

    assert await _assigned_trays(db_session, printer.id) == [0, 1, 2]
    assert pushes.unlinked() == [3]


async def test_a_different_reel_after_the_replug_is_caught(db_session, main_db, printer_factory, clock):  # noqa: F811
    printer, _spools = await _unit_of_four(db_session, printer_factory)
    pushes = Pushes(printer.id)

    await pushes.push([])
    await pushes.push([_ams([_loaded(0, tray_type="PLA", tray_color="00FF00FF"), _loaded(1), _loaded(2), _loaded(3)])])

    assert await _assigned_trays(db_session, printer.id) == [1, 2, 3]
