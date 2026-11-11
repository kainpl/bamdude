"""A usage session belongs to the execution its start bound, never to a name (GH #115, Codex USN-1/USO).

After a power cycle an A1 mini (firmware 01.08.01.00) reports ``subtask_name`` as the
file name -- ``LID_600_10mm_x10.3mf`` -- where it echoed the ``LID_600_10mm_x10`` BamDude
sent. Printers fold spaces into underscores while an archive keeps the uploaded name.
Two different prints can share a folded, truncated, legacy or even exact name. So no name
can say which print a persisted session (dispatched mapping, assignment snapshot, tray
chronology) belongs to.

The session carries the ``recovery_identity`` of the archive its start bound
(``active_print_sessions.archive_identity``, m221), and every row an owner-aware start
writes says so (``owner_tracked``).

- **Restart restore.** A stamped session is restored only when the running print resolves to that very
  archive. A stamped or unclaimed row that is not this print's is left to its owner. Each decision acts only
  on the row it read, and a row replaced meanwhile is decided again.
- **Reconcile-driven completion.** It lends only the closing archive's own session, whether or not that
  archive has a name. A session read from the row never enters memory on refusal.
- **Reconcile cleanup.** It deletes only that session's row.
- **Unclaimed starts.** A start no archive has claimed is never lent, Spoolman mode included.
- **Pre-m221 rows.** Only those keep the old rule, their exact recorded name.
"""

import logging
from contextlib import ExitStack
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
        file_path=f"x/{filename or 'unnamed'}",
        file_size=1,
        status=status,
        subtask_id=subtask_id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


T0 = datetime(2026, 11, 10, 13, 58, 10, 578000)


def _row_values(printer_id, *, print_name, owner=None, tracked=None, started_at=None, log=None):
    return {
        "printer_id": printer_id,
        "print_name": print_name,
        "started_at": started_at or T0,
        "tray_now_at_start": 1,
        "tray_change_log": log,
        "archive_identity": owner,
        # A stamped row was written by an owner-aware start; tracked=None alone is a pre-m221 row.
        "owner_tracked": tracked if tracked is not None else (True if owner else None),
    }


async def _row(db, printer_id, **kwargs) -> ActivePrintSession:
    row = ActivePrintSession(**_row_values(printer_id, **kwargs))
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


async def test_a_restart_never_lends_another_executions_session_and_leaves_it_to_its_owner(
    db_session, printer_factory, main_sessions
):
    """Codex USN-1: A (``Part Left``) ended, B (``Part_Left``) runs now. A's session reaches neither B nor the
    bin: A's reconcile books with it."""
    printer = await printer_factory()
    left = await _archive(
        db_session, printer.id, print_name="Part Left", filename="Part Left.gcode.3mf", status="completed"
    )
    await _archive(db_session, printer.id, print_name="Part_Left", filename="Part_Left.gcode.3mf")
    await _row(db_session, printer.id, print_name="Part Left", owner=left.recovery_identity, log=[[1, 0], [2, 20]])
    state = _state("Part_Left")

    assert await _restore(printer.id, state, db_session)

    assert (await _persisted(db_session, printer.id)).archive_identity == left.recovery_identity
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

    assert await _persisted(db_session, printer.id) is not None
    assert state.tray_change_log == [(3, 30)]


async def test_a_restart_never_restores_an_unclaimed_start(db_session, printer_factory, main_sessions):
    """An owner-aware start no archive claimed proves nothing, even under the exact running name."""
    printer = await printer_factory()
    await _row(db_session, printer.id, print_name="Bracket", tracked=True, log=[[1, 0]])
    state = _state("Bracket")

    assert await _restore(printer.id, state, db_session)

    assert await _persisted(db_session, printer.id) is not None
    assert state.tray_change_log == [(3, 30)]


@pytest.mark.parametrize(("running", "kept"), [("LID_600_10mm_x10", True), ("LID_600_10mm_x10.3mf", False)])
async def test_a_pre_m221_row_keeps_the_exact_name_rule(db_session, printer_factory, main_sessions, running, kept):
    """A row an earlier version wrote proves no execution: only its exact recorded name, as before."""
    printer = await printer_factory()
    await _row(db_session, printer.id, print_name="LID_600_10mm_x10", log=[[1, 0], [2, 25]])
    state = _state(running)

    assert await _restore(printer.id, state, db_session)

    assert (await _persisted(db_session, printer.id) is not None) is kept
    assert state.tray_change_log == ([(1, 0), (2, 25)] if kept else [(3, 30)])


