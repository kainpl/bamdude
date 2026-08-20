"""Background dispatch for print/reprint jobs.

This service is separate from the app's print queue feature. It exists only to
decouple "send/start print" operations (FTP upload + start command) from API
request latency so the UI can continue immediately after dispatch.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.core.database import async_session
from backend.app.core.websocket import ws_manager
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer

# ⚠️ Module scope on purpose: the ``finally`` below calls into this, and a
# dispatch that dies before reaching the local import would raise NameError
# from inside the cleanup — masking the real exception.
from backend.app.services import preheat as preheat_service
from backend.app.services.archive import ArchiveService
from backend.app.services.bambu_ftp import (
    delete_file_async,
    get_ftp_retry_settings,
    list_files_async,
    upload_file_async,
    with_ftp_retry,
)
from backend.app.services.gcode_patcher import GcodeInjectionSpec
from backend.app.services.printer_files.factory import transport_for
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.filename import derive_remote_filename

logger = logging.getLogger(__name__)

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


def _upload_failure_message(storage: str) -> str:
    """⚠️ An upload that failed after the medium was chosen is a different
    story from a refusal before it. The card advice belongs only on the path
    that used a card — on internal storage it would send the operator looking
    for a slot the print never went near."""
    if storage == "external":
        return "Failed to upload file to printer. Check if SD card is inserted and properly formatted (FAT32/exFAT)."
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


async def delete_internal_by_name(transport, *names: str) -> bool:
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
            if entry.name in wanted:
                await transport.delete(entry.path)
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

        loaded = _sched._build_loaded_filaments(status)
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


async def _apply_calibrations_for_print(
    db,
    printer_id: int,
    ams_mapping: list[int] | None,
    is_calibration: bool = False,
) -> None:
    """Pre-print bind hook. For every AMS slot the job will use, resolve the
    active calibration and fire ``extrusion_cali_sel``.

    Closes the silent-drift gap when prints start without going through the
    spool-link / RFID paths (queued prints, scheduled prints, manual restarts).
    Calibration prints skip this — their wizard's ``save_result`` runs its own
    bind. Best-effort: failures are logged, never block ``start_print``.
    """
    if is_calibration:
        return
    client = printer_manager.get_client(printer_id)
    if not client or not client.state.connected:
        return

    from backend.app.models.spool_assignment import SpoolAssignment as SA
    from backend.app.services.calibration_service import (
        apply_active_calibration_to_slot,
        derive_effective_filament_id,
    )

    state = printer_manager.get_status(printer_id)
    if not state:
        return

    nozzle_diameter = "0.4"
    if state.nozzles:
        nd = state.nozzles[0].nozzle_diameter
        if nd:
            nozzle_diameter = nd
    try:
        nozzle_dia_float = float(nozzle_diameter)
    except (TypeError, ValueError):
        nozzle_dia_float = 0.4
    nozzle_vt = str(getattr(state, "nozzle_volume_type", "standard") or "standard")

    ams_raw = (state.raw_data or {}).get("ams", [])
    if isinstance(ams_raw, dict):
        ams_raw = ams_raw.get("ams", [])
    if not isinstance(ams_raw, list):
        ams_raw = []

    used_global: set[int] | None = None
    if ams_mapping is not None:
        used_global = {int(s) for s in ams_mapping if isinstance(s, int) and s >= 0}

    from sqlalchemy.orm import selectinload as _sl

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
            global_slot = ams_id * 4 + slot_id
            if used_global is not None and global_slot not in used_global:
                continue
            tray_info_idx = tray.get("tray_info_idx") or ""

            slot_extruder = 0
            if state.ams_extruder_map:
                slot_extruder = state.ams_extruder_map.get(str(ams_id)) or 0

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

            filament_id = derive_effective_filament_id(spool=spool, slot_tray_info_idx=tray_info_idx or None)
            if not filament_id:
                continue
            try:
                await apply_active_calibration_to_slot(
                    db=db,
                    printer_id=printer_id,
                    ams_id=ams_id,
                    slot_id=slot_id,
                    filament_id=filament_id,
                    nozzle_diameter=nozzle_dia_float,
                    nozzle_volume_type=nozzle_vt,
                    extruder_id=slot_extruder,
                    spool_id=spool.id if spool else None,
                )
            except Exception as e:
                logger.warning(
                    "Pre-print apply failed printer=%s ams=%s slot=%s: %s",
                    printer_id,
                    ams_id,
                    slot_id,
                    e,
                )

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

            filament_id = derive_effective_filament_id(spool=spool, slot_tray_info_idx=tray_info_idx or None)
            if not filament_id:
                continue
            try:
                await apply_active_calibration_to_slot(
                    db=db,
                    printer_id=printer_id,
                    ams_id=255,
                    slot_id=ext_slot,
                    filament_id=filament_id,
                    nozzle_diameter=nozzle_dia_float,
                    nozzle_volume_type=nozzle_vt,
                    extruder_id=0,
                    spool_id=spool.id if spool else None,
                )
            except Exception as e:
                logger.warning(
                    "Pre-print apply (external) failed printer=%s vt_slot=%s: %s",
                    printer_id,
                    ext_slot,
                    e,
                )


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


@dataclass(slots=True)
class PrintDispatchJob:
    id: int
    kind: Literal["reprint_archive", "print_library_file"]
    source_id: int
    source_name: str
    printer_id: int
    printer_name: str
    options: dict[str, Any] = field(default_factory=dict)
    requested_by_user_id: int | None = None
    requested_by_username: str | None = None
    project_id: int | None = None
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
    outcome: dict[str, Any] = field(default_factory=dict)


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

    Never raises: this runs on the way out of a dispatch that has already gone
    wrong, and a notification provider being unreachable must not replace the
    real error with its own.
    """
    if job.awaited_by_scheduler:
        return
    outcome = job.outcome or {}
    if outcome.get("success") or outcome.get("cancelled"):
        return

    try:
        from backend.app.core.database import async_session
        from backend.app.services.notification_service import notification_service

        async with async_session() as db:
            await notification_service.on_queue_job_failed(
                job_name=job.source_name,
                printer_id=job.printer_id,
                printer_name=job.printer_name,
                reason=str(outcome.get("error") or "Dispatch failed"),
                db=db,
            )
    except Exception as exc:  # noqa: BLE001 — reporting a failure must not raise a second one
        logger.warning("Could not announce the failed dispatch of %s: %s", job.source_name, exc)


