"""An order line's configuration over the API (spec workshop-product-variants, rules 14–17, 20–21)."""

import pytest
from sqlalchemy import select

from backend.app.models.line_config import ProjectLineChoice, ProjectLinePartCount
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.services import part_stock
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """«Pipe»: flask always, straight tail (standard) or angled tail."""
    product = Product(name="Pipe")
    db_session.add(product)
    await db_session.flush()
    group = ProductVariantGroup(product_id=product.id, name="Tail")
    db_session.add(group)
    await db_session.flush()
    straight = ProductVariantOption(group_id=group.id, name="straight", position=0)
    angled = ProductVariantOption(group_id=group.id, name="angled", position=1)
    db_session.add_all([straight, angled])
    await db_session.flush()
    group.default_option_id = straight.id
    flask = ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1)
    s_tail = ProductPart(
        product_id=product.id,
        kind="printed",
        name="straight tail",
        name_key="straight tail",
        qty_per_unit=1,
        sort_order=1,
    )
    a_tail = ProductPart(
        product_id=product.id,
        kind="printed",
        name="angled tail",
        name_key="angled tail",
        qty_per_unit=1,
        sort_order=2,
    )
    bind(flask)
    bind(s_tail, straight)
    bind(a_tail, angled)
    db_session.add_all([flask, s_tail, a_tail])
    await db_session.commit()
    return {
        "product": product,
        "group": group,
        "straight": straight,
        "angled": angled,
        "flask": flask,
        "s_tail": s_tail,
        "a_tail": a_tail,
    }


async def _order(client, lines):
    r = await client.post("/api/v1/projects/", json={"name": "O", "lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_a_line_created_with_a_choice_answers_its_configuration(committing_client, pipe):
    body = await _order(
        committing_client,
        [{"product_id": pipe["product"].id, "quantity": 2, "choices": {str(pipe["group"].id): pipe["angled"].id}}],
    )
    line = body["lines"][0]
    assert line["mode"] == "product"
    assert line["config_key"] == f"{pipe['group'].id}={pipe['angled'].id}"
    assert line["configuration"]["choices"] == [
        {
            "group_id": pipe["group"].id,
            "group_name": "Tail",
            "option_id": pipe["angled"].id,
            "option_name": "angled",
            "is_default": False,
        }
    ]
    assert line["configuration"]["changed_parts"] == []
    assert sorted(p["name"] for p in line["parts"]) == ["angled tail", "flask"]


@pytest.mark.asyncio
async def test_a_standard_line_records_the_standard_choice(committing_client, db_session, pipe):
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 1}])
    line = body["lines"][0]
    assert line["configuration"]["choices"][0]["is_default"] is True
    rows = (await db_session.execute(select(ProjectLineChoice.option_id))).scalars().all()
    assert rows == [pipe["straight"].id]


@pytest.mark.asyncio
async def test_a_changed_count_names_its_standard(committing_client, pipe):
    body = await _order(
        committing_client,
        [{"product_id": pipe["product"].id, "quantity": 1, "part_counts": {str(pipe["flask"].id): 3}}],
    )
    assert body["lines"][0]["configuration"]["changed_parts"] == [
        {"part_id": pipe["flask"].id, "name": "flask", "qty": 3, "standard_qty": 1}
    ]


