"""Smoke of the split commit (WS-13 E13 T14): the migrated roles do what they did.

The default Operators and Viewers, and a custom group carried over by m194, reach the four
Workshop domains through the real gates exactly as their ``projects:*`` rights let them before:
a reader reads all four and writes nothing, an operator reads and writes all four.
"""

import pytest
from httpx import AsyncClient

from backend.app.core.permissions import DEFAULT_GROUPS
from backend.app.migrations.m194_workshop_permissions import _map_custom
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration

READS = [
    "/api/v1/projects/",
    "/api/v1/products/",
    "/api/v1/customers/",
    "/api/v1/delivery-methods/",
    "/api/v1/product-categories/",
    "/api/v1/stock/items",
    "/api/v1/stock-issues/",
]


async def _writes(client: AsyncClient, headers: dict) -> dict[str, int]:
    out = {
        "order": (await client.post("/api/v1/projects/", json={"name": "Smoke order"}, headers=headers)).status_code,
        "customer": (
            await client.post("/api/v1/customers/", json={"name": "Smoke customer"}, headers=headers)
        ).status_code,
        "category": (
            await client.post("/api/v1/product-categories/", json={"name": "Smoke category"}, headers=headers)
        ).status_code,
    }
    product = await client.post("/api/v1/products/", json={"name": "Smoke product"}, headers=headers)
    out["product"] = product.status_code
    return out


@pytest.mark.asyncio
async def test_a_migrated_reader_reads_all_four_domains_and_writes_nothing(async_client: AsyncClient, db_session):
    await _user(db_session, "smoke_viewer", list(DEFAULT_GROUPS["Viewers"]["permissions"]))
    headers = await _jwt("smoke_viewer")
    for url in READS:
        assert (await async_client.get(url, headers=headers)).status_code == 200, url
    assert set((await _writes(async_client, headers)).values()) == {403}


@pytest.mark.asyncio
async def test_a_migrated_operator_reads_and_writes_all_four_domains(async_client: AsyncClient, db_session):
    await _user(db_session, "smoke_operator", list(DEFAULT_GROUPS["Operators"]["permissions"]))
    headers = await _jwt("smoke_operator")
    for url in READS:
        assert (await async_client.get(url, headers=headers)).status_code == 200, url
    writes = await _writes(async_client, headers)
    assert all(code in (200, 201) for code in writes.values()), writes


@pytest.mark.asyncio
async def test_a_custom_group_carried_over_keeps_creating_without_editing(async_client: AsyncClient, db_session):
    await _user(db_session, "smoke_clerk", _map_custom(["projects:read", "projects:create"]))
    headers = await _jwt("smoke_clerk")
    created = await async_client.post("/api/v1/customers/", json={"name": "Clerk customer"}, headers=headers)
    assert created.status_code in (200, 201), created.text
    patched = await async_client.patch(
        f"/api/v1/customers/{created.json()['id']}", json={"name": "Renamed"}, headers=headers
    )
    assert patched.status_code == 403
