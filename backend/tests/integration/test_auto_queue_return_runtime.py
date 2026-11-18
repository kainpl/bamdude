"""Owned passes read ready receipts only, persist finite diagnostics and drain before restore."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.settings import Settings
from backend.app.services import auto_queue_return as returns
from backend.app.services.auto_queue_return_runtime import AutoQueueReturnRuntime
from backend.tests.integration.swap_terminal_support import make_rig  # noqa: F401
from backend.tests.integration.test_auto_queue_return_publication import ready
from backend.tests.integration.test_auto_queue_return_settlement import failed_auto_run  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def mature(rig, receipt_id):
    async with rig.db() as db:
        # Move both committed terminal facts and their frozen ready basis into
        # the past, as an actual elapsed cooldown would. No scan mints proof.
        from backend.app.models.archive import PrintArchive
        from backend.app.models.print_queue import PrintQueueItem

        row = await db.get(AutoQueueReturn, receipt_id)
        old = row.completed_at
        row.completed_at -= timedelta(seconds=125)
        (await db.get(PrintQueueItem, row.old_job_id)).completed_at = row.completed_at
        (await db.get(PrintArchive, row.execution_archive_id)).completed_at = row.completed_at
        assert old != row.completed_at
        await db.commit()


async def test_restart_consumes_durable_ready_but_never_scans_failed(failed_auto_run, monkeypatch):
    rig, _, _, auto_id, original = failed_auto_run
    receipt_id, _ = await ready(failed_auto_run)
    await mature(rig, receipt_id)
    await original.quiesce_for_restore()
    restarted = AutoQueueReturnRuntime()
    restarted.open_admission()
    monkeypatch.setattr(
        "backend.app.core.websocket.ws_manager.send_queue_changed", AsyncMock(side_effect=RuntimeError("socket"))
    )
    await restarted.pass_ready()
    async with rig.db() as db:
        assert (await db.get(AutoQueueItem, auto_id)).status == "pending"
        assert (await db.get(AutoQueueItem, auto_id)).failed_return_count == 1
        assert (await db.get(AutoQueueReturn, receipt_id)).last_refusal_code is None
    assert await restarted.quiesce_for_restore()


async def test_disabled_pass_does_not_create_refusal_episode(failed_auto_run, caplog, monkeypatch):
    import logging

    rig, _, _, auto_id, runtime = failed_auto_run
    receipt_id, _ = await ready(failed_auto_run)
    await mature(rig, receipt_id)
    off_preflight = AsyncMock(side_effect=AssertionError("Off must not access accepted bytes"))
    monkeypatch.setattr(returns, "preflight", off_preflight)
    async with rig.db() as db:
        (await db.scalar(select(Settings).where(Settings.key == returns.SETTING_KEY))).value = "false"
        await db.commit()
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            await runtime.pass_ready()
    assert "AutoQueue return deferred: disabled" not in caplog.text
    off_preflight.assert_not_awaited()
    async with rig.db() as db:
        assert (await db.get(AutoQueueReturn, receipt_id)).last_refusal_code is None
        assert (await db.get(AutoQueueItem, auto_id)).failed_return_count == 0


async def test_two_tick_waiters_share_one_owned_pass(monkeypatch):
    runtime = AutoQueueReturnRuntime()
    runtime.open_admission()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def blocked():
        calls.append(1)
        entered.set()
        await release.wait()

    monkeypatch.setattr(runtime, "_pass_ready", blocked)
    first = asyncio.create_task(runtime.pass_ready())
    await entered.wait()
    second = asyncio.create_task(runtime.pass_ready())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert calls == [1] and len(runtime.tasks) == 1
    assert not await runtime.quiesce_for_restore(0.01)
    assert not runtime.open_admission()
    release.set()
    await second
    assert await runtime.quiesce_for_restore(1)


async def test_real_restore_refuses_before_file_preparation(async_client, monkeypatch, tmp_path):
    from io import BytesIO
    from zipfile import ZipFile

    from backend.app.core import db_portable
    from backend.app.services import backup_files, obico_history_files
    from backend.app.services.farm_startup_guard import farm_startup_guard

    runtime = AutoQueueReturnRuntime()
    runtime.open_admission()
    entered, release = asyncio.Event(), asyncio.Event()

    async def owned():
        entered.set()
        await release.wait()

    waiter = asyncio.create_task(runtime.run(owned))
    await entered.wait()
    real_drain = runtime.quiesce_for_restore

    async def bounded():
        return await real_drain(0.01)

    monkeypatch.setattr(runtime, "quiesce_for_restore", bounded)
    monkeypatch.setattr("backend.app.services.auto_queue_return_runtime.auto_queue_return_runtime", runtime)
    prepared = []

    class Files:
        committed = False

        def __init__(self, *args):
            pass

        def prepare(self):
            prepared.append(1)

        def finish(self):
            pass

    monkeypatch.setattr(backup_files, "FileRestore", Files)
    monkeypatch.setattr(db_portable, "validate_sqlite_backup", lambda *a, **kw: None)
    monkeypatch.setattr(backup_files, "validate_staged_queue_spool", lambda *a: None)
    monkeypatch.setattr(obico_history_files, "validate_history", lambda *a: None)
    monkeypatch.setattr(farm_startup_guard, "hold", lambda *a: None)
    monkeypatch.setattr("backend.app.services.connector_runtime.connector_runtime.clear_restore_fence", lambda: None)
    # Admission proof is downstream of the real source preflight. A genuine
    # image must reach that boundary; invalid bytes only test source refusal.
    import sqlite3
    from contextlib import closing

    source = tmp_path / "restore-source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript(
            "CREATE TABLE printers(id INTEGER); CREATE TABLE settings(key TEXT,value TEXT);"
            "CREATE TABLE _migrations(version INTEGER); INSERT INTO _migrations VALUES(41);"
        )
    body = BytesIO()
    with ZipFile(body, "w") as archive:
        archive.writestr("bamdude.db", source.read_bytes())
    try:
        response = await async_client.post(
            "/api/v1/settings/restore", files={"file": ("backup.zip", body.getvalue(), "application/zip")}
        )
        assert response.status_code == 409, response.text
        assert "AutoQueue return work has not stopped" in response.text
        assert not prepared and not runtime.admitting
        assert len(runtime.tasks) == 1
    finally:
        release.set()
        await waiter
        assert await real_drain(1)
