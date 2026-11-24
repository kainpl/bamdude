"""The edges of a line's configuration the final review of WS-08 found
(spec workshop-product-variants): a completed order, parts a change drops,
parts with no shelf, bindings changed under saved orders, merges, and the
impact the dry run reports."""

import pytest
from sqlalchemy import func, select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.line_config import ProjectLinePartCount
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.services import line_config, part_stock
from backend.tests.fixtures.order_fulfilment import complete_order
from backend.tests.fixtures.part_bindings import bind
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """flask ×1 always; tail straight ×1 (standard) or angled ×1."""
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
    parts = {
        "flask": ProductPart(product_id=product.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1),
        "straight": ProductPart(
            product_id=product.id,
            kind="printed",
            name="straight",
            name_key="straight",
            qty_per_unit=1,
            sort_order=1,
        ),
        "angled": ProductPart(
            product_id=product.id,
            kind="printed",
            name="angled",
            name_key="angled",
            qty_per_unit=1,
            sort_order=2,
        ),
        "clip": ProductPart(
            product_id=product.id, kind="printed", name="clip", name_key="clip", qty_per_unit=0, sort_order=3
        ),
    }
    bind(parts["flask"])
    bind(parts["straight"], straight)
    bind(parts["angled"], angled)
    bind(parts["clip"])
    db_session.add_all(parts.values())
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, product.id, parts.values())
    await db_session.commit()
    return {"product": product, "group": group, "straight": straight, "angled": angled, "parts": parts}


async def _order(client, lines):
    r = await client.post("/api/v1/projects/", json={"name": "O", "lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


async def _file(db, project_id, line_id, **parts):
    a = PrintArchive(
        project_id=project_id,
        project_line_id=line_id,
        filename="pipe",
        file_path="",
        file_size=0,
        status="completed",
        quantity=1,
    )
    db.add(a)
    await db.flush()
    db.add_all([PrintArchivePart(archive_id=a.id, name=n, name_key=n, quantity=q) for n, q in parts.items()])
    await db.commit()


def _angled(pipe):
    return {str(pipe["group"].id): pipe["angled"].id}


async def _configure(client, order_id, line_id, *, choices=None, counts=None, dry_run=False, review=None):
    return await client.put(
        f"/api/v1/projects/{order_id}/lines/{line_id}/configuration",
        json={"choices": choices or {}, "part_counts": counts or {}, "dry_run": dry_run, **(review or {})},
    )


# ---- a completed order ----


@pytest.mark.asyncio
async def test_a_completed_orders_line_cannot_be_reconfigured(committing_client, db_session, pipe):
    for part in ("flask", "straight", "angled"):
        await part_stock.move(db_session, part_id=pipe["parts"][part].id, delta=5, reason="manual", note="seed")
    await db_session.commit()
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 2, "from_stock_units": 2}])
    line_id = body["lines"][0]["id"]
    done = await complete_order(committing_client, body["id"])
    assert done.status_code == 200, done.text
    before = await part_stock.balances(db_session, pipe["product"].id)
    for dry_run in (True, False):
        r = await _configure(committing_client, body["id"], line_id, choices=_angled(pipe), dry_run=dry_run)
        assert r.status_code == 409
        assert r.json()["detail"] == "A completed order's lines cannot be reconfigured"
    assert await part_stock.balances(db_session, pipe["product"].id) == before


# ---- parts a change drops stay visible and bankable ----


@pytest.mark.asyncio
async def test_printed_parts_a_change_drops_become_bankable_surplus(committing_client, db_session, pipe):
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 2}])
    line_id = body["lines"][0]["id"]
    await _file(db_session, body["id"], line_id, straight=3)
    impact = (await _configure(committing_client, body["id"], line_id, choices=_angled(pipe), dry_run=True)).json()
    # 3 printed for a line of 2: one was surplus already, two become surplus.
    assert [(d["name"], d["printed"]) for d in impact["dropping"]] == [("straight", 2)]
    review = {
        "expected_revision": body["lines"][0]["configuration"]["revision"],
        "review_version": impact["review_version"],
    }
    r = await _configure(committing_client, body["id"], line_id, choices=_angled(pipe), review=review)
    assert r.status_code == 200, r.text
    parts = {p["name"]: p for p in r.json()["lines"][0]["parts"]}
    assert parts["straight"]["qty_per_unit"] == 0
    assert parts["straight"]["usable"] == 3 and parts["straight"]["surplus"] == 3
    assert parts["straight"]["need"] == 0 and parts["straight"]["remaining"] == 0
    assert r.json()["lines"][0]["units_printed"] == 0
    banked = await committing_client.post(f"/api/v1/projects/{body['id']}/bank-surplus")
    assert banked.status_code == 200, banked.text
    assert (await part_stock.balances(db_session, pipe["product"].id))[pipe["parts"]["straight"].id] == 3


