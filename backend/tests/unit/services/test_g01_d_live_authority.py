"""Durable live-admission, family batching and actual-owner regression oracles."""

import asyncio
import json
import time
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core import auth, database
from backend.app.core.auth_lifetime import (
    VerifiedOutcome,
    capture_account,
    capture_long_lived,
    stage_revoke_session,
    stage_session,
    utcnow,
    validate_captured,
)
from backend.app.core.auth_runtime import auth_runtime
from backend.app.models.api_key import APIKey
from backend.app.models.auth_session import AuthSession
from backend.app.models.long_lived_token import LongLivedToken
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import live_authority as module

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def live(test_engine, monkeypatch):
    runtime = module.LiveAuthority()
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(module, "live_authority", runtime)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(auth, "async_session", factory)
    auth_runtime.open_admission()
    yield runtime, factory
    assert await runtime.shutdown(timeout=2), "actual live work or a handle leaked"


async def issue(factory):
    async with factory() as db:
        user = User(username="live-owner", role="admin", is_active=True)
        db.add(user)
        await db.flush()
        first = await stage_session(db, VerifiedOutcome(capture_account(user), "local"), remember_me=False)
        second = await stage_session(db, VerifiedOutcome(capture_account(user), "local"), remember_me=False)
        await db.commit()
        return first, second


def descriptor(issued):
    return module.LiveDescriptor(issued.capture, ("websocket:connect",), expires=time.monotonic() + 60)


async def test_exact_logout_closes_only_that_sid_after_current_read(live):
    runtime, factory = live
    first, second = await issue(factory)
    a, b = runtime.register(descriptor(first)), runtime.register(descriptor(second))
    try:
        await runtime.admit(a)
        await runtime.admit(b)
        async with factory() as db:
            await stage_revoke_session(db, first.capture)
            await db.commit()
        assert b.deliverable()  # no other-device retire or invalidation by numeric owner
        await asyncio.wait_for(a.stop.wait(), 3)
        assert a.code == 4401
        assert b.deliverable() and not b.retired
    finally:
        runtime.release(a)
        runtime.release(b)


@pytest.mark.parametrize(
    "kind,include_trashed,allowed", [("archive", False, False), ("archive", True, True), ("library", True, False)]
)
async def test_trash_thumbnail_policy_is_archive_only_and_still_requires_the_row(live, kind, include_trashed, allowed):
    from backend.app.models.archive import PrintArchive
    from backend.app.models.library import LibraryFile

    runtime, factory = live
    first, _ = await issue(factory)
    model = PrintArchive if kind == "archive" else LibraryFile
    values = {
        "filename": "trashed.3mf",
        "file_path": "trashed.3mf",
        "file_size": 0,
        "created_by_id": first.capture.user_id,
        "deleted_at": utcnow(),
    }
    if kind == "library":
        values["file_type"] = "3mf"
    async with factory() as db:
        row = model(**values)
        db.add(row)
        await db.commit()
        row_id = row.id
    metadata = (await runtime.resource_rows({(kind, row_id)}))[kind, row_id]
    permission = "archives:read_all" if kind == "archive" else "library:read_all"
    handle = runtime.register(
        module.LiveDescriptor(
            first.capture,
            (permission,),
            resource_kind=kind,
            resource_id=row_id,
            resource_stamp=module.resource_stamp(kind, metadata),
            include_trashed_archive=include_trashed,
        )
    )
    try:
        outcomes, _ = await runtime._read([handle])
        if allowed:
            assert isinstance(outcomes[handle], tuple)
            await runtime.admit(handle)
            assert handle.deliverable()
            async with factory() as db:
                await db.execute(delete(model).where(model.id == row_id))
                await db.commit()
            outcomes, _ = await runtime._read([handle])
            assert outcomes[handle] == 4401
        else:
            assert outcomes[handle] == 4401
    finally:
        runtime.release(handle)


