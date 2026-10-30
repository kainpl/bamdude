"""One captured job changes queue tiers; no printer or order publication is called."""

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.services import queue_sources, swap_compatibility as sc
from backend.app.services.filament_policy import serialize_policy
from backend.app.services.filament_routing import RoutingPolicy
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_order_resources import resource_shop, shop  # noqa: F401
from backend.tests.integration.test_swap_library_ingest import clean_spool_state, sessions  # noqa: F401
from backend.tests.integration.test_workshop_library_rights import _key

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def committing_client(async_client, sessions, monkeypatch):
    """Share the authenticated client; do not seed a second test_admin."""
    from backend.app.core.database import get_db
    from backend.app.main import app

    async def committing_db():
        async with sessions() as db:
            yield db
            await db.commit()

    monkeypatch.setitem(app.dependency_overrides, get_db, committing_db)
    return async_client


@pytest.fixture
async def test_engine(tmp_path, monkeypatch, request):
    """Real independent connections: an in-memory StaticPool cannot prove a race."""
    from backend.app.core import database
    from backend.app.core.config import settings

    database.import_all_models()
    pg = request.module.__name__.endswith("_postgres")
    url = os.environ.get("TEST_POSTGRES_URL") if pg else f"sqlite+aiosqlite:///{(tmp_path / 'transfer.db').as_posix()}"
    if pg and not url:
        pytest.skip("disposable TEST_POSTGRES_URL required")
    schema = "transfer_" + uuid.uuid4().hex
    if pg:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema + ",public"}})
    else:
        engine = create_async_engine(url)
    if pg:
        event.listen(engine.sync_engine, "before_cursor_execute", database._strip_tz_from_params, retval=True)
        from collections import defaultdict

        from backend.app.services import queue_ops

        class NoProcessLock:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            def locked(self):
                return False

        # PostgreSQL must prove its advisory guards, not be rescued by a lock
        # in this test process. Every competing writer still has its own session.
        monkeypatch.setattr(queue_ops, "_queue_claim_locks", defaultdict(NoProcessLock))
    else:
        event.listen(engine.sync_engine, "connect", database.configure_sqlite_connection)
    old_url = settings.database_url
    object.__setattr__(settings, "database_url", url)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        object.__setattr__(settings, "database_url", old_url)
        if pg:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


@pytest.fixture
async def job(async_client, db_session, sessions, tmp_path, printer_factory, request):  # noqa: F811
    printer = await printer_factory(model="P1P")
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id, is_paused=True, status="error"))
    await db_session.commit()
    path = write_routing_3mf(
        tmp_path / "test.gcode.3mf",
        {1: [{"id": 1, "type": "PLA", "used_g": "10"}]},
        prediction=getattr(request, "param", 3600),
    )
    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(path=path, format="3mf", display_filename=path.name)
    )
    owner = await db_session.scalar(select(User).where(User.username == "test_admin"))
    made = []

    async def attach(db, source):
        item = PrintQueueItem(
            queue_id=printer.id,
            queue_source_id=source.id,
            source_snapshot=queue_sources.snapshot_for(receipt, source),
            plate_id=1,
            created_by_id=owner.id,
            filament_routing=serialize_policy(RoutingPolicy(), plate_id=1),
        )
        db.add(item)
        await db.flush()
        made.append(item.id)

    await queue_sources.publish(receipt, attach, session_factory=sessions)
    return made[0], printer.id


async def proposal(client, item_id):
    response = await client.get(f"/api/v1/queue/{item_id}/auto-transfer-preview")
    assert response.status_code == 200, response.text
    return response.json()


async def move(client, item_id, pre, **extra):
    return await client.post(
        f"/api/v1/queue/{item_id}/move-to-auto",
        json={
            "token": pre["token"],
            "accept_profile_changes": True,
            "allow_automatic_start": True,
            "allow_automatic_filament": True,
            **extra,
        },
    )


