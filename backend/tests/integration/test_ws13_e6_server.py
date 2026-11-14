"""WS-13 E6 server additions (vault 60-specs/workshop-ui-parity-e06-order-forms-actions-issue, G01–G03, H01–H04).

G — the order form's limits live in the API: a name is trimmed BEFORE its length is
checked, a blank one is refused, colour and url carry their columns' lengths, and the
duplicate's own generated name never outgrows the 255-character column.

H — reads the forms draw from: a part's bankable surplus (the number ``bank-surplus``
moves), the list row's bankable total, the units of the dispatch note a batch sealed,
and each fulfilment line's configuration and stock position.
"""

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.customer import Customer
from backend.app.models.product import Product, ProductPart
from backend.app.models.product_variant import ProductVariantGroup, ProductVariantOption
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.stock_issue import StockIssue
from backend.app.services import finished_stock, line_config
from backend.tests.fixtures.sliced_sources import slice_parts
from backend.tests.unit.services.test_product_composition import counting_statements

pytestmark = pytest.mark.integration


# ---------- G01–G03: the order form's limits ----------


@pytest.mark.asyncio
async def test_an_order_name_is_trimmed_before_its_length_is_checked(committing_client):
    blank = await committing_client.post("/api/v1/projects/", json={"name": "   "})
    assert blank.status_code == 422, blank.text

    too_long = await committing_client.post("/api/v1/projects/", json={"name": "x" * 256})
    assert too_long.status_code == 422, too_long.text

    # 255 characters with spaces around them fit: the spaces are not the name.
    padded = await committing_client.post("/api/v1/projects/", json={"name": "  " + "y" * 255 + " "})
    assert padded.status_code == 200, padded.text
    assert padded.json()["name"] == "y" * 255

    r = await committing_client.post("/api/v1/projects/", json={"name": "  Lamps  "})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Lamps"


@pytest.mark.asyncio
async def test_an_order_rename_keeps_the_same_name_rules(committing_client):
    oid = (await committing_client.post("/api/v1/projects/", json={"name": "Lamps"})).json()["id"]
    url = f"/api/v1/projects/{oid}"

    assert (await committing_client.patch(url, json={"name": "   "})).status_code == 422
    assert (await committing_client.patch(url, json={"name": "z" * 256})).status_code == 422
    # An explicit null stays refused; an omitted name stays untouched.
    assert (await committing_client.patch(url, json={"name": None})).status_code == 422
    untouched = await committing_client.patch(url, json={"priority": "high"})
    assert untouched.status_code == 200 and untouched.json()["name"] == "Lamps"

    renamed = await committing_client.patch(url, json={"name": "  Shades "})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Shades"


@pytest.mark.asyncio
async def test_colour_and_url_carry_their_column_lengths(committing_client):
    long_color = "#" + "a" * 20
    long_url = "https://example.com/" + "a" * (2049 - len("https://example.com/"))
    assert len(long_url) == 2049

    assert (
        await committing_client.post("/api/v1/projects/", json={"name": "C", "color": long_color})
    ).status_code == 422
    assert (await committing_client.post("/api/v1/projects/", json={"name": "U", "url": long_url})).status_code == 422

    oid = (await committing_client.post("/api/v1/projects/", json={"name": "Ok", "color": "#4eac48"})).json()["id"]
    url = f"/api/v1/projects/{oid}"
    assert (await committing_client.patch(url, json={"color": long_color})).status_code == 422
    assert (await committing_client.patch(url, json={"url": long_url})).status_code == 422
    fits = await committing_client.patch(url, json={"url": long_url[:2048]})
    assert fits.status_code == 200, fits.text


@pytest.mark.asyncio
async def test_a_duplicate_name_is_trimmed_and_bounded(committing_client):
    oid = (await committing_client.post("/api/v1/projects/", json={"name": "Lamps"})).json()["id"]
    url = f"/api/v1/projects/{oid}/duplicate"

    assert (await committing_client.post(url, json={"name": "q" * 256})).status_code == 422
    named = await committing_client.post(url, json={"name": "  Lamps again  "})
    assert named.status_code == 200, named.text
    assert named.json()["name"] == "Lamps again"


