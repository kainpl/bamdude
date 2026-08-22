"""Family catalog endpoints: search merges system + user tiers; per-family
presets filter by printer; the sync trigger returns immediately."""

import pytest
from httpx import AsyncClient

from backend.app.models.user_filament import UserFilamentFamily


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_returns_system_and_user_families(async_client: AsyncClient, db_session):
    db_session.add(
        UserFilamentFamily(
            filament_id="P122e532",
            ecosystem="bambu",
            alias="test PETG Basic",
            vendor="test",
            filament_type="PETG",
            origin="cloud_bambu",
        )
    )
    await db_session.commit()

    resp = await async_client.get("/api/v1/filament-families", params={"q": "petg"})
    assert resp.status_code == 200
    ids = {row["filament_id"] for row in resp.json()}
    assert "GFG99" in ids and "P122e532" in ids


@pytest.mark.asyncio
@pytest.mark.integration
async def test_family_presets_filter_by_printer(async_client: AsyncClient):
    resp = await async_client.get(
        "/api/v1/filament-families/GFG99/presets",
        params={"printer_name": "Bambu Lab A1 mini 0.4 nozzle"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert rows and all("Bambu Lab A1 mini 0.4 nozzle" in r["compatible_printers"] for r in rows)
    assert any(r["setting_id"].startswith("GFSG99") for r in rows)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sync_trigger_returns_immediately(async_client: AsyncClient):
    resp = await async_client.post("/api/v1/filament-families/sync")
    assert resp.status_code == 200 and resp.json()["queued"] is True
