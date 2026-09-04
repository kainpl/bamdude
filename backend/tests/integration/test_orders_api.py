"""Orders: lines, figures, procurement, lifecycle, links — spec §5/§8 + §Order lifecycle."""

import json
from collections import Counter

import pytest
from sqlalchemy import delete, select

from backend.app.api.routes import projects as projects_routes
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.customer import Customer
from backend.app.models.library import LibraryFile
from backend.app.models.macro import Macro
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.print_options_preference import PrintOptionsPreference
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.user import User
from backend.app.schemas.print_options_preference import PrintOptionsPreferenceData
from backend.app.services import part_stock, plan_engine
from backend.app.services.order_metrics import attribute, load_order_context
from backend.app.services.plan_engine import queued_yield_by_line
from backend.app.services.product_composition import recipes_for_product
from backend.tests.unit.services.test_order_metrics import build_parity_fixture
from backend.tests.unit.services.test_product_composition import counting_statements

pytestmark = pytest.mark.integration

# ``committing_client``, not ``async_client``: these handlers never commit —
# production's ``get_db`` does it after the response. See the fixture docstrings
# in ``backend/tests/conftest.py``.


@pytest.fixture
async def catalog(db_session):
    """One product 'Lamp': 1 shade + 2 arms per unit, 4 screws purchased; one sliced plate yielding 1 shade + 2 arms."""
    file = LibraryFile(
        filename="lamp.gcode.3mf",
        file_path="lamp",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade", "2": "arm", "3": "arm_2"},
                    "print_time_seconds": 100,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    product = Product(name="Lamp")
    customer = Customer(name="ACME")
    db_session.add_all([file, product, customer])
    await db_session.flush()
    shade = ProductPart(
        product_id=product.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1, aliases=["shade"]
    )
    arm = ProductPart(
        product_id=product.id, kind="printed", name="arm", name_key="arm", qty_per_unit=2, aliases=["arm"]
    )
    screw = ProductPart(product_id=product.id, kind="purchased", name="M3", name_key="purchased:m3", qty_per_unit=4)
    db_session.add_all([shade, arm, screw, ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0)])
    await db_session.commit()
    return {"file": file, "product": product, "customer": customer, "screw": screw}


async def _completed_print(db, project_id, file_id, line_id=None, material="PETG", defective_arms=0, plate_index=1):
    # ``plate_index=1``, not 0, and deliberately: a single-plate 3MF gets ONE
    # product plate numbered 0 ("the whole file") from the sync, while its
    # prints carry the slicer's own plate index, which is 1. Production
    # produces exactly this mismatch, so the fixture has to as well — the whole
    # file wildcard in ``order_metrics.attribute`` is what bridges the two.
    a = PrintArchive(
        project_id=project_id,
        project_line_id=line_id,
        library_file_id=file_id,
        plate_index=plate_index,
        filename="lamp",
        file_path="",
        file_size=0,
        status="completed",
        filament_type=material,
        quantity=3,
        cost=1.5,
        defective_count=defective_arms,
    )
    db.add(a)
    await db.flush()
    db.add_all(
        [
            PrintArchivePart(archive_id=a.id, name="shade", name_key="shade", quantity=1),
            PrintArchivePart(archive_id=a.id, name="arm", name_key="arm", quantity=2, defective=defective_arms),
        ]
    )
    await db.commit()
    return a


async def _queue_rows(db, project_id, file_id, line_id=None):
    """One row in each queue table, filed under the order. Returns their ids.

    ``PrinterQueue`` is built here rather than fixtured because SQLite does not
    enforce the FK to ``printers`` and the queue's identity is irrelevant to
    what is being asserted.
    """
    queue = PrinterQueue(id=1, printer_id=1)
    db.add(queue)
    await db.flush()
    item = PrintQueueItem(
        queue_id=queue.id, project_id=project_id, project_line_id=line_id, library_file_id=file_id, status="pending"
    )
    auto = AutoQueueItem(project_id=project_id, project_line_id=line_id, library_file_id=file_id, status="pending")
    db.add_all([item, auto])
    await db.commit()
    return item.id, auto.id


@pytest.mark.asyncio
async def test_create_order_with_lines_and_read_figures(committing_client, db_session, catalog):
    r = await committing_client.post(
        "/api/v1/projects/",
        json={
            "name": "Order 1",
            "customer_id": catalog["customer"].id,
            "price": 200.0,
            "lines": [{"product_id": catalog["product"].id, "quantity": 3, "material": "PETG"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    pid, line = body["id"], body["lines"][0]
    assert body["customer_name"] == "ACME" and line["quantity"] == 3 and line["units_printed"] == 0
    assert body["figures"]["ordered"] == 3 and body["figures"]["all_printed"] is False

    await _completed_print(db_session, pid, catalog["file"].id, line_id=line["id"])
    await _completed_print(db_session, pid, catalog["file"].id, defective_arms=1)  # implicit: plate + material
    await _completed_print(db_session, pid, catalog["file"].id, material="PLA")  # wrong material → other prints

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    line = body["lines"][0]
    parts = {p["name"]: p for p in line["parts"]}
    assert parts["shade"]["usable"] == 2 and parts["arm"]["usable"] == 3 and parts["arm"]["need"] == 6
    assert line["units_printed"] == 1  # arms: 3 // 2
    assert body["figures"]["other_prints_count"] == 1 and body["figures"]["printed"] == 1
    assert body["figures"]["margin"] == 200.0 - 4.5
    proc = body["procurement"][0]
    assert proc["name"] == "M3" and proc["need"] == 12 and proc["acquired"] == 0

    r = await committing_client.patch(
        f"/api/v1/projects/{pid}/procurement/{catalog['screw'].id}", json={"quantity_acquired": 8}
    )
    assert r.json()["procurement"][0]["acquired"] == 8 and r.json()["figures"]["complete"] == 1


@pytest.mark.asyncio
async def test_defective_is_counted_flat_and_per_part(committing_client, db_session, catalog):
    """The flat ``PrintArchive.defective_count`` and the ``print_archive_parts``
    rows describe the SAME fact from two sides: the order-level figure comes from
    the flat column, the part's ``usable`` from the per-part rows. A change that
    reads only one of them makes the two halves of one screen disagree."""
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    pid, line_id = body["id"], body["lines"][0]["id"]
    await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id, defective_arms=1)

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    assert body["figures"]["defective"] == 1
    parts = {p["name"]: p for p in body["lines"][0]["parts"]}
    assert parts["arm"]["usable"] == 1 and parts["arm"]["need"] == 2 and parts["arm"]["remaining"] == 1
    assert parts["shade"]["usable"] == 1
    assert body["lines"][0]["units_printed"] == 0  # one arm short of a unit


@pytest.mark.asyncio
async def test_progress_is_capped_at_one_on_every_wire_that_carries_it(committing_client, db_session, catalog):
    """A line printed twice over is 100% done, not 200%.

    ``progress`` is what a bar fills from; the excess is reported by
    ``units_printed`` and ``surplus``, which stay uncapped so nothing is hidden.
    Three surfaces carry the number — the line, the order's figures and the
    order list row — and each was clamping it (or not) on its own.
    """
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "Twice over", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    pid, line_id = body["id"], body["lines"][0]["id"]
    await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id)
    await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id)

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    line = body["lines"][0]
    assert line["units_printed"] == 2 and line["progress"] == 1.0
    parts = {p["name"]: p for p in line["parts"]}
    assert parts["arm"]["surplus"] == 2 and parts["shade"]["surplus"] == 1  # the excess is still on the wire
    assert body["figures"]["printed"] == 2 and body["figures"]["ordered"] == 1
    assert body["figures"]["progress"] == 1.0

    row = next(p for p in (await committing_client.get("/api/v1/projects/")).json() if p["id"] == pid)
    assert row["printed"] == 2 and row["progress"] == 1.0


@pytest.mark.asyncio
async def test_one_file_in_two_products_feeds_both_lines(committing_client, db_session, catalog):
    """A file shared by two products of the SAME order — a real farm case. Both
    products hold the file's whole-file plate, so both lines are candidates and
    the prints are dealt out across them in sort order. While the plate index in
    ``OrderContext`` held ONE product id per key, whichever product loaded last
    took every print of the file and its sibling reported nothing printed.

    This is the DB-level half of the unit tests in ``test_order_metrics.py``: it
    proves ``load_order_context`` builds the product LISTS and that the split
    reaches the API, not only the pure function.
    """
    file_id = catalog["file"].id
    products = [Product(name="Small vase"), Product(name="Tall vase")]
    db_session.add_all(products)
    await db_session.flush()
    for product in products:
        db_session.add_all(
            [
                ProductPart(
                    product_id=product.id,
                    kind="printed",
                    name="shade",
                    name_key="shade",
                    qty_per_unit=1,
                    aliases=["shade"],
                ),
                ProductPlate(product_id=product.id, library_file_id=file_id, plate_index=0),
            ]
        )
    await db_session.commit()

    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "Two vases",
                # ``sort_order`` comes from the position in this list, and the
                # response lists the lines in that same order.
                "lines": [
                    {"product_id": products[0].id, "quantity": 1},
                    {"product_id": products[1].id, "quantity": 2},
                ],
            },
        )
    ).json()
    pid = body["id"]
    for _ in range(3):
        await _completed_print(db_session, pid, file_id)

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    first, second = body["lines"]
    assert first["units_printed"] == 1  # its one unit, then it is full
    assert second["units_printed"] == 2  # the other two go on to the next line
    assert body["figures"]["printed"] == 3 and body["figures"]["other_prints_count"] == 0


@pytest.mark.asyncio
async def test_the_response_names_which_archives_fed_each_line_and_which_fed_none(
    committing_client, db_session, catalog
):
    """The order page groups prints by line from the response alone: every line
    lists the archives attributed to it, and ``other_archive_ids`` lists the
    prints no line could take.

    Same shape as ``test_one_file_in_two_products_feeds_both_lines`` — two lines
    over two products sharing one file — plus a print of a file neither product
    holds, which is what "no line could take" means.
    """
    file_id = catalog["file"].id
    stray_file = LibraryFile(filename="stranger.gcode.3mf", file_path="stranger", file_size=1, file_type="gcode")
    products = [Product(name="Small vase"), Product(name="Tall vase")]
    db_session.add_all([stray_file, *products])
    await db_session.flush()
    for product in products:
        db_session.add_all(
            [
                ProductPart(
                    product_id=product.id,
                    kind="printed",
                    name="shade",
                    name_key="shade",
                    qty_per_unit=1,
                    aliases=["shade"],
                ),
                ProductPlate(product_id=product.id, library_file_id=file_id, plate_index=0),
            ]
        )
    await db_session.commit()

    pid = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "Two vases",
                "lines": [
                    {"product_id": products[0].id, "quantity": 1},
                    {"product_id": products[1].id, "quantity": 2},
                ],
            },
        )
    ).json()["id"]
    prints = [await _completed_print(db_session, pid, file_id) for _ in range(3)]
    stray = await _completed_print(db_session, pid, stray_file.id)

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    lines = sorted(body["lines"], key=lambda ln: ln["sort_order"])
    # The first line is full after one print; the other two go on to its sibling.
    assert lines[0]["archive_ids"] == [prints[0].id]
    assert lines[1]["archive_ids"] == [prints[1].id, prints[2].id]
    assert body["other_archive_ids"] == [stray.id]


