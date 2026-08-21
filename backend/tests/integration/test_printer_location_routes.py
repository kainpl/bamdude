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


class TestTheTree:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_child_is_created_under_its_parent(self, async_client: AsyncClient):
        parent = (await _make(async_client, "Workshop")).json()

        child = await async_client.post(
            "/api/v1/printer-locations", json={"name": "Shelf 1", "parent_id": parent["id"]}
        )

        assert child.status_code == 201
        assert child.json()["path"] == "Workshop / Shelf 1"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_same_name_is_free_under_a_different_parent(self, async_client: AsyncClient):
        one = (await _make(async_client, "Workshop")).json()
        two = (await _make(async_client, "Hall")).json()

        first = await async_client.post("/api/v1/printer-locations", json={"name": "Shelf 1", "parent_id": one["id"]})
        second = await async_client.post("/api/v1/printer-locations", json={"name": "Shelf 1", "parent_id": two["id"]})

        assert (first.status_code, second.status_code) == (201, 201)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_same_name_is_taken_under_the_same_parent(self, async_client: AsyncClient):
        parent = (await _make(async_client, "Workshop")).json()
        await async_client.post("/api/v1/printer-locations", json={"name": "Shelf 1", "parent_id": parent["id"]})

        again = await async_client.post(
            "/api/v1/printer-locations", json={"name": "shelf 1", "parent_id": parent["id"]}
        )

        assert again.status_code == 409, "case-insensitively, which is what name_key is for"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_two_roots_may_not_share_a_name_either(self, async_client: AsyncClient):
        """The composite index cannot enforce this on SQLite -- NULL != NULL --
        so the route's own check is the only thing standing here."""
        await _make(async_client, "Workshop")

        again = await _make(async_client, "workshop")

        assert again.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_location_cannot_be_moved_under_itself(self, async_client: AsyncClient):
        parent = (await _make(async_client, "Workshop")).json()
        child = (
            await async_client.post("/api/v1/printer-locations", json={"name": "Shelf", "parent_id": parent["id"]})
        ).json()

        moved = await async_client.patch(f"/api/v1/printer-locations/{parent['id']}", json={"parent_id": child["id"]})

        assert moved.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_fourth_level_is_refused(self, async_client: AsyncClient):
        parent_id = None
        deepest = None
        for name in ("Workshop", "Shelf", "Box"):
            body = (
                await async_client.post("/api/v1/printer-locations", json={"name": name, "parent_id": parent_id})
            ).json()
            parent_id = body["id"]
            deepest = body

        too_deep = await async_client.post(
            "/api/v1/printer-locations", json={"name": "Corner", "parent_id": deepest["id"]}
        )

        assert too_deep.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_parent_with_children_is_not_deleted(self, async_client: AsyncClient):
        parent = (await _make(async_client, "Workshop")).json()
        await async_client.post("/api/v1/printer-locations", json={"name": "Shelf", "parent_id": parent["id"]})

        refused = await async_client.delete(f"/api/v1/printer-locations/{parent['id']}")

        assert refused.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_list_is_ordered_by_path_so_a_parent_leads_its_children(self, async_client: AsyncClient):
        # Which is also where "the parent's own group comes first" comes from.
        workshop = (await _make(async_client, "Workshop")).json()
        await async_client.post("/api/v1/printer-locations", json={"name": "Shelf", "parent_id": workshop["id"]})
        await _make(async_client, "Hall")

        listed = (await async_client.get("/api/v1/printer-locations")).json()["locations"]

        assert [row["path"] for row in listed] == ["Hall", "Workshop", "Workshop / Shelf"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_renaming_a_parent_moves_its_children_with_it(self, async_client: AsyncClient):
        """No path is stored, so this is free -- and the test says so, because a
        cached path is exactly the optimisation somebody adds later."""
        workshop = (await _make(async_client, "Workshop")).json()
        await async_client.post("/api/v1/printer-locations", json={"name": "Shelf", "parent_id": workshop["id"]})

        await async_client.patch(f"/api/v1/printer-locations/{workshop['id']}", json={"name": "Big workshop"})

        listed = (await async_client.get("/api/v1/printer-locations")).json()["locations"]
        assert "Big workshop / Shelf" in [row["path"] for row in listed]


@pytest.mark.integration
class TestAnArchivedPrinterIsCountedInOnePlaceAndNotTheOther:
    """⚠️ The two callers of ``_holders`` ask different questions, and archived
    printers are where they part.

    An archived printer is retired and hidden everywhere else, so counting it in
    the row's own summary tells the operator a place holds machines they cannot
    see. But the delete guard MUST still count it: ``delete_location`` removes
    the row without nulling anything that pointed at it, so a location deleted
    out from under an archived printer leaves that printer pointing at nothing.
    """

    @staticmethod
    async def _place_with_an_archived_printer(async_client, db_session):
        from datetime import datetime, timezone

        from backend.app.models.printer import Printer

        created = (await _make(async_client, "Shop 2")).json()
        db_session.add(
            Printer(
                name="retired",
                ip_address="1.2.3.4",
                serial_number="S-ARCH",
                access_code="1",
                location_id=created["id"],
                archived=True,
                archived_at=datetime.now(timezone.utc),
            )
        )
        await db_session.commit()
        return created

    @pytest.mark.asyncio
    async def test_the_row_does_not_count_it(self, async_client: AsyncClient, db_session):
        created = await self._place_with_an_archived_printer(async_client, db_session)

        listed = (await async_client.get("/api/v1/printer-locations")).json()["locations"]
        row = next(loc for loc in listed if loc["id"] == created["id"])

        assert row["printer_count"] == 0

    @pytest.mark.asyncio
    async def test_an_active_printer_beside_it_still_counts(self, async_client: AsyncClient, db_session):
        """The filter must exclude the archived one, not every printer."""
        from backend.app.models.printer import Printer

        created = await self._place_with_an_archived_printer(async_client, db_session)
        db_session.add(
            Printer(
                name="working",
                ip_address="1.2.3.5",
                serial_number="S-LIVE",
                access_code="1",
                location_id=created["id"],
            )
        )
        await db_session.commit()

        listed = (await async_client.get("/api/v1/printer-locations")).json()["locations"]
        row = next(loc for loc in listed if loc["id"] == created["id"])

        assert row["printer_count"] == 1

    @pytest.mark.asyncio
    async def test_deleting_the_place_is_still_refused(self, async_client: AsyncClient, db_session):
        created = await self._place_with_an_archived_printer(async_client, db_session)

        refused = await async_client.delete(f"/api/v1/printer-locations/{created['id']}")

        assert refused.status_code == 409, "an archived printer still points at this row"
