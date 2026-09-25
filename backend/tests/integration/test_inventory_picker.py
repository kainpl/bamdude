"""Assignment picker filters before paging instead of slicing a flat list."""

from datetime import datetime, timezone

import pytest

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment


@pytest.mark.asyncio
@pytest.mark.integration
async def test_picker_finds_far_spool_and_keeps_pages_bounded(async_client, db_session):
    db_session.add_all(
        [
            Spool(material="PLA", brand="Ordinary", color_name=f"Row {index}", label_weight=1000, core_weight=250)
            for index in range(731)
        ]
    )
    target = Spool(material="PETG", brand="Faraway", color_name="Gold", label_weight=1000, core_weight=250)
    db_session.add(target)
    await db_session.commit()
    base = "/api/v1/inventory/spools/picker?printer_id=1&ams_id=0&tray_id=0"

    page = await async_client.get(f"{base}&show_all=true&page=15")
    assert page.status_code == 200, page.text
    assert page.json()["meta"]["total"] == 732
    assert len(page.json()["items"]) == 32

    search = await async_client.get(f"{base}&q=Faraway&tray_material=PETG")
    assert search.status_code == 200, search.text
    assert search.json()["meta"]["total"] == 1
    assert [spool["id"] for spool in search.json()["items"]] == [target.id]
    assert (await async_client.get(f"{base}&q=Faraway&tray_material=PLA")).json()["items"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_picker_assignment_profile_and_archived_filters(async_client, db_session, printer_factory):
    printer = await printer_factory()
    other_printer = await printer_factory()
    same_profile = Spool(
        material="ABS",
        brand="Profile",
        color_name="Same",
        slicer_filament_name="Test_1 @X1C",
        label_weight=1000,
        core_weight=250,
    )
    same_material = Spool(
        material="PETG",
        brand="Material",
        color_name="Same",
        label_weight=1000,
        core_weight=250,
    )
    assigned = Spool(material="PETG", brand="Assigned", label_weight=1000, core_weight=250)
    archived = Spool(
        material="PETG",
        brand="Archived",
        label_weight=1000,
        core_weight=250,
        archived_at=datetime.now(timezone.utc),
    )
    db_session.add_all([same_profile, same_material, assigned, archived])
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=assigned.id, printer_id=other_printer.id, ams_id=0, tray_id=0))
    await db_session.commit()

    base = f"/api/v1/inventory/spools/picker?printer_id={printer.id}&ams_id=0&tray_id=0"
    response = await async_client.get(f"{base}&tray_profile=Test_1&tray_material=PETG")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["items"]} == {same_profile.id, same_material.id}

    # SQL LIKE metacharacters in a profile are literal; show-all bypasses
    # assignment/tray predicates but never offers an archived spool.
    escaped = await async_client.get(f"{base}&tray_profile=Test%251&tray_material=NONE")
    assert escaped.status_code == 200, escaped.text
    assert same_profile.id not in {row["id"] for row in escaped.json()["items"]}
    show_all = await async_client.get(f"{base}&show_all=true&replacing_spool_id={same_material.id}")
    assert {row["id"] for row in show_all.json()["items"]} == {same_profile.id, assigned.id}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_picker_and_family_colors_require_inventory_read(async_client, db_session):
    from backend.app.core.auth import generate_api_key
    from backend.app.models.api_key import APIKey

    raw, key_hash, key_prefix = generate_api_key()
    db_session.add(
        APIKey(
            name="no-inventory-read",
            key_hash=key_hash,
            key_prefix=key_prefix,
            enabled=True,
            can_read_status=False,
        )
    )
    await db_session.commit()
    headers = {"X-API-Key": raw}
    picker = await async_client.get("/api/v1/inventory/spools/picker?printer_id=1&ams_id=0&tray_id=0", headers=headers)
    colors = await async_client.get(
        "/api/v1/inventory/spools/family-colors?filament_family_id=GF-PETG", headers=headers
    )
    assert picker.status_code == colors.status_code == 403
