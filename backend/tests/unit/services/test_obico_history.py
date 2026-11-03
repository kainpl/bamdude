"""GH105: durable bytes, truthful geometry, retention and owned file lifetime."""

import asyncio
import hashlib
import io
import json
import os
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import delete, event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.models.obico_event import ObicoEvent
from backend.app.models.printer import Printer
from backend.app.services import obico_history as module
from backend.app.services.obico_geometry import project_boxes, validate_boxes
from backend.app.services.obico_history import HistoryUnavailable, ObicoHistory, filename, now

pytestmark = pytest.mark.asyncio


def jpeg(width=640, height=360, orientation=1, color="red"):
    output = io.BytesIO()
    image = Image.new("RGB", (width, height), color)
    exif = Image.Exif()
    exif[274] = orientation
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


@pytest.fixture
async def history(tmp_path, request, monkeypatch):
    database.import_all_models()
    monkeypatch.setattr("backend.app.core.db_portable._conform_pending", False)
    postgres = request.module.__name__.endswith("_postgres")
    schema = "obico_" + uuid4().hex
    admin = None
    dedicated = postgres and request.node.name == "test_portable_restore_roundtrip"
    if postgres:
        url = os.environ.get("TEST_POSTGRES_URL")
        if not url:
            pytest.skip("disposable TEST_POSTGRES_URL required")
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        admin = create_async_engine(url)
        if dedicated:
            from sqlalchemy.engine import make_url

            # The real restore deliberately replaces public. Give it a wholly
            # new owned database, never the configured/shared test database.
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(f'CREATE DATABASE "{schema}"'))
            engine = create_async_engine(make_url(url).set(database=schema))
        else:
            async with admin.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema + ",public"}})
        event.listen(engine.sync_engine, "before_cursor_execute", database._strip_tz_from_params, retval=True)
        monkeypatch.setattr("backend.app.core.db_dialect.is_sqlite", lambda: False)
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'history.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(database.Base.metadata.create_all)
    if postgres:
        from backend.app.migrations import _discover_migrations, _ensure_migrations_table

        monkeypatch.setattr("backend.app.core.db_dialect.is_postgres", lambda: True)
        await _ensure_migrations_table(engine)
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO _migrations(version, name) VALUES (:version, :name)"),
                [{"version": m["version"], "name": m["name"]} for m in _discover_migrations()],
            )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        for i in range(1, 4):
            db.add(Printer(id=i, name=f"p{i}", ip_address="127.0.0.1", access_code="fake", serial_number=f"s{i}"))
        await db.commit()
    history = ObicoHistory(tmp_path / "obico-detections", sessions)
    await history.start(enabled=True)
    yield history
    await history.shutdown()
    await engine.dispose()
    if admin is not None:
        if dedicated:
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(f'DROP DATABASE "{schema}" WITH (FORCE)'))
        else:
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def save(history, *, verdict="warning", frame=None, printer=1, boxes=None, timestamp=None, event_id=None):
    event_id = event_id or str(uuid4())
    accepted = history.offer(
        printer_id=printer,
        token=history.token(printer),
        frame=frame if frame is not None else jpeg(),
        verdict=verdict,
        facts={
            "task_name": "same-name",
            "score": 1.55,
            "current_p": 1.55,
            "frame_source": "live-buffer",
            "buffer_received_at": "2026-11-03T01:00:00Z",
        },
        detections=boxes if boxes is not None else [["spaghetti", 0.85, [320, 180, 100, 80]]],
        event_id=event_id,
        observed_at=timestamp or now(),
    )
    assert accepted
    await history._queue.join()
    return event_id


async def test_original_raw_geometry_restart_and_idempotence(history):
    frame = jpeg(1280, 720)
    boxes = [["spaghetti", 0.9, [640, 360, 256, 256]]]
    event_id = await save(history, frame=frame, boxes=boxes)
    async with history.session() as db:
        row = await db.get(ObicoEvent, event_id)
        assert row.raw_boxes == boxes
        assert row.state == "ready"
        assert not {"epoch", "generation"}.intersection(row.__table__.columns.keys())
    detail = await history.details(1, event_id)
    assert detail["facts"]["ml_backend"] is None
    assert detail["boxes"][0]["xyxy"] == pytest.approx([0.4, 232 / 720, 0.6, 488 / 720])
    assert detail["score"] == 1.55
    assert detail["facts"]["captured_at"] is None
    assert await history.details(1, event_id, image=True) == frame
    assert await history.details(2, event_id, image=True) is None
    await save(history, event_id=event_id)
    assert history._count == 1
    await history.shutdown()
    restored = ObicoHistory(history.root, history._session_factory)
    await restored.start()  # off by default, old proof still readable
    try:
        assert await restored.details(1, event_id, image=True) == frame
        assert (await restored.page(scope=None))["oldest_saved_at"]
        assert restored._count == 1
    finally:
        await restored.shutdown()