@pytest.mark.asyncio
async def test_the_generated_copy_name_fits_the_column(committing_client):
    """R05: the generator adds its suffix AFTER validation, so it reserves the room itself —
    a 255-character original, and the collision number after it, still fit."""
    base = "n" * 255
    oid = (await committing_client.post("/api/v1/projects/", json={"name": base})).json()["id"]
    url = f"/api/v1/projects/{oid}/duplicate"

    names = []
    for body in (None, {"name": None}, {"name": "   "}):
        r = await committing_client.post(url, json=body) if body is not None else await committing_client.post(url)
        assert r.status_code == 200, r.text
        names.append(r.json()["name"])

    assert all(len(name) <= 255 for name in names), [len(n) for n in names]
    assert names[0].endswith(" (Copy)")
    assert names[1].endswith(" (Copy 2)")
    assert names[2].endswith(" (Copy 3)")
    assert len(set(names)) == 3


# ---------- H01 / H02: bankable surplus ----------


async def _overprinted_order(db, *, quantity: int = 2, printed: int = 5) -> dict:
    """«Pipe» needs one flask per unit; ``printed`` flasks were printed for ``quantity``."""
    pipe = Product(name="Pipe")
    db.add(pipe)
    await db.flush()
    flask = ProductPart(product_id=pipe.id, kind="printed", name="flask", name_key="flask", qty_per_unit=1)
    db.add(flask)
    project = Project(name="Over")
    db.add(project)
    await db.flush()
    line = ProjectLine(project_id=project.id, product_id=pipe.id, quantity=quantity)
    db.add(line)
    await db.flush()
    await line_config.seed_line(db, line, choices=None, counts=None)
    archive = PrintArchive(
        project_id=project.id,
        project_line_id=line.id,
        filename="pipe",
        file_path="",
        file_size=0,
        status="completed",
        quantity=1,
    )
    db.add(archive)
    await db.flush()
    db.add(PrintArchivePart(archive_id=archive.id, name="flask", name_key="flask", quantity=printed))
    await db.commit()
    return {"id": project.id, "line_id": line.id, "part_id": flask.id}


@pytest.mark.asyncio
async def test_a_part_says_how_much_of_its_surplus_is_bankable(committing_client, db_session):
    order = await _overprinted_order(db_session, quantity=2, printed=5)
    url = f"/api/v1/projects/{order['id']}"

    body = (await committing_client.get(url)).json()
    [part] = body["lines"][0]["parts"]
    assert (part["surplus"], part["bankable"]) == (3, 3)
    assert body["figures"]["bankable_surplus"] == 3

    banked = await committing_client.post(f"{url}/bank-surplus")
    assert banked.status_code == 200, banked.text
    # The number the dialog showed is the number the button moved.
    assert {m["part_id"]: m["delta"] for m in banked.json()["moved"]} == {order["part_id"]: 3}

    after = (await committing_client.get(url)).json()
    [part] = after["lines"][0]["parts"]
    assert part["bankable"] == 0
    assert after["figures"]["bankable_surplus"] == 0


async def _list(client) -> dict[int, dict]:
    r = await client.get("/api/v1/projects/", params={"page": 1, "per_page": 50})
    assert r.status_code == 200, r.text
    return {row["id"]: row for row in r.json()["items"]}


@pytest.mark.asyncio
async def test_a_list_row_carries_its_bankable_surplus(committing_client, db_session):
    over = await _overprinted_order(db_session, quantity=2, printed=5)
    exact = await _overprinted_order(db_session, quantity=5, printed=5)

    rows = await _list(committing_client)

    assert rows[over["id"]]["bankable_surplus"] == 3
    assert rows[exact["id"]]["bankable_surplus"] == 0


