"""The catalog's doors into the library and the shelf (WS-13 E13 T15, CAT-05/07/08/10/15/16/18).

A product door that links or reads a library file asks what the library side asks — the
library's own update right and the file's visibility — so the two directions of one link are
one rule. Exporting carries only the files the reader may see. Deleting a part or a product
that would destroy shelf stock asks ``stock:adjust`` too (O24). A product's cover is a label
drawn in orders and stock rows too.
"""

import io
import zipfile

import pytest
from httpx import AsyncClient

from backend.tests.integration.test_product_export_import import DIRECT, SHARED, _upload
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration


@pytest.fixture
async def linked(committing_client: AsyncClient):
    """As the admin: a product linked to a file the admin uploaded, one more file unlinked,
    and a folder."""
    direct = await _upload(committing_client, "lamp.gcode.3mf", DIRECT)
    spare = await _upload(committing_client, "shade.gcode.3mf", SHARED)
    folder = (await committing_client.post("/api/v1/library/folders", json={"name": "Rights folder"})).json()
    product = (await committing_client.post("/api/v1/products/", json={"name": "Rights desk lamp"})).json()["id"]
    linked = await committing_client.put(f"/api/v1/products/{product}/files", json={"library_file_ids": [direct["id"]]})
    assert linked.status_code == 200, linked.text
    return {"product": product, "direct": direct["id"], "spare": spare["id"], "folder": folder["id"]}


def _members(body: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(body)).namelist()


@pytest.mark.asyncio
async def test_an_export_carries_only_the_files_the_reader_may_see(committing_client, db_session, linked):
    await _user(db_session, "pr_blind", ["products:read"])
    await _user(db_session, "pr_seeing", ["products:read", "library:read_all"])
    blind = await committing_client.get(f"/api/v1/products/{linked['product']}/export", headers=await _jwt("pr_blind"))
    assert blind.status_code == 200, blind.text
    seeing = await committing_client.get(
        f"/api/v1/products/{linked['product']}/export", headers=await _jwt("pr_seeing")
    )
    assert len(_members(seeing.content)) > len(_members(blind.content))
    assert not [m for m in _members(blind.content) if m.endswith(".3mf")]


@pytest.mark.asyncio
async def test_linking_a_file_from_the_product_asks_the_librarys_right(committing_client, db_session, linked):
    await _user(db_session, "pr_editor", ["products:read", "products:update"])
    await _user(
        db_session, "pr_librarian", ["products:read", "products:update", "library:read_all", "library:update_all"]
    )
    url = f"/api/v1/products/{linked['product']}/files"
    both = {"library_file_ids": [linked["direct"], linked["spare"]]}
    assert (await committing_client.put(url, json=both, headers=await _jwt("pr_editor"))).status_code == 403
    unchanged = (await committing_client.get(f"/api/v1/products/{linked['product']}")).json()["library_file_ids"]
    assert unchanged == [linked["direct"]]
    assert (await committing_client.put(url, json=both, headers=await _jwt("pr_librarian"))).status_code == 200
    unlink = f"/api/v1/products/{linked['product']}/files/{linked['spare']}"
    assert (await committing_client.delete(unlink, headers=await _jwt("pr_editor"))).status_code == 403


@pytest.mark.asyncio
async def test_a_trashed_file_cannot_be_linked(committing_client, db_session, linked):
    assert (await committing_client.delete(f"/api/v1/library/files/{linked['spare']}")).status_code in (200, 204)
    url = f"/api/v1/products/{linked['product']}/files"
    refused = await committing_client.put(url, json={"library_file_ids": [linked["direct"], linked["spare"]]})
    assert refused.status_code == 404


@pytest.mark.asyncio
async def test_linking_a_folder_from_the_product_asks_the_librarys_folder_right(committing_client, db_session, linked):
    await _user(db_session, "pr_folder_editor", ["products:read", "products:update", "library:update_own"])
    await _user(db_session, "pr_folder_keeper", ["products:read", "products:update", "library:update_all"])
    url = f"/api/v1/products/{linked['product']}/folders"
    body = {"library_folder_ids": [linked["folder"]]}
    assert (await committing_client.put(url, json=body, headers=await _jwt("pr_folder_editor"))).status_code == 403
    assert (await committing_client.put(url, json=body, headers=await _jwt("pr_folder_keeper"))).status_code == 200


@pytest.mark.asyncio
async def test_rereading_a_card_reads_the_file(committing_client, db_session, linked):
    await _user(db_session, "pr_reread_blind", ["products:read", "products:update"])
    url = f"/api/v1/products/{linked['product']}/card/reread?file_id={linked['direct']}"
    assert (await committing_client.post(url, headers=await _jwt("pr_reread_blind"))).status_code == 404


