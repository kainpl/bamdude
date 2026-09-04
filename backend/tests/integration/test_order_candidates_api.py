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

from contextlib import contextmanager
from datetime import datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import event, update
from sqlalchemy.engine import Engine

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


async def _order(
    client, product_id, quantity, *, name="O", priority="normal", material="PETG", lines=None, due_date=None
):
    payload = {
        "name": name,
        "priority": priority,
        "lines": lines or [{"product_id": product_id, "quantity": quantity, "material": material}],
    }
    if due_date is not None:
        payload["due_date"] = due_date
    body = (await client.post("/api/v1/projects/", json=payload)).json()
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


@contextmanager
def _statement_spy(fragment: str):
    """Every statement issued while the block runs whose SQL names ``fragment``.

    The technique ``test_order_metrics.py`` uses for the same question: listen on
    the ``Engine`` class rather than on one engine, because the session under
    test is built by a fixture and its bind is not this test's business.
    """
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        if fragment in statement:
            seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


async def _candidates(client, file_id, plate_index=1):
    r = await client.get(f"/api/v1/library/files/{file_id}/order-candidates?plate_index={plate_index}")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_candidates_are_ranked_by_every_key_in_turn(committing_client, db_session, lamp):
    """Six orders, and each ranking key decides exactly one neighbouring pair.

    ``[D, E, F, A, B]``, with C absent:

    * **D before E** — priority. D needs two, E needs four, and the AMOUNT never
      ranks, or the big order would starve the small one (or the other way
      round, which is no better).
    * **E before F** — deadline. Both are ``normal`` and both need the plate;
      only E carries a due date, and no deadline sorts LAST rather than as
      "infinitely far away and therefore urgent".
    * **F before A** — ``created_at``, ascending. Neither has a deadline and
      nothing above it separates them; F was backdated, so the older order goes
      first.
    * **A before B** — needing the plate. B is satisfied and is still OFFERED,
      because printing ahead is legitimate; it just ranks below everything that
      needs something.
    * **C nowhere** — a completed order takes no more work.
    """
    product_id, file_id = lamp["product"].id, lamp["file"].id
    a_id, (a_line,) = await _order(committing_client, product_id, 5, name="A")
    b_id, (b_line,) = await _order(committing_client, product_id, 1, name="B")
    c_id, _ = await _order(committing_client, product_id, 3, name="C")
    d_id, (d_line,) = await _order(committing_client, product_id, 2, name="D", priority="high")
    e_id, (e_line,) = await _order(committing_client, product_id, 4, name="E", due_date="2026-10-01T00:00:00")
    f_id, (f_line,) = await _order(committing_client, product_id, 4, name="F")

    # F is made the OLDER of the two deadline-less orders by hand: SQLite's
    # ``CURRENT_TIMESTAMP`` has second resolution, so six orders created in one
    # test share a ``created_at`` and the tiebreak below it (the id) would decide
    # instead — with A winning, which is the opposite of what this pins.
    await db_session.execute(update(Project).where(Project.id == f_id).values(created_at=datetime(2020, 1, 1, 0, 0, 0)))
    await _finish_one_unit(db_session, b_id, file_id)
    closed = await committing_client.patch(f"/api/v1/projects/{c_id}", json={"status": "completed"})
    assert closed.status_code == 200, closed.text

    rows = await _candidates(committing_client, file_id)
    assert [r["project_name"] for r in rows] == ["D", "E", "F", "A", "B"]
    assert [r["project_id"] for r in rows] == [d_id, e_id, f_id, a_id, b_id]
    assert [r["project_line_id"] for r in rows] == [d_line, e_line, f_line, a_line, b_line]
    assert [r["outstanding_prints"] for r in rows] == [2, 4, 4, 5, 0]
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
async def test_the_material_rules_a_line_out_and_every_survivor_is_its_own_candidate(
    committing_client, db_session, lamp
):
    """Decision 2 as amended by pass 7's final review, both halves.

    A PETG plate against a PLA line and a PETG line is ONE candidate — the PLA
    line is ruled out and is not offered at all. Against an order whose other
    two lines both accept it is TWO candidates of the same order, not none: the
    WRITERS refuse to guess between them (a stamp made with nobody watching), but
    the operator standing in front of the dialog may answer, and hiding the
    question from them is not the same as refusing to guess it. What tells the
    two apart on screen rides on the wire — ``line_material``, the line's own.
    """
    product_id, file_id = lamp["product"].id, lamp["file"].id
    narrowed, lines = await _order(
        committing_client,
        product_id,
        2,
        name="Narrowed",
        lines=[
            {"product_id": product_id, "quantity": 2, "material": "PLA"},
            {"product_id": product_id, "quantity": 2, "material": "PETG"},
            # No material at all: a line that takes every plate, so it accepts
            # this one beside the PETG line and cannot be told apart from it by
            # the plate. On the wire it is ``null``, which is what the dialog
            # renders as no suffix rather than the word "none".
            {"product_id": product_id, "quantity": 2},
        ],
    )
    rows = await _candidates(committing_client, file_id)
    assert [(r["project_id"], r["project_line_id"]) for r in rows] == [
        (narrowed, lines[1]),
        (narrowed, lines[2]),
    ], "both accepting lines, in the order the operator sees them; the PLA line is not offered"
    assert [r["line_material"] for r in rows] == ["PETG", None]

    # Two lines of the SAME material are equally indistinguishable, and equally
    # offered — the wire cannot disambiguate them, and the operator picking
    # either one is still an answer the writers will honour.
    twins, twin_lines = await _order(
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
    assert [(r["project_id"], r["project_line_id"]) for r in rows if r["project_id"] == twins] == [
        (twins, twin_lines[0]),
        (twins, twin_lines[1]),
    ]


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


@pytest.mark.asyncio
async def test_the_number_holds_for_an_alternative_plate_the_block_did_not_pick(committing_client, db_session, lamp):
    """The same part sliced twice — one file per printer model, identical yield.

    The greedy picks ONE of them and hangs every print on it; the other is that
    row's ``alternative`` and appears in the block only as a file the operator
    may switch to. Asked about the alternative's own plate, this endpoint still
    answers — with the number the block shows for the row it replaces, because
    covering the same work with an interchangeable plate takes the same number
    of prints. A picker that answered anything else would tell the operator
    "still needs 4" about a file the block calls 3.
    """
    product_id, picked_file_id = lamp["product"].id, lamp["file"].id
    twin = _sliced("lamp-p1s.gcode.3mf")
    db_session.add(twin)
    await db_session.flush()
    db_session.add(ProductPlate(product_id=product_id, library_file_id=twin.id, plate_index=0))
    await db_session.commit()
    twin_id = twin.id

    pid, (line_id,) = await _order(committing_client, product_id, 5)

    plan = (await committing_client.get(f"/api/v1/projects/{pid}/plan")).json()
    (row,) = [r for line in plan["lines"] if line["line_id"] == line_id for r in line["rows"]]
    assert row["library_file_id"] == picked_file_id, "the fixture's plate is the lower id, so the greedy takes it"
    assert [alt["library_file_id"] for alt in row["alternatives"]] == [twin_id]

    for file_id in (picked_file_id, twin_id):
        rows = await _candidates(committing_client, file_id)
        assert [(r["project_line_id"], r["outstanding_prints"]) for r in rows] == [(line_id, row["count"])]


@pytest.mark.asyncio
async def test_three_candidate_orders_are_planned_in_one_batch(committing_client, db_session, lamp):
    """⚠️ One plan per candidate ORDER was one full context load per order.

    The endpoint asks the plan engine for its numbers — deliberately, so the
    picker and the block can never disagree — and it asks about every open order
    holding this product. Run one at a time that was the whole archive-and-parts
    read of each order, per keystroke-driven refresh of a dialog. The batch is
    ``plan_for_orders``, and the spy below counts the read that used to repeat:
    exactly one, whatever the number of orders.

    The second half is the point of doing it at all — the batched numbers must be
    the ones the per-order path computes, or the saving bought a different answer.
    """
    product_id, file_id = lamp["product"].id, lamp["file"].id
    ids = [await _order(committing_client, product_id, n, name=f"O{n}") for n in (2, 3, 4)]

    with _statement_spy("FROM print_archives") as statements:
        rows = await _candidates(committing_client, file_id)

    assert len(statements) == 1, f"one batched archive read, got {len(statements)}: {statements}"
    assert len(rows) == 3

    from backend.app.services.plan_engine import plan_for_order

    one_at_a_time = {}
    for pid, (line_id,) in ids:
        plan = await plan_for_order(db_session, pid)
        (row,) = [r for line in plan.lines if line.line_id == line_id for r in line.rows]
        one_at_a_time[line_id] = row.count
    assert {r["project_line_id"]: r["outstanding_prints"] for r in rows} == one_at_a_time
