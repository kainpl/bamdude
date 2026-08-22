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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_default_scope_is_the_users_own_set(async_client: AsyncClient, db_session):
    """Empty-query browse = BS's "installed filaments" analogue: only families
    the user actually has (spool links / own presets / customs). Search still
    sweeps the full catalog; an empty own-set falls back to everything."""
    # Fresh install: no own families -> full catalog fallback (honours limit).
    resp = await async_client.get("/api/v1/filament-families", params={"limit": 200})
    assert len(resp.json()) > 50

    # A spool linked to GFA00 narrows the browse list to it.
    from backend.app.models.spool import Spool

    db_session.add(Spool(material="PLA", filament_family_id="GFA00"))
    await db_session.commit()
    resp = await async_client.get("/api/v1/filament-families")
    ids = {row["filament_id"] for row in resp.json()}
    assert ids == {"GFA00"}

    # Searching still reaches the whole catalog.
    resp = await async_client.get("/api/v1/filament-families", params={"q": "petg"})
    assert any(r["filament_id"] == "GFG99" for r in resp.json())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_authoring_options_lists_bs_types(async_client: AsyncClient):
    resp = await async_client.get("/api/v1/filament-families/authoring-options")
    assert resp.status_code == 200
    body = resp.json()
    assert "PETG" in body["filament_types"]
    assert body["push"] == {"bambu": True, "orca": False}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_family_endpoint_and_delete_guard(async_client: AsyncClient, db_session):
    from unittest.mock import AsyncMock, patch

    from backend.app.models.spool import Spool

    with patch(
        "backend.app.services.filament_authoring._resolve_bundled_content",
        new=AsyncMock(return_value=None),  # identity-only is enough for the route contract
    ):
        resp = await async_client.post(
            "/api/v1/filament-families",
            json={"vendor": "Poly", "filament_type": "PETG", "serial": "Rt", "printer_ids": []},
        )
    assert resp.status_code == 201
    fid = resp.json()["filament_id"]
    assert fid.startswith("P") and resp.json()["attached"] is False

    # vendor refusal surfaces as 400
    resp = await async_client.post(
        "/api/v1/filament-families",
        json={"vendor": "Bambu", "filament_type": "PETG", "serial": "Rt", "printer_ids": []},
    )
    assert resp.status_code == 400

    # referenced family refuses deletion with 409
    db_session.add(Spool(brand="B", material="PETG", filament_family_id=fid))
    await db_session.commit()
    resp = await async_client.delete(f"/api/v1/filament-families/{fid}")
    assert resp.status_code == 409