@pytest.mark.parametrize("orientation", range(1, 9))
async def test_encoded_dimensions_ignore_all_exif_rotations(history, orientation):
    frame = jpeg(100, 60, orientation)
    event_id = await save(history, frame=frame, boxes=[["box", 0.8, [50, 30, 20, 12]]])
    detail = await history.details(1, event_id)
    assert (detail["width"], detail["height"]) == (100, 60)
    assert detail["facts"]["exif_orientation"] == orientation
    assert detail["boxes"][0]["xyxy"] == [0.4, 0.4, 0.6, 0.6]
    assert await history.details(1, event_id, image=True) == frame


async def test_validation_is_bounded_and_never_mutates_input():
    original = [
        ["<box>", 0.8, [320, 180, 100, 80]],
        ["bad", True, [1, 2, 3, 4]],
        ["bad", 0.5, [float("nan"), 0, 1, 2]],
        ["bad", 0.5, [True, 0, 1, 2]],
        ["bad", 2, [0, 0, 1, 1]],
        ["bad", 0.5, [0, 0, -1, 1]],
    ]
    raw, invalid = validate_boxes(original)
    assert invalid == 5 and len(raw) == 1
    assert raw[0][2] == [320, 180, 100, 80]
    assert project_boxes(raw, 640, 360)[0]["xyxy"] == [270 / 640, 140 / 360, 370 / 640, 220 / 360]
    assert original[0][2] == [320, 180, 100, 80]
    assert project_boxes([["off", 1, [-100, -100, 2, 2]]], 640, 360) == []
    assert project_boxes([["clipped", 1, [0, 0, 100, 80]]], 640, 360)[0]["xyxy"] == [0, 0, 50 / 640, 40 / 360]
    assert len(validate_boxes([original[0]] * 300)[0]) == 256


async def test_safe_cannot_evict_higher_classes_and_oldest_in_class(history):
    history.max_events = 3
    failure = await save(history, verdict="failure", timestamp=now() - timedelta(days=3))
    warning = await save(history, verdict="warning", timestamp=now() - timedelta(days=2))
    safe = await save(history, verdict="safe", timestamp=now() - timedelta(days=1))
    newer_safe = await save(history, verdict="safe")
    assert await history.details(1, safe) is None
    assert await history.details(1, failure) is not None
    assert await history.details(1, warning) is not None
    await save(history, verdict="warning")
    assert await history.details(1, newer_safe) is None
    history.max_events = 2
    await save(history, verdict="failure")
    assert await history.details(1, warning) is None
    assert await history.details(1, failure) is not None
    dropped_safe = await save(history, verdict="safe")
    assert await history.details(1, dropped_safe) is None
    assert history.dropped["quota"] == 1


async def test_pin_preserves_dump_files_during_clear_and_records_continue(history, tmp_path):
    from backend.app.core.db_portable import dump_to_sqlite
    from backend.app.services.obico_history_files import stage_history, validate_history

    first = await save(history)
    db_path = tmp_path / "dump.db"
    async with history.pin_backup():
        await dump_to_sqlite(history._session_factory.kw["bind"], database.Base.metadata, db_path)
        later = await save(history, verdict="failure")
        assert await history.details(1, later)
        assert await history.clear() == 2
        assert (history.root / filename(first)).exists()
        staging = tmp_path / "backup"
        staging.mkdir()
        await module._file_work(stage_history, history.root.parent, staging, db_path)
        assert {p.name for p in (staging / "obico-detections").iterdir()} == {filename(first)}
        validate_history(staging, db_path)
        assert history.status()["used_bytes"] > 0
    assert not list(history.root.iterdir())
    assert history.status()["used_bytes"] == 0


