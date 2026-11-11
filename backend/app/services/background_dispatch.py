"""Background dispatch for print/reprint jobs.

This service is separate from the app's print queue feature. It exists only to
decouple "send/start print" operations (FTP upload + start command) from API
request latency so the UI can continue immediately after dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import zipfile
from collections import deque
from collections.abc import Callable
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from sqlalchemy import select, update

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer

# ⚠️ Module scope on purpose: the ``finally`` below calls into this, and a
# dispatch that dies before reaching the local import would raise NameError
# from inside the cleanup — masking the real exception.
from backend.app.services import dispatch_phase, dispatch_stall_tracker, job_lineage, preheat as preheat_service
from backend.app.services.archive import ArchiveService
from backend.app.services.bambu_ftp import (
    UploadReport,
    delete_file_async,
    describe_upload_failure,
    get_ftp_retry_settings,
    list_files_async,
    upload_file_async,
    with_ftp_retry,
)
from backend.app.services.farm_startup_guard import FarmStartupRefused
from backend.app.services.filament_intake import item_descriptor, routing_detail, source_display_filename
from backend.app.services.filament_preflight import final_guard, preflight_item, settle_plan
from backend.app.services.filament_routing import RoutingDeferred
from backend.app.services.gcode_patcher import GcodeInjectionSpec
from backend.app.services.printer_files.factory import transport_for
from backend.app.services.printer_manager import printer_manager
from backend.app.services.product_sync import purge_file_product_links
from backend.app.services.queue_source_descriptor import QueueSourceDescriptor
from backend.app.services.source_io import SourceUnavailable, require_source_file, source_probe
from backend.app.utils.failure_reasons import USER_CANCELLED, dispatch_reason_text
from backend.app.utils.filename import derive_remote_filename

logger = logging.getLogger(__name__)

# Firmware accepts positive signed-int32 task identity fields only.  This is
# deliberately generated at the dispatch boundary, rather than inside MQTT,
# because the execution archive and the manager-owned run binding must know
# the exact value before the command is published.
_MAX_FIRMWARE_SUBMISSION_ID = 2_147_483_646

#: How long a dispatch waits, wall-clock, for the copy of a source that had none captured
#: (swap-source-lifecycle spec r4 §6). Beside the service's own 60 s without progress; the 30 minute bound of an
#: ordinary enqueue copy is not this one.
DISPATCH_CAPTURE_DEADLINE_SECONDS = 300


def _ensure_submission_id(job: PrintDispatchJob) -> str:
    """Return this attempt's stable firmware-safe project/subtask/task ID."""

    if job.submission_id is None:
        job.submission_id = str(secrets.randbelow(_MAX_FIRMWARE_SUBMISSION_ID) + 1)
    return job.submission_id


def _dispatch_intent(job: PrintDispatchJob, remote_filename: str, submission_id: str) -> dict[str, object]:
    """Small durable intent kept apart from printer-observed archive fields."""

    return {
        "version": 1,
        "submission_id": submission_id,
        "dispatch_job_id": job.id,
        "remote_filename": remote_filename,
        "auto_eject": job.options.get("auto_eject"),
        "auto_eject_settings": job.options.get("auto_eject_settings"),
    }


# Bambu firmware states that mean the project_file has actually been accepted
# and the printer is now processing / running / paused mid-print. Used by the
# direct-dispatch verifier (#1370 / B.3): a transition into one of these
# states means the print landed; anything else (e.g. FINISH → IDLE after the
# user dismisses a post-print prompt) is NOT a valid "command landed" signal
# even though the state value did change. Mirrors the same constant in
# print_scheduler.py — kept duplicated to avoid coupling the two services.
_ACTIVE_PRINT_STATES: frozenset[str] = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})

# The same code the printer's own screen shows, grouped the way it shows it.
_HMS_VERIFY_FAILED_DISPLAY = "0500-0500-0001-0007"

# ⚠️ Never mentions a card to a machine that does not need one. The text this
# replaced told every operator to check a card and format it FAT32/exFAT,
# including on models that print from internal storage — advice that cannot be
# followed and sends people looking for a fault that is not there.
_REFUSAL_MESSAGES = {
    "no_card_no_internal": "This printer needs an SD card to accept a print, and none is inserted.",
    "card_unusable": (
        "The SD card is present but unreadable or write-protected. Reformat it (FAT32/exFAT) or replace it."
    ),
}


def _dispatch_refusal_message(reason: str | None) -> str:
    return _REFUSAL_MESSAGES.get(reason or "", "The printer would not accept the file transfer.")


def _record_outcome_error(job, exc: BaseException) -> None:
    """Make sure the job's outcome carries a reason somebody can read.

    The runner sets ``outcome["error"]`` for anything that fails inside its own
    ``try``, but several checks run before it — and a refusal to dispatch at all
    is one of them. Without this the queue item and the Telegram notification
    both say "Dispatch failed", which tells the operator nothing about a
    missing card, a missing archive or an unreachable printer.

    Never overwrites a reason the runner already recorded.
    """
    outcome = getattr(job, "outcome", None)
    if not isinstance(outcome, dict):
        return
    if not outcome.get("error"):
        outcome["error"] = str(exc) or exc.__class__.__name__
    outcome["success"] = False


def _file_digest(path: str) -> str:
    """MD5 of the bytes we uploaded, for ``project_file``.

    ⚠️ Lowercase here; ``start_print`` upper-cases it for the MQTT command,
    which is the spelling that channel uses. Read in blocks — a 3MF is tens of
    megabytes and this runs on the dispatch path.
    """
    digest = hashlib.md5(usedforsecurity=False)  # the printer's choice of digest, not ours
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _upload_failure_message(storage: str, report: UploadReport | None = None) -> str:
    """⚠️ An upload that failed after the medium was chosen is a different
    story from a refusal before it. The card advice belongs only on the path
    that used a card — on internal storage it would send the operator looking
    for a slot the print never went near.

    On the card path the sentence comes from what the FTP client saw
    (upstream 70ee5346): the card is named only where the printer refused to
    store the file, never for a refused handshake, access code or timeout."""
    if storage == "external":
        return describe_upload_failure(report.failure if report else None)
    return "Failed to upload the file into the printer's internal storage."


def resolve_dispatch_storage(model: str | None, state) -> tuple[str | None, str | None]:
    """``(storage, reason)`` for one dispatch.

    ⚠️ Asks :func:`storage_capability_for` and nothing else. Re-deriving this
    from ``sdcard_state`` or a ``fun2`` bit here would be a second answer to a
    question that already has one, and the two would drift apart — which is
    exactly what that helper exists to prevent.

    The rule it enforces: the tunnel carries a print **only** when the printer
    reports no card. A healthy card is always FTP, and a damaged or read-only
    one refuses rather than falling back.
    """
    from backend.app.utils.printer_storage import storage_capability_for

    capability = storage_capability_for(model, state)
    return capability["print_target"], capability["reason"]


async def delete_internal_by_name(transport, *names: str, may_run: Callable[[], bool] | None = None) -> bool:
    """Delete the first file matching any of ``names`` from internal storage.

    ⚠️ **Internal-only, because only internal storage needs a lookup.** Over FTP
    the printer's root is the card, so the name *is* the path and callers delete
    it directly — with the configured socket timeout, which matters on the
    slower A1 controllers. Here the tunnel deletes by the absolute path its own
    listing reports (``/userdata/model/history/<name>``), which cannot be
    guessed: deleting by bare name silently does nothing and leaves the file in
    place.

    Used both before an upload (clear a same-named file) and after a print (the
    operator asked for cleanup) — one lookup, one meaning.

    Best-effort and never raises: a missing file is the normal case, and every
    caller has its own failure path for a printer that cannot be reached.
    """
    wanted = {n for n in names if n}
    if not wanted:
        return False
    try:
        for entry in await transport.list_files("/"):
            if may_run is not None and not may_run():
                return False
            if entry.name in wanted:
                await transport.delete(entry.path, **({"may_run": may_run} if may_run is not None else {}))
                return True
    except Exception as exc:  # noqa: BLE001 — cleanup must never fail its caller
        logger.debug("Internal-storage delete of %s skipped: %s", ", ".join(sorted(wanted)), exc)
    return False


class PrintCommandRejectedError(RuntimeError):
    """The printer refused the command outright — waiting will not help.

    Distinguished from the ordinary "did not acknowledge" timeout because the
    remedies are opposite. A timeout might come good on a retry; a printer
    reporting ``HMS_MQTT_VERIFY_FAILED`` will refuse this job and every other
    one until Developer Mode is enabled and the printer restarted, so spending
    the rest of the dispatch window and two more 3MF uploads only burns an
    upload slot the rest of the farm is queued behind (#2732).

    A ``RuntimeError`` subclass on purpose: both dispatch paths already raise
    ``RuntimeError`` here, so the failure is handled exactly as before — only
    the message a user reads changes.
    """


#: What one attempt's start command did, as far as this process knows (post-rebuild D-R2): the RAM half of
#: ``services/dispatch_phase``. Written where the runner learns it — after ``start_print`` returns with no await
#: before the write, and after the printer was SEEN running — and read by every exit of the attempt, so a
#: failure in what follows a publish is never mistaken for a print that did not go out.
SEND_REFUSED = "refused"  # ``start_print`` published nothing
SEND_SENT = "sent"  # queued for publish locally — not yet a print
SEND_ACCEPTED = "accepted"  # the printer was seen running it

#: The printer's answer to a published start (``_await_print_response``, D-R4). Only the first is a print.
RESPONSE_ACCEPTED = "accepted"
#: (a) no Phase A, no active state, the file name unchanged: nothing says the command arrived.
RESPONSE_NO_ACKNOWLEDGEMENT = "no_acknowledgement"
#: (b) the printer's file changed without Phase A: a slow parse (#1150) — it may still start.
RESPONSE_FILE_RECEIVED = "file_received"
#: (c) Phase A (the printer echoed our submission) and never an active state (#1678).
RESPONSE_STALLED = "stalled_after_accept"

#: Our own observer failing after a sent start is no verdict of the printer's (D-R4): the runner
#: looks again with the same claim, at least this many looks AND at least the printer's own
#: answer window (the observer's acknowledgement wait plus its start wait after the file landed),
#: then stops watching visibly — the start is never sent again and the attempt is never called
#: unsent. Failing fast never ends the watch while a printer still reading the file may start it.
OBSERVER_MAX_ATTEMPTS = 3
OBSERVER_RETRY_SECONDS = 5.0
OBSERVER_WATCH_SECONDS = 90.0 + 180.0

#: Execution archives whose sent start no runner of THIS process watches any more: the runner
#: ended without the printer's verdict. The live owner says so itself (it is never called
#: ended), so Info may offer the operator its close without a restart. Process memory: after
#: a restart the ended owner's own proof takes over.
_unobserved_attempts: set[int] = set()


def mark_unobserved(archive_id: int | None) -> None:
    if archive_id is not None:
        _unobserved_attempts.add(archive_id)


def attempt_unobserved(archive_id: int | None) -> bool:
    return archive_id is not None and archive_id in _unobserved_attempts


#: Which branch refused a ``dispatch_claim_changed`` (spec printer-runtime-stall r3 R4) -- one per raise site.
#: Diagnostics only: the public reason and the operator's sentence are the same for all of them.
CLAIM_DETAILS = frozenset(
    {
        "claim_missing",  # the job's row is gone
        "claim_released",  # the row is no longer printing (cancelled, released, completed)
        "claim_restarted",  # the row was claimed again: another attempt owns it
        "routing_changed",  # the operator changed the filament routing
        "auto_eject_changed",  # the operator changed auto-eject or its settings
        "source_link_changed",  # the row was re-pointed while its copy was taken
        "current_run_conflict",  # another run is bound to the printer (``bind_prepared_print_run``)
    }
)


def _claim_change_detail(item, job) -> str | None:
    """The first reason this attempt no longer owns its row, or ``None`` while it still does."""
    if item is None:
        return "claim_missing"
    if item.status != "printing":
        return "claim_released"
    if item.started_at != job.claim_started_at:
        return "claim_restarted"
    if item.filament_routing != job.routing_intent:
        return "routing_changed"
    if bool(item.auto_eject) != bool(job.options.get("auto_eject")) or item.auto_eject_settings != job.options.get(
        "auto_eject_settings"
    ):
        return "auto_eject_changed"
    return None


def _bind_conflict(printer_id: int) -> RoutingDeferred:
    """The refusal of a prepared bind, naming the run that holds the printer (read without an await)."""
    from backend.app.services.print_run_binding import current_print_run

    holder = current_print_run(printer_manager, printer_id)
    return RoutingDeferred(
        "dispatch_claim_changed",
        detail="current_run_conflict",
        blocker_archive_id=holder.archive_id if holder is not None else None,
    )


#: Where an attempt's untyped failure happened, read by :func:`dispatch_failure_code`.
STAGE_PREPARING = "preparing"


class StartRefused(RoutingDeferred):
    """``start_print`` published nothing: the printer was busy or not connected at the last moment.

    A typed refusal, not a failure: it travels the deferral path every refusal before
    publish takes, and the attempt it spent is given back when nothing physical ran.
    """


class DispatchNotStarted(RuntimeError):
    """The start was published and the printer did not start it — the attempt ends here.

    Carries the closed class (``utils/failure_reasons``) the watchdog verdict decided, so
    nothing reads it back out of the sentence. Never retried by itself: the printer may
    still pick the file up, and only the operator can know.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class UploadFailed(RuntimeError):
    """The file did not reach the printer: a refused FTP pre-delete, upload or tunnel transfer."""


class PrinterUnavailable(RuntimeError):
    """No printer, no connection or no medium to dispatch to."""


class PreparationFailed(RuntimeError):
    """A step between the upload and the start refused: a rack pick, the swap-start macro."""


class SourceRejected(RuntimeError):
    """The bytes are not something the printer can print (not a sliced file)."""


class DispatchPhaseLost(RuntimeError):
    """The ``sending`` mark could not be committed, so nothing may be sent."""


class DispatchObservationLost(RuntimeError):
    """Every bounded look at the printer after the sent start failed: its outcome is unknown.

    Not a verdict (``_ends_after_publish``): the attempt is left as the printer may have it,
    marked unobserved for the operator.
    """


def dispatch_failure_code(exc: BaseException | None, *, stage: str | None = None) -> str:
    """The closed class of an attempt that ended on ``exc`` — from its TYPE, never from its text (D-R6)."""
    from backend.app.services.filament_deferred import SourceCaptureDeferred, SwapSourceRefused
    from backend.app.services.queue_sources import QueueSourceError
    from backend.app.services.source_io import SOURCE_FAILURES
    from backend.app.utils import failure_reasons as reasons

    if isinstance(exc, DispatchNotStarted):
        return exc.code
    if isinstance(exc, PrintCommandRejectedError):
        return reasons.DISPATCH_COMMAND_REJECTED
    if isinstance(exc, UploadFailed):
        return reasons.DISPATCH_UPLOAD_FAILED
    if isinstance(exc, PrinterUnavailable):
        return reasons.DISPATCH_PRINTER_UNAVAILABLE
    if isinstance(exc, (StartRefused, SwapSourceRefused)):
        return reasons.DISPATCH_START_REFUSED
    if isinstance(exc, (SourceCaptureDeferred, SourceUnavailable, QueueSourceError, SourceRejected)):
        return reasons.DISPATCH_SOURCE_UNAVAILABLE
    if isinstance(exc, RoutingDeferred):
        return reasons.DISPATCH_SOURCE_UNAVAILABLE if exc.reason in SOURCE_FAILURES else reasons.DISPATCH_START_REFUSED
    if isinstance(exc, DispatchPhaseLost):
        return reasons.DISPATCH_INTERNAL_ERROR
    if isinstance(exc, PreparationFailed) or stage == STAGE_PREPARING:
        return reasons.DISPATCH_PREPARATION_ERROR
    return reasons.DISPATCH_INTERNAL_ERROR


def _published(job) -> bool:
    """The start may have reached the printer."""
    return job.send_fact in (SEND_SENT, SEND_ACCEPTED)


def _ends_after_publish(exc: BaseException) -> bool:
    """A verdict that may end an attempt whose start was published: the printer's own answer."""
    return isinstance(exc, (DispatchNotStarted, PrintCommandRejectedError))


def _mqtt_commands_rejected(status) -> bool:
    """True when the printer is currently reporting that it refused a command.

    Tolerates a missing status and errors carrying no ``full_code`` (the 8-char
    ``print_error`` path builds :class:`HMSError` differently), so this is safe
    to call on every watchdog poll.
    """
    from backend.app.services.bambu_mqtt import HMS_MQTT_VERIFY_FAILED

    for err in getattr(status, "hms_errors", None) or []:
        if getattr(err, "full_code", "") == HMS_MQTT_VERIFY_FAILED:
            return True
    return False


async def _warn_on_filament_deficit(db, job, archive) -> None:
    """Tell the operator this print will empty a slot, and let it run.

    ⚠️ **Best-effort, and silent on any failure.** A warning is worth having; it
    is not worth a dispatch. Everything it reads — requirements from the 3MF,
    the printer's live slots, inventory remaining — can be unavailable for
    ordinary reasons, and none of them is a reason to stop a print.
    """
    try:
        from backend.app.services.filament_deficit import compute_shortfalls
        from backend.app.services.print_scheduler import scheduler as _sched

        mapping = job.options.get("ams_mapping")
        if not mapping:
            return

        status = printer_manager.get_status(job.printer_id)
        if status is None:
            return

        item = getattr(job, "queue_item", None)
        requirements = await _sched._get_filament_requirements(db, item) if item is not None else None
        if not requirements:
            return

        loaded = _sched._build_loaded_filaments(status, job.printer_id)
        if not loaded:
            return

        remaining = await _sched._build_inventory_remain_overrides(db, job.printer_id, loaded)
        # ⚠️ Only slots whose remaining we actually track are judged; see the
        # module docstring on why silence must not read as empty.
        shortfalls = compute_shortfalls(
            requirements,
            loaded,
            mapping,
            remaining or {},
            # ⚠️ ``ams_auto_switch_filament`` is what the state actually calls it
            # (home_flag bit 10, BS AutoRefill). Guessing the name here would
            # have silently disabled the backup pooling — getattr returns the
            # default and nothing complains.
            auto_refill=bool(getattr(status, "ams_auto_switch_filament", False)),
        )
        if not shortfalls:
            return

        printer = printer_manager.get_printer(job.printer_id)
        printer_name = getattr(printer, "name", None) or f"Printer {job.printer_id}"
        print_name = archive.print_name or archive.filename or ""

        logger.warning(
            "Filament deficit on %s for %r: %s",
            printer_name,
            print_name,
            ", ".join(f"{s.slot_label} short by {s.missing_grams}g" for s in shortfalls),
        )
        from backend.app.services.notification_service import notification_service

        await notification_service.on_filament_deficit(job.printer_id, printer_name, print_name, shortfalls, db)
        await ws_manager.send_filament_deficit(
            job.printer_id,
            printer_name,
            print_name,
            [
                {
                    "slot": s.slot_label,
                    "needed": s.needed_grams,
                    "available": s.available_grams,
                    "missing": s.missing_grams,
                }
                for s in shortfalls
            ],
        )
    except Exception:  # noqa: BLE001 - a warning must never cost a dispatch
        logger.debug("Filament deficit check failed for printer %s", job.printer_id, exc_info=True)


def _bind_target(printer_id: int, ams_id: int, slot_id: int, filament_id: str, nozzle, spool) -> tuple[tuple, dict]:
    """One slot the pre-print bind binds: its key, and the kwargs it binds with.

    The key is what the bind READS — the slot, the effective filament (the
    assigned spool's family, else the slot's live profile id), the spool and the
    nozzle it feeds — so ``_rebind_if_moved`` can tell whether it would bind
    something else now.
    """
    kwargs = {
        "printer_id": printer_id,
        "ams_id": ams_id,
        "slot_id": slot_id,
        "filament_id": filament_id,
        "nozzle_diameter": nozzle.diameter_float,
        "nozzle_volume_type": nozzle.flow_or_standard,
        "extruder_id": nozzle.extruder_or_default,
        "spool_id": spool.id if spool else None,
    }
    key = (
        ams_id,
        slot_id,
        filament_id,
        kwargs["spool_id"],
        kwargs["nozzle_diameter"],
        kwargs["nozzle_volume_type"],
        kwargs["extruder_id"],
    )
    return key, kwargs


async def _calibration_targets(
    db,
    printer_id: int,
    ams_mapping: list[int] | None,
    is_calibration: bool = False,
) -> list[tuple[tuple, dict]]:
    """Every slot the pre-print bind would bind now, as ``(key, bind kwargs)``.

    Reads only — the printer's state, the slot assignments and the catalogue;
    ``_apply_calibrations_for_print`` binds what this returns.
    """
    if is_calibration:
        return []
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        return []

    from backend.app.models.spool_assignment import SpoolAssignment as SA
    from backend.app.services.calibration_service import derive_effective_filament_id
    from backend.app.utils.slot_nozzle import slot_nozzle

    state = printer_manager.get_status(printer_id)
    if not state:
        return []

    ams_raw = (state.raw_data or {}).get("ams", [])
    if isinstance(ams_raw, dict):
        ams_raw = ams_raw.get("ams", [])
    if not isinstance(ams_raw, list):
        ams_raw = []

    used_global: set[int] | None = None
    if ams_mapping is not None:
        used_global = {int(s) for s in ams_mapping if isinstance(s, int) and s >= 0}

    from sqlalchemy.orm import selectinload as _sl

    targets: list[tuple[tuple, dict]] = []
    for unit in ams_raw:
        if not isinstance(unit, dict):
            continue
        try:
            ams_id = int(unit.get("id", -1))
        except (TypeError, ValueError):
            continue
        if ams_id < 0:
            continue
        for tray in unit.get("tray", []) or []:
            if not isinstance(tray, dict):
                continue
            try:
                slot_id = int(tray.get("id", -1))
            except (TypeError, ValueError):
                continue
            if slot_id < 0:
                continue
            # An AMS-HT unit is one slot whose global id IS the unit id (128+),
            # as in the plan's mapping — ``ams_id * 4`` never matched it, so HT
            # slots were never bound before a print.
            global_slot = ams_id if ams_id >= 128 else ams_id * 4 + slot_id
            if used_global is not None and global_slot not in used_global:
                continue
            tray_info_idx = tray.get("tray_info_idx") or ""
            # The nozzle THIS slot feeds — diameter and flow type included.
            nozzle = slot_nozzle(state, ams_id, slot_id)

            assignment_row = (
                await db.execute(
                    select(SA)
                    .options(_sl(SA.spool))
                    .where(
                        SA.printer_id == printer_id,
                        SA.ams_id == ams_id,
                        SA.tray_id == slot_id,
                    )
                )
            ).scalar_one_or_none()
            spool = assignment_row.spool if assignment_row else None

            filament_id = await derive_effective_filament_id(
                spool=spool, slot_tray_info_idx=tray_info_idx or None, db=db
            )
            if not filament_id:
                continue
            targets.append(_bind_target(printer_id, ams_id, slot_id, filament_id, nozzle, spool))

    # External slots are always available regardless of AMS presence (operator
    # can mid-print swap to external on an AMS-equipped X1C, and no-AMS
    # printers like A1 Mini only have external). ``vt_tray`` lists them:
    # ``id=254`` for single-external (X1C / P1S / A1 / A1 Mini), ``id=255``
    # for the second slot on H2D dual-external. ``ams_mapping`` doesn't
    # carry external-slot info (the no-AMS branch in bambu_mqtt remaps it to
    # ``[0]`` as a firmware placeholder), so we don't filter external slots
    # by ``used_global`` — bind every populated vt slot and let the
    # active-calibration resolver decide: explicit spool_k_profile link
    # wins, else fallback to the per-(filament_id, nozzle, vol, extruder)
    # active row, else no-op (silent — that's the contract).
    vt_tray_raw = (state.raw_data or {}).get("vt_tray", []) or []
    if isinstance(vt_tray_raw, list):
        for vt in vt_tray_raw:
            if not isinstance(vt, dict):
                continue
            try:
                vt_id = int(vt.get("id", -1))
            except (TypeError, ValueError):
                continue
            if vt_id not in (254, 255):
                continue
            ext_slot = vt_id - 254  # 254→0, 255→1
            tray_info_idx = vt.get("tray_info_idx") or ""
            # Ext-L feeds the left hotend on a dual-nozzle printer — it used to
            # bind as extruder 0 against the right one's diameter.
            nozzle = slot_nozzle(state, 255, ext_slot)
            assignment_row = (
                await db.execute(
                    select(SA)
                    .options(_sl(SA.spool))
                    .where(
                        SA.printer_id == printer_id,
                        SA.ams_id == 255,
                        SA.tray_id == ext_slot,
                    )
                )
            ).scalar_one_or_none()
            spool = assignment_row.spool if assignment_row else None

            filament_id = await derive_effective_filament_id(
                spool=spool, slot_tray_info_idx=tray_info_idx or None, db=db
            )
            if not filament_id:
                continue
            targets.append(_bind_target(printer_id, 255, ext_slot, filament_id, nozzle, spool))
    return targets


