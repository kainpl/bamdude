"""The products API (spec §API): CRUD, from-file, duplicate, parts, plates, links.

``committing_client``, not ``async_client``: these handlers never commit —
production's ``get_db`` does it after the response. See the fixture docstrings
in ``backend/tests/conftest.py``.
"""

import json
import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.part_stock import ProductPartStockMovement
from backend.app.models.product import Product, ProductPlate, product_files, product_folders
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement
from backend.app.services.part_stock import move
from backend.app.services.product_files import product_attachments_dir

# One builder for the card 3MF, shared with the library-card tests, so the
# auto-fill side and the card side cannot drift about what a card file holds.
from backend.tests.integration.test_library_card_api import (
    BOM_CSV,
    EVIL_EXE,
    PNG_A,
    make_card_file,
    write_card_3mf,
)

# The order fixture whose figures are written out by hand — the product page's
# all-time count is asserted against the same numbers the order pages show.
from backend.tests.unit.services.test_order_metrics import build_parity_fixture


def _codes(notes: list[dict]) -> list[str]:
    return [n["code"] for n in notes]


def _params(notes: list[dict], code: str) -> list[dict]:
    return [n["params"] for n in notes if n["code"] == code]


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
async def test_a_plate_with_no_estimate_reports_no_time_rather_than_zero(committing_client, db_session):
    """``print_time_seconds`` of 0 (or below) is a file with NO estimate.

    The plan engine has always read it that way (``estimate_seconds``), but this
    route emitted the raw number — so the same plate said "unknown" inside a
    plan and "0s" in the "+ plate" menu that adds it to one, and a plate added
    by hand then showed ``0s`` on a row the plan would have left blank. The two
    now share one rule; the null is what the frontend maps to ``time_unknown``.
    """
    f = LibraryFile(
        filename="untimed.gcode.3mf",
        file_path="untimed",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {"index": 1, "printable_objects": {"1": "hook.stl"}, "print_time_seconds": 0},
                # Nothing produces a negative estimate on purpose; it is here
                # because "not a positive number" is the rule, not "not zero".
                {"index": 2, "printable_objects": {"1": "hook.stl"}, "print_time_seconds": -1},
            ]
        },
    )
    db_session.add(f)
    await db_session.commit()
    await db_session.refresh(f)

    pid = (await committing_client.post(f"/api/v1/products/from-file/{f.id}")).json()["id"]
    plates = (await committing_client.get(f"/api/v1/products/{pid}/plates")).json()

    assert [p["plate_index"] for p in plates] == [1, 2]
    assert [p["print_time_seconds"] for p in plates] == [None, None]


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
async def test_deleting_a_part_takes_its_stock_movements_with_it(committing_client, db_session, sliced_file):
    """``product_part_stock_movements.product_part_id`` cascades on PostgreSQL
    only, exactly like the procurement rows beside it. Left behind, the ledger
    would hold a balance for a part nothing can name — invisible to every
    reader (they all join ``product_parts``) and impossible to correct."""
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    parts = {p["name_key"]: p for p in (await committing_client.get(f"/api/v1/products/{pid}")).json()["parts"]}
    doomed, kept = parts["bracket.stl"]["id"], parts["lid.stl"]["id"]
    await move(db_session, part_id=doomed, delta=3, reason="unfiled_print")
    await move(db_session, part_id=kept, delta=2, reason="unfiled_print")
    await db_session.commit()

    assert (await committing_client.delete(f"/api/v1/products/{pid}/parts/{doomed}")).status_code == 200
    db_session.expire_all()
    rows = (await db_session.execute(select(ProductPartStockMovement))).scalars().all()
    assert [r.product_part_id for r in rows] == [kept], "the deleted part's ledger outlived it"


@pytest.mark.asyncio
async def test_merging_a_part_moves_its_stock_onto_the_target(committing_client, db_session, sliced_file):
    """Free stock is parts on a shelf, and a merge says those parts are these
    parts — so unlike the procurement counts (deliberately dropped), the
    balance follows into the surviving part rather than evaporating."""
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    parts = {p["name_key"]: p for p in (await committing_client.get(f"/api/v1/products/{pid}")).json()["parts"]}
    target, source = parts["bracket.stl"]["id"], parts["lid.stl"]["id"]
    await move(db_session, part_id=target, delta=1, reason="unfiled_print")
    await move(db_session, part_id=source, delta=4, reason="unfiled_print")
    await db_session.commit()

    r = await committing_client.post(f"/api/v1/products/{pid}/parts/{target}/merge", json={"source_part_id": source})
    assert r.status_code == 200, r.text

    db_session.expire_all()
    rows = (await db_session.execute(select(ProductPartStockMovement))).scalars().all()
    assert {row.product_part_id for row in rows} == {target}
    assert sum(row.delta for row in rows) == 5


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


