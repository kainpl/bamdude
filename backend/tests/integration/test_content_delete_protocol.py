"""C04/C08/C10: independent connections, exact incarnations, root commit ownership."""

import asyncio
import json
import os
import sys
import uuid

import pytest
from sqlalchemy import event, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import library_usage, queue_sources
from backend.app.services.library_delete_cleanup import finish
from backend.app.services.library_delete_scope import library_delete_scope
from backend.app.services.library_identity import LibraryIdentity
from backend.app.services.library_trash import library_trash_service
from backend.app.services.queue_counters import detach_auto_queue_refs, detach_print_queue_refs

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(params=["sqlite", "postgresql"])
async def independent_factory(request, tmp_path, monkeypatch):
    database.import_all_models()
    schema = "g02_c_" + uuid.uuid4().hex
    admin = None
    if request.param == "postgresql":
        url = os.environ.get("G02_C_TEST_PG_URL")
        if not url:
            pytest.skip("requires explicitly supplied disposable G02_C_TEST_PG_URL")
        admin = create_async_engine(url)
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": schema, "application_name": schema}}
        )
        event.listen(engine.sync_engine, "before_cursor_execute", database._strip_tz_from_params, retval=True)
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'protocol.db'}")
        event.listen(engine.sync_engine, "connect", database.configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    from backend.app.services import archive_write_scope, queue_ops

    archive_write_scope._archive_locks.clear()
    queue_ops._queue_claim_locks.clear()
    queue_sources._reset_state()
    try:
        yield factory
    finally:
        queue_sources._reset_state()
        archive_write_scope._archive_locks.clear()
        queue_ops._queue_claim_locks.clear()
        await engine.dispose()
        if admin:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin.dispose()


async def source(factory, path=None):
    async with factory() as db:
        file = LibraryFile(filename="A.3mf", file_path=str(path or "library/files/A.3mf"), file_type="3mf", file_size=1)
        db.add(file)
        await db.flush()
        archive = PrintArchive(filename="A.3mf", file_path="", file_size=1, status="completed", library_file_id=file.id)
        db.add(archive)
        await db.commit()
        return LibraryIdentity.capture(file), archive.id, archive.recovery_identity


@pytest.mark.parametrize("outcome", ["rollback", "savepoint_rollback", "commit"])
async def test_display_counter_belongs_only_to_the_terminal_root_commit(independent_factory, outcome):
    factory = independent_factory
    proof, archive_id, _ = await source(factory)
    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        if outcome == "savepoint_rollback":
            nested = await db.begin_nested()
            await library_usage.stage(db, archive)
            await nested.rollback()
            await db.commit()
        else:
            await library_usage.stage(db, archive)
            await (db.commit() if outcome == "commit" else db.rollback())
        await library_usage.finish(db)
    async with factory() as db:
        assert (await db.get(LibraryFile, proof.id)).print_count == (1 if outcome == "commit" else 0)


@pytest.mark.parametrize("change", ["link", "incarnation", "epoch"])
async def test_counter_cannot_follow_detached_or_reused_navigation(independent_factory, change):
    factory = independent_factory
    proof, archive_id, identity = await source(factory)
    counter = library_usage.Counter(archive_id, identity, proof, queue_sources.current_epoch())
    async with factory() as db:
        if change == "link":
            await db.execute(update(PrintArchive).where(PrintArchive.id == archive_id).values(library_file_id=None))
        elif change == "incarnation":
            await db.execute(
                update(LibraryFile).where(LibraryFile.id == proof.id).values(file_path="library/files/B.3mf")
            )
        else:
            queue_sources.invalidate_receipts()
        await db.commit()
    await library_usage.increment(factory, counter)
    async with factory() as db:
        assert (await db.get(LibraryFile, proof.id)).print_count == 0


@pytest.mark.parametrize("outcome", ["rollback", "savepoint_rollback", "commit", "shared_path", "reused_id"])
async def test_library_bytes_follow_only_their_root_commit_and_current_owner(independent_factory, outcome):
    factory = independent_factory
    path = settings.library_dir / ("c-cleanup-" + uuid.uuid4().hex + ".3mf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"A")
    proof, _, _ = await source(factory, path)
    try:
        async with factory() as db:
            async with library_delete_scope(db, [proof]):
                if outcome == "savepoint_rollback":
                    nested = await db.begin_nested()
                file = await db.get(LibraryFile, proof.id)
                await library_trash_service.purge_rows(db, file)
                assert path.read_bytes() == b"A"
                if outcome == "rollback":
                    await db.rollback()
                else:
                    if outcome == "savepoint_rollback":
                        await nested.rollback()
                    await db.commit()
            if outcome in {"shared_path", "reused_id"}:
                async with factory() as other:
                    other.add(
                        LibraryFile(
                            id=proof.id if outcome == "reused_id" else proof.id + 1,
                            filename="B.3mf",
                            file_path=str(path),
                            file_type="3mf",
                            file_size=1,
                        )
                    )
                    await other.commit()
            await finish(db)
        assert path.exists() == (outcome != "commit")
    finally:
        path.unlink(missing_ok=True)


async def test_queue_navigation_never_deletes_a_reused_auto_id(independent_factory):
    factory = independent_factory
    async with factory() as db:
        printer = Printer(name="C", serial_number=uuid.uuid4().hex, ip_address="127.0.0.1", access_code="00000000")
        db.add(printer)
        await db.flush()
        lane = PrinterQueue(printer_id=printer.id)
        auto = AutoQueueItem(status="pending")
        db.add_all([lane, auto])
        await db.flush()
        row = PrintQueueItem(
            queue_id=lane.id,
            position=1,
            status="completed",
            source_auto_item_id=auto.id,
            source_auto_identity="old-incarnation",
        )
        db.add(row)
        await db.commit()
        auto_id, row_id, identity = auto.id, row.id, auto.recovery_identity
        await detach_print_queue_refs(db, [row_id])
        await db.delete(row)
        await db.commit()
        assert (await db.get(AutoQueueItem, auto_id)).recovery_identity == identity
        survivor = PrintQueueItem(
            queue_id=lane.id, position=1, status="completed", source_auto_item_id=auto_id, source_auto_identity=identity
        )
        db.add(survivor)
        await db.flush()
        await detach_auto_queue_refs(db, [auto_id])
        await db.delete(auto)
        await db.commit()
        await db.refresh(survivor)
        assert survivor.source_auto_item_id is None
        assert survivor.source_auto_identity == identity


async def test_hard_library_purge_detaches_jobs_but_preserves_ready_source_owners(independent_factory):
    from backend.app.models.queue_source import QueueSource

    factory = independent_factory
    proof, archive_id, _ = await source(factory)
    async with factory() as db:
        file = await db.get(LibraryFile, proof.id)
        file.is_external = True
        printer = Printer(
            name="sources", serial_number=uuid.uuid4().hex, ip_address="127.0.0.1", access_code="00000000"
        )
        db.add(printer)
        await db.flush()
        lane = PrinterQueue(printer_id=printer.id)
        blob = QueueSource(sha256="a" * 64, size_bytes=3, relative_path="objects/aa/source.3mf", format="3mf")
        db.add_all([lane, blob])
        await db.flush()
        legacy = PrintQueueItem(queue_id=lane.id, position=1, status="pending", library_file_id=proof.id)
        auto = AutoQueueItem(status="pending", library_file_id=proof.id)
        ready = PrintQueueItem(
            queue_id=lane.id,
            position=2,
            status="printing",
            library_file_id=proof.id,
            queue_source_id=blob.id,
            source_snapshot={"display_filename": "A.3mf", "opaque": "preserve"},
        )
        db.add_all([legacy, auto, ready])
        await db.commit()
        legacy_id, auto_id, ready_id, source_id = legacy.id, auto.id, ready.id, blob.id
        await library_trash_service.hard_delete_now(db, file)
        db.expire_all()
        assert await db.get(LibraryFile, proof.id) is None
        assert (await db.get(PrintArchive, archive_id)).library_file_id is None
        legacy = await db.get(PrintQueueItem, legacy_id)
        auto = await db.get(AutoQueueItem, auto_id)
        ready = await db.get(PrintQueueItem, ready_id)
        assert (legacy.status, legacy.library_file_id) == ("cancelled", None)
        assert (auto.status, auto.library_file_id) == ("failed", None)
        assert (ready.status, ready.library_file_id, ready.queue_source_id) == ("printing", None, source_id)
        assert ready.source_snapshot == {"display_filename": "A.3mf", "opaque": "preserve"}
        assert await db.get(QueueSource, source_id) is not None


async def wait_for_pg_lock(factory, count=1, contender=None):
    async with factory() as db:
        for _ in range(200):
            if contender is not None and contender.done():
                contender.result()  # surface an order assertion, not a polling timeout
            waiting = await db.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name=current_setting('application_name') AND wait_event_type='Lock' AND pid<>pg_backend_pid()"
                )
            )
            if waiting >= count:
                return
            # PostgreSQL can retain a statistics snapshot for this transaction;
            # every poll must observe the newly queued contender, not that cache.
            await db.rollback()
            await asyncio.sleep(0.01)
    raise AssertionError("the contender never waited on the real PostgreSQL row lock")


