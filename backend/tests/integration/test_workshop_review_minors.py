"""The final review's server-side minors of the Workshop rights model (WS-13 E13 T19).

Each is a read or a filter that answered more than its caller's rights reach, or a read that took a
writer's lock: a «with active orders» filter without the orders' read, a part route's shelf balance
without the stock's read, a dispatch note's id told apart by a 404, search wildcards taken as
patterns, a configuration preview that took the product's gate — and the group editor's refusal
of the retired ``projects:*`` strings, which no test held.
"""

import pytest
from httpx import AsyncClient

from backend.tests.integration.test_workshop_library_rights import _jwt, _user
from backend.tests.integration.test_workshop_rights_stock import shelf  # noqa: F401 — the fixture

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_with_active_orders_is_the_orders_read(committing_client: AsyncClient, db_session):
    await _user(db_session, "rm_contacts", ["customers:read"])
    refused = await committing_client.get(
        "/api/v1/customers/", params={"page": 1, "with_active": True}, headers=await _jwt("rm_contacts")
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "workshop_read_required"


@pytest.mark.asyncio
async def test_a_part_route_tells_no_shelf_balance_without_the_stocks_read(
    committing_client: AsyncClient,
    db_session,
    shelf,  # noqa: F811
):
    await _user(db_session, "rm_author", ["products:read", "products:update"])
    renamed = await committing_client.patch(
        f"/api/v1/products/{shelf['product']}/parts/{shelf['part']}",
        json={"name": "shade v2"},
        headers=await _jwt("rm_author"),
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["stock_balance"] is None
    # The stock's reader keeps the figure (two kits on the shelf).
    seen = await committing_client.patch(
        f"/api/v1/products/{shelf['product']}/parts/{shelf['part']}", json={"name": "shade v3"}
    )
    assert seen.json()["stock_balance"] == 2


@pytest.mark.asyncio
async def test_a_missing_note_and_a_note_out_of_reach_answer_alike(committing_client: AsyncClient, db_session):
    await _user(db_session, "rm_orders", ["orders:read"])
    await _user(db_session, "rm_stock", ["stock:read"])
    missing = 987654
    assert (
        await committing_client.get(f"/api/v1/stock-issues/{missing}", headers=await _jwt("rm_orders"))
    ).status_code == 403
    assert (
        await committing_client.get(f"/api/v1/stock-issues/{missing}", headers=await _jwt("rm_stock"))
    ).status_code == 404


@pytest.mark.asyncio
async def test_search_wildcards_are_letters(committing_client: AsyncClient, shelf):  # noqa: F811
    await committing_client.post("/api/v1/customers/", json={"name": "Plain customer"})
    options = (await committing_client.get("/api/v1/customers/options", params={"q": "%"})).json()
    assert options == []
    catalog = (await committing_client.get("/api/v1/stock/catalog", params={"q": "_"})).json()
    assert catalog == []


@pytest.mark.asyncio
async def test_a_configuration_preview_takes_no_gate(committing_client: AsyncClient, db_session, shelf, monkeypatch):  # noqa: F811
    """A preview is a read (``orders:read``, a Viewer has it): it must not take the product's
    gate — SQLite's write lock, a PostgreSQL row lock."""
    from backend.app.api.routes import projects as projects_route

    async def no_gate(*_args, **_kwargs):
        raise AssertionError("a preview took the product gate")

    monkeypatch.setattr(projects_route, "product_gate", no_gate)
    url = f"/api/v1/projects/{shelf['order']}/lines/{shelf['line']}/configuration"
    preview = await committing_client.put(url, json={"dry_run": True})
    assert preview.status_code == 200, preview.text


@pytest.mark.asyncio
async def test_the_group_editor_refuses_the_retired_workshop_rights(committing_client: AsyncClient):
    created = await committing_client.post(
        "/api/v1/groups/", json={"name": "Old desk", "permissions": ["projects:read", "orders:read"]}
    )
    assert created.status_code == 400
    assert "projects:read" in created.json()["detail"]