@pytest.mark.asyncio
async def test_a_line_less_order_lists_its_prints_as_other_rather_than_losing_them(
    committing_client, db_session, catalog
):
    """An order with NO lines still accounts for the prints bound to it.

    There is no candidate line to attribute anything to, so the whole set has
    to come back under ``other_archive_ids`` — the order page renders its
    prints from that field alone, and an empty one would show a page whose
    heading says "prints" above nothing at all, for prints the operator can see
    on the archives page filed under this very order.
    """
    file_id = catalog["file"].id
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "Bare"})).json()["id"]
    strays = [await _completed_print(db_session, pid, file_id) for _ in range(2)]

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    assert body["lines"] == []
    assert body["other_archive_ids"] == [strays[0].id, strays[1].id]
    assert body["figures"]["other_prints_count"] == 2


@pytest.mark.asyncio
async def test_a_line_no_print_reached_names_an_empty_list_not_null(committing_client, db_session, catalog):
    """``archive_ids`` is ALWAYS a list — ``[]`` for a line nothing fed.

    The order page maps over the field directly. ``null`` there is a crash on
    the page of a perfectly ordinary order (one line printed, the next not
    started), and the only place the shape is decided is this response.
    """
    product_id = catalog["product"].id
    pid = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "One of two started",
                "lines": [{"product_id": product_id, "quantity": 1}, {"product_id": product_id, "quantity": 1}],
            },
        )
    ).json()["id"]
    # ONE print: a Lamp is 1 shade + 2 arms, so it fills the first line exactly
    # and the second is left untouched.
    printed = await _completed_print(db_session, pid, catalog["file"].id)

    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    lines = sorted(body["lines"], key=lambda ln: ln["sort_order"])
    assert lines[0]["archive_ids"] == [printed.id]
    assert lines[1]["archive_ids"] == []
    assert body["other_archive_ids"] == []


@pytest.mark.asyncio
async def test_one_archive_answers_to_two_lines_on_the_wire(committing_client, db_session):
    """A shared plate is named by BOTH lines whose products it carries.

    ``build_parity_fixture``'s shared bed holds a lamp's shade and a hook, and
    the live order has a line for each. ``lines[].archive_ids`` is therefore not
    a partition, and the order page's grouping leans on exactly that: rebuilt
    from the archives instead, the plate would be filed under one line and
    silently missing from the other. Pinned here at the API, because the unit
    test can only speak for ``attribute``.
    """
    ids = await build_parity_fixture(db_session)
    shared_file = (
        await db_session.execute(select(LibraryFile).where(LibraryFile.filename == "shared.gcode.3mf"))
    ).scalar_one()
    shared_archive = (
        await db_session.execute(
            select(PrintArchive).where(
                PrintArchive.project_id == ids["live"], PrintArchive.library_file_id == shared_file.id
            )
        )
    ).scalar_one()

    body = (await committing_client.get(f"/api/v1/projects/{ids['live']}")).json()
    by_id = {line["id"]: line for line in body["lines"]}
    # The lamp line also holds the bases print; the hook line holds this one alone.
    assert shared_archive.id in by_id[ids["l1"]]["archive_ids"]
    assert by_id[ids["l2"]]["archive_ids"] == [shared_archive.id]
    # Named twice, and claimed by nobody else.
    assert body["other_archive_ids"] == []


@pytest.mark.asyncio
async def test_an_order_without_lines_is_not_printed(committing_client):
    """``all_printed`` over an empty set of lines is False, not the vacuous True
    ``all()`` would give — an empty order is not a finished one."""
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "Empty"})).json()["id"]
    figures = (await committing_client.get(f"/api/v1/projects/{pid}")).json()["figures"]
    assert figures["all_printed"] is False and figures["progress"] == 0.0
    assert figures["ordered"] == 0 and figures["printed"] == 0


@pytest.mark.asyncio
async def test_lines_crud_and_product_guard(committing_client, catalog):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    r = await committing_client.post(
        f"/api/v1/projects/{pid}/lines", json={"product_id": catalog["product"].id, "quantity": 2}
    )
    assert r.status_code == 200, r.text
    line_id = r.json()["lines"][0]["id"]
    r = await committing_client.patch(
        f"/api/v1/projects/{pid}/lines/{line_id}", json={"quantity": 5, "material": "petg"}
    )
    assert r.json()["lines"][0]["quantity"] == 5 and r.json()["lines"][0]["material"] == "PETG"
    assert (
        await committing_client.post(f"/api/v1/projects/{pid}/lines", json={"product_id": 9999, "quantity": 1})
    ).status_code == 404
    assert (
        await committing_client.post(
            f"/api/v1/projects/{pid}/lines", json={"product_id": catalog["product"].id, "quantity": 0}
        )
    ).status_code == 422
    # ``quantity`` / ``sort_order`` are NOT NULL too — same 422, not an
    # IntegrityError from the flush.
    for field in ("quantity", "sort_order"):
        assert (
            await committing_client.patch(f"/api/v1/projects/{pid}/lines/{line_id}", json={field: None})
        ).status_code == 422
    r = await committing_client.delete(f"/api/v1/projects/{pid}/lines/{line_id}")
    assert r.json()["lines"] == []


@pytest.mark.asyncio
async def test_status_lifecycle_and_nullable_fields(committing_client, catalog):
    pid = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "O", "tags": "a,b", "due_date": "2026-10-01T00:00:00", "price": 10},
        )
    ).json()["id"]
    assert (await committing_client.patch(f"/api/v1/projects/{pid}", json={"status": "archived"})).status_code == 400
    r = await committing_client.patch(
        f"/api/v1/projects/{pid}", json={"status": "completed", "tags": None, "due_date": None, "price": None}
    )
    body = r.json()
    assert body["status"] == "completed" and body["tags"] is None and body["due_date"] is None and body["price"] is None
    r = await committing_client.patch(
        f"/api/v1/projects/{pid}", json={"status": "active", "customer_id": catalog["customer"].id}
    )
    assert r.json()["customer_name"] == "ACME"
    assert (await committing_client.patch(f"/api/v1/projects/{pid}", json={"customer_id": 9999})).status_code == 404
    assert (
        await committing_client.patch(f"/api/v1/projects/{pid}", json={"url": "javascript:alert(1)"})
    ).status_code == 422

    # ``status`` / ``name`` / ``priority`` are NOT NULL: clearing one used to
    # reach the flush and surface as a 500. 422, and the stored value untouched.
    for field in ("status", "name", "priority"):
        assert (await committing_client.patch(f"/api/v1/projects/{pid}", json={field: None})).status_code == 422
    body = (await committing_client.get(f"/api/v1/projects/{pid}")).json()
    assert body["status"] == "active" and body["name"] == "O" and body["priority"] == "normal"


@pytest.mark.asyncio
async def test_list_filters_by_status_and_customer(committing_client, catalog):
    await committing_client.post(
        "/api/v1/projects/",
        json={
            "name": "A",
            "customer_id": catalog["customer"].id,
            "lines": [{"product_id": catalog["product"].id, "quantity": 2}],
        },
    )
    b = (await committing_client.post("/api/v1/projects/", json={"name": "B"})).json()["id"]
    await committing_client.patch(f"/api/v1/projects/{b}", json={"status": "cancelled"})
    names = lambda r: sorted(p["name"] for p in r.json())  # noqa: E731
    assert names(await committing_client.get("/api/v1/projects/")) == ["A", "B"]
    # The list carries the line count and, per line, whether its product has a
    # cover to draw — one grouped lookup rather than a product query per row.
    row = next(p for p in (await committing_client.get("/api/v1/projects/")).json() if p["name"] == "A")
    assert row["lines_count"] == 1 and row["ordered"] == 2
    assert row["line_products"] == [{"product_id": catalog["product"].id, "has_cover": False}]
    assert "product_cover_filenames" not in row
    assert names(await committing_client.get("/api/v1/projects/?status=active")) == ["A"]
    assert names(await committing_client.get(f"/api/v1/projects/?customer_id={catalog['customer'].id}")) == ["A"]
    # An unknown status is refused, not answered with an empty list — the same
    # 400 ``PATCH`` gives. ``archived`` is the one that matters: m158 retired
    # it, and a stale bookmark asking for it must say so rather than render
    # "you have no orders" over a farm full of them.
    assert (await committing_client.get("/api/v1/projects/?status=archived")).status_code == 400
    assert (await committing_client.get("/api/v1/projects/?status=nonsense")).status_code == 400


@pytest.mark.asyncio
async def test_orders_can_be_listed_by_product(committing_client, catalog):
    """The product page asks where its product is ordered — and that filter
    composes with the others rather than replacing them."""
    a = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "A", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    b = (await committing_client.post("/api/v1/projects/", json={"name": "B"})).json()

    resp = await committing_client.get(f"/api/v1/projects/?product_id={catalog['product'].id}")
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()}
    assert a["id"] in ids and b["id"] not in ids

    resp = await committing_client.get(f"/api/v1/projects/?product_id={catalog['product'].id}&status=completed")
    assert resp.json() == []


@pytest.mark.asyncio
async def test_delete_unlinks_archives_and_duplicate_copies_lines_not_history(committing_client, db_session, catalog):
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    pid, line_id = body["id"], body["lines"][0]["id"]
    archive_id = (await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id)).id

    copy = (await committing_client.post(f"/api/v1/projects/{pid}/duplicate", json={})).json()
    assert copy["name"] == "O (Copy)" and len(copy["lines"]) == 1 and copy["figures"]["printed"] == 0
    assert copy["lines"][0]["product_id"] == catalog["product"].id and copy["lines"][0]["id"] != line_id
    assert copy["status"] == "active"  # a reorder starts open, whatever the source was

    # No body at all: the dialog's common case, and what the old signature took.
    second = await committing_client.post(f"/api/v1/projects/{pid}/duplicate")
    assert second.status_code == 200, second.text
    assert second.json()["name"] == "O (Copy 2)"  # every copy gets its own name
    # Whitespace is not a name — it falls back to the generated one.
    third = await committing_client.post(f"/api/v1/projects/{pid}/duplicate", json={"name": "  "})
    assert third.json()["name"] == "O (Copy 3)"

    assert (await committing_client.delete(f"/api/v1/projects/{pid}")).status_code == 200
    db_session.expire_all()
    a = await db_session.get(PrintArchive, archive_id)
    assert a.project_id is None and a.project_line_id is None
    assert await db_session.get(Project, pid) is None


