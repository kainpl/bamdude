"""A usage session belongs to the execution its start bound, never to a name (GH #115, Codex USN-1).

After a power cycle an A1 mini (firmware 01.08.01.00) reports ``subtask_name`` as the
file name -- ``LID_600_10mm_x10.3mf`` -- where it echoed the ``LID_600_10mm_x10`` BamDude
sent. Printers fold spaces into underscores while an archive keeps the uploaded name.
Two different prints can share a folded, truncated or legacy name. So no name, raw or
normalised, can say which print a persisted session (dispatched mapping, assignment
snapshot, tray chronology) belongs to.

The session carries the ``recovery_identity`` of the archive its start bound
(``active_print_sessions.archive_identity``, m221):

- a restart restores a stamped session only when the running print resolves to that very archive;
- a reconcile-driven completion lends only the closing archive's own session;
- the reconcile deletes only that session's row.

A session this process made for a start no archive has claimed is never lent. Only an
unstamped row an earlier version wrote keeps the old rule, its exact recorded name.
"""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import backend.app.models.printer_location  # noqa: F401 -- resolves Printer's relationship
from backend.app.models.active_print_session import ActivePrintSession
from backend.app.models.archive import PrintArchive

pytestmark = pytest.mark.integration


@pytest.fixture
def main_sessions(monkeypatch, test_engine):
    """Sessions ``main`` opens itself (the live-archive resolver, the stamp) use the test database."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    return factory


@pytest.fixture(autouse=True)
def _no_leaked_sessions():
    from backend.app.services import usage_tracker

    yield
    usage_tracker._active_sessions.clear()


async def _archive(db, printer_id, *, print_name, filename, status="printing", subtask_id=None) -> PrintArchive:
    archive = PrintArchive(
        printer_id=printer_id,
        filename=filename,
        print_name=print_name,
        file_path=f"x/{filename}",
        file_size=1,
        status=status,
        subtask_id=subtask_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


async def _row(db, printer_id, *, print_name, owner=None, started_at=None, log=None) -> ActivePrintSession:
    row = ActivePrintSession(
        printer_id=printer_id,
        print_name=print_name,
        started_at=started_at or datetime(2026, 11, 10, 13, 58, 10, 578000),
        tray_now_at_start=1,
        tray_change_log=log,
        archive_identity=owner,
    )
    db.add(row)
    await db.commit()
    return row


async def _persisted(db, printer_id):
    return await db.get(ActivePrintSession, printer_id, populate_existing=True)


def _state(subtask_name, subtask_id=None):
    return SimpleNamespace(
        subtask_name=subtask_name,
        subtask_id=subtask_id,
        tray_change_log=[],
        tray_now=3,
        layer_num=30,
        last_loaded_tray=-1,
    )


async def _restore(printer_id, state, db):
    from backend.app.main import _restore_usage_tracking_session

    return await _restore_usage_tracking_session(printer_id, state, db, logging.getLogger(__name__))


# ── the restart restore ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "running",
    [("LID_600_10mm_x10.3mf", "1225230217"), ("LID_600_10mm_x10.3mf", None), ("LID_600_10mm_x10", "0")],
)
async def test_a_restart_restores_its_executions_session_whatever_the_printer_calls_it(
    db_session, printer_factory, main_sessions, running
):
    """GH #115: the running print resolves -- by its firmware id, or as the one printing row its name fits
    -- to the archive the session is stamped with."""
    printer = await printer_factory()
    lid = await _archive(
        db_session,
        printer.id,
        print_name="LID 600 10mm x10",
        filename="LID 600 10mm x10.gcode.3mf",
        subtask_id="1225230217",
    )
    await _row(
        db_session, printer.id, print_name="LID_600_10mm_x10", owner=lid.recovery_identity, log=[[1, 0], [2, 25]]
    )
    state = _state(*running)

    assert await _restore(printer.id, state, db_session)

    assert await _persisted(db_session, printer.id) is not None
    assert state.tray_change_log == [(1, 0), (2, 25)]


async def test_a_restart_never_lends_another_executions_session_under_a_colliding_name(
    db_session, printer_factory, main_sessions
):
    """Codex USN-1: A (``Part Left``) ended, B (``Part_Left``) runs now. A's leaked session must not reach B."""
    printer = await printer_factory()
    left = await _archive(
        db_session, printer.id, print_name="Part Left", filename="Part Left.gcode.3mf", status="completed"
    )
    await _archive(db_session, printer.id, print_name="Part_Left", filename="Part_Left.gcode.3mf")
    await _row(db_session, printer.id, print_name="Part Left", owner=left.recovery_identity, log=[[1, 0], [2, 20]])
    state = _state("Part_Left")

    assert await _restore(printer.id, state, db_session)

    assert await _persisted(db_session, printer.id) is None
    assert state.tray_change_log == [(3, 30)], "seeded from the tray feeding B now, not A's chronology"


