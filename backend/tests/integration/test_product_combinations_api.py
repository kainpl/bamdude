"""The two read routes of a product's combinations (spec product-part-variant-combinations):
the table of every combination with its availability, and the state of one choice for the
order dialogs — each option marked available with the other groups unchanged and available
in any combination at all.

The jar is the gate's own (``test_combination_gate_api``): a weight (400 standard, 600, 1200),
a kind (solid standard, mesh) and a lid (screw standard, snap); bodies for solid 400 and 600,
the screw lid for 400 and 600, the snap lid for any weight. Open: solid with 400 or 600 and
either lid — four of the twelve.
"""

import itertools
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.services import combination_gate
from backend.tests.fixtures.part_bindings import bind
from backend.tests.fixtures.sliced_sources import slice_parts
from backend.tests.integration.test_combination_gate_api import jar  # noqa: F401 — the gate's jar, by name

pytestmark = pytest.mark.integration


async def _groups(
    db, product_id: int, spec, *, marked: frozenset[str] = frozenset()
) -> list[tuple[ProductVariantGroup, list[ProductVariantOption]]]:
    """Groups ``spec`` = ``[(name, [option names]), …]`` in that order; the first option standard.
    The groups named in ``marked`` need a part for every value (``requires_part``)."""
    made = []
    for position, (name, values) in enumerate(spec):
        group = ProductVariantGroup(product_id=product_id, name=name, position=position, requires_part=name in marked)
        db.add(group)
        await db.flush()
        options = [ProductVariantOption(group_id=group.id, name=v, position=i) for i, v in enumerate(values)]
        db.add_all(options)
        await db.flush()
        group.default_option_id = options[0].id
        made.append((group, options))
    return made


@pytest.fixture
async def six_by_five(db_session):
    """Six groups of five values — 15 625 combinations, past ``MAX_ENUMERATED``. One part,
    sliced, is bound to the first group's first value: that group, marked, discriminates;
    the others are descriptive."""
    product = Product(name="Six by five")
    db_session.add(product)
    await db_session.flush()
    groups = await _groups(
        db_session, product.id, [(f"G{g}", [f"v{g}{o}" for o in range(5)]) for g in range(6)], marked=frozenset({"G0"})
    )
    part = bind(
        ProductPart(product_id=product.id, kind="printed", name="core", name_key="core", qty_per_unit=1),
        groups[0][1][0],
    )
    db_session.add(part)
    await db_session.flush()
    await slice_parts(db_session, product.id, [part])
    await db_session.commit()
    return SimpleNamespace(id=product.id, groups=[(g.id, [o.id for o in options]) for g, options in groups])


@pytest.fixture
async def legacy(db_session):
    """A group with no standard value (a legacy product): «Colour» red / blue, marked, and a
    sliced red shell — the only part, bound to red."""
    product = Product(name="Legacy")
    db_session.add(product)
    await db_session.flush()
    ((group, (red, blue)),) = await _groups(
        db_session, product.id, [("Colour", ["red", "blue"])], marked=frozenset({"Colour"})
    )
    group.default_option_id = None
    shell = bind(
        ProductPart(product_id=product.id, kind="printed", name="red shell", name_key="red shell", qty_per_unit=1),
        red,
    )
    db_session.add(shell)
    await db_session.flush()
    await slice_parts(db_session, product.id, [shell])
    await db_session.commit()
    return SimpleNamespace(id=product.id, colour=group.id, red=red.id, blue=blue.id)


@pytest.fixture
async def other_product_option(db_session):
    """An option of ANOTHER product, and its group."""
    product = Product(name="Other")
    db_session.add(product)
    await db_session.flush()
    ((group, (large,)),) = await _groups(db_session, product.id, [("Size", ["L"])])
    await db_session.commit()
    return SimpleNamespace(group=group.id, option=large.id)


def _table_url(product_id: int) -> str:
    return f"/api/v1/products/{product_id}/combinations"


def _state_url(product_id: int) -> str:
    return f"/api/v1/products/{product_id}/combination"


def _names(item: dict) -> tuple[str, ...]:
    return tuple(choice["option"] for choice in item["choices"])


