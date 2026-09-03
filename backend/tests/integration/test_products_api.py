"""The products API (spec §API): CRUD, from-file, duplicate, parts, plates, links.

``committing_client``, not ``async_client``: these handlers never commit —
production's ``get_db`` does it after the response. See the fixture docstrings
in ``backend/tests/conftest.py``.
"""

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.product import ProductPlate, product_files, product_folders
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine, ProjectProcurement
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
