"""Virtual queue read ownership and bounded auxiliary query stages."""

from types import SimpleNamespace

import pytest
from sqlalchemy import event

from backend.app.core.auth import create_access_token
from backend.app.core.permissions import Permission
from backend.app.models.group import Group
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.services import queue_virtual
from backend.app.services.print_run_binding import bind_print_run, discard_print_run

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_read_own_cannot_see_foreign_or_ownerless_virtual(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app import main

    group = Group(name="queue-own-only", permissions=[Permission.QUEUE_READ_OWN.value])
    user = User(username="queue-own-reader", password_hash="x", role="user", is_active=True)
    user.groups.append(group)
    db_session.add(user)
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    foreign = await archive_factory(printer.id, status="printing", created_by_id=None)
    await db_session.commit()

    bind_print_run(queue_virtual.printer_manager, printer_id=printer.id, archive_id=foreign.id)
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda _id: SimpleNamespace(connected=True, state="RUNNING"),
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _id: None)
    headers = {"Authorization": f"Bearer {create_access_token({'sub': user.username})}"}
    try:
        response = await async_client.get("/api/v1/queue/", headers=headers)
        assert response.status_code == 200
        assert response.json() == []

        foreign.created_by_id = user.id
        await db_session.commit()
        response = await async_client.get("/api/v1/queue/", headers=headers)
        assert response.status_code == 200
        assert [row["archive_id"] for row in response.json()] == [foreign.id]
    finally:
        discard_print_run(queue_virtual.printer_manager, printer.id, foreign.id)
        main._active_prints.pop((printer.id, foreign.filename), None)


@pytest.mark.asyncio
async def test_virtual_auxiliary_selects_are_batch_bounded(db_session, printer_factory, archive_factory, monkeypatch):
    printer_ids = []
    for _ in range(10):
        printer = await printer_factory()
        printer_ids.append(printer.id)
        db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    archive = await archive_factory(printer_ids[0], status="printing")
    await db_session.commit()
    bind_print_run(queue_virtual.printer_manager, printer_id=printer_ids[0], archive_id=archive.id)
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda pid: SimpleNamespace(connected=True, state="RUNNING") if pid in printer_ids else None,
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _id: None)
    selects = []

    def count_select(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", count_select)
    try:
        rows = await queue_virtual.build_virtual_current_prints(db_session)
        assert [row["archive_id"] for row in rows] == [archive.id]
        # Queue map + real printing facts + candidate archives; no per-printer SQL.
        assert len(selects) == 3, selects
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", count_select)
        discard_print_run(queue_virtual.printer_manager, printer_ids[0], archive.id)


@pytest.mark.asyncio
async def test_run_switch_during_archive_read_drops_stale_virtual(
    db_session, printer_factory, archive_factory, monkeypatch
):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    first = await archive_factory(printer.id, status="printing", print_name="Repeat")
    second = await archive_factory(printer.id, status="printing", print_name="Repeat")
    await db_session.commit()
    bind_print_run(queue_virtual.printer_manager, printer_id=printer.id, archive_id=first.id)
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda _id: SimpleNamespace(connected=True, state="RUNNING"),
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _id: None)
    original_execute = db_session.execute
    calls = 0

    async def switch_after_archive_read(*args, **kwargs):
        nonlocal calls
        rows = await original_execute(*args, **kwargs)
        calls += 1
        if calls == 3:
            bind_print_run(queue_virtual.printer_manager, printer_id=printer.id, archive_id=second.id)
        return rows

    monkeypatch.setattr(db_session, "execute", switch_after_archive_read)
    try:
        assert await queue_virtual.build_virtual_current_prints(db_session) == []
        assert [row["archive_id"] for row in await queue_virtual.build_virtual_current_prints(db_session)] == [
            second.id
        ]
    finally:
        discard_print_run(queue_virtual.printer_manager, printer.id, second.id)


@pytest.mark.asyncio
async def test_queue_list_reads_only_printer_identity(async_client, db_session, printer_factory):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    db_session.add(PrintQueueItem(queue_id=printer.id, status="pending", position=0))
    await db_session.commit()
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.lower())

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        response = await async_client.get("/api/v1/queue/?status=pending")
        assert response.status_code == 200, response.text
        assert response.json()[0]["printer_name"] == printer.name
        bad = [query for query in statements if "printer_tag_links" in query or "printer_locations" in query]
        assert not bad, bad
        printer_selects = [query for query in statements if "from printers" in query]
        assert printer_selects
        assert all("access_code" not in query for query in printer_selects)
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_count", [1, 46, 100, 401])
async def test_virtual_read_chunks_over_sqlite_bind_boundary(
    db_session, printer_factory, archive_factory, monkeypatch, queue_count
):
    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    db_session.add_all(PrinterQueue(id=1000 + i, printer_id=1000 + i) for i in range(queue_count - 1))
    archive = await archive_factory(printer.id, status="printing")
    await db_session.commit()
    bind_print_run(queue_virtual.printer_manager, printer_id=printer.id, archive_id=archive.id)
    monkeypatch.setattr(
        queue_virtual.printer_manager,
        "get_status",
        lambda _id: SimpleNamespace(connected=True, state="RUNNING"),
    )
    monkeypatch.setattr(queue_virtual.printer_manager, "get_printer", lambda _id: None)
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        result = await queue_virtual.build_virtual_current_prints(db_session)
        assert [row["archive_id"] for row in result] == [archive.id]
        # 1 queue map + one real-row stage per 400 IDs + 1 archive batch.
        assert len(statements) == 2 + (queue_count + 399) // 400, statements
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)
        discard_print_run(queue_virtual.printer_manager, printer.id, archive.id)
