"""A page of printer cards asks once, not once per card.

Last print, AMS labels, card sensor readings and the automatic firmware check each have a route
that answers for several printers; every value is what the single-printer route answers, and a
printer outside an API key's list is left out.
"""

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from backend.app.models.printer_ha_sensor import PrinterHASensor

pytestmark = pytest.mark.integration


async def _restricted_key(async_client: AsyncClient, printer_ids: list[int]) -> dict:
    response = await async_client.post(
        "/api/v1/api-keys/", json={"name": "cards", "can_read_status": True, "printer_ids": printer_ids}
    )
    assert response.status_code == 200, response.text
    return {"X-API-Key": response.json()["key"]}


async def _with_key(async_client: AsyncClient, path: str, headers: dict):
    saved = async_client.headers.pop("Authorization", None)
    try:
        return await async_client.get(path, headers=headers)
    finally:
        if saved is not None:
            async_client.headers["Authorization"] = saved


class TestLastPrint:
    async def test_the_newest_archive_of_each_printer(self, async_client, printer_factory, archive_factory):
        first, second, idle = await printer_factory(), await printer_factory(), await printer_factory()
        now = datetime(2026, 11, 1, 12, 0, 0)
        await archive_factory(first.id, print_name="old", created_at=now - timedelta(days=2))
        newest = await archive_factory(first.id, print_name="new", created_at=now)
        other = await archive_factory(second.id, print_name="other", created_at=now)

        response = await async_client.get(
            "/api/v1/archives/last-by-printer", params={"ids": [first.id, second.id, idle.id]}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {str(first.id), str(second.id)}
        assert (body[str(first.id)]["id"], body[str(first.id)]["print_name"]) == (newest.id, "new")
        assert body[str(second.id)]["id"] == other.id

    async def test_a_trashed_archive_is_not_the_last_print(self, async_client, printer_factory, archive_factory):
        printer = await printer_factory()
        now = datetime(2026, 11, 1, 12, 0, 0)
        kept = await archive_factory(printer.id, print_name="kept", created_at=now - timedelta(hours=1))
        await archive_factory(printer.id, print_name="trashed", created_at=now, deleted_at=now)

        body = (await async_client.get("/api/v1/archives/last-by-printer", params={"ids": [printer.id]})).json()

        assert body[str(printer.id)]["id"] == kept.id

    async def test_a_key_sees_only_its_printers(self, async_client, printer_factory, archive_factory):
        mine, other = await printer_factory(), await printer_factory()
        await archive_factory(mine.id)
        await archive_factory(other.id)
        headers = await _restricted_key(async_client, [mine.id])

        response = await _with_key(
            async_client, f"/api/v1/archives/last-by-printer?ids={mine.id}&ids={other.id}", headers
        )

        assert response.status_code == 200, response.text
        assert set(response.json()) == {str(mine.id)}

    async def test_a_one_row_list_does_not_group_the_whole_table(
        self, async_client, test_engine, printer_factory, archive_factory
    ):
        printer = await printer_factory()
        await archive_factory(printer.id, content_hash="a" * 64, source_content_hash="a" * 64)
        await archive_factory(printer.id, content_hash="b" * 64, source_content_hash="b" * 64)
        statements: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(test_engine.sync_engine, "before_cursor_execute", record)
        try:
            response = await async_client.get("/api/v1/archives/", params={"printer_id": printer.id, "per_page": 1})
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", record)

        assert response.status_code == 200, response.text
        grouped = [s for s in statements if "GROUP BY" in s.upper() and "print_archives" in s]
        assert grouped, "the duplicate question is still asked"
        assert all(" IN (" in s.upper() for s in grouped), "and only about the hashes on the page"


class TestAmsLabels:
    async def test_the_batch_answers_what_each_printer_answers(self, async_client, printer_factory):
        first, second = await printer_factory(), await printer_factory()

        batch = await async_client.get("/api/v1/printers/ams-labels/batch", params={"ids": [first.id, second.id]})
        single = await async_client.get(f"/api/v1/printers/{first.id}/ams-labels")

        assert batch.status_code == 200, batch.text
        assert set(batch.json()) == {str(first.id), str(second.id)}
        assert batch.json()[str(first.id)] == single.json()

    async def test_a_key_sees_only_its_printers(self, async_client, printer_factory):
        mine, other = await printer_factory(), await printer_factory()
        headers = await _restricted_key(async_client, [mine.id])

        response = await _with_key(
            async_client, f"/api/v1/printers/ams-labels/batch?ids={mine.id}&ids={other.id}", headers
        )

        assert response.status_code == 200, response.text
        assert set(response.json()) == {str(mine.id)}


class TestSensorReadings:
    async def test_readings_are_grouped_by_printer_and_card_only(self, async_client, db_session, printer_factory):
        first, second, bare = await printer_factory(), await printer_factory(), await printer_factory()
        db_session.add_all(
            [
                PrinterHASensor(printer_id=first.id, name="Door", entity_id="binary_sensor.door", kind="binary"),
                PrinterHASensor(
                    printer_id=first.id,
                    name="Hidden",
                    entity_id="sensor.hidden",
                    kind="numeric",
                    show_on_printer_card=False,
                ),
                PrinterHASensor(printer_id=second.id, name="Temp", entity_id="sensor.temp", kind="numeric"),
            ]
        )
        await db_session.commit()

        batch = await async_client.get(
            "/api/v1/ha-sensors/readings/batch", params={"ids": [first.id, second.id, bare.id]}
        )
        single = await async_client.get(f"/api/v1/ha-sensors/by-printer/{first.id}/readings")

        assert batch.status_code == 200, batch.text
        body = batch.json()
        assert [r["name"] for r in body[str(first.id)]] == ["Door"]
        assert [r["name"] for r in body[str(second.id)]] == ["Temp"]
        assert body[str(bare.id)] == []
        assert body[str(first.id)] == single.json()


class TestFirmware:
    async def test_the_batch_answers_what_each_printer_answers(self, async_client, printer_factory, monkeypatch):
        from backend.app.api.routes import firmware

        asked: list[str] = []

        class _Service:
            async def check_for_update(self, model, current):
                asked.append(model)
                return {
                    "update_available": True,
                    "latest_version": "01.09.00.00",
                    "download_url": None,
                    "release_notes": None,
                    "available_versions": [],
                }

        monkeypatch.setattr(firmware, "get_firmware_service", lambda: _Service())
        first, second = await printer_factory(model="X1C"), await printer_factory(model="P1S")

        batch = await async_client.get("/api/v1/firmware/updates/batch", params={"ids": [first.id, second.id, 999999]})
        single = await async_client.get(f"/api/v1/firmware/updates/{first.id}")

        assert batch.status_code == 200, batch.text
        assert set(batch.json()) == {str(first.id), str(second.id)}
        assert batch.json()[str(first.id)] == single.json()

    async def test_the_setting_that_stops_automatic_checks_stops_the_batch(
        self, async_client, printer_factory, monkeypatch
    ):
        from backend.app.api.routes import firmware

        class _Service:
            async def check_for_update(self, model, current):
                raise AssertionError("nobody is contacted while automatic checks are off")

        monkeypatch.setattr(firmware, "get_firmware_service", lambda: _Service())
        printer = await printer_factory()
        assert (await async_client.put("/api/v1/settings/", json={"check_printer_firmware": False})).status_code == 200

        batch = await async_client.get("/api/v1/firmware/updates/batch", params={"ids": [printer.id]})

        assert batch.status_code == 200, batch.text
        assert batch.json()[str(printer.id)]["update_available"] is False


class TestPlugs:
    async def test_the_batch_answers_what_the_two_card_routes_answer(
        self, async_client, printer_factory, smart_plug_factory
    ):
        first, second, bare = await printer_factory(), await printer_factory(), await printer_factory()
        await smart_plug_factory(printer_id=first.id, name="Outlet")
        await smart_plug_factory(
            printer_id=first.id,
            name="Lamp",
            plug_type="homeassistant",
            ha_entity_id="light.lamp",
            show_on_printer_card=True,
        )
        await smart_plug_factory(printer_id=second.id, name="Other")

        batch = await async_client.get(
            "/api/v1/smart-plugs/by-printer/batch", params={"ids": [first.id, second.id, bare.id]}
        )
        main = await async_client.get(f"/api/v1/smart-plugs/by-printer/{first.id}")
        scripts = await async_client.get(f"/api/v1/smart-plugs/by-printer/{first.id}/scripts")

        assert batch.status_code == 200, batch.text
        body = batch.json()
        assert body[str(first.id)] == {"main": main.json(), "scripts": scripts.json()}
        assert body[str(second.id)]["main"]["name"] == "Other"
        assert body[str(bare.id)] == {"main": None, "scripts": []}