async def _attach(client, product_id: int, category: str, name: str, content: bytes) -> dict:
    r = await client.post(
        f"/api/v1/products/{product_id}/attachments",
        data={"category": category},
        files={"file": (name, content, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_duplicating_a_product_carries_the_gallery_and_its_own_copies_of_the_files(committing_client):
    """The FILES come across, not just the column.

    ``attachments`` names files inside ``products/<id>/attachments``, so a copy
    that kept the source's stored names would render correctly right up until
    somebody deleted the source — and then show a gallery of broken tiles it
    could never repair. Fresh ``uuid4`` names are what makes the two products'
    files independent; the ``original_name`` the operator gave is kept as it is.
    """
    pid = (await committing_client.post("/api/v1/products", json={"name": "Lamp"})).json()["id"]
    await _attach(committing_client, pid, "pictures", "front.png", PNG_A)
    back = await _attach(committing_client, pid, "pictures", "back.png", PNG_A + b"B")
    await _attach(committing_client, pid, "bom_docs", "bom.csv", BOM_CSV)
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/cover-image", json={"filename": back["filename"]})
    ).status_code == 200

    copy = (await committing_client.post(f"/api/v1/products/{pid}/duplicate", json={})).json()
    source = (await committing_client.get(f"/api/v1/products/{pid}")).json()

    assert [(a["category"], a["original_name"]) for a in copy["attachments"]] == [
        ("pictures", "front.png"),
        ("pictures", "back.png"),
        ("bom_docs", "bom.csv"),
    ]
    assert [a["sort_order"] for a in copy["attachments"]] == [0, 1, 0]
    stored = {a["filename"] for a in copy["attachments"]}
    assert stored.isdisjoint({a["filename"] for a in source["attachments"]})
    directory = product_attachments_dir(copy["id"])
    assert all((directory / name).is_file() for name in stored)
    assert copy["has_cover"] is True
    assert copy["cover_image_filename"] == next(
        a["filename"] for a in copy["attachments"] if a["original_name"] == "back.png"
    )
    assert (directory / copy["cover_image_filename"]).read_bytes() == PNG_A + b"B"

    # The whole point: the copy outlives the original.
    assert (await committing_client.delete(f"/api/v1/products/{pid}")).status_code == 200
    assert all((directory / name).is_file() for name in stored)


@pytest.mark.asyncio
async def test_a_duplicate_gets_its_own_dedicated_cover_file(committing_client):
    """A dedicated cover is not a gallery entry, so nothing in ``attachments``
    would have carried it — the copy would have kept a column pointing at the
    SOURCE's file, and deleting the source would have emptied both."""
    pid = (await committing_client.post("/api/v1/products", json={"name": "Bare"})).json()["id"]
    original = (
        await committing_client.put(
            f"/api/v1/products/{pid}/cover-image", files={"file": ("hero.png", PNG_A, "image/png")}
        )
    ).json()["filename"]

    copy = (await committing_client.post(f"/api/v1/products/{pid}/duplicate", json={})).json()

    assert copy["attachments"] == [] and copy["has_cover"] is True
    stored = copy["cover_image_filename"]
    assert stored.startswith("cover_") and stored != original
    assert (product_attachments_dir(copy["id"]) / stored).read_bytes() == PNG_A


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


@pytest.mark.asyncio
async def test_names_are_trimmed_before_their_length_is_measured(committing_client, sliced_file):
    """⚠️ The trim runs ``mode="before"``, so ``min_length`` / ``max_length``
    measure what will be STORED — the same fix ``schemas/customer.py`` got.

    Run after the constraints, they were decoration on one side and a lie on the
    other: ``"   "`` satisfied ``min_length=1`` and only the validator caught it,
    while a full-length name typed with a trailing space was refused for a
    length the very next statement was about to remove.
    """
    pid = (await committing_client.post("/api/v1/products/", json={"name": "  Trimmed  "})).json()["id"]
    assert (await committing_client.get(f"/api/v1/products/{pid}")).json()["name"] == "Trimmed"
    r = await committing_client.patch(f"/api/v1/products/{pid}", json={"name": "  Renamed  "})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    # A product name is bounded at 255; the padded one FITS, 256 real ones do not.
    fits = "A" * 255
    r = await committing_client.post("/api/v1/products/", json={"name": f" {fits} "})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == fits
    assert (await committing_client.post("/api/v1/products/", json={"name": "A" * 256})).status_code == 422
    assert (await committing_client.post("/api/v1/products/", json={"name": "   "})).status_code == 422

    # Parts carry the same rule at their own bound of 512.
    part_fits = "B" * 512
    r = await committing_client.post(
        f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": f" {part_fits} ", "qty_per_unit": 1}
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == part_fits
    part_id = r.json()["id"]
    r = await committing_client.patch(f"/api/v1/products/{pid}/parts/{part_id}", json={"name": "  M3 screw  "})
    assert r.status_code == 200 and r.json()["name"] == "M3 screw"
    assert (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": "B" * 513, "qty_per_unit": 1}
        )
    ).status_code == 422
    assert (
        await committing_client.patch(f"/api/v1/products/{pid}/parts/{part_id}", json={"name": "   "})
    ).status_code == 422


# ---------- the model card: auto-fill, re-read, all-time printed (spec §Decisions 2, 5, 7) ----------
#
# The two rules everything below defends:
#   * auto-fill NEVER overwrites — a field the operator wrote is theirs, and the
#     only way to get the file's value back is to clear it and re-read;
#   * only the designer's ``Auxiliaries/`` files are imported, and only into a
#     category whose allowlist accepts them. A 3MF is a ZIP an operator was
#     handed; the mesh, the sliced G-code and an .exe hidden in ``Others/`` must
#     all stay out of the attachments directory.


async def _card_product(client, db, tmp_path, **card):
    file = await make_card_file(db, tmp_path, **card)
    body = (await client.post(f"/api/v1/products/from-file/{file.id}")).json()
    return file.id, body


def _by_category(attachments: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for a in attachments:
        out.setdefault(a["category"], []).append(a)
    return out


@pytest.mark.asyncio
async def test_from_file_fills_the_card_and_imports_the_designers_files(committing_client, db_session, tmp_path):
    file_id, body = await _card_product(committing_client, db_session, tmp_path)

    assert body["name"] == "Desk Lamp", "the card's Title names the product, not the filename stem"
    assert body["designer"] == "Chef&koch" and body["license"] == "CC-BY-4.0"
    assert body["description"] == "A lamp & a shade"
    assert body["design_id"] == "1234567"
    assert body["units_printed_total"] == 0

    by_category = _by_category(body["attachments"])
    assert sorted(by_category) == ["assembly", "bom_docs", "other", "pictures"], (
        "profile pictures and thumbnails are BambuStudio's, not the designer's"
    )
    assert [a["original_name"] for a in by_category["pictures"]] == ["a.png", "b.jpg"]
    assert [a["sort_order"] for a in by_category["pictures"]] == [0, 1]
    assert [a["original_name"] for a in by_category["bom_docs"]] == ["bom.csv"]
    assert [a["original_name"] for a in by_category["other"]] == ["notes.txt"]
    for entry in body["attachments"]:
        assert entry["source"] == "3mf" and entry["source_file_id"] == file_id
        assert entry["filename"] != entry["original_name"], "stored under a uuid, like every upload"
        assert (product_attachments_dir(body["id"]) / entry["filename"]).exists()

    # The first picture becomes the effective cover for free (the cover rule).
    assert body["has_cover"] is True and body["cover_image_filename"] is None
    assert (product_attachments_dir(body["id"]) / by_category["pictures"][0]["filename"]).read_bytes() == PNG_A


@pytest.mark.asyncio
async def test_a_placeholder_title_never_becomes_the_product_name(committing_client, db_session, tmp_path):
    """BambuStudio stamps 'Exported 3D Model' on everything it exports. A farm
    of products all called that is worse than the filename it came from."""
    for title, filename, stem in (
        ("Exported 3D Model", "bracket.3mf", "bracket"),
        ("Untitled", "hinge.3mf", "hinge"),
        ("   ", "spacer.3mf", "spacer"),
        (None, "washer.3mf", "washer"),
    ):
        _, body = await _card_product(committing_client, db_session, tmp_path, name=filename, title=title)
        assert body["name"] == stem, f"{title!r} should have left the filename stem alone"
        assert body["designer"] == "Chef&koch", "the rest of the card is filled either way"


@pytest.mark.asyncio
async def test_a_second_product_from_the_same_file_gets_its_own_copies(committing_client, db_session, tmp_path):
    file = await make_card_file(db_session, tmp_path)
    first = (await committing_client.post(f"/api/v1/products/from-file/{file.id}")).json()
    second = (await committing_client.post(f"/api/v1/products/from-file/{file.id}")).json()

    assert first["id"] != second["id"]
    assert len(first["attachments"]) == len(second["attachments"]) == 5
    for entry in second["attachments"]:
        assert (product_attachments_dir(second["id"]) / entry["filename"]).exists()
    # Nothing is shared: a delete on one product cannot reach the other's files.
    assert {a["filename"] for a in first["attachments"]} & {a["filename"] for a in second["attachments"]} == set()


@pytest.mark.asyncio
async def test_an_import_skips_what_the_category_does_not_allow(committing_client, db_session, tmp_path):
    """The allowlists are the only defence against an executable landing in the
    attachments directory (spec §Risks) — and an import is an upload nobody
    watched, so it obeys them too."""
    file = await make_card_file(
        db_session,
        tmp_path,
        members={
            "Auxiliaries/Others/run.exe": EVIL_EXE,
            "Auxiliaries/Model Pictures/a.png": PNG_A,
            "Auxiliaries/Bill of Materials/sheet.png": PNG_A,
        },
    )
    body = (await committing_client.post(f"/api/v1/products/from-file/{file.id}")).json()

    assert [a["original_name"] for a in body["attachments"]] == ["a.png"]
    assert sorted(p.suffix for p in product_attachments_dir(body["id"]).iterdir()) == [".png"]


@pytest.mark.asyncio
async def test_reread_keeps_the_operators_values_and_replaces_only_this_files_imports(
    committing_client, db_session, tmp_path
):
    file = await make_card_file(db_session, tmp_path)
    file_id = file.id
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Lamp", "designer": "Me"})).json()["id"]
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [file_id]})
    ).status_code == 200, "linking a file does NOT auto-fill — the page offers a re-read"
    linked = (await committing_client.get(f"/api/v1/products/{pid}")).json()
    assert linked["license"] is None and linked["attachments"] == []

    r = await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": file_id})
    assert r.status_code == 200, r.text
    first = r.json()["product"]
    notes = r.json()["notes"]
    # Codes and params, never prose: the operator reads these in their own
    # language and only the frontend knows which that is.
    assert {n["field"] for n in _params(notes, "filled_field")} == {"license", "description", "design_id"}
    assert "name" not in {n["field"] for n in _params(notes, "filled_field")}
    assert sorted(n["category"] for n in _params(notes, "imported_files")) == [
        "assembly",
        "bom_docs",
        "other",
        "pictures",
    ]
    assert sum(n["count"] for n in _params(notes, "imported_files")) == 5
    assert first["name"] == "Lamp" and first["designer"] == "Me", "a value the operator wrote is never overwritten"
    assert first["license"] == "CC-BY-4.0" and first["description"] == "A lamp & a shade"
    imported = [a["filename"] for a in first["attachments"]]
    assert len(imported) == 5

    manual = (
        await committing_client.post(
            f"/api/v1/products/{pid}/attachments",
            data={"category": "pictures"},
            files={"file": ("mine.png", PNG_A, "image/png")},
        )
    ).json()["filename"]

    # A SECOND linked file's import is not this file's to replace either, so it
    # is here to be counted after the second read.
    other_file = await make_card_file(db_session, tmp_path, name="second.3mf", title="Second")
    other_id = other_file.id
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [file_id, other_id]})
    ).status_code == 200
    from_other = [
        a["filename"]
        for a in (
            await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": other_id})
        ).json()["product"]["attachments"]
        if a["source_file_id"] == other_id
    ]
    assert len(from_other) == 5

    again = await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": file_id})
    assert again.status_code == 200, again.text
    assert _params(again.json()["notes"], "replaced_files") == [{"count": 5}]
    second = again.json()["product"]
    names = {a["filename"] for a in second["attachments"]}
    assert manual in names, "a manual attachment is not the file's to replace"
    assert set(from_other) <= names, "another file's import is not this file's to replace"
    assert not (names & set(imported)), "the previous import of THIS file is gone"
    assert len([a for a in second["attachments"] if a["source"] == "3mf"]) == 10
    for gone in imported:
        assert not (product_attachments_dir(pid) / gone).exists()
    for kept in [manual, *from_other]:
        assert (product_attachments_dir(pid) / kept).exists()


