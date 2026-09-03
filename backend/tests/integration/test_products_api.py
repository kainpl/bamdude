"""The products API (spec §API): CRUD, from-file, duplicate, parts, plates, links.

``committing_client``, not ``async_client``: these handlers never commit —
production's ``get_db`` does it after the response. See the fixture docstrings
in ``backend/tests/conftest.py``.
"""

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import ProductPlate, product_files, product_folders
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement

pytestmark = pytest.mark.integration

MULTI = {
    "plates": [
        {
            "index": 1,
            "printable_objects": {"1": "bracket.stl", "2": "bracket.stl_2", "3": "lid.stl"},
            "print_time_seconds": 10,
            "filament_used_grams": 4.0,
            "filaments": [{"slot_id": 1, "type": "PETG", "color": "#000000"}],
        },
        {"index": 2, "printable_objects": {str(i): f"clip.stl_{i}" for i in range(1, 11)}, "print_time_seconds": 20},
    ]
}
SINGLE = {"plates": [{"index": 1, "printable_objects": {"1": "hook.stl"}, "print_time_seconds": 5}]}


@pytest.fixture
async def sliced_file(db_session):
    f = LibraryFile(filename="m.gcode.3mf", file_path="m", file_size=1, file_type="gcode", file_metadata=MULTI)
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)
    return f


async def _pivot(db, product_id: int) -> list[int]:
    """The ``product_files`` rows themselves — the link, not its plates."""
    rows = (
        await db.execute(select(product_files.c.library_file_id).where(product_files.c.product_id == product_id))
    ).scalars()
    return sorted(rows)


async def _plates(db, product_id: int) -> list[tuple[int, int]]:
    rows = (await db.execute(select(ProductPlate).where(ProductPlate.product_id == product_id))).scalars().all()
    return sorted((r.library_file_id, r.plate_index) for r in rows)


@pytest.mark.asyncio
async def test_from_file_creates_a_ready_product(committing_client, sliced_file):
    r = await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "m" and body["library_file_ids"] == [sliced_file.id]
    assert sorted(p["name_key"] for p in body["parts"]) == ["bracket.stl", "clip.stl", "lid.stl"]
    assert all(p["auto"] for p in body["parts"])
    plates = (await committing_client.get(f"/api/v1/products/{body['id']}/plates")).json()
    assert [p["plate_index"] for p in plates] == [1, 2]
    assert plates[0]["materials"] == ["PETG"] and plates[0]["sliced"] is True
    assert {y["name"]: y["count"] for y in plates[0]["yield"]} == {"bracket.stl": 2, "lid.stl": 1}