@pytest.mark.asyncio
async def test_add_archives_can_name_the_line(committing_client, db_session, catalog):
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    pid, line_id = body["id"], body["lines"][0]["id"]
    stray = PrintArchive(filename="x", file_path="", file_size=0, status="completed")
    db_session.add(stray)
    await db_session.commit()
    # Read the id BEFORE expiring: an expired attribute reloads itself, and a
    # lazy load from plain async code is a MissingGreenlet, not a query.
    stray_id = stray.id
    r = await committing_client.post(
        f"/api/v1/projects/{pid}/add-archives", json={"archive_ids": [stray_id], "project_line_id": line_id}
    )
    assert r.status_code == 200
    db_session.expire_all()
    s = await db_session.get(PrintArchive, stray_id)
    assert s.project_id == pid and s.project_line_id == line_id
    assert (
        await committing_client.post(
            f"/api/v1/projects/{pid}/add-archives", json={"archive_ids": [stray_id], "project_line_id": 9999}
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_delete_project_unlinks_both_queue_tables(committing_client, db_session, catalog):
    """The queue tables are what the explicit UPDATEs are for.

    An archive would be unlinked anyway — ``Project.archives`` is a relationship
    and the ORM de-associates it on delete. Neither queue table gets that much:
    only ``PrintQueueItem`` has a relationship, it would clear ``project_id``
    alone, and ``AutoQueueItem`` has none at all. Without the explicit
    statements both keep pointing at an order that no longer exists.
    """
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    pid, line_id = body["id"], body["lines"][0]["id"]
    item_id, auto_id = await _queue_rows(db_session, pid, catalog["file"].id, line_id=line_id)

    assert (await committing_client.delete(f"/api/v1/projects/{pid}")).status_code == 200
    db_session.expire_all()
    for model, row_id in ((PrintQueueItem, item_id), (AutoQueueItem, auto_id)):
        row = await db_session.get(model, row_id)
        assert row is not None, f"{model.__name__} was deleted instead of unlinked"
        assert row.project_id is None and row.project_line_id is None


@pytest.mark.asyncio
async def test_delete_line_drops_the_line_and_keeps_the_order(committing_client, db_session, catalog):
    """Deleting a line un-files the work from the LINE, not from the order.

    The prints were still made for this customer and the order still paid for
    them — losing ``project_id`` too would take them out of its cost and out of
    its history.
    """
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    pid, line_id = body["id"], body["lines"][0]["id"]
    archive_id = (await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id)).id
    _, auto_id = await _queue_rows(db_session, pid, catalog["file"].id, line_id=line_id)

    r = await committing_client.delete(f"/api/v1/projects/{pid}/lines/{line_id}")
    assert r.status_code == 200 and r.json()["lines"] == []

    db_session.expire_all()
    archive = await db_session.get(PrintArchive, archive_id)
    auto = await db_session.get(AutoQueueItem, auto_id)
    assert archive.project_id == pid and archive.project_line_id is None
    assert auto.project_id == pid and auto.project_line_id is None
    # Still the order's print: it stays in the cost, now as an "other" print.
    figures = (await committing_client.get(f"/api/v1/projects/{pid}")).json()["figures"]
    assert figures["total_cost"] == 1.5 and figures["other_prints_count"] == 1


@pytest.mark.asyncio
async def test_a_line_of_another_order_is_not_a_line_of_this_one(committing_client, db_session, catalog):
    """``_get_line`` checks OWNERSHIP, not existence — a real id belonging to
    somebody else is exactly what a "does this row exist" check waves through."""
    a = (await committing_client.post("/api/v1/projects/", json={"name": "A"})).json()["id"]
    b = (await committing_client.post("/api/v1/projects/", json={"name": "B"})).json()["id"]
    b_line = (
        await committing_client.post(
            f"/api/v1/projects/{b}/lines", json={"product_id": catalog["product"].id, "quantity": 1}
        )
    ).json()["lines"][0]["id"]
    stray = PrintArchive(filename="x", file_path="", file_size=0, status="completed")
    db_session.add(stray)
    await db_session.commit()
    stray_id = stray.id

    r = await committing_client.post(
        f"/api/v1/projects/{a}/add-archives", json={"archive_ids": [stray_id], "project_line_id": b_line}
    )
    assert r.status_code == 404, r.text
    db_session.expire_all()
    assert (await db_session.get(PrintArchive, stray_id)).project_id is None  # nothing filed on the way out
    assert (
        await committing_client.patch(f"/api/v1/projects/{a}/lines/{b_line}", json={"quantity": 2})
    ).status_code == 404
    assert (await committing_client.delete(f"/api/v1/projects/{a}/lines/{b_line}")).status_code == 404
    assert len((await committing_client.get(f"/api/v1/projects/{b}")).json()["lines"]) == 1  # B keeps its line


@pytest.mark.asyncio
async def test_timeline_still_reads_the_archive(committing_client, db_session, catalog):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    await _completed_print(db_session, pid, catalog["file"].id)
    events = (await committing_client.get(f"/api/v1/projects/{pid}/timeline")).json()
    assert [e["event_type"] for e in events] == ["print_completed", "project_created"]


# ---------------------------------------------------------------------------
# Attachments, cover image, and the two "file it under this order" handlers.
#
# These four handlers survived the redesign untouched, but their only coverage
# lived in ``test_projects_api.py``, which went out with the legacy feature.
# What follows are regression guards, not specifications: each one walks the
# handler end to end once and asserts the shape the frontend actually reads.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_upload_list_download_and_delete(committing_client):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]

    up = await committing_client.post(
        f"/api/v1/projects/{pid}/attachments",
        files={"file": ("spec.txt", b"how to build it", "text/plain")},
    )
    assert up.status_code == 200, up.text
    stored = up.json()["filename"]
    assert up.json()["original_name"] == "spec.txt"

    # The order reads its own attachments back — this is the list the UI renders.
    listed = (await committing_client.get(f"/api/v1/projects/{pid}")).json()["attachments"]
    assert [a["original_name"] for a in listed] == ["spec.txt"]
    assert listed[0]["filename"] == stored and listed[0]["size"] == len(b"how to build it")

    got = await committing_client.get(f"/api/v1/projects/{pid}/attachments/{stored}")
    assert got.status_code == 200 and got.content == b"how to build it"

    gone = await committing_client.delete(f"/api/v1/projects/{pid}/attachments/{stored}")
    assert gone.status_code == 200, gone.text
    assert (await committing_client.get(f"/api/v1/projects/{pid}")).json()["attachments"] is None
    assert (await committing_client.get(f"/api/v1/projects/{pid}/attachments/{stored}")).status_code == 404


@pytest.mark.asyncio
async def test_attachment_upload_rejects_a_bad_extension(committing_client):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    bad = await committing_client.post(
        f"/api/v1/projects/{pid}/attachments", files={"file": ("payload.exe", b"MZ", "application/octet-stream")}
    )
    assert bad.status_code == 400, bad.text


@pytest.mark.asyncio
async def test_a_traversing_attachment_name_is_refused_before_the_path_join():
    """Called directly, not over HTTP, and deliberately so.

    ``{filename}`` is a plain path parameter, so Starlette never routes a name
    containing a separator to the handler at all — over the wire a traversal
    attempt is a 404 from the router and the guard inside is never reached.
    That makes the guard defence in depth against a future ``:path`` converter
    or a second caller, and the only way to show it still works is to call it.
    """
    from fastapi import HTTPException

    from backend.app.api.routes.projects import delete_attachment, download_attachment

    for handler in (download_attachment, delete_attachment):
        for name in ("../../secret.txt", "sub/file.txt", "..\\win.txt", ""):
            with pytest.raises(HTTPException) as raised:
                await handler(project_id=1, filename=name, db=None, _=None)
            assert raised.value.status_code == 400, f"{handler.__name__} accepted {name!r}"


@pytest.mark.asyncio
async def test_cover_image_upload_stream_and_delete(committing_client):
    """The GET is gated by ``RequireCameraStreamToken``, not by the JWT.

    ``<img src>`` cannot carry an Authorization header, so the route takes the
    same ``?token=`` credential as ``/archives/{id}/thumbnail``.
    """
    from backend.app.core.auth import create_camera_stream_token

    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]

    up = await committing_client.post(
        f"/api/v1/projects/{pid}/cover-image", files={"file": ("cover.png", b"\x89PNG\r\n\x1a\n", "image/png")}
    )
    assert up.status_code == 200, up.text
    assert up.json()["filename"].startswith("cover_") and up.json()["filename"].endswith(".png")
    assert (await committing_client.get(f"/api/v1/projects/{pid}")).json()["cover_image_filename"] == up.json()[
        "filename"
    ]

    token = await create_camera_stream_token()
    img = await committing_client.get(f"/api/v1/projects/{pid}/cover-image", params={"token": token})
    assert img.status_code == 200, img.text
    assert img.headers["content-type"] == "image/png"
    assert img.content == b"\x89PNG\r\n\x1a\n"

    assert (await committing_client.delete(f"/api/v1/projects/{pid}/cover-image")).status_code == 200
    assert (await committing_client.get(f"/api/v1/projects/{pid}")).json()["cover_image_filename"] is None
    missing = await committing_client.get(f"/api/v1/projects/{pid}/cover-image", params={"token": token})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_the_cover_route_revalidates_and_heals_a_dangling_reference(committing_client, db_session):
    """Two rules the product cover route learned in pass 4, now here as well.

    ``Cache-Control: private, no-cache`` — REVALIDATE, not "do not store". The
    URL is stable across the cover being replaced, so an age-based cache shows
    the old picture after an upload; that is what a cache-busting query param on
    the frontend was working around.

    And the self-heal PERSISTS. ``get_db`` rolls the request back on anything
    that escapes the handler, so clearing the column and then *raising* a 404
    undid the very heal it had just performed — the next request found the same
    dangling reference and warned about it again, forever.
    """
    from backend.app.api.routes.projects import get_project_attachments_dir
    from backend.app.core.auth import create_camera_stream_token
    from backend.app.models.project import Project

    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    name = (
        await committing_client.post(
            f"/api/v1/projects/{pid}/cover-image",
            files={"file": ("cover.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    ).json()["filename"]
    token = await create_camera_stream_token()

    served = await committing_client.get(f"/api/v1/projects/{pid}/cover-image", params={"token": token})
    assert served.status_code == 200
    assert served.headers["cache-control"] == "private, no-cache"

    (get_project_attachments_dir(pid) / name).unlink()

    gone = await committing_client.get(f"/api/v1/projects/{pid}/cover-image", params={"token": token})
    assert gone.status_code == 404
    assert gone.json()["detail"] == "Cover image file not found"

    db_session.expire_all()
    assert (await db_session.get(Project, pid)).cover_image_filename is None, "the heal did not survive the response"


@pytest.mark.asyncio
async def test_the_archives_page_is_bounded_and_its_order_is_total(committing_client, db_session, catalog):
    """``limit`` had no upper bound and the order had no tiebreaker.

    The order page walks this endpoint in pages of 500, so the bound is 500 —
    an unbounded ``limit`` is a whole farm's print history in one response for
    the cost of one query param. And ``created_at`` has second resolution on
    SQLite: two prints of the same second are a TIE, which LIMIT/OFFSET is free
    to break differently per page, dropping one row from the walk and repeating
    another. ``id`` desc is the tiebreaker, and it is total.
    """
    from datetime import datetime

    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    stamp = datetime(2026, 9, 4, 12, 0, 0)
    first = await _completed_print(db_session, pid, catalog["file"].id)
    second = await _completed_print(db_session, pid, catalog["file"].id)
    first.created_at = second.created_at = stamp
    await db_session.commit()

    for bad in ({"limit": 501}, {"limit": 0}, {"offset": -1}):
        r = await committing_client.get(f"/api/v1/projects/{pid}/archives", params=bad)
        assert r.status_code == 422, f"{bad} was accepted: {r.text}"

    pages = []
    for offset in (0, 1):
        page = await committing_client.get(f"/api/v1/projects/{pid}/archives", params={"limit": 1, "offset": offset})
        assert page.status_code == 200, page.text
        pages.append([a["id"] for a in page.json()])

    assert sorted(pages[0] + pages[1]) == sorted([first.id, second.id]), "a tied row was dropped or repeated"


@pytest.mark.asyncio
async def test_add_queue_files_a_pending_queue_item_under_the_order(committing_client, db_session, catalog):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    queue = PrinterQueue(id=1, printer_id=1)
    db_session.add(queue)
    await db_session.flush()
    item = PrintQueueItem(queue_id=queue.id, library_file_id=catalog["file"].id, status="pending")
    db_session.add(item)
    await db_session.flush()
    item_id = item.id  # read before the commit expires the instance
    assert item.project_id is None
    await db_session.commit()

    r = await committing_client.post(f"/api/v1/projects/{pid}/add-queue", json={"queue_item_ids": [item_id, 9999]})
    assert r.status_code == 200, r.text
    # The unknown id is skipped rather than failing the batch, and says so.
    assert r.json()["message"] == "Added 1 queue items to project"
    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, item_id)).project_id == pid

    stray = await committing_client.post("/api/v1/projects/9999/add-queue", json={"queue_item_ids": []})
    assert stray.status_code == 404


@pytest.mark.asyncio
async def test_add_queue_drops_a_line_that_belongs_to_the_old_order(committing_client, db_session, catalog):
    """A line only ever travels with the order it belongs to.

    Re-filing a queue item under another order used to leave ``project_line_id``
    pointing at a line of the OLD one — so the work would have been credited to
    a line of an order that never asked for it, exactly the accounting
    ``archives.update_archive`` refuses. Dropped rather than refused: re-filing
    is a routine correction and the operator can name the new line afterwards.
    """
    a = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "A", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    b = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "B", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()
    a_line, b_line = a["lines"][0]["id"], b["lines"][0]["id"]
    item_id, _ = await _queue_rows(db_session, a["id"], catalog["file"].id, line_id=a_line)

    r = await committing_client.post(f"/api/v1/projects/{b['id']}/add-queue", json={"queue_item_ids": [item_id]})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    item = await db_session.get(PrintQueueItem, item_id)
    assert item.project_id == b["id"] and item.project_line_id is None
    # A's line is untouched — only the item moved.
    assert await db_session.get(ProjectLine, a_line) is not None

    # Re-filing under the SAME order keeps the line it already carries.
    item.project_line_id = b_line
    await db_session.commit()
    assert (
        await committing_client.post(f"/api/v1/projects/{b['id']}/add-queue", json={"queue_item_ids": [item_id]})
    ).status_code == 200
    db_session.expire_all()
    assert (await db_session.get(PrintQueueItem, item_id)).project_line_id == b_line


@pytest.mark.asyncio
async def test_project_archives_lists_only_this_orders_archives(committing_client, db_session, catalog):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    other = (await committing_client.post("/api/v1/projects/", json={"name": "P"})).json()["id"]
    mine = await _completed_print(db_session, pid, catalog["file"].id)
    await _completed_print(db_session, other, catalog["file"].id)

    r = await committing_client.get(f"/api/v1/projects/{pid}/archives")
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()] == [mine.id]

    assert (await committing_client.get("/api/v1/projects/9999/archives")).status_code == 404


@pytest.mark.asyncio
async def test_a_trashed_print_is_not_in_the_orders_walk(committing_client, db_session, catalog):
    """⚠️ ``deleted_at`` — the filter every other order-archive reader applies.

    ``order_metrics``' two loaders both skip trashed archives, so nothing
    attributes one to a line. Left in this list it came back as an archive the
    page could show but no line claimed, i.e. under **Unlisted** — a print the
    operator had thrown away, listed as work of theirs nobody had filed.
    """
    from datetime import datetime, timezone

    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    kept = await _completed_print(db_session, pid, catalog["file"].id)
    trashed = await _completed_print(db_session, pid, catalog["file"].id)
    trashed.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    r = await committing_client.get(f"/api/v1/projects/{pid}/archives")
    assert r.status_code == 200, r.text
    assert [a["id"] for a in r.json()] == [kept.id]


# ---------- the plan (spec pass 3) ----------


async def _order_with_line(client, product_id, quantity, material="PETG"):
    body = (
        await client.post(
            "/api/v1/projects/",
            json={"name": "O", "lines": [{"product_id": product_id, "quantity": quantity, "material": material}]},
        )
    ).json()
    return body["id"], body["lines"][0]["id"]


def _by_name(rows):
    return {row["name"]: row["count"] for row in rows}


async def _plate_id_of_the_only_row(client, project_id):
    plan = (await client.get(f"/api/v1/projects/{project_id}/plan")).json()
    return plan["lines"][0]["rows"][0]["plate_id"]


@pytest.mark.asyncio
async def test_plan_subtracts_finished_and_queued_work(committing_client, db_session, catalog):
    """Four units wanted, one plate printed, two pending queue rows filed under
    the line: 4 shades - 1 made - 2 queued = 1, and 8 arms - 2 - 4 = 2. Exactly
    one more print of the plate covers that."""
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id)
    await _queue_rows(db_session, pid, catalog["file"].id, line_id=line_id)

    r = await committing_client.get(f"/api/v1/projects/{pid}/plan")
    assert r.status_code == 200, r.text
    body = r.json()
    line = body["lines"][0]
    assert line["line_id"] == line_id and line["product_name"] == "Lamp" and line["material"] == "PETG"
    assert _by_name(line["outstanding_before"]) == {"shade": 1, "arm": 2}
    assert len(line["rows"]) == 1
    row = line["rows"][0]
    assert row["count"] == 1 and row["plate_index"] == 0 and row["filename"] == "lamp.gcode.3mf"
    assert row["library_file_id"] == catalog["file"].id
    assert _by_name(row["useful"]) == {"shade": 1, "arm": 2}
    assert row["print_time_seconds"] == 100 and row["time_unknown"] is False
    # No filament rate in settings and no grams in the metadata: both stay null
    # rather than claiming the print is free.
    assert row["cost"] is None and row["filament_used_grams"] is None
    assert line["surplus_after"] == [] and line["unsatisfiable"] == []
    assert line["candidates"] == [row["plate_id"]] and line["not_sliced"] == []
    assert body["totals"] == {"prints": 1, "print_time_seconds": 100, "filament_used_grams": 0.0, "cost": None}
    # The plan ran to the end of its work, and says so on the wire — the flag is
    # what tells a complete plan from one the iteration guard cut short.
    assert body["truncated"] is False

    assert (await committing_client.get("/api/v1/projects/9999/plan")).status_code == 404


