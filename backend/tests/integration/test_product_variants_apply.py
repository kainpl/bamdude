"""``PUT /products/{id}/variants`` — the whole variants draft in one transaction (WS-13 E1, spec VR4–VR9).

The draft is the desired final state; the revision is the state the client saw. The
server diffs, checks the whole draft first, writes behind the product gate, and a
refusal anywhere leaves nothing written.
"""

import pytest
from sqlalchemy import select

from backend.app.i18n import set_language_cache
from backend.app.models.finished_stock import StockItem, StockItemChoice
from backend.app.models.line_config import ProjectLineChoice
from backend.app.models.product import Product, ProductPart
from backend.app.models.project import Project, ProjectEvent
from backend.app.models.project_line import ProjectLine
from backend.app.services import finished_stock, line_config, product_variants
from backend.tests.fixtures.sliced_sources import slice_parts

pytestmark = pytest.mark.integration


@pytest.fixture
async def lamp(db_session):
    """A lamp with a Colour group (red standard, blue), a line of it and a position."""
    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    shade = ProductPart(product_id=product.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1)
    db_session.add(shade)
    await db_session.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db_session, product.id, [shade])
    colour = await product_variants.create_group(db_session, product.id, "Colour", ["red", "blue"], record_lines=False)
    order = Project(name="Order", status="active")
    db_session.add(order)
    await db_session.flush()
    line = ProjectLine(project_id=order.id, product_id=product.id, quantity=2, mode="product", sort_order=0)
    db_session.add(line)
    await db_session.flush()
    await line_config.seed_line(db_session, line, choices=None, counts=None)
    await db_session.commit()
    item = await finished_stock.item_for(db_session, product.id, {}, create=True)
    await db_session.commit()
    groups = await product_variants.groups(db_session, product.id)
    options = {o.name: o.id for g in groups for o in g.options}
    await db_session.commit()
    return {
        "product": product.id,
        "shade": shade.id,
        "group": colour.id,
        "options": options,
        "order": order.id,
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
    """The product's groups as a draft, unchanged."""
    return [
        {
            "id": g["id"],
            "name": g["name"],
            "options": [{"id": o["id"], "name": o["name"]} for o in g["options"]],
            "default": g["default_option_id"],
        }
        for g in state["variant_groups"]
    ]


async def _put(client, lamp, revision, groups):
    return await client.put(f"{_url(lamp)}/variants", json={"revision": revision, "groups": groups})


async def _keys(db, lamp):
    line = await db.get(ProjectLine, lamp["line"], populate_existing=True)
    item = await db.get(StockItem, lamp["item"], populate_existing=True)
    return line.config_key, item.config_key


@pytest.mark.asyncio
async def test_renaming_an_option_keeps_its_id_and_every_configuration_key(committing_client, db_session, lamp):
    state = await _state(committing_client, lamp)
    before = await _keys(db_session, lamp)
    draft = _draft(state)
    draft[0]["options"][0]["name"] = "crimson"
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    options = r.json()["variant_groups"][0]["options"]
    assert [(o["id"], o["name"]) for o in options] == [
        (lamp["options"]["red"], "crimson"),
        (lamp["options"]["blue"], "blue"),
    ]
    assert await _keys(db_session, lamp) == before
    assert r.json()["variants_revision"] != state["variants_revision"]


@pytest.mark.asyncio
async def test_a_new_group_records_its_chosen_standard_on_lines_and_positions(committing_client, db_session, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state) + [
        {
            "temp_id": "size",
            "name": "Size",
            "options": [{"temp_id": "s", "name": "S"}, {"temp_id": "l", "name": "L"}],
            "default": "l",
        }
    ]
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    size = next(g for g in r.json()["variant_groups"] if g["name"] == "Size")
    large = next(o["id"] for o in size["options"] if o["name"] == "L")
    assert size["default_option_id"] == large
    line_choice = await db_session.scalar(
        select(ProjectLineChoice.option_id).where(
            ProjectLineChoice.line_id == lamp["line"], ProjectLineChoice.group_id == size["id"]
        )
    )
    item_choice = await db_session.scalar(
        select(StockItemChoice.option_id).where(
            StockItemChoice.item_id == lamp["item"], StockItemChoice.group_id == size["id"]
        )
    )
    assert line_choice == large and item_choice == large


@pytest.mark.asyncio
async def test_two_options_may_swap_names(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    draft[0]["options"][0]["name"], draft[0]["options"][1]["name"] = "blue", "red"
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    assert {o["name"]: o["id"] for o in r.json()["variant_groups"][0]["options"]} == {
        "blue": lamp["options"]["red"],
        "red": lamp["options"]["blue"],
    }


@pytest.mark.asyncio
async def test_an_option_cannot_move_to_another_group(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    moved = draft[0]["options"].pop()
    draft.append({"temp_id": "g2", "name": "Other", "options": [moved], "default": moved["id"]})
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "option_moved"
    assert r.json()["detail"]["option"] == moved["id"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d[0]["options"].append(dict(d[0]["options"][0])),  # the same id twice
        lambda d: d.append({"temp_id": "x", "name": "A", "options": [{"temp_id": "x", "name": "a"}], "default": "x"}),
        lambda d: d[0]["options"][0].update(temp_id="both"),  # id AND temp_id
        lambda d: d[0]["options"].append({"name": "neither"}),  # neither
        lambda d: d.append({"temp_id": "empty", "name": "Empty", "options": [], "default": "none"}),
        lambda d: d[0].update(default="not-an-option"),
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_draft_is_refused_before_anything_is_written(committing_client, lamp, mutate):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    mutate(draft)
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "invalid_draft"
    assert (await _state(committing_client, lamp))["variants_revision"] == state["variants_revision"]


@pytest.mark.asyncio
async def test_a_refusal_half_way_writes_nothing(committing_client, lamp):
    state = await _state(committing_client, lamp)
    draft = _draft(state)
    draft[0]["name"] = "Renamed first"
    # «red» is the standard every line and the position recorded — removing it is refused.
    draft[0]["options"] = [o for o in draft[0]["options"] if o["id"] != lamp["options"]["red"]]
    draft[0]["default"] = lamp["options"]["blue"]
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["error"] == "option_in_use"
    assert r.json()["detail"]["option"] == lamp["options"]["red"]
    after = await _state(committing_client, lamp)
    assert after["variants_revision"] == state["variants_revision"]
    assert after["variant_groups"][0]["name"] == "Colour"


@pytest.mark.parametrize("foreign", ["rename", "standard", "reorder", "add", "delete", "apply", "mark"])
@pytest.mark.asyncio
async def test_a_stale_draft_is_refused_whatever_changed_meanwhile(committing_client, lamp, foreign):
    state = await _state(committing_client, lamp)
    base = f"{_url(lamp)}/variant-groups/{lamp['group']}"
    if foreign == "mark":
        r = await committing_client.patch(base, json={"requires_part": True})
    elif foreign == "rename":
        r = await committing_client.patch(f"{base}/options/{lamp['options']['blue']}", json={"name": "navy"})
    elif foreign == "standard":
        r = await committing_client.patch(base, json={"default_option_id": lamp["options"]["blue"]})
    elif foreign == "reorder":
        r = await committing_client.patch(f"{base}/options/{lamp['options']['blue']}", json={"position": -1})
    elif foreign == "add":
        r = await committing_client.post(f"{base}/options", json={"name": "green"})
    elif foreign == "delete":
        extra = await committing_client.post(f"{base}/options", json={"name": "green"})
        state = await _state(committing_client, lamp)
        green = next(o["id"] for o in extra.json()["variant_groups"][0]["options"] if o["name"] == "green")
        r = await committing_client.delete(f"{base}/options/{green}")
    else:
        draft = _draft(state)
        draft[0]["name"] = "Hue"
        r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    mine = _draft(state)
    mine[0]["name"] = "Mine"
    stale = await _put(committing_client, lamp, state["variants_revision"], mine)
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["error"] == "variants_changed"
    assert (await _state(committing_client, lamp))["variant_groups"][0]["name"] != "Mine"


@pytest.mark.asyncio
async def test_a_draft_without_changes_writes_nothing(committing_client, db_session, lamp):
    state = await _state(committing_client, lamp)
    events_before = await db_session.scalar(select(ProjectEvent.id).order_by(ProjectEvent.id.desc()).limit(1))
    updated_before = await db_session.scalar(select(Product.updated_at).where(Product.id == lamp["product"]))
    r = await _put(committing_client, lamp, state["variants_revision"], _draft(state))
    assert r.status_code == 200, r.text
    assert r.json()["variants_revision"] == state["variants_revision"]
    assert await db_session.scalar(select(ProjectEvent.id).order_by(ProjectEvent.id.desc()).limit(1)) == events_before
    db_session.expire_all()
    assert await db_session.scalar(select(Product.updated_at).where(Product.id == lamp["product"])) == updated_before


@pytest.mark.asyncio
async def test_a_draft_that_only_marks_a_group_writes_it_and_moves_no_configuration(
    committing_client, db_session, lamp
):
    state = await _state(committing_client, lamp)
    before = await _keys(db_session, lamp)
    draft = _draft(state)
    draft[0]["requires_part"] = True
    r = await _put(committing_client, lamp, state["variants_revision"], draft)
    assert r.status_code == 200, r.text
    assert r.json()["variant_groups"][0]["requires_part"] is True
    assert r.json()["variants_revision"] != state["variants_revision"]
    # No freeze, no re-key: the line and the position keep their configuration.
    assert await _keys(db_session, lamp) == before
    # Sent again unchanged, the mark is no change.
    again = await _put(committing_client, lamp, r.json()["variants_revision"], draft)
    assert again.status_code == 200 and again.json()["variants_revision"] == r.json()["variants_revision"]
    null = _draft(r.json())
    null[0]["requires_part"] = None
    assert (await _put(committing_client, lamp, r.json()["variants_revision"], null)).status_code == 422


@pytest.mark.asyncio
async def test_only_the_message_is_translated(committing_client, lamp):
    state = await _state(committing_client, lamp)
    set_language_cache("uk")
    try:
        r = await _put(committing_client, lamp, "stale", _draft(state))
    finally:
        set_language_cache("en")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "variants_changed"
    assert r.json()["detail"]["message"] != product_variants.VARIANTS_CHANGED  # Ukrainian


@pytest.mark.asyncio
async def test_a_groups_blockers_are_counted_like_its_delete_guard(committing_client, lamp):
    state = await _state(committing_client, lamp)
    group = state["variant_groups"][0]
    assert group["lines_count"] == 1  # the one line chose red
    assert group["stock_count"] == 1  # the one position holds red
    assert group["parts_count"] == 0
    refused = await committing_client.delete(f"{_url(lamp)}/variant-groups/{lamp['group']}")
    assert refused.status_code == 409 and "1 order lines" in refused.json()["detail"]


def test_the_revision_ignores_the_order_the_rows_were_loaded_in():
    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    a = Obj(id=1, name="red", position=0)
    b = Obj(id=2, name="blue", position=1)
    g1 = Obj(id=10, name="Colour", position=0, default_option_id=1, requires_part=False, options=[a, b])
    g2 = Obj(id=10, name="Colour", position=0, default_option_id=1, requires_part=False, options=[b, a])
    assert product_variants.revision([g1]) == product_variants.revision([g2])
    renamed = Obj(id=10, name="Hue", position=0, default_option_id=1, requires_part=False, options=[a, b])
    assert product_variants.revision([renamed]) != product_variants.revision([g1])
    # «Every value needs its own part» is part of what the client saw.
    marked = Obj(id=10, name="Colour", position=0, default_option_id=1, requires_part=True, options=[a, b])
    assert product_variants.revision([marked]) != product_variants.revision([g1])
