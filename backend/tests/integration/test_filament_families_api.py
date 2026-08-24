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
    assert body["push"] == {"bambu": True, "orca": True}


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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_authoring_options_carry_printer_profiles(async_client: AsyncClient):
    body = (await async_client.get("/api/v1/filament-families/authoring-options")).json()
    assert "Bambu Lab P1S 0.4 nozzle" in body["printer_names"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cloud_only_create_requires_the_push(async_client: AsyncClient):
    r = await async_client.post(
        "/api/v1/filament-families",
        json={
            "vendor": "Poly",
            "filament_type": "PETG",
            "serial": "NoOp",
            "printer_names": ["Bambu Lab P1S 0.4 nozzle"],
            "save_local": False,
            "push_to_bambu": False,
        },
    )
    assert r.status_code == 400


@pytest.mark.asyncio
@pytest.mark.integration
async def test_push_resolve_validates_and_routes_the_action(async_client: AsyncClient, db_session):
    from unittest.mock import AsyncMock, patch

    from backend.app.models.user_filament import UserFilamentPreset

    row = UserFilamentPreset(
        owner_user_id=None,
        ecosystem="orca",
        source="local",
        name="Mine @P1S",
        family_filament_id="Presolve1",
        orca_pushed_profile_id="uuid-x",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    with patch(
        "backend.app.services.filament_push.resolve_push_conflict",
        new=AsyncMock(return_value={"status": "overwritten", "profile_id": "uuid-x"}),
    ) as resolver:
        resp = await async_client.post(
            "/api/v1/filament-families/Presolve1/push-resolve",
            json={"preset_row_id": row.id, "action": "force"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "overwritten"
    assert resolver.call_args.kwargs["action"] == "force"

    # a row from another family is not reachable through this url
    resp = await async_client.post(
        "/api/v1/filament-families/Pother123/push-resolve",
        json={"preset_row_id": row.id, "action": "force"},
    )
    assert resp.status_code == 404

    # unknown actions die in validation
    resp = await async_client.post(
        "/api/v1/filament-families/Presolve1/push-resolve",
        json={"preset_row_id": row.id, "action": "merge"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_authored_listing_carries_both_push_states(async_client: AsyncClient, db_session):
    from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset

    db_session.add(
        UserFilamentFamily(
            filament_id="Plisted1",
            ecosystem="local",
            alias="Listed PETG",
            vendor="Poly",
            filament_type="PETG",
            origin="authored",
        )
    )
    db_session.add(
        UserFilamentPreset(
            owner_user_id=None,
            ecosystem="orca",
            source="local",
            name="Listed @P1S",
            family_filament_id="Plisted1",
            pushed_cloud_id="PFUS_1",
            push_dirty=True,
            orca_pushed_profile_id="uuid-1",
        )
    )
    await db_session.commit()

    resp = await async_client.get("/api/v1/filament-families/authored")
    assert resp.status_code == 200
    fam = next(f for f in resp.json()["families"] if f["filament_id"] == "Plisted1")
    preset = fam["presets"][0]
    assert preset["bambu_pushed_id"] == "PFUS_1"
    assert preset["bambu_dirty"] is True
    assert preset["orca_profile_id"] == "uuid-1"
    assert preset["orca_dirty"] is False


@pytest.mark.asyncio
@pytest.mark.integration
async def test_push_resolve_refuses_someone_elses_owned_row(async_client: AsyncClient, db_session):
    """Authored mirrors are farm-global (owner NULL) and pass freely; a row
    that DOES carry another owner is refused — defence in depth for a future
    where owned rows grow push bookkeeping."""
    from backend.app.models.user_filament import UserFilamentPreset

    row = UserFilamentPreset(
        owner_user_id=424242,  # someone who is not the test admin (id=1)
        ecosystem="orca",
        source="local",
        name="Foreign @P1S",
        family_filament_id="Powned01",
        orca_pushed_profile_id="uuid-y",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    resp = await async_client.post(
        "/api/v1/filament-families/Powned01/push-resolve",
        json={"preset_row_id": row.id, "action": "force"},
    )
    assert resp.status_code == 403