async def test_a_restart_that_cannot_resolve_the_running_print_lends_a_stamped_session_nothing(
    db_session, printer_factory, main_sessions
):
    """Two printing rows fit the name and the firmware gives no id: no address, so no proof -- even for the
    session's exact recorded name."""
    printer = await printer_factory()
    first = await _archive(db_session, printer.id, print_name="Bracket", filename="Bracket.gcode.3mf")
    await _archive(db_session, printer.id, print_name="Bracket", filename="Bracket.gcode.3mf")
    await _row(db_session, printer.id, print_name="Bracket", owner=first.recovery_identity, log=[[1, 0]])
    state = _state("Bracket")

    assert await _restore(printer.id, state, db_session)

    assert await _persisted(db_session, printer.id) is None
    assert state.tray_change_log == [(3, 30)]


@pytest.mark.parametrize(("running", "kept"), [("LID_600_10mm_x10", True), ("LID_600_10mm_x10.3mf", False)])
async def test_an_unstamped_row_keeps_the_exact_name_rule(db_session, printer_factory, main_sessions, running, kept):
    """A row an earlier version wrote proves no execution: only its exact recorded name, as before."""
    printer = await printer_factory()
    await _row(db_session, printer.id, print_name="LID_600_10mm_x10", log=[[1, 0], [2, 25]])
    state = _state(running)

    assert await _restore(printer.id, state, db_session)

    assert (await _persisted(db_session, printer.id) is not None) is kept
    assert state.tray_change_log == ([(1, 0), (2, 25)] if kept else [(3, 30)])


# ── the reconcile-driven completion ─────────────────────────────────────────


class _ReachedTheBooks(Exception):
    """Raised by the 3MF consumer stub: the context it was handed is the observation."""


def _session(printer_id, print_name, *, owner=None, observed_here=False):
    from backend.app.services import usage_tracker as ut

    return ut.PrintSession(
        printer_id=printer_id,
        print_name=print_name,
        started_at=datetime.now(timezone.utc),
        spool_assignments={(0, 3): 987},
        ams_mapping=[3],
        archive_identity=owner,
        observed_here=observed_here,
    )


async def _close(db, archive, session):
    """Run the tracker's reconcile-driven completion of ``archive`` up to the books. Returns what reached them."""
    from backend.app.services import usage_tracker as ut

    ut._active_sessions[archive.printer_id] = session
    pm = MagicMock()
    pm.get_status.return_value = None
    consumer = AsyncMock(side_effect=_ReachedTheBooks)
    with (
        patch.object(ut, "restore_session", new_callable=AsyncMock),
        patch.object(ut, "_track_from_3mf", consumer),
        pytest.raises(_ReachedTheBooks),
    ):
        await ut.on_print_complete(
            archive.printer_id,
            {"status": "completed"},
            pm,
            db,
            archive_id=archive.id,
            expected_print_name=archive.print_name,
            archive_snapshot=archive,
            expected_identity=archive.recovery_identity,
        )
    kwargs = consumer.await_args.kwargs
    return kwargs["ams_mapping"], kwargs["spool_assignments"], ut._active_sessions.get(archive.printer_id) is session


@pytest.mark.parametrize(
    ("session_name", "print_name", "filename"),
    [
        ("LID_600_10mm_x10", "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf"),
        ("LID_600_10mm_x10.3mf", "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf"),
        # A multi-plate archive's print_name carries " - Plate N"; the stamp needs no name at all.
        ("Bracket", "Bracket - Plate 2", "Bracket.gcode.3mf"),
    ],
)
async def test_a_reconcile_close_lends_its_own_session_whatever_the_names(
    db_session, printer_factory, session_name, print_name, filename
):
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name=print_name, filename=filename)
    session = _session(printer.id, session_name, owner=archive.recovery_identity, observed_here=True)

    mapping, assignments, put_back = await _close(db_session, archive, session)

    assert (mapping, assignments, put_back) == ([3], {(0, 3): 987}, False)