async def test_narrowed_key_class_retires_old_socket_then_fresh_admission_gets_only_current_snapshot(live, monkeypatch):
    from backend.app.core import websocket
    from backend.app.core.auth_lifetime import capture_key
    from backend.app.services import live_projection
    from backend.tests.auth_helpers import bind_test_key

    runtime, factory = live
    monkeypatch.setattr(websocket, "live_authority", runtime)
    monkeypatch.setattr(live_projection, "live_authority", runtime)
    first, _ = await issue(factory)
    async with factory() as db:
        owner = await db.get(User, first.capture.user_id)
        printers = [
            Printer(name=f"printer-{n}", serial_number=f"NARROW-{n}", ip_address="127.0.0.1", access_code="test")
            for n in range(2)
        ]
        key = APIKey(name="narrowed", key_hash="scalar-only", key_prefix="bd_test", can_read_status=True)
        await bind_test_key(db, key, owner)
        db.add_all([key, *printers])
        await db.commit()
        desc = module.LiveDescriptor(capture_key(key), ("websocket:connect",))
        key_id, printer_ids = key.id, [printer.id for printer in printers]

    class Browser:
        def __init__(self):
            self.sent, self.closed = [], []
            self.delivered = asyncio.Event()

        async def accept(self):
            pass

        async def send_text(self, value):
            self.sent.append(json.loads(value))
            self.delivered.set()

        async def close(self, code):
            self.closed.append(code)

    manager, old, fresh = websocket.ConnectionManager(), Browser(), Browser()
    old_handle = runtime.register(desc)
    try:
        await manager.connect(old, authority=desc.capture, live_handle=old_handle)
        assert old_handle.entitlements.printers is None
        await manager.send(old, {"type": "printer_status", "printer_id": printer_ids[1], "data": {"progress": 1}})
        await asyncio.wait_for(old.delivered.wait(), 3)
        async with factory() as db:
            await db.execute(update(APIKey).where(APIKey.id == key_id).values(printer_ids=[printer_ids[0]]))
            await db.commit()
        # Current durable scope changes the projection class. The old socket
        # must resync rather than silently purging some future printer events.
        await asyncio.wait_for(old_handle.stop.wait(), 3)
        assert old_handle.code == 1013 and not old_handle.deliverable()
        await manager.disconnect(old)
        assert old.closed == [1013]
        fresh_handle = runtime.register(desc)
        await manager.connect(fresh, authority=desc.capture, live_handle=fresh_handle)
        assert fresh_handle.entitlements.printers == frozenset({printer_ids[0]})
        for printer_id in printer_ids:
            await manager.send(fresh, {"type": "printer_status", "printer_id": printer_id, "data": {"progress": 2}})
        await manager.send(fresh, {"type": "initial_status_complete", "bootstrap_id": "fresh-scope", "printers": 1})
        async with asyncio.timeout(3):
            while len(fresh.sent) != 2:
                fresh.delivered.clear()
                await fresh.delivered.wait()
        assert fresh.sent[0]["type"] == "printer_status"
        assert fresh.sent[0]["printer_id"] == printer_ids[0]
        assert fresh.sent[0]["data"]["progress"] == 2
        assert fresh.sent[1] == {"type": "initial_status_complete", "bootstrap_id": "fresh-scope", "printers": 1}
        assert len(old.sent) == 1
    finally:
        await manager.shutdown()
        runtime.release(old_handle)


