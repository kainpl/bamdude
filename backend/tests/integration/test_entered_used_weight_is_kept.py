"""A used weight entered for a NEW spool is kept, and it cannot be negative.

Only an EDIT locked the weight against the AMS remain% sync, so a part-used
spool added with "used 300 g" was overwritten by the first tray report: the
tray said 80 % and the entered 300 g became 200 g (upstream f7bd06641). The
three create doors — the form, "add copies" and the CSV import — lock it when a
used weight is entered and the client did not say otherwise.
"""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool

BASE = {"material": "PLA", "brand": "Generic", "label_weight": 1000}


@pytest.mark.asyncio
@pytest.mark.integration
class TestAnEnteredUsedWeightIsLocked:
    async def test_create(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/inventory/spools", json={**BASE, "weight_used": 300})

        assert response.status_code == 200, response.text
        assert response.json()["weight_used"] == 300
        assert response.json()["weight_locked"] is True

    async def test_create_without_a_used_weight_stays_unlocked(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/inventory/spools", json=BASE)

        assert response.json()["weight_locked"] is False

    async def test_an_explicit_choice_wins(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/inventory/spools", json={**BASE, "weight_used": 300, "weight_locked": False}
        )

        assert response.json()["weight_locked"] is False

    async def test_add_copies(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/inventory/spools/bulk", json={"spool": {**BASE, "weight_used": 300}, "quantity": 2}
        )

        assert response.status_code == 200, response.text
        assert [s["weight_locked"] for s in response.json()] == [True, True]

    async def test_csv_import(self, async_client: AsyncClient, db_session: AsyncSession):
        csv_text = (
            "material,brand,color_name,label_weight,weight_used\nPLA,Generic,Red,1000,300\nPLA,Generic,Blue,1000,\n"
        )
        files = {"file": ("inventory.csv", io.BytesIO(csv_text.encode()), "text/csv")}

        response = await async_client.post("/api/v1/inventory/spools/import?dry_run=false", files=files)

        assert response.status_code == 200, response.text
        rows = (await db_session.execute(select(Spool.color_name, Spool.weight_locked))).all()
        assert dict(rows) == {"Red": True, "Blue": False}


@pytest.mark.asyncio
@pytest.mark.integration
class TestANegativeUsedWeightIsRefused:
    async def test_create(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/inventory/spools", json={**BASE, "weight_used": -5})

        assert response.status_code == 422

    async def test_edit(self, async_client: AsyncClient):
        spool_id = (await async_client.post("/api/v1/inventory/spools", json=BASE)).json()["id"]

        response = await async_client.patch(f"/api/v1/inventory/spools/{spool_id}", json={"weight_used": -5})

        assert response.status_code == 422

    async def test_a_stored_negative_is_still_served(self, async_client: AsyncClient, db_session: AsyncSession):
        """The bound is on what comes IN: an old row must not break the list."""
        spool = Spool(material="PLA", label_weight=1000, weight_used=-5)
        db_session.add(spool)
        await db_session.commit()

        response = await async_client.get(f"/api/v1/inventory/spools/{spool.id}")

        assert response.status_code == 200
        assert response.json()["weight_used"] == -5
