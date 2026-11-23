"""A slot showing the type WE wrote for a spool keeps that spool (upstream #2902, 7b181b84).

The fingerprint of a non-RFID assignment is the slot as it was BEFORE we
configured it, so the first AMS push after the assign always differs from it.
What saves the link is the second check: the tray now matches the spool. That
check compared the tray's type with ``spool.material`` — but the type we write
is the one the slot plan (``build_slot_assignment``) chooses: the family's
filament type, or the stand-in profile's when the spool has none. For PLA Aero
that is ``PLA-AERO`` against a material of ``PLA``; for a spool without a family
whose material reads "PLA Matte" it is ``PLA`` (Generic PLA). Either way the
spool was unlinked from the slot it had just been assigned to, on the first push.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app import main as main_module
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.bambu_mqtt import BambuMQTTClient

_NOT_BAMBU = "00000000000000000000000000000000"


@pytest.fixture
def main_db(monkeypatch, db_session):
    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.main.async_session", _session_ctx)


async def _assign(db_session, printer_factory, **spool_fields):
    """A spool assigned over a slot that held something else: the fingerprint is that something else."""
    printer = await printer_factory()
    spool = Spool(label_weight=1000, weight_used=0, rgba="E0E0E0FF", color_name="white", **spool_fields)
    db_session.add(spool)
    await db_session.commit()
    await db_session.refresh(spool)
    db_session.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer.id,
            ams_id=0,
            tray_id=1,
            fingerprint_color="000000FF",
            fingerprint_type="PETG",
        )
    )
    await db_session.commit()
    return printer, spool


def _slot(tray_type: str, tray_color: str = "E0E0E0FF"):
    return [
        {
            "id": 0,
            "tray": [{"id": 1, "state": 11, "tray_type": tray_type, "tray_color": tray_color, "tray_uuid": _NOT_BAMBU}],
        }
    ]


async def _push(printer_id, ams_data):
    status = SimpleNamespace(state="IDLE", raw_data={})
    with (
        patch("backend.app.services.ams_backup_compatibility_apply.rebuild_once", new=AsyncMock()),
        patch.object(main_module.printer_manager, "get_status", return_value=status),
        patch.object(main_module.ws_manager, "send_printer_status", new=AsyncMock()),
        patch.object(main_module.ws_manager, "broadcast", new=AsyncMock()),
        patch.object(main_module.mqtt_relay, "on_ams_change", new=AsyncMock()),
    ):
        await main_module.on_ams_change(printer_id, ams_data)


async def _links(db_session, printer_id):
    rows = await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer_id))
    return [(a.spool_id, a.fingerprint_type) for a in rows.scalars().all()]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_spool_whose_family_names_its_own_type_keeps_the_slot(db_session, printer_factory, main_db):
    printer, spool = await _assign(
        db_session, printer_factory, material="PLA", subtype="Aero", filament_family_id="GFA11"
    )

    await _push(printer.id, _slot("PLA-AERO"))

    assert await _links(db_session, printer.id) == [(spool.id, "PLA-AERO")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_spool_without_a_family_keeps_the_slot_its_stand_in_type_went_to(db_session, printer_factory, main_db):
    printer, spool = await _assign(db_session, printer_factory, material="PLA Matte")

    await _push(printer.id, _slot("PLA"))

    assert await _links(db_session, printer.id) == [(spool.id, "PLA")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_spool_of_a_type_the_catalogue_lacks_keeps_its_own_type(db_session, printer_factory, main_db):
    """ASA-GF has no family anywhere: Generic ASA lends the profile, the slot says ASA-GF."""
    printer, spool = await _assign(db_session, printer_factory, material="ASA-GF")

    await _push(printer.id, _slot("ASA-GF"))

    assert await _links(db_session, printer.id) == [(spool.id, "ASA-GF")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_different_filament_in_the_slot_still_unlinks(db_session, printer_factory, main_db):
    printer, _spool = await _assign(
        db_session, printer_factory, material="PLA", subtype="Aero", filament_family_id="GFA11"
    )

    await _push(printer.id, _slot("PETG-CF"))

    assert await _links(db_session, printer.id) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_support_echo_keeps_the_binding_but_plain_pla_releases_it(db_session, printer_factory, main_db):
    printer, spool = await _assign(db_session, printer_factory, material="PLA", filament_family_id="GFS05")
    support = _slot("PLA")
    support[0]["tray"][0]["tray_info_idx"] = "GFS05"
    await _push(printer.id, support)
    assert await _links(db_session, printer.id) == [(spool.id, "PLA-S")]

    plain = _slot("PLA")
    plain[0]["tray"][0]["tray_info_idx"] = "GFA00"
    await _push(printer.id, plain)
    assert await _links(db_session, printer.id) == []


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("update", ["partial-reconnect", "family-only-swap", "confirmed-empty"])
async def test_support_binding_follows_merged_mqtt_updates(db_session, printer_factory, main_db, update):
    printer, spool = await _assign(db_session, printer_factory, material="PLA", filament_family_id="GFS05")
    frames = []

    def client():
        mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="P1P")
        mqtt.on_ams_change = lambda units: frames.append(deepcopy(units))
        return mqtt

    mqtt = client()
    support = _slot("PLA")
    support[0]["tray"][0].update({"tray_info_idx": "GFS05", "remain": -1})
    mqtt._handle_ams_data({"ams": support})
    assert len(frames) == 1
    await _push(printer.id, frames.pop())
    assert await _links(db_session, printer.id) == [(spool.id, "PLA-S")]

    report = deepcopy(support)
    tray = report[0]["tray"][0]
    expected = []
    if update == "partial-reconnect":
        mqtt = client()
        del tray["tray_type"]
        tray["tray_color"] = ""
        expected = [(spool.id, "PLA-S")]
    elif update == "family-only-swap":
        tray["tray_info_idx"] = "GFA00"
    else:
        tray.update({"state": 9, "tray_type": "", "tray_color": ""})

    mqtt._handle_ams_data({"ams": report})
    assert mqtt.state.raw_data["ams"][0]["tray"][0]["tray_info_idx"] == tray["tray_info_idx"]
    for frame in frames:
        await _push(printer.id, frame)
    assert (len(frames), await _links(db_session, printer.id)) == (1, expected)

    mqtt._handle_ams_data({"ams": report})
    assert len(frames) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_partial_support_report_keeps_the_rfid_fingerprint(db_session, printer_factory, main_db):
    tray_uuid = "A" * 32
    printer, _spool = await _assign(
        db_session, printer_factory, material="PLA", filament_family_id="GFS05", tray_uuid=tray_uuid
    )
    report = _slot("PLA")
    tray = report[0]["tray"][0]
    tray.update({"tray_info_idx": "GFS05", "tray_uuid": tray_uuid})
    await _push(printer.id, report)

    del tray["tray_type"]
    tray["tray_color"] = ""
    await _push(printer.id, report)

    row = await db_session.scalar(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer.id))
    assert (row.fingerprint_type, row.fingerprint_color) == ("PLA-S", "E0E0E0FF")

    tray["tray_color"] = "202020FF"
    await _push(printer.id, report)
    assert (row.fingerprint_type, row.fingerprint_color) == ("PLA-S", "202020FF")

    del tray["tray_color"]
    tray.update({"tray_type": "PLA", "tray_info_idx": "GFA00"})
    await _push(printer.id, report)
    assert (row.fingerprint_type, row.fingerprint_color) == ("PLA", "202020FF")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("tagged", [False, True], ids=["untagged", "same-rfid"])
async def test_support_reconnect_without_color_preserves_the_binding(db_session, printer_factory, main_db, tagged):
    tray_uuid = "A" * 32 if tagged else _NOT_BAMBU
    printer, spool = await _assign(
        db_session, printer_factory, material="PLA", filament_family_id="GFS05", tray_uuid=tray_uuid if tagged else None
    )
    frames = []

    def client():
        mqtt = BambuMQTTClient("127.0.0.1", "SYNTHETIC", "00000000", model="P1P")
        mqtt.on_ams_change = lambda units: frames.append(deepcopy(units))
        return mqtt

    async def binding():
        row = await db_session.scalar(select(SpoolAssignment).where(SpoolAssignment.printer_id == printer.id))
        return (row.spool_id, row.fingerprint_type, row.fingerprint_color) if row else None

    initial = _slot("PLA")
    initial[0]["tray"][0].update({"tray_info_idx": "GFS05", "remain": -1, "tray_uuid": tray_uuid})
    client()._handle_ams_data({"ams": initial})
    await _push(printer.id, frames.pop())
    expected = (spool.id, "PLA-S", "E0E0E0FF")
    assert await binding() == expected

    mqtt = client()
    partial = deepcopy(initial)
    del partial[0]["tray"][0]["tray_color"]
    mqtt._handle_ams_data({"ams": partial})
    assert len(frames) == 1
    assert frames[0][0]["tray"][0]["tray_type"] == "PLA"
    assert "tray_color" not in frames[0][0]["tray"][0]
    await _push(printer.id, frames.pop())
    assert await binding() == expected

    # No other field suggests a change when the printer finally reports color.
    colored = deepcopy(initial)
    colored[0]["tray"][0]["tray_color"] = "202020FF"
    mqtt._handle_ams_data({"ams": colored})
    assert len(frames) == 1
    await _push(printer.id, frames.pop())
    assert await binding() == ((spool.id, "PLA-S", "202020FF") if tagged else None)

    mqtt._handle_ams_data({"ams": colored})
    assert not frames


@pytest.mark.asyncio
@pytest.mark.integration
async def test_support_family_change_still_unlinks_without_color(db_session, printer_factory, main_db):
    printer, spool = await _assign(db_session, printer_factory, material="PLA", filament_family_id="GFS05")
    initial = _slot("PLA")
    initial[0]["tray"][0]["tray_info_idx"] = "GFS05"
    await _push(printer.id, initial)
    assert await _links(db_session, printer.id) == [(spool.id, "PLA-S")]

    replacement = _slot("PLA")
    tray = replacement[0]["tray"][0]
    tray["tray_info_idx"] = "GFA00"
    del tray["tray_color"]
    await _push(printer.id, replacement)
    assert await _links(db_session, printer.id) == []