async def test_pg_counter_rereads_the_link_after_its_library_lock_wait(independent_factory):
    factory = independent_factory
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("READ COMMITTED row-lock witness is PostgreSQL specific")
    proof, archive_id, identity = await source(factory)
    async with factory() as blocker:
        await blocker.execute(select(LibraryFile.id).where(LibraryFile.id == proof.id).with_for_update())
        pending = asyncio.create_task(
            library_usage.increment(
                factory, library_usage.Counter(archive_id, identity, proof, queue_sources.current_epoch())
            )
        )
        try:
            await wait_for_pg_lock(factory)
            await blocker.execute(
                update(PrintArchive).where(PrintArchive.id == archive_id).values(library_file_id=None)
            )
            await blocker.commit()
            await asyncio.wait_for(pending, 5)
        finally:
            await blocker.rollback()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
    async with factory() as db:
        assert (await db.get(LibraryFile, proof.id)).print_count == 0


async def test_pg_counter_never_waits_on_the_finished_print_row(independent_factory):
    factory = independent_factory
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locks")
    proof, archive_id, identity = await source(factory)
    async with factory() as blocker:
        await blocker.execute(select(PrintArchive.id).where(PrintArchive.id == archive_id).with_for_update())

        def no_print_lock(conn, cursor, statement, parameters, context, executemany):
            if "print_archives" in statement and "FOR UPDATE" in statement:
                raise AssertionError("the post-terminal counter must never reacquire a print lock")

        engine = factory.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", no_print_lock)
        try:
            await asyncio.wait_for(
                library_usage.increment(
                    factory, library_usage.Counter(archive_id, identity, proof, queue_sources.current_epoch())
                ),
                3,
            )
        finally:
            event.remove(engine, "before_cursor_execute", no_print_lock)
        await blocker.rollback()
    async with factory() as db:
        assert (await db.get(LibraryFile, proof.id)).print_count == 1