@pytest.mark.asyncio
async def test_parts_can_be_edited_merged_and_aliased(committing_client, sliced_file):
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    parts = {p["name_key"]: p for p in (await committing_client.get(f"/api/v1/products/{pid}")).json()["parts"]}

    r = await committing_client.patch(
        f"/api/v1/products/{pid}/parts/{parts['bracket.stl']['id']}", json={"qty_per_unit": 4}
    )
    assert r.json()["qty_per_unit"] == 4 and r.json()["auto"] is False

    r = await committing_client.post(
        f"/api/v1/products/{pid}/parts/{parts['bracket.stl']['id']}/merge",
        json={"source_part_id": parts["lid.stl"]["id"]},
    )
    assert set(r.json()["aliases"]) == {"bracket.stl", "lid.stl"}
    plates = (await committing_client.get(f"/api/v1/products/{pid}/plates")).json()
    assert {y["name"]: y["count"] for y in plates[0]["yield"]} == {"bracket.stl": 3}

    r = await committing_client.post(
        f"/api/v1/products/{pid}/parts/{parts['bracket.stl']['id']}/aliases", json={"name_key": "clip.stl"}
    )
    assert r.status_code == 409  # taken by another part
    r = await committing_client.post(
        f"/api/v1/products/{pid}/parts",
        json={"kind": "purchased", "name": "M3 screw", "qty_per_unit": 8, "unit_price": 0.05},
    )
    assert r.status_code == 200, r.text
    assert r.json()["name_key"] == "purchased:m3 screw"
    # ``aliases`` is NULL for a purchased part — the response must still be a list.
    assert r.json()["aliases"] == []

    # Dropping the alias again hands the objects back to nobody: they become
    # ``unassigned``, which is the only thing that tells an operator a plate
    # holds parts the product does not describe.
    r = await committing_client.delete(
        f"/api/v1/products/{pid}/parts/{parts['bracket.stl']['id']}/aliases?name_key=lid.stl"
    )
    assert r.status_code == 200 and r.json()["aliases"] == ["bracket.stl"]
    plates = (await committing_client.get(f"/api/v1/products/{pid}/plates")).json()
    assert {y["name"]: y["count"] for y in plates[0]["yield"]} == {"bracket.stl": 2}
    assert plates[0]["unassigned"] == [{"name_key": "lid.stl", "count": 1}]

    # A part cannot drop its OWN key — that would leave the row unreachable.
    r = await committing_client.delete(
        f"/api/v1/products/{pid}/parts/{parts['bracket.stl']['id']}/aliases?name_key=bracket.stl"
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_deleting_a_part_takes_its_procurement_rows_with_it(committing_client, db_session, sliced_file):
    """``project_procurement.product_part_id`` is ON DELETE CASCADE, which only
    PostgreSQL honours — the route must not rely on it."""
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    part_id = (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": "M3 screw", "qty_per_unit": 8}
        )
    ).json()["id"]
    project = Project(name="O")
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectProcurement(project_id=project.id, product_part_id=part_id, quantity_acquired=2))
    await db_session.commit()

    assert (await committing_client.delete(f"/api/v1/products/{pid}/parts/{part_id}")).status_code == 200
    db_session.expire_all()
    assert (await db_session.execute(select(ProjectProcurement))).first() is None


@pytest.mark.asyncio
async def test_delete_is_refused_while_an_order_references_it(committing_client, db_session, sliced_file):
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    project = Project(name="O")
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectLine(project_id=project.id, product_id=pid, quantity=1))
    await db_session.commit()
    assert (await committing_client.delete(f"/api/v1/products/{pid}")).status_code == 409
    await db_session.execute(ProjectLine.__table__.delete())
    await db_session.commit()
    assert (await committing_client.delete(f"/api/v1/products/{pid}")).status_code == 200
    db_session.expire_all()
    assert (await db_session.execute(select(ProductPlate))).first() is None
    assert await _pivot(db_session, pid) == []


@pytest.mark.asyncio
async def test_inactive_products_are_filtered_and_duplicate_copies_setup(committing_client, sliced_file):
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    await committing_client.patch(f"/api/v1/products/{pid}", json={"is_active": False})
    assert (await committing_client.get("/api/v1/products?active=true")).json() == []
    assert len((await committing_client.get("/api/v1/products")).json()) == 1
    r = await committing_client.post(f"/api/v1/products/{pid}/duplicate", json={"name": "m v2"})
    assert r.status_code == 200, r.text
    copy = r.json()
    assert copy["name"] == "m v2" and copy["is_active"] is True and copy["library_file_ids"] == [sliced_file.id]
    assert len(copy["parts"]) == 3
    # a copy, not a move
    assert (await committing_client.get(f"/api/v1/products/{pid}")).json()["library_file_ids"] == [sliced_file.id]

    # ``?q=`` is a substring match on the name, and it composes with ``?active``.
    assert [p["name"] for p in (await committing_client.get("/api/v1/products?q=v2")).json()] == ["m v2"]
    assert [p["name"] for p in (await committing_client.get("/api/v1/products?active=true")).json()] == ["m v2"]
    assert (await committing_client.get("/api/v1/products?q=nothing")).json() == []