async def _apply_calibrations_for_print(
    db,
    printer_id: int,
    ams_mapping: list[int] | None,
    is_calibration: bool = False,
) -> frozenset:
    """Pre-print bind hook. For every AMS slot the job will use, resolve the
    active calibration and fire ``extrusion_cali_sel``.

    Closes the silent-drift gap when prints start without going through the
    spool-link / RFID paths (queued prints, scheduled prints, manual restarts).
    Calibration prints skip this — their wizard's ``save_result`` runs its own
    bind. Best-effort: failures are logged, never block ``start_print``.

    Returns the keys of what it bound (``_bind_target``), for
    ``_rebind_if_moved``.
    """
    from backend.app.services.calibration_service import apply_active_calibration_to_slot

    targets = await _calibration_targets(db, printer_id, ams_mapping, is_calibration)
    for _key, kwargs in targets:
        try:
            await apply_active_calibration_to_slot(db=db, **kwargs)
        except Exception as e:
            if kwargs["ams_id"] == 255:
                logger.warning(
                    "Pre-print apply (external) failed printer=%s vt_slot=%s: %s",
                    printer_id,
                    kwargs["slot_id"],
                    e,
                )
            else:
                logger.warning(
                    "Pre-print apply failed printer=%s ams=%s slot=%s: %s",
                    printer_id,
                    kwargs["ams_id"],
                    kwargs["slot_id"],
                    e,
                )
    return frozenset(key for key, _ in targets)


async def _calibration_bind_keys(db, printer_id: int, ams_mapping, is_calibration: bool = False) -> frozenset:
    """What the pre-start bind would bind now, without binding it."""
    return frozenset(key for key, _ in await _calibration_targets(db, printer_id, ams_mapping, is_calibration))


def _session_generation(printer_id: int) -> int:
    """The MQTT session a command sent now would reach."""
    return printer_manager.get_feed_snapshot(printer_id).generation


async def _rebind_if_moved(
    db, *, printer_id, ams_mapping, is_calibration, bound, bound_generation
) -> tuple[frozenset, int]:
    """Send the pre-start K bind again when it went to another session or another spool.

    ``bound_generation`` is the session the last bind was sent on (read just
    before sending it). A session that changed since dropped it
    (``_on_connect`` discards what paho was still retrying), so it is re-sent;
    the bind is also keyed on what IT reads — the assigned spool, the slot's live
    profile id and its nozzle — not on the routing feed: a spool re-assigned in
    a slot whose tray looks the same changes nothing in ``FeedSource`` but
    everything about the K it needs (spec dispatch-guard-follows-the-plan Д5).
    Re-sending is safe: ``extrusion_cali_sel`` is idempotent.

    Returns the keys bound and the session they were sent on, which
    ``final_guard`` holds the start to (final review I2).
    """
    generation = _session_generation(printer_id)
    if (
        generation != bound_generation
        or await _calibration_bind_keys(db, printer_id, ams_mapping, is_calibration) != bound
    ):
        keys = await _apply_calibrations_for_print(
            db=db, printer_id=printer_id, ams_mapping=ams_mapping, is_calibration=is_calibration
        )
        return keys, generation
    return bound, bound_generation