@pytest.mark.asyncio
async def test_from_file_logs_the_notes_it_has_nowhere_to_return(committing_client, db_session, tmp_path, caplog):
    """``from-file`` answers a bare ``ProductResponse`` — there is no ``notes``
    field on it, and adding one would change the wire for every caller. So the
    notes go to the log: what got filled, and what got skipped and why, is the
    only record an operator has when a designer's file arrives half-empty."""
    file = await make_card_file(
        db_session,
        tmp_path,
        members={"Auxiliaries/Model Pictures/a.png": PNG_A, "Auxiliaries/Others/run.exe": EVIL_EXE},
    )
    with caplog.at_level(logging.INFO, logger="backend.app.api.routes.products"):
        r = await committing_client.post(f"/api/v1/products/from-file/{file.id}")
    assert r.status_code == 200, r.text
    # The response is untouched — this is a log line, not a wire change. (The
    # `notes` key it does carry is the product's own free-text column, not the
    # card's; a plain `ProductResponse` is exactly what the detail GET answers.)
    body = r.json()
    detail = (await committing_client.get(f"/api/v1/products/{body['id']}")).json()
    assert set(body) == set(detail) and body["notes"] is None
    assert [a["original_name"] for a in body["attachments"]] == ["a.png"]

    lines = [rec.getMessage() for rec in caplog.records if rec.name == "backend.app.api.routes.products"]
    assert any("code=filled_field" in line and "'field': 'license'" in line for line in lines), lines
    assert any("code=imported_files" in line and "'category': 'pictures'" in line for line in lines), lines
    assert any("code=skipped_extension" in line and "run.exe" in line for line in lines), lines


