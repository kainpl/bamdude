"""«Result not checked yet» on top of defect accounting (upstream audit G04, part C).

Fitness stays the defect count. What is new is only whether somebody LOOKED:
one predicate (completed, outside the trash, no mark), one writer of the mark,
a list filter, an explicit «checked, no defects» door, and one reminder a print.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.permissions import Permission
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.project import Project
from backend.app.models.settings import Settings
from backend.app.services import archive_defects, archive_outcome, dispatch_phase, swap_terminal
from backend.tests.integration.test_plate_answers_defects import _finished_flat, _finished_on, _finished_printer
from backend.tests.integration.test_workshop_library_rights import _jwt, _user

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _mark_of(db_session, archive_id: int) -> datetime | None:
    return (
        await db_session.execute(select(PrintArchive.outcome_checked_at).where(PrintArchive.id == archive_id))
    ).scalar_one()


async def _names(client, query: str = "") -> set[str]:
    response = await client.get(f"/api/v1/archives/?per_page=200{query}")
    assert response.status_code == 200, response.text
    return {row["print_name"] for row in response.json()["data"]}


# --- the predicate and its SQL twin ----------------------------------------------------------------------


async def test_only_a_completed_live_unmarked_print_is_unchecked(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    marked = _utc_now()
    await archive_factory(printer.id, print_name="fresh")
    await archive_factory(printer.id, print_name="checked", outcome_checked_at=marked)
    await archive_factory(printer.id, print_name="failed", status="failed")
    await archive_factory(printer.id, print_name="cancelled", status="cancelled")
    await archive_factory(printer.id, print_name="running", status="printing")
    await archive_factory(printer.id, print_name="trashed", deleted_at=marked)
    await archive_factory(printer.id, print_name="calibration", is_calibration=True)

    response = await async_client.get("/api/v1/archives/?per_page=200")
    assert response.status_code == 200, response.text
    flags = {row["print_name"]: row["outcome_unchecked"] for row in response.json()["data"]}
    assert flags == {
        "fresh": True,
        "checked": False,
        "failed": False,
        "cancelled": False,
        "running": False,
        "calibration": False,
    }
    # The filter is the same predicate in SQL, and the total follows it.
    filtered = await async_client.get("/api/v1/archives/?outcome=unchecked&per_page=200")
    assert {row["print_name"] for row in filtered.json()["data"]} == {"fresh"}
    assert filtered.json()["meta"]["total"] == 1


async def test_the_filter_pages_with_the_other_filters(async_client, archive_factory, printer_factory):
    printer = await printer_factory()
    other = await printer_factory(name="B", serial_number="SERIAL-B")
    for index in range(3):
        await archive_factory(printer.id, print_name=f"p{index}")
    await archive_factory(other.id, print_name="elsewhere")
    await archive_factory(printer.id, print_name="done", outcome_checked_at=_utc_now())

    page = await async_client.get(f"/api/v1/archives/?outcome=unchecked&printer_id={printer.id}&per_page=2&page=2")
    assert page.status_code == 200, page.text
    assert page.json()["meta"]["total"] == 3 and len(page.json()["data"]) == 1


# --- the writer ------------------------------------------------------------------------------------------


async def test_the_first_mark_wins(db_session, archive_factory, printer_factory):
    archive = await archive_factory((await printer_factory()).id)
    assert archive_outcome.is_unchecked(archive)

    assert await archive_outcome.mark_checked(db_session, archive, by=1) is True
    first = archive.outcome_checked_at
    assert first is not None and first.tzinfo is None, "naive UTC, like its neighbours"
    assert await archive_outcome.mark_checked(db_session, archive, by=2) is False
    assert archive.outcome_checked_at == first
    assert not archive_outcome.is_unchecked(archive)


async def test_a_row_taken_back_as_the_running_print_loses_its_mark(db_session, archive_factory, printer_factory):
    archive = await archive_factory(
        (await printer_factory()).id, status="failed", outcome_checked_at=_utc_now(), outcome_reminded_at=_utc_now()
    )
    dispatch_phase.forget_earlier_outcome(archive)
    assert archive.outcome_checked_at is None and archive.outcome_reminded_at is None


# --- a plate the swap changed by itself (owner, 2026-11-20) ----------------------------------------------
#
# Nobody takes such a print off the plate, so nobody is asked to grade it. A swap that failed or was held
# leaves the print on the plate: somebody must go to the printer anyway, and grades it there.

_OWED_SWAP = {
    "bamdude_terminal_effects": {"swap_change_table": {"stage": "attempted", "owner": "live"}},
    "swap_macro_events_pending": ["swap_mode_change_table"],
}


async def test_a_swapped_plate_marks_the_print_before_it_reads_completed(
    db_session, archive_factory, printer_factory, session_factory
):
    # The live completion runs the macro before it writes the archive's terminal status.
    archive = await archive_factory((await printer_factory()).id, status="printing", extra_data=dict(_OWED_SWAP))

    await swap_terminal.finish_change_table(session_factory, archive.id, ok=True)

    await db_session.refresh(archive)
    assert archive.outcome_checked_at is not None
    archive.status = "completed"
    assert not archive_outcome.is_unchecked(archive)


async def test_a_failed_swap_leaves_the_print_unchecked(db_session, archive_factory, printer_factory, session_factory):
    archive = await archive_factory((await printer_factory()).id, extra_data=dict(_OWED_SWAP))

    await swap_terminal.finish_change_table(session_factory, archive.id, ok=False)

    await db_session.refresh(archive)
    assert archive.outcome_checked_at is None
    assert archive_outcome.is_unchecked(archive)


async def test_an_operator_settlement_does_not_mark(db_session, archive_factory, printer_factory):
    # «The swap was done by hand»: somebody stood at the printer and can grade the print themselves.
    archive = await archive_factory(
        (await printer_factory()).id,
        extra_data={
            "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed", "version": 1}},
            "swap_macro_events_pending": ["swap_mode_change_table"],
        },
    )

    swap_terminal.settle_by_operator(archive, action_identity=str(uuid4()), actor="operator", at=_utc_now())
    await db_session.commit()
    assert swap_terminal.operator_settlement(archive.extra_data, archive.recovery_identity) is not None

    assert await _mark_of(db_session, archive.id) is None


async def test_a_baked_clear_marks_the_print(db_session, archive_factory, printer_factory, session_factory):
    # The file's own swap sequence changed the plate: the same "nobody takes it off" as our macro.
    archive = await archive_factory((await printer_factory()).id, status="printing")

    await swap_terminal.note_baked_clear(session_factory, archive.id)

    assert await _mark_of(db_session, archive.id) is not None


async def test_the_swap_mark_keeps_an_earlier_mark(db_session, archive_factory, printer_factory, session_factory):
    earlier = datetime(2026, 11, 1, 10, 0)
    archive = await archive_factory(
        (await printer_factory()).id, outcome_checked_at=earlier, extra_data=dict(_OWED_SWAP)
    )

    await swap_terminal.finish_change_table(session_factory, archive.id, ok=True)
    await swap_terminal.note_baked_clear(session_factory, archive.id)

    assert await _mark_of(db_session, archive.id) == earlier


# --- every defects door marks; a bare plate clear does not -----------------------------------------------


@pytest.mark.parametrize("body", [{"defective_count": 1}, {"defective_count": 0}])
async def test_patch_defects_marks_the_print_an_explicit_zero_too(
    async_client, archive_factory, printer_factory, db_session, body
):
    archive = await archive_factory((await printer_factory()).id, quantity=3)

    response = await async_client.patch(f"/api/v1/archives/{archive.id}", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["outcome_unchecked"] is False and response.json()["outcome_checked_at"] is not None
    assert await _mark_of(db_session, archive.id) is not None


async def test_an_edit_that_is_not_about_defects_does_not_mark(
    async_client, archive_factory, printer_factory, db_session
):
    archive = await archive_factory((await printer_factory()).id)

    response = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"notes": "looks fine from here"})

    assert response.status_code == 200, response.text
    assert response.json()["outcome_unchecked"] is True
    assert await _mark_of(db_session, archive.id) is None


async def test_the_order_page_marks(committing_client, db_session):
    order = Project(name="O", status="active")
    db_session.add(order)
    await db_session.flush()
    archive = PrintArchive(
        project_id=order.id,
        filename="a.3mf",
        print_name="A",
        file_path="x/a.3mf",
        file_size=1,
        status="completed",
        quantity=3,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id

    wrote = await committing_client.post(
        f"/api/v1/projects/{order.id}/archives/{archive_id}/defects", json={"defective_count": 0}
    )

    assert wrote.status_code == 200, wrote.text
    assert await _mark_of(db_session, archive_id) is not None


async def test_a_plate_answer_with_defects_marks(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive, _row = await _finished_on(db_session, printer, {"lid": 2})
    lid = (await db_session.execute(select(PrintArchivePart).where(PrintArchivePart.name_key == "lid"))).scalar_one()
    archive_id = archive.id

    with _finished_printer():
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/clear-plate", json={"defects": {"parts": [{"id": lid.id, "defective": 0}]}}
        )

    assert response.status_code == 200, response.text
    assert await _mark_of(db_session, archive_id) is not None


async def test_a_repeat_with_defects_marks(async_client, printer_factory, db_session):
    printer = await printer_factory()
    archive, _row = await _finished_flat(db_session, printer, 2)
    archive_id = archive.id

    with _finished_printer():
        response = await async_client.post(
            f"/api/v1/printers/{printer.id}/repeat-print", json={"defects": {"defective_count": 1}}
        )

    assert response.status_code == 200, response.text
    assert await _mark_of(db_session, archive_id) is not None


async def test_a_plate_clear_without_defects_does_not_mark(async_client, printer_factory, db_session):
    """Clearing the plate says the bed is empty, not that anybody graded what came off it."""
    printer = await printer_factory()
    archive, _row = await _finished_flat(db_session, printer, 2)
    archive_id = archive.id

    with _finished_printer():
        response = await async_client.post(f"/api/v1/printers/{printer.id}/clear-plate")

    assert response.status_code == 200, response.text
    assert await _mark_of(db_session, archive_id) is None


async def test_the_telegram_assessment_marks(db_session, archive_factory, printer_factory):
    from backend.app.services.plate_answers import record_completion_assessment

    archive = await archive_factory((await printer_factory()).id, quantity=2)
    archive_id = archive.id

    await record_completion_assessment(db_session, archive_id, archive_defects.DefectsWrite(flat=0), actor_id=None)
    await db_session.commit()

    assert await _mark_of(db_session, archive_id) is not None


# --- the explicit door -----------------------------------------------------------------------------------


async def test_checked_no_defects_marks_and_leaves_the_count_alone(
    async_client, archive_factory, printer_factory, db_session
):
    archive = await archive_factory((await printer_factory()).id, quantity=4, defective_count=1)

    response = await async_client.post(f"/api/v1/archives/{archive.id}/outcome-checked")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome_unchecked"] is False and body["defective_count"] == 1
    first = await _mark_of(db_session, archive.id)
    assert first is not None

    again = await async_client.post(f"/api/v1/archives/{archive.id}/outcome-checked")
    assert again.status_code == 200, again.text
    assert await _mark_of(db_session, archive.id) == first, "a second mark changes nothing"


@pytest.mark.parametrize("status", ["failed", "cancelled", "aborted", "printing"])
async def test_only_a_completed_print_takes_the_mark(
    async_client, archive_factory, printer_factory, db_session, status
):
    archive = await archive_factory((await printer_factory()).id, status=status)

    response = await async_client.post(f"/api/v1/archives/{archive.id}/outcome-checked")

    assert response.status_code == 409, response.text
    assert await _mark_of(db_session, archive.id) is None


async def test_a_trashed_or_missing_print_is_not_found(async_client, archive_factory, printer_factory):
    archive = await archive_factory((await printer_factory()).id, deleted_at=datetime.now(timezone.utc))

    assert (await async_client.post(f"/api/v1/archives/{archive.id}/outcome-checked")).status_code == 404
    assert (await async_client.post("/api/v1/archives/987654/outcome-checked")).status_code == 404


async def test_the_door_asks_the_archives_update_right(committing_client, db_session, archive_factory, printer_factory):
    printer = await printer_factory()
    own = await _user(db_session, "oc_own", [Permission.ARCHIVES_READ_ALL.value, Permission.ARCHIVES_UPDATE_OWN.value])
    await _user(db_session, "oc_reader", [Permission.ARCHIVES_READ_ALL.value])
    await _user(db_session, "oc_all", [Permission.ARCHIVES_READ_ALL.value, Permission.ARCHIVES_UPDATE_ALL.value])
    mine = await archive_factory(printer.id, created_by_id=own.id)
    theirs = await archive_factory(printer.id)

    async def ask(who: str, archive_id: int) -> int:
        response = await committing_client.post(
            f"/api/v1/archives/{archive_id}/outcome-checked", headers=await _jwt(who)
        )
        return response.status_code

    assert await ask("oc_reader", mine.id) == 403
    assert await ask("oc_own", theirs.id) == 403
    assert await ask("oc_own", mine.id) == 200
    assert await ask("oc_all", theirs.id) == 200


# --- the reminder sweep ----------------------------------------------------------------------------------


@pytest.fixture
def session_factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def test_the_sweep_reminds_once_and_only_after_the_delay(
    db_session, archive_factory, printer_factory, session_factory
):
    printer = await printer_factory()
    now = _utc_now()
    due = await archive_factory(printer.id, print_name="due", completed_at=now - timedelta(minutes=61))
    await archive_factory(printer.id, print_name="recent", completed_at=now - timedelta(minutes=10))
    await archive_factory(
        printer.id, print_name="checked", completed_at=now - timedelta(hours=5), outcome_checked_at=now
    )
    await archive_factory(printer.id, print_name="failed", status="failed", completed_at=now - timedelta(hours=5))
    await archive_factory(printer.id, print_name="no-end")
    stamped_when_sent: list[bool] = []

    async def send(reminder, db):
        # The stamp is committed BEFORE the send: another session already sees it.
        row = await db.scalar(select(PrintArchive.outcome_reminded_at).where(PrintArchive.id == reminder.archive_id))
        stamped_when_sent.append(row is not None)
        sent.append(reminder)

    sent: list = []
    assert await archive_outcome.remind_due(session_factory, send) == 1
    assert [(r.archive_id, r.print_name, r.printer_id) for r in sent] == [(due.id, "due", printer.id)]
    assert sent[0].identity == due.recovery_identity
    assert stamped_when_sent == [True]

    assert await archive_outcome.remind_due(session_factory, send) == 0, "a print is reminded about once"
    assert len(sent) == 1


async def test_a_failed_send_is_not_repeated(db_session, archive_factory, printer_factory, session_factory):
    printer = await printer_factory()
    archive_id = (await archive_factory(printer.id, completed_at=_utc_now() - timedelta(hours=2))).id
    send = AsyncMock(side_effect=RuntimeError("smtp down"))

    assert await archive_outcome.remind_due(session_factory, send) == 0
    assert await archive_outcome.remind_due(session_factory, send) == 0
    assert send.await_count == 1
    stamp = await db_session.scalar(select(PrintArchive.outcome_reminded_at).where(PrintArchive.id == archive_id))
    assert stamp is not None


async def test_the_delay_is_the_setting_inside_its_bounds(
    db_session, archive_factory, printer_factory, session_factory
):
    printer = await printer_factory()
    await archive_factory(printer.id, print_name="ten", completed_at=_utc_now() - timedelta(minutes=10))
    db_session.add(Settings(key="outcome_reminder_after_minutes", value="5"))
    await db_session.commit()
    send = AsyncMock()

    assert await archive_outcome.remind_due(session_factory, send) == 1

    assert archive_outcome.reminder_delay_minutes(None) == 60
    assert archive_outcome.reminder_delay_minutes("garbage") == 60
    assert archive_outcome.reminder_delay_minutes("1") == 5
    assert archive_outcome.reminder_delay_minutes("999999") == 10080


async def test_the_setting_is_saved_inside_its_bounds(async_client):
    assert (await async_client.get("/api/v1/settings/")).json()["outcome_reminder_after_minutes"] == 60
    saved = await async_client.put("/api/v1/settings/", json={"outcome_reminder_after_minutes": 120})
    assert saved.status_code == 200, saved.text
    assert (await async_client.get("/api/v1/settings/")).json()["outcome_reminder_after_minutes"] == 120
    for bad in (4, 10081):
        refused = await async_client.put("/api/v1/settings/", json={"outcome_reminder_after_minutes": bad})
        assert refused.status_code == 422, refused.text


async def test_the_reminder_goes_out_with_the_print_and_a_link(db_session, archive_factory, printer_factory):
    from backend.app.services.notification_service import NotificationService

    printer = await printer_factory(name="Left X1")
    archive = await archive_factory(printer.id, print_name="Bracket", completed_at=datetime(2026, 11, 19, 10, 0))
    db_session.add(Settings(key="external_url", value="https://farm.example/"))
    await db_session.commit()
    service = NotificationService()
    service._send_to_providers = AsyncMock()
    reminder = archive_outcome.DueReminder(
        archive.id, archive.recovery_identity, printer.id, "Bracket", archive.completed_at
    )

    await service.on_print_outcome_unchecked(reminder, db_session)

    call = service._send_to_providers.await_args
    assert call.args[4] == "print_outcome_unchecked" and call.args[5] == printer.id
    assert "Left X1" in call.args[2] and "Bracket" in call.args[2]
    assert "https://farm.example/archives?outcome=unchecked" in call.args[2]
    assert call.kwargs["extra_data"] == {"archive_id": archive.id, "archive_identity": archive.recovery_identity}


# --- Git metadata restore --------------------------------------------------------------------------------


async def test_a_restored_print_comes_back_checked(db_session, archive_factory, printer_factory):
    printer = await printer_factory()
    ended = datetime(2026, 11, 1, 9, 0)
    recorded = datetime(2026, 11, 1, 12, 0)
    carried = await archive_factory(printer.id, completed_at=ended)
    old_backup = await archive_factory(printer.id, completed_at=ended)
    local = await archive_factory(printer.id, completed_at=ended)
    marked = await archive_factory(printer.id, completed_at=ended, outcome_checked_at=ended)

    assert archive_outcome.carry_restored(carried, recorded, insert=True) is True
    assert carried.outcome_checked_at == recorded
    # A backup from before the mark existed: the restored print is history, not «unchecked».
    assert archive_outcome.carry_restored(old_backup, None, insert=True) is True
    assert old_backup.outcome_checked_at == ended
    # A print that already lives here and has no mark in either place stays unchecked.
    assert archive_outcome.carry_restored(local, None, insert=False) is False
    assert local.outcome_checked_at is None
    # A local mark is never replaced.
    assert archive_outcome.carry_restored(marked, recorded, insert=False) is False
    assert marked.outcome_checked_at == ended