@pytest.fixture
def lane_waiter(test_engine, monkeypatch):
    """Observe the second writer at the real guard, before releasing the first."""
    from contextvars import ContextVar

    from backend.app.services import queue_ops

    competing_request = ContextVar("competing_lane_request", default=False)

    class Waiter:
        competitor = None
        pid = None

        def __init__(self):
            self.attempted = asyncio.Event()
            self.acquired = asyncio.Event()

        async def run(self, operation):
            # Starlette may execute an endpoint in a child task. Context travels
            # with that request; asyncio.current_task identity does not.
            token = competing_request.set(True)
            try:
                return await operation
            finally:
                competing_request.reset(token)

        async def prove_waiting(self):
            await asyncio.wait_for(self.attempted.wait(), 5)
            if test_engine.dialect.name == "postgresql":

                async def observed():
                    async with test_engine.connect() as monitor:
                        while True:
                            # This is the server's wait, not merely a task that
                            # has not yet run or a process-local substitute.
                            waiting = await monitor.scalar(
                                text(
                                    "SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                                    "WHERE l.pid=:pid AND l.locktype='advisory' AND NOT l.granted "
                                    "AND a.wait_event_type='Lock' AND a.wait_event='advisory'"
                                ),
                                {"pid": self.pid},
                            )
                            if waiting:
                                print("Observed PostgreSQL advisory lock wait for competitor", self.pid)
                                return
                            await asyncio.sleep(0.01)

                await asyncio.wait_for(observed(), 5)
            assert not self.acquired.is_set(), "competitor passed the held lane"
            assert not self.competitor.done(), "competitor returned before the held lane was released"

    waiter = Waiter()
    if test_engine.dialect.name == "postgresql":
        original = queue_ops._lane_db_guard

        async def db_guard(db, queue_id):
            competing = competing_request.get()
            if competing:
                waiter.pid = await db.scalar(text("SELECT pg_backend_pid()"))
                waiter.attempted.set()
            await original(db, queue_id)
            if competing:
                waiter.acquired.set()

        monkeypatch.setattr(queue_ops, "_lane_db_guard", db_guard)
    else:
        from collections import defaultdict

        original = queue_ops._queue_claim_locks

        class ObservedLock:
            def __init__(self, lock):
                self.lock = lock

            async def __aenter__(self):
                competing = competing_request.get()
                if competing:
                    assert self.lock.locked(), "first writer no longer owns the lane"
                    waiter.attempted.set()
                result = await self.lock.__aenter__()
                if competing:
                    waiter.acquired.set()
                return result

            async def __aexit__(self, *args):
                return await self.lock.__aexit__(*args)

            def locked(self):
                return self.lock.locked()

        class ObservedLocks(defaultdict):
            def __missing__(self, key):
                value = self[key] = ObservedLock(original[key])
                return value

        monkeypatch.setattr(queue_ops, "_queue_claim_locks", ObservedLocks())
    return waiter


async def finish_lane_race(first, second, release, waiter):
    waiter.competitor = second
    try:
        await waiter.prove_waiting()
    finally:
        # A failing proof must still release the writer and close its sessions.
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
    return results


async def test_direct_preserves_blob_and_paused_error_queue(async_client, db_session, job):
    item_id, queue_id = job
    original = await db_session.get(PrintQueueItem, item_id)
    blob = original.queue_source_id
    pre = await proposal(async_client, item_id)
    assert pre["allowed"] and pre["target_model"] == "P1P"
    response = await move(async_client, item_id, pre)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await db_session.get(PrintQueueItem, item_id) is None
    auto = await db_session.get(AutoQueueItem, response.json()["resulting_auto_item_id"])
    assert auto.queue_source_id == blob and auto.plate_id == 1 and auto.print_time_seconds == 3600
    assert auto.required_filament_types == '["PLA"]'
    queue = await db_session.get(PrinterQueue, queue_id)
    assert queue.is_paused and queue.status == "error" and queue.pending_count == 0
    assert (await move(async_client, item_id, pre)).status_code == 404


