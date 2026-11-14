"""Many lines in one transaction, stock picked by the server (spec workshop-add-to-order, rules 6–7, 11–12)."""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project, ProjectEvent
from backend.app.services import finished_stock, order_from_files, part_stock
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration

PLATES = {
    "sliced_for_model": "P1S",
    "plates": [
        {"index": 1, "printable_objects": {"1": "cap"}, "print_time_seconds": 600, "filaments": []},
        {"index": 2, "printable_objects": {"1": "cap", "2": "cap_2"}, "print_time_seconds": 900, "filaments": []},
    ],
}


@pytest.fixture
async def farm(db_session):
    """Pipe (Tail: straight standard / angled), shelf flask 4 · straight 3 · angled 1,
    standard position with 2 ready units; Lamp (shade 2 on the shelf, 1 ready unit)."""
    pipe = Product(name="Pipe")
    lamp = Product(name="Lamp")
    db_session.add_all([pipe, lamp])
    await db_session.flush()
    group = ProductVariantGroup(product_id=pipe.id, name="Tail")
    db_session.add(group)
    await db_session.flush()
    straight = ProductVariantOption(group_id=group.id, name="straight", position=0)
    angled = ProductVariantOption(group_id=group.id, name="angled", position=1)
    db_session.add_all([straight, angled])
    await db_session.flush()
    group.default_option_id = straight.id
    parts = {
        "flask": ProductPart(product_id=pipe.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1),
        "straight": ProductPart(
            product_id=pipe.id,
            kind="printed",
            name="straight",
            name_key="straight",
            qty_per_unit=1,
        ),
        "angled": ProductPart(
            product_id=pipe.id,
            kind="printed",
            name="angled",
            name_key="angled",
            qty_per_unit=1,
        ),
        "shade": ProductPart(product_id=lamp.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1),
    }
    bind(parts["flask"])
    bind(parts["straight"], straight)
    bind(parts["angled"], angled)
    bind(parts["shade"])
    db_session.add_all(parts.values())
    await db_session.flush()
    for name, n in (("flask", 4), ("straight", 3), ("angled", 1), ("shade", 2)):
        await part_stock.move(db_session, part_id=parts[name].id, delta=n, reason="manual", note="seed")
    standard = await finished_stock.item_for(db_session, pipe.id, {}, create=True)
    await finished_stock.receive(db_session, standard, 2)
    lamp_item = await finished_stock.item_for(db_session, lamp.id, {}, create=True)
    await finished_stock.receive(db_session, lamp_item, 1)
    await db_session.commit()
    return {"pipe": pipe, "lamp": lamp, "group": group, "angled": angled, "parts": parts}


async def _order(db, status="active"):
    order = Project(name=f"O-{status}", status=status)
    db.add(order)
    await db.commit()
    return order


@pytest.fixture
async def order(db_session):
    return await _order(db_session)


@pytest.fixture
async def plated_file(db_session):
    f = LibraryFile(
        filename="caps.gcode.3mf", file_path="caps.gcode.3mf", file_size=1, file_type="gcode", file_metadata=PLATES
    )
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)
    return f


