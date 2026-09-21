"""One short, run-bound completion operation for web and Telegram plate answers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.printer import Printer
from backend.app.services.archive_defects import DefectsResult, DefectsWrite, record_defects
from backend.app.services.archive_parts import load_rows
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


async def _validate_completion_assessment(db, archive: PrintArchive, write: DefectsWrite) -> None:
    """Completion submissions are snapshots, unlike the archive editor's PATCH.

    A stale callback must never silently ignore one of its part rows and then
    store a receipt that looks complete.  Legacy clients do not send a gate
    token, so their established partial-PATCH shape stays accepted until they
    refresh; every new, owned completion card sends the complete snapshot.
    """
    rows = await load_rows(db, archive.id)
    if not rows:
        if write.parts:
            raise InvalidPlateAssessment("This print has no part rows to assess")
        return
    submitted_ids = [part_id for part_id, _value in write.parts]
    expected_ids = {row.id for row in rows}
    if len(submitted_ids) != len(set(submitted_ids)):
        raise InvalidPlateAssessment("Each printed part may be assessed only once")
    if set(submitted_ids) != expected_ids:
        raise InvalidPlateAssessment("The completion assessment no longer matches this print's parts")


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

    async with _printer_locks[printer_id]:
        # Receipt lookup comes first: a client retry after a successful commit
        # receives its original result instead of treating a now-empty hold as
        # an error.  An opposite button is deliberately never a fallback.
        if expected_archive_id is not None:
            receipt = await db.scalar(
                select(PrintCompletionReceipt).where(PrintCompletionReceipt.archive_id == expected_archive_id)
            )
            if receipt is not None and receipt.plate_action is not None:
                if receipt.plate_action != action:
                    raise PlateAnswerAlreadyHandled("This completed print was already answered differently")
                archive = await db.get(PrintArchive, expected_archive_id)
                return PlateAnswerResult(
                    archive=archive,
                    action=action,
                    item_id=receipt.rearmed_queue_item_id,
                    already_answered=True,
                )

        # PostgreSQL locks the durable gate as well as this process's normal
        # one-printer operation.  SQLite has one writer and the local lock
        # closes the read-to-write window used by its supported deployment.
        printer = await db.scalar(select(Printer).where(Printer.id == printer_id).with_for_update())
        archive = await waiting_archive(db, printer_id)
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
