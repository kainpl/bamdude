"""A Spoolman spool keeps its own empty weight when the form names one
(upstream b6521e93f, G05 Р35).

The read side has always resolved spool → filament → vendor → 250 g; the spool
create / update routes accepted ``core_weight`` and dropped it, so the only way
to give one spool another tare was Spoolman's own UI.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.tests.unit.services.spoolman_weighing_fake import BASE_URL, WeighingSpoolman, client_for

API = "/api/v1/spoolman/inventory"


@pytest.fixture
async def spoolman(db_session):
    from backend.app.api.routes import spoolman_inventory
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="spoolman_enabled", value="true"))
    db_session.add(Settings(key="spoolman_url", value=BASE_URL))
    await db_session.commit()

    fake = WeighingSpoolman()
    fake.add_vendor(3, "Bambu Lab")
    fake.add_filament(7, vendor_id=3, name="PLA Basic", material="PLA", color_hex="FF0000", weight=1000)
    client = client_for(fake)
    spoolman_inventory._health_check_cache.clear()
    with (
        patch("backend.app.api.routes.spoolman_inventory.get_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman_inventory.init_spoolman_client", AsyncMock(return_value=client)),
        patch("backend.app.api.routes.spoolman.get_spoolman_client", AsyncMock(return_value=client)),
    ):
        yield fake
    spoolman_inventory._health_check_cache.clear()


@pytest.mark.asyncio
@pytest.mark.integration
class TestOwnTare:
    async def test_create_writes_the_tare_the_caller_named(self, async_client: AsyncClient, spoolman):
        response = await async_client.post(f"{API}/spools", json={"spoolman_filament_id": 7, "core_weight": 180})

        assert response.status_code == 200
        assert spoolman.bodies("POST", "/spool")[0]["spool_weight"] == 180.0
        assert response.json()["core_weight"] == 180
        assert response.json()["core_weight_is_inherited"] is False

    async def test_create_without_a_tare_leaves_it_to_spoolman(self, async_client: AsyncClient, spoolman):
        response = await async_client.post(f"{API}/spools", json={"spoolman_filament_id": 7})

        assert response.status_code == 200
        assert "spool_weight" not in spoolman.bodies("POST", "/spool")[0]
        assert response.json()["core_weight"] == 250
        assert response.json()["core_weight_is_inherited"] is True

    async def test_bulk_create_writes_the_tare_too(self, async_client: AsyncClient, spoolman):
        payload = {"spool": {"spoolman_filament_id": 7, "core_weight": 180}, "quantity": 2}

        response = await async_client.post(f"{API}/spools/bulk", json=payload)

        assert response.status_code == 200
        assert [body["spool_weight"] for body in spoolman.bodies("POST", "/spool")] == [180.0, 180.0]

    async def test_update_writes_a_named_tare(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=100.0)

        response = await async_client.patch(f"{API}/spools/42", json={"core_weight": 212})

        assert response.status_code == 200
        assert spoolman.view(42)["spool_weight"] == 212.0
        assert response.json()["core_weight"] == 212
        assert spoolman.view(42)["remaining_weight"] == 900.0  # the filament on it did not move

    async def test_update_that_names_no_tare_leaves_it_alone(self, async_client: AsyncClient, spoolman):
        spoolman.add_spool(42, 7, used_weight=100.0, spool_weight=199.0)

        response = await async_client.patch(f"{API}/spools/42", json={"note": "top shelf"})

        assert response.status_code == 200
        assert all("spool_weight" not in body for body in spoolman.bodies("PATCH", "/spool/42"))
        assert response.json()["core_weight"] == 199

    async def test_a_zero_gram_tare_round_trips(self, async_client: AsyncClient, spoolman):
        """0 g is a real tare (a refill on a reused core weighed without it)."""
        created = await async_client.post(f"{API}/spools", json={"spoolman_filament_id": 7, "core_weight": 0})
        spool_id = created.json()["id"]
        assert created.json()["core_weight"] == 0

        await async_client.patch(f"{API}/spools/{spool_id}", json={"core_weight": 0, "note": "x"})

        assert spoolman.view(spool_id)["spool_weight"] == 0.0
        fetched = await async_client.get(f"{API}/spools/{spool_id}")
        assert fetched.json()["core_weight"] == 0
        assert fetched.json()["core_weight_is_inherited"] is False
