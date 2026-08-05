"""Bulk "add to queue" from the file manager.

BamDude's queue is two-tier: per-printer ``print_queue`` rows under a
``PrinterQueue`` (whose id IS the printer id), and ``auto_queue_items`` above
them, which route to whichever printer becomes eligible. There is no third,
printer-less tier — ``PrintQueueItem.queue_id`` is NOT NULL and ``printer_id``
is a read-only property derived from the queue.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select


async def _sliced_file(db_session, tmp_path, name="cube.gcode.3mf"):
    from backend.app.models.library import LibraryFile

    payload = tmp_path / name
    payload.write_bytes(b"PK\x03\x04placeholder")
    row = LibraryFile(
        filename=name,
        file_path=str(payload),
        file_size=payload.stat().st_size,
        file_type="gcode",
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _printer_with_queue(db_session, name, model="X1C"):
    """A printer and its queue. ``PrinterQueue.id == printer_id`` is the
    invariant the scheduler relies on everywhere."""
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    printer = Printer(name=name, ip_address="127.0.0.1", access_code="x", serial_number=name, model=model)
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)

    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return printer, queue


async def _queued(db_session, queue_id):
    from backend.app.models.print_queue import PrintQueueItem

    rows = await db_session.execute(
        select(PrintQueueItem.library_file_id, PrintQueueItem.plate_id).where(PrintQueueItem.queue_id == queue_id)
    )
    return rows.all()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_on_each_puts_every_file_on_every_printer(async_client: AsyncClient, db_session, tmp_path):
    a = await _sliced_file(db_session, tmp_path, "a.gcode.3mf")
    b = await _sliced_file(db_session, tmp_path, "b.gcode.3mf")
    _, q1 = await _printer_with_queue(db_session, "P1")
    _, q2 = await _printer_with_queue(db_session, "P2")

    response = await async_client.post(
        "/api/v1/library/files/queue",
        json={
            "items": [{"file_id": a.id}, {"file_id": b.id}],
            "destination": {"kind": "printers", "printer_ids": [q1.id, q2.id], "mode": "each"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["added"]) == 4, body
    assert len(await _queued(db_session, q1.id)) == 2
    assert len(await _queued(db_session, q2.id)) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_spread_distributes_round_robin(async_client: AsyncClient, db_session, tmp_path):
    """Five files across three printers: 2/2/1, in the order given — not five
    copies each, which is what the other mode is for."""
    files = [await _sliced_file(db_session, tmp_path, f"f{i}.gcode.3mf") for i in range(5)]
    queues = [(await _printer_with_queue(db_session, f"P{i}"))[1] for i in range(3)]

    response = await async_client.post(
        "/api/v1/library/files/queue",
        json={
            "items": [{"file_id": f.id} for f in files],
            "destination": {"kind": "printers", "printer_ids": [q.id for q in queues], "mode": "spread"},
        },
    )
    assert response.status_code == 200, response.text

    assert len(response.json()["added"]) == 5
    counts = [len(await _queued(db_session, q.id)) for q in queues]
    assert counts == [2, 2, 1], counts


@pytest.mark.asyncio
@pytest.mark.integration
async def test_plate_ids_create_one_item_per_plate(async_client: AsyncClient, db_session, tmp_path):
    """Two ticked plates make two items carrying those ids — not one item, and
    not one per plate in the file."""
    f = await _sliced_file(db_session, tmp_path, "multi.gcode.3mf")
    _, q = await _printer_with_queue(db_session, "P1")

    response = await async_client.post(
        "/api/v1/library/files/queue",
        json={
            "items": [{"file_id": f.id, "plate_ids": [1, 3]}],
            "destination": {"kind": "printers", "printer_ids": [q.id], "mode": "each"},
        },
    )
    assert response.status_code == 200, response.text

    assert sorted(p for _, p in await _queued(db_session, q.id)) == [1, 3]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_every_combination_is_accounted_for(async_client: AsyncClient, db_session, tmp_path):
    """What makes the response trustworthy for a UI that reports "N queued":
    nothing silently disappears between the request and the report."""
    a = await _sliced_file(db_session, tmp_path, "a.gcode.3mf")
    b = await _sliced_file(db_session, tmp_path, "b.gcode.3mf")
    _, q1 = await _printer_with_queue(db_session, "P1")
    _, q2 = await _printer_with_queue(db_session, "P2")

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": a.id, "plate_ids": [1, 2]}, {"file_id": b.id}],
                "destination": {"kind": "printers", "printer_ids": [q1.id, q2.id], "mode": "each"},
            },
        )
    ).json()

    # (2 plates + 1) x 2 printers
    assert len(body["added"]) + len(body["errors"]) == 6, body


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_paused_queue_is_reported_not_skipped(async_client: AsyncClient, db_session, tmp_path):
    """The service raises for a paused queue. The bulk route must turn that into
    a visible error rather than dropping the file, or the operator believes a
    batch is queued that is not."""
    f = await _sliced_file(db_session, tmp_path, "a.gcode.3mf")
    _, q = await _printer_with_queue(db_session, "P1")
    q.is_paused = True
    await db_session.commit()

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": f.id}],
                "destination": {"kind": "printers", "printer_ids": [q.id], "mode": "each"},
            },
        )
    ).json()

    assert body["added"] == []
    assert len(body["errors"]) == 1
    assert "paused" in body["errors"][0]["error"].lower()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_non_sliced_file_is_reported_without_reaching_the_queue(
    async_client: AsyncClient, db_session, tmp_path
):
    """Selections are made in the library, where STLs sit beside sliced files."""
    from backend.app.models.library import LibraryFile

    stl = LibraryFile(filename="part.stl", file_path=str(tmp_path / "part.stl"), file_size=1, file_type="stl")
    db_session.add(stl)
    await db_session.commit()
    await db_session.refresh(stl)
    _, q = await _printer_with_queue(db_session, "P1")

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": stl.id}],
                "destination": {"kind": "printers", "printer_ids": [q.id], "mode": "each"},
            },
        )
    ).json()

    assert body["added"] == []
    assert len(body["errors"]) == 1
    assert await _queued(db_session, q.id) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_add_to_queue_reports_what_it_did(async_client: AsyncClient, db_session, tmp_path):
    """Whatever the endpoint does, it must not claim success while creating
    nothing — and must not report a failure it invented itself.

    This asserts the CURRENT contract only: every requested file is accounted
    for, in one list or the other, and every "added" id names a row that exists.
    """
    from backend.app.models.print_queue import PrintQueueItem

    f = await _sliced_file(db_session, tmp_path)

    _, q = await _printer_with_queue(db_session, "P1")
    response = await async_client.post(
        "/api/v1/library/files/queue",
        json={
            "items": [{"file_id": f.id}],
            "destination": {"kind": "printers", "printer_ids": [q.id], "mode": "each"},
        },
    )
    assert response.status_code == 200
    body = response.json()

    accounted = len(body["added"]) + len(body["errors"])
    assert accounted == 1, f"file neither added nor reported as an error: {body}"

    for entry in body["added"]:
        row = (
            await db_session.execute(select(PrintQueueItem.id).where(PrintQueueItem.id == entry["queue_item_id"]))
        ).scalar_one_or_none()
        assert row is not None, f"reported queue item {entry['queue_item_id']} does not exist"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_bulk_add_does_not_fail_on_its_own_shape(async_client: AsyncClient, db_session, tmp_path):
    """A failure whose message is about our own model rather than about the
    user's file means the endpoint cannot work at all, for anybody.

    Written as a separate assertion from the accounting above because the two
    fail for different reasons and the distinction is the whole diagnosis.
    """
    f = await _sliced_file(db_session, tmp_path)

    _, q = await _printer_with_queue(db_session, "P1")
    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": f.id}],
                "destination": {"kind": "printers", "printer_ids": [q.id], "mode": "each"},
            },
        )
    ).json()

    for err in body["errors"]:
        assert "printer_id" not in err["error"], err["error"]
        assert "queue_id" not in err["error"], err["error"]
        assert "NOT NULL" not in err["error"].upper(), err["error"]


async def _sliced_for(db_session, tmp_path, name, model):
    """A file whose 3MF says which printer model it was sliced for."""
    from backend.app.models.library import LibraryFile

    payload = tmp_path / name
    payload.write_bytes(b"PK\x03\x04placeholder")
    row = LibraryFile(
        filename=name,
        file_path=str(payload),
        file_size=payload.stat().st_size,
        file_type="gcode",
        file_metadata={"sliced_for_model": model},
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_on_each_reports_only_the_printer_that_cannot_run_it(async_client: AsyncClient, db_session, tmp_path):
    """Fanning one file onto several printers is exactly where a model mismatch
    gets created silently, because the operator picked printers, not pairs.

    Both halves asserted: a "fix" that dropped the file entirely would pass a
    test that only checked the incompatible printer.
    """
    f = await _sliced_for(db_session, tmp_path, "x1c.gcode.3mf", "X1C")
    _, ok = await _printer_with_queue(db_session, "Pok", model="X1C")
    _, bad = await _printer_with_queue(db_session, "Pbad", model="A1 mini")

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": f.id}],
                "destination": {"kind": "printers", "printer_ids": [ok.id, bad.id], "mode": "each"},
            },
        )
    ).json()

    assert [a["printer_id"] for a in body["added"]] == [ok.id], body
    assert [e["printer_id"] for e in body["errors"]] == [bad.id], body


@pytest.mark.asyncio
@pytest.mark.integration
async def test_spread_skips_a_printer_that_cannot_run_the_file(async_client: AsyncClient, db_session, tmp_path):
    """The operator asked for N prints across these machines and must get N.
    Recording an error on the first ineligible printer would report a failure
    for a request that could be satisfied."""
    f = await _sliced_for(db_session, tmp_path, "x1c.gcode.3mf", "X1C")
    _, bad = await _printer_with_queue(db_session, "Pbad", model="A1 mini")
    _, ok = await _printer_with_queue(db_session, "Pok", model="X1C")

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": f.id}],
                "destination": {"kind": "printers", "printer_ids": [bad.id, ok.id], "mode": "spread"},
            },
        )
    ).json()

    assert body["errors"] == [], body
    assert [a["printer_id"] for a in body["added"]] == [ok.id], body


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_file_no_printer_can_run_is_reported_not_dropped(async_client: AsyncClient, db_session, tmp_path):
    """The failure mode to prevent: the operator believes it is queued."""
    f = await _sliced_for(db_session, tmp_path, "x1c.gcode.3mf", "X1C")
    _, bad = await _printer_with_queue(db_session, "Pbad", model="A1 mini")

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={
                "items": [{"file_id": f.id}],
                "destination": {"kind": "printers", "printer_ids": [bad.id], "mode": "spread"},
            },
        )
    ).json()

    assert body["added"] == [], body
    assert len(body["errors"]) == 1, body
    assert await _queued(db_session, bad.id) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_auto_destination_creates_auto_rows_not_queue_rows(async_client: AsyncClient, db_session, tmp_path):
    """The router, not the executor."""
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.print_queue import PrintQueueItem

    f = await _sliced_file(db_session, tmp_path, "a.gcode.3mf")

    body = (
        await async_client.post(
            "/api/v1/library/files/queue",
            json={"items": [{"file_id": f.id}], "destination": {"kind": "auto"}},
        )
    ).json()

    assert len(body["added"]) == 1, body
    assert body["added"][0]["printer_id"] is None
    auto = (await db_session.execute(select(AutoQueueItem.id).where(AutoQueueItem.library_file_id == f.id))).all()
    queued = (await db_session.execute(select(PrintQueueItem.id).where(PrintQueueItem.library_file_id == f.id))).all()
    assert len(auto) == 1
    assert queued == []
