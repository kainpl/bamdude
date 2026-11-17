"""C03: real HTTP at both caps, WAL writer time and an independent writer."""

import asyncio
import time

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.core.auth_lifetime import capture_account
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_note import LibraryFileNote
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import user_content_delete as deletion

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def test_engine(tmp_path, monkeypatch):
    database.import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}")
    event.listen(engine.sync_engine, "connect", database.configure_sqlite_connection)
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
        from backend.app.core.credential_config import initialize_credential_config_anchor

        await initialize_credential_config_anchor(conn)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    await engine.dispose()


@pytest.mark.parametrize("extra", [0, 1])
async def test_both_caps_are_decided_before_writer_and_success_stays_under_one_second(
    async_client, db_session, test_engine, extra, record_property
):
    owner = User(username="bounded-owner")
    db_session.add(owner)
    await db_session.flush()
    files = [
        LibraryFile(filename=f"{i}.3mf", file_path="", file_type="3mf", file_size=0, created_by_id=owner.id)
        for i in range(deletion.MAX_PARENTS // 2)
    ]
    archives = [
        PrintArchive(filename=f"{i}.3mf", file_path="", file_size=0, status="completed", created_by_id=owner.id)
        for i in range(deletion.MAX_PARENTS - len(files))
    ]
    db_session.add_all([*files, *archives, Settings(key="c_writer_probe", value="before")])
    await db_session.commit()
    target_id, capture = owner.id, capture_account(owner)
    before = await deletion.plan(db_session, capture)
    note_count = deletion.MAX_CHILDREN - sum(value for _, value in before.child_counts) + extra
    assert note_count > 0
    db_session.add_all([LibraryFileNote(library_file_id=files[0].id, body=str(i)) for i in range(note_count)])
    await db_session.commit()
    if not extra:
        complete = await deletion.plan(db_session, capture)
        assert len(complete.parents) == deletion.MAX_PARENTS
        assert sum(value for _, value in complete.child_counts) == deletion.MAX_CHILDREN
    await db_session.commit()  # do not keep a reader or writer in the fixture

    entered = asyncio.Event()
    starts, durations = {}, []

    def acquired(conn, cursor, statement, parameters, context, executemany):
        sql = statement.strip().upper()
        if sql == "BEGIN IMMEDIATE" or (
            sql.startswith("UPDATE USERS SET SESSION_GENERATION=USERS.SESSION_GENERATION") and conn not in starts
        ):
            starts[conn] = time.monotonic()
            entered.set()

    def committed(conn):
        started = starts.pop(conn, None)
        if started is not None:
            durations.append(time.monotonic() - started)

    event.listen(test_engine.sync_engine, "after_cursor_execute", acquired)
    event.listen(test_engine.sync_engine, "commit", committed)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def independent_writer():
        await entered.wait()
        async with factory() as db:
            await db.execute(update(Settings).where(Settings.key == "c_writer_probe").values(value="after"))
            await db.commit()

    task = None if extra else asyncio.create_task(independent_writer())
    try:
        response = await async_client.delete(f"/api/v1/users/{target_id}?delete_items=true")
        assert response.status_code == (409 if extra else 200), response.text
        if extra:
            assert response.json()["detail"]["error"] == "user_content_delete_too_large"
            assert durations == [] and not entered.is_set()
        else:
            await asyncio.wait_for(task, 3)
            assert durations and max(durations) < 1.0, durations
            record_property("sqlite_writer_seconds", max(durations))
            print(f"C03 SQLite WAL max writer: {max(durations):.6f}s at {deletion.MAX_PARENTS}/{deletion.MAX_CHILDREN}")
        db_session.expire_all()
        assert (await db_session.get(User, target_id) is not None) == bool(extra)
        if not extra:
            assert await db_session.scalar(select(Settings.value).where(Settings.key == "c_writer_probe")) == "after"
            assert await db_session.scalar(select(func.count()).select_from(LibraryFileNote)) == 0
    finally:
        event.remove(test_engine.sync_engine, "after_cursor_execute", acquired)
        event.remove(test_engine.sync_engine, "commit", committed)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["owner", "order", "claim"])
async def test_account_plan_refuses_a_new_owner_order_or_claim_before_any_removal(
    async_client, db_session, test_engine, monkeypatch, change
):
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.project import Project

    owner = User(username="changing-owner")
    db_session.add(owner)
    await db_session.flush()
    file = LibraryFile(filename="owned.3mf", file_path="", file_type="3mf", file_size=0, created_by_id=owner.id)
    archive = PrintArchive(filename="owned.3mf", file_path="", file_size=0, status="completed", created_by_id=owner.id)
    auto = AutoQueueItem(status="pending", created_by_id=owner.id)
    db_session.add_all([file, archive, auto])
    await db_session.commit()
    user_id, file_id, archive_id, auto_id = owner.id, file.id, archive.id, auto.id
    prepared, release = asyncio.Event(), asyncio.Event()
    original = deletion.plan
    calls = 0

    async def paused(*args, **kwargs):
        nonlocal calls
        planned = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            prepared.set()
            await release.wait()
        return planned

    monkeypatch.setattr(deletion, "plan", paused)
    task = asyncio.create_task(async_client.delete(f"/api/v1/users/{user_id}?delete_items=true"))
    try:
        await asyncio.wait_for(prepared.wait(), 5)
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with factory() as writer:
            if change == "owner":
                await writer.execute(update(LibraryFile).where(LibraryFile.id == file_id).values(created_by_id=None))
            elif change == "order":
                order = Project(name="New order")
                writer.add(order)
                await writer.flush()
                await writer.execute(
                    update(PrintArchive).where(PrintArchive.id == archive_id).values(project_id=order.id)
                )
            else:
                await writer.execute(update(AutoQueueItem).where(AutoQueueItem.id == auto_id).values(status="assigned"))
            await writer.commit()
        release.set()
        response = await asyncio.wait_for(task, 5)
        assert response.status_code == 409, response.text
        expected = (
            {"user_content_delete_active"} if change == "claim" else {"user_content_delete_changed", "source_changed"}
        )
        assert response.json()["detail"]["error"] in expected
        db_session.expire_all()
        assert await db_session.get(User, user_id) is not None
        assert await db_session.get(LibraryFile, file_id) is not None
        assert await db_session.get(PrintArchive, archive_id) is not None
        assert await db_session.get(AutoQueueItem, auto_id) is not None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