@pytest.mark.asyncio
async def test_plan_ignores_an_auto_queue_row_already_handed_to_a_printer(committing_client, db_session, catalog):
    """An assigned auto-queue row is counted once - through the printer item it
    was copied into. Counting it twice would plan one print too few."""
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    await _completed_print(db_session, pid, catalog["file"].id, line_id=line_id)
    item_id, auto_id = await _queue_rows(db_session, pid, catalog["file"].id, line_id=line_id)
    (await db_session.get(AutoQueueItem, auto_id)).assigned_to_item_id = item_id
    await db_session.commit()

    line = (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()["lines"][0]
    assert _by_name(line["outstanding_before"]) == {"shade": 2, "arm": 4}
    assert line["rows"][0]["count"] == 2


async def _twin_plate(db, product, *, filename, model, seconds):
    """A second file of the SAME product making the same parts per print.

    This is the shape the whole alternatives feature is about: one part, sliced
    once per printer model, so two library files yield ``1 shade + 2 arms``
    apiece and only the target machine tells them apart.
    """
    file = LibraryFile(
        filename=filename,
        file_path=filename,
        file_size=1,
        file_type="gcode",
        file_metadata={
            "sliced_for_model": model,
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade", "2": "arm", "3": "arm_2"},
                    "print_time_seconds": seconds,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ],
        },
    )
    db.add(file)
    await db.flush()
    plate = ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0)
    db.add(plate)
    await db.commit()
    return file, plate


async def _name_the_catalog_file_a_model(db, file, model):
    """``file_metadata`` is a JSON column — mutating the dict in place is not a
    change SQLAlchemy sees, so the whole value is replaced."""
    meta = dict(file.file_metadata or {})
    meta["sliced_for_model"] = model
    file.file_metadata = meta
    await db.commit()


@pytest.mark.asyncio
async def test_the_plan_names_the_other_file_the_same_part_is_sliced_for(committing_client, db_session, catalog):
    """The greedy picks one plate and hangs every print on it, so the other
    file — the same part, sliced for the other printer — was invisible in the
    block (user report, 2026-09-04). It rides out on the row, with its own
    figures and the printer model that tells the operator which machine it is
    for."""
    await _name_the_catalog_file_a_model(db_session, catalog["file"], "X1C")
    twin_file, twin_plate = await _twin_plate(
        db_session, catalog["product"], filename="lamp-p1s.gcode.3mf", model="Bambu Lab P1S", seconds=200
    )
    pid, _line_id = await _order_with_line(committing_client, catalog["product"].id, 4)

    line = (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()["lines"][0]
    row = line["rows"][0]

    # The faster file wins the pick; the slower one is the alternative.
    assert row["filename"] == "lamp.gcode.3mf" and row["printer_model"] == "X1C"
    assert row["alternatives"] == [
        {
            "plate_id": twin_plate.id,
            "library_file_id": twin_file.id,
            "plate_index": 0,
            "filename": "lamp-p1s.gcode.3mf",
            # ⚠️ Normalised on the way out: the 3MF says "Bambu Lab P1S" and the
            # auto-queue routes on "P1S". A raw name here would match no printer.
            "printer_model": "P1S",
            "print_time_seconds": 200,
            "filament_used_grams": None,
            "cost": None,
            "time_unknown": False,
        }
    ]
    # The totals stay the PICKED plate's — the what-if of a switched file is the
    # block's arithmetic, not a second plan the server computed.
    assert (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()["totals"]["print_time_seconds"] == (
        row["count"] * 100
    )


@pytest.mark.asyncio
async def test_plan_enqueue_splits_one_line_across_two_files(committing_client, db_session, catalog):
    """Two items, one line, two plates — because the auto-queue routes an item
    by its ``target_model``, so a file only ever reaches the printers it was
    sliced for. Splitting the count is the only way one line's work reaches two
    models."""
    await _name_the_catalog_file_a_model(db_session, catalog["file"], "X1C")
    twin_file, twin_plate = await _twin_plate(
        db_session, catalog["product"], filename="lamp-p1s.gcode.3mf", model="P1S", seconds=200
    )
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [
                {"plate_id": twin_plate.id, "count": 2, "line_id": line_id},
                {"plate_id": plate_id, "count": 3, "line_id": line_id},
            ],
            "target": {"kind": "auto"},
        },
    )
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert [(c["plate_id"], len(c["queue_item_ids"])) for c in created] == [(twin_plate.id, 2), (plate_id, 3)]

    ids = [i for c in created for i in c["queue_item_ids"]]
    rows = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id.in_(ids)))).scalars().all()
    assert len(rows) == 5
    assert {row.project_line_id for row in rows} == {line_id}
    by_file = Counter(row.library_file_id for row in rows)
    assert by_file == Counter({catalog["file"].id: 3, twin_file.id: 2})


@pytest.mark.asyncio
async def test_plan_enqueue_auto_creates_rows_and_the_next_plan_sees_them(committing_client, db_session, catalog):
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={"items": [{"plate_id": plate_id, "count": 3, "line_id": line_id}], "target": {"kind": "auto"}},
    )
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert len(created) == 1 and created[0]["line_id"] == line_id and created[0]["plate_id"] == plate_id
    ids = created[0]["queue_item_ids"]
    assert len(ids) == 3

    rows = (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id.in_(ids)))).scalars().all()
    assert len(rows) == 3
    assert {row.project_id for row in rows} == {pid}
    assert {row.project_line_id for row in rows} == {line_id}
    assert {row.library_file_id for row in rows} == {catalog["file"].id}
    # ``plate_index = 0`` is "the whole file", which on a queue row is no plate
    # at all - the slicer's 1-based index is what that column carries.
    assert {row.plate_id for row in rows} == {None}
    assert {row.status for row in rows} == {"pending"}
    assert len({row.batch_id for row in rows}) == 1 and all(row.batch_id for row in rows)

    body = (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()
    assert _by_name(body["lines"][0]["outstanding_before"]) == {"shade": 7, "arm": 14}
    assert body["totals"]["prints"] == 7


@pytest.mark.asyncio
async def test_plan_enqueue_to_a_printer_fills_that_printers_queue(committing_client, db_session, catalog):
    printer = Printer(name="P1", serial_number="S1", ip_address="10.0.0.1", access_code="1234", model="P1S")
    db_session.add(printer)
    await db_session.flush()
    db_session.add(PrinterQueue(printer_id=printer.id))
    await db_session.commit()
    printer_id = printer.id

    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [{"plate_id": plate_id, "count": 3, "line_id": line_id}],
            "target": {"kind": "printer", "printer_id": printer_id},
        },
    )
    assert r.status_code == 200, r.text
    ids = r.json()["created"][0]["queue_item_ids"]
    assert len(ids) == 3

    rows = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_(ids)))).scalars().all()
    assert len(rows) == 3
    assert {row.project_id for row in rows} == {pid}
    assert {row.project_line_id for row in rows} == {line_id}
    assert {row.library_file_id for row in rows} == {catalog["file"].id}
    assert {row.plate_id for row in rows} == {None}
    assert {row.status for row in rows} == {"pending"}
    assert len({row.batch_id for row in rows}) == 1 and all(row.batch_id for row in rows)