@pytest.mark.parametrize("kind", ["queue", "auto_queue"])
@pytest.mark.parametrize("first", ["refresh", "delete"])
async def test_pg_refresh_and_delete_in_separate_processes_keep_library_before_jobs(
    independent_factory, tmp_path, kind, first
):
    factory = independent_factory
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("real PostgreSQL independent-process lock protocol")
    from backend.app.models.queue_source import QueueSource
    from backend.app.services import (
        swap_compatibility as sc,
        swap_job_refresh as refresh,
        swap_source_declarations as declarations,
    )
    from backend.app.services.queue_source_capture import capture_staged, plan_capture
    from backend.tests.integration.test_swap_declaration_service import (
        ALL,
        a_3mf,
        a_library_file,
        a_user,
        actor_of,
        declare,
    )
    from backend.tests.integration.test_swap_job_refresh import QUEUE_ALL, a_printer_queue, by_id, ref

    user = await a_user(factory)
    lane = await a_printer_queue(factory)
    path = tmp_path / "refresh.3mf"
    a_3mf(path, "C04")
    file_id = await a_library_file(factory, path, owner=user)
    made = []
    async with factory() as db:
        staged = await capture_staged(plan_capture(library_file=await db.get(LibraryFile, file_id)))

    async def attach(db, blob: QueueSource):
        fields = {
            "library_file_id": file_id,
            "status": "pending",
            "queue_source_id": blob.id,
            "source_snapshot": sc.snapshot_with_fact(
                queue_sources.snapshot_for(staged.receipt, blob), sc.UNKNOWN_SOURCE
            ),
        }
        job = PrintQueueItem(queue_id=lane, position=1, **fields) if kind == "queue" else AutoQueueItem(**fields)
        db.add(job)
        await db.flush()
        made.append(job.id)

    await queue_sources.publish(staged.receipt, attach, session_factory=factory)
    seen_bytes = await declarations.inspect("library", file_id, actor=actor_of(user), rights=ALL)
    await declare("library", file_id, seen_bytes, user, value=True)
    seen = await refresh.candidates("library", file_id, actor=actor_of(user), rights=ALL, job_rights=QUEUE_ALL)
    model = PrintQueueItem if kind == "queue" else AutoQueueItem
    from fastapi import HTTPException

    task, child = None, None
    library_requested = False

    def ordered(conn, cursor, statement, parameters, context, executemany):
        nonlocal library_requested
        if asyncio.current_task() is task and "FOR UPDATE" in statement:
            if "FROM library_files" in statement:
                library_requested = True
            if "FROM print_queue" in statement or "FROM auto_queue_items" in statement:
                assert library_requested, "refresh must lock the library before any job row"

    engine = factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", ordered)

    def start_refresh():
        return asyncio.create_task(
            refresh.refresh(
                "library",
                file_id,
                declaration_id=seen["declaration_id"],
                source_revision=seen["source_revision"],
                jobs=[ref(by_id(seen, kind, made[0]))],
                operation_id=str(uuid.uuid4()),
                actor=actor_of(user),
                rights=ALL,
                job_rights=QUEUE_ALL,
            )
        )

    async def start_delete(schema):
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "backend.tests.content_delete_pg_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        child.stdin.write(json.dumps({"schema": schema, "file_id": file_id}).encode())
        await child.stdin.drain()
        child.stdin.close()
        assert (await asyncio.wait_for(child.stdout.readline(), 15)).decode().strip() == "worker_preflight"
        return child

    try:
        async with factory() as blocker:
            await blocker.execute(select(model.id).where(model.id == made[0]).with_for_update())
            schema = await blocker.scalar(text("SELECT current_schema()"))
            try:
                if first == "refresh":
                    task = start_refresh()
                    await wait_for_pg_lock(factory, contender=task)
                    child = await start_delete(schema)
                else:
                    child = await start_delete(schema)
                    await wait_for_pg_lock(factory)
                    task = start_refresh()
                await wait_for_pg_lock(factory, 2, contender=task)
                await blocker.commit()
                if first == "refresh":
                    result = await asyncio.wait_for(task, 12)
                    assert result["refreshed"] == [{"kind": kind, "id": made[0]}]
                else:
                    with pytest.raises(HTTPException) as refused:
                        await asyncio.wait_for(task, 12)
                    assert refused.value.status_code in (404, 409)
                output, errors = await asyncio.wait_for(child.communicate(), 12)
                assert child.returncode == 0, errors.decode()
                assert b"worker_deleted" in output
            finally:
                await blocker.rollback()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if child is not None and child.returncode is None:
                    child.kill()
                    await child.communicate()
    finally:
        event.remove(engine, "before_cursor_execute", ordered)
    async with factory() as db:
        assert await db.get(LibraryFile, file_id) is None
        row = await db.get(model, made[0])
        assert row.library_file_id is None
        assert row.queue_source_id is not None


