"""The stock's two rights, and every order door that moves the shelf (WS-13 E13 T15, O06/O23).

``stock:move`` is the flow of goods (receipts, reservations, assembly, issues, taking for an
order line, banking a surplus); ``stock:adjust`` corrects the books (stocktakes, write-offs). An
order door that TAKES from the shelf asks ``stock:move`` beside the order's own right; giving back
as a consequence of an order edit asks nothing more. A product line asks ``products:read``; a
read-only preview asks only the read. Each refusal leaves nothing written.
"""

import pytest
from httpx import AsyncClient

from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = pytest.mark.integration


@pytest.fixture
async def shelf(committing_client: AsyncClient):
    """As the admin: a product with one printed part, two free kits on its shelf, a finished
    position with one unit, and an active order with a line of it."""
    product = (await committing_client.post("/api/v1/products/", json={"name": "Rights lamp"})).json()["id"]
    part = (
        await committing_client.post(
            f"/api/v1/products/{product}/parts", json={"kind": "printed", "name": "shade", "qty_per_unit": 1}
        )
    ).json()["id"]
    adjusted = await committing_client.post(
        f"/api/v1/products/{product}/stock/adjust", json={"part_id": part, "delta": 2, "note": "count"}
    )
    assert adjusted.status_code in (200, 201), adjusted.text
    receipt = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "receipt", "product_id": product, "qty": 1}
    )
    assert receipt.status_code == 200, receipt.text
    order = await committing_client.post(
        "/api/v1/projects/", json={"name": "Rights order", "lines": [{"product_id": product, "quantity": 2}]}
    )
    assert order.status_code in (200, 201), order.text
    body = order.json()
    return {
        "product": product,
        "part": part,
        "item": receipt.json()["id"],
        "order": body["id"],
        "line": body["lines"][0]["id"],
    }


async def _kits(client: AsyncClient, product: int) -> int:
    return (await client.get(f"/api/v1/products/{product}/kits")).json()["kits_available"]


@pytest.mark.asyncio
async def test_a_stocktake_asks_adjust_and_a_receipt_asks_move(committing_client, db_session, shelf):
    await _user(db_session, "st_mover", ["stock:read", "stock:move"])
    await _user(db_session, "st_counter", ["stock:read", "stock:adjust"])
    take = {"kind": "stocktake", "item_id": shelf["item"], "counted": 3, "note": "found two"}
    receipt = {"kind": "receipt", "item_id": shelf["item"], "qty": 1}

    assert (
        await committing_client.post("/api/v1/stock/moves", json=take, headers=await _jwt("st_mover"))
    ).status_code == 403
    assert (
        await committing_client.post("/api/v1/stock/moves", json=receipt, headers=await _jwt("st_counter"))
    ).status_code == 403
    item = (await committing_client.get(f"/api/v1/stock/items/{shelf['item']}")).json()
    assert item["on_hand"] == 1  # neither refusal moved a unit
    assert (
        await committing_client.post("/api/v1/stock/moves", json=take, headers=await _jwt("st_counter"))
    ).status_code == 200
    assert (
        await committing_client.post("/api/v1/stock/moves", json=receipt, headers=await _jwt("st_mover"))
    ).status_code == 200


@pytest.mark.asyncio
async def test_an_order_line_takes_from_the_shelf_only_with_the_move_right(committing_client, db_session, shelf):
    await _user(db_session, "st_desk", ["orders:read", "orders:update", "products:read"])
    await _user(db_session, "st_desk_move", ["orders:read", "orders:update", "products:read", "stock:move"])
    url = f"/api/v1/projects/{shelf['order']}/lines/batch"
    asking = {"lines": [{"kind": "product", "product_id": shelf["product"], "quantity": 1}]}  # stock defaults to auto
    refused = await committing_client.post(url, json=asking, headers=await _jwt("st_desk"))
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "stock_move_required"
    assert await _kits(committing_client, shelf["product"]) == 2

    none = {
        "lines": [
            {
                "kind": "product",
                "product_id": shelf["product"],
                "quantity": 1,
                "stock": {"from_finished": 0, "from_kits": 0},
            }
        ]
    }
    assert (await committing_client.post(url, json=none, headers=await _jwt("st_desk"))).status_code == 200
    assert (await committing_client.post(url, json=asking, headers=await _jwt("st_desk_move"))).status_code == 200


@pytest.mark.asyncio
async def test_a_product_line_needs_the_catalogs_read(committing_client, db_session, shelf):
    await _user(db_session, "st_blind", ["orders:read", "orders:update"])
    url = f"/api/v1/projects/{shelf['order']}/lines/batch"
    body = {
        "lines": [
            {
                "kind": "product",
                "product_id": shelf["product"],
                "quantity": 1,
                "stock": {"from_finished": 0, "from_kits": 0},
            }
        ]
    }
    assert (await committing_client.post(url, json=body, headers=await _jwt("st_blind"))).status_code == 403


