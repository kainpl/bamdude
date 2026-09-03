"""Orders: lines, figures, procurement, lifecycle, links — spec §5/§8 + §Order lifecycle."""

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.customer import Customer
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.schemas.project import validate_http_url

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


async def _completed_print(db, project_id, file_id, line_id=None, material="PETG", defective_arms=0):
    a = PrintArchive(
        project_id=project_id,
        project_line_id=line_id,
        library_file_id=file_id,
        plate_index=0,
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
    # The list carries the line count and each line's product cover — one
    # grouped lookup rather than a product query per row.
    row = next(p for p in (await committing_client.get("/api/v1/projects/")).json() if p["name"] == "A")
    assert row["lines_count"] == 1 and row["ordered"] == 2 and row["product_cover_filenames"] == [None]
    assert names(await committing_client.get("/api/v1/projects/?status=active")) == ["A"]
    assert names(await committing_client.get(f"/api/v1/projects/?customer_id={catalog['customer'].id}")) == ["A"]


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
async def test_timeline_still_reads_the_archive(committing_client, db_session, catalog):
    pid = (await committing_client.post("/api/v1/projects/", json={"name": "O"})).json()["id"]
    await _completed_print(db_session, pid, catalog["file"].id)
    events = (await committing_client.get(f"/api/v1/projects/{pid}/timeline")).json()
    assert [e["event_type"] for e in events] == ["print_completed", "project_created"]


def test_url_validator_accepts_http_only():
    assert validate_http_url(" https://a.b ") == "https://a.b"
    assert validate_http_url("") is None and validate_http_url(None) is None
    with pytest.raises(ValueError):
        validate_http_url("ftp://a.b")
    with pytest.raises(ValueError):
        validate_http_url("javascript:alert(1)")