async def test_bulk_reads_have_same_predicate_and_query_count_for_more_identical_viewers(live, test_engine):
    runtime, factory = live
    first, _ = await issue(factory)
    counts = []

    def count(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            counts.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    try:
        async with factory() as db:
            single = await validate_captured(db, first.capture, required_permissions=("websocket:connect",))
        counts.clear()
        small = [module.LiveHandle(descriptor(first), accepted_at=0) for _ in range(128)]
        outcomes, _ = await runtime._read(small)
        query_count = len(counts)
        assert query_count > 0
        assert all(result[0].user_id == single.user.id for result in outcomes.values())
        counts.clear()
        big = [module.LiveHandle(descriptor(first), accepted_at=0) for _ in range(256)]
        outcomes, _ = await runtime._read(big)
        assert len(counts) == query_count
        assert len(outcomes) == 256
        async with factory() as db:
            await db.execute(update(AuthSession).where(AuthSession.sid == first.sid).values(revoked_at=utcnow()))
            await db.commit()
        denied, _ = await runtime._read(big)
        assert set(denied.values()) == {4401}
        async with factory() as db:
            with pytest.raises(HTTPException) as caught:
                await validate_captured(db, first.capture)
        assert caught.value.status_code == 401
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", count)


async def test_admission_does_not_join_batch_started_before_its_accept(live, test_engine, monkeypatch):
    runtime, factory = live
    first, _ = await issue(factory)
    original = runtime._read
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []

    async def held(batch):
        result = await original(batch)
        reads.append(tuple(batch))
        if len(reads) == 1:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(runtime, "_read", held)
    a = runtime.register(descriptor(first))
    runtime.accepted(a)
    b = runtime.register(descriptor(first))  # nonadmitted while the old batch reads
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with test_engine.begin() as conn:
            # External SQL deliberately has no local ORM post-commit signal.
            await conn.execute(update(AuthSession).where(AuthSession.sid == first.sid).values(revoked_at=utcnow()))
        admission = asyncio.create_task(runtime.admit(b))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(HTTPException):
            await admission
        assert b.retired and not b.deliverable()
        assert all(b not in batch for batch in reads[:1])
        assert any(b in batch for batch in reads[1:])
    finally:
        release.set()
        runtime.release(a)
        runtime.release(b)


async def test_parent_deadline_bounds_memory_delivery_even_when_child_lives_longer(live):
    runtime, factory = live
    first, _ = await issue(factory)
    parent_expiry = utcnow() + timedelta(seconds=2)
    async with factory() as db:
        await db.execute(update(AuthSession).where(AuthSession.sid == first.sid).values(expires_at=parent_expiry))
        await db.commit()
    handle = runtime.register(descriptor(first))
    try:
        await runtime.admit(handle)
        assert 0 < handle.proof_until - time.monotonic() <= 2
        handle.proof_until = time.monotonic() - 0.01
        assert not handle.deliverable()  # no awaited SQL or scheduler turn needed
        handle.parent_until = time.monotonic() - 0.01
        assert handle.refusal_code() == 4401
    finally:
        runtime.release(handle)


async def test_cancellation_and_restore_timeout_do_not_detach_actual_check(live, monkeypatch):
    runtime, factory = live
    first, _ = await issue(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def resistant(_batch):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return {}, {}

    monkeypatch.setattr(runtime, "_read", resistant)
    handle = runtime.register(descriptor(first))
    waiter = asyncio.create_task(runtime.admit(handle))
    await asyncio.wait_for(entered.wait(), 3)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(runtime.tasks) == 1
    assert not await runtime.drain(timeout=0.01)
    assert len(runtime.tasks) == 1 and handle in runtime.handles
    release.set()
    await asyncio.gather(*runtime.tasks)
    runtime.release(handle)


async def test_unknown_commit_then_close_requires_new_admission_epoch(live, test_engine):
    runtime, factory = live
    first, _ = await issue(factory)
    db = factory()
    before = runtime.epoch

    def fail_commit(_connection):
        raise RuntimeError("injected unknown commit outcome")

    await db.execute(update(AuthSession).where(AuthSession.sid == first.sid).values(revoked_at=utcnow()))
    event.listen(test_engine.sync_engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError):
            await db.commit()
    finally:
        event.remove(test_engine.sync_engine, "commit", fail_commit)
        await db.close()
    assert runtime.epoch == before + 1
    assert "live_authority_changed" not in db.sync_session.info


async def test_pending_metadata_is_not_evicted_by_handle_only_resources(live, monkeypatch):
    runtime, _ = live
    monkeypatch.setattr(module, "MAX_HANDLES", 2)
    handle_rows = {("library", 1): {"id": 1}, ("library", 2): {"id": 2}}

    async def read(_batch):
        return {}, {("archive", 3): {"id": 3}, **handle_rows}

    monkeypatch.setattr(runtime, "_read", read)
    rows = await runtime.resource_rows({("archive", 3)})
    assert rows == {("archive", 3): {"id": 3}}
    assert set(runtime._resource_cache) == {("archive", 3)}
    assert not runtime._pending_resources and not runtime._resource_waiters


async def test_long_lived_admission_and_recheck_preserve_the_verified_scope(live):
    runtime, factory = live
    first, _ = await issue(factory)
    async with factory() as db:
        record = LongLivedToken(
            name="camera",
            user_id=first.user.id,
            owner_identity=first.capture.account_identity,
            contract_version=1,
            lookup_prefix="scope123",
            secret_hash="not-rehashed-by-live",
            scope="camera_stream",
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(record)
        await db.commit()
        capture = capture_long_lived(record)
    handle = runtime.register(module.LiveDescriptor(capture, ("camera:view",), long_lived_scope="camera_stream"))
    try:
        await runtime.admit(handle)
        async with factory() as db:
            await db.execute(
                update(LongLivedToken)
                .where(LongLivedToken.token_identity == capture.token_identity)
                .values(scope="monitor")
            )
            await db.commit()
        await asyncio.wait_for(handle.stop.wait(), 3)
        assert handle.code == 4401 and not handle.deliverable()
    finally:
        runtime.release(handle)


async def test_unrelated_signin_does_not_discard_a_current_viewers_inflight_batch(live, monkeypatch):
    runtime, factory = live
    first, _ = await issue(factory)
    original = runtime._read
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(batch):
        result = await original(batch)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(runtime, "_read", held)
    handle = runtime.register(descriptor(first))
    admission = asyncio.create_task(runtime.admit(handle))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        generation = handle.generation
        async with factory() as db:
            other = User(username="unrelated-login", role="admin", is_active=True)
            db.add(other)
            await db.flush()
            await stage_session(db, VerifiedOutcome(capture_account(other), "local"), remember_me=False)
            await db.commit()
        assert handle.generation == generation
        release.set()
        await admission
        assert handle.deliverable() and not handle.retired
    finally:
        release.set()
        await asyncio.gather(admission, return_exceptions=True)
        runtime.release(handle)


@pytest.mark.parametrize("kind", ["account", "key", "printer"])
async def test_reused_numeric_row_id_never_rebinds_pending_or_admitted_capture(live, kind):
    from backend.app.core.auth_lifetime import capture_key
    from backend.tests.auth_helpers import bind_test_key

    runtime, factory = live
    first, _ = await issue(factory)
    async with factory() as db:
        owner = await db.get(User, first.capture.user_id)
        printer = Printer(name="old", serial_number="OLD", ip_address="127.0.0.1", access_code="test")
        key = APIKey(name="old", key_hash="unused-live-scalar", key_prefix="bd_test", can_read_status=True)
        await bind_test_key(db, key, owner)
        db.add_all([key, printer])
        await db.commit()
        if kind == "key":
            desc = module.LiveDescriptor(capture_key(key), ("websocket:connect",))
        elif kind == "printer":
            desc = module.LiveDescriptor(
                first.capture, ("camera:view",), printer_id=printer.id, printer_identity=printer.connector_identity
            )
        else:
            desc = descriptor(first)
        pid, kid, uid = printer.id, key.id, owner.id
    admitted = runtime.register(desc)
    pending = runtime.register(desc)
    try:
        await runtime.admit(admitted)
        model, ident = {"printer": (Printer, pid), "key": (APIKey, kid), "account": (User, uid)}[kind]
        async with factory() as db:
            await db.execute(delete(model).where(model.id == ident))
            await db.commit()
        # Recreate in a separate committed operation, as the actual delete and
        # create doors do. SQLite's shared in-memory test connection cannot
        # model concurrent independent transactions over an uncommitted delete.
        async with factory() as db:
            if kind == "printer":
                db.add(Printer(id=pid, name="new", serial_number="NEW", ip_address="127.0.0.1", access_code="test"))
            elif kind == "key":
                owner = await db.get(User, uid)
                replacement = APIKey(
                    name="new", key_hash="unused-live-scalar", key_prefix="bd_test", can_read_status=True
                )
                await bind_test_key(db, replacement, owner)
                replacement.id = kid
                db.add(replacement)
            else:
                db.add(User(id=uid, username="live-owner", role="admin", is_active=True))
            await db.commit()
        # One batch predicate is used for both pending and already-admitted
        # captures; numeric ids cannot supply the missing immutable identity.
        outcomes, _ = await runtime._read([admitted, pending])
        assert outcomes == {admitted: 4401, pending: 4401}
        with pytest.raises(HTTPException):
            await runtime.admit(pending)
        assert not pending.deliverable()
    finally:
        runtime.release(admitted)
        runtime.release(pending)


@pytest.mark.parametrize("mutation", ["generation", "owner_disabled"])
async def test_independent_families_keep_generation_but_all_obey_current_owner(live, mutation):
    from backend.app.core.auth_lifetime import capture_key
    from backend.tests.auth_helpers import bind_test_key

    runtime, factory = live
    first, _ = await issue(factory)
    async with factory() as db:
        owner = await db.get(User, first.capture.user_id)
        key = APIKey(name="integration", key_hash="not-rehashed", key_prefix="bd_test", can_read_status=True)
        await bind_test_key(db, key, owner)
        token = LongLivedToken(
            name="camera",
            user_id=owner.id,
            owner_identity=owner.account_identity,
            contract_version=1,
            lookup_prefix="matrix12",
            secret_hash="not-rehashed",
            scope="camera_stream",
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add_all([key, token])
        await db.commit()
        captures = [first.capture, capture_key(key), capture_long_lived(token)]
    handles = [module.LiveHandle(module.LiveDescriptor(c, ("camera:view",)), accepted_at=0) for c in captures]
    before, _ = await runtime._read(handles)
    assert all(not isinstance(value, int) for value in before.values())
    async with factory() as db:
        owner = await db.get(User, first.capture.user_id)
        if mutation == "generation":
            owner.session_generation += 1
        else:
            owner.is_active = False
        await db.commit()
    after, _ = await runtime._read(handles)
    assert after[handles[0]] == 4401
    for handle in handles[1:]:
        assert (after[handle] == 4401) is (mutation == "owner_disabled")


async def test_new_metadata_demand_immediately_schedules_after_inflight_batch(live, monkeypatch):
    runtime, factory = live
    first, _ = await issue(factory)
    handle = runtime.register(descriptor(first))
    await runtime.admit(handle)
    monkeypatch.setattr(module, "RECHECK_SECONDS", 10)
    original = runtime._read
    entered, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def held(batch):
        nonlocal reads
        reads += 1
        result = await original(batch)
        if reads == 1:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(runtime, "_read", held)
    runtime._wake.set()
    await asyncio.wait_for(entered.wait(), 2)
    demand = asyncio.create_task(runtime.resource_rows({("archive", 999)}))
    try:
        await asyncio.sleep(0)
        release.set()
        # The old read did not include this reference. Waiting for the next
        # periodic tick would exhaust the request's one-second check budget.
        assert await asyncio.wait_for(demand, 2) == {("archive", 999): {}}
        assert reads >= 2
    finally:
        release.set()
        await asyncio.gather(demand, return_exceptions=True)
        runtime.release(handle)


async def test_admission_deadline_is_retryable_503_and_keeps_actual_check_owned(live, monkeypatch):
    runtime, factory = live
    first, _ = await issue(factory)
    original = runtime._read
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(batch):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # model an actual driver that hasn't completed yet
        return await original(batch)

    monkeypatch.setattr(runtime, "_read", held)
    monkeypatch.setattr(module, "CHECK_SECONDS", 0.05)
    handle = runtime.register(descriptor(first))
    try:
        with pytest.raises(HTTPException) as caught:
            await runtime.admit(handle)
        assert caught.value.status_code == 503
        assert caught.value.headers == {"Retry-After": "5"}
        assert entered.is_set() and handle.retired and handle.code == 1013
        assert runtime._check in runtime.tasks and not runtime._check.done()
    finally:
        release.set()
        await asyncio.gather(runtime._check, return_exceptions=True)
        runtime.release(handle)


async def test_actual_tick_naturally_expires_admitted_proof_and_keeps_cancel_resistant_check_owned(live, monkeypatch):
    runtime, factory = live
    issued, _ = await issue(factory)
    handle = runtime.register(descriptor(issued))
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = runtime._read
    try:
        await runtime.admit(handle)
        proof_until = handle.proof_until

        async def resistant(batch):
            result = await original(batch)
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
            return result

        monkeypatch.setattr(runtime, "_read", resistant)
        runtime._wake.set()
        await asyncio.wait_for(entered.wait(), 2)
        actual = runtime._check
        await asyncio.wait_for(cancelled.wait(), 2)
        assert actual in runtime.tasks and not actual.done()
        # Natural production proof expiry: no proof assignment, manual retire,
        # synthetic clock or delivery consumer can trigger the stop below.
        assert handle.proof_until == proof_until
        await asyncio.wait_for(handle.stop.wait(), 5)
        assert time.monotonic() >= proof_until
        assert handle.proof_until == proof_until
        assert handle.retired and handle.code == 1013 and not handle.admitted
        assert actual in runtime.tasks and not actual.done()
        assert not await runtime.drain(0)
        release.set()
        await asyncio.wait_for(asyncio.shield(actual), 2)
        assert actual.done() and actual not in runtime.tasks
        runtime.release(handle)
        assert await runtime.drain(2)
    finally:
        release.set()
        if runtime._check is not None:
            await asyncio.gather(runtime._check, return_exceptions=True)
        runtime.release(handle)