async def _record_dispatched_mapping(job) -> None:
    """The queue row keeps the mapping the printer was actually sent.

    The scheduler's preflight wrote its own plan's mapping; the dispatcher's may
    have chosen differently (a backup twin, a lower-remain spool), and the row's
    copy is the attribution fallback (``usage_tracker``) and what
    ``unlose_external_markers`` reads. A row without a routing intent re-reads
    its PINS from this column, so it is left alone — it never gets a twin anyway
    (``allow_backup_twins``). Best-effort: the print has started.
    """
    from backend.app.models.print_queue import PrintQueueItem

    plan = getattr(job.routing_guard, "plan", None) if job.routing_guard else None
    if plan is None or job.queue_item_id is None:
        return
    try:
        async with async_session() as db:
            await db.execute(
                update(PrintQueueItem)
                .where(
                    PrintQueueItem.id == job.queue_item_id,
                    PrintQueueItem.started_at == job.claim_started_at,
                    PrintQueueItem.filament_routing.is_not(None),
                )
                .values(ams_mapping=json.dumps(plan.mapping), use_ams=plan.use_ams)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — never fail a print that has started
        logger.warning("Dispatch %s: could not record the dispatched mapping", getattr(job, "id", None), exc_info=True)


def _timelapse_or_off(printer_id: int, printer, requested: bool) -> bool:
    """The timelapse flag actually sent, with the printer's own veto applied.

    ⚠️ **The browser disabling the checkbox is not the guard.** This path is also
    reached from the API, from the Telegram bot, and from a queue item created
    hours before somebody pulled the SD card out — the same reasoning as every
    other gate that refuses to live only in one client.

    A refusal is silent by design: BambuStudio's own answer to "this printer
    cannot record one" is to untick the box, not to abandon the print. Losing a
    timelapse is not a reason to not print.
    """
    if not requested:
        return False

    from backend.app.services.printer_manager import printer_manager as _pm
    from backend.app.utils.timelapse import capability_for

    client = _pm.get_client(printer_id)
    if client is None:
        return requested

    capability = capability_for(getattr(printer, "model", None), client.state)
    if capability.get("can_enable"):
        return True

    logger.info(
        "[%s] Timelapse was requested but the printer cannot record one (%s) — sending it off",
        printer_id,
        capability.get("reason"),
    )
    return False


def _timelapse_storage_for(printer_id: int, requested: str | None) -> str | None:
    """The medium the recording goes to, re-checked against the live machine.

    ⚠️ **Resolved here and not at the dialog**, for the same reason
    :func:`_timelapse_or_off` is: the choice was made whenever the item was
    queued, and the card can be pulled out in between. BambuStudio re-applies
    the same fallback every time it opens its picker; we get one chance, at
    dispatch.

    ``None`` — no live client, an unsupported machine, or nothing chosen —
    leaves the field at its old value and the medium to the printer, which is
    what every dispatch did before the picker existed.
    """
    if requested is None:
        return None

    from backend.app.services.printer_manager import printer_manager as _pm
    from backend.app.utils.timelapse import SDCARD_NONE, resolve_storage

    client = _pm.get_client(printer_id)
    if client is None:
        return None

    state = client.state
    support = getattr(state, "print_option_support", None) or {}
    resolved = resolve_storage(
        requested=requested,
        supports_internal_timelapse=bool(support.get("internal_timelapse")),
        sdcard_state=int(getattr(state, "sdcard_state", SDCARD_NONE) or 0),
    )
    if resolved != requested:
        logger.info(
            "[%s] Timelapse target %r is not available — recording to %s instead",
            printer_id,
            requested,
            resolved or "wherever the printer chooses",
        )
    return resolved


class DispatchJobCancelled(Exception):
    """Raised when a dispatch job is cancelled by the user."""


class DispatchEnqueueRejected(Exception):
    """Raised when a dispatch job should not be accepted."""

    def __init__(self, message: str, *, code: str | None = None, printer_id: int | None = None):
        super().__init__(message)
        self.code = code
        self.printer_id = printer_id

    def detail(self) -> str | dict:
        if self.code is None:
            return str(self)
        return {"code": self.code, "message": str(self), "printer_id": self.printer_id}


class DispatchOutcome(TypedDict):
    success: bool
    archive_id: int | None
    error: str | None
    cancelled: bool
    deferred: NotRequired[bool]
    reason: NotRequired[dict]
    #: The closed class of a failed attempt (``utils/failure_reasons``).
    failure_code: NotRequired[str]
    #: The start may have been published and nothing decided what it became: an
    #: exit that is neither a print nor a failure — no release, no pending, no report.
    uncertain: NotRequired[bool]


@dataclass(slots=True)
class PrintDispatchJob:
    id: int
    kind: Literal["reprint_archive", "print_library_file"]
    #: The ORIGINAL archive / library row this dispatch was started from, when
    #: one still exists. ``None`` is an ordinary answer since m173: a job backed
    #: by a captured source needs no original, and the runner then looks nothing
    #: up rather than resolving an id whose row may be somebody else's by now.
    source_id: int | None
    source_name: str
    printer_id: int
    printer_name: str
    options: dict[str, Any] = field(default_factory=dict)
    requested_by_user_id: int | None = None
    requested_by_username: str | None = None
    project_id: int | None = None
    # The order line this print is for, carried from the request that started it
    # all the way to the archive row the runner writes.
    project_line_id: int | None = None
    cleanup_library_after_dispatch: bool = False
    # Link back to a ``print_queue.id`` when the dispatch was requested by
    # the scheduler for a queue item.  The runner updates the queue item's
    # ``archive_id`` once the archive row is created so the two FSMs stay
    # in sync without a second DB round-trip from the scheduler.
    queue_item_id: int | None = None
    # True only for jobs the scheduler is awaiting through
    # ``run_from_queue_item``. ⚠️ Not the same question as
    # ``queue_item_id is not None`` — a direct print now carries a queue item
    # too (it is how it claims the printer), so that older test would silence
    # the only failure report a Print now ever gets.
    awaited_by_scheduler: bool = False
    # Signalled at the very end of ``_run_*`` (success / failure / cancel)
    # so ``run_from_queue_item`` callers can await the outcome.
    completion_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Populated by the runner before it sets ``completion_event``.  Shape:
    # ``{"success": bool, "archive_id": int | None, "error": str | None, "cancelled": bool}``.
    outcome: DispatchOutcome = field(
        default_factory=lambda: {
            "success": False,
            "archive_id": None,
            "error": None,
            "cancelled": False,
            "deferred": False,
        }
    )
    # The bytes this dispatch prints, read off the queue row by
    # ``_prepare_filament_routing`` before anything opens a file (spec §7, S2).
    # ``None`` means a legacy row (or one of §2's exemptions — an external print,
    # a calibration asset), which still reads its original.
    source: QueueSourceDescriptor | None = None
    routing_guard: Any = None
    claim_started_at: Any = None
    original_archive_id: int | None = None
    original_library_file_id: int | None = None
    execution_archive_id: int | None = None
    # Generated once at the execution boundary and threaded unchanged to the
    # archive intent, run binding and Bambu ``project_file`` payload.
    submission_id: str | None = None
    routing_intent: str | None = None
    foreign_claim: bool = False
    #: Set once ``report_failure_if_unwatched`` has told the operator: the runner's
    #: ``finally`` reports failures inside its ``try`` and ``_run_active_job``
    #: reports the ones raised before it — one failure, one message.
    failure_reported: bool = False
    #: What our swap-start macro really did on this attempt: ``not_called``, ``possibly_fired`` (sent, not
    #: confirmed) or ``fired``. The watchdog and the operator's texts read this, never the requested events
    #: (swap-source-lifecycle spec r4 §8): a muted or refused start did not happen.
    swap_start_effect: str = "not_called"
    #: ``SEND_REFUSED`` / ``SEND_SENT`` / ``SEND_ACCEPTED`` once the start was tried; None before (D-R2).
    send_fact: str | None = None
    #: The boot stamped with ``prepared`` on the execution archive: every later phase write must match it.
    dispatch_boot_id: str | None = None
    #: ``STAGE_PREPARING`` from the moment the file is on the printer until the start command.
    stage: str | None = None
    #: Bookkeeping that failed after the start was published; logged, never the print's outcome.
    secondary_errors: list[str] = field(default_factory=list)
    #: The process stopped this attempt (shutdown): nothing here is a failure to report.
    interrupted: bool = False
    #: The printer record's life when the job was made (``printer_manager.printer_life``), proved before every
    #: connect, the bind and the start: a deleted printer's job never acts for the printer created with its id
    #: (spec gh109-o1 r2 R1). Never re-read from the manager later -- that would be the next record's.
    printer_life: int | None = None

    def __post_init__(self) -> None:
        if self.printer_life is None:
            self.printer_life = printer_manager.printer_life(self.printer_id)


async def report_failure_if_unwatched(job: PrintDispatchJob) -> None:
    """Tell the operator a direct print failed, because nobody else will.

    ``notification_service.on_queue_job_failed`` is reached from exactly two
    places, both in ``print_scheduler`` — so only a **queue** job that fails to
    start is ever announced. A print started from the archive's Reprint button,
    from the library, or from the Telegram bot enqueues a dispatch job and the
    HTTP call returns ``dispatched`` immediately; if the upload or the start
    then fails, the failure goes to the dispatch panel over the websocket and
    nowhere else. Whoever pressed the button learns nothing, unless they happen
    to be looking at that panel at that moment.

    ⚠️ **Gated on ``awaited_by_scheduler``** — that is the whole discriminator.
    A job dispatched for a queue item by the scheduler is already awaited and
    reported by it, and announcing here would notify twice for one failure. It
    used to read ``queue_item_id is None`` instead, which stopped meaning
    "direct print" the moment a direct print started claiming the printer with
    a queue item of its own.

    ⚠️ Cancellations are not failures. The operator who pressed Cancel does not
    need to be told what they just did.

    ⚠️ Deferrals are skipped HERE, not ignored: ``announce_not_started`` reports a
    refused direct print from ``_handle_routing_deferred``, where the reason
    exists as a sentence.

    Never raises: this runs on the way out of a dispatch that has already gone
    wrong, and a notification provider being unreachable must not replace the
    real error with its own.
    """
    if job.awaited_by_scheduler:
        return
    outcome = job.outcome or {}
    if outcome.get("success") or outcome.get("cancelled") or outcome.get("deferred"):
        return
    # ⚠️ A shutdown, or a start that may have gone out with nothing deciding its fate,
    # is not a failed print: the seed outcome would announce one that may be running.
    if job.interrupted or outcome.get("uncertain"):
        return
    if job.failure_reported:
        return
    job.failure_reported = True

    try:
        from backend.app.core.database import async_session
        from backend.app.services.notification_service import notification_service

        async with async_session() as db:
            await notification_service.on_queue_job_failed(
                job_name=job.source_name,
                printer_id=job.printer_id,
                printer_name=job.printer_name,
                reason=dispatch_reason_text(
                    outcome.get("failure_code"), str(outcome.get("error") or "Dispatch failed")
                ),
                db=db,
            )
    except Exception as exc:  # noqa: BLE001 — reporting a failure must not raise a second one
        logger.warning("Could not announce the failed dispatch of %s: %s", job.source_name, exc)


async def announce_not_started(job: PrintDispatchJob, reason: str) -> None:
    """Tell the operator a direct print was refused before it started.

    ⚠️ Not ``report_failure_if_unwatched``: that one runs in the runner's
    ``finally``, before ``_handle_routing_deferred`` has put the refusal into
    words — it would announce ``feed_state_unavailable`` instead of a sentence.
    This runs where the sentence exists, for the one kind of job nobody else
    will report: a direct print, which never starts on its own after a refusal
    (spec direct-print-silent-cancel §4.1; the 2026-09-24 reprint that nobody
    heard about).

    Never raises: a provider being unreachable must not replace the refusal.
    """
    try:
        from backend.app.services.notification_service import notification_service

        async with async_session() as db:
            await notification_service.on_queue_job_failed(
                job_name=job.source_name,
                printer_id=job.printer_id,
                printer_name=job.printer_name,
                reason=reason,
                db=db,
            )
    except Exception as exc:  # noqa: BLE001 — reporting a refusal must not raise a second error
        logger.warning("Could not announce the refused dispatch of %s: %s", job.source_name, exc)


@dataclass(slots=True)
class ActiveDispatchState:
    job: PrintDispatchJob
    message: str
    phase: str = "preparing"
    upload_bytes: int | None = None
    upload_total_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class RackDispatch:
    """What the nozzle-rack step hands ``start_print``, or why it will not.

    At most one field is set. ``nozzle_mapping`` is a wire mapping resolved from
    the file's rack plan and the operator's pick (upstream #1784);
    ``slot_extruders`` is the #2800 fallback, JSON, whose live dock the MQTT
    layer reads; ``refusal`` is an explicit pick the rack no longer fits. All
    None means no ``nozzle_mapping`` goes out and the firmware picks.
    """

    nozzle_mapping: list[int] | None = None
    slot_extruders: str | None = None
    refusal: str | None = None


def _rack_choice(raw) -> dict[int, int]:
    """The pick as ``{group: position}`` from either form a job carries it in.

    A queue item's column is JSON text, whose object keys are strings; a direct
    print's request hands the parsed dict. Anything unreadable is no pick at all
    -- the positions are then assigned -- never a refusal: the operator cannot
    have meant a value nobody can read.
    """
    if not raw:
        return {}
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Unreadable nozzle_rack_choice %r; assigning rack positions instead", raw)
            return {}
    if not isinstance(value, dict):
        return {}
    choice: dict[int, int] = {}
    for key, position in value.items():
        try:
            choice[int(key)] = int(position)
        except (TypeError, ValueError):
            continue
    return choice


def _rack_dispatch(printer, file_path, plate_id, nozzle_mapping, rack_choice, live_rack) -> RackDispatch:
    """Resolve the nozzle-rack part of a dispatch, or say why it cannot go out.

    ⚠️ **Derived at dispatch, not at queue time** -- the first point that knows
    the actual printer, the actual file and the rack as it stands now. An item
    can be created without a printer, reassigned afterwards, or have its file
    swapped for a G-code-injected copy, and a rack can be re-loaded from the
    touchscreen between queueing and printing.

    Two attempts, in order:

    1. **The rack plan** (upstream #1784): which filament groups the plate has
       and what hotend each needs, paired with the operator's position pick --
       groups the pick does not name are assigned, preferring a position
       already carrying the group's colour. The only path that can place a
       plate needing several rack hotends. An EXPLICIT pick that no longer fits
       is a refusal: the operator named a hotend, and printing from another one
       is how a plate is levelled on one nozzle and drawn millimetres above the
       bed by another. An assignment that cannot be made is not -- nothing was
       promised -- and falls through.
    2. **The #2800 fallback**: the carriage per slot, which the MQTT layer
       resolves against the live dock. It withholds a plate that needs two rack
       hotends (``_plate_needs_several_rack_nozzles``), and the firmware picks.

    Skipped when the job already carries a BambuStudio capture from the Virtual
    Printer: the slicer's own pick outranks anything derived here.

    Takes the file **actually being sent** -- the patched copy when G-code
    injection or an M970 rewrite produced one. The patcher does not touch
    ``slice_info.config`` today, so the two agree; reading the dispatched file is
    what keeps that from mattering if it ever stops being true.

    Nothing here raises for a bad file or a bad pick: every reader answers None
    for what it cannot read, and the caller turns only ``refusal`` into a failed
    dispatch.
    """
    if nozzle_mapping or file_path is None:
        return RackDispatch()
    from backend.app.utils.printer_models import is_nozzle_rack_model

    if not is_nozzle_rack_model(getattr(printer, "model", None)):
        return RackDispatch()

    from backend.app.services.bambu_mqtt import resolve_rack_plan_mapping
    from backend.app.utils.threemf_tools import extract_rack_plan_from_3mf, extract_slot_extruders_from_3mf

    plate = plate_id or 1
    plan = extract_rack_plan_from_3mf(file_path, plate_id=plate)
    if plan is not None:
        choice = _rack_choice(rack_choice)
        wire, error = resolve_rack_plan_mapping(plan.slot_groups, plan.group_dicts(), choice, live_rack)
        if wire is not None:
            logger.info(
                "Nozzle rack mapping for %s: %s (groups %s, chosen %s)",
                getattr(file_path, "name", file_path),
                wire,
                plan.slot_groups,
                choice or "auto",
            )
            return RackDispatch(nozzle_mapping=wire)
        if choice:
            logger.warning(
                "Refusing a rack pick that no longer fits: %s (chose %s, rack ids %s)",
                error,
                choice,
                [slot.get("id") for slot in live_rack if isinstance(slot, dict)],
            )
            return RackDispatch(refusal=error)
        logger.info("No rack positions assignable (%s); falling back to the live dock", error)

    slot_extruders = extract_slot_extruders_from_3mf(file_path, plate_id=plate)
    return RackDispatch(slot_extruders=json.dumps(slot_extruders) if slot_extruders else None)


class BackgroundDispatchService:
    def __init__(self):
        self._queued_jobs: deque[PrintDispatchJob] = deque()
        self._dispatcher_task: asyncio.Task | None = None
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        # Serializes only the DB-write *startup* phase of each job
        # (archive INSERT + queue_item linking + commit). Once that phase
        # ends, the lock releases and FTP / start_print / post-write phases
        # of multiple jobs run in parallel. Replaces the older "one job at
        # a time across all printers" gate that contended on the SQLite
        # write lock when ``archive_print``'s INSERT raced an open FTP
        # session's still-uncommitted txn.
        self._startup_lock = asyncio.Lock()
        self._job_event = asyncio.Event()
        self._next_job_id = 1
        self._active_jobs: dict[int, ActiveDispatchState] = {}
        self._cancel_requested_job_ids: set[int] = set()

        # Progress for the current "batch" (since queue became non-empty)
        self._batch_total = 0
        self._batch_completed = 0
        self._batch_failed = 0

    @staticmethod
    def _printer_is_busy_printing(printer_id: int) -> bool:
        state = printer_manager.get_status(printer_id)
        if not state:
            return False
        return state.state in _ACTIVE_PRINT_STATES

    async def _release_direct_claim(self, job: PrintDispatchJob, *, status: str, queue_error: bool = True) -> None:
        """Give the printer back after a direct dispatch that will not print.

        ⚠️ Gated on ``awaited_by_scheduler``: the scheduler owns the outcome of
        its own items — including the #2598 busy-refusal that returns one to
        ``pending`` instead of failing it — and a second writer here would
        overwrite that silently.

        ⚠️ ``queue_error=False`` fails the item but leaves the QUEUE idle, for a
        refusal rather than a breakage: a strict-mode "not now" is not a hardware
        fault, and parking the queue in ``error`` — where ``check_queue`` skips
        every item in it — would freeze the very queue strict mode exists to
        protect, inverting the feature.

        Never raises. This runs on the way out of a dispatch that has already
        gone wrong, and losing the real error to a secondary one from the
        cleanup is the failure this method exists to avoid.
        """
        if job.awaited_by_scheduler or job.queue_item_id is None:
            return

        from datetime import datetime, timezone

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.queue_counters import set_queue_error, set_queue_idle, update_queue_counters
        from backend.app.services.queue_ops import queue_claim_scope

        try:
            async with async_session() as db:
                if job.claim_started_at is None:
                    return
                # A direct-claim queue id is the printer id.  Enter before the
                # first read so a fresh SQLite session can acquire its writer
                # before it observes the row it may release.
                async with queue_claim_scope(db, job.printer_id):
                    item = await db.get(PrintQueueItem, job.queue_item_id, populate_existing=True)
                    from backend.app.services.printer_occupancy import claim_token_matches, read_queue_occupancy

                    if (
                        item is None
                        or item.status != "printing"
                        or not claim_token_matches(item.started_at, job.claim_started_at)
                    ):
                        logger.debug("Direct claim %s is no longer the current printing row", job.queue_item_id)
                        return

                    if item.queue_id != job.printer_id:
                        logger.warning(
                            "Direct claim %s belongs to queue %s, not printer %s; leaving it untouched",
                            item.id,
                            item.queue_id,
                            job.printer_id,
                        )
                        return

                    occupancy = await read_queue_occupancy(db, item.queue_id, for_update=True)
                    if (
                        not claim_token_matches(item.started_at, job.claim_started_at)
                        or occupancy.queue.current_item_id != item.id
                    ):
                        logger.debug(
                            "Direct claim %s no longer owns queue %s (current=%s)",
                            item.id,
                            item.queue_id,
                            occupancy.queue.current_item_id,
                        )
                        return
                    item.status = status
                    item.completed_at = datetime.now(timezone.utc)
                    if status == "failed":
                        from backend.app.utils.failure_reasons import DISPATCH_INTERNAL_ERROR

                        item.error_message = str((job.outcome or {}).get("error") or "Dispatch failed")
                        item.error_code = (job.outcome or {}).get("failure_code") or DISPATCH_INTERNAL_ERROR
                        if queue_error:
                            await set_queue_error(db, item.queue_id, failed_item_id=item.id)
                        else:
                            await set_queue_idle(db, item.queue_id)
                    else:
                        await set_queue_idle(db, item.queue_id)
                    await update_queue_counters(db, item.queue_id)
                    await db.commit()
        except Exception:
            logger.exception("Failed to release the dispatch claim for job %s", job.id)

    async def start(self):
        async with self._lock:
            if self._dispatcher_task and not self._dispatcher_task.done():
                return
            self._dispatcher_task = asyncio.create_task(self._dispatcher_loop(), name="background-dispatch-dispatcher")
            logger.info("Background dispatch dispatcher started")

    async def stop(self):
        dispatcher: asyncio.Task | None = None
        running_tasks: list[asyncio.Task] = []
        async with self._lock:
            dispatcher = self._dispatcher_task
            self._dispatcher_task = None
            running_tasks = list(self._running_tasks.values())
            self._running_tasks.clear()
            self._active_jobs.clear()
            self._queued_jobs.clear()
            self._cancel_requested_job_ids.clear()
            self._job_event.set()

        if dispatcher:
            dispatcher.cancel()
        for task in running_tasks:
            task.cancel()

        if dispatcher:
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass

        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)

        logger.info("Background dispatch dispatcher stopped")

    async def dispatch_reprint_archive(
        self,
        *,
        archive_id: int,
        archive_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        project_line_id: int | None = None,
    ) -> dict[str, Any]:
        return await self._dispatch(
            kind="reprint_archive",
            source_id=archive_id,
            source_name=archive_name,
            printer_id=printer_id,
            printer_name=printer_name,
            options=options,
            requested_by_user_id=requested_by_user_id,
            requested_by_username=requested_by_username,
            project_id=project_id,
            project_line_id=project_line_id,
        )

    async def get_state(self) -> dict[str, Any]:
        """Get current dispatch queue state snapshot for newly connected clients."""
        async with self._lock:
            return self._build_state_payload_unlocked()

    async def get_monitor_state(self, printer_ids: frozenset[int]) -> dict[str, Any]:
        """Passive, scoped phases only; never copy another actor's job names."""
        async with self._lock:
            active = {}
            for state in self._active_jobs.values():
                if state.job.printer_id not in printer_ids:
                    continue
                progress = None
                if state.upload_total_bytes and state.upload_total_bytes > 0 and state.upload_bytes is not None:
                    progress = round(max(0.0, min(100.0, state.upload_bytes / state.upload_total_bytes * 100)), 1)
                active[state.job.printer_id] = {
                    "printer_id": state.job.printer_id,
                    "phase": state.phase,
                    "upload_progress_pct": progress,
                }
            return {
                "active_jobs": list(active.values()),
                "dispatched_jobs": [
                    {"printer_id": pid}
                    for pid in sorted({job.printer_id for job in self._queued_jobs if job.printer_id in printer_ids})
                ],
            }

    async def run_from_queue_item(
        self,
        *,
        kind: Literal["reprint_archive", "print_library_file"],
        # ``None`` when the queue row's only source is its captured copy — the
        # runner then reads the descriptor and looks no original up (spec §7).
        source_id: int | None,
        source_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        project_line_id: int | None = None,
        queue_item_id: int,
    ) -> dict[str, Any]:
        """Run a dispatch inline (bypass queue) on behalf of the scheduler.

        The scheduler already gates on stagger + printer-idle, so we don't
        need to re-enqueue through the BackgroundDispatch queue here — we
        run the job directly, still registering it as "active" so the UI
        shows it while the FTP upload and print-start happen. Returns the
        job's ``outcome`` dict once ``_run_*`` signals completion.
        """
        async with self._lock:
            job = PrintDispatchJob(
                id=self._next_job_id,
                kind=kind,
                source_id=source_id,
                source_name=source_name,
                printer_id=printer_id,
                printer_name=printer_name,
                options=options,
                requested_by_user_id=requested_by_user_id,
                requested_by_username=requested_by_username,
                project_id=project_id,
                project_line_id=project_line_id,
                queue_item_id=queue_item_id,
                awaited_by_scheduler=True,
            )
            self._next_job_id += 1
            self._active_jobs[job.id] = ActiveDispatchState(job=job, message=f"Queue dispatch to {printer_name}...")
            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "dispatched",
                    "job_id": job.id,
                    "source_name": source_name,
                    "printer_id": printer_id,
                    "printer_name": printer_name,
                    "message": f"Queue dispatching to {printer_name}",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

        try:
            await self._process_job(job)
        except (RoutingDeferred, SourceUnavailable, FarmStartupRefused) as exc:
            await self._handle_routing_deferred(job, exc)
        except DispatchJobCancelled:
            pass  # outcome.cancelled already set by the runner
        except Exception as e:
            # ⚠️ "outcome.error already set by the runner" was true only for
            # failures INSIDE the runner's own try. Anything raised before it —
            # a missing archive, a missing printer, a refusal to dispatch at
            # all — left ``error`` at None, and the scheduler then reported the
            # useless "Dispatch failed" instead of the reason we wrote. Fill it
            # in here rather than trusting the assumption.
            _record_outcome_error(job, e)
            if _published(job) and not _ends_after_publish(e):
                # The start may be on the printer: neither a failure nor a refusal.
                job.outcome["uncertain"] = True
                mark_unobserved(job.execution_archive_id)
                logger.error("Dispatch %s: outcome unknown after the start was sent: %s", job.id, e, exc_info=True)
            else:
                job.outcome.setdefault("failure_code", dispatch_failure_code(e, stage=job.stage))
                await self._close_execution_archive(job, e)
        finally:
            async with self._lock:
                self._active_jobs.pop(job.id, None)
                done_payload = self._build_state_payload_unlocked(
                    recent_event={
                        "status": "deferred"
                        if job.outcome.get("deferred")
                        else ("completed" if job.outcome.get("success") else "failed"),
                        "job_id": job.id,
                        "source_name": source_name,
                        "printer_id": printer_id,
                        "printer_name": printer_name,
                        "message": job.outcome.get("error") or "done",
                    }
                )
            await ws_manager.broadcast({"type": "background_dispatch", "data": done_payload})

        job.completion_event.set()
        return {
            **job.outcome,
            "swap_start_effect": job.swap_start_effect,
            "send_fact": job.send_fact,
            "claim_started_at": job.claim_started_at,
            "execution_archive_id": job.execution_archive_id,
        }

    async def dispatch_print_library_file(
        self,
        *,
        file_id: int,
        filename: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        project_line_id: int | None = None,
        cleanup_library_after_dispatch: bool = False,
    ) -> dict[str, Any]:
        return await self._dispatch(
            kind="print_library_file",
            source_id=file_id,
            source_name=filename,
            printer_id=printer_id,
            printer_name=printer_name,
            options=options,
            requested_by_user_id=requested_by_user_id,
            requested_by_username=requested_by_username,
            project_id=project_id,
            project_line_id=project_line_id,
            cleanup_library_after_dispatch=cleanup_library_after_dispatch,
        )

    def cancel_dispatch_for_queue_item(self, queue_item_id: int) -> bool:
        """Tell an in-flight dispatch that its queue item no longer wants to print.

        ⚠️ Stopping an item only writes ``status`` to the database, and a
        dispatch coroutine parked in ``asyncio.sleep`` cannot see that. During
        the preheat stage there is no print to stop either — the stop command
        goes to an idle printer — so without this the heaters ran for the rest
        of ``preheat_max_wait_seconds`` + ``preheat_soak_seconds`` and the
        printer stayed claimed, blocking every other queued item behind a print
        that was not happening.

        Synchronous and lock-free on purpose: the callers are HTTP routes on
        the request path, and the flag is read by the dispatch's own
        cancel-check. Returns whether a live job was signalled.
        """
        signalled = False
        for state in list(self._active_jobs.values()):
            if state.job.queue_item_id == queue_item_id:
                self._cancel_requested_job_ids.add(state.job.id)
                logger.info(
                    "Cancel requested for dispatch job %s - its queue item %s was stopped",
                    state.job.id,
                    queue_item_id,
                )
                signalled = True
        return signalled

    async def cancel_job(self, job_id: int) -> dict[str, Any]:
        """Cancel a queued dispatch job.

        Queued jobs are removed immediately. Active jobs are cancelled
        cooperatively and will stop at the next cancellation checkpoint.
        """
        async with self._lock:
            # Check active jobs first
            active_state = self._active_jobs.get(job_id)
            if active_state is not None:
                logger.info("Cancel requested for active dispatch job %s", job_id)
                self._cancel_requested_job_ids.add(job_id)
                active_job = active_state.job
                payload = self._build_state_payload_unlocked(
                    recent_event={
                        "status": "cancelling",
                        "job_id": active_job.id,
                        "source_name": active_job.source_name,
                        "printer_id": active_job.printer_id,
                        "printer_name": active_job.printer_name,
                        "message": "Cancelling current dispatch...",
                    }
                )
                result = {
                    "cancelled": True,
                    "pending": True,
                    "job_id": active_job.id,
                    "source_name": active_job.source_name,
                    "printer_id": active_job.printer_id,
                    "printer_name": active_job.printer_name,
                }
                await ws_manager.broadcast({"type": "background_dispatch", "data": payload})
                return result

            # Check queued jobs
            cancelled_job: PrintDispatchJob | None = None
            for job in self._queued_jobs:
                if job.id == job_id:
                    cancelled_job = job
                    break

            if not cancelled_job:
                logger.info("Cancel requested for unknown dispatch job %s", job_id)
                return {"cancelled": False, "reason": "not_found"}

            self._queued_jobs.remove(cancelled_job)
            logger.info("Cancelled queued dispatch job %s", cancelled_job.id)
            self._batch_total = max(0, self._batch_total - 1)

            if self._batch_total == 0 and len(self._queued_jobs) == 0 and len(self._active_jobs) == 0:
                self._batch_completed = 0
                self._batch_failed = 0

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "cancelled",
                    "job_id": cancelled_job.id,
                    "source_name": cancelled_job.source_name,
                    "printer_id": cancelled_job.printer_id,
                    "printer_name": cancelled_job.printer_name,
                    "message": "Cancelled from queue",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})
        return {
            "cancelled": True,
            "pending": False,
            "job_id": cancelled_job.id,
            "source_name": cancelled_job.source_name,
            "printer_id": cancelled_job.printer_id,
            "printer_name": cancelled_job.printer_name,
        }

    async def _dispatch(
        self,
        *,
        kind: Literal["reprint_archive", "print_library_file"],
        source_id: int,
        source_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None = None,
        project_line_id: int | None = None,
        cleanup_library_after_dispatch: bool = False,
    ) -> dict[str, Any]:
        # ⚠️ The source is copied into queue-sources BEFORE the printer is
        # claimed, and outside ``_lock`` (spec §5 steps 1-3). A copy can take
        # minutes over a share: claiming first would park the machine for all of
        # it — and, on a failure, for nothing — while holding ``_lock`` would stop
        # every *other* printer being dispatched to as well.
        #
        # The price is that time passes between asking whether this printer is
        # free and taking its claim, so the question is asked twice: here, so an
        # already-refused dispatch costs no walk over the share, and again under
        # the lock in ``_enqueue_claimed_job``, which is the answer that decides.
        # A refusal after the copy discards the staged bytes and leaves no row.
        from backend.app.services.farm_startup_guard import farm_startup_guard

        farm_startup_guard.require()
        async with self._lock:
            self._refuse_unless_free(printer_id, printer_name)

        from backend.app.services.queue_batch import direct_print_capture_plan
        from backend.app.services.queue_source_capture import capture_staged, discard_staged

        async with async_session() as prep_db:
            capture_plan = await direct_print_capture_plan(prep_db, kind=kind, source_id=source_id)
            printer_row = await prep_db.get(Printer, printer_id)
            printer_swap_on = bool(printer_row is not None and printer_row.swap_mode_enabled)
        staged = await capture_staged(capture_plan)
        try:
            from backend.app.services.queue_source_capture import swap_request

            # The swap fields this print may honour, from the declaration the copy
            # confirmed (swap-source-lifecycle spec r4 §8) -- written into ``options``
            # itself, which the claim row and the job both carry. An unknown source
            # asked for our macros is refused here, before the printer is claimed.
            execute, events = swap_request(
                staged.receipt.swap_fact.value,
                execute=options.get("execute_swap_macros"),
                events=options.get("swap_macro_events"),
                printer_swap_mode=printer_swap_on,
            )
            options["execute_swap_macros"] = execute
            options["swap_macro_events"] = events
            return await self._enqueue_claimed_job(
                kind=kind,
                source_id=source_id,
                source_name=source_name,
                printer_id=printer_id,
                printer_name=printer_name,
                options=options,
                requested_by_user_id=requested_by_user_id,
                requested_by_username=requested_by_username,
                project_id=project_id,
                project_line_id=project_line_id,
                cleanup_library_after_dispatch=cleanup_library_after_dispatch,
                staged=staged,
            )
        except BaseException:
            await discard_staged(staged)
            raise

    def has_work_for_printer(self, printer_id: int) -> bool:
        """Snapshot of queued/preparing dispatches, including work before MQTT publish.

        Read synchronously on the event loop; this is a recovery veto, not an
        admission lock. Admission still holds ``_lock`` across its checks.
        """
        return any(job.printer_id == printer_id for job in self._queued_jobs) or any(
            active.job.printer_id == printer_id for active in self._active_jobs.values()
        )

    def printer_of_job(self, job_id: int) -> int | None:
        """The printer a queued or active dispatch job is for; None when it is neither.

        Read synchronously, like ``has_work_for_printer``: the API-key printer
        scope asks it before ``cancel_job`` takes the lock, and a job that ends
        in between is simply not found by the cancel.
        """
        active = self._active_jobs.get(job_id)
        if active is not None:
            return active.job.printer_id
        return next((job.printer_id for job in self._queued_jobs if job.id == job_id), None)

    def _refuse_unless_free(self, printer_id: int, printer_name: str) -> None:
        """Both availability questions, asked under ``_lock``.

        Extracted because ``_dispatch`` asks them twice — before the copy and
        again before the claim — and two copies of the same two refusals would
        drift. Never call it without the lock: it reads the dispatcher's own
        in-memory job lists.
        """
        if self.has_work_for_printer(printer_id):
            raise DispatchEnqueueRejected(f"Printer {printer_name} already has a background dispatch in progress")

        if self._printer_is_busy_printing(printer_id):
            raise DispatchEnqueueRejected(f"Printer {printer_name} is currently busy printing")

        from backend.app.services.print_run_binding import prepared_print_run_conflicts

        if prepared_print_run_conflicts(printer_manager, printer_id):
            raise DispatchEnqueueRejected(
                "A previous print context is blocking this start. Check print states on the System page.",
                code="dispatch_runtime_conflict",
                printer_id=printer_id,
            )

    async def _enqueue_claimed_job(
        self,
        *,
        kind: Literal["reprint_archive", "print_library_file"],
        source_id: int,
        source_name: str,
        printer_id: int,
        printer_name: str,
        options: dict[str, Any],
        requested_by_user_id: int | None,
        requested_by_username: str | None,
        project_id: int | None,
        project_line_id: int | None,
        cleanup_library_after_dispatch: bool,
        staged,
    ) -> dict[str, Any]:
        """Re-check availability, take the claim, queue the job — the fast half.

        ``staged`` is the ``queue_source_capture.StagedSource`` the copy above
        produced; the claim row is written inside its publication's transaction.
        The bytes are already on disk when this runs, so ``_lock`` is held for one
        INSERT, one UPDATE and the dispatcher's own bookkeeping, exactly as it was
        before the spool existed.
        """
        async with self._lock:
            from backend.app.services.farm_startup_guard import farm_startup_guard

            farm_startup_guard.require()
            # The re-check. Time has passed while the source was copied, and the
            # answer from before the copy is not evidence any more.
            self._refuse_unless_free(printer_id, printer_name)

            # Claim the printer now, in the DB, so ``check_queue`` sees this
            # print for the whole of its dispatch. Until this existed the claim
            # arrived only with ``on_print_start`` — i.e. once the printer had
            # already started — and the queue dispatched over the file on its
            # way, which is the bug reported against 0.5.4. Inside the lock and
            # after the refusals above: if this raises, no job was created and
            # nothing leaked, and a rejected dispatch cannot park a printer.
            #
            # ⚠️ Yes, this does DB I/O while holding ``_lock``, and that is
            # deliberate. Moving it out would leave the job queued but unclaimed
            # for however long the write takes — which is the exact window the
            # claim exists to close. The cost is bounded: one INSERT plus one
            # UPDATE — plus, only when the caller named an ORDER and no line,
            # the three small reads ``order_filing.resolve_line_id`` makes to
            # find it (spec pass 7) — and the enqueue path is not on any hot
            # loop.
            from backend.app.services.printer_occupancy import PrinterOccupancyConflict
            from backend.app.services.queue_batch import claim_printer_for_direct_print

            async with async_session() as claim_db:
                try:
                    claim_item = await claim_printer_for_direct_print(
                        claim_db,
                        printer_id=printer_id,
                        origin="direct",
                        archive_id=source_id if kind == "reprint_archive" else None,
                        library_file_id=source_id if kind == "print_library_file" else None,
                        options=options,
                        created_by_id=requested_by_user_id,
                        project_id=project_id,
                        project_line_id=project_line_id,
                        staged=staged,
                    )
                except PrinterOccupancyConflict as exc:
                    raise DispatchEnqueueRejected(
                        f"Printer {printer_name} is no longer available ({exc.code})",
                        code=exc.code,
                        printer_id=printer_id,
                    ) from exc
                claim_item_id = claim_item.id if claim_item is not None else None
                if claim_item is not None:
                    # ⚠️ **The claim row is the resolved answer, and the job must
                    # carry it.** ``claim_printer_for_direct_print`` files the
                    # line when the caller named only an order (spec pass 7,
                    # Decision 4a) — and the job below is what
                    # ``_run_print_library_file`` stamps the ARCHIVE from
                    # (``archive_print(project_line_id=job.project_line_id)``).
                    # Built from the caller's original ids instead, the queue row
                    # knew the line and the print history did not, so a "print
                    # now" for an order landed in the archive unfiled and the
                    # order page's own attribution had to re-derive it. Reading
                    # them BOTH off the row also keeps the two rows about one
                    # print from disagreeing about the order.
                    project_id = claim_item.project_id
                    project_line_id = claim_item.project_line_id

            dispatch_position = len(self._queued_jobs) + len(self._active_jobs) + 1
            job = PrintDispatchJob(
                id=self._next_job_id,
                kind=kind,
                source_id=source_id,
                source_name=source_name,
                printer_id=printer_id,
                printer_name=printer_name,
                options=options,
                requested_by_user_id=requested_by_user_id,
                requested_by_username=requested_by_username,
                project_id=project_id,
                project_line_id=project_line_id,
                cleanup_library_after_dispatch=cleanup_library_after_dispatch,
                queue_item_id=claim_item_id,
                claim_started_at=claim_item.started_at if claim_item is not None else None,
                # ⚠️ ``awaited_by_scheduler`` stays False: the dispatcher owns
                # this item, and is therefore the one that must release it.
            )
            self._next_job_id += 1
            self._batch_total += 1
            self._queued_jobs.append(job)
            self._job_event.set()

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "dispatched",
                    "job_id": job.id,
                    "source_name": source_name,
                    "printer_id": printer_id,
                    "printer_name": printer_name,
                    "message": f"Dispatched to {printer_name}",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

        return {
            "dispatch_job_id": job.id,
            "dispatch_position": dispatch_position,
            "status": "dispatched",
            "printer_id": printer_id,
            "source_id": source_id,
            "source_name": source_name,
        }

    async def _dispatcher_loop(self):
        while True:
            await self._job_event.wait()
            self._job_event.clear()

            while True:
                payload: dict[str, Any] | None = None
                job_to_start: PrintDispatchJob | None = None
                async with self._lock:
                    # Multiple jobs can be active concurrently. Mutual
                    # exclusion of the *startup* (DB-write) phase is
                    # enforced inside ``_run_*`` via ``self._startup_lock``;
                    # the FTP / start_print / post-write phases run in
                    # parallel across printers.
                    if not self._queued_jobs:
                        break

                    job_to_start = self._queued_jobs.popleft()
                    self._active_jobs[job_to_start.id] = ActiveDispatchState(
                        job=job_to_start,
                        message="Preparing background dispatch...",
                    )

                    task = asyncio.create_task(
                        self._run_active_job(job_to_start), name=f"background-dispatch-job-{job_to_start.id}"
                    )
                    self._running_tasks[job_to_start.id] = task

                    payload = self._build_state_payload_unlocked(
                        recent_event={
                            "status": "processing",
                            "job_id": job_to_start.id,
                            "source_name": job_to_start.source_name,
                            "printer_id": job_to_start.printer_id,
                            "printer_name": job_to_start.printer_name,
                            "message": "Preparing background dispatch...",
                        }
                    )

                if payload:
                    await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    async def _run_active_job(self, job: PrintDispatchJob):
        # Only direct prints reach here: ``_queued_jobs`` is fed by ``_dispatch``
        # alone, and the scheduler runs its own items through
        # ``run_from_queue_item``. So this method owns the claim's release.
        #
        # ⚠️ The release is on the failure paths, never in ``finally``: a
        # ``finally`` release would also fire on success and hand the printer
        # away in the middle of the print it just started. A normal return means
        # the print IS running — every failure inside the runners raises,
        # including a refused ``start_print`` — and from there the claim belongs
        # to the running print, for ``on_print_complete`` to close.
        try:
            await self._process_job(job)
            # ⚠️ Guarded, not unconditional: a strict-mode refusal returns
            # normally from ``_process_job`` having already walked the failure
            # exits itself, and marking it finished a second time would count it
            # in both batch tallies and leave "completed" as the last thing the
            # dispatch panel heard about a print that never ran. Every other
            # normal return comes from a runner, and a runner sets
            # ``success: True`` as the last statement before its ``finally`` —
            # so for them this reads exactly as the bare call did.
            if (job.outcome or {}).get("success"):
                await self._mark_job_finished(job, failed=False, message="Background dispatch complete")
        except (RoutingDeferred, SourceUnavailable, FarmStartupRefused) as exc:
            await self._handle_routing_deferred(job, exc)
        except DispatchJobCancelled:
            await self._release_direct_claim(job, status="cancelled")
            await self._mark_job_cancelled(job)
        except asyncio.CancelledError:
            # Process shutdown. Deliberately not released here — that would race
            # the shutdown's own session teardown, and
            # ``release_interrupted_dispatch_claims`` reclaims it at next start.
            job.interrupted = True
            raise
        except Exception as e:
            _record_outcome_error(job, e)
            if _published(job) and not _ends_after_publish(e):
                # The start may be running on the printer. Releasing the claim, or
                # closing its archive, would hand the printer to the next job over it.
                job.outcome["uncertain"] = True
                mark_unobserved(job.execution_archive_id)
                logger.error(
                    "Background dispatch job %s: outcome unknown after the start was sent: %s", job.id, e, exc_info=True
                )
                await self._mark_job_finished(job, failed=False, message=f"Start sent; its outcome is unknown: {e}")
                return
            logger.error("Background dispatch job %s failed: %s", job.id, e, exc_info=True)
            job.outcome.setdefault("failure_code", dispatch_failure_code(e, stage=job.stage))
            await self._release_direct_claim(job, status="failed")
            await self._close_execution_archive(job, e)
            await self._mark_job_finished(job, failed=True, message=str(e))
            # A failure raised before the runner's upload ``try`` (printer not
            # connected, no card, the pre-delete FTP call, archive or patch errors)
            # never reached its ``finally``, the one place that reported. No-op when
            # the runner already told the operator.
            await report_failure_if_unwatched(job)
        finally:
            job.completion_event.set()
            self._job_event.set()

    async def _build_injection_spec(
        self,
        *,
        job: PrintDispatchJob,
        printer_model: str | None,
        plate_id: int,
    ) -> GcodeInjectionSpec | None:
        """Resolve the per-job injection spec from settings + per-printer model (#422).

        Returns a ``GcodeInjectionSpec`` for ``apply_3mf_transforms`` to splice
        in during its single open/write pass, or None when injection is off,
        the printer model is unknown, or no snippets are configured for the
        target model. The actual zip mutation lives in ``apply_3mf_transforms``
        so M970-commenting and snippet-injection share one open/repack cycle
        instead of two — important on multi-plate 50+ MB 3MFs.

        The setting is read on every attempt through the module's own
        ``async_session``, closed before any file work. A setting that cannot be
        read skips the injection, never the print; the warning names only the
        exception's class, because its text can carry SQL parameters or the
        snippets themselves (issue #96).
        """
        if not job.options.get("gcode_injection"):
            return None
        if not printer_model:
            logger.info("Dispatch job %s: gcode_injection on but no printer model, skipping", job.id)
            return None
        try:
            import json as _json

            from backend.app.api.routes.settings import get_setting

            async with async_session() as _sdb:
                snippets_raw = await get_setting(_sdb, "gcode_snippets")
            if not snippets_raw:
                return None
            snippets = _json.loads(snippets_raw)
            model_snippets = snippets.get(printer_model, {}) if isinstance(snippets, dict) else {}
            start_gc = (model_snippets.get("start_gcode") or "").strip()
            end_gc = (model_snippets.get("end_gcode") or "").strip()
            if not start_gc and not end_gc:
                return None
            return GcodeInjectionSpec(
                plate_id=plate_id,
                start_gcode=start_gc or None,
                end_gcode=end_gc or None,
            )
        except Exception as exc:
            logger.warning(
                "Dispatch job %s: gcode injection skipped (injection_settings_failed, %s)",
                job.id,
                type(exc).__name__,
            )
            return None

    async def _set_active_message(self, job: PrintDispatchJob, message: str, *, phase: str = "preparing"):
        async with self._lock:
            active = self._active_jobs.get(job.id)
            if not active:
                return
            active.message = message
            active.phase = phase
            # New phase → previous upload progress is no longer relevant.
            # Without this the toast keeps rendering a 100% progress bar
            # during post-upload phases (swap macros, "Starting print…")
            # because ``_set_active_upload_progress(job, 1, 1)`` runs
            # right after upload finishes and nothing ever clears it.
            # The next upload (if any) will repopulate via the same setter.
            active.upload_bytes = None
            active.upload_total_bytes = None
            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "processing",
                    "job_id": active.job.id,
                    "source_name": active.job.source_name,
                    "printer_id": active.job.printer_id,
                    "printer_name": active.job.printer_name,
                    "message": message,
                }
            )
        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    async def _set_active_upload_progress(self, job: PrintDispatchJob, uploaded: int, total: int):
        async with self._lock:
            active = self._active_jobs.get(job.id)
            if not active:
                return

            active.upload_bytes = max(0, int(uploaded))
            active.phase = "uploading"
            active.upload_total_bytes = max(0, int(total))
            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "processing",
                    "job_id": active.job.id,
                    "source_name": active.job.source_name,
                    "printer_id": active.job.printer_id,
                    "printer_name": active.job.printer_name,
                    "message": active.message,
                }
            )
        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    async def _mark_job_finished(self, job: PrintDispatchJob, *, failed: bool, message: str, deferred: bool = False):
        async with self._lock:
            # A refused direct print is a failed dispatch to the batch; a deferral
            # that was not ours to report counts as nothing.
            if failed:
                self._batch_failed += 1
            elif deferred:
                # Neither completed nor failed: it leaves the batch, as a
                # cancellation does — or the toast waits for a tally that never adds up.
                self._batch_total = max(0, self._batch_total - 1)
            else:
                self._batch_completed += 1

            self._active_jobs.pop(job.id, None)
            self._running_tasks.pop(job.id, None)
            self._cancel_requested_job_ids.discard(job.id)

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "deferred" if deferred else ("failed" if failed else "completed"),
                    "job_id": job.id,
                    "source_name": job.source_name,
                    "printer_id": job.printer_id,
                    "printer_name": job.printer_name,
                    "message": message,
                }
            )
            should_reset_batch = len(self._queued_jobs) == 0 and len(self._active_jobs) == 0

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

        if should_reset_batch:
            async with self._lock:
                if len(self._queued_jobs) == 0 and len(self._active_jobs) == 0:
                    self._batch_total = 0
                    self._batch_completed = 0
                    self._batch_failed = 0

    async def _mark_job_cancelled(self, job: PrintDispatchJob):
        async with self._lock:
            self._active_jobs.pop(job.id, None)
            self._running_tasks.pop(job.id, None)
            self._cancel_requested_job_ids.discard(job.id)
            self._batch_total = max(0, self._batch_total - 1)

            if self._batch_total == 0 and len(self._queued_jobs) == 0 and len(self._active_jobs) == 0:
                self._batch_completed = 0
                self._batch_failed = 0

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "cancelled",
                    "job_id": job.id,
                    "source_name": job.source_name,
                    "printer_id": job.printer_id,
                    "printer_name": job.printer_name,
                    "message": "Cancelled during dispatch",
                }
            )

        await ws_manager.broadcast({"type": "background_dispatch", "data": payload})

    def _is_cancel_requested(self, job_id: int) -> bool:
        return job_id in self._cancel_requested_job_ids

    def _raise_if_cancel_requested(self, job: PrintDispatchJob):
        if self._is_cancel_requested(job.id):
            raise DispatchJobCancelled(f"Dispatch job {job.id} cancelled")

    def _build_state_payload_unlocked(self, recent_event: dict[str, Any] | None = None) -> dict[str, Any]:
        processing = len(self._active_jobs)
        dispatched = len(self._queued_jobs)

        dispatched_jobs = [
            {
                "job_id": job.id,
                "kind": job.kind,
                "source_id": job.source_id,
                "source_name": job.source_name,
                "printer_id": job.printer_id,
                "printer_name": job.printer_name,
            }
            for job in list(self._queued_jobs)
        ]

        active_jobs: list[dict[str, Any]] = []
        for active in self._active_jobs.values():
            upload_progress_pct = None
            if active.upload_total_bytes and active.upload_total_bytes > 0 and active.upload_bytes is not None:
                upload_progress_pct = round(
                    max(0.0, min(100.0, (active.upload_bytes / active.upload_total_bytes) * 100.0)), 1
                )

            active_jobs.append(
                {
                    "job_id": active.job.id,
                    "kind": active.job.kind,
                    "source_id": active.job.source_id,
                    "source_name": active.job.source_name,
                    "printer_id": active.job.printer_id,
                    "printer_name": active.job.printer_name,
                    "message": active.message,
                    "upload_bytes": active.upload_bytes,
                    "phase": active.phase,
                    "upload_total_bytes": active.upload_total_bytes,
                    "upload_progress_pct": upload_progress_pct,
                }
            )

        active_jobs.sort(key=lambda item: int(item["job_id"]))
        active_job = active_jobs[0] if active_jobs else None

        return {
            "total": self._batch_total,
            "dispatched": dispatched,
            "processing": processing,
            "completed": self._batch_completed,
            "failed": self._batch_failed,
            "dispatched_jobs": dispatched_jobs,
            "active_jobs": active_jobs,
            "active_job": active_job,
            "recent_event": recent_event,
        }

    async def _process_job(self, job: PrintDispatchJob):
        # Lazy import — print_scheduler imports us back.
        from backend.app.services.farm_startup_guard import farm_startup_guard

        farm_startup_guard.require()
        from backend.app.services.print_scheduler import scheduler as print_scheduler

        # Strict mode (opt-in): a DIRECT print whose printer's stagger group has
        # no free slot is refused here, before it would otherwise wait for one.
        # Queue-originated jobs are never refused — the queue waits by design and
        # pre-registered its slot in ``_start_print``. Decided before the acquire
        # below on purpose: after it the print has already waited and holds a
        # slot, so a check there could only fire on a race (which is what the
        # two old in-runner blocks did).
        if not job.awaited_by_scheduler and await self._strict_stagger_refuses(job.printer_id):
            await self._refuse_dispatch(
                job, "Stagger cap reached — wait for a free slot or disable stagger_strict_for_direct_dispatch"
            )
            return

        # Stagger gate: applies to both direct prints (cold acquire — polls
        # until a slot frees) and queue dispatch (slot was pre-registered
        # synchronously by ``print_scheduler._start_print``, so this returns
        # immediately).
        await print_scheduler.acquire_stagger_slot(job.printer_id)

        if job.kind == "reprint_archive":
            await self._run_reprint_archive(job)
            return
        if job.kind == "print_library_file":
            await self._run_print_library_file(job)
            return
        raise RuntimeError(f"Unknown dispatch job kind: {job.kind}")

    async def _prepare_filament_routing(self, db, job, pins: AsyncExitStack):
        """Read the claim, the job's source and its routing — before any file is opened.

        ⚠️ ``pins`` is how the blob survives the dispatch (spec §9). The GC never
        releases a blob a job row still names, but a row can be deleted under a
        running dispatch (a printer deleted, a user deleted), and from that moment
        the bytes being uploaded are unowned. The pin is taken here because this is
        the first point that knows which blob it is, and released when the runner
        exits — so it spans preflight, patch, archive-write and upload.

        **Required, with no default**, because the failure mode of forgetting it is
        a file unlinked under a running print and no error anywhere: a caller that
        does not hold a stack must be a type error, not a silent no-pin dispatch.

        Taking it needs the storage guard for the registration only, and this
        session has done nothing but read at that point, so a publication holding
        the guard can always finish and hand it over.
        """
        from backend.app.models.print_queue import PrintQueueItem

        item = await db.get(PrintQueueItem, job.queue_item_id) if job.queue_item_id else None
        if item is None or item.status != "printing":
            raise RoutingDeferred(
                "dispatch_claim_changed", detail="claim_missing" if item is None else "claim_released"
            )
        job.options["auto_eject"] = item.auto_eject
        job.options["auto_eject_settings"] = deepcopy(item.auto_eject_settings)
        job.claim_started_at = item.started_at
        job.original_archive_id, job.original_library_file_id = item.archive_id, item.library_file_id
        job.source = await item_descriptor(db, item)
        if job.source is not None:
            from backend.app.services import queue_sources

            await pins.enter_async_context(queue_sources.pin(item.queue_source_id))
        job.routing_intent = item.filament_routing
        job.routing_guard = await preflight_item(db, item, job.printer_id)
        if job.routing_guard:
            plan = job.routing_guard.plan
            job.options.update(ams_mapping=plan.mapping, use_ams=plan.use_ams, plate_id=plan.resolved_plate_id)

    async def _capture_descriptorless_source(self, db, job, pins: AsyncExitStack) -> None:
        """Give a job with no captured source one copy for this attempt (swap-source-lifecycle spec r4 §6).

        A pre-m173 job or a calibration print has no descriptor, and that is not a known False. Right after the
        claim and the routing were read, and before any archive, patch, macro or preheat, the UNPATCHED source is
        copied into staging, and ``job.source`` becomes that copy: everything the attempt reads afterwards reads
        it, and the original is not opened again. The declaration rides the copy, proved by the hash the worker
        computed -- for calibration as for anything else; ``is_calibration`` proves nothing.

        The request's transaction is closed first: the copy may take minutes and must not hold one. The staged
        bytes belong to ``pins`` from the moment the receipt exists, so every exit of the runner -- success, a
        failure, a cancellation -- drops them after the last reader. A refused copy is answered by its class
        (:func:`~backend.app.services.filament_deferred.capture_refusal`). After the await the claim and the job's
        link to its source are read again: a job cancelled, reclaimed or re-pointed meanwhile does not dispatch.
        """
        if job.source is not None:
            return
        from fastapi import HTTPException

        from backend.app.services.filament_deferred import capture_refusal
        from backend.app.services.queue_source_capture import capture_for_dispatch, discard_staged, plan_capture
        from backend.app.services.queue_sources import QueueSourceError

        archive = library_file = None
        if job.kind == "reprint_archive" and job.source_id:
            archive = await ArchiveService(db).get_archive(job.source_id)
        elif job.kind == "print_library_file" and job.source_id:
            library_file = await db.scalar(LibraryFile.active().where(LibraryFile.id == job.source_id))
        if archive is None and library_file is None:
            raise SourceUnavailable()
        try:
            request = plan_capture(archive=archive, library_file=library_file)
        except HTTPException as exc:  # a row with no file, or a file that is not a sliced container
            raise SourceUnavailable() from exc
        await db.commit()
        try:
            staged = await capture_for_dispatch(request, deadline=DISPATCH_CAPTURE_DEADLINE_SECONDS)
        except QueueSourceError as exc:
            raise capture_refusal(exc, queued=job.awaited_by_scheduler) from exc
        pins.push_async_callback(discard_staged, staged)
        await self._verify_routing_claim(db, job)
        from backend.app.models.print_queue import PrintQueueItem

        item = await db.get(PrintQueueItem, job.queue_item_id, populate_existing=True)
        if (item.archive_id, item.library_file_id) != (job.original_archive_id, job.original_library_file_id):
            raise RoutingDeferred("dispatch_claim_changed", detail="source_link_changed")
        job.source = staged.descriptor
        await self._anchor_routing_to_copy(job, staged)

    @staticmethod
    async def _apply_swap_source_policy(db, job) -> None:
        """The one runtime decision on OUR swap macros for this attempt (swap-source-lifecycle spec r4 §8).

        Taken once the claim, the routing and the copy are settled and before any archive, preheat or macro, from
        the job's own declaration: True mutes ours, False keeps exactly what was asked under the printer's swap
        mode, and an unknown source with an effective request is refused before dispatch (D2). ``job.options`` is
        rewritten IN PLACE, so the execution archive's pending events, the swap-start step and the config
        ``on_print_complete`` reads all see this answer and nothing else; selected macros, order options and
        auto-eject are not touched.
        """
        from backend.app.services import swap_compatibility
        from backend.app.services.filament_deferred import SwapSourceRefused

        options = job.options
        printer = await db.get(Printer, job.printer_id)
        fact = job.source.swap_fact.value if job.source is not None else None
        decision = swap_compatibility.decide(
            fact,
            execute=options.get("execute_swap_macros"),
            events=options.get("swap_macro_events"),
            printer_swap_mode=bool(printer is not None and printer.swap_mode_enabled),
        )
        if decision.refused:
            raise SwapSourceRefused(decision.reason)
        options["execute_swap_macros"] = decision.execute
        options["swap_macro_events"] = list(decision.events) if decision.execute else []

    @staticmethod
    async def _anchor_routing_to_copy(job, staged) -> None:
        """Make the routing guard ask about the COPY from now on, never the original.

        The plan was computed from the original as preflight read it, under its stat revision. When the copy was
        taken from that same revision -- the worker proves the original's stat did not move while it read -- the
        copy IS what the plan describes, and the guard's later probes (``final_guard``) re-read the staged file by
        its hash. A copy taken from another revision is a plan about other bytes: ``source_changed``, the answer
        the guard itself would have given, and the next attempt plans again.
        """
        from dataclasses import replace

        from backend.app.services.filament_requirements import SourceIdentity, probe_identity

        guard = job.routing_guard
        identity = getattr(getattr(guard, "requirements", None), "source_identity", None)
        if guard is None or identity is None:
            return
        receipt = staged.receipt
        if (identity.size, identity.mtime_ns) != (receipt.size_bytes, receipt.source_mtime_ns):
            raise RoutingDeferred("source_changed")
        try:
            copied = await probe_identity(
                SourceIdentity(str(staged.staging_path), receipt.size_bytes, 0, sha256=receipt.sha256)
            )
        except SourceUnavailable as exc:
            raise RoutingDeferred(exc.reason) from exc
        job.routing_guard = replace(guard, requirements=replace(guard.requirements, source_identity=copied))

    async def _verify_routing_claim(self, db, job):
        from backend.app.models.print_queue import PrintQueueItem

        item = await db.get(PrintQueueItem, job.queue_item_id, populate_existing=True)
        detail = _claim_change_detail(item, job)
        if detail is not None:
            from backend.app.models.printer_queue import PrinterQueue

            queue = (
                await db.execute(
                    select(PrinterQueue)
                    .where(PrinterQueue.printer_id == job.printer_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            job.foreign_claim = bool(
                queue
                and queue.status == "printing"
                and (
                    queue.current_item_id != job.queue_item_id
                    or (item and item.status == "printing" and item.started_at != job.claim_started_at)
                )
            )
            raise RoutingDeferred("dispatch_claim_changed", detail=detail)

    async def _handle_routing_deferred(self, job, exc):
        from backend.app.services.filament_deferred import defer_claim
        from backend.app.services.print_scheduler import scheduler

        # Two exception types land here: ``RoutingDeferred``, which can name the
        # channel and what either side holds, and ``SourceUnavailable``, whose
        # refusal is about the file and has no channel to name.
        reason = routing_detail(exc.reason, **getattr(exc, "params", {}))
        disposition = getattr(exc, "disposition", None)
        job.outcome = {
            "success": False,
            "archive_id": job.execution_archive_id,
            "error": reason["message"],
            "cancelled": False,
            "deferred": True,
            "disposition": disposition,
            "reason": reason,
            "revision": exc.revision,
            "claim_started_at": job.claim_started_at,
            "claim_detail": getattr(exc, "detail", None),
            "source_archive_id": job.original_archive_id,
            "source_library_file_id": job.original_library_file_id,
            # Read by ``defer_claim`` if the refusal ends the attempt (direct, failed, a swap start that ran).
            "failure_code": dispatch_failure_code(exc),
        }
        released = False
        async with async_session() as db:
            if job.awaited_by_scheduler:
                # The scheduler decides this attempt's row in ``defer_claim``, which also closes the
                # archive — failed when the refusal ends the attempt, aborted when the row waits again.
                pass
            else:
                released = await defer_claim(
                    db,
                    item_id=job.queue_item_id,
                    started_at=job.claim_started_at,
                    reason=exc.reason,
                    claim_detail=getattr(exc, "detail", None),
                    revision=exc.revision,
                    direct=True,
                    source_archive_id=job.original_archive_id,
                    source_library_file_id=job.original_library_file_id,
                    restore_source=True,
                    disposition=disposition,
                    execution_archive_id=job.execution_archive_id,
                    swap_start_effect=job.swap_start_effect,
                    published=_published(job),
                    failure_code=job.outcome["failure_code"],
                )
            await db.commit()
        if released:
            await scheduler.release_prepared_dispatch(job.printer_id)
        detail = getattr(exc, "detail", None)
        blocker = getattr(exc, "blocker_archive_id", None)
        logger.info(
            "Dispatch %s deferred before publish: %s printer=%s item=%s detail=%s blocker_archive=%s",
            job.id,
            exc.reason,
            job.printer_id,
            job.queue_item_id,
            detail or "unknown",
            blocker,
        )
        # Facts, never the sentence: the tracker groups attempts of one job behind one blocker (R4).
        dispatch_stall_tracker.observe_deferral(job.printer_id, job.queue_item_id, exc.reason, detail, blocker)
        if not job.awaited_by_scheduler:
            # Only when THIS attempt still owned its row (or had none): a row the
            # operator cancelled, another attempt reclaimed or somebody deleted is
            # not a failure to report — and not one to count.
            not_started = released or job.queue_item_id is None
            await self._mark_job_finished(job, failed=not_started, message=reason["message"], deferred=True)
            if not_started:
                await announce_not_started(job, reason["message"])

    async def _strict_stagger_refuses(self, printer_id: int) -> bool:
        """Strict mode on AND this printer's group(s) have no free slot right now.

        Read every call — the toggle is a setting the operator may flip while
        jobs are queued. Any failure here answers False: strictness is a
        refinement, and a broken read must not refuse somebody's print.
        """
        try:
            from backend.app.api.routes.settings import get_setting
            from backend.app.services.print_scheduler import scheduler as print_scheduler

            async with async_session() as db:
                strict_raw = await get_setting(db, "stagger_strict_for_direct_dispatch")
            if (strict_raw or "false").lower() != "true":
                return False
            return await print_scheduler.stagger_blocks(printer_id)
        except Exception as e:
            logger.debug("Strict stagger check failed (non-fatal): %s", e)
            return False

    async def _refuse_dispatch(self, job: PrintDispatchJob, reason: str) -> None:
        """Fail a job that never reached its runner, through the runner's own exits.

        The direct claim taken at submit is released (or the printer stays
        "busy"), the job row is marked failed with the reason, the operator is
        told, and the completion event fires for anyone awaiting it. Same four
        steps as the runner's ``except``/``finally``, because those are the only
        four places a failed dispatch becomes visible.

        ⚠️ ``queue_error=False``: the item failed, the queue did not. See
        ``_release_direct_claim``.
        """
        from backend.app.utils.failure_reasons import DISPATCH_START_REFUSED

        logger.info("Background dispatch job %s refused: %s", job.id, reason)
        job.outcome = {
            "success": False,
            "archive_id": None,
            "error": reason,
            "cancelled": False,
            "deferred": False,
            "failure_code": DISPATCH_START_REFUSED,
        }
        await self._release_direct_claim(job, status="failed", queue_error=False)
        await self._mark_job_finished(job, failed=True, message=reason)
        await report_failure_if_unwatched(job)
        job.completion_event.set()

    async def _run_reprint_archive(self, job: PrintDispatchJob):
        from backend.app.main import register_expected_print, withdraw_expected_print
        from backend.app.services.print_run_binding import bind_prepared_print_run, discard_print_run

        job.outcome = {"success": False, "archive_id": None, "error": None, "cancelled": False, "deferred": False}

        async with AsyncExitStack() as pins, async_session() as db:
            service = ArchiveService(db)
            # Capture the dispatch claim, the job's own source and the original
            # refs before any source probe can fail; the failure path must be able
            # to release the claim. ``pins`` holds the blob for the whole runner.
            await self._prepare_filament_routing(db, job, pins)
            await self._capture_descriptorless_source(db, job, pins)
            await self._apply_swap_source_policy(db, job)
            # ⚠️ The source archive is provenance now, not the bytes: a job with a
            # snapshot prints the copy it took, and the row it was taken from may
            # have been purged since (spec §7). Only a job with neither is broken.
            source_archive = await service.get_archive(job.source_id) if job.source_id else None
            if not source_archive and job.source is None:
                raise SourceUnavailable()

            printer = await db.scalar(select(Printer).where(Printer.id == job.printer_id))
            if not printer:
                raise PrinterUnavailable("Printer not found")

            printer_name = printer.name
            printer_ip = printer.ip_address
            printer_access_code = printer.access_code
            printer_model = printer.model
            # The human name, from the row while it exists and from the snapshot
            # after that — never the object's own name, which is its hash (A04):
            # a payload this version cannot read refuses here instead of naming
            # the print after a hash. See ``source_display_filename``.
            archive_filename = source_archive.filename if source_archive else source_display_filename(job.source)
            submission_id = _ensure_submission_id(job)
            remote_filename = derive_remote_filename(archive_filename)

            if not printer_manager.is_connected(job.printer_id):
                raise PrinterUnavailable("Printer is not connected")

            # re-Connect MQTT if stalled
            if not await printer_manager.ensure_fresh_connection_for_printer(printer, life=job.printer_life):
                raise PrinterUnavailable("Can`t re-connect printer MQTT")

            file_path = job.source.path if job.source is not None else settings.base_dir / source_archive.file_path
            await require_source_file(file_path)

            # Unified 3MF post-processing: M970 commenting (mesh-mode-fast-check
            # off) and per-plate G-code injection (#422) share a single
            # open/mutate/write pass instead of unzipping+rezipping the file
            # twice. ``apply_3mf_transforms`` returns the source path unchanged
            # when no transform actually mutated any byte (e.g. an already-
            # patched Swaplist export, or an injection toggle without snippets
            # configured for this printer model).
            upload_file_path = file_path
            _patch_cleanup_dir = None
            # Set at registration, cleared on a confirmed send, withdrawn in
            # the finally. Initialised here so that finally cannot trip over an
            # unbound name when the body fails before registration.
            _unconfirmed_expected_print: tuple[int, str] | None = None
            inject_spec = await self._build_injection_spec(
                job=job,
                printer_model=printer_model,
                plate_id=job.options.get("plate_id") or 1,
            )
            if not job.options.get("mesh_mode_fast_check", True) or inject_spec is not None:
                from backend.app.services.gcode_patcher import apply_3mf_transforms

                patched_path, patches = await asyncio.to_thread(
                    apply_3mf_transforms,
                    file_path,
                    mesh_mode_fast_check_off=not job.options.get("mesh_mode_fast_check", True),
                    gcode_injection=inject_spec,
                )
                if patches and patched_path != file_path:
                    upload_file_path = patched_path
                    _patch_cleanup_dir = patched_path.parent
                    existing_patches = job.options.get("applied_patches") or []
                    job.options["applied_patches"] = existing_patches + patches
                    logger.info("Dispatch job %s: 3MF transformed (%s)", job.id, patches)

            # Reprint creates a NEW archive row inheriting chain identity from
            # the source — never mutates the source row. Mirrors the library-
            # file dispatch path (``_run_print_library_file``). Before this
            # rework, ``_run_reprint_archive`` reused the source archive and
            # ``on_print_start`` then unconditionally flipped its status to
            # 'printing', destroying any prior terminal state ('failed',
            # 'cancelled', or 'completed') — reprinting a failed run silently
            # erased the failure record from the print history.
            #
            # ``source_content_hash`` is forced from the source so chain-of-
            # custody groups source + reprint together (frontend dedup badge
            # uses ``COALESCE(source_content_hash, content_hash)``).
            # ``library_file_id`` carries through so the library row's
            # ``print_count`` + ``last_printed_at`` advance when this reprint
            # finishes successfully (``m014`` backfill flow). On-disk file
            # dedup inside ``archive_print`` will reuse the source's
            # ``file_path`` whenever ``content_hash`` matches (typical when
            # the reprint applies the same patches), so the new row costs
            # one DB row + zero extra disk.
            #
            # Hold the startup-lock for the DB-write critical section only
            # (mirrors library-file path). Commit closes the txn before FTP
            # starts so two parallel jobs don't race on SQLite's single
            # writer through the entire upload window.
            opts = job.options if isinstance(job.options, dict) else {}
            applied_patches = opts.get("applied_patches") if isinstance(opts, dict) else None
            swap_pending = (
                opts.get("swap_macro_events")
                if opts.get("execute_swap_macros") and "swap_mode_change_table" in (opts.get("swap_macro_events") or [])
                else None
            )
            selected_macros = opts.get("selected_macro_ids")

            await self._startup_lock.acquire()
            try:
                # Same source/dispatched split as ``_run_print_library_file``:
                # ``source_file=file_path`` carries the chain root for naming
                # + ``source_content_hash`` inheritance from the source
                # archive; ``dispatched_file=upload_file_path`` is what FTP
                # is about to upload so ``content_hash`` matches the bytes
                # the printer will read back on restart-recovery.
                archive = await service.archive_print(
                    printer_id=job.printer_id,
                    source_file=file_path,
                    dispatched_file=upload_file_path,
                    original_filename=archive_filename,
                    # The JOB's link — what the request decided and the claim row carries
                    # (WS-13 E13 Q-07) — never the source archive's: a reprint chosen
                    # without the order, or a queue row filed under another order, prints
                    # where it was filed.
                    project_id=job.project_id,
                    project_line_id=job.project_line_id,
                    # ⚠️ With a snapshot the chain root is the snapshot's own hash,
                    # and that is not a fallback for a missing row — it is the
                    # correct answer whenever one exists. ``source_content_hash``
                    # has to describe the bytes written to ``file_path`` (that is
                    # what ``effective_hash`` dedups on), and those bytes ARE the
                    # snapshot. Inheriting the row's value instead would carry a
                    # hash of whatever the original used to be, and the disk-dedup
                    # lookup would point this archive at another row's file. The
                    # two agree by construction for every chain BamDude wrote, so
                    # reprints still group with their source (spec §3, S9).
                    source_content_hash=(
                        job.source.sha256
                        if job.source is not None
                        else (source_archive.source_content_hash or source_archive.content_hash)
                    ),
                    applied_patches=applied_patches or None,
                    library_file_id=source_archive.library_file_id if source_archive else None,
                    # A captured source is stored under its hash; the archive keeps
                    # its copy under the name the folder around it already uses.
                    stored_filename=archive_filename if job.source is not None else None,
                    created_by_id=job.requested_by_user_id,
                    plate_index=job.options.get("plate_id"),
                    print_data={"status": "printing"},
                    dispatch_intent=_dispatch_intent(job, remote_filename, submission_id),
                    swap_macro_events_pending=swap_pending,
                    selected_macro_ids=selected_macros,
                    source_swap_compatible=job.source.swap_fact.value if job.source is not None else None,
                )
                if not archive:
                    raise RuntimeError("Failed to create reprint archive")
                job.execution_archive_id = archive.id
                _unobserved_attempts.discard(archive.id)  # a reused id names this attempt now
                # The durable half of the send fact, in the transaction that inserts the row (D-R3).
                job.dispatch_boot_id = dispatch_phase.stamp_prepared(archive)

                # Queue-item dispatches: re-point the queue item at the new
                # archive (the actual print this run will execute) and copy
                # queue_id + batch_id onto the new archive so the archive-
                # driven queue counters (post-m019) include it. The source
                # archive keeps its original queue_id / batch_id from when
                # IT was originally dispatched — they describe historical
                # provenance, not the current queue state.
                if job.queue_item_id:
                    from backend.app.models.print_queue import PrintQueueItem

                    q_item = await db.get(PrintQueueItem, job.queue_item_id)
                    if q_item is not None:
                        q_item.archive_id = archive.id
                        archive.queue_id = q_item.queue_id
                        archive.batch_id = q_item.batch_id
                        archive.from_auto_queue = q_item.source_auto_item_id is not None
                        job_lineage.stamp_execution(archive, q_item)

                # Print Now (no queue item): attribute the new archive to the
                # printer's default queue so GET /printer-queues/ counters
                # include it (mirrors library-file path).
                if archive.queue_id is None and job.printer_id is not None:
                    from backend.app.models.printer_queue import PrinterQueue as _PQ

                    archive.queue_id = (
                        await db.execute(select(_PQ.id).where(_PQ.printer_id == job.printer_id))
                    ).scalar_one_or_none()

                # Prefer the loaded built-in-inventory spool colours (per-slot) over
                # the slicer's 3MF colours as soon as the archive exists; usage
                # tracking refines this at completion. See services/archive_colors.py.
                from backend.app.services.archive_colors import apply_loaded_spool_colors

                await apply_loaded_spool_colors(db, archive, job.printer_id, job.options.get("ams_mapping"))

                # ⚠️ Warn, never gate. The print goes out either way — see
                # ``services/filament_deficit``. Placed here because this is the
                # one point that has all three inputs at once: the printer, the
                # mapping actually dispatched, and an archive to name.
                await _warn_on_filament_deficit(db, job, archive)

                await db.commit()
            finally:
                self._startup_lock.release()

            remote_path = f"/{remote_filename}"

            # Which medium this print goes to. Decided once, here, and carried
            # all the way to start_print — the choice of medium and the URL
            # scheme are one decision, not two.
            storage, refusal = resolve_dispatch_storage(printer_model, printer_manager.get_status(job.printer_id))
            if storage is None:
                raise PrinterUnavailable(_dispatch_refusal_message(refusal))
            transport = transport_for(printer, storage)

            ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
            self._raise_if_cancel_requested(job)

            await self._set_active_message(job, f"Preparing upload to {printer_name}...")
            await self._clear_remote_name(
                transport,
                storage,
                printer_ip=printer_ip,
                access_code=printer_access_code,
                remote_path=remote_path,
                remote_filename=remote_filename,
                ftp_timeout=ftp_timeout,
                printer_model=printer_model,
            )

            # Clean up /cache/ - delete stale .3mf and .bbl files from previous
            # prints. ⚠️ External only: /cache is a directory on the card, and
            # internal storage has no such place.
            sanitized_base = remote_filename[:-4] if remote_filename.endswith(".3mf") else remote_filename
            try:
                cache_files = (
                    await list_files_async(
                        printer_ip,
                        printer_access_code,
                        "/cache",
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                    )
                    if storage == "external"
                    else []
                )
                for f in cache_files:
                    fname = f.get("name", "")
                    # ⚠️ ``is_directory``, not ``is_dir`` — the key the FTP
                    # listing actually emits (bambu_ftp.list_files, pinned by
                    # test_bambu_ftp). Reading the wrong name meant this guard
                    # never fired, and a directory in /cache went on to DELE,
                    # which cannot delete one.
                    if f.get("is_directory"):
                        continue
                    if fname == remote_filename or fname.endswith(f"_{sanitized_base}.bbl"):
                        try:
                            await delete_file_async(
                                printer_ip,
                                printer_access_code,
                                f"/cache/{fname}",
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                            )
                            logger.info("Dispatch job %s: Deleted /cache/%s", job.id, fname)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Dispatch job %s: Cache cleanup failed (non-critical): %s", job.id, e)

            self._raise_if_cancel_requested(job)

            try:
                await self._set_active_message(
                    job, f"Uploading {archive_filename} to {printer_name}...", phase="uploading"
                )
                loop = asyncio.get_running_loop()
                progress_state = {"last_emit": 0.0, "last_bytes": 0}

                def upload_progress_callback(uploaded: int, total: int):
                    if self._is_cancel_requested(job.id):
                        raise DispatchJobCancelled(f"Dispatch job {job.id} cancelled during upload")

                    now = time.monotonic()
                    should_emit = (
                        uploaded >= total
                        or now - progress_state["last_emit"] >= 0.2
                        or uploaded - progress_state["last_bytes"] >= 256 * 1024
                    )

                    if should_emit:
                        progress_state["last_emit"] = now
                        progress_state["last_bytes"] = uploaded
                        loop.call_soon_threadsafe(
                            lambda u=uploaded, t=total: asyncio.create_task(self._set_active_upload_progress(job, u, t))
                        )

                # ⚠️ The FTP branch is untouched, retry wrapper and all. Routing
                # it through the transport would have silently dropped the
                # configured socket timeout and the retry settings on the path
                # that already works for every printer with a card.
                # The printer may go quiet on MQTT while it takes the file; that is
                # not a stale session (spec direct-print-silent-cancel §4.2).
                # Why the card upload failed, if it does — this operation's own
                # record, never a shared per-printer one (upstream 70ee5346).
                upload_report = UploadReport()
                try:
                    with printer_manager.transfer_in_progress(job.printer_id):
                        if storage != "external":
                            uploaded = await transport.upload(
                                Path(upload_file_path), remote_filename, upload_progress_callback
                            )
                        elif ftp_retry_enabled:
                            uploaded = await with_ftp_retry(
                                upload_file_async,
                                printer_ip,
                                printer_access_code,
                                upload_file_path,
                                remote_path,
                                progress_callback=upload_progress_callback,
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                                max_retries=ftp_retry_count,
                                retry_delay=ftp_retry_delay,
                                operation_name=f"Upload for reprint to {printer_name}",
                                non_retry_exceptions=(DispatchJobCancelled,),
                                report=upload_report,
                            )
                        else:
                            uploaded = await upload_file_async(
                                printer_ip,
                                printer_access_code,
                                upload_file_path,
                                remote_path,
                                progress_callback=upload_progress_callback,
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                                report=upload_report,
                            )
                except DispatchJobCancelled:
                    raise
                except Exception as exc:
                    raise UploadFailed(str(exc) or _upload_failure_message(storage, upload_report)) from exc

                if uploaded:
                    await self._set_active_upload_progress(job, 1, 1)

                if not uploaded:
                    raise UploadFailed(_upload_failure_message(storage, upload_report))
                # The file is on the printer: what follows until the start is preparation.
                job.stage = STAGE_PREPARING

                # ⚠️ Both media get a real digest. The FTP path sent an empty
                # one for as long as this code existed, reasoning that Bambu's
                # own capture puts the literal "from_sd_card" there for
                # removable media — so there was nothing to copy and no
                # evidence the field was read.
                #
                # Orca disproves it. Captured off a P1S on 2026-08-16, its
                # project_file for an `ftp://` print carries a real digest
                # ("B80E980FC407C0D09B69C81B01D5100E") of the file it had just
                # uploaded — same medium, same command, same field we sent
                # empty. Whatever the firmware does with it, a slicer in the
                # field supplies it, and matching costs one hash of bytes
                # already on disk.
                file_md5 = await source_probe(("digest", str(upload_file_path)), _file_digest, upload_file_path)

                plate_id = await source_probe(
                    ("plate", str(file_path), job.options.get("plate_id")),
                    self._resolve_plate_id,
                    file_path,
                    job.options.get("plate_id"),
                )
                # H2C nozzle rack (upstream #1784): resolved here, before preheat,
                # so a pick the rack no longer fits refuses before anything heats.
                from backend.app.services.order_auto_eject import dispatch_admission

                await dispatch_admission(db, job, printer)
                rack = await self._resolve_rack(
                    job,
                    printer,
                    upload_file_path,
                    plate_id,
                    printer_ip=printer_ip,
                    access_code=printer_access_code,
                    remote_path=remote_path,
                    printer_model=printer_model,
                )

                # Preheat / heat-soak (#1468) — bring the bed (and chamber, on supported
                # models) up to temperature on the now-idle printer before start_print.
                # Best-effort: only a cancel request propagates (via cancel_check); every
                # other failure logs and falls through to the normal start path.
                from backend.app.services.preheat import preheat_and_soak

                await preheat_and_soak(
                    db,
                    printer,
                    archive,
                    options=job.options,
                    cancel_check=lambda: self._raise_if_cancel_requested(job),
                    on_heating=lambda: self._set_active_message(job, "Preheating...", phase="heating"),
                )

                if not printer_manager.printer_life_is_current(job.printer_id, job.printer_life):
                    # The printer was deleted while this job uploaded or preheated; its id may already name
                    # the next printer, whose client must not take this attempt (spec gh109-o1 r2 R1).
                    job.foreign_claim = True
                    raise RoutingDeferred("dispatch_claim_changed", detail="claim_missing")
                prepared_run = bind_prepared_print_run(
                    printer_manager,
                    printer_id=job.printer_id,
                    archive_id=archive.id,
                    archive_identity=archive.recovery_identity,
                    queue_item_id=job.queue_item_id,
                    claim_started_at=job.claim_started_at,
                    expected_submission_id=submission_id,
                    client_generation=printer_manager.current_client_generation(job.printer_id),
                )
                if prepared_run is None:
                    # An external/other current run acquired the printer while
                    # A was uploading or preheating.  The deferred path aborts
                    # only A's execution archive and CAS-releases only A's
                    # claim; B's manager binding survives untouched.
                    job.foreign_claim = True
                    raise _bind_conflict(job.printer_id)
                dispatch_stall_tracker.observe_progress(job.printer_id, job.queue_item_id)
                register_expected_print(
                    job.printer_id,
                    remote_filename,
                    archive.id,
                    ams_mapping=job.options.get("ams_mapping"),
                    archive_identity=archive.recovery_identity,
                )
                # Withdrawn in the ``finally`` unless the print command actually
                # goes out — everything between here and ``start_print`` can still
                # fail, and a leftover entry adopts the next print of this file
                # into this archive for the next two hours.
                _unconfirmed_expected_print = (job.printer_id, remote_filename)

                self._raise_if_cancel_requested(job)

                # Swap-mode start macro — fires before the print starts. Its outcome
                # goes onto the execution archive: a hold at completion says
                # whether our swap cycle began (swap-source-lifecycle spec r4 R2-6).
                await self._run_swap_macro_if_needed(
                    job,
                    printer,
                    "swap_mode_start",
                    f"Running swap start macro on {printer_name}...",
                    record=self._start_effect_recorder(db, archive),
                )

                # Tick swap_mode_start off the pending checklist now that
                # it actually fired. Keeps extra_data["swap_macro_events_pending"]
                # honest as a "what's still to do" list (variant 2 — proper
                # checklist). Safe to write here: macro completed, start_print
                # hasn't fired yet → runtime-tracker isn't producing writes.
                from backend.app.services.archive import remove_swap_pending_event

                if archive and remove_swap_pending_event(archive, "swap_mode_start"):
                    await db.commit()

                await self._set_active_message(job, f"Starting print on {printer_name}...", phase="starting")
                # The session the bind reaches, read BEFORE it is sent: a reconnect
                # while it is on the wire then shows as a later session.
                bind_generation = _session_generation(job.printer_id)
                bound = await _apply_calibrations_for_print(
                    db=db,
                    printer_id=job.printer_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    is_calibration=bool(job.options.get("is_calibration")),
                )
                await self._ensure_live_connection_before_start(printer, printer_name, life=job.printer_life)
                # #1721: pass the user's explicit timelapse choice straight through.
                # The #1397 force-on at dispatch was removed because it flipped the
                # printer's timelapse_record_flag and un-gated the per-layer M622 J1
                # wipe blocks that Smooth-mode slicer profiles bake in — parking the
                # toolhead off the part every layer on prints the user opted out of.
                # Finish-photo capture is now driven by the stg_cur=22 transition in
                # bambu_mqtt.py (on_finish_photo_moment) with a FINISH-state fallback.
                effective_timelapse = _timelapse_or_off(
                    job.printer_id, printer, bool(job.options.get("timelapse", False))
                )
                # One bounded wait until the prepared plan holds: a new session,
                # an operator mid-swap, our own slot writes (spec dispatch-guard-
                # follows-the-plan Д6; direct-print-silent-cancel §4.3).
                await settle_plan(
                    job.routing_guard,
                    job.printer_id,
                    raise_if_cancelled=lambda: self._raise_if_cancel_requested(job),
                    on_wait=lambda _reason: self._set_active_message(
                        job, f"Waiting for a filament slot on {printer_name}...", phase="starting"
                    ),
                )
                # The pre-start K bind went to another session, or the slot now
                # holds another spool than it bound for: send it again. The final
                # check then holds the start to the session it reached.
                bound, bind_generation = await _rebind_if_moved(
                    db,
                    printer_id=job.printer_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    is_calibration=bool(job.options.get("is_calibration")),
                    bound=bound,
                    bound_generation=bind_generation,
                )
                job.routing_guard = await final_guard(
                    job.routing_guard, job.printer_id, bind_generation=bind_generation
                )
                await self._verify_routing_claim(db, job)
                from backend.app.services.order_auto_eject import dispatch_check

                await dispatch_check(db, job, printer, self._verify_routing_claim, self._raise_if_cancel_requested)
                start_command = partial(
                    printer_manager.start_print,
                    job.printer_id,
                    remote_filename,
                    plate_id,
                    routing_guard=job.routing_guard,
                    printer_life=job.printer_life,
                    ams_mapping=job.options.get("ams_mapping"),
                    timelapse=effective_timelapse,
                    bed_levelling=job.options.get("bed_levelling", True),
                    flow_cali=job.options.get("flow_cali", False),
                    layer_inspect=job.options.get("layer_inspect", False),
                    use_ams=job.options.get("use_ams", True),
                    nozzle_offset_cali=job.options.get("nozzle_offset_cali", False),
                    # A BambuStudio capture, forwarded verbatim -- or, on an H2C,
                    # the rack mapping resolved from the plan and the pick.
                    nozzle_mapping=(
                        json.dumps(rack.nozzle_mapping) if rack.nozzle_mapping else job.options.get("nozzle_mapping")
                    ),
                    # H2C only, when the plan placed nothing: the #2800 fallback,
                    # whose live dock the MQTT layer reads.
                    nozzle_slot_extruders=rack.slot_extruders,
                    # The medium and the URL scheme are one decision, carried
                    # here from where it was made rather than re-derived.
                    storage=storage,
                    file_md5=file_md5,
                    submission_id=submission_id,
                    # ⚠️ Unrelated to ``storage`` above, which is where the FILE
                    # came from. A print read off the card can record to eMMC
                    # and the other way round.
                    timelapse_storage=_timelapse_storage_for(job.printer_id, job.options.get("timelapse_storage")),
                )
                started = await self._send_start(db, job, start_command)

                if not started:
                    await self._cleanup_sd_card_file(
                        printer_ip,
                        printer_access_code,
                        remote_path,
                        printer_model,
                    )
                    # Nothing was published: a typed refusal, which the deferral path handles.
                    raise StartRefused(printer_manager.start_refusal_reason(job.printer_id))

                # Confirmed send: the entry is now the printer's to resolve.
                _unconfirmed_expected_print = None
                # The print owns the heaters from here; preheat must not
                # undo its own work on the way out.
                preheat_service.clear_pin(job.printer_id)
                # From here on only the printer's answer decides the attempt; every
                # write below is bookkeeping (D-R4).
                await self._record_phase(db, job, dispatch_phase.SENT)
                # The row keeps the mapping the printer was actually sent.
                await self._secondary(db, job, "the dispatched mapping", lambda: _record_dispatched_mapping(job))

                # Wait for the printer to actually pick up the command before
                # marking the dispatch job complete (#1042/#1134). MQTT-publish
                # success only proves the command queued locally; the printer
                # can still reject it (HMS error pending, half-broken session,
                # SD card missing) and never transition. Until #1134 this
                # watchdog was fire-and-forget — the job was reported
                # successful and the user had no signal that the print never
                # started. The uploaded file is intentionally left on the
                # printer's SD card on timeout: the next dispatch will
                # overwrite it via the existing delete-then-upload step, and
                # the printer may still be in the middle of reading it if it
                # picked up just past the timeout.
                _post_status = printer_manager.get_status(job.printer_id)
                pre_state = getattr(_post_status, "state", None)
                pre_subtask_id = getattr(_post_status, "subtask_id", None)
                pre_gcode_file = getattr(_post_status, "gcode_file", None)
                if pre_state:
                    await self._secondary(
                        db,
                        job,
                        "the dispatch panel message",
                        lambda: self._set_active_message(
                            job, f"Waiting for {printer_name} to acknowledge print...", phase="acknowledging"
                        ),
                    )
                    # The printer's answer is the only thing that may end an attempt whose
                    # start was published (D-R4): ``accepted``, or a typed negative verdict.
                    await self._require_running(
                        db,
                        job,
                        printer_name,
                        pre_state,
                        pre_subtask_id=pre_subtask_id,
                        pre_gcode_file=pre_gcode_file,
                    )

                # Register in-memory swap config for on_print_complete's fast
                # path. Persistence to archive.extra_data (restart recovery)
                # is handled where the archive row is created / loaded — see
                # archive_print's swap_macro_events_pending parameter for the
                # library-file path, and the explicit pre-stamp block below
                # the archive lookup for the reprint path.
                from backend.app.main import register_macro_selection, register_swap_config

                options = job.options if isinstance(job.options, dict) else {}
                await self._secondary(db, job, "the swap config", lambda: register_swap_config(job.printer_id, options))
                await self._secondary(
                    db, job, "the macro selection", lambda: register_macro_selection(job.printer_id, options)
                )

                # Register stagger slot so subsequent queue-driven
                # dispatches respect the grid-load cap.  Uses system-wide
                # default interval; per-printer override is queue-only.
                try:
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _stagger_enabled, _, _stagger_interval, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if _stagger_enabled:
                        print_scheduler._register_stagger_start(job.printer_id, _stagger_interval)
                except Exception as _e:
                    logger.debug("Stagger registration for direct dispatch failed: %s", _e)

                if job.requested_by_user_id and job.requested_by_username:
                    await self._secondary(
                        db,
                        job,
                        "the print's user",
                        lambda: printer_manager.set_current_print_user(
                            job.printer_id,
                            job.requested_by_user_id,
                            job.requested_by_username,
                            archive_id=job.execution_archive_id,
                        ),
                    )

                job.outcome = {
                    "success": True,
                    "archive_id": job.execution_archive_id,
                    "error": None,
                    "cancelled": False,
                    "deferred": False,
                }
            except (RoutingDeferred, SourceUnavailable, FarmStartupRefused) as exc:
                job.outcome = {
                    "success": False,
                    "archive_id": None,
                    "error": exc.reason,
                    "cancelled": False,
                    "deferred": True,
                }
                raise
            except DispatchJobCancelled:
                # Scalar ids only from here: the rollback below expires every row the session held.
                await self._end_own_transaction(db)
                await self._set_active_message(job, f"Cancelled upload on {printer_name}.")
                # archive_print committed the row before this branch, so the
                # outer session rollback can't undo it. Flip the zombie from
                # "printing" → "cancelled" in a fresh session so the UI
                # doesn't keep it spinning forever.
                await self._mark_dispatch_archive_terminal(
                    job.execution_archive_id, "cancelled", "Cancelled before start", failure_reason=USER_CANCELLED
                )
                job.outcome = {
                    "success": False,
                    "archive_id": job.execution_archive_id,
                    "error": "Cancelled",
                    "cancelled": True,
                    "deferred": False,
                }
                raise
            except asyncio.CancelledError:
                job.interrupted = True
                raise
            except Exception as e:
                await self._end_attempt_on_error(db, job, e)
                raise
            finally:
                # An expected print whose command never went out must not linger.
                # Same "every exit path" argument as the temp dir below, and the
                # same single choke point: a raise, an early return, a cancel, or
                # start_print returning False all land here.
                if _unconfirmed_expected_print is not None:
                    withdraw_expected_print(*_unconfirmed_expected_print, expected_archive_id=job.execution_archive_id)
                    if job.execution_archive_id is not None:
                        discard_print_run(printer_manager, job.printer_id, job.execution_archive_id)
                    _unconfirmed_expected_print = None
                # Same "every exit path" argument: a dispatch that dies after
                # preheat ran left the machine heating for a print that was
                # never going to happen. Nothing switched it off, because
                # nothing knew it was on. A no-op once the print has started.
                if not job.foreign_claim:
                    preheat_service.rollback(job.printer_id)
                # Patched-3MF temp dir must clean up on every exit path —
                # cancel mid-upload otherwise leaks the temp into /tmp until
                # process restart.
                if _patch_cleanup_dir:
                    import shutil

                    shutil.rmtree(_patch_cleanup_dir, ignore_errors=True)
                    _patch_cleanup_dir = None
                # ⚠️ Before the event, not after: a caller that awaits the
                # event may act on the outcome immediately, and this is the one
                # place every exit path of the runner passes through.
                await report_failure_if_unwatched(job)

    async def _send_start(self, db, job: PrintDispatchJob, start: Callable[[], bool]) -> bool:
        """Commit ``sending``, then publish — and record what the publish did before anything else runs.

        ``sending`` is committed on the runner's own session, on its own execution
        archive, after every pre-action; a commit that fails or a compare-and-set that
        loses forbids the send (D-R3). The synchronous ``validate`` inside ``start_print``
        stays the last check before the publish. ``send_fact`` is written with no await
        between the call returning and the write (D-R2). A call that raised may have
        published: it counts as sent, and the printer's answer decides — never an
        invented refusal.
        """
        from backend.app.services import dispatch_phase

        if not await dispatch_phase.advance(
            db, job.execution_archive_id, dispatch_phase.SENDING, boot_id=job.dispatch_boot_id
        ):
            raise DispatchPhaseLost("This attempt's archive is no longer the one being started; nothing was sent.")
        await db.commit()
        try:
            started = start()
        except RoutingDeferred:
            # The farm guard or the plan's own validate refused before any publish.
            job.send_fact = SEND_REFUSED
            raise
        except Exception:
            job.send_fact = SEND_SENT
            logger.exception("Dispatch %s: the start call raised; treating the command as possibly sent", job.id)
            return True
        job.send_fact = SEND_SENT if started else SEND_REFUSED
        return bool(started)

    async def _end_attempt_on_error(self, db, job: PrintDispatchJob, exc: BaseException) -> None:
        """The runner's own exit on an exception: a failed attempt with its class — unless the start may be out.

        After a publish only the printer's own verdict (``DispatchNotStarted``, a command
        rejection) ends the attempt. Anything else that escapes then leaves the archive
        and the claim as the printer has them, marked ``uncertain`` for the callers.
        """
        message = str(exc) or exc.__class__.__name__
        if _published(job) and not _ends_after_publish(exc):
            logger.error("Dispatch %s: outcome unknown after the start was sent: %s", job.id, exc, exc_info=True)
            mark_unobserved(job.execution_archive_id)
            job.outcome = {
                "success": False,
                "archive_id": job.execution_archive_id,
                "error": message,
                "cancelled": False,
                "deferred": False,
                "uncertain": True,
            }
            return
        code = dispatch_failure_code(exc, stage=job.stage)
        await self._end_own_transaction(db)
        await self._mark_dispatch_archive_terminal(job.execution_archive_id, "failed", message, failure_reason=code)
        job.outcome = {
            "success": False,
            "archive_id": job.execution_archive_id,
            "error": message,
            "cancelled": False,
            "deferred": False,
            "failure_code": code,
        }

    async def _secondary(self, db, job: PrintDispatchJob, label: str, action: Callable[[], Any]) -> None:
        """Run bookkeeping that follows a published start; its failure is logged, never the print's.

        After a publish only the printer's verdict may end the attempt (D-R4): a mapping
        write, a phase write, a registration or a cleanup that fails leaves the print
        exactly as the printer has it. The runner's own uncommitted work is rolled back
        so its session stays usable; nothing after this reads an ORM row it held.
        """
        try:
            result = action()
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - bookkeeping after a publish never decides the print
            job.secondary_errors.append(label)
            logger.error(
                "Dispatch %s on printer %s: %s failed after the start was sent: %s",
                job.id,
                job.printer_id,
                label,
                exc,
                exc_info=True,
            )
            await self._end_own_transaction(db)

    async def _record_phase(self, db, job: PrintDispatchJob, to: str) -> None:
        """Persist a phase the RAM fact already holds; bookkeeping, so its failure changes nothing."""
        from backend.app.services import dispatch_phase

        async def write():
            await dispatch_phase.advance(db, job.execution_archive_id, to, boot_id=job.dispatch_boot_id)
            await db.commit()

        await self._secondary(db, job, f"the {to} phase", write)

    async def _require_running(self, db, job: PrintDispatchJob, printer_name: str, pre_state: str, **verify) -> None:
        """The printer's answer to the published start: ``accepted`` when seen running, else the attempt ends.

        Only the printer's own answers decide (D-R4). A look that fails — our observer raised,
        or answered something that is no verdict — is looked at again with the same claim and
        the job still active, for at least ``OBSERVER_MAX_ATTEMPTS`` looks and at least
        ``OBSERVER_WATCH_SECONDS``; then the watch ends visibly (``DispatchObservationLost``),
        never with a second start.
        """
        from backend.app.services import dispatch_phase
        from backend.app.utils.failure_reasons import DISPATCH_ACCEPTED_NOT_STARTED, DISPATCH_NO_ACKNOWLEDGEMENT

        watching_since = time.monotonic()
        look = 0
        while True:
            look += 1
            try:
                answer = await self._await_print_response(job.printer_id, printer_name, pre_state, **verify)
                failure = None
            except (asyncio.CancelledError, PrintCommandRejectedError):
                raise
            except Exception as exc:  # noqa: BLE001 - our own observer failing is no verdict of the printer's
                answer, failure = None, exc
            if answer == RESPONSE_ACCEPTED:
                job.send_fact = SEND_ACCEPTED
                await self._record_phase(db, job, dispatch_phase.ACCEPTED)
                return
            if answer == RESPONSE_STALLED:
                raise DispatchNotStarted(
                    DISPATCH_ACCEPTED_NOT_STARTED,
                    "The printer accepted the file but never started printing. Check its screen for a prompt "
                    "or an error before you retry.",
                )
            if answer == RESPONSE_FILE_RECEIVED:
                raise DispatchNotStarted(
                    DISPATCH_ACCEPTED_NOT_STARTED,
                    "The printer received the file but had not started printing when the wait ended — it may "
                    "still be reading it. Check the printer before you retry.",
                )
            if answer == RESPONSE_NO_ACKNOWLEDGEMENT:
                raise DispatchNotStarted(
                    DISPATCH_NO_ACKNOWLEDGEMENT,
                    f"Printer did not acknowledge print command — state still {pre_state}. Check the printer for a "
                    "pending error (HMS code, plate-clear prompt, SD card) and try again.",
                )
            why = (
                f"{type(failure).__name__}: {failure}"
                if failure is not None
                else f"no verdict in the answer {answer!r}"
            )
            # The first failure with its traceback; the repeats of a failing observer in one line each.
            (logger.error if look == 1 else logger.warning)(
                "Dispatch %s: watching printer %s after the start was sent failed (look %d): %s",
                job.id,
                job.printer_id,
                look,
                why,
                exc_info=failure is not None and look == 1,
            )
            if look >= OBSERVER_MAX_ATTEMPTS and time.monotonic() - watching_since >= OBSERVER_WATCH_SECONDS:
                break
            await asyncio.sleep(OBSERVER_RETRY_SECONDS)
        raise DispatchObservationLost(
            f"BamDude could not watch {printer_name}'s answer to the start command ({why}). The print may be "
            "running: check the printer, then close the attempt in Info."
        )

    @staticmethod
    def _start_effect_recorder(db, archive):
        """Record the swap-start macro's outcome on the execution archive, committed before going on."""

        async def record(stage: str) -> None:
            if archive is None:
                return
            from backend.app.services import swap_terminal

            swap_terminal.record_start_effect(archive, stage)
            await db.commit()

        return record

    async def _run_swap_macro_if_needed(
        self,
        job: PrintDispatchJob,
        printer,
        event: str,
        status_message: str,
        *,
        record=None,
    ):
        """Execute a swap macro if the job's options request it for *event*.

        Raises ``RuntimeError`` on failure so the dispatch job aborts. ``record``
        is told the start macro's outcome: ``possibly_fired`` before the command
        may leave, ``fired`` once the printer confirmed it.
        """
        opts = job.options if isinstance(job.options, dict) else {}
        if not opts.get("execute_swap_macros"):
            return
        events = opts.get("swap_macro_events") or []
        if event not in events:
            return

        from backend.app.core.database import async_session
        from backend.app.services.macro_executor import find_swap_macro

        async with async_session() as db:
            macro = await find_swap_macro(db, event, printer)

        if not macro or not macro.gcode:
            logger.info(
                "Dispatch job %s: no gcode for swap event '%s' on printer %s — skipping",
                job.id,
                event,
                printer.name,
            )
            return

        await self._set_active_message(job, status_message, phase="swapping")
        if event == "swap_mode_start":
            # From the moment the command may leave, it may have happened.
            job.swap_start_effect = "possibly_fired"
            if record is not None:
                await record("possibly_fired")
        success, msg = await printer_manager.execute_macro_and_wait(job.printer_id, macro.gcode, macro.name)
        if not success:
            raise PreparationFailed(f"Swap macro '{macro.name}' failed: {msg}")
        if event == "swap_mode_start":
            job.swap_start_effect = "fired"
            if record is not None:
                await record("fired")

    async def _run_print_library_file(self, job: PrintDispatchJob):
        from backend.app.main import register_expected_print, withdraw_expected_print
        from backend.app.services.print_run_binding import bind_prepared_print_run, discard_print_run

        # Seeded in case any early branch raises — keeps the outcome shape
        # consistent for queue-item callers awaiting completion_event.
        job.outcome = {"success": False, "archive_id": None, "error": None, "cancelled": False, "deferred": False}

        async with AsyncExitStack() as pins, async_session() as db:
            await self._prepare_filament_routing(db, job, pins)
            await self._capture_descriptorless_source(db, job, pins)
            await self._apply_swap_source_policy(db, job)
            # ⚠️ The library row is provenance now, not the bytes (spec §7): the
            # file this job prints was copied into the spool when it was queued,
            # and the row may have been trashed since — which is exactly the case
            # the snapshot exists for. Only a job with neither is broken.
            lib_file = (
                await db.scalar(LibraryFile.active().where(LibraryFile.id == job.source_id)) if job.source_id else None
            )
            if not lib_file and job.source is None:
                raise SourceUnavailable()

            # Same rule as the reprint runner: the row's name while it exists, the
            # snapshot's after that, and a refusal rather than a hash — which
            # ``_require_sliced_source`` below would otherwise reject as "not a
            # sliced file", blaming a 3MF that is perfectly good.
            library_filename = lib_file.filename if lib_file else source_display_filename(job.source)
            # Plain values for the transient-source cleanup after the print started: that
            # bookkeeping may run on a session a failed write before it rolled back.
            library_row = (
                (lib_file.id, bool(lib_file.is_external), lib_file.file_path, lib_file.thumbnail_path)
                if lib_file
                else None
            )
            submission_id = _ensure_submission_id(job)
            remote_filename = derive_remote_filename(library_filename)

            file_path = job.source.path if job.source is not None else Path(settings.base_dir) / lib_file.file_path
            await require_source_file(file_path)
            await self._require_sliced_source(library_filename, file_path)

            printer = await db.scalar(select(Printer).where(Printer.id == job.printer_id))
            if not printer:
                raise PrinterUnavailable("Printer not found")

            printer_name = printer.name
            printer_ip = printer.ip_address
            printer_access_code = printer.access_code
            printer_model = printer.model

            if not printer_manager.is_connected(job.printer_id):
                raise PrinterUnavailable("Printer is not connected")

            # re-Connect MQTT if stalled
            if not await printer_manager.ensure_fresh_connection_for_printer(printer, life=job.printer_life):
                raise PrinterUnavailable("Can`t re-connect printer MQTT")

            # Unified 3MF post-processing — same single-pass pipeline as the
            # archive path above. See _maybe_inject_gcode → _build_injection_spec.
            upload_file_path = file_path
            _patch_cleanup_dir_lib = None
            # Set at registration, cleared on a confirmed send, withdrawn in
            # the finally. Initialised here so that finally cannot trip over an
            # unbound name when the body fails before registration.
            _unconfirmed_expected_print: tuple[int, str] | None = None
            inject_spec_lib = await self._build_injection_spec(
                job=job,
                printer_model=printer_model,
                plate_id=int(job.options.get("plate_id") or 1),
            )
            if not job.options.get("mesh_mode_fast_check", True) or inject_spec_lib is not None:
                from backend.app.services.gcode_patcher import apply_3mf_transforms

                patched_path, patches = await asyncio.to_thread(
                    apply_3mf_transforms,
                    file_path,
                    mesh_mode_fast_check_off=not job.options.get("mesh_mode_fast_check", True),
                    gcode_injection=inject_spec_lib,
                )
                if patches and patched_path != file_path:
                    upload_file_path = patched_path
                    _patch_cleanup_dir_lib = patched_path.parent
                    existing_patches = job.options.get("applied_patches") or []
                    job.options["applied_patches"] = existing_patches + patches
                    logger.info("Dispatch job %s: 3MF transformed (%s)", job.id, patches)

            await self._set_active_message(job, f"Creating archive for {library_filename}...")
            # Hold the startup-lock for the DB-write critical section only:
            # ``archive_print`` (heavy INSERT into print_archives + related
            # rows) plus the queue-item linking. Commit closes the txn
            # before FTP starts, so two parallel jobs no longer race on
            # SQLite's single-writer lock during a held FTP session. The
            # finally-block guarantees release on any exception path.
            await self._startup_lock.acquire()
            try:
                archive_service = ArchiveService(db)
                applied_patches = job.options.get("applied_patches") if isinstance(job.options, dict) else None
                # Two distinct files in play after the patcher:
                # - ``file_path`` is the unpatched source — the job's captured
                #   copy when it has one, else the library original — used as
                #   ``source_file`` so the archive's display name / suffix
                #   come from it and ``source_content_hash`` (set explicitly
                #   below) chains correctly to the bytes on disk.
                # - ``upload_file_path`` is the post-patch tempfile that the
                #   FTP step is about to send to the printer. Pass it as
                #   ``dispatched_file`` so ``content_hash`` reflects the
                #   bytes that actually land on the SD card. When no patch
                #   ran ``upload_file_path is file_path`` and the two
                #   hashes coincide.
                # Why this matters: ``on_print_start``'s restart-recovery
                # path (post-download adoption block in main.py) hashes
                # the printer's copy and looks for ``content_hash ==
                # temp_hash``. With the pre-fix invariant (content_hash =
                # unpatched) every BamDude restart mid-print on a patched
                # job created a fallback archive instead of adopting the
                # in-flight one. Cross-printer file dedup is on EXACT
                # ``content_hash`` and patches are deterministic, so 6
                # prints with the same patch set share a single on-disk
                # archive copy of the patched bytes (no extra disk).
                archive = await archive_service.archive_print(
                    printer_id=job.printer_id,
                    source_file=file_path,
                    dispatched_file=upload_file_path,
                    original_filename=library_filename,
                    project_id=job.project_id,
                    project_line_id=job.project_line_id,
                    # ⚠️ The snapshot's hash, not the library row's, whenever
                    # there is one: ``source_content_hash`` must describe the
                    # bytes ``archive_print`` writes to disk — which are the
                    # captured copy — because that is the key on-disk dedup
                    # reuses another row's file by. ``library_files.file_hash``
                    # is the hash of whatever is on the share NOW, and a share
                    # that changed after this job was queued (the case the spool
                    # exists for) would point this archive at the wrong bytes.
                    source_content_hash=job.source.sha256 if job.source is not None else lib_file.file_hash,
                    applied_patches=applied_patches or None,
                    library_file_id=lib_file.id if lib_file else None,
                    # A captured source is stored under its hash; the archive keeps
                    # its copy under the name the folder around it already uses.
                    stored_filename=library_filename if job.source is not None else None,
                    # Tag the resulting archive row as a calibration print
                    # when the queue item was an is_calibration job — keeps
                    # archive.kind='calibration' filter in /archives in sync
                    # with what the wizard fired off. Forwarded from
                    # PrintQueueItem via print_scheduler's options dict.
                    is_calibration=bool(job.options.get("is_calibration")),
                    calibration_session_id=job.options.get("calibration_session_id"),
                    # Forward the requesting user so per-user stats filter sees this
                    # archive and the post-print notification has a recipient. Prior
                    # to upstream #276a1db3 all library-print archives landed with
                    # created_by_id=NULL regardless of who clicked Print.
                    created_by_id=job.requested_by_user_id,
                    # Born in "printing" so the UI doesn't flash a transient
                    # "archived" label during the FTP/MQTT window (#876 follow-up).
                    # Error paths below flip it to "failed" before the txn commits.
                    print_data={"status": "printing"},
                    dispatch_intent=_dispatch_intent(job, remote_filename, submission_id),
                    # Persist swap intent in the same INSERT (post-start_print
                    # UPDATE raced the runtime-tracker on SQLite's single
                    # writer and timed out). The marker is only meaningful for
                    # ``on_print_complete``'s restart-recovery branch — fast
                    # path still uses ``_active_swap_config`` set by
                    # ``register_swap_config`` after start_print.
                    swap_macro_events_pending=(
                        job.options.get("swap_macro_events")
                        if isinstance(job.options, dict) and job.options.get("execute_swap_macros")
                        else None
                    ),
                    selected_macro_ids=(
                        job.options.get("selected_macro_ids") if isinstance(job.options, dict) else None
                    ),
                    source_swap_compatible=job.source.swap_fact.value if job.source is not None else None,
                    # Plate the user picked when scheduling — same value the
                    # dispatch loop already uses for FTP filename / MQTT
                    # start_print. Persisting it on the archive gives the
                    # file-manager + 3D viewer a hard signal of "what was
                    # actually printed" instead of guessing from filename
                    # parsing or the print_name suffix.
                    plate_index=(
                        int(job.options.get("plate_id"))
                        if isinstance(job.options, dict) and job.options.get("plate_id") is not None
                        else None
                    ),
                )
                if not archive:
                    raise RuntimeError("Failed to create archive")
                job.execution_archive_id = archive.id
                _unobserved_attempts.discard(archive.id)  # a reused id names this attempt now
                archive_file_path = archive.file_path
                # The durable half of the send fact, in the transaction that inserts the row (D-R3).
                job.dispatch_boot_id = dispatch_phase.stamp_prepared(archive)

                # Queue-item dispatches: keep queue_item + archive aligned in the
                # same txn so the scheduler's follow-up logic sees a consistent
                # view. Also copies queue_id + batch_id onto the archive so the
                # archive-driven queue counters post-m019 can find this row.
                if job.queue_item_id:
                    from backend.app.models.print_queue import PrintQueueItem

                    q_item = await db.get(PrintQueueItem, job.queue_item_id)
                    if q_item is not None:
                        q_item.archive_id = archive.id
                        archive.queue_id = q_item.queue_id
                        archive.batch_id = q_item.batch_id
                        archive.from_auto_queue = q_item.source_auto_item_id is not None
                        job_lineage.stamp_execution(archive, q_item)

                # For non-queue dispatches (Print Now qty=1), attribute the
                # archive to the printer's default queue so GET /printer-queues/
                # counters include it.
                if archive.queue_id is None and job.printer_id is not None:
                    from backend.app.models.printer_queue import PrinterQueue as _PQ

                    archive.queue_id = (
                        await db.execute(select(_PQ.id).where(_PQ.printer_id == job.printer_id))
                    ).scalar_one_or_none()

                # Prefer the loaded built-in-inventory spool colours (per-slot) over
                # the slicer's 3MF colours as soon as the archive exists; usage
                # tracking refines this at completion. See services/archive_colors.py.
                from backend.app.services.archive_colors import apply_loaded_spool_colors

                await apply_loaded_spool_colors(db, archive, job.printer_id, job.options.get("ams_mapping"))

                # Commit closes the write txn — was a flush() before, which
                # left an open txn that other jobs' archive_print INSERTs
                # contended on through the entire FTP upload window.
                await db.commit()
            finally:
                self._startup_lock.release()

            remote_path = f"/{remote_filename}"

            # Which medium this print goes to. Decided once, here, and carried
            # all the way to start_print — the choice of medium and the URL
            # scheme are one decision, not two.
            storage, refusal = resolve_dispatch_storage(printer_model, printer_manager.get_status(job.printer_id))
            if storage is None:
                raise PrinterUnavailable(_dispatch_refusal_message(refusal))
            transport = transport_for(printer, storage)

            ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
            self._raise_if_cancel_requested(job)

            await self._set_active_message(job, f"Preparing upload to {printer_name}...")
            await self._clear_remote_name(
                transport,
                storage,
                printer_ip=printer_ip,
                access_code=printer_access_code,
                remote_path=remote_path,
                remote_filename=remote_filename,
                ftp_timeout=ftp_timeout,
                printer_model=printer_model,
            )

            # Clean up /cache/ - delete stale .3mf and .bbl files from previous
            # prints. ⚠️ External only: /cache is a directory on the card, and
            # internal storage has no such place.
            sanitized_base = remote_filename[:-4] if remote_filename.endswith(".3mf") else remote_filename
            try:
                cache_files = (
                    await list_files_async(
                        printer_ip,
                        printer_access_code,
                        "/cache",
                        socket_timeout=ftp_timeout,
                        printer_model=printer_model,
                    )
                    if storage == "external"
                    else []
                )
                for f in cache_files:
                    fname = f.get("name", "")
                    # ⚠️ ``is_directory``, not ``is_dir`` — the key the FTP
                    # listing actually emits (bambu_ftp.list_files, pinned by
                    # test_bambu_ftp). Reading the wrong name meant this guard
                    # never fired, and a directory in /cache went on to DELE,
                    # which cannot delete one.
                    if f.get("is_directory"):
                        continue
                    if fname == remote_filename or fname.endswith(f"_{sanitized_base}.bbl"):
                        try:
                            await delete_file_async(
                                printer_ip,
                                printer_access_code,
                                f"/cache/{fname}",
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                            )
                            logger.info("Dispatch job %s: Deleted /cache/%s", job.id, fname)
                        except Exception:
                            pass
            except Exception as e:
                logger.debug("Dispatch job %s: Cache cleanup failed (non-critical): %s", job.id, e)

            self._raise_if_cancel_requested(job)

            try:
                await self._set_active_message(
                    job, f"Uploading {library_filename} to {printer_name}...", phase="uploading"
                )
                loop = asyncio.get_running_loop()
                progress_state = {"last_emit": 0.0, "last_bytes": 0}

                def upload_progress_callback(uploaded: int, total: int):
                    if self._is_cancel_requested(job.id):
                        raise DispatchJobCancelled(f"Dispatch job {job.id} cancelled during upload")

                    now = time.monotonic()
                    should_emit = (
                        uploaded >= total
                        or now - progress_state["last_emit"] >= 0.2
                        or uploaded - progress_state["last_bytes"] >= 256 * 1024
                    )

                    if should_emit:
                        progress_state["last_emit"] = now
                        progress_state["last_bytes"] = uploaded
                        loop.call_soon_threadsafe(
                            lambda u=uploaded, t=total: asyncio.create_task(self._set_active_upload_progress(job, u, t))
                        )

                # ⚠️ The FTP branch is untouched, retry wrapper and all. Routing
                # it through the transport would have silently dropped the
                # configured socket timeout and the retry settings on the path
                # that already works for every printer with a card.
                # The printer may go quiet on MQTT while it takes the file; that is
                # not a stale session (spec direct-print-silent-cancel §4.2).
                # Why the card upload failed, if it does — this operation's own
                # record, never a shared per-printer one (upstream 70ee5346).
                upload_report = UploadReport()
                try:
                    with printer_manager.transfer_in_progress(job.printer_id):
                        if storage != "external":
                            uploaded = await transport.upload(
                                Path(upload_file_path), remote_filename, upload_progress_callback
                            )
                        elif ftp_retry_enabled:
                            uploaded = await with_ftp_retry(
                                upload_file_async,
                                printer_ip,
                                printer_access_code,
                                upload_file_path,
                                remote_path,
                                progress_callback=upload_progress_callback,
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                                max_retries=ftp_retry_count,
                                retry_delay=ftp_retry_delay,
                                operation_name=f"Upload for print to {printer_name}",
                                non_retry_exceptions=(DispatchJobCancelled,),
                                report=upload_report,
                            )
                        else:
                            uploaded = await upload_file_async(
                                printer_ip,
                                printer_access_code,
                                upload_file_path,
                                remote_path,
                                progress_callback=upload_progress_callback,
                                socket_timeout=ftp_timeout,
                                printer_model=printer_model,
                                report=upload_report,
                            )
                except DispatchJobCancelled:
                    raise
                except Exception as exc:
                    raise UploadFailed(str(exc) or _upload_failure_message(storage, upload_report)) from exc

                if uploaded:
                    await self._set_active_upload_progress(job, 1, 1)

                if not uploaded:
                    raise UploadFailed(_upload_failure_message(storage, upload_report))
                # The file is on the printer: what follows until the start is preparation.
                job.stage = STAGE_PREPARING

                # ⚠️ Both media get a real digest. The FTP path sent an empty
                # one for as long as this code existed, reasoning that Bambu's
                # own capture puts the literal "from_sd_card" there for
                # removable media — so there was nothing to copy and no
                # evidence the field was read.
                #
                # Orca disproves it. Captured off a P1S on 2026-08-16, its
                # project_file for an `ftp://` print carries a real digest
                # ("B80E980FC407C0D09B69C81B01D5100E") of the file it had just
                # uploaded — same medium, same command, same field we sent
                # empty. Whatever the firmware does with it, a slicer in the
                # field supplies it, and matching costs one hash of bytes
                # already on disk.
                file_md5 = await source_probe(("digest", str(upload_file_path)), _file_digest, upload_file_path)

                plate_id = await source_probe(
                    ("plate", str(file_path), job.options.get("plate_id")),
                    self._resolve_plate_id,
                    file_path,
                    job.options.get("plate_id"),
                )
                # H2C nozzle rack (upstream #1784): resolved here, before preheat,
                # so a pick the rack no longer fits refuses before anything heats.
                from backend.app.services.order_auto_eject import dispatch_admission

                await dispatch_admission(db, job, printer)
                rack = await self._resolve_rack(
                    job,
                    printer,
                    upload_file_path,
                    plate_id,
                    printer_ip=printer_ip,
                    access_code=printer_access_code,
                    remote_path=remote_path,
                    printer_model=printer_model,
                )

                # Preheat / heat-soak (#1468) — same idle-window stage as the reprint
                # path: bed (and chamber, on supported models) up to temperature before
                # start_print. Best-effort; only a cancel request propagates.
                from backend.app.services.preheat import preheat_and_soak

                await preheat_and_soak(
                    db,
                    printer,
                    archive,
                    options=job.options,
                    cancel_check=lambda: self._raise_if_cancel_requested(job),
                    on_heating=lambda: self._set_active_message(job, "Preheating...", phase="heating"),
                )

                if not printer_manager.printer_life_is_current(job.printer_id, job.printer_life):
                    # The printer was deleted while this job uploaded or preheated; its id may already name
                    # the next printer, whose client must not take this attempt (spec gh109-o1 r2 R1).
                    job.foreign_claim = True
                    raise RoutingDeferred("dispatch_claim_changed", detail="claim_missing")
                prepared_run = bind_prepared_print_run(
                    printer_manager,
                    printer_id=job.printer_id,
                    archive_id=archive.id,
                    archive_identity=archive.recovery_identity,
                    queue_item_id=job.queue_item_id,
                    claim_started_at=job.claim_started_at,
                    expected_submission_id=submission_id,
                    client_generation=printer_manager.current_client_generation(job.printer_id),
                )
                if prepared_run is None:
                    # See the reprint branch: B won while A prepared, so A
                    # defers without rewriting B's run address or queue claim.
                    job.foreign_claim = True
                    raise _bind_conflict(job.printer_id)
                dispatch_stall_tracker.observe_progress(job.printer_id, job.queue_item_id)
                register_expected_print(
                    job.printer_id,
                    remote_filename,
                    archive.id,
                    ams_mapping=job.options.get("ams_mapping"),
                    archive_identity=archive.recovery_identity,
                )
                # Withdrawn in the ``finally`` unless the print command actually
                # goes out — everything between here and ``start_print`` can still
                # fail, and a leftover entry adopts the next print of this file
                # into this archive for the next two hours.
                _unconfirmed_expected_print = (job.printer_id, remote_filename)

                self._raise_if_cancel_requested(job)

                # Swap-mode start macro — fires before the print starts. Its outcome
                # goes onto the execution archive: a hold at completion says
                # whether our swap cycle began (swap-source-lifecycle spec r4 R2-6).
                await self._run_swap_macro_if_needed(
                    job,
                    printer,
                    "swap_mode_start",
                    f"Running swap start macro on {printer_name}...",
                    record=self._start_effect_recorder(db, archive),
                )

                # Tick swap_mode_start off the pending checklist now that
                # it actually fired. Keeps extra_data["swap_macro_events_pending"]
                # honest as a "what's still to do" list (variant 2 — proper
                # checklist). Safe to write here: macro completed, start_print
                # hasn't fired yet → runtime-tracker isn't producing writes.
                from backend.app.services.archive import remove_swap_pending_event

                if archive and remove_swap_pending_event(archive, "swap_mode_start"):
                    await db.commit()

                await self._set_active_message(job, f"Starting print on {printer_name}...", phase="starting")
                # The session the bind reaches, read BEFORE it is sent: a reconnect
                # while it is on the wire then shows as a later session.
                bind_generation = _session_generation(job.printer_id)
                bound = await _apply_calibrations_for_print(
                    db=db,
                    printer_id=job.printer_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    is_calibration=bool(job.options.get("is_calibration")),
                )
                await self._ensure_live_connection_before_start(printer, printer_name, life=job.printer_life)
                # #1721: pass the user's explicit timelapse choice straight through.
                # The #1397 force-on at dispatch was removed because it flipped the
                # printer's timelapse_record_flag and un-gated the per-layer M622 J1
                # wipe blocks that Smooth-mode slicer profiles bake in — parking the
                # toolhead off the part every layer on prints the user opted out of.
                # Finish-photo capture is now driven by the stg_cur=22 transition in
                # bambu_mqtt.py (on_finish_photo_moment) with a FINISH-state fallback.
                effective_timelapse = _timelapse_or_off(
                    job.printer_id, printer, bool(job.options.get("timelapse", False))
                )
                # One bounded wait until the prepared plan holds: a new session,
                # an operator mid-swap, our own slot writes (spec dispatch-guard-
                # follows-the-plan Д6; direct-print-silent-cancel §4.3).
                await settle_plan(
                    job.routing_guard,
                    job.printer_id,
                    raise_if_cancelled=lambda: self._raise_if_cancel_requested(job),
                    on_wait=lambda _reason: self._set_active_message(
                        job, f"Waiting for a filament slot on {printer_name}...", phase="starting"
                    ),
                )
                # The pre-start K bind went to another session, or the slot now
                # holds another spool than it bound for: send it again. The final
                # check then holds the start to the session it reached.
                bound, bind_generation = await _rebind_if_moved(
                    db,
                    printer_id=job.printer_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    is_calibration=bool(job.options.get("is_calibration")),
                    bound=bound,
                    bound_generation=bind_generation,
                )
                job.routing_guard = await final_guard(
                    job.routing_guard, job.printer_id, bind_generation=bind_generation
                )
                await self._verify_routing_claim(db, job)
                from backend.app.services.order_auto_eject import dispatch_check

                await dispatch_check(db, job, printer, self._verify_routing_claim, self._raise_if_cancel_requested)
                start_command = partial(
                    printer_manager.start_print,
                    job.printer_id,
                    remote_filename,
                    plate_id,
                    routing_guard=job.routing_guard,
                    printer_life=job.printer_life,
                    ams_mapping=job.options.get("ams_mapping"),
                    timelapse=effective_timelapse,
                    bed_levelling=job.options.get("bed_levelling", True),
                    flow_cali=job.options.get("flow_cali", False),
                    layer_inspect=job.options.get("layer_inspect", False),
                    use_ams=job.options.get("use_ams", True),
                    nozzle_offset_cali=job.options.get("nozzle_offset_cali", False),
                    # A BambuStudio capture, forwarded verbatim -- or, on an H2C,
                    # the rack mapping resolved from the plan and the pick.
                    nozzle_mapping=(
                        json.dumps(rack.nozzle_mapping) if rack.nozzle_mapping else job.options.get("nozzle_mapping")
                    ),
                    # H2C only, when the plan placed nothing: the #2800 fallback,
                    # whose live dock the MQTT layer reads.
                    nozzle_slot_extruders=rack.slot_extruders,
                    # The medium and the URL scheme are one decision, carried
                    # here from where it was made rather than re-derived.
                    storage=storage,
                    file_md5=file_md5,
                    submission_id=submission_id,
                    # ⚠️ Unrelated to ``storage`` above, which is where the FILE
                    # came from. A print read off the card can record to eMMC
                    # and the other way round.
                    timelapse_storage=_timelapse_storage_for(job.printer_id, job.options.get("timelapse_storage")),
                )
                started = await self._send_start(db, job, start_command)

                if not started:
                    await self._cleanup_sd_card_file(
                        printer_ip,
                        printer_access_code,
                        remote_path,
                        printer_model,
                    )
                    # Nothing was published: a typed refusal, which the deferral path handles.
                    raise StartRefused(printer_manager.start_refusal_reason(job.printer_id))

                # Confirmed send: the entry is now the printer's to resolve.
                _unconfirmed_expected_print = None
                # The print owns the heaters from here; preheat must not
                # undo its own work on the way out.
                preheat_service.clear_pin(job.printer_id)
                # From here on only the printer's answer decides the attempt; every
                # write below is bookkeeping (D-R4).
                await self._record_phase(db, job, dispatch_phase.SENT)
                # The row keeps the mapping the printer was actually sent.
                await self._secondary(db, job, "the dispatched mapping", lambda: _record_dispatched_mapping(job))

                # Register in-memory swap config for on_print_complete's fast
                # path. Persistence to archive.extra_data (restart recovery)
                # is handled where the archive row is created / loaded — see
                # archive_print's swap_macro_events_pending parameter for the
                # library-file path, and the explicit pre-stamp block below
                # the archive lookup for the reprint path.
                from backend.app.main import register_macro_selection, register_swap_config

                options = job.options if isinstance(job.options, dict) else {}
                await self._secondary(db, job, "the swap config", lambda: register_swap_config(job.printer_id, options))
                await self._secondary(
                    db, job, "the macro selection", lambda: register_macro_selection(job.printer_id, options)
                )

                # Register stagger slot so subsequent queue-driven
                # dispatches respect the grid-load cap.  Uses system-wide
                # default interval; per-printer override is queue-only.
                try:
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _stagger_enabled, _, _stagger_interval, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if _stagger_enabled:
                        print_scheduler._register_stagger_start(job.printer_id, _stagger_interval)
                except Exception as _e:
                    logger.debug("Stagger registration for direct dispatch failed: %s", _e)

                # See _run_reprint_archive for rationale (#1042/#1134). A
                # negative verdict raises ``DispatchNotStarted``, which the
                # ``except Exception`` below turns into this attempt's failed
                # archive — never a phantom row for a print that never started.
                _post_status = printer_manager.get_status(job.printer_id)
                pre_state = getattr(_post_status, "state", None)
                pre_subtask_id = getattr(_post_status, "subtask_id", None)
                pre_gcode_file = getattr(_post_status, "gcode_file", None)
                if pre_state:
                    await self._secondary(
                        db,
                        job,
                        "the dispatch panel message",
                        lambda: self._set_active_message(
                            job, f"Waiting for {printer_name} to acknowledge print...", phase="acknowledging"
                        ),
                    )
                    # The printer's answer is the only thing that may end an attempt whose
                    # start was published (D-R4): ``accepted``, or a typed negative verdict.
                    await self._require_running(
                        db,
                        job,
                        printer_name,
                        pre_state,
                        pre_subtask_id=pre_subtask_id,
                        pre_gcode_file=pre_gcode_file,
                    )

                # Register the requesting user so per-user stats filter sees
                # this print and the post-print notification has a recipient.
                # Mirrors the reprint path above — prior to upstream #276a1db3
                # the library-print branch skipped this call even though the
                # user was plumbed into the job object.
                if job.requested_by_user_id and job.requested_by_username:
                    await self._secondary(
                        db,
                        job,
                        "the print's user",
                        lambda: printer_manager.set_current_print_user(
                            job.printer_id,
                            job.requested_by_user_id,
                            job.requested_by_username,
                            archive_id=job.execution_archive_id,
                        ),
                    )

                # Direct-Print flow only: archive_print copies the 3MF, so
                # deleting the transient library row + files here leaves the
                # archive intact. ⚠️ Bookkeeping AFTER the printer accepted: a
                # failure here leaves the print printing, the library row in
                # place, and says so in the log — it never fails the print. Disk
                # deletes run AFTER commit so a rollback leaves no orphan
                # library_file row pointing at a file we already unlinked.
                # External library files (is_external=True) are never touched.
                # Upstream #730 / #1682b695.
                # ``lib_file is None`` means the row this dispatch was started
                # from is already gone — there is nothing left to clean up, and
                # the print ran from its own copy regardless.
                if job.cleanup_library_after_dispatch and library_row is not None and not library_row[1]:

                    async def remove_transient_source():
                        library_id, _external, library_path, library_thumbnail = library_row
                        # A transient library source is removed after success. Its
                        # execution archive becomes the durable source for Repeat;
                        # keep semantic rules while recording the archive revision.
                        if job.routing_guard and job.queue_item_id:
                            from backend.app.models.print_queue import PrintQueueItem
                            from backend.app.services.filament_policy import serialize_policy
                            from backend.app.services.filament_requirements import PrintRequirementsCache

                            archive_path = Path(archive_file_path)
                            if not archive_path.is_absolute():
                                archive_path = (
                                    settings.base_dir / archive_path
                                )  # SEC-PATH-OK: archive_print generated this persisted relative path.
                            retained = await PrintRequirementsCache().read(archive_path, job.options["plate_id"])
                            # FTP, preheat and MQTT acknowledgement all happened
                            # before this cleanup branch.  The initial queue-item
                            # read is still in this session's identity map, so
                            # force a real read before touching the row again.
                            queued = await db.get(PrintQueueItem, job.queue_item_id, populate_existing=True)
                            if queued is not None and queued.started_at == job.claim_started_at:
                                queued.filament_routing = serialize_policy(
                                    job.routing_guard.policy,
                                    archive_id=job.execution_archive_id,
                                    requirements=retained,
                                    plate_id=job.options["plate_id"],
                                    printer_id=job.printer_id,
                                    exact_model=job.routing_guard.exact_model,
                                )
                        cleanup_disk_paths = [
                            Path(settings.base_dir) / library_path
                        ]  # SEC-PATH-OK: the transient library row's own stored file path.
                        if library_thumbnail:
                            thumb_path = Path(library_thumbnail)
                            if not thumb_path.is_absolute():
                                thumb_path = (
                                    Path(settings.base_dir) / library_thumbnail
                                )  # SEC-PATH-OK: the transient library row's own stored thumbnail path.
                            cleanup_disk_paths.append(thumb_path)
                        # The ORM clears ``product_files`` for us, but nothing
                        # clears ``product_plates`` — it has no cascade from the
                        # library file on SQLite, where FK actions never fire.
                        await purge_file_product_links(db, [library_id])
                        transient = await db.get(LibraryFile, library_id)
                        if transient is not None:
                            await db.delete(transient)
                        await db.commit()

                        for cleanup_path in cleanup_disk_paths:
                            try:
                                if cleanup_path.exists():
                                    cleanup_path.unlink()
                            except OSError as cleanup_err:
                                logger.warning(
                                    "Failed to delete transient library file %s: %s",
                                    cleanup_path,
                                    cleanup_err,
                                )

                    await self._secondary(db, job, "the transient library cleanup", remove_transient_source)

                job.outcome = {
                    "success": True,
                    "archive_id": job.execution_archive_id,
                    "error": None,
                    "cancelled": False,
                    "deferred": False,
                }
            except (RoutingDeferred, SourceUnavailable, FarmStartupRefused) as exc:
                job.outcome = {
                    "success": False,
                    "archive_id": None,
                    "error": exc.reason,
                    "cancelled": False,
                    "deferred": True,
                }
                raise
            except DispatchJobCancelled:
                # Scalar ids only from here: the rollback expires every row the session held.
                await self._end_own_transaction(db)
                await self._set_active_message(job, f"Cancelled upload on {printer_name}.")
                # archive_print committed the row before this branch, so the
                # outer session rollback can't undo it. Flip the zombie from
                # "printing" → "cancelled" in a fresh session so the UI
                # doesn't keep it spinning forever.
                await self._mark_dispatch_archive_terminal(
                    job.execution_archive_id, "cancelled", "Cancelled before start", failure_reason=USER_CANCELLED
                )
                job.outcome = {
                    "success": False,
                    "archive_id": job.execution_archive_id,
                    "error": "Cancelled",
                    "cancelled": True,
                    "deferred": False,
                }
                raise
            except asyncio.CancelledError:
                job.interrupted = True
                raise
            except Exception as e:
                await self._end_attempt_on_error(db, job, e)
                raise
            finally:
                # An expected print whose command never went out must not linger.
                # Same "every exit path" argument as the temp dir below, and the
                # same single choke point: a raise, an early return, a cancel, or
                # start_print returning False all land here.
                if _unconfirmed_expected_print is not None:
                    withdraw_expected_print(*_unconfirmed_expected_print, expected_archive_id=job.execution_archive_id)
                    if job.execution_archive_id is not None:
                        discard_print_run(printer_manager, job.printer_id, job.execution_archive_id)
                    _unconfirmed_expected_print = None
                # Same "every exit path" argument: a dispatch that dies after
                # preheat ran left the machine heating for a print that was
                # never going to happen. Nothing switched it off, because
                # nothing knew it was on. A no-op once the print has started.
                if not job.foreign_claim:
                    preheat_service.rollback(job.printer_id)
                # Patched-3MF temp dir must clean up on every exit path —
                # cancel mid-upload otherwise leaks the temp into /tmp until
                # process restart.
                if _patch_cleanup_dir_lib:
                    import shutil

                    shutil.rmtree(_patch_cleanup_dir_lib, ignore_errors=True)
                    _patch_cleanup_dir_lib = None
                # ⚠️ Before the event, not after: a caller that awaits the
                # event may act on the outcome immediately, and this is the one
                # place every exit path of the runner passes through.
                await report_failure_if_unwatched(job)

    @staticmethod
    async def _await_print_response(
        printer_id: int,
        printer_name: str,
        pre_state: str,
        pre_subtask_id: str | None = None,
        timeout: float = 90.0,
        poll_interval: float = 3.0,
        pre_gcode_file: str | None = None,
        phase_b_timeout: float = 180.0,
    ) -> str:
        """Wait for the printer to *actually start* a print command, and say how it answered.

        Returns ``RESPONSE_ACCEPTED`` once an active print state is observed, else which of
        the three negative answers it was (post-rebuild D-R4): ``RESPONSE_STALLED`` (Phase A,
        no active state), ``RESPONSE_FILE_RECEIVED`` (the printer's file changed without
        Phase A — a slow parse) or ``RESPONSE_NO_ACKNOWLEDGEMENT``. A negative answer is the
        printer's, not a proof that it will not start the file later.

        Two-phase (#1678):

        - **Phase A — command landed.** ``subtask_id`` advancing past
          ``pre_subtask_id`` proves the printer accepted our ``project_file``
          (the H2D echoes our per-dispatch identity back on ``push_status``
          while still sitting at FINISH for ~50 s before flipping to PREPARE,
          #1078). This is NOT final success on its own.
        - **Phase B — print running.** After Phase A we keep watching, up to
          ``phase_b_timeout`` from the landing, for the printer to reach an
          active print state (PREPARE/SLICING/RUNNING/PAUSE). Only then do we
          return True.

        On timeout: if the command never landed (no active state, no subtask
        advance) we log + force an MQTT reconnect when the ``gcode_file``
        discriminator says the publish didn't land (#1150); if it landed but
        never started (Phase A reached, Phase B timed out — printer accepted
        the file then stalled, e.g. cloud+LAN re-auth after a power cycle on
        old firmware), or the file changed without Phase A (a slow parse), we
        answer WITHOUT a forced reconnect (a reconnect mid-parse would trigger
        0500_4003, #1150). The runner turns every negative answer into a
        failed attempt with its class rather than leaving it wrongly
        "started" — before #1678, Phase A alone counted as started and a
        printer that accepted-but-stalled wedged the queue item in
        ``printing`` until a container restart.
        """
        deadline = time.monotonic() + timeout
        last_status = None  # captured for #1150 gcode_file discriminator on timeout
        phase_a_reached = False  # subtask_id advanced — command landed, still watching for active state
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            state = printer_manager.get_status(printer_id)
            if not state:
                # Printer momentarily not reporting — could be a brief MQTT
                # disconnect mid-window. Keep polling rather than declaring
                # failure on the first missed tick; the printer may reconnect
                # within the remaining timeout and still surface a transition.
                continue
            last_status = state
            if state.state in _ACTIVE_PRINT_STATES:
                # Active print state — the print is genuinely running. We do NOT
                # accept arbitrary state transitions: a printer going
                # FINISH → IDLE (user dismissed the post-print prompt without
                # accepting our project_file) would otherwise look like "command
                # landed" and the dispatch job would be marked successful even
                # though no print is running. Upstream #1370 / commit 5680f5d3.
                return RESPONSE_ACCEPTED
            # Checked only AFTER the active-state exit above: a stale HMS left
            # over from an earlier job must never abort a print that is visibly
            # running. An actually-refused command leaves the printer idle, so
            # this ordering costs the detection nothing (#2732).
            if _mqtt_commands_rejected(state):
                raise PrintCommandRejectedError(
                    "The printer rejected the print command: MQTT command verification failed "
                    f"(HMS {_HMS_VERIFY_FAILED_DISPLAY}). Enable Developer Mode on the printer, "
                    "restart it, then start the job again."
                )
            if (
                not phase_a_reached
                and pre_subtask_id is not None
                and state.subtask_id is not None
                and state.subtask_id != pre_subtask_id
            ):
                # Phase A: command landed. Extend the window to phase_b_timeout
                # from here and keep watching for the active-state transition
                # instead of declaring success now (#1678).
                phase_a_reached = True
                deadline = max(deadline, time.monotonic() + phase_b_timeout)
        logger.warning(
            "Printer %s (%d) did not respond to print command within %.0fs "
            "(state still %s, subtask_id still %s) — printer may need restart",
            printer_name,
            printer_id,
            timeout,
            pre_state,
            pre_subtask_id,
        )
        if phase_a_reached:
            # Phase A landed (subtask_id advanced) but the printer never reached
            # an active print state within phase_b_timeout — it accepted the file
            # and then stalled (#1678). The publish is proven landed, so skip the
            # forced reconnect (a reconnect mid-parse triggers 0500_4003, #1150)
            # and just report the stall; the caller fails the job so the queue
            # item doesn't wedge in ``printing``.
            logger.warning(
                "Printer %s (%d): accepted project_file (subtask_id advanced) but never "
                "started printing within %.0fs — stalled after accept.",
                printer_name,
                printer_id,
                phase_b_timeout,
            )
            return RESPONSE_STALLED
        # P1P 0500_4003 discriminator (#1150): if `gcode_file` advanced from
        # what we observed pre-dispatch, the printer accepted our project_file
        # and is just slow-parsing on the SD-card MCU side. Forcing an MQTT
        # reconnect mid-parse triggers 0500_4003. Only reconnect when
        # `gcode_file` is unchanged — that's the half-broken-publish signal
        # from #887 / #936.
        current_gcode_file = getattr(last_status, "gcode_file", None) if last_status else None
        publish_landed = current_gcode_file is not None and current_gcode_file != pre_gcode_file
        if publish_landed:
            logger.warning(
                "Printer %s (%d): gcode_file changed to %r (was %r) — printer "
                "received the command and is parsing slowly. Skipping forced "
                "MQTT reconnect to avoid 0500_4003 mid-parse (#1150).",
                printer_name,
                printer_id,
                current_gcode_file,
                pre_gcode_file,
            )
            return RESPONSE_FILE_RECEIVED
        client = printer_manager.get_client(printer_id)
        if client and hasattr(client, "force_reconnect_stale_session"):
            client.force_reconnect_stale_session(
                f"print command unacknowledged after {timeout:.0f}s "
                f"(state still {pre_state}, gcode_file {current_gcode_file!r})"
            )
        return RESPONSE_NO_ACKNOWLEDGEMENT

    @staticmethod
    async def _verify_print_response(
        printer_id: int,
        printer_name: str,
        pre_state: str,
        pre_subtask_id: str | None = None,
        timeout: float = 90.0,
        poll_interval: float = 3.0,
        pre_gcode_file: str | None = None,
        phase_b_timeout: float = 180.0,
    ) -> bool:
        """True once the printer was seen running the command (``_await_print_response`` says how it answered)."""
        return (
            await BackgroundDispatchService._await_print_response(
                printer_id,
                printer_name,
                pre_state,
                pre_subtask_id=pre_subtask_id,
                timeout=timeout,
                poll_interval=poll_interval,
                pre_gcode_file=pre_gcode_file,
                phase_b_timeout=phase_b_timeout,
            )
            == RESPONSE_ACCEPTED
        )

    @staticmethod
    async def _ensure_live_connection_before_start(printer, printer_name: str, *, life: int | None = None) -> None:
        """Guarantee a live MQTT connection immediately before the start command.

        The upload + 3MF-patch + archive-write steps that precede
        ``start_print`` can take long enough for a printer to go stale in
        between — notably the P1S, whose firmware silently stops publishing
        MQTT while the TCP socket stays alive, so it reconnects far more
        often than an A1 mini. If the connection has gone stale by the time
        the print command is issued, ``start_print`` fails synchronously
        (the ``connected`` flag is False) — the job errors out and only
        "works on the second try" once the connection has recovered.
        Re-probing here (``is_connected`` runs the staleness check) and
        forcing a full reconnect when needed closes that window. Best-effort:
        a failed reconnect simply falls through to ``start_print``, which
        then fails through the existing SD-cleanup + rollback path.
        """
        # ``is_connected`` alone is blind for a minute after an upload (the stale
        # detector's post-transfer grace): a session that died during the upload
        # still looks live. A printer silent since the upload is asked for a full
        # report first, and reconnected when it does not answer — the new session
        # then settles before the final check (spec direct-print-silent-cancel §4.3).
        if printer_manager.is_connected(printer.id) and await printer_manager.confirm_heard_after_transfer(printer.id):
            return
        logger.info(
            "Dispatch: %s MQTT not live just before start_print — forcing reconnect",
            printer_name,
        )
        if not await printer_manager.connect_printer(printer, life=life):
            logger.warning("Dispatch: %s reconnect before start_print failed", printer_name)

    async def _resolve_rack(
        self,
        job: PrintDispatchJob,
        printer,
        upload_file_path,
        plate_id,
        *,
        printer_ip: str,
        access_code: str,
        remote_path: str,
        printer_model: str | None,
    ) -> RackDispatch:
        """The nozzle-rack step of both runners (upstream #1784), refusal included.

        Runs after the upload and before preheat, the swap macro and the
        expected-print registration, so a refused pick has nothing to undo but
        the file on the card -- which is deleted here: a 3MF left there is a
        phantom print waiting to be started from the touchscreen, from a hotend
        nobody chose.
        """
        status = printer_manager.get_status(job.printer_id)
        live_rack = [dict(slot) for slot in (getattr(status, "nozzle_rack", None) or []) if isinstance(slot, dict)]
        choice = job.options.get("nozzle_rack_choice")
        rack = await source_probe(
            (
                "rack",
                printer.model,
                str(upload_file_path),
                plate_id,
                json.dumps(job.options.get("nozzle_mapping")),
                json.dumps(choice, sort_keys=True, default=str),
                json.dumps(live_rack, sort_keys=True, default=str),
            ),
            _rack_dispatch,
            printer,
            upload_file_path,
            plate_id,
            job.options.get("nozzle_mapping"),
            choice,
            live_rack,
        )
        if rack.refusal:
            await self._cleanup_sd_card_file(printer_ip, access_code, remote_path, printer_model)
            raise PreparationFailed(
                f"Nozzle rack pick no longer fits the printer: {rack.refusal}. "
                "Choose another rack position and send the print again."
            )
        return rack

    @staticmethod
    async def _clear_remote_name(
        transport,
        storage: str,
        *,
        printer_ip: str,
        access_code: str,
        remote_path: str,
        remote_filename: str,
        ftp_timeout,
        printer_model: str | None,
    ) -> None:
        """Remove a same-named file before the upload; a refused card delete is a typed upload failure.

        Internal storage deletes by lookup and never raises (``delete_internal_by_name``).
        """
        if storage != "external":
            await delete_internal_by_name(transport, remote_filename)
            return
        try:
            await delete_file_async(
                printer_ip,
                access_code,
                remote_path,
                socket_timeout=ftp_timeout,
                printer_model=printer_model,
            )
        except DispatchJobCancelled:
            raise
        except Exception as exc:
            raise UploadFailed(f"Could not clear the previous copy on the printer before the upload: {exc}") from exc

    @staticmethod
    async def _cleanup_sd_card_file(
        printer_ip: str,
        access_code: str,
        remote_path: str,
        printer_model: str | None,
    ):
        """Best-effort delete of uploaded file from printer SD card."""
        try:
            await delete_file_async(printer_ip, access_code, remote_path, printer_model=printer_model)
        except Exception:
            pass  # Best-effort - don't fail the error handler

    async def _close_execution_archive(self, job: PrintDispatchJob, exc: BaseException) -> None:
        """Flip the execution archive of a job that failed outside the runner's ``try``.

        ``archive_print`` commits the row before the storage check and the
        pre-delete FTP call; a failure between them left it «printing» for a
        print that never started. Idempotent — only a still-printing row flips.
        ⚠️ Never for a start that may be on the printer: only the printer's own
        verdict ends an attempt whose start was published.
        """
        if job.execution_archive_id is None or (job.outcome or {}).get("uncertain"):
            return
        if _published(job) and not _ends_after_publish(exc):
            return
        await self._mark_dispatch_archive_terminal(
            job.execution_archive_id,
            "failed",
            str(exc) or exc.__class__.__name__,
            failure_reason=(job.outcome or {}).get("failure_code") or dispatch_failure_code(exc, stage=job.stage),
        )

    @staticmethod
    async def _end_own_transaction(db) -> None:
        """Roll back only this runner's own uncommitted work, before a cleanup writes from another session.

        A second session that writes while this one still holds SQLite's writer (or a
        row lock on PostgreSQL) waits on its own caller for the whole busy timeout. The
        ORM rows the rollback expires are not read again: every exit uses scalar ids.
        """
        try:
            await db.rollback()
        except Exception as exc:  # noqa: BLE001 - the cleanup that follows matters more
            logger.debug("Dispatch runner rollback before cleanup failed: %s", exc)

    @staticmethod
    async def _mark_dispatch_archive_terminal(
        archive_id: int | None, status: str, error_message: str, *, failure_reason: str | None = None
    ) -> None:
        """Flip a dispatch-time archive to a terminal state on error.

        ``archive_print`` commits the row before the upload/start block runs,
        so a later FTP or start-print failure can leave the archive stuck in
        "printing". This writes a terminal ``status`` + ``error_message`` +
        ``completed_at`` (+ the closed ``failure_reason``) in a fresh session —
        a compare-and-set on the row still being "printing", so neither an
        on_print_complete that raced with us nor a repeated cleanup writes twice.
        The caller has ended its own transaction first (``_end_own_transaction``).
        """
        from datetime import datetime, timezone

        from backend.app.models.archive import PrintArchive

        if archive_id is None:
            return
        values: dict[str, Any] = {
            "status": status,
            "error_message": error_message,
            "completed_at": datetime.now(timezone.utc),
        }
        if failure_reason:
            values["failure_reason"] = failure_reason
        try:
            async with async_session() as fdb:
                await fdb.execute(
                    update(PrintArchive)
                    .where(PrintArchive.id == archive_id, PrintArchive.status == "printing")
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                await fdb.commit()
        except Exception as cleanup_err:
            logger.warning(
                "Failed to mark dispatch archive %s as %s: %s",
                archive_id,
                status,
                cleanup_err,
            )

    @staticmethod
    def _resolve_plate_id(file_path: Path, requested_plate_id: int | None) -> int:
        if requested_plate_id is not None:
            return requested_plate_id

        plate_id = 1
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                for name in zf.namelist():
                    if name.startswith("Metadata/plate_") and name.endswith(".gcode"):
                        plate_str = name[15:-6]
                        plate_id = int(plate_str)
                        break
        except (ValueError, zipfile.BadZipFile, OSError):
            pass
        return plate_id

    @staticmethod
    def _is_sliced_file(filename: str) -> bool:
        lower = filename.lower()
        return lower.endswith(".gcode") or lower.endswith(".gcode.3mf")

    @staticmethod
    async def _require_sliced_source(filename: str, file_path: Path) -> None:
        """The last gate before FTP: the name says sliced, or the 3MF holds G-code.

        The name rule stays this runner's own, narrower than the queue's; the
        content arm is the one every print gate shares (upstream #2993) — a
        sliced ``Foo.3mf`` the queue accepted must not be refused here. It reads
        the bytes about to be sent (the job's snapshot when there is one), and
        the container is opened only when the name has not already settled it.
        """
        from backend.app.services.library_helpers import sliced_by_content

        if BackgroundDispatchService._is_sliced_file(filename) or await sliced_by_content(filename, file_path):
            return
        raise SourceRejected("Not a sliced file. Only G-code, or a 3MF with sliced G-code inside, can be printed.")


background_dispatch = BackgroundDispatchService()


async def enqueue_calibration_print(
    *,
    printer_id: int,
    asset_path: str,
    cali_mode: str,
    user_id: int | None,
    ams_id: int,
    slot_id: int,
    tray_id: int,
    library_file_id: int | None = None,
    print_options: dict | None = None,
    swap_macros: dict | None = None,
    calibration_session_id: int | None = None,
) -> int:
    """Enqueue a Filament Calibration print job (m062 / Plan 1).

    Creates a ``PrintQueueItem`` with ``is_calibration=True`` referencing a
    sliced ``.gcode.3mf`` LibraryFile produced by the calibration service.
    The dispatcher pipeline picks it up like any other queued item — the
    on_print_complete hook routes the linked ``calibration_session`` to
    ``awaiting_user_input`` (or ``saved`` for tower modes) instead of
    producing a normal archive entry.

    ``print_options`` / ``swap_macros`` mirror PrintModal's
    ``PrintOptions`` / ``SwapMacrosOptions``: operator-chosen toggles for
    bed-levelling / flow-cali / layer-inspect / timelapse /
    mesh-mode-fast-check / gcode-injection and the swap-macro event
    list. The scheduler reads these off the queue item when building
    dispatcher ``options``, so the same per-job behaviour you get for a
    library print is available here too. ``None`` falls back to a
    calibration-safe default (bed_levelling on, everything else off,
    swap macros disabled).

    Returns the new ``PrintQueueItem.id``. Caller updates
    ``calibration_session_id`` separately once the session row exists.
    """
    import json as _json

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    opts = print_options or {}
    swap = swap_macros or {}
    swap_events = swap.get("events") or []
    execute_swap = bool(swap.get("execute") and swap_events)

    async with async_session() as db:
        queue = (
            await db.execute(select(PrinterQueue).where(PrinterQueue.printer_id == printer_id))
        ).scalar_one_or_none()
        if queue is None:
            raise ValueError(f"No PrinterQueue for printer_id={printer_id}")

        item = PrintQueueItem(
            queue_id=queue.id,
            status="pending",
            is_calibration=True,
            calibration_session_id=calibration_session_id,
            library_file_id=library_file_id,
            created_by_id=user_id,
            ams_mapping=_json.dumps([tray_id]),
            bed_levelling=bool(opts.get("bed_levelling", True)),
            flow_cali=bool(opts.get("flow_cali", False)),
            layer_inspect=bool(opts.get("layer_inspect", False)),
            timelapse=bool(opts.get("timelapse", False)),
            timelapse_storage=opts.get("timelapse_storage"),
            mesh_mode_fast_check=bool(opts.get("mesh_mode_fast_check", True)),
            gcode_injection=bool(opts.get("gcode_injection", False)),
            execute_swap_macros=execute_swap,
            swap_macro_events=_json.dumps(swap_events) if execute_swap else None,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return item.id