@pytest.mark.parametrize("door", ["withdraw", "trash_previous"])
@pytest.mark.parametrize("refuse_second", [False, True])
async def test_assembly_uses_a_clean_plan_per_file_and_reports_partial_cleanup(
    independent_factory, tmp_path, monkeypatch, door, refuse_second
):
    from contextlib import asynccontextmanager
    from dataclasses import replace

    from fastapi import HTTPException

    from backend.app.models.product import Product, product_files
    from backend.app.services import domain_authority, library_delete_scope as scopes
    from backend.app.services.assembly import job
    from backend.app.services.assembly.job import Source
    from backend.tests.unit.services.test_assembly_job_guards import _plan, _row

    factory = independent_factory
    proofs = []
    sources = []
    async with factory() as db:
        product = Product(name="shared product")
        db.add(product)
        await db.flush()
        for number in range(2):
            path = tmp_path / f"output-{number}.3mf"
            path.write_bytes(b"output")
            file = LibraryFile(filename=path.name, file_path=str(path), file_type="3mf", file_size=6, is_external=True)
            db.add(file)
            await db.flush()
            proofs.append(LibraryIdentity.capture(file))
            sources.append(
                Source((file.id, file.file_path, file.file_hash, file.created_at), path, "3mf", file.filename)
            )
            await db.execute(product_files.insert().values(product_id=product.id, library_file_id=file.id))
        await db.commit()
    observed = []
    original_scope = scopes.library_delete_scope
    original_purge = library_trash_service.purge_rows

    async def owned_product_plan(db, file):
        from backend.app.core.lock_ledger import ledger

        assert product.id in ledger(db).gates, "the shared product gate must precede the library phase"
        return await original_purge(db, file)

    monkeypatch.setattr(library_trash_service, "purge_rows", owned_product_plan)

    @asynccontextmanager
    async def checked(db, *args, **kwargs):
        # Hold the sessions in this list too: Python cannot recycle their ids.
        observed.append(db)
        assert not db.new and not db.dirty and not db.deleted
        from backend.app.core.lock_ledger import ledger

        assert not ledger(db).gates
        async with original_scope(db, *args, **kwargs):
            yield

    monkeypatch.setattr(scopes, "library_delete_scope", checked)
    calls = 0
    original_guard = domain_authority.guard

    async def guard(*args, **kwargs):
        nonlocal calls
        calls += 1
        if refuse_second and calls == 2:
            raise HTTPException(403, "fresh authority refused")
        return await original_guard(*args, **kwargs)

    monkeypatch.setattr(domain_authority, "guard", guard)
    if door == "withdraw":
        row = _row()
        row.status = "partial"
        row.files = [
            {"library_file_id": proof.id, "name": f"output-{number}.3mf"} for number, proof in enumerate(proofs)
        ]
        row.cleanup_proofs = {proof.id: proof for proof in proofs}
        await job._withdraw(row)
        assert [entry["library_file_id"] for entry in row.files] == ([proofs[1].id] if refuse_second else [])
    else:
        plan = replace(_plan(sources=sources), trash_ids=tuple(proof.id for proof in proofs))
        trashed, removed, kept = await job._trash_previous(plan)
        assert trashed == []
        assert removed == [proof.id for proof in proofs[: 1 if refuse_second else 2]]
        assert kept == ([proofs[1].id] if refuse_second else [])
    assert len(observed) == 2 and observed[0] is not observed[1]
    async with factory() as db:
        assert await db.get(LibraryFile, proofs[0].id) is None
        assert (await db.get(LibraryFile, proofs[1].id) is not None) == refuse_second
        assert await db.get(Product, product.id) is not None
    assert all(source.path.read_bytes() == b"output" for source in sources)  # external bytes stay


