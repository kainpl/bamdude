"""Synthetic-only inventory-group and printer-policy API contract."""

import pytest

from backend.tests.unit.services.test_auto_stock_spool import GROUP, spool

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_normal_single_and_bulk_create_are_available_without_full_marker(async_client):
    # Product A's ordinary inventory form does not post added_full, and the
    # create schema does not expose it. Reproduce through public APIs, not an
    # ORM fixture that artificially supplies True.
    payload = {k: v for k, v in GROUP.items() if k != "filament_family_id"}
    single = await async_client.post("/api/v1/inventory/spools", json=payload)
    assert single.status_code == 200, single.text
    assert single.json()["added_full"] is None
    bulk = await async_client.post("/api/v1/inventory/spools/bulk", json={"spool": payload, "quantity": 2})
    assert bulk.status_code == 200, bulk.text
    assert all(s["added_full"] is None for s in bulk.json())
    partial = await async_client.post("/api/v1/inventory/spools", json=payload | {"weight_used": 300})
    assert partial.status_code == 200, partial.text
    response = await async_client.get("/api/v1/inventory/spools/auto-stock-groups")
    assert response.status_code == 200, response.text
    assert response.json() == [payload | {"filament_family_id": "", "available_count": 3}]


async def test_groups_are_strict_and_count_only_unused_unassigned_full_stock(async_client, db_session):
    await spool(db_session)
    await spool(db_session)
    await spool(db_session, filament_family_id="GFA99")
    await spool(db_session, weight_used=200)
    response = await async_client.get("/api/v1/inventory/spools/auto-stock-groups")
    assert response.status_code == 200, response.text
    groups = response.json()
    assert len(groups) == 2
    assert next(g for g in groups if g["filament_family_id"] == GROUP["filament_family_id"]) == GROUP | {
        "available_count": 2
    }


async def test_patch_saves_policy_and_preserves_backup_and_foreign_namespaces(async_client, printer_factory):
    p = await printer_factory(ams_policies={"future": {"x": 1}, "backup_compatibility": {"normalize_color": True}})
    policy = {"enabled": True, "group": GROUP}
    response = await async_client.patch(f"/api/v1/printers/{p.id}", json={"ams_policies": {"auto_stock_spool": policy}})
    assert response.status_code == 200, response.text
    assert response.json()["ams_policies"]["auto_stock_spool"] == policy
    assert response.json()["ams_policies"]["backup_compatibility"]["normalize_color"] is True
    renamed = await async_client.patch(f"/api/v1/printers/{p.id}", json={"name": "Synthetic B"})
    assert renamed.json()["ams_policies"]["auto_stock_spool"] == policy


@pytest.mark.parametrize("group", [None, GROUP | {"rgba": "black"}, GROUP | {"label_weight": 0}])
async def test_enabled_policy_requires_a_valid_full_stock_group(async_client, printer_factory, group):
    p = await printer_factory()
    response = await async_client.patch(
        f"/api/v1/printers/{p.id}", json={"ams_policies": {"auto_stock_spool": {"enabled": True, "group": group}}}
    )
    assert response.status_code == 422


async def test_printer_editor_without_inventory_write_cannot_change_policy(async_client, db_session, printer_factory):
    from backend.app.core.auth import create_access_token
    from backend.app.core.permissions import Permission
    from backend.tests.integration.test_api_key_owner_authority import _user

    p = await printer_factory()
    await _user(db_session, "printer_editor", [Permission.PRINTERS_UPDATE.value, Permission.PRINTERS_READ.value])
    headers = {"Authorization": "Bearer " + create_access_token({"sub": "printer_editor"})}
    response = await async_client.patch(
        f"/api/v1/printers/{p.id}",
        headers=headers,
        json={"ams_policies": {"auto_stock_spool": {"enabled": True, "group": GROUP}}},
    )
    assert response.status_code == 403, response.text
    unrelated = await async_client.patch(f"/api/v1/printers/{p.id}", headers=headers, json={"name": "Still allowed"})
    assert unrelated.status_code == 200, unrelated.text
    groups = await async_client.get("/api/v1/inventory/spools/auto-stock-groups", headers=headers)
    assert groups.status_code == 403