async def test_rearm_preserves_ancestor_plate_identity_and_history(async_client, db_session, job):
    item_id, _ = job
    item = await db_session.get(PrintQueueItem, item_id)
    ancestor = AutoQueueItem(
        status="assigned",
        assigned_to_item_id=item.id,
        plate_id=None,
        queue_source_id=item.queue_source_id,
        source_snapshot=item.source_snapshot,
        created_by_id=item.created_by_id,
        target_model="P1P",
        been_jumped=True,
        rebalanced_at=datetime.now(timezone.utc),
        rebalanced_from_model="P1S",
    )
    db_session.add(ancestor)
    await db_session.flush()
    item.source_auto_item_id = ancestor.id
    await db_session.commit()
    identity, ancestor_id = ancestor.swap_declaration_identity, ancestor.id
    pre = await proposal(async_client, item_id)
    assert pre["allowed"], pre
    response = await move(async_client, item_id, pre)
    assert response.status_code == 200, response.text
    await db_session.refresh(ancestor)
    assert response.json()["resulting_auto_item_id"] == ancestor_id
    assert ancestor.plate_id is None and ancestor.swap_declaration_identity == identity and ancestor.been_jumped
    assert (
        ancestor.status == "pending"
        and ancestor.assigned_to_item_id is None
        and ancestor.rebalanced_from_model == "P1S"
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("dispatch_attempts", 1, "transfer_started"),
        ("manual_start", True, None),
        ("origin", "direct", "transfer_started"),
        ("is_calibration", True, "transfer_started"),
        ("queue_source_id", None, "transfer_source_unavailable"),
        ("filament_routing", "{}", "transfer_policy_invalid"),
        ("nozzle_rack_choice", "{}", "transfer_rack_choice"),
    ],
)
async def test_refusals_and_manual_opt_in(async_client, db_session, job, field, value, reason):
    item_id, _ = job
    item = await db_session.get(PrintQueueItem, item_id)
    setattr(item, field, value)
    await db_session.commit()
    pre = await proposal(async_client, item_id)
    if reason:
        assert pre == {"allowed": False, "refusal": reason}
    else:
        response = await move(async_client, item_id, pre, allow_automatic_start=False)
        assert response.status_code == 409 and response.json()["detail"]["code"] == "transfer_manual_consent"
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0


async def test_changed_row_and_restore_fence_preview(async_client, db_session, job):
    item_id, _ = job
    pre = await proposal(async_client, item_id)
    item = await db_session.get(PrintQueueItem, item_id)
    item.position += 1
    await db_session.commit()
    assert (await move(async_client, item_id, pre)).status_code == 409
    pre = await proposal(async_client, item_id)
    queue_sources.invalidate_receipts()
    assert (await move(async_client, item_id, pre)).status_code == 409


async def test_rearm_rejects_newer_swap_fact(async_client, db_session, job):
    item_id, _ = job
    item = await db_session.get(PrintQueueItem, item_id)
    ancestor = AutoQueueItem(
        status="assigned",
        assigned_to_item_id=item.id,
        plate_id=1,
        queue_source_id=item.queue_source_id,
        source_snapshot=sc.snapshot_with_fact(item.source_snapshot, sc.SourceFact(True)),
        created_by_id=item.created_by_id,
    )
    db_session.add(ancestor)
    await db_session.flush()
    item.source_auto_item_id = ancestor.id
    await db_session.commit()
    assert (await proposal(async_client, item_id))["refusal"] == "swap_declaration_changed"


async def test_restricted_key_on_allowed_printer_is_farm_wide_refusal(async_client, db_session, job):
    item_id, printer_id = job
    owner = await db_session.scalar(select(User).where(User.username == "test_admin"))
    raw = await _key(db_session, owner, can_read_status=True, can_queue=True, printer_ids=[printer_id])
    headers = {"Authorization": f"Bearer {raw}"}
    for method, suffix, kwargs in [
        ("get", "auto-transfer-preview", {}),
        ("post", "move-to-auto", {"json": {"token": "x"}}),
    ]:
        response = await getattr(async_client, method)(f"/api/v1/queue/{item_id}/{suffix}", headers=headers, **kwargs)
        assert response.status_code == 403, response.text


async def test_two_applies_create_one_auto_row(async_client, db_session, job):
    item_id, _ = job
    pre = await proposal(async_client, item_id)
    results = await asyncio.gather(move(async_client, item_id, pre), move(async_client, item_id, pre))
    assert sorted(r.status_code for r in results) in ([200, 404], [200, 409])
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 1


@pytest.mark.parametrize("job", [None], indirect=True)
async def test_captured_plate_without_prediction_does_not_inherit_original(async_client, db_session, job):
    from backend.app.models.library import LibraryFile

    item_id, _ = job
    original = LibraryFile(
        filename="different.3mf",
        file_path="unused",
        file_type="gcode",
        file_size=1,
        file_metadata={"print_time_seconds": 99999},
    )
    db_session.add(original)
    await db_session.flush()
    item = await db_session.get(PrintQueueItem, item_id)
    item.library_file_id = original.id
    await db_session.commit()
    response = await async_client.get(f"/api/v1/queue/{item_id}")
    assert response.status_code == 200, response.text
    assert response.json()["print_time_seconds"] is None
    pre = await proposal(async_client, item_id)
    response = await move(async_client, item_id, pre)
    assert response.status_code == 200, response.text
    auto = await db_session.get(AutoQueueItem, response.json()["resulting_auto_item_id"])
    assert auto.print_time_seconds is None