@pytest.mark.asyncio
async def test_the_list_reads_bankable_surplus_in_a_fixed_number_of_statements(
    committing_client, db_session, test_engine
):
    await _overprinted_order(db_session)
    await _list(committing_client)  # one-off auth questions first
    with counting_statements(test_engine) as one:
        await _list(committing_client)

    for _ in range(19):
        await _overprinted_order(db_session)
    with counting_statements(test_engine) as twenty:
        await _list(committing_client)

    assert len(twenty) == len(one)


# ---------- H03: the units of the sealed dispatch note ----------


async def _issuable_order(db) -> dict:
    """Two ready units of «Lamp» held for Acme's order of five."""
    lamp = Product(name="Lamp")
    acme = Customer(name="Acme")
    db.add_all([lamp, acme])
    await db.flush()
    position = await finished_stock.item_for(db, lamp.id, {}, create=True)
    await finished_stock.receive(db, position, 2)
    project = Project(name="Issue", customer_id=acme.id)
    db.add(project)
    await db.flush()
    line = ProjectLine(project_id=project.id, product_id=lamp.id, quantity=5)
    db.add(line)
    await db.flush()
    await line_config.seed_line(db, line, choices=None, counts=None)
    assert await finished_stock.reserve_for_line(db, line, 2) == 2
    await db.commit()
    return {"id": project.id, "line_id": line.id}


@pytest.mark.asyncio
async def test_a_batch_answers_the_units_of_its_dispatch_note(committing_client, db_session):
    order = await _issuable_order(db_session)
    url = f"/api/v1/projects/{order['id']}/fulfilment"

    r = await committing_client.post(url, json={"lines": [{"line_id": order["line_id"], "issue": 2}]})
    assert r.status_code == 200, r.text
    out = r.json()
    issue = await db_session.get(StockIssue, out["issue_id"])
    assert out["issue_units"] == issue.units == 2


@pytest.mark.asyncio
async def test_a_batch_without_an_issue_has_no_units(committing_client, db_session):
    order = await _overprinted_order(db_session, quantity=5, printed=2)
    url = f"/api/v1/projects/{order['id']}/fulfilment"

    r = await committing_client.post(url, json={"lines": [{"line_id": order["line_id"], "receive": 2}]})
    assert r.status_code == 200, r.text
    assert (r.json()["issue_id"], r.json()["issue_units"]) == (None, None)


@pytest.mark.asyncio
async def test_apply_still_refuses_above_the_state_and_writes_nothing(committing_client, db_session):
    """I3: the H04 split of `state` (one context per GET) leaves `apply` as it was — a number
    above what the state allows is a 409 sentence, nothing moves, and the state reads the same."""
    order = await _issuable_order(db_session)
    url = f"/api/v1/projects/{order['id']}/fulfilment"
    before = (await committing_client.get(url)).json()

    r = await committing_client.post(url, json={"lines": [{"line_id": order["line_id"], "issue": 3}]})
    assert r.status_code == 409, r.text
    assert (await committing_client.get(url)).json() == before

    r = await committing_client.post(url, json={"lines": [{"line_id": order["line_id"], "issue": 2}]})
    assert r.status_code == 200, r.text
    after = (await committing_client.get(url)).json()
    (line,) = [entry for entry in after["lines"] if entry["line_id"] == order["line_id"]]
    assert (line["held"], line["issued"]) == (0, 2)


# ---------- H04: a fulfilment line's configuration and stock position ----------