@pytest.mark.asyncio
async def test_a_copy_without_the_librarys_right_leaves_the_links_behind(committing_client, db_session, linked):
    await _user(db_session, "pr_copier", ["products:read", "products:create"])
    copy = await committing_client.post(
        f"/api/v1/products/{linked['product']}/duplicate", json={}, headers=await _jwt("pr_copier")
    )
    assert copy.status_code in (200, 201), copy.text
    assert copy.json()["library_file_ids"] == []
    assert copy.json()["links_skipped"] is True
    full = await committing_client.post(f"/api/v1/products/{linked['product']}/duplicate", json={})
    assert full.json()["library_file_ids"] == [linked["direct"]]
    assert full.json()["links_skipped"] is False


async def _part_with_stock(client: AsyncClient, product: int, qty: int, name: str = "knob") -> int:
    part = (
        await client.post(
            f"/api/v1/products/{product}/parts", json={"kind": "printed", "name": name, "qty_per_unit": 1}
        )
    ).json()["id"]
    if qty:
        moved = await client.post(
            f"/api/v1/products/{product}/stock/adjust", json={"part_id": part, "delta": qty, "note": "count"}
        )
        assert moved.status_code in (200, 201), moved.text
    return part


@pytest.mark.asyncio
async def test_deleting_a_part_with_stock_asks_adjust_and_one_without_does_not(committing_client, db_session):
    product = (await committing_client.post("/api/v1/products/", json={"name": "Knob box"})).json()["id"]
    stocked = await _part_with_stock(committing_client, product, 2)
    await _user(db_session, "pr_part_editor", ["products:read", "products:update"])
    refused = await committing_client.delete(
        f"/api/v1/products/{product}/parts/{stocked}", headers=await _jwt("pr_part_editor")
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == {
        "error": "consequence_right_required",
        "right": "stock:adjust",
        "message": "Missing required permissions: stock:adjust",
    }
    stock = (await committing_client.get(f"/api/v1/products/{product}/stock")).json()
    assert {b["part_id"]: b["balance"] for b in stock["balances"]}[stocked] == 2
    empty = await _part_with_stock(committing_client, product, 0, name="bare knob")
    assert (
        await committing_client.delete(
            f"/api/v1/products/{product}/parts/{empty}", headers=await _jwt("pr_part_editor")
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_deleting_a_product_with_stock_asks_adjust(committing_client, db_session):
    product = (await committing_client.post("/api/v1/products/", json={"name": "Knob crate"})).json()["id"]
    await _part_with_stock(committing_client, product, 3)
    await _user(db_session, "pr_deleter", ["products:read", "products:delete"])
    await _user(db_session, "pr_deleter_adjust", ["products:read", "products:delete", "stock:adjust"])
    refused = await committing_client.delete(f"/api/v1/products/{product}", headers=await _jwt("pr_deleter"))
    assert refused.status_code == 403
    assert refused.json()["detail"]["right"] == "stock:adjust"
    assert (
        await committing_client.delete(f"/api/v1/products/{product}", headers=await _jwt("pr_deleter_adjust"))
    ).status_code == 200


@pytest.mark.asyncio
async def test_a_products_cover_is_a_label_in_stock_rows_too(committing_client, db_session, linked):
    await _user(db_session, "pr_cover_keeper", ["stock:read"])
    await _user(db_session, "pr_cover_nobody", ["inventory:read"])
    url = f"/api/v1/products/{linked['product']}/cover-image"
    assert (await committing_client.get(url, headers=await _jwt("pr_cover_keeper"))).status_code != 403
    assert (await committing_client.get(url, headers=await _jwt("pr_cover_nobody"))).status_code == 403


@pytest.mark.asyncio
async def test_merging_away_a_part_an_order_acquired_asks_the_orders_right(committing_client, db_session):
    product = (await committing_client.post("/api/v1/products/", json={"name": "Screw kit"})).json()["id"]
    parts = []
    for name in ("M3 screw", "M3 bolt"):
        part = await committing_client.post(
            f"/api/v1/products/{product}/parts", json={"kind": "purchased", "name": name, "qty_per_unit": 2}
        )
        assert part.status_code in (200, 201), part.text
        parts.append(part.json()["id"])
    target, source = parts
    order = await committing_client.post(
        "/api/v1/projects/", json={"name": "Screw order", "lines": [{"product_id": product, "quantity": 1}]}
    )
    assert order.status_code in (200, 201), order.text
    acquired = await committing_client.patch(
        f"/api/v1/projects/{order.json()['id']}/procurement/{source}", json={"quantity_acquired": 2}
    )
    assert acquired.status_code == 200, acquired.text
    await _user(db_session, "pr_merger", ["products:read", "products:update"])
    refused = await committing_client.post(
        f"/api/v1/products/{product}/parts/{target}/merge",
        json={"source_part_id": source},
        headers=await _jwt("pr_merger"),
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["right"] == "orders:update"
    still = (await committing_client.get(f"/api/v1/products/{product}")).json()
    assert {p["id"] for p in still["parts"]} == {target, source}
