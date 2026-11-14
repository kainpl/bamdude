"""A part is saved in one request — its aliases, and on creation its variant binding (WS-13 E10 A05).

The part dialog has one «Save»: nothing is written before it, and a refusal leaves
nothing half-written. So the whole alias list travels with the part — absent or
``null`` changes nothing, ``[]`` keeps only the part's own key, a list replaces the
rest — and a part can be created already bound to an option.

A new bound part enters the FINAL composition (K22): saved product-mode lines and
stock positions are not frozen, they take it by the same rule as new ones — the lines
that chose its option get ``qty_per_unit``, the others nothing. A parts-only line is an
explicit list and never gains a new part. Creating with a binding takes the product
gate before anything is changed and reads the option behind it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.finished_stock import StockItem
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import finished_stock, line_composition, line_config, product_variants
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def lamp(db_session):
    """A lamp: a «Tail» group (A standard, B), a printed «shade» with an extra alias, and
    saved lines L_A, L_B, L_O (A, with its own count of shade), L_P (parts only, shade
    alone) plus stock positions P_A and P_B."""
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    shade = ProductPart(
        product_id=product.id,
        kind="printed",
        name="shade",
        name_key="shade.stl",
        aliases=["shade.stl", "shade_v2"],
        qty_per_unit=1,
    )
    db_session.add(shade)
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, product.id, [shade])
    tail = await product_variants.create_group(db_session, product.id, "Tail", ["A", "B"], record_lines=False)
    await db_session.flush()
    groups = await product_variants.groups(db_session, product.id)
    opt = {o.name: o.id for g in groups for o in g.options}
    order = Project(name="Order", status="active")
    db_session.add(order)
    await db_session.flush()
    lines = {}
    for label, mode, choices, counts in (
        ("L_A", "product", {tail.id: opt["A"]}, None),
        ("L_B", "product", {tail.id: opt["B"]}, None),
        ("L_O", "product", {tail.id: opt["A"]}, {shade.id: 2}),
        ("L_P", "parts", None, {shade.id: 1}),
    ):
        line = ProjectLine(project_id=order.id, product_id=product.id, quantity=1, mode=mode, sort_order=len(lines))
        db_session.add(line)
        await db_session.flush()
        await line_config.seed_line(db_session, line, choices=choices, counts=counts)
        lines[label] = line.id
    await db_session.commit()
    items = {}
    for label, choices in (("P_A", {tail.id: opt["A"]}), ("P_B", {tail.id: opt["B"]})):
        item = await finished_stock.item_for(db_session, product.id, choices, create=True)
        await db_session.commit()
        items[label] = item.id
    return {"product": product.id, "shade": shade.id, "group": tail.id, "options": opt, "lines": lines, "items": items}


def _url(lamp, tail=""):
    return f"/api/v1/products/{lamp['product']}{tail}"


async def _per(db, lamp, part_id: int) -> dict[str, int]:
    """``label → per-unit count of part_id`` for every saved line and position."""
    parts = (
        (
            await db.execute(
                select(ProductPart)
                .where(ProductPart.product_id == lamp["product"])
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_product = {lamp["product"]: parts}
    lines = [await db.get(ProjectLine, i, populate_existing=True) for i in lamp["lines"].values()]
    items = [await db.get(StockItem, i, populate_existing=True) for i in lamp["items"].values()]
    line_comp = await line_composition.compositions_for_lines(db, lines, by_product)
    item_comp = await line_composition.compositions_for_items(db, items, by_product)
    out = {}
    for label, line_id in lamp["lines"].items():
        out[label] = {p.id: n for p, n in line_comp[line_id]}.get(part_id, 0)
    for label, item_id in lamp["items"].items():
        out[label] = {p.id: n for p, n in item_comp[item_id]}.get(part_id, 0)
    return out


# ------------------------------------------------------------------ aliases


@pytest.mark.asyncio
async def test_creating_a_printed_part_with_aliases_normalises_them_beside_its_own_key(committing_client, lamp):
    r = await committing_client.post(
        _url(lamp, "/parts"), json={"kind": "printed", "name": "Cap", "aliases": [" Cap_V2 ", "cap_v2", "", "lid.stl"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["aliases"] == [body["name_key"], "cap_v2", "lid.stl"]


@pytest.mark.asyncio
async def test_an_empty_list_keeps_only_the_own_key_and_absent_or_null_changes_nothing(committing_client, lamp):
    part = _url(lamp, f"/parts/{lamp['shade']}")
    r = await committing_client.patch(part, json={"qty_per_unit": 1})
    assert r.json()["aliases"] == ["shade.stl", "shade_v2"]
    r = await committing_client.patch(part, json={"aliases": None})
    assert r.status_code == 200, r.text
    assert r.json()["aliases"] == ["shade.stl", "shade_v2"]
    r = await committing_client.patch(part, json={"aliases": []})
    assert r.status_code == 200, r.text
    assert r.json()["aliases"] == ["shade.stl"]
    r = await committing_client.patch(part, json={"aliases": ["shade_v3", "SHADE.STL"]})
    assert r.status_code == 200, r.text
    assert r.json()["aliases"] == ["shade.stl", "shade_v3"]


@pytest.mark.asyncio
async def test_an_alias_of_another_part_refuses_the_whole_save(committing_client, db_session, lamp):
    cap = ProductPart(product_id=lamp["product"], kind="printed", name="cap", name_key="cap.stl", aliases=["cap.stl"])
    db_session.add(cap)
    await db_session.commit()
    r = await committing_client.patch(
        _url(lamp, f"/parts/{lamp['shade']}"), json={"name": "Shade 2", "qty_per_unit": 3, "aliases": ["Cap.stl"]}
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "'cap.stl' already belongs to part 'cap'"
    after = (await committing_client.get(_url(lamp))).json()
    shade = next(p for p in after["parts"] if p["id"] == lamp["shade"])
    assert (shade["name"], shade["qty_per_unit"], shade["aliases"]) == ("shade", 1, ["shade.stl", "shade_v2"])


@pytest.mark.asyncio
async def test_a_purchased_part_takes_no_alias_list_and_an_alias_is_bounded(committing_client, lamp):
    r = await committing_client.post(
        _url(lamp, "/parts"), json={"kind": "purchased", "name": "Nut", "aliases": ["nut"]}
    )
    assert r.status_code == 400, r.text
    r = await committing_client.patch(_url(lamp, f"/parts/{lamp['shade']}"), json={"aliases": ["a" * 513]})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_a_refused_alias_rolls_back_a_binding_change_sent_with_it(committing_client, db_session, lamp):
    """The binding is written first (`freeze_binding` moves saved configurations), the alias
    refusal comes after it — the whole request rolls back: no binding, no frozen counts."""
    cap = ProductPart(product_id=lamp["product"], kind="printed", name="cap", name_key="cap.stl", aliases=["cap.stl"])
    db_session.add(cap)
    await db_session.commit()
    before = await _per(db_session, lamp, lamp["shade"])
    r = await committing_client.patch(
        _url(lamp, f"/parts/{lamp['shade']}"),
        json={"variant_option_ids": [lamp["options"]["A"]], "aliases": ["cap.stl"]},
    )
    assert r.status_code == 409, r.text
    shade = await db_session.get(ProductPart, lamp["shade"], populate_existing=True)
    assert shade.option_links == []
    assert shade.aliases == ["shade.stl", "shade_v2"]
    assert await _per(db_session, lamp, lamp["shade"]) == before


# ------------------------------------------------------------------ binding on create


@pytest.mark.asyncio
async def test_a_part_created_bound_enters_only_the_lines_and_positions_of_its_option(
    committing_client, db_session, lamp
):
    before_shade = await _per(db_session, lamp, lamp["shade"])
    r = await committing_client.post(
        _url(lamp, "/parts"),
        json={"kind": "printed", "name": "Tail A", "qty_per_unit": 1, "variant_option_ids": [lamp["options"]["A"]]},
    )
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["variant_option_ids"] == [lamp["options"]["A"]]
    assert await _per(db_session, lamp, x["id"]) == {"L_A": 1, "L_B": 0, "L_O": 1, "L_P": 0, "P_A": 1, "P_B": 0}
    # Nothing else moved: L_O keeps its own count of shade, every other count stands.
    assert await _per(db_session, lamp, lamp["shade"]) == before_shade


@pytest.mark.asyncio
async def test_a_new_line_takes_a_bound_part_by_its_choice(committing_client, db_session, lamp):
    r = await committing_client.post(
        _url(lamp, "/parts"),
        json={"kind": "printed", "name": "Tail A", "qty_per_unit": 1, "variant_option_ids": [lamp["options"]["A"]]},
    )
    x = r.json()["id"]
    # The new lines' combinations must be open (spec product-part-variant-combinations, п. 10):
    # «B» needs a part that names it, and every printed part of a kit a sliced source.
    tail_b = await committing_client.post(
        _url(lamp, "/parts"),
        json={"kind": "printed", "name": "Tail B", "qty_per_unit": 1, "variant_option_ids": [lamp["options"]["B"]]},
    )
    assert tail_b.status_code == 200, tail_b.text
    new_parts = (
        (await db_session.execute(select(ProductPart).where(ProductPart.id.in_([x, tail_b.json()["id"]]))))
        .scalars()
        .all()
    )
    await slice_parts(db_session, lamp["product"], new_parts)
    order = await db_session.get(Project, (await db_session.get(ProjectLine, lamp["lines"]["L_A"])).project_id)
    fresh = {}
    for label, option in (("new_A", "A"), ("new_B", "B")):
        line = ProjectLine(project_id=order.id, product_id=lamp["product"], quantity=1, mode="product", sort_order=10)
        db_session.add(line)
        await db_session.flush()
        await line_config.seed_line(db_session, line, choices={lamp["group"]: lamp["options"][option]}, counts=None)
        fresh[label] = line
    await db_session.commit()
    parts = (
        (await db_session.execute(select(ProductPart).where(ProductPart.product_id == lamp["product"]))).scalars().all()
    )
    comp = await line_composition.compositions_for_lines(db_session, list(fresh.values()), {lamp["product"]: parts})
    per = {label: {p.id: n for p, n in comp[line.id]}.get(x, 0) for label, line in fresh.items()}
    assert per == {"new_A": 1, "new_B": 0}


@pytest.mark.asyncio
async def test_an_unbound_new_part_still_enters_every_product_line_and_position(committing_client, db_session, lamp):
    """Today's behaviour, pinned: a new part without a binding is part of every
    product-mode kit; a parts-only line is an explicit list and does not gain it."""
    r = await committing_client.post(_url(lamp, "/parts"), json={"kind": "printed", "name": "Base", "qty_per_unit": 1})
    assert r.status_code == 200, r.text
    assert await _per(db_session, lamp, r.json()["id"]) == {"L_A": 1, "L_B": 1, "L_O": 1, "L_P": 0, "P_A": 1, "P_B": 1}


@pytest.mark.asyncio
async def test_a_foreign_option_refuses_the_create_and_writes_nothing(committing_client, db_session, lamp):
    other = Product(name="Other")
    db_session.add(other)
    await db_session.flush()
    g = await product_variants.create_group(db_session, other.id, "Size", ["S"], record_lines=False)
    await db_session.commit()
    foreign = (await product_variants.groups(db_session, other.id))[0].options[0].id
    count = len((await committing_client.get(_url(lamp))).json()["parts"])
    r = await committing_client.post(
        _url(lamp, "/parts"),
        json={"kind": "printed", "name": "Odd", "aliases": ["odd_v2"], "variant_option_ids": [foreign]},
    )
    assert r.status_code == 422, r.text
    assert len((await committing_client.get(_url(lamp))).json()["parts"]) == count
    assert g.id  # the foreign group exists — the refusal is about ownership, not absence