@pytest.mark.parametrize("cancel_first", [True, False])
@pytest.mark.parametrize("action", ["cancel", "delete", "auto-cancel"])
async def test_cancel_delete_and_transfer_share_lane(
    async_client, db_session, job, monkeypatch, sessions, lane_waiter, action, cancel_first
):
    """Pause while holding the actual lane, then let the competing writer queue."""
    from contextlib import asynccontextmanager

    from backend.app.services import queue_auto_transfer, queue_cancel_scope

    item_id, queue_id = job
    auto_id = None
    if action == "auto-cancel":
        item = await db_session.get(PrintQueueItem, item_id)
        ancestor = AutoQueueItem(
            status="assigned",
            assigned_to_item_id=item.id,
            plate_id=1,
            queue_source_id=item.queue_source_id,
            source_snapshot=item.source_snapshot,
            created_by_id=item.created_by_id,
        )
        db_session.add(ancestor)
        await db_session.flush()
        item.source_auto_item_id = ancestor.id
        await db_session.commit()
        auto_id = ancestor.id
    pre = await proposal(async_client, item_id)
    held, release = asyncio.Event(), asyncio.Event()
    module = queue_cancel_scope if cancel_first else queue_auto_transfer
    original_scope = module.claim_lanes

    @asynccontextmanager
    async def barrier(*args, **kwargs):
        async with original_scope(*args, **kwargs):
            held.set()
            await release.wait()
            yield

    monkeypatch.setattr(module, "claim_lanes", barrier)

    async def cancel():
        if action == "auto-cancel":
            return await async_client.delete(f"/api/v1/auto-queue/{auto_id}")
        if action == "delete":
            return await async_client.delete(f"/api/v1/queue/{item_id}")
        return await async_client.post(f"/api/v1/queue/{item_id}/cancel")

    first = asyncio.create_task(cancel() if cancel_first else move(async_client, item_id, pre))
    await asyncio.wait_for(held.wait(), 5)
    second = asyncio.create_task(lane_waiter.run(move(async_client, item_id, pre) if cancel_first else cancel()))
    responses = await finish_lane_race(first, second, release, lane_waiter)
    assert not any(isinstance(response, BaseException) for response in responses), responses
    assert all(response.status_code in (200, 404, 409) for response in responses), [r.text for r in responses]
    async with sessions() as fresh:
        rows = (await fresh.scalars(select(AutoQueueItem))).all()
        assert len(rows) <= 1
        for row in rows:
            if row.assigned_to_item_id is not None:
                assert await fresh.get(PrintQueueItem, row.assigned_to_item_id) is not None
        if cancel_first:
            assert responses[1].status_code in (404, 409)


async def test_failure_before_commit_rolls_back_both_tiers(async_client, db_session, job, monkeypatch):
    from backend.app.services import queue_auto_transfer

    item_id, _ = job
    pre = await proposal(async_client, item_id)

    async def fail(*args):
        raise RuntimeError("injected before commit")

    monkeypatch.setattr(queue_auto_transfer, "update_queue_counters", fail)
    with pytest.raises(Exception, match="injected before commit"):
        await move(async_client, item_id, pre)
    assert await db_session.get(PrintQueueItem, item_id) is not None
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0


async def test_existing_order_job_needs_no_new_filing_right(async_client, db_session, job):
    from backend.app.models.product import Product
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine
    from backend.tests.integration.test_workshop_library_rights import _jwt, _user

    owner = await _user(
        db_session, "transfer_own", ["queue:create", "queue:read", "queue:read_own", "queue:delete_own"]
    )
    project, product = Project(name="Synthetic order"), Product(name="Synthetic product")
    db_session.add_all([project, product])
    await db_session.flush()
    line = ProjectLine(project_id=project.id, product_id=product.id, quantity=7)
    db_session.add(line)
    await db_session.flush()
    item_id, _ = job
    item = await db_session.get(PrintQueueItem, item_id)
    item.project_id, item.project_line_id, item.created_by_id = project.id, line.id, owner.id
    await db_session.commit()
    headers = _jwt(owner.username)
    response = await async_client.get(f"/api/v1/queue/{item_id}/auto-transfer-preview", headers=headers)
    assert response.status_code == 200 and response.json()["allowed"], response.text
    response = await async_client.post(
        f"/api/v1/queue/{item_id}/move-to-auto",
        headers=headers,
        json={"token": response.json()["token"], "accept_profile_changes": True},
    )
    assert response.status_code == 200, response.text
    auto = await db_session.get(AutoQueueItem, response.json()["resulting_auto_item_id"])
    assert (auto.project_id, auto.project_line_id, auto.created_by_id) == (project.id, line.id, owner.id)
    await db_session.refresh(line)
    assert line.quantity == 7


