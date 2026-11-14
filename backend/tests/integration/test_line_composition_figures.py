"""Figures, reservation, stock and procurement follow the line's composition
(spec workshop-product-variants, rules 8–9, 15–16, 22)."""

import pytest
from sqlalchemy import select, update

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.line_config import ProjectLineChoice, ProjectLinePartCount
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project_line import ProjectLine
from backend.app.services import part_stock
from backend.tests.fixtures.part_bindings import bind

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipe(db_session):
    """«Pipe»: flask ×1 always; tail straight ×1 (standard) or angled ×1; 2 screws only with the angled tail."""
    product = Product(name="Pipe")
    db_session.add(product)
    await db_session.flush()
    group = ProductVariantGroup(product_id=product.id, name="Tail", position=0)
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
        "screw": ProductPart(
            product_id=product.id,
            kind="purchased",
            name="screw",
            name_key="purchased:screw",
            qty_per_unit=2,
            sort_order=3,
        ),
    }
    bind(parts["flask"])
    bind(parts["straight"], straight)
    bind(parts["angled"], angled)
    bind(parts["screw"], angled)
    db_session.add_all(parts.values())
    await db_session.commit()
    return {"product": product, "group": group, "straight": straight, "angled": angled, "parts": parts}


async def _order(client, db, pipe, *, angled_flask=None):
    """Two lines of 4: line 1 standard (straight), line 2 angled (flask ×2 when asked)."""
    gid = str(pipe["group"].id)
    angled = {"product_id": pipe["product"].id, "quantity": 4, "choices": {gid: pipe["angled"].id}}
    if angled_flask is not None:
        angled["part_counts"] = {str(pipe["parts"]["flask"].id): angled_flask}
    body = (
        await client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": pipe["product"].id, "quantity": 4}, angled]},
        )
    ).json()
    first, second = (line["id"] for line in body["lines"])
    return body["id"], first, second


async def _shelf(db, parts, **balances):
    for name, qty in balances.items():
        await part_stock.move(db, part_id=parts[name].id, delta=qty, reason="manual", note="counted")
    await db.commit()


def _parts(line):
    return {p["name"]: (p["qty_per_unit"], p["need"], p["usable"]) for p in line["parts"]}


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


@pytest.mark.asyncio
async def test_each_line_needs_its_own_parts(committing_client, db_session, pipe):
    pid, _, _ = await _order(committing_client, db_session, pipe, angled_flask=2)
    lines = (await committing_client.get(f"/api/v1/projects/{pid}")).json()["lines"]
    assert _parts(lines[0]) == {"flask": (1, 4, 0), "straight": (1, 4, 0)}
    assert _parts(lines[1]) == {"flask": (2, 8, 0), "angled": (1, 4, 0)}


@pytest.mark.asyncio
async def test_a_print_of_straight_tails_does_not_cover_the_angled_line(committing_client, db_session, pipe):
    # A print of straight tails filed under the ANGLED line: that line does not
    # count a straight tail, so it covers nothing there — the tails read as its
    # surplus (a per-unit 0 row, bankable), never as coverage.
    pid, first, second = await _order(committing_client, db_session, pipe)
    await _file(db_session, pid, second, straight=4)
    lines = (await committing_client.get(f"/api/v1/projects/{pid}")).json()["lines"]
    parts = _parts(lines[1])
    assert parts.pop("straight") == (0, 0, 4)
    assert set(parts) == {"flask", "angled"}
    assert all(usable == 0 for _per, _need, usable in parts.values())
    assert lines[1]["covered_units"] == 0
    await _file(db_session, pid, first, straight=4, flask=4)
    lines = (await committing_client.get(f"/api/v1/projects/{pid}")).json()["lines"]
    assert lines[0]["covered_units"] == 4