@pytest.mark.asyncio
async def test_plan_enqueue_validates_every_item_before_writing_anything(committing_client, db_session, catalog):
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    _other_pid, other_line = await _order_with_line(committing_client, catalog["product"].id, 1)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    stranger = Product(name="Stranger")
    raw = LibraryFile(filename="raw.3mf", file_path="raw", file_size=1, file_type="3mf")
    db_session.add_all([stranger, raw])
    await db_session.flush()
    foreign = ProductPlate(product_id=stranger.id, library_file_id=catalog["file"].id, plate_index=0)
    unsliced = ProductPlate(product_id=catalog["product"].id, library_file_id=raw.id, plate_index=0)
    archived = Printer(name="Old", serial_number="S9", ip_address="10.0.0.9", access_code="9999", archived=True)
    db_session.add_all([foreign, unsliced, archived])
    await db_session.commit()
    foreign_id, unsliced_id, archived_id = foreign.id, unsliced.id, archived.id

    auto = {"kind": "auto"}
    refused = [
        ([{"plate_id": plate_id, "count": 1, "line_id": other_line}], auto, 404),  # a line of another order
        ([{"plate_id": foreign_id, "count": 1, "line_id": line_id}], auto, 404),  # a plate of another product
        ([{"plate_id": unsliced_id, "count": 1, "line_id": line_id}], auto, 404),  # nothing to print yet
        ([{"plate_id": 999999, "count": 1, "line_id": line_id}], auto, 404),
        ([{"plate_id": plate_id, "count": 1, "line_id": 999999}], auto, 404),
        # The target's SHAPE is the schema's business, so both halves are 422s
        # and neither reaches the handler: a printer kind with no id …
        ([{"plate_id": plate_id, "count": 1, "line_id": line_id}], {"kind": "printer"}, 422),
        # … and an auto kind carrying one, which used to be accepted with the
        # id quietly dropped — i.e. filed nowhere near the printer it named.
        ([{"plate_id": plate_id, "count": 1, "line_id": line_id}], {"kind": "auto", "printer_id": 1}, 422),
        ([{"plate_id": plate_id, "count": 1, "line_id": line_id}], {"kind": "printer", "printer_id": 9999}, 404),
        (
            [{"plate_id": plate_id, "count": 1, "line_id": line_id}],
            {"kind": "printer", "printer_id": archived_id},
            404,
        ),
        # A good item beside a bad one: the whole call is refused.
        (
            [
                {"plate_id": plate_id, "count": 1, "line_id": line_id},
                {"plate_id": foreign_id, "count": 1, "line_id": line_id},
            ],
            auto,
            404,
        ),
    ]
    for items, target, code in refused:
        r = await committing_client.post(
            f"/api/v1/projects/{pid}/plan/enqueue", json={"items": items, "target": target}
        )
        assert r.status_code == code, f"{items} / {target} -> {r.status_code} {r.text}"

    # Nothing was written by any of them - validation precedes every writer.
    assert (await db_session.execute(select(AutoQueueItem))).scalars().all() == []
    assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []

    # An empty item list is a 422 from the schema, not an empty success.
    r = await committing_client.post(f"/api/v1/projects/{pid}/plan/enqueue", json={"items": [], "target": auto})
    assert r.status_code == 422, r.text

    # Precedence, and a decision rather than an accident of statement order:
    # EVERY line is checked before ANY plate is looked up, so a request whose
    # first item names a plate that does not exist and whose second names a line
    # that does not is refused for the LINE. It has to be that way round — a
    # plate is looked up inside the line's product, so the plate question cannot
    # even be asked about an item whose line is unknown.
    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [
                {"plate_id": 999999, "count": 1, "line_id": line_id},
                {"plate_id": plate_id, "count": 1, "line_id": 999999},
            ],
            "target": auto,
        },
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Order line not found in this project"


@pytest.mark.asyncio
async def test_plan_enqueue_reports_what_landed_when_a_later_item_fails(
    committing_client, db_session, catalog, monkeypatch
):
    """The writers commit per item, so a failure half-way cannot be rolled back.

    The first item's rows are real and stay real; the 500 names them rather than
    leaving the operator with queue rows nobody mentioned.
    """
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    real = projects_routes.add_items_to_auto_queue
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("the writer fell over")
        return await real(*args, **kwargs)

    monkeypatch.setattr(projects_routes, "add_items_to_auto_queue", flaky)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [
                {"plate_id": plate_id, "count": 2, "line_id": line_id},
                {"plate_id": plate_id, "count": 3, "line_id": line_id},
            ],
            "target": {"kind": "auto"},
        },
    )
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert "the writer fell over" in detail["message"]
    # Same shape as a success, so one reader covers both.
    assert len(detail["created"]) == 1
    landed = detail["created"][0]
    assert landed["line_id"] == line_id and landed["plate_id"] == plate_id
    assert len(landed["queue_item_ids"]) == 2

    rows = (
        (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.id.in_(landed["queue_item_ids"]))))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    # The second item wrote nothing at all — 2 rows in the table, not 5.
    assert len((await db_session.execute(select(AutoQueueItem))).scalars().all()) == 2


# ---------- queued_yield_by_line: the cases that need a session ----------


async def _queued_yield(db, project_id):
    """Exactly what ``plan_for_order`` does — including where the counted parts
    come from: one ``attribute`` pass over the same context, so the map under
    test is the map the plan subtracts."""
    ctx = await load_order_context(db, project_id)
    recipes = {pid: await recipes_for_product(db, product) for pid, product in ctx.products_by_id.items()}
    figures, _other = attribute(ctx)
    counted = {line_id: {pf.part_id for pf in figs.parts} for line_id, figs in figures.items()}
    return await queued_yield_by_line(db, recipes, ctx.lines, counted)


async def _parts_of(db, product_id):
    rows = (await db.execute(select(ProductPart).where(ProductPart.product_id == product_id))).scalars().all()
    return {row.name: row.id for row in rows}


@pytest.mark.asyncio
async def test_queued_yield_counts_a_pending_print_queue_row(committing_client, db_session, catalog):
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    parts = await _parts_of(db_session, catalog["product"].id)
    queue = PrinterQueue(id=1, printer_id=1)
    db_session.add(queue)
    await db_session.flush()
    db_session.add(
        PrintQueueItem(queue_id=queue.id, project_line_id=line_id, library_file_id=catalog["file"].id, status="pending")
    )
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {parts["shade"]: 1, parts["arm"]: 2}}


@pytest.mark.asyncio
async def test_queued_yield_counts_a_pending_unassigned_auto_queue_row(committing_client, db_session, catalog):
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    parts = await _parts_of(db_session, catalog["product"].id)
    db_session.add(AutoQueueItem(project_line_id=line_id, library_file_id=catalog["file"].id, status="pending"))
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {parts["shade"]: 1, parts["arm"]: 2}}


@pytest.mark.asyncio
async def test_queued_yield_ignores_an_assigned_auto_queue_row(committing_client, db_session, catalog):
    """It is already counted through the printer item it was copied into."""
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    db_session.add(
        AutoQueueItem(
            project_line_id=line_id,
            library_file_id=catalog["file"].id,
            status="pending",
            assigned_to_item_id=4242,
        )
    )
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {}}


@pytest.mark.asyncio
async def test_queued_yield_counts_only_the_parts_the_line_counts(committing_client, db_session, catalog):
    """The queued bucket is filtered exactly as a PLANNED plate's yield is.

    A plate is shared (two products' objects on one bed) and a product zeroes a
    part it does not want counted — ``qty_per_unit = 0``, the modelling
    convention the shared-plate rule is built on. Those instances are not this
    line's incoming work, so ``line_yield`` keeps them out of the plan; the
    queued map used to be keyed on the raw ``yield_by_part`` and named parts no
    figure of the line ever mentions. It is a public map, so that is wrong even
    where the subtraction downstream survives it.
    """
    product_id = catalog["product"].id
    db_session.add(
        ProductPart(
            product_id=product_id, kind="printed", name="clip", name_key="clip", qty_per_unit=0, aliases=["clip"]
        )
    )
    shared = LibraryFile(
        filename="shared.gcode.3mf",
        file_path="shared",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [{"index": 1, "printable_objects": {"1": "shade", "2": "clip"}, "print_time_seconds": 100}]
        },
    )
    db_session.add(shared)
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product_id, library_file_id=shared.id, plate_index=0))
    await db_session.commit()
    shared_id = shared.id

    pid, line_id = await _order_with_line(committing_client, product_id, 4)
    parts = await _parts_of(db_session, product_id)
    db_session.add(AutoQueueItem(project_line_id=line_id, library_file_id=shared_id, status="pending"))
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {parts["shade"]: 1}}


@pytest.mark.asyncio
async def test_queued_yield_takes_the_exact_plate_index(committing_client, db_session, catalog):
    """Two plates of one file, each its own product plate: the row's ``plate_id``
    picks one of them, never the other and never their sum."""
    two = LibraryFile(
        filename="two.gcode.3mf",
        file_path="two",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {"index": 1, "printable_objects": {"1": "shade"}, "print_time_seconds": 50},
                {"index": 2, "printable_objects": {"1": "arm", "2": "arm_2"}, "print_time_seconds": 70},
            ]
        },
    )
    db_session.add(two)
    await db_session.flush()
    db_session.add_all(
        [
            ProductPlate(product_id=catalog["product"].id, library_file_id=two.id, plate_index=1),
            ProductPlate(product_id=catalog["product"].id, library_file_id=two.id, plate_index=2),
        ]
    )
    await db_session.commit()
    two_id = two.id
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    parts = await _parts_of(db_session, catalog["product"].id)

    db_session.add(AutoQueueItem(project_line_id=line_id, library_file_id=two_id, plate_id=2, status="pending"))
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {parts["arm"]: 2}}


@pytest.mark.asyncio
async def test_queued_yield_falls_through_to_the_whole_file_plate(committing_client, db_session, catalog):
    """The product holds ONE plate at index 0 - the whole file - while the queue
    row carries the slicer's own index. The exact lookup misses and the
    whole-file plate claims it; a row naming no plate hits index 0 outright."""
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "O",
                "lines": [
                    {"product_id": catalog["product"].id, "quantity": 4, "material": "PETG"},
                    {"product_id": catalog["product"].id, "quantity": 4, "material": "PETG"},
                ],
            },
        )
    ).json()
    pid = body["id"]
    first, second = body["lines"][0]["id"], body["lines"][1]["id"]
    parts = await _parts_of(db_session, catalog["product"].id)
    db_session.add_all(
        [
            AutoQueueItem(project_line_id=first, library_file_id=catalog["file"].id, plate_id=1, status="pending"),
            AutoQueueItem(project_line_id=second, library_file_id=catalog["file"].id, plate_id=None, status="pending"),
        ]
    )
    await db_session.commit()

    yielded = await _queued_yield(db_session, pid)
    assert yielded[first] == {parts["shade"]: 1, parts["arm"]: 2}
    assert yielded[second] == {parts["shade"]: 1, parts["arm"]: 2}


