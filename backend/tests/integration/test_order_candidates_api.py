"""``GET /library/files/{id}/order-candidates`` — which order a print is for (spec pass 7).

The endpoint answers the Print and auto-queue dialogs: the active orders whose
product holds this plate, the line each would land on, and how many prints of
the plate that line still needs. It is a READ — it writes nothing and asks
nothing about any printer.

⚠️ The number it reports is the ORDER PLAN's number. The last test here pins
that against ``GET /projects/{id}/plan``: the moment the two derive it
separately they start disagreeing, and the operator is told "still needs 5" in
one place and "3" in the other about the same plate.
"""

import pytest
from httpx import AsyncClient

from backend.app.core.auth import create_access_token
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.library import LibraryFile
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine

pytestmark = pytest.mark.integration

_PW = "Str0ng-Passw0rd!"


def _sliced(filename: str, *, material: str = "PETG", objects=None) -> LibraryFile:
    """A single-plate sliced 3MF yielding one shade and two arms."""
    return LibraryFile(
        filename=filename,
        file_path=filename,
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": objects or {"1": "shade", "2": "arm", "3": "arm_2"},
                    "print_time_seconds": 100,
                    "filaments": [{"slot_id": 1, "type": material}],
                }
            ]
        },
    )


@pytest.fixture
async def lamp(db_session):
    """One product, one whole-file product plate, one sliced file.

    ``plate_index=0`` on the product plate and index 1 in the file is the
    ordinary production shape — the sync writes one 0-row per single-plate file
    while every print carries the slicer's own index — so asking for plate 1
    exercises the whole-file fall-through.
    """
    file = _sliced("lamp.gcode.3mf")
    product = Product(name="Lamp")
    db_session.add_all([file, product])
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(
                product_id=product.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1, aliases=["shade"]
            ),
            ProductPart(
                product_id=product.id, kind="printed", name="arm", name_key="arm", qty_per_unit=2, aliases=["arm"]
            ),
            ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0),
        ]
    )
    await db_session.commit()
    return {"file": file, "product": product}


async def _order(client, product_id, quantity, *, name="O", priority="normal", material="PETG", lines=None):
    body = (
        await client.post(
            "/api/v1/projects/",
            json={
                "name": name,
                "priority": priority,
                "lines": lines or [{"product_id": product_id, "quantity": quantity, "material": material}],
            },
        )
    ).json()
    return body["id"], [line["id"] for line in body["lines"]]


async def _finish_one_unit(db, project_id, file_id):
    """A completed print of the plate — one shade, two arms — filed on the order."""
    archive = PrintArchive(
        project_id=project_id,
        library_file_id=file_id,
        plate_index=1,
        filename="lamp",
        file_path="",
        file_size=0,
        status="completed",
        filament_type="PETG",
        quantity=3,
    )
    db.add(archive)
    await db.flush()
    db.add_all(
        [
            PrintArchivePart(archive_id=archive.id, name="shade", name_key="shade", quantity=1),
            PrintArchivePart(archive_id=archive.id, name="arm", name_key="arm", quantity=2),
        ]
    )
    await db.commit()