async def _configured_order(db, *, extra_lines: int = 0) -> dict:
    """Two lines of «Diffuser» in different colours (one with a stock position at «A-3»),
    a parts line of the same product, and ``extra_lines`` more plain lines."""
    diffuser = Product(name="Diffuser")
    db.add(diffuser)
    await db.flush()
    colour = ProductVariantGroup(product_id=diffuser.id, name="Colour", position=0)
    db.add(colour)
    await db.flush()
    white = ProductVariantOption(group_id=colour.id, name="white", position=0)
    amber = ProductVariantOption(group_id=colour.id, name="amber", position=1)
    db.add_all([white, amber])
    await db.flush()
    colour.default_option_id = white.id
    shade = ProductPart(product_id=diffuser.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1)
    db.add(shade)
    await db.flush()
    # A variant product's line needs its kit sliced (spec product-part-variant-combinations, п. 10).
    await slice_parts(db, diffuser.id, [shade])

    project = Project(name="Diffusers")
    db.add(project)
    await db.flush()
    lines = []
    for choice in (white.id, amber.id):
        line = ProjectLine(project_id=project.id, product_id=diffuser.id, quantity=4)
        db.add(line)
        await db.flush()
        await line_config.seed_line(db, line, choices={colour.id: choice}, counts=None)
        lines.append(line)
    parts_line = ProjectLine(project_id=project.id, product_id=diffuser.id, quantity=1, mode="parts")
    db.add(parts_line)
    await db.flush()
    await line_config.seed_line(db, parts_line, choices=None, counts={shade.id: 2})
    for _ in range(extra_lines):
        line = ProjectLine(project_id=project.id, product_id=diffuser.id, quantity=1)
        db.add(line)
        await db.flush()
        await line_config.seed_line(db, line, choices=None, counts=None)

    position = await finished_stock.item_for(db, diffuser.id, {colour.id: amber.id}, create=True)
    position.location = "A-3"
    await db.commit()
    return {
        "id": project.id,
        "white": lines[0].id,
        "amber": lines[1].id,
        "parts": parts_line.id,
        "position": position.id,
    }


@pytest.mark.asyncio
async def test_a_fulfilment_line_names_its_configuration_and_stock_position(committing_client, db_session):
    order = await _configured_order(db_session)

    r = await committing_client.get(f"/api/v1/projects/{order['id']}/fulfilment")
    assert r.status_code == 200, r.text
    by_id = {row["line_id"]: row for row in r.json()["lines"]}

    white, amber = by_id[order["white"]], by_id[order["amber"]]
    # Two lines of one product are told apart by what they chose.
    assert [c["option_name"] for c in white["configuration"]["choices"]] == ["white"]
    assert [c["option_name"] for c in amber["configuration"]["choices"]] == ["amber"]
    assert white["configuration"]["choices"][0]["is_default"] is True
    assert amber["configuration"]["choices"][0]["is_default"] is False

    assert amber["stock_position"] == {
        "id": order["position"],
        "code": f"SK-{order['position']:04d}",
        "location": "A-3",
    }
    # No position for the white configuration yet; a parts line has none at all.
    assert white["stock_position"] is None
    assert by_id[order["parts"]]["stock_position"] is None


@pytest.mark.asyncio
async def test_the_fulfilment_state_reads_in_a_fixed_number_of_statements(committing_client, db_session, test_engine):
    small = await _configured_order(db_session)
    big = await _configured_order(db_session, extra_lines=3)
    await committing_client.get(f"/api/v1/projects/{small['id']}/fulfilment")  # warm-up
    with counting_statements(test_engine) as few:
        await committing_client.get(f"/api/v1/projects/{small['id']}/fulfilment")
    with counting_statements(test_engine) as more:
        await committing_client.get(f"/api/v1/projects/{big['id']}/fulfilment")

    assert len(more) == len(few)


@pytest.mark.asyncio
async def test_a_position_of_another_product_is_not_the_lines(committing_client, db_session):
    """The position is matched by product AND configuration key, never by key alone."""
    order = await _configured_order(db_session)
    other = Product(name="Other")
    db_session.add(other)
    await db_session.flush()
    stranger = await finished_stock.item_for(db_session, other.id, {}, create=True)
    stranger.location = "Z-9"
    await db_session.commit()

    r = await committing_client.get(f"/api/v1/projects/{order['id']}/fulfilment")
    rows = {row["line_id"]: row for row in r.json()["lines"]}
    assert rows[order["white"]]["stock_position"] is None