@pytest.mark.asyncio
async def test_queued_yield_counts_nothing_for_a_file_the_product_does_not_have(committing_client, db_session, catalog):
    """The file was unlinked, or the row points somewhere else entirely. There
    is no yield to invent - and the same goes for a row queued from an archive,
    which names no library file at all."""
    stray = LibraryFile(filename="stray.gcode.3mf", file_path="stray", file_size=1, file_type="gcode")
    db_session.add(stray)
    await db_session.flush()
    stray_id = stray.id
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    db_session.add_all(
        [
            AutoQueueItem(project_line_id=line_id, library_file_id=stray_id, status="pending"),
            AutoQueueItem(project_line_id=line_id, archive_id=1, status="pending"),
        ]
    )
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {}}


# ---------- the implicit branch: rows filed under the ORDER and no line ----------
#
# The writers file the line for every NEW row they can (spec pass 7, Decision
# 4a). These rows are the other half: legacy, Telegram, hand-written — an order
# id and no line. ``queued_yield_by_line`` resolves them the same way, plate →
# product → material, on every read. Both halves are needed; neither argues the
# other away.


async def _unfiled_rows(db, project_id, file_id, plate_id=None):
    """One row in each queue table carrying the ORDER and no line."""
    queue = PrinterQueue(id=1, printer_id=1)
    db.add(queue)
    await db.flush()
    db.add_all(
        [
            PrintQueueItem(
                queue_id=queue.id, project_id=project_id, library_file_id=file_id, plate_id=plate_id, status="pending"
            ),
            AutoQueueItem(project_id=project_id, library_file_id=file_id, plate_id=plate_id, status="pending"),
        ]
    )
    await db.commit()


@pytest.mark.asyncio
async def test_queued_yield_counts_a_line_less_row_of_this_order(committing_client, db_session, catalog):
    """The live incident, as a test: rows carrying ``project_id`` and no
    ``project_line_id`` used to count nowhere, so the plan block kept asking for
    plates the operator had already queued."""
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    parts = await _parts_of(db_session, catalog["product"].id)
    await _unfiled_rows(db_session, pid, catalog["file"].id)

    # One print-queue row and one auto-queue row, both resolved to the only line.
    assert await _queued_yield(db_session, pid) == {line_id: {parts["shade"]: 2, parts["arm"]: 4}}


@pytest.mark.asyncio
async def test_queued_yield_ignores_a_line_less_row_of_another_order(committing_client, db_session, catalog):
    """The implicit branch names THIS order and only it. A row filed under
    somebody else's order is somebody else's incoming work, whatever plate it
    carries."""
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    other, _other_line = await _order_with_line(committing_client, catalog["product"].id, 4)
    await _unfiled_rows(db_session, other, catalog["file"].id)

    assert await _queued_yield(db_session, pid) == {line_id: {}}


@pytest.mark.asyncio
async def test_queued_yield_ignores_a_line_less_row_whose_plate_belongs_to_no_line(
    committing_client, db_session, catalog
):
    """A file none of the order's products holds resolves to no line, and there
    is no yield to invent — the same answer a row filed under a line gets when
    its file was unlinked."""
    stray = LibraryFile(filename="stray.gcode.3mf", file_path="stray", file_size=1, file_type="gcode")
    db_session.add(stray)
    await db_session.flush()
    stray_id = stray.id
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    await _unfiled_rows(db_session, pid, stray_id)

    assert await _queued_yield(db_session, pid) == {line_id: {}}


@pytest.mark.asyncio
async def test_queued_yield_refuses_to_guess_between_two_alike_lines(committing_client, db_session, catalog):
    """Two lines of the same product and the same material: the plate cannot
    tell them apart, so a line-less row counts NOWHERE rather than being dealt
    to whichever line sorts first. The material rule is what makes the ordinary
    two-line order answerable — the second half of this test."""
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "Twins",
                "lines": [
                    {"product_id": catalog["product"].id, "quantity": 4, "material": "PETG"},
                    {"product_id": catalog["product"].id, "quantity": 4, "material": "PETG"},
                ],
            },
        )
    ).json()
    pid, first, second = body["id"], body["lines"][0]["id"], body["lines"][1]["id"]
    await _unfiled_rows(db_session, pid, catalog["file"].id)

    assert await _queued_yield(db_session, pid) == {first: {}, second: {}}

    narrowed = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "Narrowed",
                "lines": [
                    {"product_id": catalog["product"].id, "quantity": 4, "material": "PLA"},
                    {"product_id": catalog["product"].id, "quantity": 4, "material": "PETG"},
                ],
            },
        )
    ).json()
    parts = await _parts_of(db_session, catalog["product"].id)
    auto = AutoQueueItem(project_id=narrowed["id"], library_file_id=catalog["file"].id, status="pending")
    db_session.add(auto)
    await db_session.commit()

    yielded = await _queued_yield(db_session, narrowed["id"])
    assert yielded[narrowed["lines"][0]["id"]] == {}
    assert yielded[narrowed["lines"][1]["id"]] == {parts["shade"]: 1, parts["arm"]: 2}


@pytest.mark.asyncio
async def test_a_row_is_never_counted_twice_by_the_two_branches(committing_client, db_session, catalog):
    """The branches are disjoint by construction — ``project_line_id IN lines``
    against ``IS NULL`` — so a row carrying BOTH ids is counted once, by the
    line branch, and the implicit one never sees it."""
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 4)
    parts = await _parts_of(db_session, catalog["product"].id)
    db_session.add(
        AutoQueueItem(project_id=pid, project_line_id=line_id, library_file_id=catalog["file"].id, status="pending")
    )
    await db_session.commit()

    assert await _queued_yield(db_session, pid) == {line_id: {parts["shade"]: 1, parts["arm"]: 2}}


@pytest.mark.asyncio
async def test_line_products_carries_a_cover_flag_per_line_in_line_order(committing_client, db_session, catalog):
    """The order card draws a cover strip from ``line_products`` — one entry per
    line, in line order, saying whether that product has a cover to draw at all
    (spec §Decisions 4). It replaces ``product_cover_filenames``: the effective
    cover may be the first picture attachment, which is not a column."""
    plain = (await committing_client.post("/api/v1/products/", json={"name": "Plain"})).json()["id"]
    covered = catalog["product"].id
    up = await committing_client.post(
        f"/api/v1/products/{covered}/attachments",
        data={"category": "pictures"},
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
    )
    assert up.status_code == 200, up.text

    pid = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "Strip",
                "lines": [{"product_id": plain, "quantity": 1}, {"product_id": covered, "quantity": 1}],
            },
        )
    ).json()["id"]

    row = next(p for p in (await committing_client.get("/api/v1/projects/")).json() if p["id"] == pid)
    assert row["line_products"] == [
        {"product_id": plain, "has_cover": False},
        {"product_id": covered, "has_cover": True},
    ]

    # ⚠️ "line order" is ``(sort_order, id)`` — what every figure path sorts by —
    # and NOT the order the relationship happens to load in. Move the first line
    # behind the second and the strip must follow, or the card and the order page
    # it opens disagree about which product comes first.
    first = (
        await db_session.execute(
            select(ProjectLine).where(ProjectLine.project_id == pid, ProjectLine.product_id == plain)
        )
    ).scalar_one()
    first.sort_order = 5
    await db_session.commit()

    row = next(p for p in (await committing_client.get("/api/v1/projects/")).json() if p["id"] == pid)
    assert row["line_products"] == [
        {"product_id": covered, "has_cover": True},
        {"product_id": plain, "has_cover": False},
    ]


@pytest.mark.asyncio
async def test_the_orders_list_reports_what_each_order_page_reports(committing_client, db_session):
    """The list stopped loading an order context per row. The figures it prints
    are hand-written in ``build_parity_fixture`` - and must still equal, order
    for order, what the detail endpoint answers."""
    ids = await build_parity_fixture(db_session)

    rows = {r["name"]: r for r in (await committing_client.get("/api/v1/projects/")).json()}
    live, dead = rows["Parity live order"], rows["Parity cancelled order"]
    assert (live["ordered"], live["printed"], live["progress"]) == (3, 4, 1.0)
    assert (dead["ordered"], dead["printed"], dead["progress"]) == (1, 0, 0.0)
    assert (live["lines_count"], dead["lines_count"]) == (2, 1)

    for row, project_id in ((live, ids["live"]), (dead, ids["dead"])):
        figures = (await committing_client.get(f"/api/v1/projects/{project_id}")).json()["figures"]
        assert (row["ordered"], row["printed"], row["progress"]) == (
            figures["ordered"],
            figures["printed"],
            figures["progress"],
        )


@pytest.fixture
async def three_product_order(committing_client, db_session):
    """An order with three lines, each on its own product and its own sliced file."""
    files = [
        LibraryFile(
            filename=f"p{i}.gcode.3mf",
            file_path=f"p{i}",
            file_size=1,
            file_type="gcode",
            file_metadata={
                "plates": [
                    {
                        "index": 1,
                        "printable_objects": {"1": f"body{i}"},
                        "print_time_seconds": 100,
                        "filaments": [{"slot_id": 1, "type": "PETG"}],
                    }
                ]
            },
        )
        for i in range(3)
    ]
    products = [Product(name=f"Widget {i}") for i in range(3)]
    db_session.add_all([*files, *products])
    await db_session.flush()
    for i, (product, file) in enumerate(zip(products, files, strict=True)):
        db_session.add_all(
            [
                ProductPart(
                    product_id=product.id,
                    kind="printed",
                    name=f"body{i}",
                    name_key=f"body{i}",
                    qty_per_unit=1,
                    aliases=[f"body{i}"],
                ),
                ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0),
            ]
        )
    await db_session.commit()
    body = (
        await committing_client.post(
            "/api/v1/projects/",
            json={
                "name": "Three",
                "lines": [{"product_id": p.id, "quantity": 2, "material": "PETG"} for p in products],
            },
        )
    ).json()
    return body["id"], [line["id"] for line in body["lines"]], [p.id for p in products]


@pytest.mark.asyncio
async def test_the_plan_loads_every_products_recipes_in_one_batch(
    committing_client, test_engine, monkeypatch, three_product_order
):
    """One order, three products, ONE recipe load — not one per product.

    The plan is computed on every read of the order page, so the per-product
    loop was a round trip per line on a page nobody thinks of as expensive.
    """
    pid, _lines, _products = three_product_order
    calls = {"batched": 0}
    real = plan_engine.recipes_for_products

    async def counting(db, products):
        calls["batched"] += 1
        return await real(db, products)

    monkeypatch.setattr(plan_engine, "recipes_for_products", counting)
    assert not hasattr(plan_engine, "recipes_for_product"), (
        "the engine holds the batch only — a per-product name here is the N+1 growing back"
    )

    with counting_statements(test_engine, match="library_files") as seen:
        r = await committing_client.get(f"/api/v1/projects/{pid}/plan")

    assert r.status_code == 200, r.text
    assert len(r.json()["lines"]) == 3
    assert all(line["rows"] for line in r.json()["lines"]), "each line still gets its own plate to print"
    assert calls["batched"] == 1
    assert len(seen) == 1, seen