@pytest.mark.asyncio
async def test_a_reread_that_fills_nothing_leaves_the_attachments_column_alone(committing_client, db_session, tmp_path):
    """A no-op re-read used to rewrite the column anyway — ``rows`` is a
    REBUILT list, so entries the loader skips (a legacy row with no filename,
    from a hand-edited column or a restored backup) were silently pruned by an
    operation that reported nothing. Nothing filled and nothing replaced means
    nothing written, byte for byte."""
    file = await make_card_file(db_session, tmp_path, name="bare.3mf", members={})
    file_id = file.id
    pid = (
        await committing_client.post(
            "/api/v1/products/",
            json={"name": "Lamp", "designer": "Me", "license": "mine", "description": "d", "design_id": "1"},
        )
    ).json()["id"]
    assert (
        await committing_client.put(f"/api/v1/products/{pid}/files", json={"library_file_ids": [file_id]})
    ).status_code == 200

    # A row the loader would drop, planted the way a restored backup would.
    legacy = [{"category": "pictures", "original_name": "old.png"}, {"category": "other", "filename": "keep.txt"}]
    product = await db_session.get(Product, pid)
    product.attachments = legacy
    await db_session.commit()
    before = json.dumps((await db_session.get(Product, pid)).attachments)

    r = await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": file_id})
    assert r.status_code == 200, r.text
    assert _codes(r.json()["notes"]) == ["nothing_to_fill"]

    db_session.expire_all()
    assert json.dumps((await db_session.get(Product, pid)).attachments) == before