@pytest.mark.asyncio
async def test_raising_a_lines_kits_asks_move_and_lowering_them_does_not(committing_client, db_session, shelf):
    await _user(db_session, "st_editor", ["orders:read", "orders:update"])
    url = f"/api/v1/projects/{shelf['order']}/lines/{shelf['line']}"
    raised = await committing_client.patch(url, json={"from_stock_units": 1}, headers=await _jwt("st_editor"))
    assert raised.status_code == 403
    assert await _kits(committing_client, shelf["product"]) == 2

    assert (await committing_client.patch(url, json={"from_stock_units": 1})).status_code == 200  # the admin takes one
    lowered = await committing_client.patch(url, json={"from_stock_units": 0}, headers=await _jwt("st_editor"))
    assert lowered.status_code == 200, lowered.text
    assert await _kits(committing_client, shelf["product"]) == 2


@pytest.mark.asyncio
async def test_a_plate_line_takes_from_the_shelf_only_with_the_move_right(committing_client, db_session, shelf):
    """A plate's one-off product is reused per file and plate, so its shelf can hold goods (a
    customer-less order closed to stock, a banked surplus): a plate line asks the shelf like a
    product line — ``auto`` by default needs ``stock:move``, ``none`` does not (O06/O23)."""
    from backend.app.models.library import LibraryFile
    from backend.tests.integration.test_order_lines_batch import PLATES

    plated = LibraryFile(
        filename="rights-caps.gcode.3mf",
        file_path="rights-caps.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata=PLATES,
    )
    db_session.add(plated)
    await db_session.commit()
    await db_session.refresh(plated)
    await _user(db_session, "st_plate", ["orders:read", "orders:update", "library:read_all"])
    await _user(db_session, "st_plate_move", ["orders:read", "orders:update", "library:read_all", "stock:move"])
    url = f"/api/v1/projects/{shelf['order']}/lines/batch"
    plate = {"kind": "plate", "library_file_id": plated.id, "plate_index": 1, "copies": 1}

    refused = await committing_client.post(url, json={"lines": [plate]}, headers=await _jwt("st_plate"))
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "stock_move_required"
    none = {**plate, "stock": {"from_finished": 0, "from_kits": 0}}
    assert (
        await committing_client.post(url, json={"lines": [none]}, headers=await _jwt("st_plate"))
    ).status_code == 200
    assert (
        await committing_client.post(url, json={"lines": [plate]}, headers=await _jwt("st_plate_move"))
    ).status_code == 200


@pytest.mark.asyncio
async def test_issuing_asks_move_and_a_write_off_asks_adjust_too(committing_client, db_session, shelf):
    await _user(db_session, "st_issuer", ["orders:read", "orders:update"])
    await _user(db_session, "st_shipper", ["orders:read", "orders:update", "stock:move"])
    url = f"/api/v1/projects/{shelf['order']}/fulfilment"
    assert (
        await committing_client.post(
            url, json={"lines": [{"line_id": shelf["line"], "issue": 1}]}, headers=await _jwt("st_issuer")
        )
    ).status_code == 403
    write_off = {"lines": [{"line_id": shelf["line"], "write_off": 1}], "write_off_note": "cracked"}
    assert (await committing_client.post(url, json=write_off, headers=await _jwt("st_shipper"))).status_code == 403


@pytest.mark.asyncio
async def test_completing_an_order_whose_goods_are_out_asks_no_stock_right(committing_client, db_session, shelf):
    """Completing is a consequence of the order's own right (O23): a batch that moves nothing
    and only completes asks ``orders:update`` alone — the order manager closes an order whose
    goods are already out; a batch that moves a unit still asks ``stock:move``."""
    await _user(db_session, "st_closer", ["orders:read", "orders:update"])
    receipt = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "receipt", "product_id": shelf["product"], "qty": 1}
    )
    assert receipt.status_code == 200, receipt.text
    created = await committing_client.post(
        "/api/v1/projects/",
        json={"name": "Held order", "lines": [{"product_id": shelf["product"], "quantity": 1, "from_finished": 1}]},
    )
    assert created.status_code in (200, 201), created.text
    order = created.json()["id"]
    state = (await committing_client.get(f"/api/v1/projects/{order}/fulfilment")).json()
    assert state["closes_to_stock"] and state["can_complete"], state
    url = f"/api/v1/projects/{order}/fulfilment"

    moving = {"lines": [{"line_id": created.json()["lines"][0]["id"], "receive": 1}], "complete": True}
    assert (await committing_client.post(url, json=moving, headers=await _jwt("st_closer"))).status_code == 403
    done = await committing_client.post(url, json={"lines": [], "complete": True}, headers=await _jwt("st_closer"))
    assert done.status_code == 200, done.text
    assert (await committing_client.get(f"/api/v1/projects/{order}")).json()["status"] == "completed"


