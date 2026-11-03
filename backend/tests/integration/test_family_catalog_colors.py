"""The AMS palette offers exact product-series shades without owning a spool."""

import pytest

from backend.app.models.color_catalog import ColorCatalogEntry
from backend.app.models.user_filament import UserFilamentFamily

pytestmark = pytest.mark.integration
PATH = "/api/v1/inventory/colors/family"


async def test_system_family_palette_without_inventory_spools(async_client):
    await async_client.post("/api/v1/inventory/colors/reset")
    response = await async_client.get(PATH, params={"filament_family_id": "GFG02"})
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows
    assert {(r["manufacturer"], r["material"]) for r in rows} == {("Bambu Lab", "PETG HF")}
    assert [(r["color_name"], r["hex_color"]) for r in rows if r["color_name"] == "Cream"] == [("Cream", "#F9DFB9")]
    assert not any(r["hex_color"] == "#FFFDD0" for r in rows)


async def test_system_alias_without_vendor_prefix_uses_its_complete_series(async_client, db_session):
    response = await async_client.post("/api/v1/inventory/colors/reset")
    assert response.status_code == 200, response.text
    response = await async_client.get(PATH, params={"filament_family_id": "GFL01"})
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 19
    assert {(r["manufacturer"], r["material"]) for r in rows} == {("Polymaker", "PolyTerra PLA")}

    # The exception is for authoritative system identities, not custom aliases.
    db_session.add(
        UserFilamentFamily(
            filament_id="Ppolyterra",
            ecosystem="bambu",
            alias="PolyTerra PLA",
            vendor="Polymaker",
            filament_type="PLA",
            origin="local",
        )
    )
    await db_session.commit()
    response = await async_client.get(PATH, params={"filament_family_id": "Ppolyterra"})
    assert response.status_code == 200 and response.json() == []


@pytest.mark.parametrize("family_id, material", [("GFA00", "PLA Basic"), ("GFA01", "PLA Matte")])
async def test_palette_does_not_mix_variants_or_manufacturers(async_client, db_session, family_id, material):
    for brand, mat, name in [
        ("Bambu Lab", material, "Right shade"),
        ("Other", material, "Wrong brand"),
        ("Bambu Lab", material + " Extra", "Wrong series"),
        ("Bambu Lab", "PLA", "Base only"),
    ]:
        db_session.add(ColorCatalogEntry(manufacturer=brand, material=mat, color_name=name, hex_color="#112233"))
    await db_session.commit()
    response = await async_client.get(PATH, params={"filament_family_id": family_id})
    assert response.status_code == 200, response.text
    assert [r["color_name"] for r in response.json()] == ["Right shade"]


async def test_mirrored_family_uses_exact_identity_and_unknown_names_are_not_guessed(async_client, db_session):
    for fid, alias in [("Pexact", "Acme PETG HF"), ("Pcustom", "My PETG HF")]:
        db_session.add(
            UserFilamentFamily(
                filament_id=fid, ecosystem="bambu", alias=alias, vendor="Acme", filament_type="PETG", origin="local"
            )
        )
    db_session.add(ColorCatalogEntry(manufacturer="Acme", material="PETG HF", color_name="Cream", hex_color="#123456"))
    await db_session.commit()
    exact = await async_client.get(PATH, params={"filament_family_id": "Pexact"})
    assert [r["hex_color"] for r in exact.json()] == ["#123456"]
    for fid in ["Pcustom", "missing", "GFG99"]:
        response = await async_client.get(PATH, params={"filament_family_id": fid})
        assert response.status_code == 200 and response.json() == []


@pytest.mark.parametrize("permissions, status", [(["inventory:read"], 200), ([], 403)])
async def test_family_palette_uses_inventory_read_permission(async_client, db_session, permissions, status):
    from backend.app.core.auth import create_access_token, get_password_hash
    from backend.app.models.group import Group
    from backend.app.models.user import User

    group = Group(name="palette-reader", permissions=permissions)
    user = User(
        username="palette-reader",
        password_hash=get_password_hash("test-only"),
        role="user",
        is_active=True,
        groups=[group],
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(data={"sub": user.username})
    response = await async_client.get(
        PATH, params={"filament_family_id": "GFG02"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == status, response.text