@pytest.mark.asyncio
async def test_reread_wants_a_file_the_product_is_actually_linked_to(committing_client, db_session, tmp_path):
    linked = await make_card_file(db_session, tmp_path, name="linked.3mf")
    stranger = await make_card_file(db_session, tmp_path, name="stranger.3mf")
    linked_id, stranger_id = linked.id, stranger.id

    pid = (await committing_client.post(f"/api/v1/products/from-file/{linked_id}")).json()["id"]
    assert (
        await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": stranger_id})
    ).status_code == 404
    assert (
        await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": 999999})
    ).status_code == 404
    assert (
        await committing_client.post("/api/v1/products/999999/card/reread", params={"file_id": linked_id})
    ).status_code == 404

    # A file the product holds THROUGH A FOLDER is linked just as much.
    folder = LibraryFolder(name="Lamps")
    db_session.add(folder)
    await db_session.flush()
    written = write_card_3mf(tmp_path / "library" / "child.3mf", title="Child Lamp", designer="Someone")
    child = LibraryFile(
        filename="child.3mf",
        file_path="library/child.3mf",
        file_size=written.stat().st_size,
        file_type="3mf",
        folder_id=folder.id,
        file_metadata=SINGLE,
    )
    db_session.add(child)
    await db_session.commit()
    folder_id, child_id = folder.id, child.id

    other = (await committing_client.post("/api/v1/products/", json={"name": "Via folder"})).json()["id"]
    assert (
        await committing_client.put(f"/api/v1/products/{other}/folders", json={"library_folder_ids": [folder_id]})
    ).status_code == 200
    r = await committing_client.post(f"/api/v1/products/{other}/card/reread", params={"file_id": child_id})
    assert r.status_code == 200, r.text
    assert r.json()["product"]["designer"] == "Someone"