@pytest.mark.parametrize("state", ["completed", "cancelled", "foreign", "deleted"])
async def test_invalid_order_link_is_a_readable_conflict(async_client, db_session, job, resource_shop, state):
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine

    item_id, _ = job
    item = await db_session.get(PrintQueueItem, item_id)
    item.project_id, item.project_line_id = resource_shop["order"], resource_shop["line"]
    await db_session.commit()
    pre = await proposal(async_client, item_id)
    if state in ("completed", "cancelled"):
        project = await db_session.get(Project, item.project_id)
        project.status = state
    elif state == "foreign":
        other = Project(name="Other order")
        db_session.add(other)
        await db_session.flush()
        item.project_id = other.id
    else:
        await db_session.delete(await db_session.get(ProjectLine, item.project_line_id))
    await db_session.commit()
    # Real deletion nulls the FK: the old proposal must still conflict. The
    # foreign/closed preview must refuse without leaking an exception as 500.
    if state != "deleted":
        response = await async_client.get(f"/api/v1/queue/{item_id}/auto-transfer-preview")
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["message"]
    response = await move(async_client, item_id, pre)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["message"]
    assert await db_session.get(PrintQueueItem, item_id) is not None
    assert await db_session.scalar(select(func.count()).select_from(AutoQueueItem)) == 0


async def test_waiting_metadata_does_not_expire_transfer(async_client, db_session, job, monkeypatch, printer_factory):
    from unittest.mock import AsyncMock

    from backend.app.core.websocket import ws_manager

    item_id, _ = job
    printer = await printer_factory(model="P1P")
    printer_id = printer.id
    queue = PrinterQueue(id=109, printer_id=printer_id)
    db_session.add(queue)
    await db_session.flush()
    item = await db_session.get(PrintQueueItem, item_id)
    item.queue_id = queue.id
    await db_session.commit()
    pre = await proposal(async_client, item_id)
    item.waiting_reason = "Waiting for another print"
    item.waiting_reason_code = "printer_busy"
    item.waiting_reason_checked_at = datetime.now(timezone.utc)
    await db_session.commit()
    send = AsyncMock()
    monkeypatch.setattr(ws_manager, "send_queue_changed", send)
    response = await move(async_client, item_id, pre)
    assert response.status_code == 200, response.text
    send.assert_awaited_once_with(printer_id)


async def test_dangling_order_line_preview_is_conflict(async_client, test_engine, job, resource_shop):
    if test_engine.dialect.name != "sqlite":
        pytest.skip("legacy dangling FK fixture; real deletion is covered on both databases")
    item_id, _ = job
    # Model the inconsistent legacy row from a database created without FK
    # enforcement. Normal deletion (SET NULL) is covered separately above.
    async with test_engine.connect() as conn:
        await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            await conn.execute(
                text("UPDATE print_queue SET project_id=:order_id, project_line_id=987654 WHERE id=:id"),
                {"order_id": resource_shop["order"], "id": item_id},
            )
            await conn.commit()
        finally:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    response = await async_client.get(f"/api/v1/queue/{item_id}/auto-transfer-preview")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "transfer_order_invalid"
    assert response.json()["detail"]["message"]


