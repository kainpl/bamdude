"""An AMS Lite shows its serial and firmware, and its label survives getting one (upstream 5eae04359).

``get_version`` names the Lite's module ``ams_f1/<id>``; while the prefix was
unknown the unit had no serial, so a label was saved under the position key
``p<printer>a<ams>``. Once the serial is read the label must not vanish from
the location column, and saving / clearing must not leave the old row behind.
"""

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.ams_label import AmsLabel
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    client = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    client._client = MagicMock()
    client.state.connected = True
    return client


def _version(client: BambuMQTTClient, name: str) -> None:
    client._handle_version_info(
        {"command": "get_version", "module": [{"name": name, "sw_ver": "00.00.07.94", "sn": "LITE-SN-1"}]}
    )


def test_an_a1_lite_module_gives_the_unit_its_serial_and_firmware():
    client = _client()
    client.state.raw_data["ams"] = [{"id": "0", "tray": []}]

    _version(client, "ams_f1/0")

    unit = client.state.raw_data["ams"][0]
    assert (unit["sn"], unit["sw_ver"], unit["module_type"]) == ("LITE-SN-1", "00.00.07.94", "ams_f1")


def test_the_a2l_lite_module_is_filed_under_its_normalised_unit():
    """The A2L reports the Lite as unit 16; everything downstream knows it as 6."""
    client = _client()
    client.state.raw_data["ams"] = [{"id": "6", "tray": []}]

    _version(client, "ams_f1/16")

    unit = client.state.raw_data["ams"][0]
    assert (unit["sn"], unit["sw_ver"]) == ("LITE-SN-1", "00.00.07.94")
    assert 16 not in client._ams_version_cache and 6 in client._ams_version_cache


def _state(sn: str = "LITE-SN-1") -> MagicMock:
    state = MagicMock()
    state.connected = True
    state.raw_data = {"ams": [{"id": "0", "sn": sn}]}
    return state


async def _keys(db_session) -> set[str]:
    db_session.expire_all()
    return set((await db_session.execute(select(AmsLabel.ams_serial_number))).scalars().all())


@pytest.mark.asyncio
@pytest.mark.integration
class TestALabelSavedBeforeTheSerialWasKnown:
    async def test_it_is_still_shown_on_the_card(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()
        db_session.add(AmsLabel(ams_serial_number=f"p{printer.id}a0", ams_id=0, label="Shelf Lite"))
        await db_session.commit()

        with patch("backend.app.api.routes.printers.printer_manager") as mock_pm:
            mock_pm.get_status.return_value = _state()
            response = await async_client.get(f"/api/v1/printers/{printer.id}/ams-labels")

        assert response.json() == {"0": "Shelf Lite"}

    async def test_saving_under_the_serial_replaces_it(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()
        db_session.add(AmsLabel(ams_serial_number=f"p{printer.id}a0", ams_id=0, label="Shelf Lite"))
        await db_session.commit()

        response = await async_client.put(
            f"/api/v1/printers/{printer.id}/ams-labels/0", json={"label": "Bench Lite", "ams_serial": "LITE-SN-1"}
        )

        assert response.status_code == 200
        assert await _keys(db_session) == {"LITE-SN-1"}

    async def test_clear_removes_both_rows(self, async_client: AsyncClient, printer_factory, db_session):
        printer = await printer_factory()
        db_session.add(AmsLabel(ams_serial_number=f"p{printer.id}a0", ams_id=0, label="Shelf Lite"))
        db_session.add(AmsLabel(ams_serial_number="LITE-SN-1", ams_id=0, label="Bench Lite"))
        await db_session.commit()

        response = await async_client.delete(f"/api/v1/printers/{printer.id}/ams-labels/0?ams_serial=LITE-SN-1")

        assert response.status_code == 200
        assert await _keys(db_session) == set()

    async def test_clear_leaves_another_printers_position_row_alone(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer_id = (await printer_factory()).id
        other_id = (await printer_factory(name="Other", serial_number="OTHER-SN")).id
        db_session.add(AmsLabel(ams_serial_number=f"p{other_id}a0", ams_id=0, label="Theirs"))
        db_session.add(AmsLabel(ams_serial_number="LITE-SN-1", ams_id=0, label="Bench Lite"))
        await db_session.commit()

        await async_client.delete(f"/api/v1/printers/{printer_id}/ams-labels/0?ams_serial=LITE-SN-1")

        assert await _keys(db_session) == {f"p{other_id}a0"}

    async def test_the_filaments_location_column_still_names_it(
        self, async_client: AsyncClient, printer_factory, db_session, monkeypatch
    ):
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory()
        spool = Spool(material="PLA", label_weight=1000, core_weight=250)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        db_session.add(AmsLabel(ams_serial_number=f"p{printer.id}a0", ams_id=0, label="Shelf Lite"))
        await db_session.commit()
        monkeypatch.setattr(printer_manager, "get_all_statuses", lambda: {printer.id: _state()})

        response = await async_client.get(f"/api/v1/inventory/assignments?printer_id={printer.id}")

        assert response.status_code == 200, response.text
        assert [row["ams_label"] for row in response.json()] == ["Shelf Lite"]

    async def test_a_label_under_the_serial_wins_over_the_position_row(
        self, async_client: AsyncClient, printer_factory, db_session, monkeypatch
    ):
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory()
        spool = Spool(material="PLA", label_weight=1000, core_weight=250)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        db_session.add(AmsLabel(ams_serial_number=f"p{printer.id}a0", ams_id=0, label="Old"))
        db_session.add(AmsLabel(ams_serial_number="LITE-SN-1", ams_id=0, label="New"))
        await db_session.commit()
        monkeypatch.setattr(printer_manager, "get_all_statuses", lambda: {printer.id: _state()})

        response = await async_client.get(f"/api/v1/inventory/assignments?printer_id={printer.id}")

        assert [row["ams_label"] for row in response.json()] == ["New"]