@pytest.mark.asyncio
async def test_units_printed_total_counts_every_order_the_product_appears_in(committing_client, db_session):
    """The all-time count on the product page (spec §Decisions 7): Σ over the
    lines of EVERY order, and never over another product's lines."""
    file = LibraryFile(
        filename="lamp.gcode.3mf",
        file_path="lamp",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade"},
                    "print_time_seconds": 10,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    db_session.add(file)
    await db_session.commit()
    file_id = file.id

    pid = (await committing_client.post(f"/api/v1/products/from-file/{file_id}")).json()["id"]
    other_pid = (await committing_client.post("/api/v1/products/", json={"name": "Not a lamp"})).json()["id"]
    assert (await committing_client.get(f"/api/v1/products/{pid}")).json()["units_printed_total"] == 0

    async def _order(product_id: int, quantity: int, printed: int) -> None:
        body = (
            await committing_client.post(
                "/api/v1/projects/",
                json={
                    "name": f"O{product_id}-{quantity}",
                    "lines": [{"product_id": product_id, "quantity": quantity}],
                },
            )
        ).json()
        archive = PrintArchive(
            project_id=body["id"],
            project_line_id=body["lines"][0]["id"],
            library_file_id=file_id,
            plate_index=1,
            filename="lamp",
            file_path="",
            file_size=0,
            status="completed",
            filament_type="PETG",
            quantity=printed,
        )
        db_session.add(archive)
        await db_session.flush()
        db_session.add(PrintArchivePart(archive_id=archive.id, name="shade", name_key="shade", quantity=printed))
        await db_session.commit()

    await _order(pid, 2, 2)
    await _order(pid, 5, 3)
    await _order(other_pid, 4, 4)  # another product's order contributes nothing

    assert (await committing_client.get(f"/api/v1/products/{pid}")).json()["units_printed_total"] == 5
    assert (await committing_client.get(f"/api/v1/products/{other_pid}")).json()["units_printed_total"] == 0


@pytest.mark.asyncio
async def test_the_same_name_in_two_folders_is_two_files(committing_client, db_session, tmp_path):
    """The re-import guard is keyed on (category, name), not the name alone.

    One 3MF legitimately ships ``guide.png`` as a model picture AND as a step of
    the assembly guide; they are different images and both belong on the product.
    Keyed on the name alone the second one silently vanished.
    """
    file = await make_card_file(
        db_session,
        tmp_path,
        name="twins.3mf",
        members={
            "Auxiliaries/Model Pictures/guide.png": PNG_A,
            "Auxiliaries/Assembly Guide/guide.png": PNG_A + b"different",
        },
    )
    body = (await committing_client.post(f"/api/v1/products/from-file/{file.id}")).json()

    pairs = sorted((a["category"], a["original_name"]) for a in body["attachments"])
    assert pairs == [("assembly", "guide.png"), ("pictures", "guide.png")]
    stored = {a["category"]: a["filename"] for a in body["attachments"]}
    assert (product_attachments_dir(body["id"]) / stored["pictures"]).read_bytes() == PNG_A
    assert (product_attachments_dir(body["id"]) / stored["assembly"]).read_bytes() == PNG_A + b"different"


@pytest.mark.asyncio
async def test_an_oversized_member_is_skipped_with_a_note_never_read(
    committing_client, db_session, tmp_path, monkeypatch
):
    """An import buffers each member whole, so the ZIP's DECLARED uncompressed
    size is checked before a byte is inflated — a 2 GB member inside a 3MF must
    not become a 2 GB allocation on a Raspberry Pi."""
    monkeypatch.setattr("backend.app.services.product_files.MAX_ATTACHMENT_BYTES", len(PNG_A))

    file = await make_card_file(
        db_session,
        tmp_path,
        name="heavy.3mf",
        members={
            "Auxiliaries/Model Pictures/small.png": PNG_A,
            "Auxiliaries/Bill of Materials/huge.csv": BOM_CSV * 20,
        },
    )
    pid = (await committing_client.post(f"/api/v1/products/from-file/{file.id}")).json()["id"]
    notes = (await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": file.id})).json()[
        "notes"
    ]

    skipped = _params(notes, "skipped_too_large")
    assert [n["name"] for n in skipped] == ["huge.csv"]
    assert skipped[0]["size"] == len(BOM_CSV) * 20 and skipped[0]["limit"] == len(PNG_A)
    body = (await committing_client.get(f"/api/v1/products/{pid}")).json()
    assert [a["original_name"] for a in body["attachments"]] == ["small.png"]


@pytest.mark.asyncio
async def test_an_upload_past_the_cap_is_413(committing_client, monkeypatch):
    """The same ceiling on the manual route, so the two ways a file reaches the
    attachments directory cannot disagree about what fits."""
    monkeypatch.setattr("backend.app.services.product_files.MAX_ATTACHMENT_BYTES", 8)
    pid = (await committing_client.post("/api/v1/products/", json={"name": "Heavy"})).json()["id"]

    r = await committing_client.post(
        f"/api/v1/products/{pid}/attachments",
        data={"category": "pictures"},
        files={"file": ("big.png", PNG_A + b"way past the cap", "image/png")},
    )
    assert r.status_code == 413, r.text
    assert (await committing_client.get(f"/api/v1/products/{pid}/attachments")).json() == []


@pytest.mark.asyncio
async def test_a_file_with_nothing_left_to_give_says_so(committing_client, db_session, tmp_path):
    """``nothing_to_fill`` is a code like any other — an empty list would leave
    the dialog with nothing to say and look like a failure."""
    file = await make_card_file(db_session, tmp_path, name="twice.3mf")
    pid = (await committing_client.post(f"/api/v1/products/from-file/{file.id}")).json()["id"]

    # Everything is already filled and every attachment is re-imported, so the
    # second read reports the replacement and the imports, not "nothing".
    notes = (await committing_client.post(f"/api/v1/products/{pid}/card/reread", params={"file_id": file.id})).json()[
        "notes"
    ]
    assert "nothing_to_fill" not in _codes(notes)

    empty = await make_card_file(
        db_session,
        tmp_path,
        name="empty.3mf",
        title=None,
        description=None,
        designer=None,
        license=None,
        design_model_id=None,
        members={},
    )
    other = (await committing_client.post("/api/v1/products/", json={"name": "Named already"})).json()["id"]
    await committing_client.put(f"/api/v1/products/{other}/files", json={"library_file_ids": [empty.id]})
    nothing = (
        await committing_client.post(f"/api/v1/products/{other}/card/reread", params={"file_id": empty.id})
    ).json()["notes"]
    assert _codes(nothing) == ["nothing_to_fill"]


@pytest.mark.asyncio
async def test_plates_count_ignores_the_plates_of_a_trashed_file(committing_client, db_session):
    """``plates_count`` is what the card promises can be printed, so it has to
    count what ``GET /plates`` lists - and that one drops a trashed file's
    plates. A trashed file keeps its ``product_plates`` rows (it is restorable),
    which is exactly why the count has to ask rather than count the links."""
    doomed = LibraryFile(
        filename="doomed.gcode.3mf", file_path="doomed", file_size=1, file_type="gcode", file_metadata=MULTI
    )
    kept = LibraryFile(
        filename="kept.gcode.3mf", file_path="kept", file_size=1, file_type="gcode", file_metadata=SINGLE
    )
    db_session.add_all([doomed, kept])
    await db_session.commit()

    pid = (await committing_client.post("/api/v1/products/", json={"name": "Two files"})).json()["id"]
    db_session.add_all(
        [
            ProductPlate(product_id=pid, library_file_id=doomed.id, plate_index=1),
            ProductPlate(product_id=pid, library_file_id=doomed.id, plate_index=2),
            ProductPlate(product_id=pid, library_file_id=kept.id, plate_index=1),
        ]
    )
    await db_session.commit()

    async def counts() -> tuple[int, int, int]:
        detail = (await committing_client.get(f"/api/v1/products/{pid}")).json()["plates_count"]
        listed = next(p for p in (await committing_client.get("/api/v1/products/")).json() if p["id"] == pid)
        plates = (await committing_client.get(f"/api/v1/products/{pid}/plates")).json()
        return detail, listed["plates_count"], len(plates)

    assert await counts() == (3, 3, 3)

    doomed.deleted_at = datetime.now(timezone.utc)
    await db_session.commit()

    assert await counts() == (1, 1, 1)

    rows = (await db_session.execute(select(ProductPlate).where(ProductPlate.product_id == pid))).scalars().all()
    assert len(rows) == 3, "the links themselves survive the trash - only the count stops believing them"


@pytest.mark.asyncio
async def test_renaming_a_purchased_part_refreshes_its_key(committing_client, db_session, sliced_file):
    """A purchased part IS its name: ``name_key`` is derived from it, and a
    rename that leaves the key behind makes the two disagree for good. The
    procurement rows reference the part id, so nothing of the order moves."""
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    screw = (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": "M3 screw", "qty_per_unit": 8}
        )
    ).json()
    nut = (
        await committing_client.post(
            f"/api/v1/products/{pid}/parts", json={"kind": "purchased", "name": "M4 screw", "qty_per_unit": 1}
        )
    ).json()
    assert (screw["name_key"], nut["name_key"]) == ("purchased:m3 screw", "purchased:m4 screw")

    order = (
        await committing_client.post(
            "/api/v1/projects/", json={"name": "Screws", "lines": [{"product_id": pid, "quantity": 2}]}
        )
    ).json()
    r = await committing_client.patch(
        f"/api/v1/projects/{order['id']}/procurement/{screw['id']}", json={"quantity_acquired": 9}
    )
    assert r.status_code == 200, r.text

    # A rename onto a key another part already holds is a 409, not an IntegrityError.
    r = await committing_client.patch(f"/api/v1/products/{pid}/parts/{screw['id']}", json={"name": "M4 SCREW"})
    assert r.status_code == 409, r.text
    kept = next(
        p for p in (await committing_client.get(f"/api/v1/products/{pid}")).json()["parts"] if p["id"] == screw["id"]
    )
    assert (kept["name"], kept["name_key"]) == ("M3 screw", "purchased:m3 screw"), "the refused rename changed nothing"

    r = await committing_client.patch(f"/api/v1/products/{pid}/parts/{screw['id']}", json={"name": "  M5  screw  "})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["name_key"]) == ("M5  screw", "purchased:m5 screw")

    procurement = (await committing_client.get(f"/api/v1/projects/{order['id']}")).json()["procurement"]
    entry = next(p for p in procurement if p["part_id"] == screw["id"])
    assert (entry["name"], entry["acquired"], entry["need"]) == ("M5  screw", 9, 16)

    rows = (
        (await db_session.execute(select(ProjectProcurement).where(ProjectProcurement.product_part_id == screw["id"])))
        .scalars()
        .all()
    )
    assert [row.quantity_acquired for row in rows] == [9], "the row follows the id, so there is nothing to move"