async def test_a_session_written_during_the_resolvers_await_survives_and_is_the_one_decided(
    db_session, printer_factory, main_sessions
):
    """Codex USO-1: A ended and B is printing, both named ``Same``. The row is A's when read; while the
    running print is resolved, B's start writes its own session there. B's row must survive with its own
    data, and it is B's session that the decision -- made again on the fresh row -- restores."""
    from backend.app import main

    printer = await printer_factory()
    a = await _archive(db_session, printer.id, print_name="Same", filename="Same.gcode.3mf", status="completed")
    b = await _archive(db_session, printer.id, print_name="Same", filename="Same.gcode.3mf")
    a_identity, b_identity, b_id = a.recovery_identity, b.recovery_identity, b.id
    await _row(db_session, printer.id, print_name="Same", owner=a_identity, log=[[1, 0]])
    b_started = T0 + timedelta(hours=2)
    real = main._live_archive_for_running_print
    written = []

    async def resolve(printer_id, data):
        if not written:
            async with main_sessions() as other:
                row = await other.get(ActivePrintSession, printer_id)
                for key, value in _row_values(
                    printer_id, print_name="Same", owner=b_identity, started_at=b_started, log=[[2, 0], [3, 7]]
                ).items():
                    setattr(row, key, value)
                await other.commit()
            written.append(True)
        return await real(printer_id, data)

    state = _state("Same")
    with patch.object(main, "_live_archive_for_running_print", side_effect=resolve):
        assert await _restore(printer.id, state, db_session)

    row = await _persisted(db_session, printer.id)
    assert (row.archive_identity, row.started_at, row.tray_change_log) == (b_identity, b_started, [[2, 0], [3, 7]])
    assert state.tray_change_log == [(2, 0), (3, 7)], f"B's session (archive {b_id}) is what was restored"


async def test_a_pre_m221_discard_never_deletes_a_row_written_after_its_read(
    db_session, printer_factory, main_sessions, monkeypatch
):
    """Codex USO-1, the discard: A's old row does not match the running print. B's row lands between the
    read and the delete. The delete addresses A's read and leaves B's row; B is decided again and restored."""
    from backend.app.services import usage_tracker

    printer = await printer_factory()
    await _row(db_session, printer.id, print_name="Old", log=[[1, 0]])
    real = usage_tracker.read_persisted_snapshot
    reads = []

    async def read_then_replace(db, printer_id):
        snapshot = await real(db, printer_id)
        reads.append(snapshot)
        if len(reads) == 1:
            async with main_sessions() as other:
                row = await other.get(ActivePrintSession, printer_id)
                for key, value in _row_values(
                    printer_id, print_name="New", started_at=T0 + timedelta(hours=1), log=[[2, 4]]
                ).items():
                    setattr(row, key, value)
                await other.commit()
        return snapshot

    monkeypatch.setattr(usage_tracker, "read_persisted_snapshot", read_then_replace)
    state = _state("New")

    assert await _restore(printer.id, state, db_session)

    row = await _persisted(db_session, printer.id)
    assert (row.print_name, row.tray_change_log) == ("New", [[2, 4]])
    assert state.tray_change_log == [(2, 4)]


# ── the reconcile-driven completion ─────────────────────────────────────────


class _ReachedTheBooks(Exception):
    """Raised by the 3MF consumer stub: the context it was handed is the observation."""


def _session(printer_id, print_name, *, owner=None, tracked=False):
    from backend.app.services import usage_tracker as ut

    return ut.PrintSession(
        printer_id=printer_id,
        print_name=print_name,
        started_at=datetime.now(timezone.utc),
        spool_assignments={(0, 3): 987},
        ams_mapping=[3],
        archive_identity=owner,
        owner_tracked=tracked,
    )


