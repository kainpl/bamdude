"""Integration tests for stock-forecasting endpoints (upstream #1184).

Cover the SKU-settings upsert + shopping-list CRUD endpoints introduced in
``B.stock-forecasting``. Forecasting algorithm itself runs client-side and
is exercised in the frontend test suite; here we pin the persistence layer
and permission gates.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
class TestSkuSettingsEndpoints:
    async def test_list_initially_empty(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/inventory/sku-settings")
        assert response.status_code == 200
        assert response.json() == []

    async def test_upsert_creates_new_row(self, async_client: AsyncClient):
        payload = {
            "material": "PLA",
            "subtype": "Basic",
            "brand": "Bambu",
            "lead_time_days": 7,
            "safety_margin_value": 14,
            "safety_margin_unit": "days",
            "alerts_snoozed": False,
        }
        response = await async_client.post("/api/v1/inventory/sku-settings", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["material"] == "PLA"
        assert body["subtype"] == "Basic"
        assert body["brand"] == "Bambu"
        assert body["lead_time_days"] == 7
        assert body["safety_margin_value"] == 14
        assert body["safety_margin_unit"] == "days"
        assert body["alerts_snoozed"] is False

        # Verify it shows up in the list
        list_resp = await async_client.get("/api/v1/inventory/sku-settings")
        assert len(list_resp.json()) == 1

    async def test_upsert_updates_existing_row(self, async_client: AsyncClient):
        """Second POST for the same SKU tuple mutates instead of duplicating."""
        first = {
            "material": "PETG",
            "subtype": None,
            "brand": None,
            "lead_time_days": 5,
            "safety_margin_value": 100,
            "safety_margin_unit": "g",
            "alerts_snoozed": False,
        }
        r1 = await async_client.post("/api/v1/inventory/sku-settings", json=first)
        original_id = r1.json()["id"]

        # Same SKU tuple, new values
        second = {**first, "lead_time_days": 14, "alerts_snoozed": True}
        r2 = await async_client.post("/api/v1/inventory/sku-settings", json=second)
        assert r2.status_code == 200
        body = r2.json()
        assert body["id"] == original_id  # same row
        assert body["lead_time_days"] == 14
        assert body["alerts_snoozed"] is True

        list_resp = await async_client.get("/api/v1/inventory/sku-settings")
        assert len(list_resp.json()) == 1  # not duplicated


@pytest.mark.asyncio
@pytest.mark.integration
class TestShoppingListEndpoints:
    async def test_list_initially_empty(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/inventory/shopping-list")
        assert response.status_code == 200
        assert response.json() == []

    async def test_add_creates_pending_item(self, async_client: AsyncClient):
        payload = {
            "material": "ABS",
            "subtype": "Glow",
            "brand": "eSun",
            "quantity_spools": 3,
            "note": "for project X",
        }
        response = await async_client.post("/api/v1/inventory/shopping-list", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["material"] == "ABS"
        assert body["quantity_spools"] == 3
        assert body["note"] == "for project X"
        assert body["status"] == "pending"
        assert body["purchased_at"] is None
        assert body["added_at"]  # non-empty ISO string

    async def test_status_transitions_set_purchased_at(self, async_client: AsyncClient):
        payload = {
            "material": "TPU",
            "subtype": None,
            "brand": "Sunlu",
            "quantity_spools": 2,
            "note": None,
        }
        item = (await async_client.post("/api/v1/inventory/shopping-list", json=payload)).json()
        item_id = item["id"]

        # pending → purchased stamps purchased_at
        r = await async_client.patch(f"/api/v1/inventory/shopping-list/{item_id}/status", json={"status": "purchased"})
        assert r.status_code == 200
        assert r.json()["status"] == "purchased"
        assert r.json()["purchased_at"] is not None
        stamped_at = r.json()["purchased_at"]

        # purchased → received keeps the original purchased_at
        r = await async_client.patch(f"/api/v1/inventory/shopping-list/{item_id}/status", json={"status": "received"})
        assert r.json()["status"] == "received"
        assert r.json()["purchased_at"] == stamped_at

        # received → pending clears purchased_at again
        r = await async_client.patch(f"/api/v1/inventory/shopping-list/{item_id}/status", json={"status": "pending"})
        assert r.json()["status"] == "pending"
        assert r.json()["purchased_at"] is None

    async def test_status_validation_rejects_unknown(self, async_client: AsyncClient):
        item = (
            await async_client.post(
                "/api/v1/inventory/shopping-list",
                json={"material": "PLA", "subtype": None, "brand": None, "quantity_spools": 1},
            )
        ).json()
        r = await async_client.patch(f"/api/v1/inventory/shopping-list/{item['id']}/status", json={"status": "shipped"})
        assert r.status_code == 400

    async def test_remove_single_item(self, async_client: AsyncClient):
        item = (
            await async_client.post(
                "/api/v1/inventory/shopping-list",
                json={"material": "PLA", "subtype": None, "brand": None, "quantity_spools": 1},
            )
        ).json()
        r = await async_client.delete(f"/api/v1/inventory/shopping-list/{item['id']}")
        assert r.status_code == 200
        assert r.json() == {"status": "deleted"}

        list_resp = await async_client.get("/api/v1/inventory/shopping-list")
        assert list_resp.json() == []

    async def test_clear_all_deletes_everything(self, async_client: AsyncClient):
        for material in ("PLA", "PETG", "ABS"):
            await async_client.post(
                "/api/v1/inventory/shopping-list",
                json={"material": material, "subtype": None, "brand": None, "quantity_spools": 1},
            )

        r = await async_client.delete("/api/v1/inventory/shopping-list")
        assert r.status_code == 200
        assert r.json() == {"deleted": 3}

        list_resp = await async_client.get("/api/v1/inventory/shopping-list")
        assert list_resp.json() == []