async def test_pin_delete_reuse_and_rollback(history):
    event_id = await save(history)
    old_token = history.token(1)
    async with history.pin_backup():
        async with history.printer_deletion(1), history.session() as db:
            await db.execute(delete(ObicoEvent).where(ObicoEvent.printer_id == 1))
            await db.execute(delete(Printer).where(Printer.id == 1))
            await db.commit()
        assert (history.root / filename(event_id)).exists()
        assert (await history.page(scope=None))["items"] == []
        assert not history.offer(
            printer_id=1,
            token=old_token,
            frame=jpeg(),
            verdict="failure",
            facts={},
            detections=[],
            event_id=str(uuid4()),
            observed_at=now(),
        )
    assert not (history.root / filename(event_id)).exists()
    surviving = await save(history, printer=2)
    with pytest.raises(RuntimeError):
        async with history.printer_deletion(2):
            async with history.session() as db:
                await db.execute(delete(ObicoEvent).where(ObicoEvent.printer_id == 2))
                await db.rollback()
            raise RuntimeError("delete rejected")
    assert await history.details(2, surviving, image=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_p1_pin_registration_waits_between_no_pin_check_and_unlink(history, monkeypatch, cancel):
    event_id = await save(history)
    started, release = threading.Event(), threading.Event()
    original = module._unlink

    def blocked_unlink(root, name):
        if name == filename(event_id):
            started.set()
            assert release.wait(5)
        original(root, name)

    monkeypatch.setattr(module, "_unlink", blocked_unlink)
    deleting = asyncio.create_task(history.clear())
    assert await asyncio.to_thread(started.wait, 5)
    if cancel:
        deleting.cancel()
    registered = asyncio.Event()
    attempted = asyncio.Event()

    async def backup():
        attempted.set()
        async with history.pin_backup():
            registered.set()
            assert not (history.root / filename(event_id)).exists()

    pinning = asyncio.create_task(backup())
    await attempted.wait()
    locked_before_release = history.guard.locked()
    registered_early = registered.is_set()
    pins_before_release = history._pins
    release.set()
    results = await asyncio.gather(deleting, pinning, return_exceptions=True)
    assert locked_before_release and not registered_early
    assert pins_before_release == 0
    if cancel:
        assert isinstance(results[0], asyncio.CancelledError) and not isinstance(results[1], BaseException)
    else:
        assert not any(isinstance(result, BaseException) for result in results)
    assert registered.is_set()


async def test_cancel_keeps_writer_reservation_and_restore_refuses_until_thread_ends(history, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = module._publish

    def blocked_publish(*args):
        started.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(module, "_publish", blocked_publish)
    event_id = str(uuid4())
    assert history.offer(
        printer_id=1,
        token=history.token(1),
        frame=jpeg(),
        verdict="failure",
        facts={},
        detections=[],
        event_id=event_id,
        observed_at=now(),
    )
    assert await asyncio.to_thread(started.wait, 5)
    async with history.session() as db:
        assert (await db.get(ObicoEvent, event_id)).state == "writing"
    assert not await history.quiesce(timeout=0.01)
    assert history.guard.locked() and history._reserved > 0
    assert not history._worker.done()
    release.set()
    assert await history.quiesce()
    assert history._reserved == 0
    async with history.session() as db:
        assert await db.get(ObicoEvent, event_id) is None


@pytest.mark.parametrize("change", ["epoch", "not-ready"])
async def test_queued_observation_cannot_cross_epoch_change(history, change):
    event_id = str(uuid4())
    async with history.guard:
        assert history.offer(
            printer_id=1,
            token=history.token(1),
            frame=jpeg(),
            verdict="failure",
            facts={},
            detections=[],
            event_id=event_id,
            observed_at=now(),
        )
        if change == "epoch":
            history.set_enabled(False)
            history.set_enabled(True)
        else:
            history._ready = False
    await history._queue.join()
    async with history.session() as db:
        assert await db.get(ObicoEvent, event_id) is None


async def test_cancelled_printer_delete_waiter_does_not_leave_admission_blocked(history):
    attempted = asyncio.Event()
    old_token = history.token(1)

    async def deleting():
        attempted.set()
        async with history.printer_deletion(1):
            pytest.fail("still waiting for the guard")

    async with history.guard:
        pending = asyncio.create_task(deleting())
        await attempted.wait()
        assert 1 in history._blocked
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert 1 not in history._blocked
        assert history.token(1) != old_token
    event_id = await save(history)
    assert await history.details(1, event_id, image=True) == jpeg()


async def test_pending_state_is_visible_in_one_batch_before_writer_admission(history):
    event_id = str(uuid4())
    assert history.offer(
        printer_id=1,
        token=history.token(1),
        frame=jpeg(),
        verdict="warning",
        facts={},
        detections=[],
        event_id=event_id,
        observed_at=now(),
    )
    # No yield before the read takes the free guard: the writer queues behind it.
    assert await history.recent_states([event_id]) == {event_id: "writing"}
    await history._queue.join()
    assert await history.recent_states([event_id]) == {event_id: "ready"}


async def test_backup_cancel_keeps_its_pin_until_copy_ends_without_releasing_another_pin(history):
    copying, release = threading.Event(), threading.Event()

    def copy():
        copying.set()
        assert release.wait(5)

    async def backup():
        async with history.pin_backup():
            await module._file_work(copy)

    async with history.pin_backup():
        task = asyncio.create_task(backup())
        assert await asyncio.to_thread(copying.wait, 5)
        assert history._pins == 2
        task.cancel()
        checkpoint = asyncio.Event()
        asyncio.get_running_loop().call_soon(checkpoint.set)
        await checkpoint.wait()
        held = history._pins
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert held == 2 and history._pins == 1
        with pytest.raises(OSError):
            async with history.pin_backup():
                await module._file_work(lambda: (_ for _ in ()).throw(OSError("copy failed")))
        assert history._pins == 1
    assert history._pins == 0


async def test_read_cancel_owns_file_worker_and_restore_waits_for_it(history, monkeypatch):
    event_id = await save(history)
    started, release = threading.Event(), threading.Event()
    original = module._read

    def blocked_read(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(module, "_read", blocked_read)
    reader = asyncio.create_task(history.details(1, event_id, image=True))
    assert await asyncio.to_thread(started.wait, 5)
    reader.cancel()
    assert not await history.quiesce(timeout=0.01)
    assert history.guard.locked() and not reader.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert await history.quiesce()
    # _read closed its handle before the request could finish; Windows can move it.
    path = history.root / filename(event_id)
    renamed = path.with_suffix(".part")
    path.rename(renamed)
    renamed.rename(path)


async def test_admission_and_queries_remain_bounded_without_poll_time_inventory(history, monkeypatch):
    from sqlalchemy import event

    monkeypatch.setattr(module, "inventory", lambda *args: pytest.fail("poll-time inventory"))
    statements = []
    engine = history._session_factory.kw["bind"]

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        await save(history)
        first_count = len(statements)
        assert not any("count(" in statement for statement in statements)
        for _ in range(5):
            await save(history)
        statements.clear()
        await save(history)
        assert len(statements) == first_count
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    async with history.guard:
        assert history._queue.maxsize == 8
        for _ in range(8):
            assert history.offer(
                printer_id=1,
                token=history.token(1),
                frame=jpeg(),
                verdict="warning",
                facts={},
                detections=[],
                event_id=str(uuid4()),
                observed_at=now(),
            )
        assert not history.offer(
            printer_id=1,
            token=history.token(1),
            frame=jpeg(),
            verdict="warning",
            facts={},
            detections=[],
            event_id=str(uuid4()),
            observed_at=now(),
        )
        assert len(history._pending) == 8 and history.dropped["queue_full"] == 1
        history.set_enabled(False)
    await history._queue.join()
    assert not history._pending and history._queue_bytes == 0


async def test_published_file_replaces_reservation_before_ready_commit(history, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = module._read
    frame = jpeg()
    history.max_bytes = len(frame)

    def verify(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(module, "_read", verify)
    writing = asyncio.create_task(save(history, frame=frame))
    assert await asyncio.to_thread(started.wait, 5)
    charged = history.status()["used_bytes"]
    reservation = history._reserved
    release.set()
    await writing
    assert charged == len(frame) and reservation == 0
    assert history.status()["used_bytes"] <= history.max_bytes


async def test_queue_byte_limit_applies_before_count_limit(history):
    frame = b"x" * module.MAX_FRAME_BYTES
    async with history.guard:
        for _ in range(4):
            assert history.offer(
                printer_id=1,
                token=history.token(1),
                frame=frame,
                verdict="warning",
                facts={},
                detections=[],
                event_id=str(uuid4()),
                observed_at=now(),
            )
        assert history._queue_bytes == module.QUEUE_BYTES
        assert not history.offer(
            printer_id=1,
            token=history.token(1),
            frame=frame,
            verdict="warning",
            facts={},
            detections=[],
            event_id=str(uuid4()),
            observed_at=now(),
        )
        assert history._queue.qsize() == 4 < history._queue.maxsize
        assert history.dropped["queue_full"] == 1
        history.set_enabled(False)
    await history._queue.join()
    assert history._queue_bytes == 0


async def test_recording_is_opt_in_and_age_boundary_is_strict(history, monkeypatch):
    from backend.app.schemas.settings import AppSettings

    assert AppSettings().obico_save_detections is False
    clock = now()
    monkeypatch.setattr(module, "now", lambda: clock)
    boundary = await save(history, timestamp=clock - timedelta(days=module.KEEP_DAYS))
    assert await history.details(1, boundary)
    clock += timedelta(microseconds=1)
    history.set_enabled(False)
    async with history.guard:
        await history._sweep_locked()
    assert await history.details(1, boundary) is None


async def test_historical_migration_creates_a_writable_table(history):
    from backend.app.migrations.m215_obico_history import upgrade

    engine = history._session_factory.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(ObicoEvent.__table__.drop)
        await upgrade(connection)
    event_id = await save(history)
    assert await history.details(1, event_id, image=True) == jpeg()


async def test_scoped_keyset_before_page_cursor_and_counts(history):
    for printer in (1, 2, 3):
        await save(history, printer=printer)
    visible = await history.page(scope=frozenset({3}), limit=1)
    assert [item["printer_id"] for item in visible["items"]] == [3]
    assert visible["saved_count"] == 1 and visible["next_cursor"] is None
    empty = await history.page(scope=frozenset(), limit=1)
    assert empty == {"items": [], "next_cursor": None, "saved_count": 0, "total_count": 0, "oldest_saved_at": None}
    first = await history.page(scope=None, limit=1, audience="key-a")
    second = await history.page(scope=None, limit=1, audience="key-a", cursor=first["next_cursor"])
    assert first["items"][0]["id"] != second["items"][0]["id"]
    with pytest.raises(ValueError):
        await history.page(scope=None, cursor=first["next_cursor"], audience="key-b")
    await history.clear()
    with pytest.raises(ValueError):
        await history.page(scope=None, cursor=first["next_cursor"], audience="key-a")


async def test_age_failed_unlink_pin_budget_invalid_jpeg_and_off(history, monkeypatch):
    event_id = await save(history)
    frame_size = history.status()["used_bytes"]
    history.max_bytes = frame_size
    async with history.pin_backup():
        dropped = await save(history, verdict="failure")
        assert await history.details(1, dropped) is None
        assert history.dropped["quota_pinned"] == 1
    original = module._unlink
    monkeypatch.setattr(module, "_unlink", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    await history.clear()
    assert history.status()["used_bytes"] == frame_size
    assert (history.root / filename(event_id)).exists()
    monkeypatch.setattr(module, "_unlink", original)
    async with history.guard:
        await history._sweep_locked()
    assert history.status()["used_bytes"] == 0
    expired = await save(history, timestamp=now() - timedelta(days=16))
    history.set_enabled(False)
    async with history.guard:
        await history._sweep_locked()
    assert await history.details(1, expired) is None
    assert not history.offer(
        printer_id=1,
        token=history.token(1),
        frame=jpeg(),
        verdict="failure",
        facts={},
        detections=[],
        event_id=str(uuid4()),
        observed_at=now(),
    )
    history.set_enabled(True)
    await save(history, frame=b"not a jpeg")
    assert history.dropped["invalid_frame"] == 1


@pytest.mark.parametrize("fault", ["before", "part", "renamed", "corrupt", "ready-commit"])
async def test_publication_faults_never_leave_ready_missing_files(history, monkeypatch, fault):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    original = module._publish

    def failed_publish(root, event_id, frame):
        if fault == "before":
            raise OSError("before temp")
        if fault == "part":
            (root / (event_id.replace("-", "") + ".part")).write_bytes(frame[:100])
            raise OSError("partial temp")
        original(root, event_id, frame)
        if fault == "renamed":
            raise OSError("after rename")
        if fault == "corrupt":
            (root / filename(event_id)).write_bytes(b"corrupt")

    def fail_ready_commit(session):
        if fault == "ready-commit" and any(
            isinstance(row, ObicoEvent) and row.state == "ready" for row in session.dirty
        ):
            raise RuntimeError("ready commit failed")

    monkeypatch.setattr(module, "_publish", failed_publish)
    event.listen(Session, "before_commit", fail_ready_commit)
    try:
        event_id = await save(history)
    finally:
        event.remove(Session, "before_commit", fail_ready_commit)
    assert await history.details(1, event_id) is None
    assert not list(history.root.iterdir())
    assert history.status()["used_bytes"] == 0
    assert history.dropped["write_failed"] == 1


async def test_startup_reconciles_orphans_incomplete_and_missing_references(history):
    good = await save(history)
    missing = await save(history)
    writing = await save(history)
    (history.root / filename(missing)).unlink()
    orphan = history.root / (uuid4().hex + ".part")
    orphan.write_bytes(b"orphan")
    async with history.session() as db:
        row = await db.get(ObicoEvent, writing)
        row.state = "writing"
        await db.commit()
    await history.shutdown()
    recovered = ObicoHistory(history.root, history._session_factory)
    await recovered.start()
    try:
        assert (await recovered.details(1, missing))["state"] == "unavailable"
        assert await recovered.details(1, writing) is None
        assert await recovered.details(1, good, image=True) == jpeg()
        assert not orphan.exists()
        assert recovered.status()["used_bytes"] == len(jpeg())
    finally:
        await recovered.shutdown()


async def test_foreign_regular_files_do_not_disable_or_enter_store_accounting(history):
    kept = await save(history)
    foreign = dict.fromkeys((".DS_Store", "desktop.ini", "Thumbs.db"), b"foreign")
    for name, data in foreign.items():
        (history.root / name).write_bytes(data)
    await history.shutdown()
    restarted = ObicoHistory(history.root, history._session_factory)
    await restarted.start(enabled=True)
    try:
        assert (await restarted.page(scope=None))["saved_count"] == 1
        assert await restarted.details(1, kept, image=True) == jpeg()
        assert restarted.status()["used_bytes"] == len(jpeg())
        await save(restarted)
        await restarted.clear()
        assert {p.name: p.read_bytes() for p in restarted.root.iterdir()} == foreign
    finally:
        await restarted.shutdown()


async def test_foreign_directories_are_ignored_without_traversal(history, monkeypatch):
    kept = await save(history)
    directories = [history.root / name for name in ("@eaDir", ".AppleDouble")]
    for directory in directories:
        directory.mkdir()
        (directory / (uuid4().hex + ".jpg")).write_bytes(b"foreign child")
    await history.shutdown()
    original_iterdir = type(history.root).iterdir

    def flat_inventory(path):
        assert path == history.root, "Inventory must never traverse a foreign directory"
        return original_iterdir(path)

    restarted = ObicoHistory(history.root, history._session_factory)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(type(history.root), "iterdir", flat_inventory)
            await restarted.start(enabled=True)
            assert (await restarted.page(scope=None))["saved_count"] == 1
            assert await restarted.details(1, kept, image=True) == jpeg()
            await save(restarted)
            await restarted.clear()
        assert restarted.status()["used_bytes"] == 0
        assert all([p.read_bytes() for p in directory.iterdir()] == [b"foreign child"] for directory in directories)
        owned_directory = history.root / (uuid4().hex + ".jpg")
        owned_directory.mkdir()
        with pytest.raises(ValueError, match="special files"):
            module.inventory(history.root)
    finally:
        await restarted.shutdown()


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
@pytest.mark.parametrize("directory", [False, True])
async def test_foreign_links_are_still_refused(history, monkeypatch, kind, directory):
    foreign = history.root / "desktop.ini"
    if directory:
        foreign.mkdir()
    else:
        foreign.write_bytes(b"not an owned file")
    real_check = getattr(type(foreign), kind)
    monkeypatch.setattr(type(foreign), kind, lambda path: path == foreign or real_check(path))
    with pytest.raises(ValueError, match="refuses links"):
        module.inventory(history.root)


async def test_startup_checks_sizes_without_reading_original_contents(history, monkeypatch):
    kept = await save(history)
    await history.shutdown()
    original_open = type(history.root).open

    def no_frame_read(path, mode="r", *args, **kwargs):
        if path.parent == history.root and "r" in mode:
            raise AssertionError("Startup must not read frame contents")
        return original_open(path, mode, *args, **kwargs)

    restarted = ObicoHistory(history.root, history._session_factory)
    with monkeypatch.context() as patch:
        patch.setattr(type(history.root), "open", no_frame_read)
        await restarted.start()
        assert (await restarted.page(scope=None))["saved_count"] == 1
    try:
        assert await restarted.details(1, kept, image=True) == jpeg()
        path = history.root / filename(kept)
        damaged = bytearray(path.read_bytes())
        damaged[-1] ^= 1  # same size, still requires the on-read digest check
        path.write_bytes(damaged)
        assert await restarted.details(1, kept, image=True) is None
    finally:
        await restarted.shutdown()


@pytest.mark.parametrize("operation", ["read", "inventory"])
async def test_transient_startup_io_never_deletes_an_intact_original(history, monkeypatch, operation):
    kept = await save(history)
    await history.shutdown()
    restarted = ObicoHistory(history.root, history._session_factory)
    with monkeypatch.context() as patch:
        target = "_read" if operation == "read" else "inventory"
        patch.setattr(module, target, lambda *args: (_ for _ in ()).throw(PermissionError("sharing violation")))
        await restarted.start()
        async with restarted.session() as db:
            assert (await db.get(ObicoEvent, kept)).state == "ready"
        assert (history.root / filename(kept)).exists()
    await restarted.shutdown()
    retried = ObicoHistory(history.root, history._session_factory)
    await retried.start()
    try:
        assert await retried.details(1, kept, image=True) == jpeg()
    finally:
        await retried.shutdown()


async def test_unrelated_cleanup_failure_does_not_drop_after_successful_eviction(history, monkeypatch):
    first = await save(history, verdict="safe", timestamp=now() - timedelta(hours=1))
    second = await save(history, verdict="safe")
    size = len(jpeg())
    history.max_bytes = 2 * size + size // 2
    stuck = uuid4().hex + ".jpg"
    (history.root / stuck).write_bytes(b"x")
    history._files[stuck] = 1
    history._garbage_names.add(stuck)
    original = module._unlink

    def unlink(root, name):
        if name == stuck:
            raise PermissionError("unrelated orphan is locked")
        return original(root, name)

    monkeypatch.setattr(module, "_unlink", unlink)
    failure = await save(history, verdict="failure")
    assert await history.details(1, first) is None
    assert (await history.details(1, second))["state"] == "ready"
    assert (await history.details(1, failure))["state"] == "ready"
    assert not history.dropped.get("quota")
    assert history.status()["used_bytes"] == 2 * size + 1
    assert history.status()["reason"] == "cleanup_failed"


async def test_failed_victim_unlink_does_not_evict_more_frames(history, monkeypatch):
    first = await save(history, verdict="safe", timestamp=now() - timedelta(hours=1))
    second = await save(history, verdict="safe")
    history.max_bytes = 2 * len(jpeg())
    original = module._unlink

    def unlink(root, name):
        if name == filename(first):
            raise PermissionError("victim is locked")
        return original(root, name)

    monkeypatch.setattr(module, "_unlink", unlink)
    failure = await save(history, verdict="failure")
    assert await history.details(1, failure) is None
    assert (history.root / filename(first)).exists()
    assert (await history.details(1, second))["state"] == "ready"
    assert history.dropped["quota"] == 1
    assert history.status()["used_bytes"] == 2 * len(jpeg())


async def test_printer_delete_without_pin_unlinks_before_return(history):
    kept = await save(history, printer=2)
    removed = await save(history)
    async with history.printer_deletion(1), history.session() as db:
        await db.execute(delete(ObicoEvent).where(ObicoEvent.printer_id == 1))
        await db.execute(delete(Printer).where(Printer.id == 1))
        await db.commit()
    assert not (history.root / filename(removed)).exists()
    assert (history.root / filename(kept)).exists()
    assert history.status()["used_bytes"] == len(jpeg())
    assert history._count == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_p1_on_printer_delete_cleanup(history, monkeypatch, cancel):
    victim = await save(history)
    started, release = threading.Event(), threading.Event()
    original = module._unlink

    def blocked_unlink(root, name):
        if name == filename(victim):
            started.set()
            assert release.wait(5)
        original(root, name)

    monkeypatch.setattr(module, "_unlink", blocked_unlink)

    async def deleting_printer():
        async with history.printer_deletion(1), history.session() as db:
            await db.execute(delete(ObicoEvent).where(ObicoEvent.printer_id == 1))
            await db.execute(delete(Printer).where(Printer.id == 1))
            await db.commit()

    deleting = asyncio.create_task(deleting_printer())
    attempted, registered = asyncio.Event(), asyncio.Event()

    async def backup():
        attempted.set()
        async with history.pin_backup():
            registered.set()

    pinning = None
    try:
        assert await asyncio.to_thread(started.wait, 5)
        if cancel:
            deleting.cancel()
        pinning = asyncio.create_task(backup())
        await asyncio.wait_for(attempted.wait(), 5)
        locked_before_release = history.guard.locked()
        registered_early = registered.is_set()
        pins_before_release = history._pins
    finally:
        release.set()
        results = await asyncio.gather(deleting, *([pinning] if pinning else []), return_exceptions=True)
    assert locked_before_release and not registered_early
    assert pins_before_release == 0 and registered.is_set()
    assert not (history.root / filename(victim)).exists()
    if cancel:
        assert isinstance(results[0], asyncio.CancelledError) and not isinstance(results[1], BaseException)
    else:
        assert not any(isinstance(result, BaseException) for result in results)


@pytest.mark.parametrize("cancel", [False, True])
async def test_delete_error_rolls_back_sqlite_writer_before_store_guard(history, monkeypatch, cancel):
    from fastapi import HTTPException
    from sqlalchemy import update

    from backend.app.api.routes import printers as routes

    kept = await save(history)
    other = str(uuid4())
    writer_entered = asyncio.Event()
    engine = history._session_factory.kw["bind"]
    owner = None
    lock_states = []

    def before_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO OBICO_EVENTS"):
            writer_entered.set()

    event.listen(engine.sync_engine, "before_cursor_execute", before_insert)
    monkeypatch.setattr(module, "obico_history", history)
    try:
        async with history.session() as route_db:
            acquire = history.guard.acquire

            async def acquire_without_sqlite_writer():
                if asyncio.current_task() is owner:
                    lock_states.append(route_db.in_transaction())
                    assert not route_db.in_transaction(), "Request must rollback before waiting on store guard"
                return await acquire()

            monkeypatch.setattr(history.guard, "acquire", acquire_without_sqlite_writer)

            async def failed_delete(printer_id, delete_archives, user, db):
                await db.execute(update(Printer).where(Printer.id == 1).values(name="uncommitted"))
                assert history.offer(
                    printer_id=2,
                    token=history.token(2),
                    frame=jpeg(),
                    verdict="failure",
                    facts={},
                    detections=[],
                    event_id=other,
                    observed_at=now(),
                )
                await asyncio.wait_for(writer_entered.wait(), 5)
                assert history.guard.locked()  # writer owns it and is waiting for SQLite
                if cancel:
                    raise asyncio.CancelledError
                raise HTTPException(404, "Concurrent delete refused")

            monkeypatch.setattr(routes, "_delete_printer", failed_delete)
            owner = asyncio.create_task(routes.delete_printer(1, False, None, route_db))
            try:
                with pytest.raises(asyncio.CancelledError if cancel else HTTPException) as refused:
                    await asyncio.wait_for(owner, 5)
                if not cancel:
                    assert refused.value.status_code == 404
            finally:
                await route_db.rollback()
                await asyncio.wait_for(history._queue.join(), 5)
        assert lock_states and not any(lock_states)
        assert history._ready and history.status()["reason"] is None
        assert not history.dropped.get("write_failed")
        assert await history.details(2, other, image=True) == jpeg()
        assert await history.details(1, kept, image=True) == jpeg()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_insert)


async def test_restore_drains_printer_delete_body_without_holding_store_guard(history, monkeypatch):
    inside, release = asyncio.Event(), asyncio.Event()
    waiting = asyncio.Event()
    original_wait = history._deletions_idle.wait

    async def wait_for_deletions():
        waiting.set()
        await original_wait()

    monkeypatch.setattr(history._deletions_idle, "wait", wait_for_deletions)

    async def deleting():
        async with history.printer_deletion(1):
            inside.set()
            await release.wait()

    task = asyncio.create_task(deleting())
    try:
        await asyncio.wait_for(inside.wait(), 5)
        assert not history.guard.locked()
        assert not await history.quiesce(timeout=0)
        await asyncio.wait_for(waiting.wait(), 5)
        assert not history._drain.done()
        with pytest.raises(HistoryUnavailable):
            async with history.printer_deletion(2):
                pytest.fail("A new delete cannot enter during restore drain")
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert await history.quiesce(timeout=5)


async def test_oldest_and_saved_count_only_include_ready_frames(history):
    unavailable = await save(history, timestamp=now() - timedelta(days=3))
    writing = await save(history, timestamp=now() - timedelta(days=2))
    available = await save(history, timestamp=now() - timedelta(days=1))
    async with history.session() as db:
        (await db.get(ObicoEvent, unavailable)).state = "unavailable"
        (await db.get(ObicoEvent, writing)).state = "writing"
        ready_time = (await db.get(ObicoEvent, available)).observed_at
        await db.commit()
    page = await history.page(scope=None)
    assert page["total_count"] == 3 and page["saved_count"] == 1
    assert page["oldest_saved_at"] == module.iso(ready_time)
    async with history.session() as db:
        (await db.get(ObicoEvent, available)).state = "unavailable"
        await db.commit()
    page = await history.page(scope=None)
    assert page["saved_count"] == 0 and page["oldest_saved_at"] is None


async def test_portable_restore_roundtrip(history, tmp_path):
    from backend.app.core.config import Settings
    from backend.app.core.db_portable import _snapshot_sqlite, dump_to_sqlite, import_sqlite_to_postgres
    from backend.app.services.backup_files import FileRestore
    from backend.app.services.obico_history_files import stage_history, validate_history

    first = await save(history)
    second = await save(history, verdict="failure", frame=jpeg(color="blue"))
    engine = history._session_factory.kw["bind"]
    staging = tmp_path / "roundtrip"
    staging.mkdir()
    db_path = staging / "bamdude.db"
    async with history.pin_backup():
        await dump_to_sqlite(engine, database.Base.metadata, db_path)
        await module._file_work(stage_history, history.root.parent, staging, db_path)
    validate_history(staging, db_path)
    await history.clear()
    await history.shutdown()
    settings = Settings(_env_file=None, data_dir=history.root.parent)
    files = FileRestore(staging, settings, history.root.parent)
    await engine.dispose()
    try:
        await module._file_work(files.prepare)
        await module._file_work(files.apply)
        if engine.dialect.name == "sqlite":
            await module._file_work(_snapshot_sqlite, db_path, engine.url.database)
        else:
            await import_sqlite_to_postgres(engine, database.Base.metadata, db_path)
        files.committed = True
    finally:
        await module._file_work(files.finish)
    restored = ObicoHistory(history.root, history._session_factory)
    await restored.start()
    try:
        assert (await restored.page(scope=frozenset()))["items"] == []
        assert await restored.details(1, first, image=True) == jpeg()
        assert await restored.details(1, second, image=True) == jpeg(color="blue")
        assert restored._count == 2 and restored.status()["used_bytes"] == len(jpeg()) + len(jpeg(color="blue"))
    finally:
        await restored.shutdown()
