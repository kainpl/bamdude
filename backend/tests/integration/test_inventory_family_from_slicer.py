"""A spool written without a family gets one from its slicer code — on the
server, through the one resolver that still understands every legacy format.

The spool form used to derive ``filament_family_id`` itself from
``slicer_filament`` with a client-side "strip the S" rule, which turned the
support families (``GFS00`` Support W, ``GFS04`` PVA …) into ids that exist
nowhere, and the route then refused its own client's edit with
``422 unknown filament family`` — the weight of a support spool sitting in an
AMS could not be corrected at all (2026-09-04). Resolution belongs to
``filament_identity.resolve_raw``; the client sends what it has and the server
fills the link. Garbage sent explicitly is still refused: the picker only
offers known families, so an unknown id means a broken client, not a request.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_and_update_derive_the_family_from_the_slicer_code(async_client: AsyncClient, db_session):
    created = await async_client.post(
        "/api/v1/inventory/spools",
        json={"material": "PLA", "brand": "Bambu Lab", "slicer_filament": "GFS00"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["filament_family_id"] == "GFS00"
    spool_id = created.json()["id"]

    # The form's own payload shape: family absent, slicer code present, plus
    # the field the user actually changed.
    updated = await async_client.patch(
        f"/api/v1/inventory/spools/{spool_id}",
        json={"filament_family_id": None, "slicer_filament": "GFSG99_00", "weight_used": 5.0},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["filament_family_id"] == "GFG99"
    assert updated.json()["weight_used"] == 5.0

    # A slicer code nothing can resolve leaves an honest NULL, never garbage.
    unknown = await async_client.patch(
        f"/api/v1/inventory/spools/{spool_id}",
        json={"filament_family_id": None, "slicer_filament": "P1a2b3c4d"},
    )
    assert unknown.status_code == 200, unknown.text
    assert unknown.json()["filament_family_id"] is None
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_explicit_unknown_family_is_still_refused(async_client: AsyncClient, db_session):
    created = await async_client.post(
        "/api/v1/inventory/spools",
        json={"material": "PLA", "brand": "Bambu Lab", "slicer_filament": "GFS00"},
    )
    spool_id = created.json()["id"]

    refused = await async_client.patch(
        f"/api/v1/inventory/spools/{spool_id}",
        json={"filament_family_id": "GF00", "slicer_filament": "GFS00", "weight_used": 5.0},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "unknown filament family"
    await db_session.rollback()