@pytest.mark.parametrize(
    ("session_name", "print_name", "filename"),
    [
        ("Part_Left", "Part Left", "Part Left.gcode.3mf"),  # space/underscore folding
        ("shared_long_prefix...", "shared_long_prefix_left", "shared_long_prefix_left.gcode.3mf"),  # truncation
        ("gear.mount", "gear.gcode.mount", "gear.gcode.mount.gcode.3mf"),  # the lossy legacy label
        ("Part Left", "Part Left", "Part Left.gcode.3mf"),  # even an exact name
    ],
)
async def test_a_reconcile_close_never_lends_another_executions_session(
    db_session, printer_factory, session_name, print_name, filename
):
    """Codex USN-1: B's stamped session, whatever its name, never books A."""
    printer = await printer_factory()
    a = await _archive(db_session, printer.id, print_name=print_name, filename=filename)
    b = await _archive(db_session, printer.id, print_name=session_name, filename=f"{session_name}.gcode.3mf")
    session = _session(printer.id, session_name, owner=b.recovery_identity, observed_here=True)

    mapping, assignments, put_back = await _close(db_session, a, session)

    assert (mapping, assignments, put_back) == (None, None, True)


async def test_an_unclaimed_session_of_this_process_is_never_lent(db_session, printer_factory):
    """A start this process saw, whose archive has not stamped its session yet, proves nothing."""
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name="Overnight_Job", filename="job.gcode.3mf")
    session = _session(printer.id, "Overnight_Job", observed_here=True)

    assert await _close(db_session, archive, session) == (None, None, True)


@pytest.mark.parametrize(("session_name", "lent"), [("Overnight_Job", True), ("Overnight_Job.3mf", False)])
async def test_a_restored_unstamped_session_keeps_the_exact_name_rule(db_session, printer_factory, session_name, lent):
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name="Overnight_Job", filename="job.gcode.3mf")
    session = _session(printer.id, session_name)

    mapping, _assignments, put_back = await _close(db_session, archive, session)

    assert (mapping == [3], put_back) == (lent, not lent)


# ── the reconcile's cleanup ─────────────────────────────────────────────────


async def _reconcile(db, archive):
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock):
        await _reconcile_complete_archive(db, archive, status="completed", uncertain=False)


async def test_a_reconcile_close_deletes_its_own_sessions_row(db_session, printer_factory):
    printer = await printer_factory()
    archive = await _archive(
        db_session, printer.id, print_name="LID 600 10mm x10", filename="LID 600 10mm x10.gcode.3mf"
    )
    await _row(db_session, printer.id, print_name="LID_600_10mm_x10.3mf", owner=archive.recovery_identity)

    await _reconcile(db_session, archive)

    assert await _persisted(db_session, printer.id) is None


@pytest.mark.parametrize("newer_owner", ["b", None])
async def test_a_reconcile_close_leaves_a_newer_prints_row(db_session, printer_factory, newer_owner):
    """Codex USN-1: B started during A's sweep; its row (stamped, or not yet claimed) survives A's close,
    even under A's exact name."""
    printer = await printer_factory()
    a = await _archive(db_session, printer.id, print_name="Part Left", filename="Part Left.gcode.3mf")
    b = await _archive(db_session, printer.id, print_name="Part Left", filename="Part Left.gcode.3mf")
    await _row(
        db_session,
        printer.id,
        print_name="Part Left",
        owner=b.recovery_identity if newer_owner else None,
        started_at=datetime(2026, 11, 11, 10, 26, 31),
    )

    await _reconcile(db_session, a)

    assert await _persisted(db_session, printer.id) is not None


# ── the stamp ───────────────────────────────────────────────────────────────


async def test_the_stamp_claims_only_the_session_the_start_made(db_session, printer_factory):
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    started = datetime.now(timezone.utc)
    session = _session(printer.id, "Plate_3", observed_here=True)
    session.started_at = started
    ut._active_sessions[printer.id] = session
    await _row(db_session, printer.id, print_name="Plate_3", started_at=started.replace(tzinfo=None))

    assert await ut.claim_session(db_session, printer.id, started_at=started, archive_identity="uuid-a")
    # The claim is once: a second archive never takes it over.
    assert not await ut.claim_session(db_session, printer.id, started_at=started, archive_identity="uuid-b")

    assert session.archive_identity == "uuid-a"
    assert (await _persisted(db_session, printer.id)).archive_identity == "uuid-a"


