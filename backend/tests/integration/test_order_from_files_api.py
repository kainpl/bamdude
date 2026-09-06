"""Orders from files (spec 2026-09-06): the parts preview and the three order shapes."""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductOrigin, ProductPart, ProductPlate, product_files
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine

pytestmark = pytest.mark.integration

P1S = {
    "sliced_for_model": "P1S",
    "plates": [
        {
            "index": 1,
            "printable_objects": {"1": "flask", "2": "flask_2", "3": "cap"},
            "print_time_seconds": 3600,
            "filaments": [{"slot_id": 1, "type": "PETG"}],
        },
        {
            "index": 2,
            "printable_objects": {"1": "cap", "2": "cap_2", "3": "cap_3", "4": "cap_4"},
            "print_time_seconds": 1800,
            "filaments": [{"slot_id": 1, "type": "PETG"}],
        },
    ],
}
X1C = {
    "sliced_for_model": "X1C",
    "plates": [
        {
            "index": 1,
            "printable_objects": {"1": "flask", "2": "flask_2", "3": "flask_3", "4": "cap"},
            "print_time_seconds": 3000,
            "filaments": [{"slot_id": 1, "type": "PETG"}],
        }
    ],
}


async def _file(db, name, meta, file_type="gcode"):
    f = LibraryFile(filename=name, file_path=name, file_size=1, file_type=file_type, file_metadata=meta)
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return f


@pytest.mark.asyncio
async def test_preview_unifies_parts_across_files_and_names_where_each_comes_from(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    x1c = await _file(db_session, "job-x1c.gcode.3mf", X1C)
    r = await committing_client.post("/api/v1/library/files/parts-preview", json={"file_ids": [p1s.id, x1c.id]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [f["id"] for f in body["files"]] == [p1s.id, x1c.id]
    assert body["files"][0]["sliced_for_model"] == "P1S" and [p["plate_index"] for p in body["files"][0]["plates"]] == [
        1,
        2,
    ]
    assert body["files"][1]["plates"] == [{"plate_index": 0, "sliced": True, "print_time_seconds": 3000}]
    parts = {p["name_key"]: p for p in body["parts"]}
    assert set(parts) == {"flask", "cap"}
    assert parts["flask"]["yields"] == [
        {"library_file_id": p1s.id, "plate_index": 1, "count": 2},
        {"library_file_id": x1c.id, "plate_index": 0, "count": 3},
    ]
    assert parts["cap"]["yields"] == [
        {"library_file_id": p1s.id, "plate_index": 1, "count": 1},
        {"library_file_id": p1s.id, "plate_index": 2, "count": 4},
        {"library_file_id": x1c.id, "plate_index": 0, "count": 1},
    ]
    assert body["catalog_product"] is None


@pytest.mark.asyncio
async def test_preview_offers_the_one_catalogue_product_linking_every_file(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    x1c = await _file(db_session, "job-x1c.gcode.3mf", X1C)
    both = Product(name="Flask kit")
    partial = Product(name="Only P1S")
    db_session.add_all([both, partial])
    await db_session.flush()
    db_session.add(
        ProductPart(
            product_id=both.id, kind="printed", name="flask", name_key="flask", qty_per_unit=2, aliases=["flask"]
        )
    )
    for pid, fid in ((both.id, p1s.id), (both.id, x1c.id), (partial.id, p1s.id)):
        await db_session.execute(product_files.insert().values(product_id=pid, library_file_id=fid))
    await db_session.commit()
    body = (
        await committing_client.post("/api/v1/library/files/parts-preview", json={"file_ids": [p1s.id, x1c.id]})
    ).json()
    assert body["catalog_product"] == {
        "id": both.id,
        "name": "Flask kit",
        "parts": [{"id": body["catalog_product"]["parts"][0]["id"], "name": "flask", "qty_per_unit": 2}],
    }


@pytest.mark.asyncio
async def test_preview_refuses_a_geometry_file_and_an_unknown_id(committing_client, db_session):
    stl = await _file(db_session, "part.stl", None, file_type="stl")
    r = await committing_client.post("/api/v1/library/files/parts-preview", json={"file_ids": [stl.id]})
    assert r.status_code == 400 and r.json()["detail"] == "Only 3MF files can be planned"
    r = await committing_client.post("/api/v1/library/files/parts-preview", json={"file_ids": [999999]})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_job_order_builds_the_gcd_kit(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    x1c = await _file(db_session, "job-x1c.gcode.3mf", X1C)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={
            "kind": "job",
            "name": "Flasks for Monday",
            "file_ids": [p1s.id, x1c.id],
            "targets": {"flask": 100, "cap": 50},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Flasks for Monday" and body["status"] == "active"
    (line,) = body["lines"]
    assert line["quantity"] == 50
    product = await db_session.get(Product, line["product_id"])
    assert product.origin == "adhoc_job"
    parts = {
        p.name_key: p
        for p in (await db_session.execute(select(ProductPart).where(ProductPart.product_id == product.id))).scalars()
    }
    assert {k: p.qty_per_unit for k, p in parts.items()} == {"flask": 2, "cap": 1}
    assert not parts["flask"].auto and not parts["cap"].auto
    linked = sorted(
        (
            await db_session.execute(
                select(product_files.c.library_file_id).where(product_files.c.product_id == product.id)
            )
        ).scalars()
    )
    assert linked == sorted([p1s.id, x1c.id])
    # The catalogue does not list it; the order page's plan sees its plates.
    assert product.id not in {p["id"] for p in (await committing_client.get("/api/v1/products/")).json()}
    plan = (await committing_client.get(f"/api/v1/projects/{body['id']}/plan")).json()
    assert plan["lines"][0]["line_id"] == line["id"]


@pytest.mark.asyncio
async def test_job_order_kits_one_part_and_non_proportional_targets(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "job", "name": "Caps", "file_ids": [p1s.id], "targets": {"cap": 100}},
    )
    (line,) = r.json()["lines"]
    parts = {
        p.name_key: p.qty_per_unit
        for p in (
            await db_session.execute(select(ProductPart).where(ProductPart.product_id == line["product_id"]))
        ).scalars()
    }
    assert line["quantity"] == 100 and parts == {"flask": 0, "cap": 1}
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "job", "name": "Odd", "file_ids": [p1s.id], "targets": {"flask": 100, "cap": 30}},
    )
    (line,) = r.json()["lines"]
    parts = {
        p.name_key: p.qty_per_unit
        for p in (
            await db_session.execute(select(ProductPart).where(ProductPart.product_id == line["product_id"]))
        ).scalars()
    }
    assert line["quantity"] == 10 and parts == {"flask": 10, "cap": 3}


@pytest.mark.asyncio
async def test_job_order_refuses_empty_or_unknown_targets_and_leaves_nothing_behind(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "job", "name": "Nothing", "file_ids": [p1s.id], "targets": {"flask": 0}},
    )
    assert r.status_code == 400 and r.json()["detail"] == "No part has a target"
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "job", "name": "Typo", "file_ids": [p1s.id], "targets": {"flaskk": 3}},
    )
    assert r.status_code == 400 and r.json()["detail"] == "Unknown part key: flaskk"
    assert (await db_session.execute(select(Product))).scalars().all() == []
    assert (await db_session.execute(select(Project))).scalars().all() == []


