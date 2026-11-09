"""Run the same state machines on separate file-backed SQLite connections."""

import asyncio
from collections import defaultdict

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.print_claims import observe_claim
from backend.app.services.queue_ops import claim_lanes
from backend.tests.integration.test_duplicate_claim_repair import (  # noqa: F401
    telemetry,
    test_group_apply_refuses_changes_and_rolls_back_whole_set,
    test_group_limit_is_complete_and_independent_of_issue_page,
    test_settlement_then_whole_group_cancel_and_replay,
)
from backend.tests.integration.test_duplicate_print_execution import (  # noqa: F401
    setup,
    test_db_difference_cannot_promote_unproven_client,
    test_duplicate_claims_return_unresolved_without_third_row,
    test_first_commit_closes_only_noncurrent_claim,
    test_real_client_unproven_db_difference_never_enters_new_start,
    test_reconnect_boundary_publishes_and_retires_before_ftp,
    test_replayed_noncurrent_marker_never_books_and_closes_own_claims,
)
from backend.tests.integration.test_queue_auto_transfer import test_engine  # noqa: F401


@pytest.mark.asyncio
async def test_waiting_claim_reads_after_the_real_database_writer(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """Remove process mutexes: BEGIN IMMEDIATE / advisory locks must serialize."""
    from backend.app.services import queue_ops

    class NoProcessLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def locked(self):
            return False

    monkeypatch.setattr(queue_ops, "_queue_claim_locks", defaultdict(NoProcessLock))
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    await db_session.commit()
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    attempted, acquired = asyncio.Event(), asyncio.Event()

    async def contender():
        async with sessions() as db:
            attempted.set()
            async with claim_lanes(db, [q.id]):
                acquired.set()
                result = await observe_claim(db, p.id, archive_id=a.id)
                await db.commit()
                return result

    async with sessions() as holder, claim_lanes(holder, [q.id]):
        # The second row is invisible until the waiting writer owns its lock.
        holder.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing"))
        await holder.flush()
        task = asyncio.create_task(contender())
        await asyncio.wait_for(attempted.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(acquired.wait(), 0.15)
        await holder.commit()
    result = await asyncio.wait_for(task, 10)
    assert result.state == "unresolved"
    await db_session.refresh(q)
    assert q.is_paused


@pytest.mark.asyncio
async def test_sqlite_reissued_archive_id_cannot_receive_old_terminal_or_download(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, tmp_path
):
    from backend.app import main
    from backend.app.models.archive import PrintArchive
    from backend.app.services.archive import ArchiveService
    from backend.app.services.print_run_binding import PrintRunBinding

    if test_engine.dialect.name != "sqlite":
        pytest.skip("This test proves actual SQLite rowid reuse")
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    old_id, old_identity, old_item = a.id, a.recovery_identity, item.id
    q.current_item_id = None
    await db_session.delete(item)
    await db_session.delete(a)
    await db_session.commit()
    new = await archive_factory(printer_id=p.id, queue_id=q.id, status="printing", subtask_id="100")
    assert new.id == old_id and new.recovery_identity != old_identity
    row = PrintQueueItem(queue_id=q.id, archive_id=new.id, status="printing")
    db_session.add(row)
    await db_session.commit()
    assert row.id == old_item
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    binding = PrintRunBinding(p.id, old_id, queue_item_id=old_item, archive_identity=old_identity)
    assert await main._accept_bound_terminal_run(p.id, binding, {"status": "completed"}) is None
    assert (
        await observe_claim(db_session, p.id, archive_id=old_id, expected_identity=old_identity)
    ).state == "unresolved"
    service = ArchiveService(db_session)
    assert not await service.mark_3mf_unavailable(old_id, expected_identity=old_identity)
    assert not await service.attach_3mf_to_archive(
        old_id, tmp_path / "absent.3mf", "x.3mf", expected_identity=old_identity
    )
    await db_session.refresh(new)
    await db_session.refresh(row)
    assert new.status == row.status == "printing"
    assert "bamdude_terminal_acceptance" not in (new.extra_data or {})
    assert "no_3mf_available" not in (new.extra_data or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["energy", "ams", "usage", "timelapse", "converter"])
async def test_reissued_archive_id_rejects_background_consumers(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, tmp_path, consumer
):
    from types import SimpleNamespace

    from backend.app import main
    from backend.app.core.config import settings
    from backend.app.models.archive import PrintArchive
    from backend.app.services import print_file_analysis, usage_tracker
    from backend.app.services.archive import ArchiveService

    if test_engine.dialect.name != "sqlite":
        pytest.skip("Actual SQLite id reissue")
    p = await printer_factory()
    old = await archive_factory(printer_id=p.id, status="completed", file_path="old.3mf")
    aid, identity = old.id, old.recovery_identity
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
    replacements = []

    async def reuse():
        await db_session.delete(old)
        await db_session.commit()
        new = await archive_factory(printer_id=p.id, status="printing", file_path="new.3mf")
        assert new.id == aid and new.recovery_identity != identity
        replacements.append(new)
        return new

    if consumer == "timelapse":
        original = asyncio.to_thread

        async def barrier(fn, *args, **kwargs):
            if getattr(fn, "__name__", "") == "write_bytes":
                await reuse()
            return await original(fn, *args, **kwargs)

        monkeypatch.setattr(asyncio, "to_thread", barrier)
        async with factory() as db:
            assert not await ArchiveService(db).attach_timelapse(aid, b"old video", expected_identity=identity)
    elif consumer == "converter":
        from unittest.mock import AsyncMock

        from backend.app.services.archive import _convert_timelapse_to_mp4

        monkeypatch.setattr("backend.app.core.database.async_session", factory)
        monkeypatch.setattr("backend.app.services.camera.get_ffmpeg_path", lambda: "ffmpeg")
        source = tmp_path / "old-video.avi"
        source.write_bytes(b"old")

        async def finish():
            await reuse()
            source.with_suffix(".mp4").write_bytes(b"converted")
            return b"", b""

        process = SimpleNamespace(returncode=0, communicate=finish)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        await _convert_timelapse_to_mp4(aid, source, expected_identity=identity)
        assert source.exists()  # Refusal must not delete the original.
    elif consumer == "usage":
        from backend.app.models.spool import Spool
        from backend.app.models.spool_assignment import SpoolAssignment

        spool = Spool(material="PLA", label_weight=1000, weight_used=0)
        db_session.add(spool)
        await db_session.flush()
        db_session.add(SpoolAssignment(printer_id=p.id, spool_id=spool.id, ams_id=0, tray_id=0))
        await db_session.commit()

        async def analysis(*args, **kwargs):
            await reuse()
            return print_file_analysis.PrintFileAnalysis(
                [{"slot_id": 1, "used_g": 5, "type": "PLA", "color": "#FFFFFF"}], None, {}
            )

        monkeypatch.setattr(print_file_analysis, "get_print_file_analysis", analysis)
        manager = SimpleNamespace(get_status=lambda pid: None)
        async with factory() as db:
            assert (
                await usage_tracker.on_print_complete(
                    p.id,
                    {"status": "completed"},
                    manager,
                    db,
                    archive_id=aid,
                    expected_identity=identity,
                    ams_mapping=[0],
                )
                == []
            )
    else:
        new = await reuse()
        if consumer == "energy":
            async with factory() as db:
                await main._write_print_energy(
                    db,
                    aid,
                    kind="metered",
                    kwh=1.5,
                    cost_per_kwh=1,
                    approximate=False,
                    record={"final": True},
                    expected_identity=identity,
                )
        else:
            monkeypatch.setitem(main._print_ams_mappings, aid, [3])
            monkeypatch.setitem(main._print_ams_mapping_identities, aid, identity)
            assert main._get_start_ams_mapping({}, aid, new.recovery_identity) is None
    if consumer == "usage":
        await db_session.refresh(spool)
        assert spool.weight_used == 0
    new = replacements[0]
    await db_session.refresh(new)
    assert new.timelapse_path is None
    assert new.energy_kwh is None
    assert new.extra_data is None or not new.extra_data


@pytest.mark.asyncio
async def test_analysis_cache_and_release_require_the_archive_incarnation(tmp_path):
    from types import SimpleNamespace

    from backend.app.services import print_file_analysis as analysis
    from backend.app.services.print_run_binding import PrintRunBinding

    manager = SimpleNamespace()
    path = tmp_path / "same.3mf"
    path.write_bytes(b"same bytes")
    manager._print_run_bindings = {1: PrintRunBinding(1, 1, archive_identity="old")}
    analysis.bind_print_file_analysis(manager, 1, 1, path, 1, expected_identity="old")
    old = manager._print_file_analysis_contexts[1]
    manager._print_run_bindings[1] = PrintRunBinding(1, 1, archive_identity="new")
    analysis.bind_print_file_analysis(manager, 1, 1, path, 1, expected_identity="new")
    current = manager._print_file_analysis_contexts[1]
    assert current is not old and old.lifecycle == "retired"
    assert await analysis.get_print_file_analysis(manager, 1, 1, path, 1, expected_identity="old") is None
    analysis.discard_print_file_analysis(manager, 1, 1, expected_identity="old")
    assert manager._print_file_analysis_contexts[1] is current
    from backend.app.services.print_run_binding import discard_print_run

    discard_print_run(manager, 1, 1, expected_identity="old")
    assert manager._print_run_bindings[1].archive_identity == "new"


def test_old_alias_cleanup_preserves_reissued_archive_registration(monkeypatch):
    from types import SimpleNamespace

    from backend.app import main

    monkeypatch.setattr(main, "_active_prints", {})
    monkeypatch.setattr(main, "_active_print_identities", {})
    key = (1, "same.3mf")
    main._remember_active_print(key, SimpleNamespace(id=1, recovery_identity="old"))
    main._remember_active_print(key, SimpleNamespace(id=1, recovery_identity="new"))
    main._forget_active_print(1, "old")
    assert main._active_prints[key] == 1
    assert main._active_print_identities[key] == "new"