@pytest.mark.asyncio
async def test_enqueue_loads_the_products_and_their_recipes_once_for_the_whole_call(
    committing_client, db_session, test_engine, monkeypatch, three_product_order
):
    """The handler used to load a product AND build its recipes per distinct
    product of the request — two round trips per line, on the write path."""
    pid, lines, _products = three_product_order
    plan = (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()
    items = [
        {"plate_id": line["rows"][0]["plate_id"], "count": 1, "line_id": line["line_id"]} for line in plan["lines"]
    ]
    assert len({item["plate_id"] for item in items}) == 3

    calls = {"batched": 0}
    real = projects_routes.recipes_for_products

    async def counting(db, products):
        calls["batched"] += 1
        return await real(db, products)

    monkeypatch.setattr(projects_routes, "recipes_for_products", counting)
    assert not hasattr(projects_routes, "recipes_for_product"), (
        "the handler holds the batch only — a per-product name here is the N+1 growing back"
    )

    with counting_statements(test_engine, match="FROM products") as products_seen:
        r = await committing_client.post(
            f"/api/v1/projects/{pid}/plan/enqueue", json={"items": items, "target": {"kind": "auto"}}
        )

    assert r.status_code == 200, r.text
    assert [c["line_id"] for c in r.json()["created"]] == lines
    # One product load and one recipe load for the whole request. The queue
    # writers below still read their own file per item — that is their business,
    # and it is why only the handler's own two loads are counted here.
    assert calls["batched"] == 1
    assert len(products_seen) == 1, products_seen

    rows = (await db_session.execute(select(AutoQueueItem))).scalars().all()
    assert sorted(row.project_line_id for row in rows) == sorted(lines)


# ── The plan's enqueue applies the operator's print-options preference ───────
#
# The print dialog reads ``print_options_preferences`` before it builds a queue
# payload; the plan block sends the same work with no dialog in front of it.
# Until 2026-09-04 it therefore wrote the writers' own defaults, and a farm
# configured to run swap macros printed without them.

#: Every toggle set AWAY from the writers' own default, so an assertion that
#: finds the profile value cannot be reading a coincidence.
_PROFILE_TOGGLES = {
    "bed_levelling": "auto",  # writers: "on"
    "flow_cali": "off",  # writers: "on"
    "layer_inspect": True,  # writers: False
    "timelapse": True,  # writers: False
    "timelapse_storage": "external",  # writers: None
    "mesh_mode_fast_check": False,  # writers: True
    "gcode_injection": True,  # printer writer only; its default is False
    "nozzle_offset_cali": "off",  # printer writer only; its default is "on"
    # In the profile, but NO writer takes either — pinned below on the queue row.
    "preheat_override": "on",
    "preheat_chamber_target_override": 45,
}
_SWAP_EVENTS = ["swap_mode_start", "swap_mode_change_table"]


async def _the_dialogs_saved_preference(
    db, *, printer_model="P1S", deselected_macro_ids=(), events=tuple(_SWAP_EVENTS)
):
    """The row a PrintModal submit leaves behind for ``test_admin``."""
    user_id = (await db.execute(select(User.id).where(User.username == "test_admin"))).scalar_one()
    db.add(
        PrintOptionsPreference(
            user_id=user_id,
            printer_model=printer_model,
            options=PrintOptionsPreferenceData.model_validate(
                {
                    "print_options": dict(_PROFILE_TOGGLES),
                    "swap_macros": {"execute": True, "events": list(events)},
                    "event_macros": {"deselected_ids": list(deselected_macro_ids)},
                }
            ).model_dump(),
        )
    )
    await db.commit()


async def _three_macros(db):
    """Two event macros and a swap macro — the swap one has its own fields and
    its own trigger, so it must never reach ``selected_macro_ids``."""
    ticked = Macro(name="lights on", event="print_started", gcode="M355 S1", printer_models='["*"]')
    unticked = Macro(name="beep", event="print_completed", gcode="M300", printer_models='["*"]')
    swap = Macro(name="change table", event="swap_mode_change_table", gcode="G28", printer_models='["*"]')
    db.add_all([ticked, unticked, swap])
    await db.commit()
    return ticked.id, unticked.id, swap.id


async def _swap_printer(db, *, swap_mode_enabled=True, model="P1S"):
    printer = Printer(
        name="Pswap",
        serial_number="SW1",
        ip_address="10.0.0.7",
        access_code="1234",
        model=model,
        swap_mode_enabled=swap_mode_enabled,
    )
    db.add(printer)
    await db.flush()
    db.add(PrinterQueue(printer_id=printer.id))
    await db.commit()
    return printer.id


async def _written_rows(db, model, ids):
    rows = (await db.execute(select(model).where(model.id.in_(ids)))).scalars().all()
    assert len(rows) == len(ids)
    return rows


def _assert_carries_the_shared_profile(row):
    """The nine options BOTH writers take, on a row of either queue."""
    assert row.layer_inspect is True
    assert row.timelapse is True
    assert row.timelapse_storage == "external"
    assert row.mesh_mode_fast_check is False
    assert row.execute_swap_macros is True
    assert json.loads(row.swap_macro_events) == _SWAP_EVENTS


@pytest.mark.asyncio
async def test_plan_enqueue_to_the_auto_queue_carries_the_saved_profile(committing_client, db_session, catalog):
    """The whole profile the dialog spreads, on the auto target.

    ``catalog["file"]`` names no ``sliced_for_model``, so this also pins the
    fallback: with no model to key by, the operator's most recent row is used.
    """
    ticked, unticked, _swap = await _three_macros(db_session)
    await _the_dialogs_saved_preference(db_session, deselected_macro_ids=[unticked])
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={"items": [{"plate_id": plate_id, "count": 2, "line_id": line_id}], "target": {"kind": "auto"}},
    )
    assert r.status_code == 200, r.text
    rows = await _written_rows(db_session, AutoQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert len(rows) == 2
    for row in rows:
        _assert_carries_the_shared_profile(row)
        # The preference stores the EXCEPTIONS, so the unticked macro is the
        # only one missing — and the swap macro is not an event macro at all.
        assert json.loads(row.selected_macro_ids) == [ticked]
        # 'auto' and 'off' both mirror to False: the auto-queue has no ``*_mode``
        # column, which is why the mode itself only survives the per-printer
        # tier. Both differ from the schema's own ``"on"`` default.
        assert row.bed_levelling is False and row.flow_cali is False
        # ⚠️ The two the printer writer alone takes do NOT arrive here.
        # ``AutoQueueItemCreate`` has no field for either — the row has a
        # ``gcode_injection`` column and keeps its default, and no
        # ``nozzle_offset_cali`` column at all. That asymmetry is the writers',
        # not this handler's to smooth over.
        assert row.gcode_injection is False
        assert not hasattr(row, "nozzle_offset_cali")


@pytest.mark.asyncio
async def test_plan_enqueue_to_a_printer_carries_the_saved_profile(committing_client, db_session, catalog):
    """Same profile, the other target — plus the two fields only this writer
    takes, and the tri-state, which only this row can keep."""
    ticked, unticked, _swap = await _three_macros(db_session)
    await _the_dialogs_saved_preference(db_session, deselected_macro_ids=[unticked])
    printer_id = await _swap_printer(db_session)
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [{"plate_id": plate_id, "count": 2, "line_id": line_id}],
            "target": {"kind": "printer", "printer_id": printer_id},
        },
    )
    assert r.status_code == 200, r.text
    rows = await _written_rows(db_session, PrintQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert len(rows) == 2
    for row in rows:
        _assert_carries_the_shared_profile(row)
        assert json.loads(row.selected_macro_ids) == [ticked]
        assert row.bed_levelling is False and row.bed_levelling_mode == "auto"
        assert row.flow_cali is False and row.flow_cali_mode == "off"
        # Only this writer takes these two.
        assert row.gcode_injection is True
        assert row.nozzle_offset_cali is False and row.nozzle_offset_cali_mode == "off"
        # ⚠️ The profile carries a preheat override, and NO writer takes it. The
        # column keeps its own default rather than the handler inventing a path
        # for it — pinned so a later "why is preheat ignored here" reads as a
        # decision instead of an oversight.
        assert row.preheat_override == "inherit"
        assert row.preheat_chamber_target_override is None


@pytest.mark.asyncio
async def test_plan_enqueue_reads_the_profile_for_the_model_the_file_was_sliced_for(
    committing_client, db_session, catalog
):
    """The auto target names no machine but the FILE names a model, and it is
    the model the auto-queue will route by — so it is the model the profile is
    keyed on. One request whose plates were sliced for two machines reads two
    profiles."""
    await _name_the_catalog_file_a_model(db_session, catalog["file"], "X1C")
    twin_file, twin_plate = await _twin_plate(
        db_session, catalog["product"], filename="lamp-p1s.gcode.3mf", model="P1S", seconds=200
    )
    await _the_dialogs_saved_preference(db_session, printer_model="X1C", events=["swap_mode_start"])
    await _the_dialogs_saved_preference(db_session, printer_model="P1S", events=["swap_mode_change_table"])
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [
                {"plate_id": plate_id, "count": 1, "line_id": line_id},
                {"plate_id": twin_plate.id, "count": 1, "line_id": line_id},
            ],
            "target": {"kind": "auto"},
        },
    )
    assert r.status_code == 200, r.text
    ids = [i for c in r.json()["created"] for i in c["queue_item_ids"]]
    rows = await _written_rows(db_session, AutoQueueItem, ids)
    by_file = {row.library_file_id: row for row in rows}
    # Each row took the events of the row saved for ITS file's model — not the
    # most recent row, which is the P1S one and would have won everywhere.
    assert json.loads(by_file[catalog["file"].id].swap_macro_events) == ["swap_mode_start"]
    assert json.loads(by_file[twin_file.id].swap_macro_events) == ["swap_mode_change_table"]


@pytest.mark.asyncio
async def test_plan_enqueue_finds_a_preference_saved_under_a_longer_model_name(committing_client, db_session, catalog):
    """The two sides spell the model differently, and the lookup normalises both.

    The dialog saves under whatever the PRINTER row calls itself ("Bambu Lab X1
    Carbon", or an internal code like "C12"); the plan keys on the 3MF's
    ``sliced_for_model``, already normalised to "X1C". A ``WHERE printer_model =
    :model`` missed a preference that plainly existed, and missed it silently —
    the writers' defaults applied and nobody was told.
    """
    await _name_the_catalog_file_a_model(db_session, catalog["file"], "X1C")
    await _the_dialogs_saved_preference(db_session, printer_model="Bambu Lab X1 Carbon", events=["swap_mode_start"])
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)
    item = {"plate_id": plate_id, "count": 1, "line_id": line_id}

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue", json={"items": [item], "target": {"kind": "auto"}}
    )
    assert r.status_code == 200, r.text
    (row,) = await _written_rows(db_session, AutoQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert json.loads(row.swap_macro_events) == ["swap_mode_start"]
    # The whole profile arrived, not just the field the match was checked on.
    assert row.timelapse is True and row.mesh_mode_fast_check is False

    # ⚠️ And matching NORMALISED is still matching: a row saved for another
    # machine is not borrowed. Widening is exactly what a too-eager comparison
    # would do, and it would be as silent as the miss it replaced.
    await db_session.execute(delete(PrintOptionsPreference))
    await _the_dialogs_saved_preference(db_session, printer_model="Bambu Lab P1S", events=["swap_mode_start"])
    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue", json={"items": [item], "target": {"kind": "auto"}}
    )
    assert r.status_code == 200, r.text
    (other,) = await _written_rows(db_session, AutoQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert other.swap_macro_events is None and other.mesh_mode_fast_check is True


@pytest.mark.asyncio
async def test_the_event_macros_of_a_long_named_printer_are_matched_on_its_short_name(
    committing_client, db_session, catalog
):
    """⚠️ One normalising rule for the whole profile lookup, not just half of it.

    ``_saved_preference`` already compared the model through
    ``normalize_model_name``; the macro narrowing beside it compared raw. So a
    printer row spelled "Bambu Lab X1 Carbon" found its saved profile and then
    matched NO macro at all, because the macro editor writes short names — and an
    empty ``selected_macro_ids`` is indistinguishable from "the operator ticked
    nothing", so the print simply ran without its macros and said nothing.
    """
    targeted = Macro(name="lights on", event="print_started", gcode="M355 S1", printer_models='["X1C"]')
    elsewhere = Macro(name="p1s only", event="print_started", gcode="M300", printer_models='["P1S"]')
    db_session.add_all([targeted, elsewhere])
    await db_session.commit()
    await _the_dialogs_saved_preference(db_session, printer_model="Bambu Lab X1 Carbon")
    printer_id = await _swap_printer(db_session, model="Bambu Lab X1 Carbon")
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={
            "items": [{"plate_id": plate_id, "count": 1, "line_id": line_id}],
            "target": {"kind": "printer", "printer_id": printer_id},
        },
    )
    assert r.status_code == 200, r.text
    (row,) = await _written_rows(db_session, PrintQueueItem, r.json()["created"][0]["queue_item_ids"])
    # The X1C macro is ticked and the P1S one is not: normalising is still
    # MATCHING — it does not widen a macro onto another machine.
    assert json.loads(row.selected_macro_ids) == [targeted.id]