async def test_the_stamp_never_reaches_a_newer_starts_session(db_session, printer_factory):
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    older = datetime.now(timezone.utc) - timedelta(minutes=5)
    newer = _session(printer.id, "Next", observed_here=True)
    ut._active_sessions[printer.id] = newer
    await _row(db_session, printer.id, print_name="Next", started_at=newer.started_at.replace(tzinfo=None))

    assert not await ut.claim_session(db_session, printer.id, started_at=older, archive_identity="uuid-old")

    assert newer.archive_identity is None
    assert (await _persisted(db_session, printer.id)).archive_identity is None


async def test_a_persisted_stamp_survives_the_round_trip(db_session, printer_factory):
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    session = _session(printer.id, "Plate_3", owner="uuid-a", observed_here=True)
    await ut.persist_session(db_session, session)
    ut._active_sessions.clear()

    await ut.restore_session(db_session, printer.id)

    restored = ut._active_sessions[printer.id]
    assert (restored.archive_identity, restored.observed_here) == ("uuid-a", False)


# ── the start path stamps what it binds ─────────────────────────────────────


@pytest.fixture
def _no_active_prints():
    from backend.app.main import _active_prints

    _active_prints.clear()
    yield
    _active_prints.clear()


async def _start_with_session(db_session, test_engine, tmp_path, monkeypatch, printer, started):
    from backend.tests.integration.test_archive_row_at_print_start import _drive_print_start, _Recorder

    src = tmp_path / "Plate_3.gcode.3mf"
    from backend.tests.integration.test_archive_row_at_print_start import _write_3mf

    _write_3mf(src)
    with patch("backend.app.services.usage_tracker.on_print_start", AsyncMock(return_value=started)):
        return await _drive_print_start(
            db_session=db_session,
            test_engine=test_engine,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            printer=printer,
            download_result=(src, "Plate_3.gcode.3mf"),
            recorder=_Recorder(),
        )


async def test_a_new_external_start_stamps_its_session_with_its_archive(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory, _no_active_prints
):
    from backend.tests.integration.test_archive_row_at_print_start import _prepare_printer

    printer = await _prepare_printer(printer_factory)
    printer_id = printer.id
    started = datetime.now(timezone.utc)
    await _row(db_session, printer_id, print_name="Plate_3", started_at=started.replace(tzinfo=None))

    rows = await _start_with_session(db_session, test_engine, tmp_path, monkeypatch, printer, started)
    created = [r.recovery_identity for r in rows]

    assert len(created) == 1
    assert (await _persisted(db_session, printer_id)).archive_identity == created[0]


async def test_our_own_dispatch_stamps_its_session_with_the_dispatched_archive(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory, _no_active_prints
):
    from backend.app import main
    from backend.tests.integration.test_archive_row_at_print_start import _prepare_printer

    printer = await _prepare_printer(printer_factory)
    ours = await _archive(
        db_session, printer.id, print_name="Plate_3", filename="Plate_3.gcode.3mf", subtask_id="sub-42"
    )
    ours.dispatch_phase = "sent"
    await db_session.commit()
    ours_id, ours_identity, printer_id = ours.id, ours.recovery_identity, printer.id
    monkeypatch.setitem(main._expected_prints, (printer_id, "Plate_3"), ours_id)
    started = datetime.now(timezone.utc)
    await _row(db_session, printer_id, print_name="Plate_3", started_at=started.replace(tzinfo=None))

    rows = await _start_with_session(db_session, test_engine, tmp_path, monkeypatch, printer, started)
    ids = [r.id for r in rows]

    assert ids == [ours_id]
    assert (await _persisted(db_session, printer_id)).archive_identity == ours_identity


async def test_a_start_whose_session_was_replaced_stamps_nothing(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory, _no_active_prints
):
    """The row on disk is a newer start's (another ``started_at``): the older start's archive never claims it."""
    from backend.tests.integration.test_archive_row_at_print_start import _prepare_printer

    printer = await _prepare_printer(printer_factory)
    printer_id = printer.id
    started = datetime.now(timezone.utc)
    await _row(
        db_session, printer_id, print_name="Plate_3", started_at=(started + timedelta(seconds=5)).replace(tzinfo=None)
    )

    rows = await _start_with_session(db_session, test_engine, tmp_path, monkeypatch, printer, started)

    assert len(rows) == 1
    assert (await _persisted(db_session, printer_id)).archive_identity is None
