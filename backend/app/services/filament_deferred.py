"""Bookkeeping for an attempt refused before MQTT publish; never a failed print."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, func, update

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.filament_intake import routing_detail
from backend.app.services.filament_policy import decode
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.queue_counters import update_queue_counters
from backend.app.services.queue_sources import QueueSourceBusy, QueueSourceError
from backend.app.services.source_io import SOURCE_FAILURES

#: What a refused attempt becomes, named by the refusal itself rather than guessed from its reason
#: (swap-source-lifecycle spec r4 §6). ``failed``: before dispatch, ``gate_acknowledged``, only a manual Retry.
#: ``retry_later``: back to pending with its own wait reason, the scheduler's bounded cooldown deciding when.
DISPOSITION_FAILED = "failed"
DISPOSITION_RETRY_LATER = "retry_later"
#: The wait reason of a queued job whose source copy found the spool busy.
SOURCE_CAPTURE_WAIT_CODE = "source_capture"


@dataclass(frozen=True)
class DeferResult:
    """What :func:`defer_claim` did. Truthy when THIS attempt's row took the refusal;
    ``terminal`` when it ended the row as failed with ``code`` / ``message`` (not yet committed);
    ``swap_started`` when our swap-start macro ran or may have (the printer is left as it is)."""

    owned: bool
    terminal: bool = False
    code: str | None = None
    message: str | None = None
    swap_started: bool = False

    def __bool__(self) -> bool:
        return self.owned


class SourceCaptureDeferred(RoutingDeferred):
    """A dispatch-time copy of the source was refused; ``disposition`` says what the job becomes."""

    def __init__(self, reason: str, *, disposition: str):
        super().__init__(reason)
        self.disposition = disposition


class SwapSourceRefused(SourceCaptureDeferred):
    """D2 of the swap-source-lifecycle spec: our swap macros were asked for a source whose declaration is unknown.

    Failed before dispatch, like a refused copy -- ``gate_acknowledged``, no waiting reason, the job's events kept
    -- and never a silent strip of what the operator asked for.
    """

    def __init__(self, reason: str):
        super().__init__(reason, disposition=DISPOSITION_FAILED)


def capture_refusal(exc: QueueSourceError, *, queued: bool) -> SourceCaptureDeferred:
    """The class of the refusal decides, never its text or an HTTP status (§6's table).

    A busy or replaced spool is "ask again later" -- for a queued job only, which then waits; a direct print never
    retries by itself. A timeout, an unreadable / changed / invalid source, a spool without room or a failed write
    end the attempt before dispatch with their own reason, and wait for the operator's Retry.
    """
    retry = queued and isinstance(exc, QueueSourceBusy)
    return SourceCaptureDeferred(exc.reason, disposition=DISPOSITION_RETRY_LATER if retry else DISPOSITION_FAILED)


def dispatched_archive_filter():
    return func.coalesce(PrintArchive.extra_data["dispatch_aborted"].as_boolean(), False).is_(False)


async def abort_execution_archive(db, archive_id, reason, *, failed_code=None):
    """Close a deferred attempt's execution archive: aborted (never published), or failed with its class.

    ``failed_code`` is given when the refusal ENDED the attempt — a terminal refusal of a
    row this attempt still owned, or a swap start that ran: such an archive is a
    «Dispatch errors» attempt, not a print nobody started.
    """
    if archive_id is None:
        return
    archive = await db.get(PrintArchive, archive_id)
    if archive is None or archive.status != "printing":
        return
    archive.completed_at = datetime.now(timezone.utc)
    message = reason["message"] if isinstance(reason, dict) else str(reason)
    if failed_code:
        archive.status = "failed"
        archive.failure_reason = failed_code
        archive.error_message = message
        return
    archive.status = "cancelled"
    archive.extra_data = {**(archive.extra_data or {}), "dispatch_aborted": True, "dispatch_abort_reason": reason}


async def swap_start_may_have_run(db, execution_archive_id, swap_start_effect=None) -> bool:
    """D/I's one veto (post-rebuild D-R4/I-R3): did our swap-start macro run, or may it have, on this attempt?

    The live attempt's own fact first (``PrintDispatchJob.swap_start_effect``); then the
    execution archive's record, read through ``swap_terminal.cycle_state`` — the one
    reader of it — when the attempt has one. Before an archive exists the start cannot
    have run. A pending-events list is an INTENT and proves nothing either way.
    """
    if swap_start_effect in ("fired", "possibly_fired"):
        return True
    if execution_archive_id is None:
        return False
    from backend.app.services import swap_terminal

    archive = await db.get(PrintArchive, execution_archive_id)
    if archive is None:
        return False
    return swap_terminal.cycle_state(archive.extra_data) != swap_terminal.CYCLE_NOT_STARTED


async def defer_claim(
    db,
    *,
    item_id,
    started_at,
    reason,
    claim_detail=None,
    revision=None,
    direct=False,
    source_archive_id=None,
    source_library_file_id=None,
    restore_source=False,
    disposition=None,
    execution_archive_id=None,
    swap_start_effect=None,
    published=False,
    failure_code=None,
):
    """CAS only our active attempt. Cancelled/deleted/reclaimed rows stay untouched.

    Answers a :class:`DeferResult` — truthy when this attempt's row took the refusal, and
    saying whether it ended the row, so the caller can report it once after its commit.

    ``disposition`` is an explicit answer from a refusal that knows what it is (a source copy at dispatch,
    :func:`capture_refusal`): ``failed`` ends the attempt whatever the reason, ``retry_later`` sends a queued job
    back to pending under its own wait reason. Neither is a routing state, so the routing intent is left as it
    is. Without one, a caller gets exactly the behaviour it always had.

    Post-rebuild D-R4/D-R5 add three rules, in the same compare-and-set as the row:

    * a refusal after our swap-start macro ran, or may have (:func:`swap_start_may_have_run`), never returns the
      row to pending — the attempt fails and the printer's queue halts, so the start is never repeated by itself;
    * a queued row the refusal ENDS fails with its closed class (``error_code``) and halts its queue until Resume;
      a direct print's ends as it always has, leaving the queue to the jobs behind it;
    * a row that goes back to pending returns the one attempt its claim spent — nothing was published and nothing
      physical ran — while a crash, a failed disposition or an unknown result keeps it spent. A repeated or lost
      CAS returns nothing: the claim token it matches is gone after the first.
    """
    from backend.app.services.queue_counters import set_queue_error
    from backend.app.utils.failure_reasons import DISPATCH_SOURCE_UNAVAILABLE, DISPATCH_START_REFUSED

    swap_started = await swap_start_may_have_run(db, execution_archive_id, swap_start_effect)
    source_failed = not direct and reason in SOURCE_FAILURES
    # A refused direct print never starts on its own (no future autostart), so its
    # row is a failure with a Retry — not «cancelled», which reads as the operator's
    # own act (spec direct-print-silent-cancel §4.1).
    if disposition is None:
        terminal = direct or source_failed
    else:
        terminal = direct or disposition != DISPOSITION_RETRY_LATER
    terminal = terminal or swap_started or published
    code = None
    if terminal:
        code = failure_code or (DISPATCH_SOURCE_UNAVAILABLE if reason in SOURCE_FAILURES else DISPATCH_START_REFUSED)
    halts_queue = terminal and (not direct or swap_started)
    detail = routing_detail(reason)
    message = detail["message"]
    runtime_conflict = reason == "dispatch_claim_changed" and claim_detail == "current_run_conflict"
    if runtime_conflict:
        message = "A previous print context is blocking this start. Check print states on the System page."
    if swap_started:
        message = f"{message} The swap start macro ran, or may have, so the print is not retried by itself."

    item = await db.get(PrintQueueItem, item_id) if item_id is not None else None
    if item is None or started_at is None:
        await abort_execution_archive(db, execution_archive_id, detail, failed_code=code if swap_started else None)
        return DeferResult(False)
    wait_code = SOURCE_CAPTURE_WAIT_CODE if disposition == DISPOSITION_RETRY_LATER else "filament_unavailable"
    if runtime_conflict:
        wait_code = "dispatch_runtime_conflict"
    # Only a refusal proven to have had no effect clears the claim time: a row sent back to
    # pending, or one refused before its attempt had an execution archive (nothing reached
    # the printer — such a failed job stays refreshable, swap-source-declaration §6). Any
    # other row it ends keeps it: the claim time is the proof the row was dispatched before,
    # which a Retry keeps and ``swap_job_refresh`` reads.
    no_effect = not terminal or (execution_archive_id is None and not swap_started and not published)
    values = {
        "status": "failed" if terminal else "pending",
        "started_at": None if no_effect else started_at,
        "completed_at": datetime.now(timezone.utc) if terminal else None,
        "waiting_reason": None if terminal else message,
        "waiting_reason_code": None if terminal else wait_code,
        "waiting_reason_checked_at": None if terminal else datetime.now(timezone.utc),
        "error_message": message if terminal else None,
        "error_code": code,
    }
    if not terminal:
        # Nothing was published and nothing physical ran: the claim's attempt is given back.
        values["dispatch_attempts"] = case(
            (PrintQueueItem.dispatch_attempts > 0, PrintQueueItem.dispatch_attempts - 1), else_=0
        )
    if restore_source:
        values.update(archive_id=source_archive_id, library_file_id=source_library_file_id)
    if terminal:
        # This preparation never published a print, so there is no physical
        # failure for the next item's require_previous_success gate.
        values["gate_acknowledged"] = True
    routing = decode(item.filament_routing)
    if disposition is None and isinstance(routing, dict):
        routing["runtime"] = {"reason": reason, "blocked_revision": revision}
        values["filament_routing"] = json.dumps(routing)
    result = await db.execute(
        update(PrintQueueItem)
        .where(
            PrintQueueItem.id == item_id,
            PrintQueueItem.status == "printing",
            PrintQueueItem.started_at == started_at,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    owned = bool(result.rowcount)
    # A refusal that ENDED this attempt's row is a «Dispatch errors» attempt (D-R4 row 2):
    # its archive fails with the row's class; one that sent the row back is aborted.
    await abort_execution_archive(
        db, execution_archive_id, detail, failed_code=code if (swap_started or (terminal and owned)) else None
    )
    if not owned:
        return DeferResult(False)
    if halts_queue:
        await set_queue_error(db, item.queue_id, failed_item_id=item_id, expected_item_id=item_id)
    else:
        # The queue can already belong to a later attempt; do not release that owner.
        await db.execute(
            update(PrinterQueue)
            .where(
                PrinterQueue.id == item.queue_id,
                PrinterQueue.current_item_id == item_id,
                PrinterQueue.status == "printing",
            )
            .values(status="idle", current_item_id=None)
            .execution_options(synchronize_session=False)
        )
    await update_queue_counters(db, item.queue_id)
    return DeferResult(True, terminal, code if terminal else None, message if terminal else None, swap_started)
