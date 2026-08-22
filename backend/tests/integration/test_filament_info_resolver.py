"""/cloud/filament-info answers from the identity catalog — zero cloud calls
for names; the cloud phase is the calibration wizard's opt-in only."""

import pytest
from httpx import AsyncClient

from backend.app.models.user_filament import UserFilamentFamily


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filament_info_resolves_from_catalog_without_cloud(async_client: AsyncClient):
    resp = await async_client.post("/api/v1/cloud/filament-info", json=["GFA00", "GFSG99_00", "NOPE123"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["GFA00"]["name"] == "Bambu PLA Basic"
    assert data["GFSG99_00"]["name"] == "Generic PETG"
    assert data["NOPE123"]["name"] == ""  # unknown -> empty, present


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filament_info_names_custom_families(async_client: AsyncClient, db_session):
    db_session.add(
        UserFilamentFamily(
            filament_id="P122e532",
            ecosystem="bambu",
            alias="test PETG Basic",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()
    resp = await async_client.post("/api/v1/cloud/filament-info", json=["P122e532"])
    assert resp.status_code == 200
    assert resp.json()["P122e532"]["name"] == "test PETG Basic"
