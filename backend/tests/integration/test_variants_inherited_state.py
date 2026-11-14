"""The variants batch keeps an inherited group without a standard, or an empty one (WS-13 E10 A06).

The manager saves the whole draft in one ``PUT``. A group that already has no
standard — such groups exist — used to make every save of that product a 422,
because the batch demanded a standard in every group; an empty group made it a 422
too. A06 holds two INDEPENDENT checks against the stored group (R11):

- ``default: null`` passes only when the stored standard is already ``null``; the
  group may still be renamed and its options renamed, reordered or added — the
  standard does not appear by itself; setting one explicitly is allowed;
- ``options: []`` passes only when the stored group is already empty; its first
  option comes without an automatic standard.

A new group without a standard or without options, a standard taken away, and a
non-empty group emptied are refused as before.
"""

from __future__ import annotations

import pytest

from backend.app.models.finished_stock import StockItem
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.services import finished_stock, line_config, product_variants
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def lamp(db_session):
    """«Tail» with a standard (A, B), an inherited «Base» without one (X, Y), an
    inherited empty «Extra», a saved line and a stock position."""
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    shade = ProductPart(product_id=product.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1)
    db_session.add(shade)
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, product.id, [shade])
    tail = await product_variants.create_group(db_session, product.id, "Tail", ["A", "B"], record_lines=False)
    base = ProductVariantGroup(product_id=product.id, name="Base", position=1, default_option_id=None)
    extra = ProductVariantGroup(product_id=product.id, name="Extra", position=2, default_option_id=None)
    db_session.add_all([base, extra])
    await db_session.flush()
    db_session.add_all(
        [
            ProductVariantOption(group_id=base.id, name="X", position=0),
            ProductVariantOption(group_id=base.id, name="Y", position=1),
        ]
    )
    order = Project(name="Order", status="active")
    db_session.add(order)
    await db_session.flush()
    line = ProjectLine(project_id=order.id, product_id=product.id, quantity=1, mode="product", sort_order=0)
    db_session.add(line)
    await db_session.flush()
    await line_config.seed_line(db_session, line, choices=None, counts=None)
    await db_session.commit()
    item = await finished_stock.item_for(db_session, product.id, {}, create=True)
    await db_session.commit()
    return {
        "product": product.id,
        "tail": tail.id,
        "base": base.id,
        "extra": extra.id,
        "line": line.id,
        "item": item.id,
    }


def _url(lamp):
    return f"/api/v1/products/{lamp['product']}"


async def _state(client, lamp) -> dict:
    r = await client.get(_url(lamp))
    assert r.status_code == 200, r.text
    return r.json()


def _draft(state: dict) -> list[dict]:
    return [
        {
            "id": g["id"],
            "name": g["name"],
            "options": [{"id": o["id"], "name": o["name"]} for o in g["options"]],
            "default": g["default_option_id"],
        }
        for g in state["variant_groups"]
    ]


def _group(draft, gid):
    return next(g for g in draft if g["id"] == gid)


async def _put(client, lamp, revision, groups):
    return await client.put(f"{_url(lamp)}/variants", json={"revision": revision, "groups": groups})


async def _keys(db, lamp):
    line = await db.get(ProjectLine, lamp["line"], populate_existing=True)
    item = await db.get(StockItem, lamp["item"], populate_existing=True)
    return line.config_key, item.config_key


@pytest.mark.asyncio
async def test_editing_another_group_keeps_the_inherited_ones_as_they_are(committing_client, db_session, lamp):
    state = await _state(committing_client, lamp)
    keys = await _keys(db_session, lamp)
    draft = _draft(state)
    _group(draft, lamp["tail"])["name"] = "Tail end"
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    groups = {g["id"]: g for g in r.json()["variant_groups"]}
    assert groups[lamp["base"]]["default_option_id"] is None
    assert groups[lamp["extra"]]["options"] == [] and groups[lamp["extra"]]["default_option_id"] is None
    assert await _keys(db_session, lamp) == keys


@pytest.mark.asyncio
async def test_an_inherited_group_without_a_standard_may_be_renamed_and_gain_an_option(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    base = _group(draft, lamp["base"])
    base["name"] = "Stand"
    base["options"][0]["name"] = "X wide"
    base["options"].append({"temp_id": "z", "name": "Z"})
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    stand = next(g for g in r.json()["variant_groups"] if g["id"] == lamp["base"])
    assert stand["name"] == "Stand" and [o["name"] for o in stand["options"]] == ["X wide", "Y", "Z"]
    assert stand["default_option_id"] is None


@pytest.mark.asyncio
async def test_an_inherited_empty_group_takes_its_first_option_without_a_standard(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    _group(draft, lamp["extra"])["options"] = [{"temp_id": "first", "name": "Felt pad"}]
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    extra = next(g for g in r.json()["variant_groups"] if g["id"] == lamp["extra"])
    assert [o["name"] for o in extra["options"]] == ["Felt pad"]
    assert extra["default_option_id"] is None


@pytest.mark.asyncio
async def test_a_standard_may_be_set_on_an_inherited_group(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    base = _group(draft, lamp["base"])
    base["default"] = base["options"][1]["id"]
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    assert (
        next(g for g in r.json()["variant_groups"] if g["id"] == lamp["base"])["default_option_id"]
        == base["options"][1]["id"]
    )


@pytest.mark.asyncio
async def test_a_standard_is_never_taken_away(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    _group(draft, lamp["tail"])["default"] = None
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "invalid_draft"
    assert r.json()["detail"]["group"] == lamp["tail"]


@pytest.mark.asyncio
async def test_a_non_empty_group_is_never_emptied(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    base = _group(draft, lamp["base"])
    base["options"] = []
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "invalid_draft"
    assert r.json()["detail"]["group"] == lamp["base"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "new_group",
    [
        {"temp_id": "g", "name": "Size", "options": [{"temp_id": "s", "name": "S"}], "default": None},
        {"temp_id": "g", "name": "Size", "options": [], "default": None},
    ],
)
async def test_a_new_group_still_needs_options_and_a_standard(committing_client, lamp, new_group):
    state = await _state(committing_client, lamp)
    r = await _put(committing_client, lamp, state["variants_revision"], _draft(state) + [new_group])
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "invalid_draft"