@pytest.mark.asyncio
async def test_taking_stock_and_banking_a_surplus_ask_move(committing_client, db_session, shelf):
    await _user(db_session, "st_bank", ["orders:read", "orders:update", "stock:read"])
    headers = await _jwt("st_bank")
    assert (
        await committing_client.post(f"/api/v1/projects/{shelf['order']}/take-stock", json={}, headers=headers)
    ).status_code == 403
    assert (
        await committing_client.post(f"/api/v1/projects/{shelf['order']}/bank-surplus", json={}, headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_the_shelfs_offers_need_the_stocks_read(committing_client, db_session, shelf):
    await _user(db_session, "st_offers", ["orders:read"])
    assert (
        await committing_client.get(f"/api/v1/projects/{shelf['order']}/stock-offers", headers=await _jwt("st_offers"))
    ).status_code == 403


@pytest.mark.asyncio
async def test_a_configuration_preview_is_a_read_and_saving_it_is_an_edit(committing_client, db_session, shelf):
    await _user(db_session, "st_preview", ["orders:read"])
    url = f"/api/v1/projects/{shelf['order']}/lines/{shelf['line']}/configuration"
    headers = await _jwt("st_preview")
    preview = await committing_client.put(url, json={"dry_run": True}, headers=headers)
    assert preview.status_code == 200, preview.text
    assert (await committing_client.put(url, json={"dry_run": False}, headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_copying_an_order_reads_it(committing_client, db_session, shelf):
    await _user(db_session, "st_copy_blind", ["orders:create"])
    assert (
        await committing_client.post(
            f"/api/v1/projects/{shelf['order']}/duplicate", json={}, headers=await _jwt("st_copy_blind")
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_a_suggestion_for_an_order_line_reads_the_order(committing_client, db_session, shelf):
    await _user(db_session, "st_suggest", ["stock:read"])
    body = {"items": [{"product_id": shelf["product"], "quantity": 1, "line_id": shelf["line"]}]}
    assert (
        await committing_client.post("/api/v1/stock/suggest", json=body, headers=await _jwt("st_suggest"))
    ).status_code == 403
    body["items"][0].pop("line_id")
    assert (
        await committing_client.post("/api/v1/stock/suggest", json=body, headers=await _jwt("st_suggest"))
    ).status_code == 200


@pytest.mark.asyncio
async def test_the_stock_catalog_lets_a_storekeeper_pick_a_product_without_the_catalog(
    committing_client, db_session, shelf
):
    await _user(db_session, "st_keeper", ["stock:read", "stock:move"])
    await _user(db_session, "st_nobody", ["inventory:read"])
    rows = await committing_client.get("/api/v1/stock/catalog", params={"q": "Rights"}, headers=await _jwt("st_keeper"))
    assert rows.status_code == 200, rows.text
    [row] = [r for r in rows.json() if r["id"] == shelf["product"]]
    assert row["name"] == "Rights lamp"
    assert row["origin"] == "catalog"
    assert row["variant_groups"] == []
    assert "parts" not in row and "kits_available" not in row
    assert (await committing_client.get("/api/v1/products/", headers=await _jwt("st_keeper"))).status_code == 403
    assert (await committing_client.get("/api/v1/stock/catalog", headers=await _jwt("st_nobody"))).status_code == 403


@pytest.mark.asyncio
async def test_a_dialog_opened_for_one_product_reads_it_from_the_stock_catalog_whatever_its_origin(
    committing_client, db_session, shelf
):
    """WS-13 E13 T17: a stock dialog opened for one product (a position's row, a product page) reads
    that product's options here — a one-off product too, which the pick list leaves out — and
    says whether it is retired, as the picker hides a retired one."""
    from backend.app.models.product import Product

    one_off = Product(name="One-off lamp", origin="adhoc_job")
    db_session.add(one_off)
    await db_session.commit()
    await _user(db_session, "st_keeper_one", ["stock:read", "stock:move"])
    listed = await committing_client.get("/api/v1/stock/catalog", headers=await _jwt("st_keeper_one"))
    assert one_off.id not in [r["id"] for r in listed.json()]
    named = await committing_client.get(
        "/api/v1/stock/catalog", params={"product_id": one_off.id}, headers=await _jwt("st_keeper_one")
    )
    assert named.status_code == 200, named.text
    [row] = named.json()
    assert (row["id"], row["origin"], row["is_active"]) == (one_off.id, "adhoc_job", True)