@dataclass(slots=True)
class ActiveDispatchState:
    job: PrintDispatchJob
    message: str
    upload_bytes: int | None = None
    upload_total_bytes: int | None = None


def _rack_slot_extruders(printer, file_path, plate_id, nozzle_mapping) -> str | None:
    """Per-slot extruder assignment for a nozzle-rack printer, as JSON, or None.

    ⚠️ **Derived at dispatch, not at queue time.** This is the first point that
    knows both the actual printer and the actual file: an item can be created
    without a printer (model-based assignment), reassigned afterwards, or have
    its file swapped for a G-code-injected copy. One call here therefore covers
    the print dialog, a bulk library add, the webhook and a pipeline run, and no
    column is needed.

    Skipped when the job already carries a BambuStudio capture from the Virtual
    Printer: that one wins downstream anyway, so reading the 3MF again would be
    work thrown away on every dispatch.

    ⚠️ Takes the file **actually being sent** — the patched copy when G-code
    injection or an M970 rewrite produced one — rather than the archive on disk.
    The patcher does not touch ``slice_info.config`` today, so the two agree;
    reading the dispatched file is what keeps that from mattering if it ever
    stops being true.

    ⚠️ Nothing here may fail a dispatch — the queue item is already committed as
    ``printing`` and the command is published with no handler above it. Every
    failure degrades to "no field, firmware picks", which is the behaviour that
    existed before.
    """
    if nozzle_mapping or file_path is None:
        return None
    from backend.app.utils.printer_models import is_nozzle_rack_model

    if not is_nozzle_rack_model(getattr(printer, "model", None)):
        return None
    import json

    from backend.app.utils.threemf_tools import extract_slot_extruders_from_3mf

    slot_extruders = extract_slot_extruders_from_3mf(file_path, plate_id=plate_id or 1)
    return json.dumps(slot_extruders) if slot_extruders else None


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
        return state.state in ("RUNNING", "PAUSE", "PAUSED") and bool(state.gcode_file)

    async def _release_direct_claim(self, job: PrintDispatchJob, *, status: str) -> None:
        """Give the printer back after a direct dispatch that will not print.

        ⚠️ Gated on ``awaited_by_scheduler``: the scheduler owns the outcome of
        its own items — including the #2598 busy-refusal that returns one to
        ``pending`` instead of failing it — and a second writer here would
        overwrite that silently.

        Never raises. This runs on the way out of a dispatch that has already
        gone wrong, and losing the real error to a secondary one from the
        cleanup is the failure this method exists to avoid.
        """
        if job.awaited_by_scheduler or job.queue_item_id is None:
            return

        from datetime import datetime, timezone

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.queue_counters import set_queue_error, set_queue_idle, update_queue_counters

        try:
            async with async_session() as db:
                item = await db.get(PrintQueueItem, job.queue_item_id)
                if item is None:
                    return
                item.status = status
                item.completed_at = datetime.now(timezone.utc)
                if status == "failed":
                    item.error_message = str((job.outcome or {}).get("error") or "Dispatch failed")
                    await set_queue_error(db, item.queue_id, failed_item_id=item.id)
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
        )

    async def get_state(self) -> dict[str, Any]:
        """Get current dispatch queue state snapshot for newly connected clients."""
        async with self._lock:
            return self._build_state_payload_unlocked()

    async def run_from_queue_item(
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
        finally:
            async with self._lock:
                self._active_jobs.pop(job.id, None)
                done_payload = self._build_state_payload_unlocked(
                    recent_event={
                        "status": "completed" if job.outcome.get("success") else "failed",
                        "job_id": job.id,
                        "source_name": source_name,
                        "printer_id": printer_id,
                        "printer_name": printer_name,
                        "message": job.outcome.get("error") or "done",
                    }
                )
            await ws_manager.broadcast({"type": "background_dispatch", "data": done_payload})

        return dict(job.outcome)

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
        cleanup_library_after_dispatch: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            has_pending_for_printer = any(job.printer_id == printer_id for job in self._queued_jobs)
            has_active_for_printer = any(active.job.printer_id == printer_id for active in self._active_jobs.values())

            if has_pending_for_printer or has_active_for_printer:
                raise DispatchEnqueueRejected(f"Printer {printer_name} already has a background dispatch in progress")

            if self._printer_is_busy_printing(printer_id):
                raise DispatchEnqueueRejected(f"Printer {printer_name} is currently busy printing")

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
            # UPDATE, and the enqueue path is not on any hot loop.
            from backend.app.services.queue_batch import claim_printer_for_direct_print

            async with async_session() as claim_db:
                claim_item = await claim_printer_for_direct_print(
                    claim_db,
                    printer_id=printer_id,
                    archive_id=source_id if kind == "reprint_archive" else None,
                    library_file_id=source_id if kind == "print_library_file" else None,
                    options=options,
                    created_by_id=requested_by_user_id,
                    project_id=project_id,
                )
                claim_item_id = claim_item.id if claim_item is not None else None

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
                cleanup_library_after_dispatch=cleanup_library_after_dispatch,
                queue_item_id=claim_item_id,
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
            await self._mark_job_finished(job, failed=False, message="Background dispatch complete")
        except DispatchJobCancelled:
            await self._release_direct_claim(job, status="cancelled")
            await self._mark_job_cancelled(job)
        except asyncio.CancelledError:
            # Process shutdown. Deliberately not released here — that would race
            # the shutdown's own session teardown, and
            # ``release_interrupted_dispatch_claims`` reclaims it at next start.
            raise
        except Exception as e:
            logger.error("Background dispatch job %s failed: %s", job.id, e, exc_info=True)
            _record_outcome_error(job, e)
            await self._release_direct_claim(job, status="failed")
            await self._mark_job_finished(job, failed=True, message=str(e))
        finally:
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
        """
        if not job.options.get("gcode_injection"):
            return None
        if not printer_model:
            logger.info("Dispatch job %s: gcode_injection on but no printer model, skipping", job.id)
            return None
        try:
            import json as _json

            from backend.app.api.routes.settings import get_setting

            async with self._session_factory() as _sdb:
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
            logger.warning("Dispatch job %s: failed to resolve gcode_snippets (%s), skipping", job.id, exc)
            return None

    async def _set_active_message(self, job: PrintDispatchJob, message: str):
        async with self._lock:
            active = self._active_jobs.get(job.id)
            if not active:
                return
            active.message = message
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

    async def _mark_job_finished(self, job: PrintDispatchJob, *, failed: bool, message: str):
        async with self._lock:
            if failed:
                self._batch_failed += 1
            else:
                self._batch_completed += 1

            self._active_jobs.pop(job.id, None)
            self._running_tasks.pop(job.id, None)
            self._cancel_requested_job_ids.discard(job.id)

            payload = self._build_state_payload_unlocked(
                recent_event={
                    "status": "failed" if failed else "completed",
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
        # Stagger gate: applies to both direct prints (cold acquire — polls
        # until a slot frees) and queue dispatch (slot was pre-registered
        # synchronously by ``print_scheduler._start_print``, so this returns
        # immediately). Lazy import — print_scheduler imports us back.
        from backend.app.services.print_scheduler import scheduler as print_scheduler

        await print_scheduler.acquire_stagger_slot(job.printer_id)

        if job.kind == "reprint_archive":
            await self._run_reprint_archive(job)
            return
        if job.kind == "print_library_file":
            await self._run_print_library_file(job)
            return
        raise RuntimeError(f"Unknown dispatch job kind: {job.kind}")

    async def _run_reprint_archive(self, job: PrintDispatchJob):
        from backend.app.main import register_expected_print, withdraw_expected_print

        job.outcome = {"success": False, "archive_id": None, "error": None, "cancelled": False}

        async with async_session() as db:
            service = ArchiveService(db)
            source_archive = await service.get_archive(job.source_id)
            if not source_archive:
                raise RuntimeError("Archive not found")

            printer = await db.scalar(select(Printer).where(Printer.id == job.printer_id))
            if not printer:
                raise RuntimeError("Printer not found")

            printer_name = printer.name
            printer_ip = printer.ip_address
            printer_access_code = printer.access_code
            printer_model = printer.model
            archive_filename = source_archive.filename

            if not printer_manager.is_connected(job.printer_id):
                raise RuntimeError("Printer is not connected")

            # re-Connect MQTT if stalled
            if not await printer_manager.ensure_fresh_connection_for_printer(printer):
                raise RuntimeError("Can`t re-connect printer MQTT")

            file_path = settings.base_dir / source_archive.file_path
            if not file_path.exists():
                raise RuntimeError("Archive file not found")

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
                plate_id=source_archive.plate_index or 1,
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
                    original_filename=source_archive.filename,
                    project_id=source_archive.project_id,
                    source_content_hash=source_archive.source_content_hash or source_archive.content_hash,
                    applied_patches=applied_patches or None,
                    library_file_id=source_archive.library_file_id,
                    created_by_id=job.requested_by_user_id,
                    plate_index=source_archive.plate_index,
                    print_data={"status": "printing"},
                    swap_macro_events_pending=swap_pending,
                    selected_macro_ids=selected_macros,
                )
                if not archive:
                    raise RuntimeError("Failed to create reprint archive")

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

            remote_filename = derive_remote_filename(source_archive.filename)
            remote_path = f"/{remote_filename}"

            # Which medium this print goes to. Decided once, here, and carried
            # all the way to start_print — the choice of medium and the URL
            # scheme are one decision, not two.
            storage, refusal = resolve_dispatch_storage(printer_model, printer_manager.get_status(job.printer_id))
            if storage is None:
                raise RuntimeError(_dispatch_refusal_message(refusal))
            transport = transport_for(printer, storage)

            ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
            self._raise_if_cancel_requested(job)

            await self._set_active_message(job, f"Preparing upload to {printer_name}...")
            if storage == "external":
                await delete_file_async(
                    printer_ip,
                    printer_access_code,
                    remote_path,
                    socket_timeout=ftp_timeout,
                    printer_model=printer_model,
                )
            else:
                await delete_internal_by_name(transport, remote_filename)

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
                await self._set_active_message(job, f"Uploading {archive_filename} to {printer_name}...")
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
                if storage != "external":
                    uploaded = await transport.upload(Path(upload_file_path), remote_filename, upload_progress_callback)
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
                    )

                if uploaded:
                    await self._set_active_upload_progress(job, 1, 1)

                if not uploaded:
                    raise RuntimeError(_upload_failure_message(storage))

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
                file_md5 = _file_digest(upload_file_path)

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
                )

                register_expected_print(
                    job.printer_id,
                    remote_filename,
                    archive.id,
                    ams_mapping=job.options.get("ams_mapping"),
                )
                # Withdrawn in the ``finally`` unless the print command actually
                # goes out — everything between here and ``start_print`` can still
                # fail, and a leftover entry adopts the next print of this file
                # into this archive for the next two hours.
                _unconfirmed_expected_print = (job.printer_id, remote_filename)

                plate_id = self._resolve_plate_id(file_path, job.options.get("plate_id"))

                self._raise_if_cancel_requested(job)

                # Strict stagger check (optional, off by default): if enabled,
                # refuse to start if no free slot, so Print Now respects the
                # grid-load cap just like queue-driven dispatches.
                try:
                    from backend.app.api.routes.settings import get_setting
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _strict_raw = await get_setting(_sdb, "stagger_strict_for_direct_dispatch")
                        _stagger_enabled, _stagger_concurrent, _, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if (
                        _stagger_enabled
                        and (_strict_raw or "false").lower() == "true"
                        and not print_scheduler._can_start_staggered(_stagger_concurrent)
                    ):
                        raise RuntimeError(
                            "Stagger cap reached — wait for a free slot or disable stagger_strict_for_direct_dispatch"
                        )
                except RuntimeError:
                    raise
                except Exception as _e:
                    logger.debug("Strict stagger check failed (non-fatal): %s", _e)

                # Swap-mode start macro — fires before the print starts.
                await self._run_swap_macro_if_needed(
                    job, printer, "swap_mode_start", f"Running swap start macro on {printer_name}..."
                )

                # Tick swap_mode_start off the pending checklist now that
                # it actually fired. Keeps extra_data["swap_macro_events_pending"]
                # honest as a "what's still to do" list (variant 2 — proper
                # checklist). Safe to write here: macro completed, start_print
                # hasn't fired yet → runtime-tracker isn't producing writes.
                from backend.app.services.archive import remove_swap_pending_event

                if archive and remove_swap_pending_event(archive, "swap_mode_start"):
                    await db.commit()

                await self._set_active_message(job, f"Starting print on {printer_name}...")
                await _apply_calibrations_for_print(
                    db=db,
                    printer_id=job.printer_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    is_calibration=bool(job.options.get("is_calibration")),
                )
                await self._ensure_live_connection_before_start(printer, printer_name)
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
                started = printer_manager.start_print(
                    job.printer_id,
                    remote_filename,
                    plate_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    timelapse=effective_timelapse,
                    bed_levelling=job.options.get("bed_levelling", True),
                    flow_cali=job.options.get("flow_cali", False),
                    layer_inspect=job.options.get("layer_inspect", False),
                    use_ams=job.options.get("use_ams", True),
                    nozzle_offset_cali=job.options.get("nozzle_offset_cali", False),
                    nozzle_mapping=job.options.get("nozzle_mapping"),
                    # H2C only: the physical rack position is resolved in the
                    # MQTT layer, where the live mounted hotend is known.
                    nozzle_slot_extruders=_rack_slot_extruders(
                        printer, upload_file_path, plate_id, job.options.get("nozzle_mapping")
                    ),
                    # The medium and the URL scheme are one decision, carried
                    # here from where it was made rather than re-derived.
                    storage=storage,
                    file_md5=file_md5,
                    # ⚠️ Unrelated to ``storage`` above, which is where the FILE
                    # came from. A print read off the card can record to eMMC
                    # and the other way round.
                    timelapse_storage=_timelapse_storage_for(job.printer_id, job.options.get("timelapse_storage")),
                )

                if started:
                    # Confirmed send: the entry is now the printer's to resolve.
                    _unconfirmed_expected_print = None
                    # The print owns the heaters from here; preheat must not
                    # undo its own work on the way out.
                    preheat_service.clear_pin(job.printer_id)

                if not started:
                    await self._cleanup_sd_card_file(
                        printer_ip,
                        printer_access_code,
                        remote_path,
                        printer_model,
                    )
                    raise RuntimeError("Failed to start print")

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
                    await self._set_active_message(job, f"Waiting for {printer_name} to acknowledge print...")
                    transitioned = await self._verify_print_response(
                        job.printer_id,
                        printer_name,
                        pre_state,
                        pre_subtask_id=pre_subtask_id,
                        pre_gcode_file=pre_gcode_file,
                    )
                    if not transitioned:
                        raise RuntimeError(
                            f"Printer did not acknowledge print command — state still {pre_state}. "
                            f"Check the printer for a pending error (HMS code, plate-clear prompt, "
                            f"SD card) and try again."
                        )

                # Register in-memory swap config for on_print_complete's fast
                # path. Persistence to archive.extra_data (restart recovery)
                # is handled where the archive row is created / loaded — see
                # archive_print's swap_macro_events_pending parameter for the
                # library-file path, and the explicit pre-stamp block below
                # the archive lookup for the reprint path.
                from backend.app.main import register_macro_selection, register_swap_config

                register_swap_config(
                    job.printer_id,
                    job.options if isinstance(job.options, dict) else {},
                )
                register_macro_selection(
                    job.printer_id,
                    job.options if isinstance(job.options, dict) else {},
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
                    printer_manager.set_current_print_user(
                        job.printer_id,
                        job.requested_by_user_id,
                        job.requested_by_username,
                    )

                job.outcome = {"success": True, "archive_id": archive.id, "error": None, "cancelled": False}
            except DispatchJobCancelled:
                await self._set_active_message(job, f"Cancelled upload on {printer_name}.")
                # archive_print committed the row before this branch, so the
                # outer session rollback can't undo it. Flip the zombie from
                # "printing" → "cancelled" in a fresh session so the UI
                # doesn't keep it spinning forever. Defensive id check in
                # case future refactors move cancel checkpoints earlier.
                _archive_id = getattr(archive, "id", None) if archive else None
                if _archive_id:
                    await self._mark_dispatch_archive_terminal(_archive_id, "cancelled", "Cancelled before start")
                job.outcome = {"success": False, "archive_id": _archive_id, "error": "Cancelled", "cancelled": True}
                raise
            except Exception as e:
                job.outcome = {"success": False, "archive_id": None, "error": str(e), "cancelled": False}
                raise
            finally:
                # An expected print whose command never went out must not linger.
                # Same "every exit path" argument as the temp dir below, and the
                # same single choke point: a raise, an early return, a cancel, or
                # start_print returning False all land here.
                if _unconfirmed_expected_print is not None:
                    withdraw_expected_print(*_unconfirmed_expected_print)
                    _unconfirmed_expected_print = None
                # Same "every exit path" argument: a dispatch that dies after
                # preheat ran left the machine heating for a print that was
                # never going to happen. Nothing switched it off, because
                # nothing knew it was on. A no-op once the print has started.
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
                job.completion_event.set()

    async def _run_swap_macro_if_needed(
        self,
        job: PrintDispatchJob,
        printer,
        event: str,
        status_message: str,
    ):
        """Execute a swap macro if the job's options request it for *event*.

        Raises ``RuntimeError`` on failure so the dispatch job aborts.
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

        await self._set_active_message(job, status_message)
        success, msg = await printer_manager.execute_macro_and_wait(job.printer_id, macro.gcode, macro.name)
        if not success:
            raise RuntimeError(f"Swap macro '{macro.name}' failed: {msg}")

    async def _run_print_library_file(self, job: PrintDispatchJob):
        from backend.app.main import register_expected_print, withdraw_expected_print

        # Seeded in case any early branch raises — keeps the outcome shape
        # consistent for queue-item callers awaiting completion_event.
        job.outcome = {"success": False, "archive_id": None, "error": None, "cancelled": False}

        async with async_session() as db:
            lib_file = await db.scalar(LibraryFile.active().where(LibraryFile.id == job.source_id))
            if not lib_file:
                raise RuntimeError("File not found")

            if not self._is_sliced_file(lib_file.filename):
                raise RuntimeError("Not a sliced file. Only .gcode or .gcode.3mf files can be printed.")

            file_path = Path(settings.base_dir) / lib_file.file_path
            if not file_path.exists():
                raise RuntimeError("File not found on disk")

            printer = await db.scalar(select(Printer).where(Printer.id == job.printer_id))
            if not printer:
                raise RuntimeError("Printer not found")

            printer_name = printer.name
            printer_ip = printer.ip_address
            printer_access_code = printer.access_code
            printer_model = printer.model
            library_filename = lib_file.filename

            if not printer_manager.is_connected(job.printer_id):
                raise RuntimeError("Printer is not connected")

            # re-Connect MQTT if stalled
            if not await printer_manager.ensure_fresh_connection_for_printer(printer):
                raise RuntimeError("Can`t re-connect printer MQTT")

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

            await self._set_active_message(job, f"Creating archive for {lib_file.filename}...")
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
                # - ``file_path`` is the unpatched library original, used as
                #   ``source_file`` so the archive's display name / suffix
                #   come from it and ``source_content_hash`` (set explicitly
                #   below from ``lib_file.file_hash``) chains correctly to
                #   the library row.
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
                    original_filename=lib_file.filename,
                    project_id=job.project_id,
                    source_content_hash=lib_file.file_hash,
                    applied_patches=applied_patches or None,
                    library_file_id=lib_file.id,
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

            remote_filename = derive_remote_filename(lib_file.filename)
            remote_path = f"/{remote_filename}"

            # Which medium this print goes to. Decided once, here, and carried
            # all the way to start_print — the choice of medium and the URL
            # scheme are one decision, not two.
            storage, refusal = resolve_dispatch_storage(printer_model, printer_manager.get_status(job.printer_id))
            if storage is None:
                raise RuntimeError(_dispatch_refusal_message(refusal))
            transport = transport_for(printer, storage)

            ftp_retry_enabled, ftp_retry_count, ftp_retry_delay, ftp_timeout = await get_ftp_retry_settings()
            self._raise_if_cancel_requested(job)

            await self._set_active_message(job, f"Preparing upload to {printer_name}...")
            if storage == "external":
                await delete_file_async(
                    printer_ip,
                    printer_access_code,
                    remote_path,
                    socket_timeout=ftp_timeout,
                    printer_model=printer_model,
                )
            else:
                await delete_internal_by_name(transport, remote_filename)

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
                await self._set_active_message(job, f"Uploading {library_filename} to {printer_name}...")
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
                if storage != "external":
                    uploaded = await transport.upload(Path(upload_file_path), remote_filename, upload_progress_callback)
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
                    )

                if uploaded:
                    await self._set_active_upload_progress(job, 1, 1)

                if not uploaded:
                    await db.rollback()
                    raise RuntimeError(_upload_failure_message(storage))

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
                file_md5 = _file_digest(upload_file_path)

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
                )

                register_expected_print(
                    job.printer_id,
                    remote_filename,
                    archive.id,
                    ams_mapping=job.options.get("ams_mapping"),
                )
                # Withdrawn in the ``finally`` unless the print command actually
                # goes out — everything between here and ``start_print`` can still
                # fail, and a leftover entry adopts the next print of this file
                # into this archive for the next two hours.
                _unconfirmed_expected_print = (job.printer_id, remote_filename)

                plate_id = self._resolve_plate_id(file_path, job.options.get("plate_id"))

                self._raise_if_cancel_requested(job)

                # Strict stagger check (optional, off by default): if enabled,
                # refuse to start if no free slot, so Print Now respects the
                # grid-load cap just like queue-driven dispatches.
                try:
                    from backend.app.api.routes.settings import get_setting
                    from backend.app.services.print_scheduler import scheduler as print_scheduler

                    async with async_session() as _sdb:
                        _strict_raw = await get_setting(_sdb, "stagger_strict_for_direct_dispatch")
                        _stagger_enabled, _stagger_concurrent, _, _ = await print_scheduler._get_stagger_settings(_sdb)
                    if (
                        _stagger_enabled
                        and (_strict_raw or "false").lower() == "true"
                        and not print_scheduler._can_start_staggered(_stagger_concurrent)
                    ):
                        raise RuntimeError(
                            "Stagger cap reached — wait for a free slot or disable stagger_strict_for_direct_dispatch"
                        )
                except RuntimeError:
                    raise
                except Exception as _e:
                    logger.debug("Strict stagger check failed (non-fatal): %s", _e)

                # Swap-mode start macro — fires before the print starts.
                await self._run_swap_macro_if_needed(
                    job, printer, "swap_mode_start", f"Running swap start macro on {printer_name}..."
                )

                # Tick swap_mode_start off the pending checklist now that
                # it actually fired. Keeps extra_data["swap_macro_events_pending"]
                # honest as a "what's still to do" list (variant 2 — proper
                # checklist). Safe to write here: macro completed, start_print
                # hasn't fired yet → runtime-tracker isn't producing writes.
                from backend.app.services.archive import remove_swap_pending_event

                if archive and remove_swap_pending_event(archive, "swap_mode_start"):
                    await db.commit()

                await self._set_active_message(job, f"Starting print on {printer_name}...")
                await _apply_calibrations_for_print(
                    db=db,
                    printer_id=job.printer_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    is_calibration=bool(job.options.get("is_calibration")),
                )
                await self._ensure_live_connection_before_start(printer, printer_name)
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
                started = printer_manager.start_print(
                    job.printer_id,
                    remote_filename,
                    plate_id,
                    ams_mapping=job.options.get("ams_mapping"),
                    timelapse=effective_timelapse,
                    bed_levelling=job.options.get("bed_levelling", True),
                    flow_cali=job.options.get("flow_cali", False),
                    layer_inspect=job.options.get("layer_inspect", False),
                    use_ams=job.options.get("use_ams", True),
                    nozzle_offset_cali=job.options.get("nozzle_offset_cali", False),
                    nozzle_mapping=job.options.get("nozzle_mapping"),
                    # H2C only: the physical rack position is resolved in the
                    # MQTT layer, where the live mounted hotend is known.
                    nozzle_slot_extruders=_rack_slot_extruders(
                        printer, upload_file_path, plate_id, job.options.get("nozzle_mapping")
                    ),
                    # The medium and the URL scheme are one decision, carried
                    # here from where it was made rather than re-derived.
                    storage=storage,
                    file_md5=file_md5,
                    # ⚠️ Unrelated to ``storage`` above, which is where the FILE
                    # came from. A print read off the card can record to eMMC
                    # and the other way round.
                    timelapse_storage=_timelapse_storage_for(job.printer_id, job.options.get("timelapse_storage")),
                )

                if started:
                    # Confirmed send: the entry is now the printer's to resolve.
                    _unconfirmed_expected_print = None
                    # The print owns the heaters from here; preheat must not
                    # undo its own work on the way out.
                    preheat_service.clear_pin(job.printer_id)

                if not started:
                    await self._cleanup_sd_card_file(
                        printer_ip,
                        printer_access_code,
                        remote_path,
                        printer_model,
                    )
                    await db.rollback()
                    raise RuntimeError("Failed to start print")

                # Register in-memory swap config for on_print_complete's fast
                # path. Persistence to archive.extra_data (restart recovery)
                # is handled where the archive row is created / loaded — see
                # archive_print's swap_macro_events_pending parameter for the
                # library-file path, and the explicit pre-stamp block below
                # the archive lookup for the reprint path.
                from backend.app.main import register_macro_selection, register_swap_config

                register_swap_config(
                    job.printer_id,
                    job.options if isinstance(job.options, dict) else {},
                )
                register_macro_selection(
                    job.printer_id,
                    job.options if isinstance(job.options, dict) else {},
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

                # See _run_reprint_archive for rationale (#1042/#1134). Outer
                # ``except Exception`` block already runs
                # ``_mark_dispatch_archive_terminal(archive.id, "failed", ...)``
                # so a RuntimeError raised here flips the freshly-created
                # archive from "printing" → "failed" without leaving a
                # phantom row for a print that never started.
                _post_status = printer_manager.get_status(job.printer_id)
                pre_state = getattr(_post_status, "state", None)
                pre_subtask_id = getattr(_post_status, "subtask_id", None)
                pre_gcode_file = getattr(_post_status, "gcode_file", None)
                if pre_state:
                    await self._set_active_message(job, f"Waiting for {printer_name} to acknowledge print...")
                    transitioned = await self._verify_print_response(
                        job.printer_id,
                        printer_name,
                        pre_state,
                        pre_subtask_id=pre_subtask_id,
                        pre_gcode_file=pre_gcode_file,
                    )
                    if not transitioned:
                        raise RuntimeError(
                            f"Printer did not acknowledge print command — state still {pre_state}. "
                            f"Check the printer for a pending error (HMS code, plate-clear prompt, "
                            f"SD card) and try again."
                        )

                # Register the requesting user so per-user stats filter sees
                # this print and the post-print notification has a recipient.
                # Mirrors the reprint path above — prior to upstream #276a1db3
                # the library-print branch skipped this call even though the
                # user was plumbed into the job object.
                if job.requested_by_user_id and job.requested_by_username:
                    printer_manager.set_current_print_user(
                        job.printer_id,
                        job.requested_by_user_id,
                        job.requested_by_username,
                    )

                # Direct-Print flow only: archive_print copies the 3MF, so
                # deleting the transient library row + files here leaves the
                # archive intact. Staged in the same transaction as everything
                # else — a mid-flight FTP / start_print failure rolls both
                # archive creation and library deletion back cleanly. Disk
                # deletes run AFTER commit so a rollback leaves no orphan
                # library_file row pointing at a file we already unlinked.
                # External library files (is_external=True) are never touched.
                # Upstream #730 / #1682b695.
                cleanup_disk_paths: list[Path] = []
                if job.cleanup_library_after_dispatch and not lib_file.is_external:
                    cleanup_disk_paths.append(Path(settings.base_dir) / lib_file.file_path)
                    if lib_file.thumbnail_path:
                        thumb_path = Path(lib_file.thumbnail_path)
                        if not thumb_path.is_absolute():
                            thumb_path = Path(settings.base_dir) / lib_file.thumbnail_path
                        cleanup_disk_paths.append(thumb_path)
                    await db.delete(lib_file)

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

                job.outcome = {"success": True, "archive_id": archive.id, "error": None, "cancelled": False}
            except DispatchJobCancelled:
                await db.rollback()
                await self._set_active_message(job, f"Cancelled upload on {printer_name}.")
                # archive_print committed the row before this branch, so the
                # outer session rollback can't undo it. Flip the zombie from
                # "printing" → "cancelled" in a fresh session so the UI
                # doesn't keep it spinning forever.
                await self._mark_dispatch_archive_terminal(archive.id, "cancelled", "Cancelled before start")
                job.outcome = {"success": False, "archive_id": archive.id, "error": "Cancelled", "cancelled": True}
                raise
            except Exception as e:
                await self._mark_dispatch_archive_terminal(archive.id, "failed", str(e))
                job.outcome = {"success": False, "archive_id": archive.id, "error": str(e), "cancelled": False}
                raise
            finally:
                # An expected print whose command never went out must not linger.
                # Same "every exit path" argument as the temp dir below, and the
                # same single choke point: a raise, an early return, a cancel, or
                # start_print returning False all land here.
                if _unconfirmed_expected_print is not None:
                    withdraw_expected_print(*_unconfirmed_expected_print)
                    _unconfirmed_expected_print = None
                # Same "every exit path" argument: a dispatch that dies after
                # preheat ran left the machine heating for a print that was
                # never going to happen. Nothing switched it off, because
                # nothing knew it was on. A no-op once the print has started.
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
                job.completion_event.set()

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
        """Wait for the printer to *actually start* a print command.

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

        Returns True once an active print state is observed. Returns False on
        timeout: if the command never landed (no active state, no subtask
        advance) we log + force an MQTT reconnect when the ``gcode_file``
        discriminator says the publish didn't land (#1150); if it landed but
        never started (Phase A reached, Phase B timed out — printer accepted
        the file then stalled, e.g. cloud+LAN re-auth after a power cycle on
        old firmware) we return False WITHOUT a forced reconnect (a reconnect
        mid-parse would trigger 0500_4003, #1150). The caller raises on False
        so the dispatch job is marked failed rather than left wrongly
        "started" — before this, Phase A alone returned True and a printer
        that accepted-but-stalled wedged the queue item in ``printing`` until
        a container restart.
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
                return True
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
            return False
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
            return False
        client = printer_manager.get_client(printer_id)
        if client and hasattr(client, "force_reconnect_stale_session"):
            client.force_reconnect_stale_session(
                f"print command unacknowledged after {timeout:.0f}s "
                f"(state still {pre_state}, gcode_file {current_gcode_file!r})"
            )
        return False

    @staticmethod
    async def _ensure_live_connection_before_start(printer, printer_name: str) -> None:
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
        if printer_manager.is_connected(printer.id):
            return
        logger.info(
            "Dispatch: %s MQTT not live just before start_print — forcing reconnect",
            printer_name,
        )
        if not await printer_manager.connect_printer(printer):
            logger.warning("Dispatch: %s reconnect before start_print failed", printer_name)

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

    @staticmethod
    async def _mark_dispatch_archive_terminal(archive_id: int, status: str, error_message: str) -> None:
        """Flip a dispatch-time archive to a terminal state on error.

        ``archive_print`` commits the row before the upload/start block runs,
        so a later FTP or start-print failure can leave the archive stuck in
        "printing". This writes a terminal ``status`` + ``error_message`` +
        ``completed_at`` in a fresh session — only if the archive is still
        in "printing", so we don't clobber an on_print_complete transition
        that raced with us.
        """
        from datetime import datetime, timezone

        from backend.app.models.archive import PrintArchive

        try:
            async with async_session() as fdb:
                archive = await fdb.get(PrintArchive, archive_id)
                if archive is None or archive.status != "printing":
                    return
                archive.status = status
                archive.error_message = error_message
                archive.completed_at = datetime.now(timezone.utc)
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