@pytest.mark.parametrize("kind", ["library", "archive"])
@pytest.mark.parametrize("purge_first", [False, True])
async def test_original_purge_and_transfer_do_not_deadlock(
    async_client, db_session, sessions, job, monkeypatch, test_engine, kind, purge_first
):
    from contextvars import ContextVar

    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile
    from backend.app.services import queue_auto_transfer, queue_source_release

    item_id, _ = job
    original = (
        LibraryFile(filename="nav.3mf", file_path="unused", file_size=1, file_type="gcode")
        if kind == "library"
        else PrintArchive(filename="nav.3mf", file_path="unused", file_size=1, status="completed")
    )
    db_session.add(original)
    await db_session.flush()
    original_id = original.id
    item = await db_session.get(PrintQueueItem, item_id)
    setattr(item, "library_file_id" if kind == "library" else "archive_id", original_id)
    await db_session.commit()
    pre = await proposal(async_client, item_id)
    held, release, attempted, completed = (asyncio.Event() for _ in range(4))
    competing = ContextVar("purge_competitor", default=False)
    pids = set()
    holder_pid = None
    pg = test_engine.dialect.name == "postgresql"

    def blocked_statement(statement):
        sql = statement.upper()
        return sql.startswith("BEGIN IMMEDIATE") if purge_first else sql.startswith("UPDATE PRINT_QUEUE ")

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if competing.get():
            if pg:
                pids.add(conn.connection.driver_connection.get_server_pid())
                attempted.set()
            elif blocked_statement(statement):
                attempted.set()

    def after_sql(conn, cursor, statement, parameters, context, executemany):
        if competing.get() and not pg and blocked_statement(statement):
            completed.set()

    async def hold(db):
        nonlocal holder_pid
        if pg:
            conn = await db.connection()
            holder_pid = conn.sync_connection.connection.driver_connection.get_server_pid()
        held.set()
        await release.wait()

    real_facts = queue_auto_transfer._facts

    async def facts(*args, **kwargs):
        result = await real_facts(*args, **kwargs)
        # Hold the actual job row, not merely a lane that purge doesn't use.
        if kwargs.get("lock") and not purge_first:
            await hold(args[0])
        return result

    monkeypatch.setattr(queue_auto_transfer, "_facts", facts)

    async def purge():
        async with sessions() as db:
            if kind == "archive":
                row = await db.scalar(select(PrintArchive).where(PrintArchive.id == original_id).with_for_update())
            await queue_source_release.source_purged(
                db,
                reason="synthetic purge",
                **{"library_file_ids" if kind == "library" else "archive_ids": [original_id]},
            )
            if kind == "library":
                row = await db.get(LibraryFile, original_id)
            await db.delete(row)
            if purge_first:
                await hold(db)
            await db.commit()

    async def competitor(operation):
        token = competing.set(True)
        try:
            return await operation
        finally:
            competing.reset(token)

    async def prove_wait():
        await asyncio.wait_for(attempted.wait(), 5)
        if pg:

            async def observe():
                async with test_engine.connect() as monitor:
                    while True:
                        row = (
                            await monitor.execute(
                                text(
                                    "SELECT pid, wait_event FROM pg_stat_activity WHERE pid=ANY(:pids) "
                                    "AND wait_event_type='Lock' AND :holder=ANY(pg_blocking_pids(pid))"
                                ),
                                {"pids": list(pids), "holder": holder_pid},
                            )
                        ).first()
                        if row:
                            print("Observed PostgreSQL purge row wait", kind, purge_first, row.pid, row.wait_event)
                            return
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(observe(), 5)
        else:
            # The first transaction owns SQLite's writer. The competing UPDATE
            # or BEGIN IMMEDIATE has been submitted but cannot complete yet.
            assert not completed.is_set()
        assert not second.done()

    event.listen(test_engine.sync_engine, "before_cursor_execute", before_sql)
    event.listen(test_engine.sync_engine, "after_cursor_execute", after_sql)
    first = asyncio.create_task(purge() if purge_first else move(async_client, item_id, pre))
    try:
        await asyncio.wait_for(held.wait(), 5)
        second = asyncio.create_task(competitor(move(async_client, item_id, pre) if purge_first else purge()))
        try:
            await prove_wait()
        finally:
            release.set()
            results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 10)
    finally:
        release.set()
        event.remove(test_engine.sync_engine, "before_cursor_execute", before_sql)
        event.remove(test_engine.sync_engine, "after_cursor_execute", after_sql)
    assert not any(isinstance(result, BaseException) for result in results), results
    response = results[1] if purge_first else results[0]
    assert response.status_code in (200, 409), response.text
    async with sessions() as fresh:
        source_item = await fresh.get(PrintQueueItem, item_id)
        autos = list((await fresh.scalars(select(AutoQueueItem))).all())
        assert (source_item is not None) + len(autos) == 1
        for remaining in [source_item, *autos]:
            if remaining is not None:
                assert getattr(remaining, "library_file_id" if kind == "library" else "archive_id") is None


