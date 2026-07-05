"""Regression for #1807: false "Print Stopped" state corruption on reprint
after an MQTT reconnect.

BamDude mints a fresh ``subtask_id`` per dispatch. When ``on_print_start``
adopts a leftover ``status='printing'`` archive by name/hash match, the old
guard rewrote ``archive.subtask_id`` only when it was empty
(``... is None``) — so a reprint that adopted the FIRST run's row kept the
FIRST run's id. On the next MQTT reconnect the reconciler's
``_subtask_stale`` then compared the stale stored id against the printer's
live id, saw a mismatch, classified the live print as a ghost-replay
("uncertain"), and false-closed a RUNNING archive (spurious queue-idle +
premature awaiting-plate-clear).

Unlike upstream this never fired a literal "Print Stopped" push — our
reconciler closes without notifying — but the state corruption is the same
root cause. The fix rewrites ``archive.subtask_id`` whenever the live id
differs from the stored one (``!= subtask_id``), so an adopted archive
always tracks the CURRENT run's id. Equal id in ⇒ no rewrite (no-op on
stable pushes).

These tests drive the real ``on_print_start`` name-match adoption branch
against the in-memory DB, then verify the downstream reconciler leaves the
refreshed archive running.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.services.print_reconciliation import _reconcile

_LIVE_ID = "2103771517"
_STALE_ID = "1844213296"


@pytest.fixture(autouse=True)
def _clear_active_print_state():
    """``on_print_start`` writes module-level tracking dicts keyed by
    ``(printer_id, filename)``. printer_factory resets to id=1 each function,
    so without clearing, one test's adoption leaves an ``_active_prints`` entry
    that makes the next test hit the duplicate-print_start guard."""
    from backend.app import main as main_module

    for _d in (
        main_module._active_prints,
        main_module._expected_prints,
        main_module._expected_print_registered_at,
        main_module._expected_print_creators,
    ):
        _d.clear()
    yield
    for _d in (
        main_module._active_prints,
        main_module._expected_prints,
        main_module._expected_print_registered_at,
        main_module._expected_print_creators,
    ):
        _d.clear()


async def _make_printing_archive(db_session, printer_id: int, *, subtask_id: str | None) -> int:
    """A leftover ``status='printing'`` row adoptable by the name-match branch.

    ``started_at=None`` + ``print_time_seconds=None`` puts it on the legacy
    unconditional-adoption path (and keeps ``_close_stale_printing_rows`` from
    touching it), so ``on_print_start`` adopts it deterministically.
    """
    archive = PrintArchive(
        printer_id=printer_id,
        filename="widget.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="widget",
        status="printing",
        started_at=None,
        print_time_seconds=None,
        subtask_id=subtask_id,
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive.id


async def _drive_print_start(test_engine, monkeypatch, printer_id: int, *, mqtt_subtask_id: str) -> None:
    """Run ``on_print_start`` against the in-memory DB with the network /
    notification side-effects stubbed, so it reaches the name-match adoption
    branch and its ``subtask_id`` backfill."""
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.main.async_session", test_factory)

    with (
        patch("backend.app.main.notify_missing_spool_assignments_on_print_start", new_callable=AsyncMock),
        patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
        patch("backend.app.main._load_objects_from_archive"),
        patch("backend.app.main._record_energy_start", new_callable=AsyncMock),
        patch("backend.app.main.mark_queue_printing_for_printer", new_callable=AsyncMock),
        patch("backend.app.services.macro_trigger.fire_event_macros", new_callable=AsyncMock),
    ):
        from backend.app.main import on_print_start

        await on_print_start(
            printer_id,
            {
                "filename": "widget.gcode.3mf",
                "subtask_name": "widget",
                "subtask_id": mqtt_subtask_id,
                "raw_data": {},
            },
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reprint_updates_stale_adopted_subtask_id(db_session, test_engine, monkeypatch, printer_factory):
    """The #1807 case: an adopted archive stored an OLD subtask_id from the
    first run. On reprint the printer echoes a fresh one — the archive's
    stored id must be rewritten so the reconciler doesn't flag the live print
    as stale on the next MQTT reconnect."""
    printer = await printer_factory()
    archive_id = await _make_printing_archive(db_session, printer.id, subtask_id=_STALE_ID)

    await _drive_print_start(test_engine, monkeypatch, printer.id, mqtt_subtask_id=_LIVE_ID)

    db_session.expire_all()
    stored_id, status = (
        await db_session.execute(
            select(PrintArchive.subtask_id, PrintArchive.status).where(PrintArchive.id == archive_id)
        )
    ).one()
    assert stored_id == _LIVE_ID, (
        "name-match adoption must update archive.subtask_id to the new dispatch id; "
        "leaving the old value lets the reconciler false-close the live print (#1807)"
    )
    assert status == "printing"  # adopted, not duplicated / closed


@pytest.mark.asyncio
@pytest.mark.integration
async def test_first_run_sets_subtask_id_from_null(db_session, test_engine, monkeypatch, printer_factory):
    """Regression guard for the previously-correct first-run path: an adopted
    archive with no stored subtask_id must still have it written."""
    printer = await printer_factory()
    archive_id = await _make_printing_archive(db_session, printer.id, subtask_id=None)

    await _drive_print_start(test_engine, monkeypatch, printer.id, mqtt_subtask_id=_LIVE_ID)

    db_session.expire_all()
    stored_id = await db_session.scalar(select(PrintArchive.subtask_id).where(PrintArchive.id == archive_id))
    assert stored_id == _LIVE_ID


@pytest.mark.asyncio
@pytest.mark.integration
async def test_stable_push_does_not_change_subtask_id(db_session, test_engine, monkeypatch, printer_factory):
    """Same id in ⇒ no rewrite. The archive already carries the live id, so
    the subtask_id pre-check adopts it directly and the inequality guard is a
    no-op — the stored value is unchanged."""
    printer = await printer_factory()
    archive_id = await _make_printing_archive(db_session, printer.id, subtask_id=_LIVE_ID)

    await _drive_print_start(test_engine, monkeypatch, printer.id, mqtt_subtask_id=_LIVE_ID)

    db_session.expire_all()
    stored_id, status = (
        await db_session.execute(
            select(PrintArchive.subtask_id, PrintArchive.status).where(PrintArchive.id == archive_id)
        )
    ).one()
    assert stored_id == _LIVE_ID
    assert status == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reconciler_leaves_refreshed_archive_running(db_session, test_engine, monkeypatch, printer_factory):
    """End-to-end: after the reprint refreshes the adopted archive's id, a
    subsequent connect-edge reconcile against the SAME live id classifies it
    as still-running and leaves it printing — the false-close is gone.

    The contrast (a stale stored id → 'uncertain' close) is pinned by
    ``test_print_reconciliation.test_reconcile_ghost_replay_new_subtask_closes_uncertain``.
    """
    printer = await printer_factory()
    printer_id = printer.id
    archive_id = await _make_printing_archive(db_session, printer_id, subtask_id=_STALE_ID)

    # Reprint dispatch: printer echoes the fresh live id → guard refreshes it.
    await _drive_print_start(test_engine, monkeypatch, printer_id, mqtt_subtask_id=_LIVE_ID)

    # MQTT reconnect mid-print: reconciler runs against the live id. expire_all
    # drops db_session's cached (stale-id) view so _reconcile re-reads the row
    # the reprint's separate session just updated.
    db_session.expire_all()
    await _reconcile(
        db_session,
        printer_id=printer_id,
        live_state="RUNNING",
        live_file="widget.gcode.3mf",
        live_subtask_id=_LIVE_ID,
    )

    status = await db_session.scalar(select(PrintArchive.status).where(PrintArchive.id == archive_id))
    assert status == "printing", (
        "reconciler must leave the live print running once the archive tracks the "
        "current subtask_id — the #1807 false-close is fixed"
    )