async def _close(db, archive, session=None, *, restore=False, owner_only=True):
    """Run the reconcile's completion of ``archive`` up to the books, as the reconcile calls it.

    ``session`` is the in-memory one; with ``restore`` the persisted row is restored for real instead.
    Returns what reached the books and whether a session is in memory afterwards.
    """
    from backend.app.services import usage_tracker as ut

    if session is not None:
        ut._active_sessions[archive.printer_id] = session
    pm = MagicMock()
    pm.get_status.return_value = None
    consumer = AsyncMock(side_effect=_ReachedTheBooks)
    with ExitStack() as stack:
        stack.enter_context(patch.object(ut, "_track_from_3mf", consumer))
        if not restore:
            stack.enter_context(patch.object(ut, "restore_session", new_callable=AsyncMock))
        with pytest.raises(_ReachedTheBooks):
            await ut.on_print_complete(
                archive.printer_id,
                {"status": "completed"},
                pm,
                db,
                archive_id=archive.id,
                expected_print_name=archive.print_name,
                archive_snapshot=archive,
                expected_identity=archive.recovery_identity,
                owner_only=owner_only,
            )
    kwargs = consumer.await_args.kwargs
    return kwargs["ams_mapping"], kwargs["spool_assignments"], ut._active_sessions.get(archive.printer_id)


@pytest.mark.parametrize(
    ("session_name", "print_name", "filename"),
    [
        ("LID_600_10mm_x10", "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf"),
        ("LID_600_10mm_x10.3mf", "LID 600 10mm x10", "LID 600 10mm x10.gcode.3mf"),
        # A multi-plate archive's print_name carries " - Plate N"; the stamp needs no name at all.
        ("Bracket", "Bracket - Plate 2", "Bracket.gcode.3mf"),
        # Codex USO-2: an archive with no name still has an address.
        ("Bracket", None, "Bracket.gcode.3mf"),
        ("Bracket", "", "Bracket.gcode.3mf"),
    ],
)
async def test_a_reconcile_close_lends_its_own_session_whatever_the_names(
    db_session, printer_factory, session_name, print_name, filename
):
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name=print_name, filename=filename)
    session = _session(printer.id, session_name, owner=archive.recovery_identity, tracked=True)

    mapping, assignments, in_memory = await _close(db_session, archive, session)

    assert (mapping, assignments, in_memory) == ([3], {(0, 3): 987}, None)


@pytest.mark.parametrize(
    ("session_name", "print_name", "filename"),
    [
        ("Part_Left", "Part Left", "Part Left.gcode.3mf"),  # space/underscore folding
        ("shared_long_prefix...", "shared_long_prefix_left", "shared_long_prefix_left.gcode.3mf"),  # truncation
        ("gear.mount", "gear.gcode.mount", "gear.gcode.mount.gcode.3mf"),  # the lossy legacy label
        ("Part Left", "Part Left", "Part Left.gcode.3mf"),  # even an exact name
        ("Part Left", None, "Part Left.gcode.3mf"),  # Codex USO-2: no name is no exemption
        ("Part Left", "", "Part Left.gcode.3mf"),
    ],
)
async def test_a_reconcile_close_never_lends_another_executions_session(
    db_session, printer_factory, session_name, print_name, filename
):
    """Codex USN-1/USO-2: B's stamped session, whatever its name or A's, never books A."""
    printer = await printer_factory()
    a = await _archive(db_session, printer.id, print_name=print_name, filename=filename)
    b = await _archive(db_session, printer.id, print_name=session_name, filename=f"{session_name}.gcode.3mf")
    session = _session(printer.id, session_name, owner=b.recovery_identity, tracked=True)

    mapping, assignments, in_memory = await _close(db_session, a, session)

    assert (mapping, assignments, in_memory) == (None, None, session)


@pytest.mark.parametrize("print_name", ["Overnight_Job", None, ""])
async def test_an_unclaimed_start_is_never_lent(db_session, printer_factory, print_name):
    """A start no archive has stamped yet proves nothing, whatever the closing archive is called."""
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name=print_name, filename="job.gcode.3mf")
    session = _session(printer.id, "Overnight_Job", tracked=True)

    assert await _close(db_session, archive, session) == (None, None, session)


@pytest.mark.parametrize(("session_name", "lent"), [("Overnight_Job", True), ("Overnight_Job.3mf", False)])
async def test_a_pre_m221_session_keeps_the_exact_name_rule(db_session, printer_factory, session_name, lent):
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name="Overnight_Job", filename="job.gcode.3mf")
    session = _session(printer.id, session_name)

    mapping, _assignments, in_memory = await _close(db_session, archive, session)

    assert (mapping == [3], in_memory is session) == (lent, not lent)