@pytest.mark.parametrize("dispatch_first", [True, False])
async def test_real_dispatch_claim_and_transfer(
    async_client, db_session, sessions, job, monkeypatch, lane_waiter, dispatch_first
):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from backend.app.services import print_scheduler, queue_auto_transfer
    from backend.app.services.ha_sensor_manager import ha_sensor_manager

    item_id, queue_id = job
    queue = await db_session.get(PrinterQueue, queue_id)
    queue.status, queue.is_paused = "idle", False
    await db_session.commit()
    pre = await proposal(async_client, item_id)
    monkeypatch.setattr(print_scheduler, "async_session", sessions)
    monkeypatch.setattr(print_scheduler.farm_startup_guard, "allowed", lambda: True)
    monkeypatch.setattr(ha_sensor_manager, "blocked_printers", AsyncMock(return_value=set()))
    held, release = asyncio.Event(), asyncio.Event()
    module, attribute = (
        (print_scheduler, "queue_claim_scope") if dispatch_first else (queue_auto_transfer, "claim_lanes")
    )
    original = getattr(module, attribute)

    @asynccontextmanager
    async def barrier(*args, **kwargs):
        async with original(*args, **kwargs):
            held.set()
            await release.wait()
            yield

    monkeypatch.setattr(module, attribute, barrier)

    async def claim():
        return await print_scheduler.PrintScheduler()._claim_for_dispatch(
            item_id=item_id, queue_id=queue_id, printer_id=queue_id, plan_fields={}
        )

    first = asyncio.create_task(claim() if dispatch_first else move(async_client, item_id, pre))
    await asyncio.wait_for(held.wait(), 5)
    second = asyncio.create_task(lane_waiter.run(move(async_client, item_id, pre) if dispatch_first else claim()))
    results = await finish_lane_race(first, second, release, lane_waiter)
    assert not any(isinstance(r, BaseException) for r in results), results
    dispatch, response = results if dispatch_first else results[::-1]
    assert dispatch.claimed is dispatch_first
    assert response.status_code == (409 if dispatch_first else 200), response.text


@pytest.mark.parametrize("resources_first", [True, False])
async def test_real_order_resource_apply_and_transfer(
    async_client, db_session, sessions, job, resource_shop, monkeypatch, lane_waiter, resources_first
):
    from contextlib import asynccontextmanager

    from backend.app.core.database import get_db
    from backend.app.main import app
    from backend.app.services import queue_auto_transfer, queue_ops
    from backend.tests.integration.test_order_resources import apply_body, choose, review

    # This HTTP writer commits through its dependency in production.
    async def committing_db():
        async with sessions() as db:
            yield db
            await db.commit()

    monkeypatch.setitem(app.dependency_overrides, get_db, committing_db)
    item_id, _ = job
    item = await db_session.get(PrintQueueItem, item_id)
    item.project_id, item.project_line_id = resource_shop["order"], resource_shop["line"]
    item.library_file_id = resource_shop["file"].id
    await db_session.commit()
    await choose(async_client, resource_shop, models=["A1 mini"])
    pre = await proposal(async_client, item_id)
    path, plan = await review(async_client, resource_shop)
    assert item_id in plan["remove_printer_ids"]
    held, release = asyncio.Event(), asyncio.Event()
    module = queue_ops if resources_first else queue_auto_transfer
    original = module.claim_lanes

    @asynccontextmanager
    async def barrier(*args, **kwargs):
        async with original(*args, **kwargs):
            held.set()
            await release.wait()
            yield

    monkeypatch.setattr(module, "claim_lanes", barrier)

    async def apply_resources():
        return await async_client.post(path, json=apply_body(plan))

    first = asyncio.create_task(apply_resources() if resources_first else move(async_client, item_id, pre))
    await asyncio.wait_for(held.wait(), 5)
    second = asyncio.create_task(
        lane_waiter.run(move(async_client, item_id, pre) if resources_first else apply_resources())
    )
    results = await finish_lane_race(first, second, release, lane_waiter)
    assert not any(isinstance(r, BaseException) for r in results), results
    assert results[0].status_code == 200, results[0].text
    assert results[1].status_code in (404, 409), results[1].text