@pytest.mark.parametrize("parent", ["library", "product"])
async def test_pg_an_inflight_fk_publisher_refuses_delete_without_a_late_lock_wait(independent_factory, parent):
    from fastapi import HTTPException

    from backend.app.models.product import Product, product_files

    factory = independent_factory
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL FK KEY SHARE witness")
    proof, archive_id, _ = await source(factory)
    model, row_id = LibraryFile, proof.id
    if parent == "product":
        async with factory() as db:
            product = Product(name="FK publisher parent")
            db.add(product)
            await db.flush()
            await db.execute(product_files.insert().values(product_id=product.id, library_file_id=proof.id))
            await db.commit()
            model, row_id = Product, product.id
    async with factory() as publisher:
        # The same lock an INSERT referencing this parent takes, in another
        # transaction/process. The delete must refuse rather than wait here.
        await publisher.execute(select(model.id).where(model.id == row_id).with_for_update(read=True, key_share=True))
        async with factory() as db:
            with pytest.raises(HTTPException) as refusal:
                async with library_delete_scope(db, [proof]):
                    pytest.fail("the final FK fence was not proved")
            assert refusal.value.status_code == 409
            await db.rollback()
        await publisher.rollback()
    async with factory() as db:
        assert await db.get(LibraryFile, proof.id) is not None
        assert (await db.get(PrintArchive, archive_id)).library_file_id == proof.id