@pytest.mark.asyncio
async def test_a_part_marked_not_counted_on_the_plate_counts_nowhere(committing_client, db_session, pipe):
    # A part marked «не рахувати» (no shelf) is not surplus of anybody (spec
    # workshop-order-issue-followups, rule 34).
    pipe["parts"]["clip"].ignored = True
    await db_session.commit()
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 1}])
    line_id = body["lines"][0]["id"]
    await _file(db_session, body["id"], line_id, clip=4)
    line = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()["lines"][0]
    assert "clip" not in {p["name"] for p in line["parts"]}


@pytest.mark.asyncio
async def test_an_out_of_kit_part_on_the_plate_is_the_lines_surplus(committing_client, db_session, pipe):
    # A zero without the mark is a part out of the kit — made for this order, so it is
    # the line's surplus and can be banked (spec workshop-order-issue-followups, rule 34).
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 1}])
    line_id = body["lines"][0]["id"]
    await _file(db_session, body["id"], line_id, clip=4)
    line = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()["lines"][0]
    assert {p["name"]: p["surplus"] for p in line["parts"] if p["name"] == "clip"} == {"clip": 4}


# ---- an out-of-kit part's surplus is bankable ----


@pytest.mark.asyncio
async def test_surplus_of_an_out_of_kit_part_is_bankable(committing_client, db_session, pipe):
    clip = pipe["parts"]["clip"]
    body = await _order(
        committing_client, [{"product_id": pipe["product"].id, "mode": "parts", "part_counts": {str(clip.id): 2}}]
    )
    line_id = body["lines"][0]["id"]
    await _file(db_session, body["id"], line_id, clip=4)
    order = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()
    assert {p["name"]: p["surplus"] for p in order["lines"][0]["parts"]} == {"clip": 2}
    assert order["figures"]["bankable_surplus"] == 2
    r = await committing_client.post(f"/api/v1/projects/{body['id']}/bank-surplus")
    assert r.status_code == 200, r.text
    total = await db_session.scalar(
        select(func.sum(ProductPartStockMovement.delta)).where(ProductPartStockMovement.product_part_id == clip.id)
    )
    assert total == 2


# ---- a binding changed under saved orders ----


@pytest.mark.asyncio
async def test_binding_a_part_keeps_the_kit_of_saved_lines(committing_client, db_session, pipe):
    flask = pipe["parts"]["flask"]
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 2}])
    line_id = body["lines"][0]["id"]
    r = await committing_client.patch(
        f"/api/v1/products/{pipe['product'].id}/parts/{flask.id}", json={"variant_option_ids": [pipe["angled"].id]}
    )
    assert r.status_code == 200, r.text
    line = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()["lines"][0]
    assert {p["name"]: p["qty_per_unit"] for p in line["parts"]} == {"flask": 1, "straight": 1}
    # A line created after the binding follows it.
    fresh = (
        await committing_client.post(
            f"/api/v1/projects/{body['id']}/lines", json={"product_id": pipe["product"].id, "quantity": 1}
        )
    ).json()
    new_line = next(ln for ln in fresh["lines"] if ln["id"] != line_id)
    assert {p["name"] for p in new_line["parts"]} == {"straight"}


@pytest.mark.asyncio
async def test_unbinding_a_part_keeps_it_out_of_saved_lines_that_did_not_choose_it(committing_client, db_session, pipe):
    angled = pipe["parts"]["angled"]
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 1}])
    line_id = body["lines"][0]["id"]
    r = await committing_client.patch(
        f"/api/v1/products/{pipe['product'].id}/parts/{angled.id}", json={"variant_option_ids": []}
    )
    assert r.status_code == 200, r.text
    line = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()["lines"][0]
    assert {p["name"] for p in line["parts"]} == {"flask", "straight"}
    rows = (
        await db_session.execute(
            select(ProjectLinePartCount.part_id, ProjectLinePartCount.qty).where(
                ProjectLinePartCount.line_id == line_id
            )
        )
    ).all()
    # The captured kit excludes it without writing a new order override.
    assert rows == []