@pytest.mark.asyncio
async def test_table_lists_every_combination_with_its_reason(async_client, jar):  # noqa: F811
    r = await async_client.get(_table_url(jar.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["total"], body["page"]) == (2 * 2 * 3, 1)
    items = body["items"]
    # Groups by position, values by position, the last group fastest.
    assert [_names(i) for i in items[:4]] == [
        ("400", "solid", "screw"),
        ("400", "solid", "snap"),
        ("400", "mesh", "screw"),
        ("400", "mesh", "snap"),
    ]
    assert items[0]["choices"] == [
        {"group_id": jar.weight, "group": "Weight", "option_id": jar.w400, "option": "400"},
        {"group_id": jar.kind, "group": "Kind", "option_id": jar.solid, "option": "solid"},
        {"group_id": jar.lid, "group": "Lid", "option_id": jar.screw, "option": "screw"},
    ]
    assert [i["available"] for i in items] == [True, True, False, False] * 2 + [False] * 4
    assert all(i["reasons"] == [] for i in items if i["available"])
    closed = [i for i in items if not i["available"]]
    assert all(i["reasons"] for i in closed)
    # The reason is the one the 409 refusal carries for the same choice.
    assert items[2]["reasons"] == [{"code": "option_without_part", "group": "Kind", "option": "mesh", "part": None}]


@pytest.mark.asyncio
async def test_table_filters_by_state_and_counts_both_kinds(async_client, jar):  # noqa: F811
    """The page's two tabs: the filter keeps one kind, ``total`` and the pages follow it, and
    both counts are answered whatever the filter."""
    everything = (await async_client.get(_table_url(jar.id))).json()
    assert (everything["available_total"], everything["unavailable_total"]) == (4, 8)
    open_ = (await async_client.get(f"{_table_url(jar.id)}?state=available")).json()
    assert (open_["total"], open_["available_total"], open_["unavailable_total"]) == (4, 4, 8)
    assert [_names(i) for i in open_["items"]] == [
        ("400", "solid", "screw"),
        ("400", "solid", "snap"),
        ("600", "solid", "screw"),
        ("600", "solid", "snap"),
    ]
    closed = (await async_client.get(f"{_table_url(jar.id)}?state=unavailable&page=2&page_size=5")).json()
    assert (closed["total"], closed["page"], len(closed["items"])) == (8, 2, 3)
    assert all(not i["available"] and i["reasons"] for i in closed["items"])
    past = (await async_client.get(f"{_table_url(jar.id)}?state=available&page=2")).json()
    assert (past["total"], past["items"], past["available_total"]) == (4, [], 4)
    assert (await async_client.get(f"{_table_url(jar.id)}?state=some")).status_code == 422


@pytest.mark.asyncio
async def test_a_table_too_large_to_count_is_answered_unfiltered(async_client, six_by_five):
    body = (await async_client.get(f"{_table_url(six_by_five.id)}?state=unavailable&page_size=10")).json()
    assert (body["total"], len(body["items"])) == (15625, 10)
    assert (body["available_total"], body["unavailable_total"]) == (None, None)


@pytest.mark.asyncio
async def test_table_reads_a_trashed_source_as_unsliced(async_client, db_session, jar):  # noqa: F811
    f = await db_session.get(LibraryFile, jar.files["body_400"])
    f.deleted_at = datetime.now()
    await db_session.commit()
    items = (await async_client.get(_table_url(jar.id))).json()["items"]
    assert items[0]["available"] is False
    assert items[0]["reasons"] == [{"code": "part_not_sliced", "group": None, "option": None, "part": "body 400 solid"}]
    assert items[4]["available"] is True  # 600 solid screw keeps its own body


@pytest.mark.asyncio
async def test_table_pages_a_large_product(async_client, six_by_five):  # Review Focus 2
    r = await async_client.get(f"{_table_url(six_by_five.id)}?page=2&page_size=500")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 15625 and body["page"] == 2 and len(body["items"]) == 500
    # Combination 500 is the third group's fifth value, every other group at its first (500 = 4·5³).
    first = [choice["option_id"] for choice in body["items"][0]["choices"]]
    assert first == [options[4 if g == 2 else 0] for g, (_gid, options) in enumerate(six_by_five.groups)]
    last_page = (await async_client.get(f"{_table_url(six_by_five.id)}?page=32&page_size=500")).json()
    assert (last_page["total"], len(last_page["items"])) == (15625, 125)
    past = (await async_client.get(f"{_table_url(six_by_five.id)}?page=40&page_size=500")).json()
    assert (past["total"], past["items"]) == (15625, [])
    too_big = await async_client.get(f"{_table_url(six_by_five.id)}?page_size=501")
    assert too_big.status_code == 422


@pytest.mark.asyncio
async def test_table_seeks_a_deep_page_without_walking_to_it(async_client, db_session):
    """5³⁰ combinations: the last page is decoded from its number — skipping to it one
    combination at a time would never end (and ``islice`` refuses a start that large)."""
    product = Product(name="Thirty groups")
    db_session.add(product)
    await db_session.flush()
    groups = await _groups(db_session, product.id, [(f"G{g}", [f"v{o}" for o in range(5)]) for g in range(30)])
    await db_session.commit()
    total = 5**30
    page = -(-total // 500)
    r = await async_client.get(f"{_table_url(product.id)}?page={page}&page_size=500")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["total"], body["page"], len(body["items"])) == (total, page, total - (page - 1) * 500)
    assert [c["option_id"] for c in body["items"][-1]["choices"]] == [options[-1].id for _g, options in groups]
    assert all(i["available"] for i in body["items"])  # no parts: nothing discriminates


def test_a_seek_gives_what_a_walk_gives_from_every_start():
    values = [["a0", "a1"], ["b0", "b1", "b2"], ["c0"], ["d0", "d1", "d2", "d3"]]
    walk = list(itertools.product(*values))
    for start in range(len(walk)):
        assert list(combination_gate._from_index(values, start)) == walk[start:]


@pytest.mark.asyncio
async def test_describe_marks_here_and_anywhere(async_client, jar):  # noqa: F811
    r = await async_client.post(_state_url(jar.id), json={"choices": {str(jar.weight): jar.w1200}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    # 1200 is named by no part at all — the one reason said, as the 409 says it.
    assert body["reasons"] == [{"code": "option_without_part", "group": "Weight", "option": "1200", "part": None}]
    flags = {o["option_id"]: (o["available_here"], o["available_anywhere"]) for o in body["options"]}
    assert flags[jar.w400] == (True, True) and flags[jar.mesh] == (False, False)
    assert flags == {
        jar.w400: (True, True),
        jar.w600: (True, True),
        jar.w1200: (False, False),
        jar.solid: (False, True),
        jar.mesh: (False, False),
        jar.screw: (False, True),
        jar.snap: (False, True),
    }
    # Every option of every group, in the table's order, with its group.
    assert [(o["group_id"], o["option_id"]) for o in body["options"]] == [
        (jar.weight, jar.w400),
        (jar.weight, jar.w600),
        (jar.weight, jar.w1200),
        (jar.kind, jar.solid),
        (jar.kind, jar.mesh),
        (jar.lid, jar.screw),
        (jar.lid, jar.snap),
    ]


@pytest.mark.asyncio
async def test_describe_without_choices_is_the_standard(async_client, jar):  # noqa: F811
    body = (await async_client.post(_state_url(jar.id), json={})).json()
    assert (body["available"], body["reasons"]) == (True, [])
    flags = {o["option_id"]: o["available_here"] for o in body["options"]}
    assert flags == {
        jar.w400: True,
        jar.w600: True,
        jar.w1200: False,
        jar.solid: True,
        jar.mesh: False,
        jar.screw: True,
        jar.snap: True,
    }


@pytest.mark.asyncio
async def test_anywhere_is_null_past_the_cap(async_client, six_by_five):
    assert 5**6 > combination_gate.MAX_ENUMERATED == 2000
    r = await async_client.post(_state_url(six_by_five.id), json={"choices": {}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["available"], body["reasons"]) == (True, [])
    assert {o["available_anywhere"] for o in body["options"]} == {None}
    here = {o["option_id"]: o["available_here"] for o in body["options"]}
    (first_group, first_options), *rest = six_by_five.groups
    # Only the bound value of the discriminating group keeps the core in the kit.
    assert [here[oid] for oid in first_options] == [True, False, False, False, False]
    assert all(here[oid] for _gid, options in rest for oid in options)
    assert len(body["options"]) == 30


@pytest.mark.asyncio
async def test_unchosen_group_without_a_standard_names_no_value(async_client, legacy):
    body = (await async_client.post(_state_url(legacy.id), json={})).json()
    assert body["available"] is False
    assert body["reasons"] == [{"code": "option_without_part", "group": "Colour", "option": None, "part": None}]
    flags = {o["option_id"]: (o["available_here"], o["available_anywhere"]) for o in body["options"]}
    assert flags == {legacy.red: (True, True), legacy.blue: (False, False)}
    # The table enumerates the group's real values only.
    table = (await async_client.get(_table_url(legacy.id))).json()
    assert [(_names(i), i["available"]) for i in table["items"]] == [(("red",), True), (("blue",), False)]


@pytest.mark.asyncio
async def test_foreign_option_in_choices_is_422(async_client, jar, other_product_option):  # noqa: F811
    for choices in (
        {str(jar.weight): other_product_option.option},
        {str(other_product_option.group): other_product_option.option},
        {str(jar.kind): jar.w400},  # this product's option, under another group
    ):
        r = await async_client.post(_state_url(jar.id), json={"choices": choices})
        assert r.status_code == 422, r.text
        assert r.json()["detail"] == "That option does not belong to this product"


@pytest.mark.asyncio
async def test_product_without_groups_is_not_computed(async_client, db_session, other_product_option):
    product = Product(name="Plain")
    db_session.add(product)
    await db_session.flush()
    # A printed part without any source: not subject to the check (п. 12).
    db_session.add(bind(ProductPart(product_id=product.id, kind="printed", name="x", name_key="x", qty_per_unit=1)))
    await db_session.commit()
    table = await async_client.get(_table_url(product.id))
    assert table.status_code == 200, table.text
    assert table.json() == {"total": 0, "page": 1, "items": [], "available_total": 0, "unavailable_total": 0}
    state = await async_client.post(_state_url(product.id), json={"choices": {}})
    assert state.status_code == 200, state.text
    assert state.json() == {"available": True, "reasons": [], "options": []}
    # Its choices are still validated: it has no value at all.
    foreign = await async_client.post(
        _state_url(product.id), json={"choices": {str(other_product_option.group): other_product_option.option}}
    )
    assert foreign.status_code == 422, foreign.text


@pytest.mark.asyncio
async def test_unknown_product_is_404(async_client):
    for r in (
        await async_client.get(_table_url(999999)),
        await async_client.post(_state_url(999999), json={"choices": {}}),
    ):
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "Product not found"


def _gates(dependant) -> list[tuple[str, tuple[str, ...]]]:
    """The permission gates a route's dependencies ask, read off their closures."""
    out = []
    for dep in dependant.dependencies:
        call = dep.call
        names = getattr(getattr(call, "__code__", None), "co_freevars", ())
        cells = dict(zip(names, getattr(call, "__closure__", None) or (), strict=False))
        if "perm_strings" in cells:
            out.append((call.__qualname__, tuple(cells["perm_strings"].cell_contents)))
        elif "all_perm" in cells:
            out.append((call.__qualname__, (cells["all_perm"].cell_contents,)))
        out += _gates(dep)
    return out


def _route_gates(method: str, path: str) -> list[tuple[str, tuple[str, ...]]]:
    from backend.app.main import app

    (route,) = [r for r in app.routes if isinstance(r, APIRoute) and r.path == path and method in r.methods]
    return _gates(route.dependant)


def test_the_table_and_the_state_ask_what_the_product_asks():
    """Both answer facts about the product's structure — its own read (owner, 2026-11-14), so
    an order manager without stock rights still sees the marks."""
    base = "/api/v1/products/{product_id}"
    assert _route_gates("GET", f"{base}/combinations") == _route_gates("GET", base)
    assert _route_gates("POST", f"{base}/combination") == _route_gates("GET", base)
    assert _route_gates("GET", base)
    assert _route_gates("POST", f"{base}/combination") != _route_gates("GET", f"{base}/kits")