@pytest.mark.parametrize("change", ["foreign_lane", "product_link", "source_move"])
async def test_a_new_preflight_dependency_refuses_the_whole_library_mutation(independent_factory, monkeypatch, change):
    from fastapi import HTTPException

    from backend.app.models.product import Product, product_files
    from backend.app.services import library_delete_scope as scopes

    factory = independent_factory
    proof, archive_id, _ = await source(factory)
    prepared, release = asyncio.Event(), asyncio.Event()
    original = scopes.footprint
    calls = 0

    async def paused(*args, **kwargs):
        nonlocal calls
        found = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            prepared.set()
            await release.wait()
        return found

    monkeypatch.setattr(scopes, "footprint", paused)

    async def remove():
        async with factory() as db, scopes.library_delete_scope(db, [proof]):
            await library_trash_service.purge_rows(db, await db.get(LibraryFile, proof.id))
            await db.commit()

    task = asyncio.create_task(remove())
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        async with factory() as db:
            if change == "foreign_lane":
                printer = Printer(
                    name="foreign", serial_number=uuid.uuid4().hex, ip_address="127.0.0.1", access_code="00000000"
                )
                db.add(printer)
                await db.flush()
                lane = PrinterQueue(printer_id=printer.id)
                db.add(lane)
                await db.flush()
                db.add(PrintQueueItem(queue_id=lane.id, position=1, status="pending", library_file_id=proof.id))
            elif change == "product_link":
                product = Product(name="new dependency")
                db.add(product)
                await db.flush()
                await db.execute(product_files.insert().values(product_id=product.id, library_file_id=proof.id))
            else:
                await db.execute(update(LibraryFile).where(LibraryFile.id == proof.id).values(file_path="moved.3mf"))
            await db.commit()
        release.set()
        with pytest.raises(HTTPException) as refusal:
            await asyncio.wait_for(task, 5)
        assert refusal.value.status_code == 409
        async with factory() as db:
            assert await db.get(LibraryFile, proof.id) is not None
            assert (await db.get(PrintArchive, archive_id)).library_file_id == proof.id
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("keep", ["printing", "plate_gate", "foreign_current"])
async def test_archive_root_preserves_a_physical_owner_except_its_retired_printer(independent_factory, keep):
    from backend.app.services.archive import ArchiveService

    factory = independent_factory
    async with factory() as db:
        printer = Printer(name="owner", serial_number=uuid.uuid4().hex, ip_address="127.0.0.1", access_code="00000000")
        db.add(printer)
        await db.flush()
        archive = PrintArchive(
            filename="retained.3mf",
            file_path="",
            file_size=0,
            status="printing" if keep == "printing" else "completed",
            printer_id=printer.id,
        )
        db.add(archive)
        await db.flush()
        if keep == "plate_gate":
            printer.awaiting_plate_clear_archive_id = archive.id
        if keep == "foreign_current":
            foreign = Printer(
                name="other", serial_number=uuid.uuid4().hex, ip_address="127.0.0.1", access_code="00000000"
            )
            db.add(foreign)
            await db.flush()
            lane = PrinterQueue(printer_id=foreign.id, status="idle")
            db.add(lane)
            await db.flush()
            job = PrintQueueItem(queue_id=lane.id, position=1, status="completed", archive_id=archive.id)
            db.add(job)
            await db.flush()
            lane.current_item_id = job.id
        await db.commit()
        archive_id, printer_id = archive.id, printer.id
        assert await ArchiveService(db).delete_archive(archive_id) is False
        assert await db.get(PrintArchive, archive_id) is not None
        await db.rollback()
        db.info["retired_printer_cleanup"] = printer_id
        assert await ArchiveService(db).delete_archive(archive_id) is (keep != "foreign_current")
        db.info.pop("retired_printer_cleanup")