@pytest.mark.asyncio
async def test_setting_and_unlinking_files_keeps_pivot_and_plates_in_step(committing_client, db_session, sliced_file):
    """The contract the routes delegate to ``sync_product_for_file`` to keep:
    the pivot and ``product_plates`` are never allowed to disagree, and a part
    outlives the link that introduced it."""
    second = LibraryFile(filename="s.gcode.3mf", file_path="s", file_size=1, file_type="gcode", file_metadata=SINGLE)
    db_session.add(second)
    await db_session.commit()
    # Plain ints: ``expire_all`` below would make reading ``second.id`` a lazy
    # load, and a lazy load inside an async session is a ``MissingGreenlet``.
    second_id = second.id

    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]

    r = await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [second_id]})
    assert r.status_code == 200, r.text
    assert r.json()["library_file_ids"] == [second_id]
    assert len(r.json()["parts"]) == 4  # hook.stl joins; bracket/clip/lid survive the unlink
    db_session.expire_all()
    assert await _pivot(db_session, pid) == [second_id]
    assert await _plates(db_session, pid) == [(second_id, 0)]  # a one-plate file is plate 0

    r = await committing_client.delete(f"/api/v1/products/{pid}/files/{second_id}")
    assert r.status_code == 200, r.text
    assert r.json()["library_file_ids"] == [] and len(r.json()["parts"]) == 4
    db_session.expire_all()
    assert await _pivot(db_session, pid) == []
    assert await _plates(db_session, pid) == []

    # A file the library does not have is a 404, and nothing is written.
    r = await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [999999]})
    assert r.status_code == 404
    db_session.expire_all()
    assert await _pivot(db_session, pid) == []


@pytest.mark.asyncio
async def test_a_second_product_on_the_same_file_survives_the_first_unlinking(
    committing_client, db_session, sliced_file
):
    """``sync_product_for_file`` is handed the file's FULL product set, so one
    product letting go must not evict the other from the pivot."""
    fid = sliced_file.id  # plain int: ``expire_all`` turns attribute reads into lazy loads
    first = (await committing_client.post(f"/api/v1/products/from-file/{fid}")).json()["id"]
    second = (await committing_client.post("/api/v1/products", json={"name": "Shared"})).json()["id"]

    r = await committing_client.put(f"/api/v1/products/{second}/files", json={"library_file_ids": [fid]})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert await _pivot(db_session, first) == [fid]
    assert await _pivot(db_session, second) == [fid]
    assert await _plates(db_session, second) == [(fid, 1), (fid, 2)]

    assert (await committing_client.delete(f"/api/v1/products/{second}/files/{fid}")).status_code == 200
    db_session.expire_all()
    assert await _pivot(db_session, first) == [fid]  # untouched
    assert await _plates(db_session, first) == [(fid, 1), (fid, 2)]
    assert await _pivot(db_session, second) == []
    assert await _plates(db_session, second) == []


@pytest.mark.asyncio
async def test_folder_links_go_through_the_folder_door(committing_client, db_session, sliced_file):
    folder = LibraryFolder(name="F")
    db_session.add(folder)
    await db_session.flush()
    child = LibraryFile(
        filename="c.gcode.3mf",
        file_path="F/c",
        file_size=1,
        file_type="gcode",
        file_metadata=SINGLE,
        folder_id=folder.id,
    )
    db_session.add(child)
    await db_session.commit()
    folder_id, child_id = folder.id, child.id  # plain ints, see the note in the test above

    pid = (await committing_client.post("/api/v1/products", json={"name": "Folder product"})).json()["id"]
    r = await committing_client.put(f"/api/v1/products/{pid}/folders", json={"library_folder_ids": [folder_id]})
    assert r.status_code == 200, r.text
    assert r.json()["library_folder_ids"] == [folder_id]
    db_session.expire_all()
    # The folder link mirrors onto the child file: pivot, plates and a seeded part.
    assert await _pivot(db_session, pid) == [child_id]
    assert await _plates(db_session, pid) == [(child_id, 0)]
    assert [p["name_key"] for p in r.json()["parts"]] == ["hook.stl"]

    assert (await committing_client.delete(f"/api/v1/products/{pid}/folders/{folder_id}")).status_code == 200
    db_session.expire_all()
    assert await _pivot(db_session, pid) == []
    assert await _plates(db_session, pid) == []
    rows = (
        await db_session.execute(select(product_folders.c.product_id).where(product_folders.c.product_id == pid))
    ).first()
    assert rows is None

    # A folder the library does not have is a 404, and nothing is written.
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/folders", json={"library_folder_ids": [999999]})
    ).status_code == 404
    db_session.expire_all()
    assert await _pivot(db_session, pid) == []