@pytest.mark.asyncio
async def test_a_foreign_option_is_refused(committing_client, pipe):
    r = await committing_client.post(
        "/api/v1/projects/",
        json={"name": "O", "lines": [{"product_id": pipe["product"].id, "choices": {str(pipe["group"].id): 999999}}]},
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "That option does not belong to this product"


@pytest.mark.asyncio
async def test_a_parts_line_reads_in_parts(committing_client, pipe):
    body = await _order(
        committing_client,
        [
            {
                "product_id": pipe["product"].id,
                "mode": "parts",
                "quantity": 5,
                "part_counts": {str(pipe["a_tail"].id): 4, str(pipe["flask"].id): 2},
            }
        ],
    )
    line = body["lines"][0]
    assert line["mode"] == "parts"
    assert line["quantity"] == 6
    assert {p["name"]: p["need"] for p in line["parts"]} == {"flask": 2, "angled tail": 4}
    assert line["configuration"]["choices"] == []
    assert {p["name"]: (p["qty"], p["standard_qty"]) for p in line["configuration"]["changed_parts"]} == {
        "flask": (2, 1),
        "angled tail": (4, 1),
    }


@pytest.mark.asyncio
async def test_a_parts_line_takes_nothing_from_the_shelf(committing_client, pipe):
    r = await committing_client.post(
        "/api/v1/projects/",
        json={
            "name": "O",
            "lines": [
                {
                    "product_id": pipe["product"].id,
                    "mode": "parts",
                    "part_counts": {str(pipe["flask"].id): 1},
                    "from_stock_units": 1,
                }
            ],
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "A parts line takes nothing from the shelf"


@pytest.mark.asyncio
async def test_a_parts_line_keeps_quantity_one(committing_client, db_session, pipe):
    body = await _order(
        committing_client,
        [{"product_id": pipe["product"].id, "mode": "parts", "part_counts": {str(pipe["flask"].id): 2}}],
    )
    line_id = body["lines"][0]["id"]
    assert (await db_session.get(ProjectLine, line_id)).quantity == 1
    r = await committing_client.patch(f"/api/v1/projects/{body['id']}/lines/{line_id}", json={"quantity": 2})
    assert r.status_code == 422
    assert r.json()["detail"] == "A parts line always has quantity 1"
    same = await committing_client.patch(f"/api/v1/projects/{body['id']}/lines/{line_id}", json={"note": "ok"})
    assert same.status_code == 200


@pytest.mark.asyncio
async def test_a_parts_line_added_later_is_seeded_too(committing_client, pipe):
    body = await _order(committing_client, [])
    r = await committing_client.post(
        f"/api/v1/projects/{body['id']}/lines",
        json={"product_id": pipe["product"].id, "mode": "parts", "part_counts": {str(pipe["flask"].id): 2}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["config_key"] == f"parts:{pipe['flask'].id}=2"
    empty = await committing_client.post(
        f"/api/v1/projects/{body['id']}/lines", json={"product_id": pipe["product"].id, "mode": "parts"}
    )
    assert empty.status_code == 422
    assert empty.json()["detail"] == "A parts line needs at least one part"


@pytest.mark.asyncio
async def test_a_dry_run_answers_the_impact_and_changes_nothing(committing_client, db_session, pipe):
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 2}])
    line = body["lines"][0]
    r = await committing_client.put(
        f"/api/v1/projects/{body['id']}/lines/{line['id']}/configuration",
        json={"choices": {str(pipe["group"].id): pipe["angled"].id}, "part_counts": {}, "dry_run": True},
    )
    assert r.status_code == 200, r.text
    impact = r.json()
    assert impact["reserved_before"] == 0 and impact["reserved_after"] == 0
    assert [d["name"] for d in impact["dropping"]] == ["straight tail"]
    assert impact["dropping"][0]["per_before"] == 1 and impact["dropping"][0]["per_after"] == 0
    stored = await db_session.get(ProjectLine, line["id"])
    await db_session.refresh(stored)
    assert stored.config_key == line["config_key"]


@pytest.mark.asyncio
async def test_a_configuration_change_moves_the_reservation_and_is_journaled(committing_client, db_session, pipe):
    for part in (pipe["flask"], pipe["s_tail"], pipe["a_tail"]):
        await part_stock.move(db_session, part_id=part.id, delta=5, reason="manual", note="seed")
    await db_session.commit()
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 3, "from_stock_units": 2}])
    line = body["lines"][0]
    assert line["from_stock_units"] == 2
    r = await committing_client.put(
        f"/api/v1/projects/{body['id']}/lines/{line['id']}/configuration",
        json={"choices": {str(pipe["group"].id): pipe["angled"].id}, "part_counts": {}},
    )
    assert r.status_code == 200, r.text
    after = r.json()["lines"][0]
    assert after["config_key"] == f"{pipe['group'].id}={pipe['angled'].id}"
    assert after["from_stock_units"] == 2
    assert sorted(p["name"] for p in after["parts"]) == ["angled tail", "flask"]
    balances = await part_stock.balances(db_session, pipe["product"].id)
    assert balances[pipe["s_tail"].id] == 5 and balances[pipe["a_tail"].id] == 3
    kinds = (await db_session.execute(select(ProjectEvent.kind))).scalars().all()
    assert "line_configured" in kinds


@pytest.mark.asyncio
async def test_a_foreign_line_or_order_is_not_found(committing_client, pipe):
    body = await _order(committing_client, [{"product_id": pipe["product"].id}])
    r = await committing_client.put(
        f"/api/v1/projects/{body['id']}/lines/999999/configuration", json={"choices": {}, "part_counts": {}}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_a_copy_of_the_order_carries_each_lines_configuration(committing_client, db_session, pipe):
    body = await _order(
        committing_client,
        [
            {"product_id": pipe["product"].id, "quantity": 2, "choices": {str(pipe["group"].id): pipe["angled"].id}},
            {"product_id": pipe["product"].id, "mode": "parts", "part_counts": {str(pipe["flask"].id): 4}},
        ],
    )
    copy = (await committing_client.post(f"/api/v1/projects/{body['id']}/duplicate", json={})).json()
    assert [(line["mode"], line["config_key"]) for line in copy["lines"]] == [
        (line["mode"], line["config_key"]) for line in body["lines"]
    ]
    copied_ids = [line["id"] for line in copy["lines"]]
    counts = (
        await db_session.execute(
            select(ProjectLinePartCount.line_id, ProjectLinePartCount.qty).where(
                ProjectLinePartCount.line_id.in_(copied_ids)
            )
        )
    ).all()
    assert counts == [(copied_ids[1], 4)]
