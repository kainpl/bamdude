"""An AMS change re-binds a slot's K only when the slot is not already on it (owner decision Р12).

While idle, ``on_ams_change`` re-applies the calibration of a tagged spool that
has a K link. The helper it calls publishes whenever it resolves a profile, and
an AMS change is any change of the unit's hash — a remain% tick included — so a
slot that was already on the right profile got ``extrusion_cali_sel`` again and
again; the comment above the block claimed the opposite. Upstream deleted the
block (f45cc599f); ours stays, because BamDude is the binder, and now skips when
the tray in the SAME pushed data already reports the resolved index.

What is sent, before → after, for a tagged spool with a link whose profile is
live at index 5:

| the slot reports      | before | after |
|-----------------------|--------|-------|
| ``cali_idx`` 5        | sel 5  | —     |
| ``cali_idx`` -1       | sel 5  | sel 5 |
| ``cali_idx`` 3        | sel 5  | sel 5 |
| no ``cali_idx`` field | sel 5  | sel 5 |
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import main as main_module
from backend.app.models.filament_calibration import FilamentCalibration
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_k_profile import SpoolKProfile
from backend.app.services import usage_tracker

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

UUID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
FAMILY = "GFA00"


@pytest.fixture(autouse=True)
def _clean_memory():
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()
    yield
    usage_tracker._active_sessions.clear()
    usage_tracker._completing.clear()


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


async def _linked_reel(db, printer_factory):
    printer = await printer_factory(model="P1S")
    spool = Spool(
        material="PLA",
        rgba="FF0000FF",
        label_weight=1000,
        weight_used=200,
        tray_uuid=UUID,
        filament_family_id=FAMILY,
    )
    calibration = FilamentCalibration(
        printer_id=printer.id,
        filament_id=FAMILY,
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        extruder_id=0,
        pa_k_value=0.02,
        cali_mode="pa_line",
        source="printer_sync",
        is_active=True,
        cali_idx=5,
        name="PLA K0.020",
    )
    db.add_all([spool, calibration])
    await db.commit()
    db.add_all(
        [
            SpoolAssignment(
                spool_id=spool.id,
                printer_id=printer.id,
                ams_id=0,
                tray_id=0,
                fingerprint_color="FF0000FF",
                fingerprint_type="PLA",
            ),
            SpoolKProfile(
                spool_id=spool.id,
                printer_id=printer.id,
                extruder=0,
                filament_calibration_id=calibration.id,
                auto_linked=True,
            ),
        ]
    )
    await db.commit()
    return printer


def _client() -> MagicMock:
    live = MagicMock(slot_id=5, k_value="0.020000", filament_id=FAMILY, extruder_id=0, nozzle_diameter="0.4")
    live.name = "PLA K0.020"
    client = MagicMock()
    client.state = SimpleNamespace(connected=True, kprofiles=[live], ams_extruder_map={}, nozzles=[])
    client.extrusion_cali_sel = MagicMock(return_value=True)
    return client


async def _push(printer_id: int, client: MagicMock, *, state: str = "IDLE", **tray_extra) -> None:
    """One AMS push through the real callback; ``tray_extra`` is what the slot reports."""
    tray = {
        "id": 0,
        "state": 11,
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "tray_info_idx": FAMILY,
        "tray_uuid": UUID,
        "tag_uid": "0102030405060708",
        "remain": 80,
        **tray_extra,
    }
    status = SimpleNamespace(state=state, raw_data={}, connected=True, nozzles=[], ams_extruder_map={})
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
        patch.object(main_module.printer_manager, "get_status", return_value=status),
        patch.object(main_module.printer_manager, "get_client", return_value=client),
        patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
        patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
        patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
    ):
        await main_module.on_ams_change(printer_id, [{"id": 0, "tray": [tray]}])


async def test_a_slot_already_on_its_profile_gets_nothing(db_session, main_db, printer_factory):
    printer = await _linked_reel(db_session, printer_factory)
    client = _client()

    await _push(printer.id, client, cali_idx=5)
    await _push(printer.id, client, cali_idx=5, remain=79)  # a remain% tick is an AMS change too

    client.extrusion_cali_sel.assert_not_called()


@pytest.mark.parametrize("reported", [{"cali_idx": -1}, {"cali_idx": 3}, {}], ids=["default", "another", "unreported"])
async def test_a_slot_on_anything_else_is_bound_once(db_session, main_db, printer_factory, reported):
    printer = await _linked_reel(db_session, printer_factory)
    client = _client()

    await _push(printer.id, client, **reported)

    client.extrusion_cali_sel.assert_called_once()
    sent = client.extrusion_cali_sel.call_args.kwargs
    assert (sent["ams_id"], sent["tray_id"], sent["cali_idx"], sent["filament_id"]) == (0, 0, 5, FAMILY)


async def test_a_printing_printer_is_still_left_alone(db_session, main_db, printer_factory):
    """Unchanged: the block runs only while no print session is registered."""
    printer = await _linked_reel(db_session, printer_factory)
    client = _client()
    usage_tracker._active_sessions[printer.id] = MagicMock()

    await _push(printer.id, client, state="RUNNING", cali_idx=-1)

    client.extrusion_cali_sel.assert_not_called()


async def test_the_same_index_with_another_k_is_another_tables_profile(db_session, main_db, printer_factory):
    """Indexes are numbered per nozzle table: index 5 of another table is a different profile. A slot
    that reports the resolved index but another K is not "already on it"."""
    printer = await _linked_reel(db_session, printer_factory)
    client = _client()

    await _push(printer.id, client, cali_idx=5, k=0.035)

    client.extrusion_cali_sel.assert_called_once()
    assert client.extrusion_cali_sel.call_args.kwargs["cali_idx"] == 5


async def test_the_same_index_with_the_profiles_own_k_gets_nothing(db_session, main_db, printer_factory):
    printer = await _linked_reel(db_session, printer_factory)
    client = _client()

    await _push(printer.id, client, cali_idx=5, k=0.02)
    await _push(printer.id, client, cali_idx=5, k="0.020")

    client.extrusion_cali_sel.assert_not_called()