@pytest.mark.parametrize("spoolman", [True, False])
async def test_a_fresh_start_read_back_from_its_row_is_still_unclaimed(db_session, printer_factory, spoolman):
    """Codex USO-3: a real start writes its row and no archive has claimed it yet. Spoolman mode keeps no
    session in memory, and the internal one is lost here as at a restart. An older archive of the exact same
    name closes now: the row read back is still unclaimed, so it lends nothing, and it does not enter memory
    either. A Spoolman print's remain% sync must keep running."""
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    older = await _archive(db_session, printer.id, print_name="Same", filename="Same.gcode.3mf")
    pm = MagicMock()
    pm.get_status.return_value = SimpleNamespace(
        raw_data={"ams": {"ams": []}, "mapping": None}, tray_now=254, last_loaded_tray=254
    )
    with patch.object(ut, "_journal_print_start", new_callable=AsyncMock):
        started = await ut.on_print_start(
            printer.id, {"subtask_name": "Same", "ams_mapping": [3]}, pm, db=db_session, spoolman_owns_usage=spoolman
        )
    ut._active_sessions.pop(printer.id, None)
    row = await _persisted(db_session, printer.id)
    assert (row.archive_identity, row.owner_tracked, row.started_at) == (None, True, started.replace(tzinfo=None))

    mapping, assignments, in_memory = await _close(db_session, older, restore=True)

    assert (mapping, assignments, in_memory) == (None, None, None)


@pytest.mark.parametrize(
    ("owner", "lent"), [("own", True), ("foreign", False), (None, True)], ids=["own", "foreign", "unstamped"]
)
async def test_a_live_completion_never_lends_a_session_stamped_with_another_archive(
    db_session, printer_factory, owner, lent
):
    """Codex USO-2: a known archive address always checks the stamp, on the live path too, even with no name.
    The live path keeps lending its print's unstamped session, as before."""
    printer = await printer_factory()
    archive = await _archive(db_session, printer.id, print_name=None, filename="Bracket.gcode.3mf")
    other = await _archive(db_session, printer.id, print_name="Bracket", filename="Bracket.gcode.3mf")
    identity = {"own": archive.recovery_identity, "foreign": other.recovery_identity, None: None}[owner]
    session = _session(printer.id, "Bracket", owner=identity, tracked=True)

    mapping, _assignments, in_memory = await _close(db_session, archive, session, owner_only=False)

    assert (mapping == [3], in_memory is session) == (lent, not lent)


# ── the reconcile's cleanup ─────────────────────────────────────────────────


async def _reconcile(db, archive):
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    with patch("backend.app.services.usage_tracker.on_print_complete", new_callable=AsyncMock) as book:
        await _reconcile_complete_archive(db, archive, status="completed", uncertain=False)
    return book


async def test_a_reconcile_close_deletes_its_own_sessions_row_and_asks_for_the_owner(db_session, printer_factory):
    printer = await printer_factory()
    archive = await _archive(
        db_session, printer.id, print_name="LID 600 10mm x10", filename="LID 600 10mm x10.gcode.3mf"
    )
    await _row(db_session, printer.id, print_name="LID_600_10mm_x10.3mf", owner=archive.recovery_identity)

    book = await _reconcile(db_session, archive)

    assert book.await_args.kwargs["owner_only"] is True
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
        tracked=True,
        started_at=datetime(2026, 11, 11, 10, 26, 31),
    )

    await _reconcile(db_session, a)

    assert await _persisted(db_session, printer.id) is not None


# ── the stamp ───────────────────────────────────────────────────────────────


async def test_the_stamp_claims_only_the_session_the_start_made(db_session, printer_factory):
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    started = datetime.now(timezone.utc)
    session = _session(printer.id, "Plate_3", tracked=True)
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
    newer = _session(printer.id, "Next", tracked=True)
    ut._active_sessions[printer.id] = newer
    await _row(db_session, printer.id, print_name="Next", started_at=newer.started_at.replace(tzinfo=None))

    assert not await ut.claim_session(db_session, printer.id, started_at=older, archive_identity="uuid-old")

    assert newer.archive_identity is None
    assert (await _persisted(db_session, printer.id)).archive_identity is None


async def test_a_persisted_stamp_survives_the_round_trip(db_session, printer_factory):
    from backend.app.services import usage_tracker as ut

    printer = await printer_factory()
    session = _session(printer.id, "Plate_3", owner="uuid-a", tracked=True)
    await ut.persist_session(db_session, session)
    ut._active_sessions.clear()

    await ut.restore_session(db_session, printer.id)

    restored = ut._active_sessions[printer.id]
    assert (restored.archive_identity, restored.owner_tracked) == ("uuid-a", True)


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