async def _folder(db, name="F"):
    folder = LibraryFolder(name=name)
    db.add(folder)
    await db.flush()
    return folder


async def _child(db, folder_id, name="c.gcode.3mf", meta=None):
    f = LibraryFile(
        filename=name, file_path=f"F/{name}", file_size=1, file_type="gcode", file_metadata=meta, folder_id=folder_id
    )
    db.add(f)
    await db.commit()
    return f


@pytest.mark.asyncio
async def test_unlinking_a_folder_leaves_a_childs_direct_product_alone(committing_client, db_session):
    """The folder axis of the co-owner rule: a file linked to X directly and to
    P through its folder keeps X — and X's plates — when P lets the folder go."""
    folder = await _folder(db_session)
    child = await _child(db_session, folder.id, meta=SINGLE)
    folder_id, child_id = folder.id, child.id

    x = (await committing_client.post("/api/v1/products", json={"name": "X"})).json()["id"]
    assert (
        await committing_client.put(f"/api/v1/products/{x}/files", json={"library_file_ids": [child_id]})
    ).status_code == 200
    p = (await committing_client.post("/api/v1/products", json={"name": "P"})).json()["id"]
    assert (
        await committing_client.put(f"/api/v1/products/{p}/folders", json={"library_folder_ids": [folder_id]})
    ).status_code == 200
    db_session.expire_all()
    assert await _pivot(db_session, x) == [child_id] and await _pivot(db_session, p) == [child_id]

    assert (await committing_client.delete(f"/api/v1/products/{p}/folders/{folder_id}")).status_code == 200
    db_session.expire_all()
    assert await _pivot(db_session, x) == [child_id]  # the direct link survives
    assert await _plates(db_session, x) == [(child_id, 0)]
    assert await _pivot(db_session, p) == [] and await _plates(db_session, p) == []


@pytest.mark.asyncio
async def test_duplicating_a_folder_linked_product_copies_folder_files_and_plates(committing_client, db_session):
    """The copy inherits the folder, and the plates come from the sync — the
    route plants none of its own."""
    folder = await _folder(db_session)
    child = await _child(db_session, folder.id, name="m.gcode.3mf", meta=MULTI)
    folder_id, child_id = folder.id, child.id

    pid = (await committing_client.post("/api/v1/products", json={"name": "P"})).json()["id"]
    await committing_client.put(f"/api/v1/products/{pid}/folders", json={"library_folder_ids": [folder_id]})

    copy = (await committing_client.post(f"/api/v1/products/{pid}/duplicate", json={})).json()
    assert copy["name"] == "P (Copy)"
    assert copy["library_folder_ids"] == [folder_id] and copy["library_file_ids"] == [child_id]
    assert copy["plates_count"] == 2 and len(copy["parts"]) == 3
    plates = (await committing_client.get(f"/api/v1/products/{copy['id']}/plates")).json()
    assert [p["plate_index"] for p in plates] == [1, 2]
    db_session.expire_all()
    assert await _plates(db_session, copy["id"]) == [(child_id, 1), (child_id, 2)]
    # a copy, not a move
    source = (await committing_client.get(f"/api/v1/products/{pid}")).json()
    assert source["library_folder_ids"] == [folder_id] and source["plates_count"] == 2


@pytest.mark.asyncio
async def test_a_purchased_part_refuses_an_alias(committing_client, sliced_file):
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    part_id = (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": "M3 screw", "qty_per_unit": 8}
        )
    ).json()["id"]
    r = await committing_client.post(f"/api/v1/products/{pid}/parts/{part_id}/aliases", json={"name_key": "washer.stl"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_an_empty_patch_does_not_clear_the_seeded_flag(committing_client, sliced_file):
    """``auto`` records that an operator has taken the row over. A PATCH that
    edits nothing has not."""
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    part_id = (await committing_client.get(f"/api/v1/products/{pid}")).json()["parts"][0]["id"]
    r = await committing_client.patch(f"/api/v1/products/{pid}/parts/{part_id}", json={})
    assert r.status_code == 200 and r.json()["auto"] is True
