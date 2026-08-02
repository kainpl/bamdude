"""One place, one row, one name — enforced where it can be enforced."""

import pytest
from httpx import AsyncClient


async def _make(client: AsyncClient, name: str):
    return await client.post("/api/v1/printer-locations", json={"name": name})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_location_can_be_created_and_listed(async_client: AsyncClient):
    assert (await _make(async_client, "Shop 2")).status_code == 201

    listed = (await async_client.get("/api/v1/printer-locations")).json()["locations"]

    assert [loc["name"] for loc in listed] == ["Shop 2"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_surrounding_space_is_trimmed_on_the_way_in(async_client: AsyncClient):
    created = (await _make(async_client, "  Shop 2  ")).json()

    assert created["name"] == "Shop 2"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_differently_cased_duplicate_is_refused(async_client: AsyncClient):
    """Otherwise the entity carries the very problem it exists to remove."""
    await _make(async_client, "Цех 2")

    assert (await _make(async_client, "цех 2")).status_code == 409


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_empty_name_is_refused(async_client: AsyncClient):
    assert (await _make(async_client, "   ")).status_code == 422


class TestRenaming:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_rename_changes_the_name(self, async_client: AsyncClient):
        created = (await _make(async_client, "Shop 2")).json()

        renamed = await async_client.patch(f"/api/v1/printer-locations/{created['id']}", json={"name": "Workshop"})

        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Workshop"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_rename_rewrites_the_lookup_key(self, async_client: AsyncClient):
        """Silently broken otherwise: the new name is unique while the key still
        matches the old one, so the next differently-cased duplicate slips in."""
        created = (await _make(async_client, "Shop 2")).json()
        await async_client.patch(f"/api/v1/printer-locations/{created['id']}", json={"name": "Workshop"})

        assert (await _make(async_client, "workshop")).status_code == 409
        assert (await _make(async_client, "shop 2")).status_code == 201

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_renaming_onto_an_existing_name_is_refused(self, async_client: AsyncClient):
        await _make(async_client, "Shop 1")
        second = (await _make(async_client, "Shop 2")).json()

        clash = await async_client.patch(f"/api/v1/printer-locations/{second['id']}", json={"name": "shop 1"})

        assert clash.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_renaming_a_place_to_its_own_name_is_fine(self, async_client: AsyncClient):
        """Otherwise fixing only the capitalisation would be refused as a clash
        with itself."""
        created = (await _make(async_client, "shop 2")).json()

        renamed = await async_client.patch(f"/api/v1/printer-locations/{created['id']}", json={"name": "Shop 2"})

        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Shop 2"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_renaming_one_that_does_not_exist_is_a_404(self, async_client: AsyncClient):
        assert (await async_client.patch("/api/v1/printer-locations/999", json={"name": "x"})).status_code == 404


class TestDeleting:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_unused_location_is_deleted(self, async_client: AsyncClient):
        created = (await _make(async_client, "Shop 2")).json()

        assert (await async_client.delete(f"/api/v1/printer-locations/{created['id']}")).status_code == 200
        assert (await async_client.get("/api/v1/printer-locations")).json()["locations"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_one_in_use_by_a_printer_is_refused_and_says_so(self, async_client: AsyncClient, db_session):
        from backend.app.models.printer import Printer

        created = (await _make(async_client, "Shop 2")).json()
        db_session.add(
            Printer(name="p1", ip_address="1.2.3.4", serial_number="S1", access_code="1", location_id=created["id"])
        )
        await db_session.commit()

        refused = await async_client.delete(f"/api/v1/printer-locations/{created['id']}")

        assert refused.status_code == 409
        assert "1 printer" in refused.json()["detail"], "the message names how many hold it"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_one_targeted_by_queued_work_is_refused(self, async_client: AsyncClient, db_session):
        """The reason this is blocked rather than nulled: an item waiting for a
        specific place would otherwise start going anywhere."""
        from backend.app.models.auto_queue import AutoQueueItem

        created = (await _make(async_client, "Shop 2")).json()
        db_session.add(AutoQueueItem(target_location_id=created["id"]))
        await db_session.commit()

        assert (await async_client.delete(f"/api/v1/printer-locations/{created['id']}")).status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleting_one_that_does_not_exist_is_a_404(self, async_client: AsyncClient):
        assert (await async_client.delete("/api/v1/printer-locations/999")).status_code == 404


class TestUsageCounts:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_list_says_how_many_hold_each_place(self, async_client: AsyncClient, db_session):
        from backend.app.models.printer import Printer

        created = (await _make(async_client, "Shop 2")).json()
        db_session.add(
            Printer(name="p1", ip_address="1.2.3.4", serial_number="S1", access_code="1", location_id=created["id"])
        )
        await db_session.commit()

        listed = (await async_client.get("/api/v1/printer-locations")).json()["locations"][0]

        assert listed["printer_count"] == 1
        assert listed["sensor_count"] == 0
        assert listed["queued_count"] == 0