@pytest.mark.asyncio
async def test_plan_enqueue_without_a_preference_keeps_the_writers_defaults(committing_client, db_session, catalog):
    """Nothing saved changes nothing: the two writers' own defaults, pinned —
    including the asymmetry between them, which is theirs and not this
    handler's to smooth over. The printer here has swap mode ON and the file
    carries no baked-in macros, so the mute below is not in play."""
    printer_id = await _swap_printer(db_session)
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)
    item = {"plate_id": plate_id, "count": 1, "line_id": line_id}

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue", json={"items": [item], "target": {"kind": "auto"}}
    )
    assert r.status_code == 200, r.text
    (auto_row,) = await _written_rows(db_session, AutoQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert auto_row.execute_swap_macros is True  # ``AutoQueueItemCreate``'s own default
    assert auto_row.swap_macro_events is None
    assert auto_row.selected_macro_ids is None
    assert auto_row.bed_levelling is True and auto_row.flow_cali is True
    assert auto_row.layer_inspect is False and auto_row.timelapse is False
    assert auto_row.timelapse_storage is None and auto_row.mesh_mode_fast_check is True

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={"items": [item], "target": {"kind": "printer", "printer_id": printer_id}},
    )
    assert r.status_code == 200, r.text
    (queue_row,) = await _written_rows(db_session, PrintQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert queue_row.execute_swap_macros is False  # ``enqueue_batch_copies``' own default
    assert queue_row.swap_macro_events is None
    assert queue_row.selected_macro_ids is None
    assert queue_row.bed_levelling is True and queue_row.bed_levelling_mode == "on"
    assert queue_row.flow_cali is True and queue_row.flow_cali_mode == "on"
    assert queue_row.layer_inspect is False and queue_row.timelapse is False
    assert queue_row.timelapse_storage is None and queue_row.mesh_mode_fast_check is True
    assert queue_row.gcode_injection is False
    assert queue_row.nozzle_offset_cali is True and queue_row.nozzle_offset_cali_mode == "on"


@pytest.mark.asyncio
async def test_plan_enqueue_mutes_swap_macros_that_would_fire_twice(committing_client, db_session, catalog):
    """The rule every other queue door applies (``queue_add``, both print-now
    routes): swap macros are meaningful only on a printer with swap mode ON and
    a file that does not already carry them baked in by third-party tooling.
    Carrying the profile here without that gate would run the plate change
    twice — the printer target used to default the flag to False, so nothing
    ever asked the question on this path before."""
    await _the_dialogs_saved_preference(db_session)
    printer_id = await _swap_printer(db_session, swap_mode_enabled=False)
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)
    item = {"plate_id": plate_id, "count": 1, "line_id": line_id}

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={"items": [item], "target": {"kind": "printer", "printer_id": printer_id}},
    )
    assert r.status_code == 200, r.text
    (row,) = await _written_rows(db_session, PrintQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert row.execute_swap_macros is False and row.swap_macro_events is None
    # Only the swap half is muted — the rest of the same profile has nothing to
    # do with the gate.
    assert row.bed_levelling_mode == "auto" and row.flow_cali_mode == "off"
    assert row.timelapse is True

    # The file's own baked-in macros mute it on either target, printer or not.
    catalog["file"].swap_compatible = True
    await db_session.commit()
    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue", json={"items": [item], "target": {"kind": "auto"}}
    )
    assert r.status_code == 200, r.text
    (auto_row,) = await _written_rows(db_session, AutoQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert auto_row.execute_swap_macros is False and auto_row.swap_macro_events is None
    assert auto_row.bed_levelling is False


@pytest.mark.asyncio
async def test_plan_enqueue_mutes_a_baked_in_file_even_with_no_preference(committing_client, db_session, catalog):
    """⚠️ The gate is UNCONDITIONAL, not "only when a profile turned them on".

    ``AutoQueueItemCreate`` defaults ``execute_swap_macros`` to True, so a
    ``swap_compatible`` file with nothing saved at all would double-fire through
    the writer's own default — the one case where the writers' defaults are NOT
    left alone, and deliberately."""
    catalog["file"].swap_compatible = True
    await db_session.commit()
    pid, line_id = await _order_with_line(committing_client, catalog["product"].id, 10)
    plate_id = await _plate_id_of_the_only_row(committing_client, pid)

    r = await committing_client.post(
        f"/api/v1/projects/{pid}/plan/enqueue",
        json={"items": [{"plate_id": plate_id, "count": 1, "line_id": line_id}], "target": {"kind": "auto"}},
    )
    assert r.status_code == 200, r.text
    (row,) = await _written_rows(db_session, AutoQueueItem, r.json()["created"][0]["queue_item_ids"])
    assert row.execute_swap_macros is False and row.swap_macro_events is None
    # Nothing else moved: the writer's other defaults still stand.
    assert row.bed_levelling is True and row.mesh_mode_fast_check is True


# ---------- pass 8, Decision 2: banking an order's surplus ----------


async def _two_lines(client, catalog, quantity: int = 1) -> tuple[int, dict, dict]:
    """An order with two lines of the same product. The greedy pass fills each
    line's need in ``sort_order`` and drops the leftover on the FIRST — so line
    one is the overprinted one and line two comes out exact, which is the pair
    Decision 2 is about."""
    r = await client.post(
        "/api/v1/projects/",
        json={
            "name": "Lamps",
            "lines": [
                {"product_id": catalog["product"].id, "quantity": quantity, "material": "PETG"},
                {"product_id": catalog["product"].id, "quantity": quantity, "material": "PETG"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["id"], body["lines"][0], body["lines"][1]


async def _banked(db) -> list[ProductPartStockMovement]:
    db.expire_all()
    rows = await db.execute(select(ProductPartStockMovement).where(ProductPartStockMovement.reason == "surplus_banked"))
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_banking_an_overprinted_line_moves_the_surplus_to_free_stock(committing_client, db_session, catalog):
    """Seven prints against two lines of one each: two prints cover the need and
    five are surplus — 5 shades and 10 arms (the arm is wanted twice per unit).
    Nothing is banked until the button is pressed, because a surplus is
    sometimes shipped and sometimes scrapped and only the operator knows."""
    project_id, first, second = await _two_lines(committing_client, catalog)
    for _ in range(7):
        await _completed_print(db_session, project_id, catalog["file"].id)

    assert await _banked(db_session) == []

    r = await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nothing_to_bank"] is False
    assert {m["name"]: m["delta"] for m in body["moved"]} == {"shade": 5, "arm": 10}
    rows = await _banked(db_session)
    # Every movement names the LINE it came from — that is what "already banked"
    # is counted by on the next press, and it is the only trail back to the order.
    assert {row.project_line_id for row in rows} == {first["id"]}
    assert second["units_printed"] == 0  # the fixture's lines start empty


@pytest.mark.asyncio
async def test_pressing_bank_again_moves_nothing(committing_client, db_session, catalog):
    """A second press is a success, not an error: the surplus is already on the
    shelf. Without the ``already banked`` subtraction it would be banked twice."""
    project_id, _first, _second = await _two_lines(committing_client, catalog)
    for _ in range(7):
        await _completed_print(db_session, project_id, catalog["file"].id)
    await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    r = await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    assert r.status_code == 200, r.text
    assert r.json() == {"moved": [], "nothing_to_bank": True}
    assert len(await _banked(db_session)) == 2


@pytest.mark.asyncio
async def test_a_surplus_that_grows_later_banks_only_the_difference(committing_client, db_session, catalog):
    """The button can be pressed whenever, so what it moves is the surplus MINUS
    what this line has already banked — never the whole surplus again."""
    project_id, first, _second = await _two_lines(committing_client, catalog)
    for _ in range(7):
        await _completed_print(db_session, project_id, catalog["file"].id)
    await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    await _completed_print(db_session, project_id, catalog["file"].id)
    r = await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    assert r.status_code == 200, r.text
    assert {m["name"]: m["delta"] for m in r.json()["moved"]} == {"shade": 1, "arm": 2}
    totals = Counter()
    for row in await _banked(db_session):
        assert row.project_line_id == first["id"]
        totals[row.product_part_id] += row.delta
    assert sorted(totals.values()) == [6, 12]


@pytest.mark.asyncio
async def test_a_cancelled_order_can_still_bank_its_surplus(committing_client, db_session, catalog):
    """The parts came off a bed regardless of what happened to the order, and
    the ones from an order nobody will ship are the most worth keeping."""
    project_id, _first, _second = await _two_lines(committing_client, catalog)
    for _ in range(7):
        await _completed_print(db_session, project_id, catalog["file"].id)
    assert (await committing_client.patch(f"/api/v1/projects/{project_id}", json={"status": "cancelled"})).status_code
    r = await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    assert r.status_code == 200, r.text
    assert {m["name"]: m["delta"] for m in r.json()["moved"]} == {"shade": 5, "arm": 10}


@pytest.mark.asyncio
async def test_an_order_printed_exactly_has_nothing_to_bank(committing_client, db_session, catalog):
    """The ordinary case, and the one that must not write a ledger of zeroes."""
    project_id, _first, _second = await _two_lines(committing_client, catalog)
    for _ in range(2):
        await _completed_print(db_session, project_id, catalog["file"].id)

    r = await committing_client.post(f"/api/v1/projects/{project_id}/bank-surplus")

    assert r.json() == {"moved": [], "nothing_to_bank": True}
    assert await _banked(db_session) == []


@pytest.mark.asyncio
async def test_banking_an_order_that_does_not_exist_is_404(committing_client):
    assert (await committing_client.post("/api/v1/projects/9999/bank-surplus")).status_code == 404


@pytest.mark.asyncio
async def test_filing_an_order_less_print_under_an_order_takes_its_stock_back(committing_client, db_session, catalog):
    """«Додати архіви» is the order page's own filing button, and filing is
    filing: the same reversal the archive editor does (pass 8, Decision 3).
    Without it the parts would sit on the shelf AND count towards the order."""
    archive = await _completed_print(db_session, None, catalog["file"].id)
    credited = await part_stock.credit_unfiled_print(db_session, archive)
    await db_session.commit()
    assert [m.delta for m in credited] == [1, 2], "one shade and two arms went to free stock"

    project_id = (
        await committing_client.post(
            "/api/v1/projects/",
            json={"name": "Lamps", "lines": [{"product_id": catalog["product"].id, "quantity": 1}]},
        )
    ).json()["id"]
    r = await committing_client.post(f"/api/v1/projects/{project_id}/add-archives", json={"archive_ids": [archive.id]})

    assert r.status_code == 200, r.text
    db_session.expire_all()
    rows = (await db_session.execute(select(ProductPartStockMovement))).scalars().all()
    assert sum(row.delta for row in rows) == 0
    assert {row.note for row in rows if row.reason == "manual"} == {"filed under order Lamps"}
