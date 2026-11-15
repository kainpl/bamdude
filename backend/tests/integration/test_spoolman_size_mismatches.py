"""Spools whose own size differs from their filament's — shown, never rewritten.

Spoolman stamps a spool's ``initial_weight`` when it is created and never moves
it with the filament (measured on 0.27.1). The old BamDude form wrote a changed
label weight to ``filament.weight``, and every reader took that; the readers now
take the spool's own size, as Spoolman's ``remaining_weight`` does. So a spool
created at 1000 g and corrected to 250 g in the old form reads 1000 g again
(review accounting I2). Stored values are not rewritten for the operator: the
list names such spools and one action, per spool, sets that spool's size.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.tests.unit.services.spoolman_weighing_fake import BASE_URL, WeighingSpoolman, client_for

INVENTORY = "/api/v1/spoolman/inventory"


@pytest.fixture
async def spoolman(db_session):
    from backend.app.api.routes import spoolman_inventory
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value=BASE_URL))
    await db_session.commit()

    fake = WeighingSpoolman()
    fake.add_vendor(3, "Sunlu")
    fake.add_filament(7, vendor_id=3, name="Matte", material="PLA", color_hex="FF0000", weight=250)
    fake.add_filament(8, vendor_id=3, name="Basic", material="PETG", color_hex="00FF00", weight=1000)
    client = client_for(fake)
    spoolman_inventory._health_check_cache.clear()
    with (
        patch("backend.app.api.routes.spoolman_inventory.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman_inventory.init_spoolman_client", AsyncMock(return_value=client)),
    ):
        yield fake


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheList:
    async def test_a_spool_resized_through_the_old_form_is_listed_with_both_sizes(
        self, async_client: AsyncClient, spoolman
    ):
        # Created at 1000 g; the old form then wrote 250 g to the filament.
        spoolman.add_spool(41, 7, initial_weight=1000, used_weight=100)
        spoolman.add_spool(42, 8)  # its own size is its filament's: fine

        response = await async_client.get(f"{INVENTORY}/spools/size-mismatches")

        assert response.status_code == 200
        rows = response.json()
        assert [row["id"] for row in rows] == [41]
        row = rows[0]
        assert (row["initial_weight"], row["filament_weight"], row["remaining_weight"]) == (1000, 250, 900)
        # The fields the naming template is composed from, as the spool list gives them.
        assert (row["spool"]["brand"], row["spool"]["material"], row["spool"]["subtype"]) == ("Sunlu", "PLA", "Matte")

    async def test_spools_without_one_of_the_sizes_and_archived_ones_are_not_listed(
        self, async_client: AsyncClient, spoolman
    ):
        spoolman.add_filament(9, vendor_id=3, name="No size", material="PLA", color_hex="0000FF")
        spoolman.add_spool(43, 9, initial_weight=750)  # the filament states no weight
        spoolman.add_spool(44, 7, initial_weight=None)  # an older Spoolman: no size of its own
        spoolman.add_spool(45, 7, initial_weight=1000, archived=True)

        response = await async_client.get(f"{INVENTORY}/spools/size-mismatches")

        assert response.json() == []

    async def test_reading_the_list_writes_nothing(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, initial_weight=1000)

        await async_client.get(f"{INVENTORY}/spools/size-mismatches")

        assert [m for m, _p, _b in spoolman.calls if m != "GET"] == []
        assert spoolman.spools[41]["initial_weight"] == 1000


@pytest.mark.asyncio
@pytest.mark.integration
class TestUseTheFilamentsSizeForOneSpool:
    async def test_it_sets_that_spools_size_and_nothing_else(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(41, 7, initial_weight=1000, used_weight=100)
        spoolman.add_spool(46, 7, initial_weight=1000, used_weight=0)

        response = await async_client.post(f"{INVENTORY}/spools/41/use-filament-size")

        assert response.status_code == 200
        assert spoolman.bodies("PATCH", "/spool/41") == [{"initial_weight": 250}]
        assert spoolman.spools[41]["used_weight"] == 100, "what was consumed is not touched"
        body = response.json()
        assert (body["initial_weight"], body["remaining_weight"]) == (250, 150)
        assert spoolman.spools[46]["initial_weight"] == 1000, "one spool at a time"
        rows = (await async_client.get(f"{INVENTORY}/spools/size-mismatches")).json()
        assert [row["id"] for row in rows] == [46]

    async def test_a_spool_whose_filament_states_no_size_is_refused(self, async_client: AsyncClient, spoolman):
        spoolman.add_filament(9, vendor_id=3, name="No size", material="PLA", color_hex="0000FF")
        spoolman.add_spool(43, 9, initial_weight=750)

        response = await async_client.post(f"{INVENTORY}/spools/43/use-filament-size")

        assert response.status_code == 409
        assert spoolman.bodies("PATCH", "/spool/43") == []

    async def test_a_spool_that_does_not_exist_is_404(self, async_client: AsyncClient, spoolman):
        response = await async_client.post(f"{INVENTORY}/spools/999/use-filament-size")

        assert response.status_code == 404
