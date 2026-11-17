"""C04: both real terminal owners commit before a concurrent user library purge."""

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.user import User
from backend.app.services import library_usage
from backend.tests.integration.swap_terminal_support import (  # noqa: F401
    a_library_file,
    an_execution,
    complete,
    give_job_a_snapshot,
    make_rig,
)
from backend.tests.integration.test_content_delete_protocol import independent_factory  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def test_engine(independent_factory, request):
    from backend.app.core.credential_config import initialize_credential_config_anchor

    engine = independent_factory.kw["bind"]
    if engine.dialect.name != "postgresql" and request.node.callspec.params["terminal"] != "calibration":
        pytest.skip("PostgreSQL lock witness; source-reuse calibration also runs on SQLite")
    async with engine.begin() as conn:
        await initialize_credential_config_anchor(conn)
    return engine


@pytest.mark.parametrize("terminal", ["live", "reconcile", "calibration"])
async def test_terminal_commit_releases_print_and_job_before_library_only_counter(
    async_client, db_session, make_rig, terminal, monkeypatch
):
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, item_id = await an_execution(rig)
    file_id = await a_library_file(rig, declared=False)
    await give_job_a_snapshot(rig, item_id, library_file_id=file_id)
    async with rig.factory() as db:
        owner = User(username="terminal-source-owner")
        db.add(owner)
        await db.flush()
        file = await db.get(LibraryFile, file_id)
        file.created_by_id = owner.id
        archive = await db.get(PrintArchive, archive_id)
        archive.library_file_id = file_id
        if terminal == "calibration":
            job = await db.get(PrintQueueItem, item_id)
            job.is_calibration = True
        await db.commit()
        owner_id = owner.id
    calls, failures = [], []
    original = library_usage.increment

    async def exercise_counter(factory, proof):
        # NOWAIT is the direct assertion: another connection can already lock
        # both rows. The terminal transaction is no longer their owner.
        async with rig.factory() as check:
            archive = await check.scalar(
                select(PrintArchive).where(PrintArchive.id == archive_id).with_for_update(nowait=True)
            )
            job = await check.scalar(
                select(PrintQueueItem).where(PrintQueueItem.id == item_id).with_for_update(nowait=True)
            )
            assert archive.status == "completed", (archive.status, job.status)
            # Reconcile advances the queue only after closing the archive;
            # NOWAIT above proves it owns neither row during the counter.
            assert job.status == ("printing" if terminal == "reconcile" else "completed"), (archive.status, job.status)
            await check.rollback()
        if factory.kw["bind"].dialect.name == "sqlite":
            # The shared terminal Rig deliberately creates its own SQLite
            # database. Use the real standalone purge on that same engine;
            # the HTTP client belongs to the original fixture database.
            from backend.app.services.library_trash import library_trash_service

            async with rig.factory() as cleanup:
                await library_trash_service.hard_delete_now(cleanup, await cleanup.get(LibraryFile, file_id))
        else:
            response = await async_client.delete(f"/api/v1/users/{owner_id}?delete_items=true")
            assert response.status_code == 200, response.text
        async with rig.factory() as check:
            assert await check.get(LibraryFile, file_id) is None
            check.add(
                LibraryFile(id=file_id, filename="B.3mf", file_path="library/new-B.3mf", file_type="3mf", file_size=1)
            )
            await check.commit()
        await original(factory, proof)

    async def counter(factory, proof):
        calls.append(proof)
        try:
            await exercise_counter(factory, proof)
        except Exception as error:
            # The product deliberately isolates optional display failures.
            # Keep the hook's exact failure outside that best-effort boundary.
            failures.append(repr(error))
            raise

    monkeypatch.setattr(library_usage, "increment", counter)
    if terminal != "reconcile":
        await complete(rig)
        await complete(rig)
    else:
        async with rig.factory() as db:
            archive = await db.get(PrintArchive, archive_id)
            assert await _reconcile_complete_archive(db, archive, status="completed", uncertain=False) == archive_id
        async with rig.factory() as db:
            archive = await db.get(PrintArchive, archive_id)
            assert await _reconcile_complete_archive(db, archive, status="completed", uncertain=False) is None
    assert failures == [], failures
    assert len(calls) == 1
    async with rig.factory() as check:
        assert (await check.get(PrintArchive, archive_id)).status == "completed"
        assert (await check.get(PrintArchive, archive_id)).library_file_id is None
        survivor = await check.get(LibraryFile, file_id)
        assert survivor is not None, "completion cleanup removed the reused source id"
        assert survivor.filename == "B.3mf"
        assert survivor.print_count == 0