async def _candidates(client, file_id, plate_index=1):
    r = await client.get(f"/api/v1/library/files/{file_id}/order-candidates?plate_index={plate_index}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_candidates_are_ranked_needy_first_then_by_priority(committing_client, db_session, lamp):
    """Four orders, three answers.

    A needs five of this plate, D needs two but is more urgent, B is already
    satisfied and C is closed. The list is ``[D, A, B]``: needing the plate
    comes first, then priority — and the AMOUNT needed never ranks, or the big
    order would starve the small one (or the other way round, which is no
    better).
    """
    product_id, file_id = lamp["product"].id, lamp["file"].id
    a_id, (a_line,) = await _order(committing_client, product_id, 5, name="A")
    b_id, (b_line,) = await _order(committing_client, product_id, 1, name="B")
    c_id, _ = await _order(committing_client, product_id, 3, name="C")
    d_id, (d_line,) = await _order(committing_client, product_id, 2, name="D", priority="high")

    await _finish_one_unit(db_session, b_id, file_id)
    closed = await committing_client.patch(f"/api/v1/projects/{c_id}", json={"status": "completed"})
    assert closed.status_code == 200, closed.text

    rows = await _candidates(committing_client, file_id)
    assert [r["project_name"] for r in rows] == ["D", "A", "B"]
    assert [r["project_id"] for r in rows] == [d_id, a_id, b_id]
    assert [r["project_line_id"] for r in rows] == [d_line, a_line, b_line]
    assert [r["outstanding_prints"] for r in rows] == [2, 5, 0]
    # A closed order takes no more work and is not offered at all.
    assert c_id not in {r["project_id"] for r in rows}
    # Every candidate names the product the line is for, so the dialog can say
    # which of two lines of the same order it means.
    assert {r["product_id"] for r in rows} == {product_id}
    assert {r["product_name"] for r in rows} == {"Lamp"}


@pytest.mark.asyncio
async def test_a_whole_file_product_plate_answers_for_the_slicers_plate(committing_client, lamp):
    """The product holds one plate at index 0 — the whole file — and the caller
    asks about the slicer's index 1. The exact lookup misses and the whole-file
    plate claims it, exactly as attribution resolves a finished print."""
    product_id, file_id = lamp["product"].id, lamp["file"].id
    pid, (line_id,) = await _order(committing_client, product_id, 4)

    for plate_index in (0, 1):
        rows = await _candidates(committing_client, file_id, plate_index)
        assert [(r["project_id"], r["project_line_id"], r["outstanding_prints"]) for r in rows] == [(pid, line_id, 4)]

    # ⚠️ An index the FILE does not carry is a different question: the whole-file
    # product plate still claims it, but there are no filaments to read for a
    # plate that is not in the metadata, and a plate with unknown materials
    # matches no CONSTRAINED line — the same reading an archive with no
    # ``filament_type`` gets. Nothing is offered rather than the wrong thing.
    assert await _candidates(committing_client, file_id, 7) == []


@pytest.mark.asyncio
async def test_the_material_picks_the_line_and_two_alike_lines_pick_none(committing_client, db_session, lamp):
    """Decision 2, both halves. A PETG plate against a PETG line and a PLA line
    is one candidate; against two lines it cannot tell apart it is none — an
    order with nothing to file under is not offered, because the writers would
    refuse to stamp it anyway."""
    product_id, file_id = lamp["product"].id, lamp["file"].id
    narrowed, lines = await _order(
        committing_client,
        product_id,
        2,
        name="Narrowed",
        lines=[
            {"product_id": product_id, "quantity": 2, "material": "PLA"},
            {"product_id": product_id, "quantity": 2, "material": "PETG"},
        ],
    )
    rows = await _candidates(committing_client, file_id)
    assert [(r["project_id"], r["project_line_id"]) for r in rows] == [(narrowed, lines[1])]

    await _order(
        committing_client,
        product_id,
        2,
        name="Twins",
        lines=[
            {"product_id": product_id, "quantity": 2, "material": "PETG"},
            {"product_id": product_id, "quantity": 2, "material": "PETG"},
        ],
    )
    rows = await _candidates(committing_client, file_id)
    assert [(r["project_id"], r["project_line_id"]) for r in rows] == [(narrowed, lines[1])]


@pytest.mark.asyncio
async def test_a_file_no_product_holds_has_no_candidates(committing_client, db_session, lamp):
    stray = _sliced("stray.gcode.3mf")
    db_session.add(stray)
    await db_session.commit()
    await _order(committing_client, lamp["product"].id, 3)

    assert await _candidates(committing_client, stray.id) == []


@pytest.mark.asyncio
async def test_an_unknown_or_trashed_file_is_404(committing_client, db_session, lamp):
    r = await committing_client.get("/api/v1/library/files/999999/order-candidates")
    assert r.status_code == 404, r.text

    trashed = await committing_client.delete(f"/api/v1/library/files/{lamp['file'].id}")
    assert trashed.status_code in (200, 204), trashed.text
    r = await committing_client.get(f"/api/v1/library/files/{lamp['file'].id}/order-candidates")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_a_read_own_caller_only_sees_their_own_files_candidates(async_client: AsyncClient, db_session, lamp):
    """The ownership gate is the FILE's, and it answers 404 rather than 403 —
    the same shape ``/card`` gives, so an id cannot be enumerated through this
    door either. ``projects:read`` is required beside it because the answer
    names orders and how much of them is left."""
    admin = {"Authorization": f"Bearer {create_access_token(data={'sub': 'test_admin'})}"}
    grp = await async_client.post(
        "/api/v1/groups/",
        headers=admin,
        json={"name": "oc_read_own", "permissions": ["library:read_own", "projects:read"]},
    )
    assert grp.status_code == 201, grp.text
    created = await async_client.post(
        "/api/v1/users/",
        headers=admin,
        json={"username": "oc_own", "password": _PW, "role": "user", "group_ids": [grp.json()["id"]]},
    )
    assert created.status_code == 201, created.text
    login = await async_client.post("/api/v1/auth/login", json={"username": "oc_own", "password": _PW})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    uid = created.json()["id"]

    # Written straight to the session: ``async_client``'s handlers do not commit
    # (production's ``get_db`` does), and this test needs the scoped client, not
    # the committing one.
    order = Project(name="Scoped", status="active", priority="normal")
    db_session.add(order)
    await db_session.flush()
    db_session.add(ProjectLine(project_id=order.id, product_id=lamp["product"].id, quantity=3, material="PETG"))
    await db_session.commit()

    # The fixture's file is OWNERLESS, which fails closed for a scoped caller.
    r = await async_client.get(f"/api/v1/library/files/{lamp['file'].id}/order-candidates", headers=headers)
    assert r.status_code == 404, r.text

    mine = _sliced("mine.gcode.3mf")
    mine.created_by_id = uid
    db_session.add(mine)
    await db_session.flush()
    db_session.add(ProductPlate(product_id=lamp["product"].id, library_file_id=mine.id, plate_index=0))
    await db_session.commit()

    r = await async_client.get(f"/api/v1/library/files/{mine.id}/order-candidates", headers=headers)
    assert r.status_code == 200, r.text
    assert [row["outstanding_prints"] for row in r.json()] == [3]


@pytest.mark.asyncio
async def test_the_number_is_the_plan_blocks_own_number(committing_client, db_session, lamp):
    """The one guarantee this endpoint owes: the count in the picker is the
    count in the plan block, for the same plate and the same line — including
    after work has been queued against it, which is the whole point of pass 7.
    """
    product_id, file_id = lamp["product"].id, lamp["file"].id
    pid, (line_id,) = await _order(committing_client, product_id, 6)
    await _finish_one_unit(db_session, pid, file_id)

    queued = await committing_client.post(
        "/api/v1/auto-queue/", json={"library_file_id": file_id, "project_id": pid, "quantity": 2}
    )
    assert queued.status_code == 200, queued.text

    plan = (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()
    row = next(r for line in plan["lines"] if line["line_id"] == line_id for r in line["rows"])
    assert row["library_file_id"] == file_id

    rows = await _candidates(committing_client, file_id)
    assert [(r["project_line_id"], r["outstanding_prints"]) for r in rows] == [(line_id, row["count"])]
    # 6 ordered − 1 printed − 2 queued = 3, and the queued pair counts only
    # because the writer filed the line for it.
    assert row["count"] == 3
