"""Spoolman-mode mutations refuse what they cannot do safely (upstream c2bc41a5e).

* a storage location is not deleted while Spoolman — the only inventory that
  knows which spools use it — cannot be read;
* labels print for archived Spoolman spools (the Inventory page selects them);
* an archived Spoolman spool is not put on a slot, by assign or by a tag link;
* a slot no printer has is refused at validation, before Spoolman is written.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.location import Location
from backend.app.models.settings import Settings
from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

TAG = "A1B2C3D4E5F60718"


@pytest.fixture
async def spoolman_enabled(db_session: AsyncSession):
    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value="http://localhost:7912"))
    await db_session.commit()


def _spool(spool_id: int, *, archived: bool = False, location: str | None = None) -> dict:
    return {
        "id": spool_id,
        "remaining_weight": 800.0,
        "used_weight": 200.0,
        "registered": "2026-09-01T10:00:00",
        "archived": archived,
        "location": location,
        "filament": {
            "id": 5,
            "name": "PLA Basic",
            "material": "PLA",
            "color_hex": "FF0000",
            "weight": 1000,
            "vendor": {"id": 1, "name": "Bambu Lab"},
        },
        "extra": {},
    }


class FakeSpoolman:
    """Answers as Spoolman does: archived spools only when asked for them."""

    is_connected = True
    base_url = "http://localhost:7912"
    api_url = "http://localhost:7912/api/v1"

    def __init__(self, spools: list[dict], *, down: bool = False):
        self.spools = {s["id"]: s for s in spools}
        self.down = down
        self.writes: list[tuple] = []

    async def health_check(self) -> bool:
        return not self.down

    def _fail_when_down(self):
        if self.down:
            from backend.app.services.spoolman import SpoolmanUnavailableError

            raise SpoolmanUnavailableError("Spoolman is not reachable")

    async def get_spools(self) -> list[dict]:
        self._fail_when_down()
        return [s for s in self.spools.values() if not s.get("archived")]

    async def get_all_spools(self, allow_archived: bool = False) -> list[dict]:
        self._fail_when_down()
        return [s for s in self.spools.values() if allow_archived or not s.get("archived")]

    async def get_spool(self, spool_id: int) -> dict:
        self._fail_when_down()
        if spool_id not in self.spools:
            from backend.app.services.spoolman import SpoolmanNotFoundError

            raise SpoolmanNotFoundError(f"spool {spool_id}")
        return self.spools[spool_id]

    async def merge_spool_extra(self, spool_id: int, extra: dict):
        self.writes.append(("merge_spool_extra", spool_id, extra))

    async def link_native_tags(self, spool_id: int, uids: list, tag_format: str | None = None) -> list[str]:
        # A native tag is a write too: an archived spool must get neither.
        self.writes.append(("link_native_tags", spool_id, list(uids)))
        return []

    async def take_back_native_tags(self, spool_id: int, uids: list) -> None:
        self.writes.append(("take_back_native_tags", spool_id, list(uids)))


async def _slot_rows(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.count()).select_from(SpoolmanSlotAssignment)) or 0)


@pytest.mark.asyncio
@pytest.mark.integration
class TestLocationDeleteNeedsSpoolman:
    async def _location(self, db: AsyncSession) -> Location:
        from backend.app.services.location_service import assign_location_name

        location = Location()
        assign_location_name(location, "Shelf A")
        db.add(location)
        await db.commit()
        await db.refresh(location)
        return location

    async def _delete(self, async_client: AsyncClient, location_id: int, client):
        with patch("backend.app.api.routes.inventory._ensure_spoolman_client", AsyncMock(return_value=client)):
            return await async_client.delete(f"/api/v1/inventory/locations/{location_id}")

    async def test_it_is_refused_while_spoolman_cannot_be_read(
        self, async_client: AsyncClient, db_session: AsyncSession, spoolman_enabled
    ):
        location = await self._location(db_session)

        response = await self._delete(
            async_client, location.id, FakeSpoolman([_spool(1, location="Shelf A")], down=True)
        )

        assert response.status_code == 503
        assert await db_session.scalar(select(func.count()).select_from(Location)) == 1

    async def test_it_is_refused_while_there_is_no_client_at_all(
        self, async_client: AsyncClient, db_session: AsyncSession, spoolman_enabled
    ):
        location = await self._location(db_session)

        response = await self._delete(async_client, location.id, None)

        assert response.status_code == 503
        assert await db_session.scalar(select(func.count()).select_from(Location)) == 1

    async def test_without_an_address_there_is_no_spoolman_to_ask(
        self, async_client: AsyncClient, db_session: AsyncSession
    ):
        """Spoolman switched on with no address yet: there is no server whose
        spools could sit at the location, so the delete is not held for ever
        behind "Spoolman is not reachable" (review slots M-11)."""
        db_session.add(Settings(key="spoolman_enabled", value="true"))
        db_session.add(Settings(key="spoolman_url", value="  "))
        await db_session.commit()
        location = await self._location(db_session)

        response = await async_client.delete(f"/api/v1/inventory/locations/{location.id}")

        assert response.status_code == 200
        assert await db_session.scalar(select(func.count()).select_from(Location)) == 0

    async def test_a_location_spoolman_spools_use_is_still_409(
        self, async_client: AsyncClient, db_session: AsyncSession, spoolman_enabled
    ):
        location = await self._location(db_session)

        response = await self._delete(async_client, location.id, FakeSpoolman([_spool(1, location="Shelf A")]))

        assert response.status_code == 409

    async def test_an_unused_location_is_deleted(
        self, async_client: AsyncClient, db_session: AsyncSession, spoolman_enabled
    ):
        location = await self._location(db_session)

        response = await self._delete(async_client, location.id, FakeSpoolman([_spool(1)]))

        assert response.status_code == 200
        assert await db_session.scalar(select(func.count()).select_from(Location)) == 0

    async def test_the_list_still_answers_while_spoolman_is_down(
        self, async_client: AsyncClient, db_session: AsyncSession, spoolman_enabled
    ):
        await self._location(db_session)
        with patch(
            "backend.app.api.routes.inventory._ensure_spoolman_client",
            AsyncMock(return_value=FakeSpoolman([], down=True)),
        ):
            response = await async_client.get("/api/v1/inventory/locations")

        assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.integration
class TestLabelsForArchivedSpools:
    async def test_a_batch_with_an_archived_spool_renders(self, async_client: AsyncClient, spoolman_enabled):
        client = FakeSpoolman([_spool(1), _spool(2, archived=True)])
        with patch("backend.app.api.routes.labels.get_spoolman_client", AsyncMock(return_value=client)):
            response = await async_client.post(
                "/api/v1/spoolman/labels", json={"spools": [{"id": 1}, {"id": 2}], "template": "box_62x29"}
            )

        assert response.status_code == 200, response.text
        assert response.content.startswith(b"%PDF")


@pytest.mark.asyncio
@pytest.mark.integration
class TestAnArchivedSpoolIsNotPutOnASlot:
    async def test_assign_refuses_it_and_writes_no_row(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession
    ):
        printer = await printer_factory(name="X1C")
        client = FakeSpoolman([_spool(11, archived=True)])
        with (
            patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=client)),
            patch("backend.app.api.routes.spoolman_inventory.printer_manager") as pm,
        ):
            pm.get_client.return_value = None
            response = await async_client.post(
                "/api/v1/spoolman/inventory/slot-assignments",
                json={"spoolman_spool_id": 11, "printer_id": printer.id, "ams_id": 0, "tray_id": 1},
            )

        assert response.status_code == 400
        assert await _slot_rows(db_session) == 0

    async def _link(self, async_client: AsyncClient, client: FakeSpoolman, body: dict):
        with (
            patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
            patch("backend.app.api.routes.spoolman.printer_manager") as pm,
        ):
            pm.get_client.return_value = None
            return await async_client.post("/api/v1/spoolman/spools/11/link", json=body)

    async def test_a_link_to_a_slot_refuses_it_before_the_tag_is_written(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        client = FakeSpoolman([_spool(11, archived=True)])

        response = await self._link(
            async_client, client, {"spool_tag": TAG, "printer_id": printer.id, "ams_id": 0, "tray_id": 1}
        )

        assert response.status_code == 400
        assert client.writes == []
        assert await _slot_rows(db_session) == 0

    async def test_a_live_spool_still_links_to_a_slot(
        self, async_client: AsyncClient, printer_factory, db_session: AsyncSession, spoolman_enabled
    ):
        printer = await printer_factory(name="X1C")
        client = FakeSpoolman([_spool(11)])

        response = await self._link(
            async_client, client, {"spool_tag": TAG, "printer_id": printer.id, "ams_id": 0, "tray_id": 1}
        )

        assert response.status_code == 200
        assert await _slot_rows(db_session) == 1


@pytest.mark.asyncio
@pytest.mark.integration
class TestASlotNoPrinterHasIsRefusedAtValidation:
    @pytest.mark.parametrize(("ams_id", "tray_id"), [(50, 0), (8, 0), (192, 0), (0, 4), (-1, 0)])
    async def test_assign(self, async_client: AsyncClient, printer_factory, ams_id, tray_id):
        printer = await printer_factory(name="X1C")
        get_client = AsyncMock()
        with patch("backend.app.api.routes.spoolman_inventory._get_client", new=get_client):
            response = await async_client.post(
                "/api/v1/spoolman/inventory/slot-assignments",
                json={"spoolman_spool_id": 11, "printer_id": printer.id, "ams_id": ams_id, "tray_id": tray_id},
            )

        assert response.status_code == 422
        get_client.assert_not_awaited()

    @pytest.mark.parametrize(("ams_id", "tray_id"), [(50, 0), (0, 4)])
    async def test_link(self, async_client: AsyncClient, printer_factory, spoolman_enabled, ams_id, tray_id):
        printer = await printer_factory(name="X1C")
        client = FakeSpoolman([_spool(11)])
        with patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)):
            response = await async_client.post(
                "/api/v1/spoolman/spools/11/link",
                json={"spool_tag": TAG, "printer_id": printer.id, "ams_id": ams_id, "tray_id": tray_id},
            )

        assert response.status_code == 422
        assert client.writes == []

    @pytest.mark.parametrize("ams_id", [128, 255])
    async def test_the_slot_read_accepts_every_unit_the_table_does(
        self, async_client: AsyncClient, printer_factory, ams_id
    ):
        printer = await printer_factory(name="X1C")
        with patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=MagicMock())):
            response = await async_client.get(
                "/api/v1/spoolman/inventory/slot-assignments",
                params={"printer_id": printer.id, "ams_id": ams_id, "tray_id": 0},
            )

        assert response.status_code == 200
        assert response.json() is None
