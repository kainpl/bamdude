"""One short, run-bound completion operation for web and Telegram plate answers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, text

from backend.app.models.archive import PrintArchive
from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.printer import Printer
from backend.app.services.archive_defects import DefectsResult, DefectsWrite, record_defects
from backend.app.services.archive_parts import load_rows
from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write
from backend.app.services.plate_hold import (
    StalePlateAnswer,
    answer_by_clearing,
    answer_by_repeating,
    waiting_archive,
)

_printer_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(frozen=True)
class PlateAnswerResult:
    archive: PrintArchive | None
    action: str
    defects: DefectsResult | None = None
    item_id: int | None = None
    already_answered: bool = False


class PlateAnswerAlreadyHandled(StalePlateAnswer):
    """The held run has a receipt for the other completion action."""


class InvalidPlateAssessment(ValueError):
    """A completion card submitted a partial or foreign part assessment."""


def _assessment_payload(result: DefectsResult) -> dict:
    return {
        "defective_count": result.defective_count,
        "parts": [{"id": row.id, "defective": int(row.defective or 0)} for row in result.parts],
        "ledger_refused_parts": result.ledger_refused,
    }


def completion_assessment_snapshot(archive: PrintArchive, rows) -> tuple:
    """The archive facts a completion form is allowed to decide from.

    This intentionally includes attribution and the existing grade as well as
    part ids/quantities.  A Telegram form is a complete snapshot, not the
    archive editor's forgiving PATCH: if any of these facts moved while an
    operator was answering, they reopen against the current plate.
    """
    return (
        archive.status,
        int(archive.quantity or 0),
        archive.library_file_id,
        archive.project_id,
        archive.project_line_id,
        int(archive.defective_count or 0),
        tuple((row.id, int(row.quantity or 0), int(row.defective or 0)) for row in rows),
    )


async def record_completion_assessment(
    db,
    archive: PrintArchive | int,
    write: DefectsWrite,
    *,
    actor_id: int | None = None,
    expected_snapshot: tuple | None = None,
) -> DefectsResult:
    """Persist a complete Telegram assessment without answering the plate gate.

    A defect grade and Clear/Repeat are separate facts.  The latter may happen
    first, later, or never; recording zero just because a plate was cleared
    would falsify the print history.
    """
    archive_id = archive if isinstance(archive, int) else archive.id
    async with archive_write_scope(db, archive_id):
        current = await load_active_archive_for_write(db, archive_id)
        if current is None:
            raise StalePlateAnswer("This print is no longer available for assessment")
        await _validate_completion_assessment(db, current, write, expected_snapshot=expected_snapshot)
        result = await record_defects(db, current, write, actor_id=actor_id)
        receipt = await db.scalar(
            select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == current.id).with_for_update()
        )
        if receipt is None:
            receipt = PrintCompletionReceipt(archive_id=current.id)
            db.add(receipt)
        receipt.assessment = _assessment_payload(result)
        receipt.assessment_at = datetime.now(timezone.utc)
        receipt.assessment_actor_id = actor_id
        return result


async def _validate_completion_assessment(
    db, archive: PrintArchive, write: DefectsWrite, *, expected_snapshot: tuple | None = None
) -> None:
    """Completion submissions are snapshots, unlike the archive editor's PATCH.

    A stale callback must never silently ignore one of its part rows and then
    store a receipt that looks complete.  Legacy clients do not send a gate
    token, so their established partial-PATCH shape stays accepted until they
    refresh; every new, owned completion card sends the complete snapshot.
    """
    rows = await load_rows(db, archive.id)
    if expected_snapshot is not None and completion_assessment_snapshot(archive, rows) != expected_snapshot:
        raise InvalidPlateAssessment("The print changed while this completion assessment was open")
    if not rows:
        if write.parts:
            raise InvalidPlateAssessment("This print has no part rows to assess")
        if write.flat is None or write.flat < 0 or write.flat > int(archive.quantity or 0):
            raise InvalidPlateAssessment("A completion assessment is outside the current printed quantity")
        return
    submitted_ids = [part_id for part_id, _value in write.parts]
    expected_ids = {row.id for row in rows}
    if len(submitted_ids) != len(set(submitted_ids)):
        raise InvalidPlateAssessment("Each printed part may be assessed only once")
    if set(submitted_ids) != expected_ids:
        raise InvalidPlateAssessment("The completion assessment no longer matches this print's parts")
    quantities = {row.id: int(row.quantity or 0) for row in rows}
    if any(value < 0 or value > quantities[part_id] for part_id, value in write.parts):
        raise InvalidPlateAssessment("A completion assessment is outside the current printed quantity")
    if write.flat is not None:
        raise InvalidPlateAssessment("A multipart completion assessment cannot carry a flat defect count")


async def answer_plate_run(
    db,
    *,
    printer_id: int,
    expected_archive_id: int | None,
    action: str,
    defects: DefectsWrite | None = None,
    actor_id: int | None = None,
    expected_gate_token: str | None = None,
) -> PlateAnswerResult:
    """Write defects and answer exactly the held run, then release the gate.

    The lock is intentionally process-local: BamDude has one scheduler and one
    Telegram poller in this process.  It closes the SQLite read-to-write window;
    the exact archive check remains the database-independent final guard.
    """
    if action not in {"clear", "repeat"}:
        raise ValueError(f"Unsupported plate answer: {action}")

    from backend.app.services.printer_manager import printer_manager

    async with _printer_locks[printer_id], AsyncExitStack() as write_scopes:
        # SQLite must become the writer before receipt/gate/row reads.  When an
        # expected archive is present its archive scope does this; legacy calls
        # have no archive identity to lock, so take the short DB-wide writer
        # first.  PostgreSQL row/advisory guards below provide the equivalent.
        archive_guarded = False
        if expected_archive_id is not None:
            await write_scopes.enter_async_context(archive_write_scope(db, expected_archive_id))
            archive_guarded = True
        elif db.get_bind().dialect.name == "sqlite" and not db.in_transaction():
            await db.execute(text("BEGIN IMMEDIATE"))

        # Receipt lookup happens only inside the same write transaction as the
        # gate mutation. A retry cannot observe half of a previous answer.
        if expected_archive_id is not None:
            receipt = await db.scalar(
                select(PrintCompletionReceipt)
                .where(PrintCompletionReceipt.archive_id == expected_archive_id)
                .with_for_update()
            )
            if receipt is not None and receipt.plate_action is not None:
                if receipt.plate_action != action:
                    raise PlateAnswerAlreadyHandled("This completed print was already answered differently")
                archive = await load_active_archive_for_write(db, expected_archive_id)
                return PlateAnswerResult(
                    archive=archive,
                    action=action,
                    item_id=receipt.rearmed_queue_item_id,
                    already_answered=True,
                )

        # PostgreSQL locks the durable gate as well as this process's normal
        # one-printer operation. SQLite already owns the writer at this point.
        printer = await db.scalar(select(Printer).where(Printer.id == printer_id).with_for_update())
        archive = await waiting_archive(db, printer_id)
        if archive is not None and not archive_guarded:
            await write_scopes.enter_async_context(archive_write_scope(db, archive.id))
            archive = await load_active_archive_for_write(db, archive.id)
        if expected_archive_id is not None and (archive is None or archive.id != expected_archive_id):
            raise StalePlateAnswer("This completion card is no longer current for this printer")
        if printer is not None and printer.awaiting_plate_clear_archive_id is not None:
            if archive is None or printer.awaiting_plate_clear_archive_id != archive.id:
                raise StalePlateAnswer("This completion card no longer owns the printer's plate-clear gate")
            if expected_gate_token is not None and expected_gate_token != printer.awaiting_plate_clear_token:
                raise StalePlateAnswer("This completion card has expired")
        if archive is None and defects is not None:
            raise StalePlateAnswer("No finished print is waiting on this printer")

        defects_result = None
        if defects is not None:
            if archive is None:
                raise StalePlateAnswer("No finished print is waiting on this printer")
            if expected_gate_token is not None:
                await _validate_completion_assessment(db, archive, defects)
            defects_result = await record_defects(db, archive, defects, actor_id=actor_id)

        async with AsyncExitStack() as source_guards:
            item_id = None
            if action == "clear":
                await answer_by_clearing(db, printer_id, expected_archive_id=expected_archive_id, commit=False)
            else:
                row = await answer_by_repeating(
                    db,
                    printer_id,
                    expected_archive_id=expected_archive_id,
                    commit=False,
                    source_guards=source_guards,
                )
                if row is None:
                    raise StalePlateAnswer("No finished print is waiting on this printer")
                item_id = row.id

            receipt = None
            if archive is not None:
                receipt = await db.scalar(
                    select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == archive.id)
                )
                if receipt is None:
                    receipt = PrintCompletionReceipt(archive_id=archive.id)
                    db.add(receipt)
                if defects_result is not None:
                    receipt.assessment = _assessment_payload(defects_result)
                    receipt.assessment_at = datetime.now(timezone.utc)
                    receipt.assessment_actor_id = actor_id
                receipt.plate_action = action
                receipt.plate_action_at = datetime.now(timezone.utc)
                receipt.plate_action_actor_id = actor_id
                receipt.gate_token = printer.awaiting_plate_clear_token if printer is not None else None
                receipt.rearmed_queue_item_id = item_id

            # Clear exactly the gate that was just answered in the same commit as
            # the receipt and queue mutation.  Legacy ownerless gates remain
            # supported, but never acquire a guessed archive owner.
            if printer is not None:
                printer.awaiting_plate_clear = False
                printer.awaiting_plate_clear_archive_id = None
                printer.awaiting_plate_clear_token = None

            await db.commit()

    # The scheduler may run only after the database says which held row was answered.
    printer_manager.confirm_awaiting_plate_clear_released(printer_id)
    return PlateAnswerResult(archive=archive, action=action, defects=defects_result, item_id=item_id)
