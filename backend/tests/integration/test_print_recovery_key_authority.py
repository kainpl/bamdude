"""Real HTTP key authorization on file SQLite, including the authority read under recovery's writer."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.api.routes import print_state_recovery as routes
from backend.app.core import auth, database
from backend.app.models.api_key import APIKey
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.user import User
from backend.app.services import print_state_recovery_store as store
from backend.tests.auth_helpers import bind_test_key

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def recovery_key(tmp_path, monkeypatch):
    database.import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery-key.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def connection(dbapi, _):
        dbapi.execute("PRAGMA journal_mode=WAL")
        dbapi.execute("PRAGMA busy_timeout=300")

    async with engine.begin() as db:
        await db.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    monkeypatch.setattr(auth, "async_session", factory)
    raw, hashed, prefix = auth.generate_api_key()
    async with factory() as db:
        user = User(username="key-operator", password_hash="x", role="admin", is_active=True)
        printer = Printer(name="Synthetic", serial_number="key-test", ip_address="127.0.0.1", access_code="00000000")
        db.add_all([user, printer])
        await db.flush()
        queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
        db.add(queue)
        await db.flush()
        start = datetime.now(timezone.utc) - timedelta(hours=2)
        archive = PrintArchive(
            printer_id=printer.id,
            queue_id=queue.id,
            filename="synthetic.gcode.3mf",
            file_path="synthetic",
            file_size=0,
            status="completed",
            started_at=start,
            completed_at=start + timedelta(hours=1),
            extra_data={},
        )
        db.add(archive)
        await db.flush()
        item = PrintQueueItem(queue_id=queue.id, archive_id=archive.id, status="printing", started_at=start)
        key = APIKey(
            name="review",
            key_hash=hashed,
            key_prefix=prefix,
            user_id=user.id,
            enabled=True,
            can_read_status=True,
            can_queue=True,
            can_control_printer=True,
            printer_ids=[printer.id],
        )
        await bind_test_key(db, key, user)
        db.add_all([item, key])
        await db.flush()
        queue.current_item_id = item.id
        await db.commit()
        ids = printer.id, item.id, key.id
    monkeypatch.setattr(
        store,
        "live_snapshot",
        Mock(
            return_value={
                "mode": "inactive",
                "identity": [1, "IDLE"],
                "busy": False,
                "effects": False,
                "observed_at": 1.0,
            }
        ),
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, factory, ids, raw
    await engine.dispose()


async def preview(client, ids, headers):
    pid, iid, _ = ids
    base = f"/api/v1/system/printers/{pid}/print-state"
    result = await client.post(
        base + "/preview", headers=headers, json={"kind": "item", "target_id": iid, "action": "align_terminal_item"}
    )
    assert result.status_code == 200, result.text
    return base, {"preview_token": result.json()["preview_token"], "confirmed": True}


@pytest.mark.parametrize("presentation", ["header", "bearer"])
async def test_a_key_can_apply_and_replay_without_a_second_sqlite_writer(recovery_key, presentation):
    client, factory, ids, raw = recovery_key
    headers = {"X-API-Key": raw} if presentation == "header" else {"Authorization": f"Bearer {raw}"}
    base, body = await preview(client, ids, headers)
    first = await asyncio.wait_for(client.post(base + "/apply", headers=headers, json=body), 5)
    assert first.status_code == 200, first.text
    again = await client.post(base + "/apply", headers=headers, json=body)
    assert again.status_code == 200 and again.json() == first.json()
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 1
        assert (await db.get(PrintQueueItem, ids[1])).status == "completed"
        assert (await db.get(PrinterQueue, ids[0])).is_paused is True


async def test_a_cloned_item_gets_a_new_recovery_identity(recovery_key):
    from backend.app.services.queue_ops import _copy_item_fields

    _, factory, ids, _ = recovery_key
    async with factory() as db:
        source = await db.get(PrintQueueItem, ids[1])
        clone = _copy_item_fields(source, None, 1)
        db.add(clone)
        await db.flush()
        assert clone.recovery_identity and clone.recovery_identity != source.recovery_identity


@pytest.mark.parametrize(
    "revoked",
    [
        "enabled",
        "printer_ids",
        "can_control_printer",
        "can_queue",
        "can_read_status",
        "expires_at",
        "key_hash",
        "user_id",
    ],
)
async def test_a_key_revoked_during_guard_wait_changes_nothing(recovery_key, monkeypatch, revoked):
    client, factory, ids, raw = recovery_key
    headers = {"X-API-Key": raw}
    base, body = await preview(client, ids, headers)
    original = store.claim_lanes
    waiting, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def held(*args, **kwargs):
        waiting.set()
        await release.wait()
        async with original(*args, **kwargs):
            yield

    monkeypatch.setattr(store, "claim_lanes", held)
    task = asyncio.create_task(client.post(base + "/apply", headers=headers, json=body))
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        async with factory() as db:
            key = await db.get(APIKey, ids[2])
            value = {
                "printer_ids": [],
                "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
                "key_hash": "rotated",
                "user_id": None,
            }.get(revoked, False)
            setattr(key, revoked, value)
            await db.commit()
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status_code == 403, result.text
        async with factory() as db:
            assert await db.scalar(select(func.count()).select_from(PrintStateRecoveryAction)) == 0
            assert (await db.get(PrintQueueItem, ids[1])).status == "printing"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