@pytest.mark.asyncio
async def test_a_printed_parts_key_survives_a_rename(committing_client, sliced_file):
    """The mirror image: a printed part's key is the 3MF object name, not its
    display name. Refreshing it on a rename would orphan every archive row."""
    pid = (await committing_client.post(f"/api/v1/products/from-file/{sliced_file.id}")).json()["id"]
    part = next(
        p
        for p in (await committing_client.get(f"/api/v1/products/{pid}")).json()["parts"]
        if p["name_key"] == "bracket.stl"
    )
    r = await committing_client.patch(f"/api/v1/products/{pid}/parts/{part['id']}", json={"name": "Bracket Mk II"})
    assert r.status_code == 200, r.text
    assert (r.json()["name"], r.json()["name_key"]) == ("Bracket Mk II", "bracket.stl")


@pytest.mark.asyncio
async def test_units_printed_total_matches_the_order_pages_on_a_shared_plate(committing_client, db_session):
    """The product page's all-time count now rides the same batched loader the
    orders list does. Hand-written in ``build_parity_fixture``: the lamp is
    printed 3 times across a live and a cancelled order, the hook once."""
    ids = await build_parity_fixture(db_session)

    assert (await committing_client.get(f"/api/v1/products/{ids['lamp']}")).json()["units_printed_total"] == 3
    assert (await committing_client.get(f"/api/v1/products/{ids['hook_product']}")).json()["units_printed_total"] == 1
