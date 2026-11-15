"""«Last dried» on a spool (upstream 84f320b85).

An AMS drying cycle dries every spool in the unit, but the unit's humidity says
nothing about a spool once it has left. When a cycle that ran long enough ends,
every spool ASSIGNED to a slot of that printer + AMS unit is stamped; the date
can also be set or cleared by hand for a spool dried elsewhere.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
from backend.app.services import spool_drying
from backend.app.services.bambu_mqtt import DryingCycleEnd
from backend.tests.integration.test_spoolman_inventory_translucent_2912 import (  # noqa: F401
    mock_spoolman_client,
    spoolman_settings,
)
from backend.tests.unit.services.test_spoolman_extra_field_registration_2903 import FakeSpoolman, _client

FULL = DryingCycleEnd(ams_id=0, remaining_minutes=1, peak_minutes=480, start_seen=True, target_temp=55, target_hours=8)
SHORT = DryingCycleEnd(
    ams_id=0, remaining_minutes=400, peak_minutes=480, start_seen=True, target_temp=55, target_hours=8
)
OLD = datetime(2026, 1, 2, 3, 4, 5)


async def _spool(db, **fields) -> Spool:
    spool = Spool(**{"material": "PLA", "color_name": "Red", "rgba": "FF0000FF", "label_weight": 1000, **fields})
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _assign(db, spool, printer, ams_id, tray_id=0):
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()


async def _fresh(db, spool) -> Spool:
    return (
        await db.execute(select(Spool).where(Spool.id == spool.id).execution_options(populate_existing=True))
    ).scalar_one()


@pytest.fixture
def broadcast():
    with patch("backend.app.core.websocket.ws_manager.broadcast", AsyncMock()) as mock:
        yield mock


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheStampOnInternalSpools:
    async def test_every_spool_assigned_to_the_unit_is_stamped_and_no_other(
        self, db_session, printer_factory, broadcast
    ):
        printer = await printer_factory()
        other_printer = await printer_factory()
        in_unit_a = await _spool(db_session)
        in_unit_b = await _spool(db_session)
        other_unit = await _spool(db_session)
        elsewhere = await _spool(db_session)
        on_the_shelf = await _spool(db_session)
        await _assign(db_session, in_unit_a, printer, 0, 0)
        await _assign(db_session, in_unit_b, printer, 0, 3)
        await _assign(db_session, other_unit, printer, 1, 0)
        await _assign(db_session, elsewhere, other_printer, 0, 0)

        before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=2)
        assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 2

        for spool in (in_unit_a, in_unit_b):
            row = await _fresh(db_session, spool)
            assert row.last_dried_at is not None and row.last_dried_at.replace(tzinfo=None) >= before
            assert (row.last_dried_temp, row.last_dried_hours) == (55, 8.0)
        for spool in (other_unit, elsewhere, on_the_shelf):
            assert (await _fresh(db_session, spool)).last_dried_at is None
        broadcast.assert_awaited_once_with({"type": "inventory_changed"})

    async def test_a_cycle_too_short_leaves_an_earlier_record_alone(self, db_session, printer_factory, broadcast):
        printer = await printer_factory()
        spool = await _spool(db_session, last_dried_at=OLD, last_dried_temp=65, last_dried_hours=12.0)
        await _assign(db_session, spool, printer, 0)

        assert await spool_drying.record_drying_cycle(db_session, printer.id, SHORT) == 0

        row = await _fresh(db_session, spool)
        assert (row.last_dried_at, row.last_dried_temp, row.last_dried_hours) == (OLD, 65, 12.0)
        broadcast.assert_not_awaited()

    async def test_a_cycle_started_at_the_printer_clears_the_earlier_temperature(
        self, db_session, printer_factory, broadcast
    ):
        printer = await printer_factory()
        spool = await _spool(db_session, last_dried_at=OLD, last_dried_temp=65, last_dried_hours=12.0)
        await _assign(db_session, spool, printer, 0)
        cycle = DryingCycleEnd(ams_id=0, remaining_minutes=0, peak_minutes=200, start_seen=False)

        assert await spool_drying.record_drying_cycle(db_session, printer.id, cycle) == 1

        row = await _fresh(db_session, spool)
        assert row.last_dried_at != OLD
        assert (row.last_dried_temp, row.last_dried_hours) == (None, None)

    async def test_a_unit_holding_no_assigned_spool_announces_nothing(self, db_session, printer_factory, broadcast):
        printer = await printer_factory()

        assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 0
        broadcast.assert_not_awaited()


@pytest.fixture
def retired_session(printer_factory):
    """A callback of a client session whose printer record a deletion has since retired."""
    from contextlib import asynccontextmanager

    from backend.app.services.printer_manager import printer_manager

    @asynccontextmanager
    async def scope():
        printer = await printer_factory()
        printer_id = printer.id  # the refused write rolls back, and the row object expires
        life = printer_manager.printer_life(printer_id)
        printer_manager.begin_printer_deletion(printer_id)
        try:
            with printer_manager.session_scope(printer_id, life):
                yield printer
        finally:
            printer_manager._printer_lives.pop(printer_id, None)
            printer_manager._client_generations.pop(printer_id, None)

    return scope


@pytest.mark.asyncio
@pytest.mark.integration
class TestADeletedPrintersCycleStampsNothing:
    """The slot rows are read by printer id, and SQLite gives a deleted
    printer's id to the next one: the write is proved under the record's hold."""

    async def test_internal_spools_of_a_retired_record_are_left_alone(self, db_session, retired_session, broadcast):
        async with retired_session() as printer:
            spool = await _spool(db_session)
            await _assign(db_session, spool, printer, 0)
            spool_id = spool.id

            assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 0

        assert (await db_session.get(Spool, spool_id, populate_existing=True)).last_dried_at is None
        broadcast.assert_not_awaited()

    async def test_a_printer_whose_row_is_gone_stamps_nothing(self, db_session, printer_factory, broadcast):
        from sqlalchemy import delete

        from backend.app.models.printer import Printer

        printer = await printer_factory()
        spool = await _spool(db_session)
        await _assign(db_session, spool, printer, 0)  # SQLite runs no FK action: the row outlives its printer
        printer_id, spool_id = printer.id, spool.id
        await db_session.execute(delete(Printer).where(Printer.id == printer_id))
        await db_session.commit()

        assert await spool_drying.record_drying_cycle(db_session, printer_id, FULL) == 0

        assert (await db_session.get(Spool, spool_id, populate_existing=True)).last_dried_at is None

    async def test_spoolman_spools_of_a_retired_record_are_left_alone(self, db_session, retired_session, broadcast):
        db_session.add(Settings(key="spoolman_enabled", value="true"))
        await db_session.commit()
        fake = FakeSpoolman()
        fake.spools[11] = {"id": 11, "extra": {}}
        async with retired_session() as printer:
            await _spoolman_slots(db_session, printer, {(0, 0): 11})
            with patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=_client(fake))):
                assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 0

        assert "bambu_last_dried_at" not in fake.spools[11]["extra"]


@pytest.fixture
async def spoolman_mode(db_session):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    await db_session.commit()


async def _spoolman_slots(db, printer, slots: dict[tuple[int, int], int]):
    for (ams_id, tray_id), spoolman_id in slots.items():
        db.add(
            SpoolmanSlotAssignment(printer_id=printer.id, ams_id=ams_id, tray_id=tray_id, spoolman_spool_id=spoolman_id)
        )
    await db.commit()


def _extra(fake: FakeSpoolman, spool_id: int, key: str):
    raw = (fake.spools[spool_id].get("extra") or {}).get(key)
    return None if raw is None else json.loads(raw)


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheStampInSpoolmanMode:
    async def test_a_spoolman_that_never_saw_the_keys_gets_them_registered_and_written(
        self, db_session, printer_factory, spoolman_mode, broadcast
    ):
        """Spoolman answers 400 to a write carrying an extra key it was never
        told about; the write registers its own keys first."""
        printer = await printer_factory()
        internal = await _spool(db_session)
        await _assign(db_session, internal, printer, 0)
        await _spoolman_slots(db_session, printer, {(0, 0): 11, (0, 1): 12, (1, 0): 13})
        fake = FakeSpoolman()
        for spool_id in (11, 12, 13):
            fake.spools[spool_id] = {"id": spool_id, "extra": {"tag": '"AA"'}}
        fake.registered.add("tag")

        with patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=_client(fake))):
            assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 2

        assert {"bambu_last_dried_at", "bambu_last_dried_temp", "bambu_last_dried_hours"} <= fake.registered
        for spool_id in (11, 12):
            stamped = datetime.fromisoformat(_extra(fake, spool_id, "bambu_last_dried_at"))
            assert abs((datetime.now(timezone.utc).replace(tzinfo=None) - stamped).total_seconds()) < 60
            assert _extra(fake, spool_id, "bambu_last_dried_temp") == "55"
            assert _extra(fake, spool_id, "bambu_last_dried_hours") == "8.0"
            assert _extra(fake, spool_id, "tag") == "AA"
        assert "bambu_last_dried_at" not in fake.spools[13]["extra"]
        # Spoolman owns the slots: the internal table is not the inventory.
        assert (await _fresh(db_session, internal)).last_dried_at is None
        broadcast.assert_awaited_once_with({"type": "inventory_changed"})

    async def test_a_spoolman_that_refuses_the_registration_stamps_nothing_and_raises_nothing(
        self, db_session, printer_factory, spoolman_mode, broadcast, caplog
    ):
        """Registration is best-effort: when it fails Spoolman refuses each
        write, every spool is named in the log and the callback survives."""
        printer = await printer_factory()
        await _spoolman_slots(db_session, printer, {(0, 0): 11, (0, 1): 12})
        fake = FakeSpoolman(field_status=500)
        for spool_id in (11, 12):
            fake.spools[spool_id] = {"id": spool_id, "extra": {}}

        with (
            patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=_client(fake))),
            caplog.at_level("WARNING", logger="backend.app.services.spool_drying"),
        ):
            assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 0

        assert all("bambu_last_dried_at" not in fake.spools[i]["extra"] for i in (11, 12))
        logged = [r.getMessage() for r in caplog.records if r.name == "backend.app.services.spool_drying"]
        assert any("spool 11" in line for line in logged) and any("spool 12" in line for line in logged)
        broadcast.assert_not_awaited()

    async def test_an_unreachable_spool_does_not_skip_the_rest(
        self, db_session, printer_factory, spoolman_mode, broadcast
    ):
        printer = await printer_factory()
        await _spoolman_slots(db_session, printer, {(0, 0): 11, (0, 1): 12})
        fake = FakeSpoolman()
        fake.spools[12] = {"id": 12, "extra": {}}  # 11 was deleted in Spoolman

        with patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=_client(fake))):
            assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 1

        assert _extra(fake, 12, "bambu_last_dried_temp") == "55"

    async def test_no_spoolman_client_stamps_nothing(self, db_session, printer_factory, spoolman_mode, broadcast):
        printer = await printer_factory()
        await _spoolman_slots(db_session, printer, {(0, 0): 11})

        with patch("backend.app.services.spoolman.get_spoolman_client", AsyncMock(return_value=None)):
            assert await spool_drying.record_drying_cycle(db_session, printer.id, FULL) == 0

    async def test_the_extras_read_back_as_the_three_fields(self):
        from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool

        mapped = _map_spoolman_spool(
            {
                "id": 1,
                "filament": {"id": 2, "material": "PLA", "weight": 1000},
                "extra": {
                    "bambu_last_dried_at": '"2026-10-06T11:05:01"',
                    "bambu_last_dried_temp": '"55"',
                    "bambu_last_dried_hours": '"7.5"',
                },
            }
        )
        assert (mapped["last_dried_at"], mapped["last_dried_temp"], mapped["last_dried_hours"]) == (
            "2026-10-06T11:05:01",
            55,
            7.5,
        )

        cleared = _map_spoolman_spool(
            {
                "id": 1,
                "filament": {"id": 2, "material": "PLA", "weight": 1000},
                "extra": {"bambu_last_dried_at": '""', "bambu_last_dried_temp": '""', "bambu_last_dried_hours": '""'},
            }
        )
        assert (cleared["last_dried_at"], cleared["last_dried_temp"], cleared["last_dried_hours"]) == (None, None, None)

        never = _map_spoolman_spool({"id": 1, "filament": {"id": 2, "material": "PLA", "weight": 1000}})
        assert (never["last_dried_at"], never["last_dried_temp"], never["last_dried_hours"]) == (None, None, None)

    async def test_a_hand_set_date_goes_to_the_extras_and_drops_temperature_and_hours(
        self,
        async_client,
        spoolman_settings,  # noqa: F811
        mock_spoolman_client,  # noqa: F811
    ):
        response = await async_client.patch(
            "/api/v1/spoolman/inventory/spools/42", json={"last_dried_at": "2026-10-06T14:05:01+03:00"}
        )

        assert response.status_code == 200, response.text
        spool_id, extra = mock_spoolman_client.merge_spool_extra.call_args.args
        assert spool_id == 42
        assert extra == {
            "bambu_last_dried_at": '"2026-10-06T11:05:01"',
            "bambu_last_dried_temp": '""',
            "bambu_last_dried_hours": '""',
        }

    async def test_a_null_clears_it_and_an_unrelated_save_does_not_touch_it(
        self,
        async_client,
        spoolman_settings,  # noqa: F811
        mock_spoolman_client,  # noqa: F811
    ):
        response = await async_client.patch("/api/v1/spoolman/inventory/spools/42", json={"last_dried_at": None})
        assert response.status_code == 200, response.text
        assert mock_spoolman_client.merge_spool_extra.call_args.args[1]["bambu_last_dried_at"] == '""'

        mock_spoolman_client.merge_spool_extra.reset_mock()
        response = await async_client.patch("/api/v1/spoolman/inventory/spools/42", json={"color_name": "Red"})
        assert response.status_code == 200, response.text
        assert "bambu_last_dried_at" not in mock_spoolman_client.merge_spool_extra.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.integration
class TestByHand:
    async def test_patch_sets_the_date_in_utc_and_drops_temperature_and_hours(self, async_client, db_session):
        spool = await _spool(db_session, last_dried_at=OLD, last_dried_temp=65, last_dried_hours=12.0)

        response = await async_client.patch(
            f"/api/v1/inventory/spools/{spool.id}", json={"last_dried_at": "2026-10-06T14:05:01+03:00"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["last_dried_at"].startswith("2026-10-06T11:05:01")
        assert (body["last_dried_temp"], body["last_dried_hours"]) == (None, None)
        row = await _fresh(db_session, spool)
        assert row.last_dried_at.replace(tzinfo=None) == datetime(2026, 10, 6, 11, 5, 1)

    async def test_patch_with_null_clears_all_three(self, async_client, db_session):
        spool = await _spool(db_session, last_dried_at=OLD, last_dried_temp=65, last_dried_hours=12.0)

        response = await async_client.patch(f"/api/v1/inventory/spools/{spool.id}", json={"last_dried_at": None})

        assert response.status_code == 200, response.text
        row = await _fresh(db_session, spool)
        assert (row.last_dried_at, row.last_dried_temp, row.last_dried_hours) == (None, None, None)

    async def test_an_unrelated_patch_keeps_the_record_and_the_response_carries_it(self, async_client, db_session):
        spool = await _spool(db_session, last_dried_at=OLD, last_dried_temp=65, last_dried_hours=12.0)

        response = await async_client.patch(f"/api/v1/inventory/spools/{spool.id}", json={"note": "x"})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["last_dried_at"].startswith("2026-01-02T03:04:05")
        assert (body["last_dried_temp"], body["last_dried_hours"]) == (65, 12.0)

    async def test_bulk_update_sets_and_clears(self, async_client, db_session):
        a = await _spool(db_session, last_dried_at=OLD, last_dried_temp=65, last_dried_hours=12.0)
        b = await _spool(db_session)

        response = await async_client.patch(
            "/api/v1/inventory/spools/bulk-update",
            json={"spool_ids": [a.id, b.id], "fields": {"last_dried_at": "2026-10-06T11:05:01Z"}},
        )
        assert response.status_code == 200, response.text
        for spool in (a, b):
            row = await _fresh(db_session, spool)
            assert row.last_dried_at.replace(tzinfo=None) == datetime(2026, 10, 6, 11, 5, 1)
            assert (row.last_dried_temp, row.last_dried_hours) == (None, None)

        response = await async_client.patch(
            "/api/v1/inventory/spools/bulk-update",
            json={"spool_ids": [a.id, b.id], "fields": {"last_dried_at": None}},
        )
        assert response.status_code == 200, response.text
        for spool in (a, b):
            assert (await _fresh(db_session, spool)).last_dried_at is None

    async def test_a_new_spool_cannot_be_created_dried(self, async_client, db_session):
        response = await async_client.post(
            "/api/v1/inventory/spools", json={"material": "PLA", "last_dried_at": "2026-10-06T11:05:01Z"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["last_dried_at"] is None


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheList:
    async def _three(self, db_session):
        never = await _spool(db_session)
        old = await _spool(db_session, last_dried_at=datetime(2026, 1, 1), last_dried_temp=55, last_dried_hours=8.0)
        new = await _spool(db_session, last_dried_at=datetime(2026, 6, 1))
        return never, old, new

    async def test_the_paged_row_carries_the_three_fields(self, async_client, db_session):
        _, old, _ = await self._three(db_session)

        response = await async_client.get("/api/v1/inventory/spools?archived=active&page=1&per_page=50")

        assert response.status_code == 200, response.text
        row = next(item for item in response.json()["items"] if item["id"] == old.id)
        assert row["last_dried_at"].startswith("2026-01-01")
        assert (row["last_dried_temp"], row["last_dried_hours"]) == (55, 8.0)

    async def test_sorting_puts_a_never_dried_spool_first_ascending_and_last_descending(self, async_client, db_session):
        never, old, new = await self._three(db_session)
        url = "/api/v1/inventory/spools?archived=active&page=1&per_page=50&sort_by="

        asc = await async_client.get(url + "last_dried_asc")
        desc = await async_client.get(url + "last_dried_desc")

        assert asc.status_code == 200, asc.text
        assert [row["id"] for row in asc.json()["items"]] == [never.id, old.id, new.id]
        assert [row["id"] for row in desc.json()["items"]] == [new.id, old.id, never.id]

    async def test_the_operators_order_takes_a_last_dried_level(self, async_client, db_session):
        never, old, new = await self._three(db_session)
        # Ascending on purpose: the list's own order (newest id first) is the reverse.
        db_session.add(Settings(key="spool_sort_order", value="last_dried_asc"))
        await db_session.commit()

        response = await async_client.get("/api/v1/inventory/spools?archived=active&page=1&per_page=50")

        assert [row["id"] for row in response.json()["items"]] == [never.id, old.id, new.id]

    async def test_grouping_similar_spools_ignores_the_date(self, async_client, db_session):
        """The 2026-09-07 lot ruling: a value unique per spool in the key makes
        grouping do nothing."""
        await _spool(db_session, last_dried_at=datetime(2026, 1, 1))
        await _spool(db_session, last_dried_at=datetime(2026, 6, 1))
        await _spool(db_session)

        response = await async_client.get(
            "/api/v1/inventory/spools?archived=active&page=1&per_page=50&group_similar=true"
        )

        assert response.status_code == 200, response.text
        assert response.json()["meta"]["total"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
class TestCsv:
    async def test_export_writes_the_date_and_import_reads_it_back(self, async_client, db_session):
        await _spool(db_session, last_dried_at=datetime(2026, 6, 1, 12, 30))

        exported = await async_client.get("/api/v1/inventory/spools/export")
        assert exported.status_code == 200, exported.text
        header, row = exported.text.strip().splitlines()
        assert "last_dried_at" in header.split(",")
        assert row.split(",")[header.split(",").index("last_dried_at")] == "2026-06-01T12:30:00"

        imported = await async_client.post(
            "/api/v1/inventory/spools/import",
            files={"file": ("inventory.csv", exported.text.encode("utf-8"), "text/csv")},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["created"] == 1
        dates = (await db_session.execute(select(Spool.last_dried_at))).scalars().all()
        assert [d.replace(tzinfo=None) for d in dates] == [datetime(2026, 6, 1, 12, 30)] * 2

    async def test_import_rejects_a_date_it_cannot_read(self, async_client):
        csv_text = "material,color_name,rgba,last_dried_at\nPLA,White,ffffffff,yesterday\n"

        response = await async_client.post(
            "/api/v1/inventory/spools/import?dry_run=true",
            files={"file": ("inventory.csv", csv_text.encode("utf-8"), "text/csv")},
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["error_count"] == 1
        assert "last_dried_at" in data["rows"][0]["reason"]