@pytest.mark.asyncio
async def test_catalog_order_uses_the_product_as_is(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    kit = Product(name="Flask kit")
    db_session.add(kit)
    await db_session.flush()
    db_session.add(
        ProductPart(
            product_id=kit.id, kind="printed", name="flask", name_key="flask", qty_per_unit=2, aliases=["flask"]
        )
    )
    await db_session.execute(product_files.insert().values(product_id=kit.id, library_file_id=p1s.id))
    await db_session.commit()
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "catalog", "name": "Kits", "product_id": kit.id, "file_ids": [p1s.id], "quantity": 7},
    )
    assert r.status_code == 200, r.text
    (line,) = r.json()["lines"]
    assert line["quantity"] == 7 and line["product_id"] == kit.id
    assert (
        await db_session.execute(select(ProductPart.qty_per_unit).where(ProductPart.product_id == kit.id))
    ).scalar_one() == 2
    other = await _file(db_session, "other.gcode.3mf", X1C)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "catalog", "name": "Kits", "product_id": kit.id, "file_ids": [p1s.id, other.id], "quantity": 1},
    )
    assert r.status_code == 400 and r.json()["detail"] == "Every file must be linked to the product"


@pytest.mark.asyncio
async def test_plates_order_makes_a_line_per_plate_and_reuses_the_plate_product(committing_client, db_session):
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={
            "kind": "plates",
            "library_file_id": p1s.id,
            "plates": [{"plate_index": 1, "copies": 2}, {"plate_index": 2, "copies": 5}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "job-p1s · 2 plates"
    assert [(line["quantity"], line["product_name"]) for line in body["lines"]] == [
        (2, "job-p1s · plate 1"),
        (5, "job-p1s · plate 2"),
    ]
    plate2 = await db_session.get(Product, body["lines"][1]["product_id"])
    assert (plate2.origin, plate2.origin_file_id, plate2.origin_plate_index) == ("adhoc_plate", p1s.id, 2)
    parts = {
        p.name_key: p.qty_per_unit
        for p in (await db_session.execute(select(ProductPart).where(ProductPart.product_id == plate2.id))).scalars()
    }
    assert parts == {"flask": 0, "cap": 4}
    # A second batch of plate 2 reuses the SAME product and names itself ×N.
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "plates", "library_file_id": p1s.id, "plates": [{"plate_index": 2, "copies": 3}]},
    )
    assert r.json()["name"] == "job-p1s ×3" and r.json()["lines"][0]["product_id"] == plate2.id
    assert len((await db_session.execute(select(Product).where(Product.origin == "adhoc_plate"))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_plates_order_normalises_a_single_plate_file_to_plate_zero(committing_client, db_session):
    x1c = await _file(db_session, "job-x1c.gcode.3mf", X1C)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "plates", "library_file_id": x1c.id, "plates": [{"plate_index": 1, "copies": 4}]},
    )
    assert r.status_code == 200, r.text
    product = await db_session.get(Product, r.json()["lines"][0]["product_id"])
    assert product.origin_plate_index == 0 and product.name == "job-x1c"
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={
            "kind": "plates",
            "library_file_id": x1c.id,
            "plates": [{"plate_index": 1, "copies": 1}, {"plate_index": 0, "copies": 1}],
        },
    )
    assert r.status_code == 400 and r.json()["detail"] == "Duplicate plate"
    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    r = await committing_client.post(
        "/api/v1/projects/from-files",
        json={"kind": "plates", "library_file_id": p1s.id, "plates": [{"plate_index": 9, "copies": 1}]},
    )
    assert r.status_code == 404 and r.json()["detail"] == "Plate not found"


@pytest.mark.asyncio
async def test_a_plate_product_that_already_exists_is_found_not_duplicated(db_session):
    from backend.app.services.order_from_files import find_or_create_plate_product

    p1s = await _file(db_session, "job-p1s.gcode.3mf", P1S)
    existing = Product(name="old", origin=ProductOrigin.ADHOC_PLATE.value, origin_file_id=p1s.id, origin_plate_index=1)
    db_session.add(existing)
    await db_session.commit()
    found = await find_or_create_plate_product(db_session, file=p1s, plate_index=1, stem="job-p1s")
    assert found.id == existing.id
