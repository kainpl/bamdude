"""The deferred slot configuration is not sent into the slot a running print feeds from (upstream 1cc7f4f18).

A spool assigned to a slot that reports no filament type is configured later, when the slot reports
filament ("pre-config replay"). After a runout the printer blanks the slot; a reel put in and assigned
mid-print leaves it loaded-but-blank, and the replay went out on EVERY AMS change for the rest of the
print -- into the slot the print is feeding from. It is held for that slot only, while the printer
prints, and goes out at the first AMS change after the print. A slot the print does not feed from is
configured at once, as before.

That a printer refuses the setting for its feeding slot is upstream's observation (#3434), not measured
on ours: the hold costs nothing where it would have been accepted -- the same command goes out when the
print ends.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main as main_module
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.bambu_mqtt import ExtruderSlot
from backend.tests.integration.test_slot_unlink_hold import clock, main_db  # noqa: F401 — fixtures

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _blank_loaded(tray_id: int) -> dict:
    """Presence bit on, fed, nothing configured: a reel the printer knows nothing about."""
    return {"id": tray_id, "exists": True, "state": 11, "tray_type": "", "tray_color": ""}


def _loaded(tray_id: int) -> dict:
    return {"id": tray_id, "exists": True, "state": 11, "tray_type": "PETG", "tray_color": "858585FF"}


async def _preassigned(db, printer_factory, tray_id: int = 0):
    printer = await printer_factory()
    spool = Spool(material="PETG", rgba="858585FF", label_weight=1000, weight_used=0)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    db.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=0,
            tray_id=tray_id,
            fingerprint_color="",
            fingerprint_type="",
        )
    )
    await db.commit()
    return printer


class Card:
    def __init__(self, printer_id: int):
        self.printer_id = printer_id
        self.configure = AsyncMock(return_value=True)

    async def push(self, trays: list[dict], *, state: str, tray_now: int = 255, extruder_slots=None) -> None:
        ams = [{"id": 0, "tray": trays}]
        status = SimpleNamespace(
            state=state,
            raw_data={"ams": ams},
            connected=True,
            connection_generation=1,
            last_ams_update=1.0,
            tray_now=tray_now,
            last_loaded_tray=-1,
            tray_change_log=[],
            extruder_slots=extruder_slots or {},
        )
        with (
            patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
            patch("backend.app.api.routes.inventory.apply_spool_to_slot_via_mqtt", new=self.configure),
            patch.object(main_module.printer_manager, "get_status", return_value=status),
            patch.object(main_module.printer_manager, "get_client", return_value=None),
            patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
            patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
            patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
        ):
            await main_module.on_ams_change(self.printer_id, ams)

    def sent_to(self) -> list[tuple[int, int]]:
        return [(call.kwargs["ams_id"], call.kwargs["tray_id"]) for call in self.configure.await_args_list]


FOUR = [_blank_loaded(0), _loaded(1), _loaded(2), _loaded(3)]


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE"])
async def test_the_feeding_slot_is_not_configured_while_the_print_runs(db_session, main_db, printer_factory, state):  # noqa: F811
    printer = await _preassigned(db_session, printer_factory)
    card = Card(printer.id)

    await card.push(FOUR, state=state, tray_now=0)
    await card.push(FOUR, state=state, tray_now=0)  # every AMS change used to send it again

    assert card.sent_to() == []
    assert (await db_session.execute(select(SpoolAssignment.spool_id))).scalars().all() != []  # the row stays


async def test_it_goes_out_at_the_first_change_after_the_print(db_session, main_db, printer_factory):  # noqa: F811
    printer = await _preassigned(db_session, printer_factory)
    card = Card(printer.id)

    await card.push(FOUR, state="RUNNING", tray_now=0)
    await card.push(FOUR, state="FINISH", tray_now=0)

    assert card.sent_to() == [(0, 0)]


async def test_a_slot_fed_into_the_other_hotend_is_held_too(db_session, main_db, printer_factory):  # noqa: F811
    """``tray_now`` names one slot for the whole printer; a dual-nozzle machine feeds two."""
    printer = await _preassigned(db_session, printer_factory)
    card = Card(printer.id)

    await card.push(
        FOUR,
        state="RUNNING",
        tray_now=5,
        extruder_slots={1: ExtruderSlot(ams_id=0, slot_id=0, has_filament=True)},
    )

    assert card.sent_to() == []


async def test_a_slot_the_print_does_not_feed_from_is_configured_at_once(db_session, main_db, printer_factory):  # noqa: F811
    """Unchanged: a reel pre-assigned to another slot gets its profile (and K) during a long print."""
    printer = await _preassigned(db_session, printer_factory, tray_id=1)
    card = Card(printer.id)

    await card.push([_loaded(0), _blank_loaded(1), _loaded(2), _loaded(3)], state="RUNNING", tray_now=0)

    assert card.sent_to() == [(0, 1)]


async def test_an_idle_printer_is_configured_at_once(db_session, main_db, printer_factory):  # noqa: F811
    """Unchanged."""
    printer = await _preassigned(db_session, printer_factory)
    card = Card(printer.id)

    await card.push(FOUR, state="IDLE", tray_now=0)

    assert card.sent_to() == [(0, 0)]