async def _batch(client, order, lines):
    r = await client.post(f"/api/v1/projects/{order.id}/lines/batch", json={"lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


async def _free(client, product_id, quantity=1):
    r = await client.post("/api/v1/stock/suggest", json={"items": [{"product_id": product_id, "quantity": quantity}]})
    return r.json()["items"][0]


@pytest.mark.asyncio
async def test_three_kinds_in_one_batch(committing_client, farm, order, plated_file):
    body = await _batch(
        committing_client,
        order,
        [
            {"kind": "product", "product_id": farm["pipe"].id, "quantity": 6, "stock": "auto"},
            {"kind": "parts", "product_id": farm["pipe"].id, "part_counts": {str(farm["parts"]["flask"].id): 3}},
            {"kind": "plate", "library_file_id": plated_file.id, "plate_index": 2, "copies": 4},
        ],
    )
    lines = body["order"]["lines"]
    assert [line["mode"] for line in lines] == ["product", "parts", "product"]
    assert body["results"][0] == {
        "line_id": lines[0]["id"],
        "asked_finished": 2,
        "got_finished": 2,
        "asked_kits": 3,
        "got_kits": 3,
    }
    assert lines[0]["from_finished"] == 2 and lines[0]["from_stock_units"] == 5 and lines[0]["from_kit_units"] == 3
    assert lines[2]["product_name"].endswith("plate 2") and lines[2]["quantity"] == 4


@pytest.mark.asyncio
async def test_explicit_numbers_are_clamped_not_refused(committing_client, farm, order):
    body = await _batch(
        committing_client,
        order,
        [
            {
                "kind": "product",
                "product_id": farm["pipe"].id,
                "quantity": 3,
                "stock": {"from_finished": 5, "from_kits": 5},
            }
        ],
    )
    result = body["results"][0]
    assert (result["asked_finished"], result["got_finished"]) == (5, 2)
    assert (result["asked_kits"], result["got_kits"]) == (5, 1)  # ≤ quantity − finished


@pytest.mark.asyncio
async def test_a_configuration_takes_its_own_position(committing_client, farm, order):
    body = await _batch(
        committing_client,
        order,
        [
            {
                "kind": "product",
                "product_id": farm["pipe"].id,
                "quantity": 2,
                "choices": {str(farm["group"].id): farm["angled"].id},
                "stock": "auto",
            }
        ],
    )
    result = body["results"][0]
    # No angled position: nothing ready; flask 4 and angled 1 make one kit.
    assert (result["got_finished"], result["got_kits"]) == (0, 1)


@pytest.mark.asyncio
async def test_the_shelf_moving_under_the_dialog_gives_less(committing_client, farm, order, db_session):
    proposal = (await _free(committing_client, farm["pipe"].id, 2))["from_finished"]
    other = await _order(db_session)
    await _batch(committing_client, other, [{"kind": "product", "product_id": farm["pipe"].id, "quantity": 2}])
    body = await _batch(
        committing_client,
        order,
        [
            {
                "kind": "product",
                "product_id": farm["pipe"].id,
                "quantity": 2,
                "stock": {"from_finished": proposal, "from_kits": 0},
            }
        ],
    )
    assert body["results"][0]["asked_finished"] == 2 and body["results"][0]["got_finished"] == 0


@pytest.mark.asyncio
async def test_one_bad_line_adds_nothing(committing_client, farm, order):
    r = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines/batch",
        json={
            "lines": [
                {"kind": "product", "product_id": farm["lamp"].id, "quantity": 1, "stock": "auto"},
                {"kind": "product", "product_id": 999999, "quantity": 1, "stock": "auto"},
            ]
        },
    )
    assert r.status_code == 404 and r.json()["detail"] == "Product not found"
    assert (await committing_client.get(f"/api/v1/projects/{order.id}")).json()["lines"] == []
    assert (await _free(committing_client, farm["lamp"].id))["finished_free"] == 1  # nothing reserved


@pytest.mark.asyncio
async def test_an_order_that_is_not_active_takes_no_stock(committing_client, farm, db_session):
    done = await _order(db_session, status="completed")
    body = await _batch(committing_client, done, [{"kind": "product", "product_id": farm["pipe"].id, "quantity": 2}])
    assert body["results"][0]["got_finished"] == 0 and body["order"]["lines"][0]["from_stock_units"] == 0
    assert (await _free(committing_client, farm["pipe"].id))["finished_free"] == 2


@pytest.mark.asyncio
async def test_the_single_line_route_is_the_same_road(committing_client, farm, order, db_session):
    r = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines",
        json={"product_id": farm["pipe"].id, "quantity": 2, "from_finished": 2, "from_stock_units": 0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["from_finished"] == 2
    payload = (
        await db_session.execute(
            select(ProjectEvent.payload).where(ProjectEvent.project_id == order.id, ProjectEvent.kind == "line_added")
        )
    ).scalar_one()
    assert payload["from_finished"] == 2 and payload["from_stock"] == 0


@pytest.mark.asyncio
async def test_an_order_created_with_lines_takes_the_same_road(committing_client, farm):
    r = await committing_client.post(
        "/api/v1/projects/",
        json={"name": "N", "lines": [{"product_id": farm["lamp"].id, "quantity": 1, "from_finished": 1}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["from_finished"] == 1


@pytest.mark.asyncio
async def test_a_plate_product_is_made_once(committing_client, order, plated_file, monkeypatch):
    line = {"kind": "plate", "library_file_id": plated_file.id, "plate_index": 1, "copies": 1}
    await _batch(committing_client, order, [line])
    original = order_from_files._plate_product
    calls = []

    async def blind_once(db, file_id, idx):
        calls.append(idx)
        return None if len(calls) == 1 else await original(db, file_id, idx)

    monkeypatch.setattr(order_from_files, "_plate_product", blind_once)
    await _batch(committing_client, order, [line])
    lines = (await committing_client.get(f"/api/v1/projects/{order.id}")).json()["lines"]
    assert lines[0]["product_id"] == lines[1]["product_id"]


@pytest.mark.asyncio
async def test_a_plate_that_does_not_exist_is_refused(committing_client, order, plated_file):
    r = await committing_client.post(
        f"/api/v1/projects/{order.id}/lines/batch",
        json={"lines": [{"kind": "plate", "library_file_id": plated_file.id, "plate_index": 9, "copies": 1}]},
    )
    assert r.status_code == 404 and r.json()["detail"] == "Plate not found"


@pytest.mark.asyncio
async def test_a_parts_line_names_its_parts(committing_client, farm, order):
    body = await _batch(
        committing_client,
        order,
        [
            {
                "kind": "parts",
                "product_id": farm["pipe"].id,
                "part_counts": {str(farm["parts"]["flask"].id): 3, str(farm["parts"]["angled"].id): 1},
            }
        ],
    )
    line = body["order"]["lines"][0]
    assert line["mode"] == "parts" and line["quantity"] == 4 and line["from_stock_units"] == 0