@pytest.mark.asyncio
async def test_kits_of_the_catalog_are_the_standard_configuration(committing_client, db_session, pipe):
    await _shelf(db_session, pipe["parts"], flask=3, straight=3)
    rows = (await committing_client.get("/api/v1/products/")).json()
    assert next(r for r in rows if r["id"] == pipe["product"].id)["kits_available"] == 3


@pytest.mark.asyncio
async def test_reserving_a_line_takes_its_own_composition(committing_client, db_session, pipe):
    pid, _, second = await _order(committing_client, db_session, pipe, angled_flask=2)
    await _shelf(db_session, pipe["parts"], flask=2, straight=5, angled=1)
    r = await committing_client.patch(f"/api/v1/projects/{pid}/lines/{second}", json={"from_stock_units": 1})
    assert r.status_code == 200, r.text
    line = next(ln for ln in r.json()["lines"] if ln["id"] == second)
    assert line["from_stock_units"] == 1
    balances = await part_stock.balances(db_session, pipe["product"].id)
    assert balances[pipe["parts"]["flask"].id] == 0  # 2 per unit
    assert balances[pipe["parts"]["straight"].id] == 5  # not in the angled kit
    assert balances[pipe["parts"]["angled"].id] == 0


@pytest.mark.asyncio
async def test_the_stock_tab_reads_reservations_per_line_composition(committing_client, db_session, pipe):
    pid, _, second = await _order(committing_client, db_session, pipe, angled_flask=2)
    await _shelf(db_session, pipe["parts"], flask=2, angled=1)
    await committing_client.patch(f"/api/v1/projects/{pid}/lines/{second}", json={"from_stock_units": 1})
    rows = (await committing_client.get("/api/v1/stock")).json()["products"]
    row = next(r for r in rows if r["id"] == pipe["product"].id)
    assert [(h["line_id"], h["kits"]) for h in row["reservations"]] == [(second, 1)]


@pytest.mark.asyncio
async def test_parts_mode_figures(committing_client, db_session, pipe):
    pid, first, _ = await _order(committing_client, db_session, pipe)
    await db_session.execute(update(ProjectLine).where(ProjectLine.id == first).values(mode="parts", quantity=1))
    await db_session.execute(ProjectLineChoice.__table__.delete().where(ProjectLineChoice.line_id == first))
    db_session.add_all(
        [
            ProjectLinePartCount(line_id=first, part_id=pipe["parts"]["angled"].id, qty=3),
            ProjectLinePartCount(line_id=first, part_id=pipe["parts"]["flask"].id, qty=1),
        ]
    )
    await db_session.commit()
    await _file(db_session, pid, first, angled=2)
    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    line = next(ln for ln in body["lines"] if ln["id"] == first)
    assert line["quantity"] == 4 and line["covered_units"] == 2
    assert _parts(line) == {"flask": (1, 1, 0), "angled": (3, 3, 2)}
    assert body["figures"]["ordered"] == 8  # 4 parts + 4 units of the other line


@pytest.mark.asyncio
async def test_procurement_follows_the_configuration(committing_client, db_session, pipe):
    pid, _, _ = await _order(committing_client, db_session, pipe)
    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    screw = next(p for p in body["procurement"] if p["name"] == "screw")
    assert screw["need"] == 8  # 2 per unit × the angled line's 4 only


@pytest.mark.asyncio
async def test_a_line_with_no_choice_row_reads_the_standard_option(committing_client, db_session, pipe):
    # A line that predates its product's first group (the writer backfills it;
    # this pins the reader's fallback for the moment in between).
    body = (
        await committing_client.post(
            "/api/v1/projects/", json={"name": "O", "lines": [{"product_id": pipe["product"].id, "quantity": 2}]}
        )
    ).json()
    # The API records the standard choice; take it away to stand in that moment.
    await db_session.execute(ProjectLineChoice.__table__.delete())
    await db_session.commit()
    line = (await committing_client.get(f"/api/v1/projects/{body['id']}")).json()["lines"][0]
    assert set(_parts(line)) == {"flask", "straight"}
