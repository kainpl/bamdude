"""The pictures arrive on every surface's real route (spec §11.3, guard 2; plan E4, task 30).

A field that exists and stays ``None`` proves nothing: each request here goes through the real
route and finds a ready picture in the answer. DroppedPartOut has no fixture of its own here (a
dry-run reconfiguration of a variant product); it is covered by the fill unit test and the
route-coverage guard (plan E4, ruling)."""

import pytest
from sqlalchemy import select

from backend.app.models.finished_stock import StockItem
from backend.app.services import part_images, part_stock
from backend.tests.fixtures.part_images import part, rendered_farm
from backend.tests.unit.test_part_images_writer import image_bytes

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def images_in(value) -> list:
    found = []
    if isinstance(value, dict):
        if "image" in value:
            found.append(value["image"])
        for item in value.values():
            found += images_in(item)
    elif isinstance(value, list):
        for item in value:
            found += images_in(item)
    return found


def ready(body) -> bool:
    return any(ref and ref.get("status") == "ready" for ref in images_in(body))


@pytest.fixture
async def farm(db_session, committing_client):
    farm = await rendered_farm(db_session)
    bought = part(farm.product.id, "Screw", kind="purchased")
    db_session.add(bought)
    await db_session.commit()
    await part_images.set_photo(db_session, bought.id, image_bytes())
    await part_stock.move(db_session, part_id=farm.body.id, delta=3, reason="manual", note=None)
    await db_session.commit()
    # A "parts" line too: the fulfilment state lists its parts per part (PartStateOut) only for that mode.
    lines = [
        {"product_id": farm.product.id, "quantity": 4},
        {"product_id": farm.product.id, "quantity": 1, "mode": "parts", "part_counts": {str(farm.body.id): 2}},
    ]
    order = await committing_client.post("/api/v1/projects/", json={"name": "O", "lines": lines})
    assert order.status_code in (200, 201), order.text
    received = await committing_client.post(
        "/api/v1/stock/moves", json={"kind": "receipt", "product_id": farm.product.id, "qty": 2}
    )
    assert received.status_code in (200, 201), received.text
    farm.order_id = order.json()["id"]
    farm.item_id = (
        await db_session.execute(select(StockItem.id).where(StockItem.product_id == farm.product.id))
    ).scalar_one()
    return farm


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/products/{product}",
        "/api/v1/products/{product}/plates",
        "/api/v1/products/{product}/files",
        "/api/v1/products/{product}/stock",
        "/api/v1/products/parts",
        "/api/v1/stock",
        "/api/v1/stock/items/{item}",
        "/api/v1/stock/journal?page=1",
        "/api/v1/stock/movements",
        "/api/v1/projects/{order}",
        "/api/v1/projects/{order}/plan",
        "/api/v1/projects/{order}/fulfilment",
    ],
)
async def test_the_surface_route_answers_with_a_ready_picture(committing_client, farm, path):
    url = path.format(product=farm.product.id, item=farm.item_id, order=farm.order_id)
    r = await committing_client.get(url)
    assert r.status_code == 200, r.text
    assert ready(r.json()), f"{url}: no ready picture in {images_in(r.json())}"


async def test_the_product_page_carries_the_editor_state_and_the_unassigned_object(committing_client, farm):
    product = (await committing_client.get(f"/api/v1/products/{farm.product.id}")).json()
    body = next(p for p in product["parts"] if p["name"] == "Body")
    assert body["image"]["kind"] == "render" and body["image_choice"] == {
        "source": "auto",
        "instance": None,
        "pin": None,
        "photo": None,
    }
    plates = (await committing_client.get(f"/api/v1/products/{farm.product.id}/plates")).json()
    cube = next(u for u in plates[0]["unassigned"] if u["name_key"] == "cube")
    assert cube["identify_id"] == 14 and cube["image"]["status"] == "ready"


async def test_an_order_line_shows_the_purchased_part_photo(committing_client, farm):
    order = (await committing_client.get(f"/api/v1/projects/{farm.order_id}")).json()
    purchased = [p for line in order["lines"] for p in line.get("purchased", [])]
    assert purchased and purchased[0]["image"]["kind"] == "photo"


async def test_a_parts_journal_row_names_its_part(committing_client, farm):
    page = (await committing_client.get("/api/v1/stock/journal?page=1&book=parts")).json()
    rows = [row for row in page["items"] if row["book"] == "parts"]
    assert rows and rows[0]["part_id"] == farm.body.id and rows[0]["image"]["status"] == "ready"