# ---- a merge keeps what each line wanted ----


@pytest.mark.asyncio
async def test_a_merge_does_not_let_an_exclusion_of_the_source_drop_the_target(committing_client, db_session, pipe):
    clip, flask = pipe["parts"]["clip"], pipe["parts"]["flask"]
    clip.qty_per_unit = 1
    await db_session.commit()
    body = await _order(
        committing_client,
        [{"product_id": pipe["product"].id, "quantity": 1, "part_counts": {str(clip.id): 0}}],
    )
    r = await committing_client.post(
        f"/api/v1/products/{pipe['product'].id}/parts/{flask.id}/merge", json={"source_part_id": clip.id}
    )
    assert r.status_code == 200, r.text
    line = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()["lines"][0]
    assert {p["name"]: p["qty_per_unit"] for p in line["parts"]} == {"flask": 1, "straight": 1}


# ---- the dry run reports what actually becomes surplus ----


@pytest.mark.asyncio
async def test_a_smaller_count_reports_only_what_becomes_surplus(committing_client, db_session, pipe):
    flask = pipe["parts"]["flask"]
    body = await _order(
        committing_client,
        [{"product_id": pipe["product"].id, "quantity": 10, "part_counts": {str(flask.id): 4}}],
    )
    line_id = body["lines"][0]["id"]
    await _file(db_session, body["id"], line_id, flask=20)
    fewer = (await _configure(committing_client, body["id"], line_id, counts={str(flask.id): 3}, dry_run=True)).json()
    assert [(d["name"], d["printed"]) for d in fewer["dropping"]] == [("flask", 0)]
    await _file(db_session, body["id"], line_id, flask=12)
    fewer = (await _configure(committing_client, body["id"], line_id, counts={str(flask.id): 3}, dry_run=True)).json()
    # 32 printed: 40 needed before (no surplus), 30 after — 2 become surplus.
    assert [(d["name"], d["printed"]) for d in fewer["dropping"]] == [("flask", 2)]


# ---- a no-op change writes nothing ----


@pytest.mark.asyncio
async def test_saving_the_same_configuration_journals_nothing(committing_client, db_session, pipe):
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 1}])
    line_id = body["lines"][0]["id"]
    r = await _configure(committing_client, body["id"], line_id, choices={str(pipe["group"].id): pipe["straight"].id})
    assert r.status_code == 200
    kinds = (await db_session.execute(select(ProjectEvent.kind))).scalars().all()
    assert "line_configured" not in kinds


@pytest.mark.asyncio
async def test_a_real_save_does_not_compute_the_dry_run_impact(committing_client, db_session, pipe, monkeypatch):
    async def _no(*_a, **_k):
        raise AssertionError("the impact is for the dry run")

    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 1}])
    line = body["lines"][0]
    draft = await _configure(committing_client, body["id"], line["id"], choices=_angled(pipe), dry_run=True)
    assert draft.status_code == 200, draft.text
    review = {
        "expected_revision": line["configuration"]["revision"],
        "review_version": draft.json()["review_version"],
    }
    monkeypatch.setattr(line_config, "_dropping", _no)
    r = await _configure(committing_client, body["id"], line["id"], choices=_angled(pipe), review=review)
    assert r.status_code == 200, r.text


# ---- kits of a configuration ----


@pytest.mark.asyncio
async def test_kits_of_a_configuration(committing_client, db_session, pipe):
    for part, n in (("flask", 5), ("straight", 5), ("angled", 2)):
        await part_stock.move(db_session, part_id=pipe["parts"][part].id, delta=n, reason="manual", note="seed")
    await db_session.commit()
    base = f"/api/v1/products/{pipe['product'].id}/kits"
    assert (await committing_client.get(base)).json() == {"kits_available": 5}
    assert (await committing_client.get(base, params={"options": str(pipe["angled"].id)})).json() == {
        "kits_available": 2
    }
    flask = pipe["parts"]["flask"]
    assert (await committing_client.get(base, params={"counts": f"{flask.id}:2"})).json() == {"kits_available": 2}
    bad = await committing_client.get(base, params={"options": "999999"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_the_line_row_quantity_is_untouched_by_the_fixes(db_session, pipe, committing_client):
    body = await _order(committing_client, [{"product_id": pipe["product"].id, "quantity": 3}])
    assert (await db_session.get(ProjectLine, body["lines"][0]["id"])).quantity == 3
