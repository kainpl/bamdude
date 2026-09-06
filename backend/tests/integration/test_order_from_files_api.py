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
