"""Bounded reads and a single addressed writer for operator recovery."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.exc import IntegrityError

from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.calibration_session import CalibrationSession
from backend.app.models.print_completion_receipt import PrintCompletionReceipt
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import dispatch_phase, print_state_recovery_preview as previews, queue_sources
from backend.app.services.archive_write_scope import load_active_archive_for_write
from backend.app.services.print_state_recovery import (
    CLOSE_PRINT_ACTION,
    GATE_OWNER_ACTION,
    RUNTIME_ACTION,
    RecoveryFacts,
    classify,
    plan_action,
    utc,
)
from backend.app.services.queue_ops import claim_lanes
from backend.app.utils.failure_reasons import row_class

logger = logging.getLogger(__name__)
#: ``stamp -> (first seen, last seen)`` of each database-row observation (see :func:`_seen_for`).
_observations: dict[tuple, tuple[float, float]] = {}
#: The monotonic clock every observation's stability and age is measured by (a test moves it).
clock = time.monotonic
#: The same for runtime observations.
_runtime_observations: dict[tuple, tuple[float, float]] = {}
_RUNTIME_STABLE_SECONDS = 10
#: A start or completion still under way is shown only once it outlives the completion's own budget
#: (``PrintCompletionTasks`` timeout, 360 s) and a minute more: before that it is an ordinary phase.
LINGERING_SECONDS = 420
#: An observation no read has seen for this long is forgotten. Far longer than a page's revisit -- Info reads
#: every minute, the observer walks the farm a page per tick -- so a state still there never loses its age.
OBSERVATION_HORIZON_SECONDS = 1800
_MAX_OBSERVATIONS = 1000


def _seen_for(table: dict, stamp: tuple) -> float:
    """Seconds ``stamp`` has been seen unchanged: its first sighting is kept for as long as reads still see it."""
    now = clock()
    for old, (_first, last) in list(table.items()):
        if now - last > OBSERVATION_HORIZON_SECONDS:
            table.pop(old, None)
    first = table[stamp][0] if stamp in table else now
    if stamp not in table and len(table) >= _MAX_OBSERVATIONS:
        table.pop(min(table, key=lambda k: table[k][1]))  # the one unseen longest
    table[stamp] = (first, now)
    return now - first


_SHOWN_TEMPERATURES = ("bed", "bed_target", "chamber", "chamber_target")
_ITEM_FIELDS = (
    "id",
    "recovery_identity",
    "queue_id",
    "archive_id",
    "status",
    "origin",
    "started_at",
    "completed_at",
    "source_snapshot",
    "source_auto_item_id",
    "project_id",
    "project_line_id",
    "waiting_reason",
    "queue_source_id",
    "auto_eject",
    "auto_eject_settings",
    "swap_macro_events",
    "execute_swap_macros",
    # A Retry resets it to 0; any dispatch since makes the row no longer the one an old gate waits on.
    "dispatch_attempts",
)
_ARCHIVE_FIELDS = (
    "id",
    "recovery_identity",
    "printer_id",
    "queue_id",
    "status",
    "started_at",
    "completed_at",
    "deleted_at",
    "print_time_seconds",
    "extra_data",
    "dispatch_phase",
    "dispatch_boot_id",
    "failure_reason",
)
_QUEUE_FIELDS = ("id", "recovery_identity", "printer_id", "status", "is_paused", "current_item_id")


def project(row, fields):
    return {name: getattr(row, name, None) for name in fields} if row is not None else None


def live_snapshot(printer_id: int) -> dict:
    """Read owners, never resolve, cancel or clear them. Changes fence apply."""
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.print_reconciliation import _live_completions
    from backend.app.services.print_run_binding import current_print_run, finishing_print_runs
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    state, received, stale = printer_manager.peek_status(printer_id)
    run = current_print_run(printer_manager, printer_id)
    finishing = finishing_print_runs(printer_manager, printer_id)
    resolutions = getattr(printer_manager, "_print_start_resolutions", {})
    admissions = getattr(printer_manager, "_print_start_admissions", {})
    busy = bool(
        scheduler.has_dispatch_in_flight(printer_id)
        or background_dispatch.has_work_for_printer(printer_id)
        or (isinstance(resolutions, dict) and resolutions.get(printer_id))
        or (isinstance(admissions, dict) and admissions.get(printer_id) and run is None)
    )
    effects = bool(finishing or _live_completions.get(printer_id, 0))
    fresh = bool(state and state.connected and received is not None and not stale)
    identity = (
        getattr(state, "connection_generation", None),
        getattr(state, "state", None),
        getattr(state, "subtask_id", None),
        asdict(run) if run else None,
        tuple(asdict(r) for r in finishing),
        busy,
        effects,
        repr(resolutions.get(printer_id)) if isinstance(resolutions, dict) else "unknown",
    )
    mode = "unknown"
    # A fresh, idle printer with nothing of ours in flight — whatever run is still bound.
    idle = bool(fresh and not busy and not effects and state.state in {"IDLE", "FINISH", "FAILED"})
    if fresh and not busy and not effects:
        if state.state in {"IDLE", "FINISH", "FAILED"} and run is None:
            mode = "inactive"
        elif state.state in {"RUNNING", "PAUSE"} and run and str(state.subtask_id or "") not in {"", "0"}:
            if (
                str(run.observed_subtask_id or "") == str(state.subtask_id)
                and run.client_generation == state.connection_generation
            ):
                mode = "successor"
    unresolved = getattr(printer_manager, "_unproven_starts", {}).get(printer_id)
    unproven = bool(
        unresolved
        and unresolved.get("epoch") == queue_sources.current_epoch()
        and printer_manager.life_holds(printer_id, unresolved.get("life"))
    )
    return {
        "mode": mode,
        "observation_unproven": unproven,
        "identity": identity,
        "busy": busy,
        "effects": effects,
        "idle": idle,
        "item_id": run.queue_item_id if run else None,
        "archive_id": run.archive_id if run else None,
        "started_at": run.claim_started_at if run else None,
        "observed_at": received,
    }


def _ask(question) -> bool | None:
    """One owner's answer, or unknown: an accessor that fails proves nothing either way."""
    try:
        return bool(question())
    except Exception:  # noqa: BLE001 - an unknown owner is a veto, never a crash of the reader
        logger.warning("print-state runtime owner could not be read", exc_info=True)
        return None


def _registration(entry, valid) -> dict:
    present, value = entry
    if not present:
        return {"present": False, "ref": None, "value": None, "valid": True}
    if not valid(value):
        return {"present": True, "ref": id(value), "value": None, "valid": False}
    return {"present": True, "ref": id(value), "value": json.loads(json.dumps(value)), "valid": True}


def _macro_selection_valid(value) -> bool:
    return isinstance(value, list) and all(type(i) is int for i in value)


def _swap_config_valid(value) -> bool:
    if not isinstance(value, dict) or set(value) != {"swap_macro_events"}:
        return False
    events = value["swap_macro_events"]
    return isinstance(events, list) and all(isinstance(e, str) for e in events)


def runtime_snapshot(printer_id: int) -> dict:
    """The printer's in-process context as plain data (spec r3 R2). Reads only; resolves, cancels, clears nothing.

    Object references are ``id()``s: they let an addressed repair later prove it removes the very objects
    that were checked. An owner whose accessor fails is ``None`` -- unknown, never "no owner".
    """
    from backend.app import main
    from backend.app.services import layer_timelapse, preheat, swap_terminal
    from backend.app.services.archive_download_retry import archive_download_retry
    from backend.app.services.background_dispatch import background_dispatch
    from backend.app.services.macro_trigger import has_event_tasks
    from backend.app.services.print_completion_tasks import has_open_scope
    from backend.app.services.print_reconciliation import live_completion_active
    from backend.app.services.print_run_binding import (
        PrintStartAdmission,
        current_print_run,
        effect_generation,
        finishing_print_runs,
        has_pending_terminal,
        start_admission,
        start_resolution,
        terminal_accepted,
    )
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager
    from backend.app.services.smart_plug_manager import smart_plug_manager

    state, received, stale = printer_manager.peek_status(printer_id)
    fresh = bool(state and state.connected and received is not None and not stale)
    run = current_print_run(printer_manager, printer_id)
    admission = start_admission(printer_manager, printer_id)
    if admission is None:
        started = None
    elif isinstance(admission, PrintStartAdmission):
        started = {
            "ref": id(admission),
            "phase": admission.phase,
            "subtask_id": admission.subtask_id,
            "archive_id": admission.archive_id,
            "generation": admission.generation,
        }
    else:
        started = "unknown"
    archive_id = run.archive_id if run is not None else None
    owners = {
        "dispatch_in_flight": _ask(lambda: scheduler.has_dispatch_in_flight(printer_id)),
        "dispatch_work": _ask(lambda: background_dispatch.has_work_for_printer(printer_id)),
        "completion_scope": _ask(lambda: has_open_scope(printer_id)),
        "live_completion": _ask(lambda: live_completion_active(printer_id)),
        "swap_sending": _ask(lambda: archive_id is not None and swap_terminal.sending(archive_id)),
        "macro_tasks": _ask(lambda: has_event_tasks(printer_id)),
        "plug_off": _ask(lambda: smart_plug_manager.has_pending_auto_off(printer_id)),
        "bed_cooldown": _ask(lambda: main.has_bed_cooldown(printer_id)),
        "archive_download": _ask(lambda: archive_id is not None and archive_download_retry.in_progress(archive_id)),
    }
    registrations = main.run_config_registrations(printer_id)
    temperatures = getattr(state, "temperatures", None) if fresh else None
    return {
        "telemetry": {"fresh": fresh, "state": getattr(state, "state", None) if state is not None else None},
        "current": None
        if run is None
        else {
            "ref": id(run),
            "archive_id": run.archive_id,
            "archive_identity": run.archive_identity,
            "sequence": run.sequence,
            "origin": run.origin,
            "queue_item_id": run.queue_item_id,
            "expected_submission_id": run.expected_submission_id,
            "observed_subtask_id": run.observed_subtask_id,
            "effect_generation": run.effect_generation,
            "client_generation": run.client_generation,
        },
        "admission": started,
        "resolution": start_resolution(printer_manager, printer_id) is not None,
        "pending_terminal": has_pending_terminal(printer_manager, printer_id),
        "finishing": len(finishing_print_runs(printer_manager, printer_id)),
        "effect_generation": effect_generation(printer_manager, printer_id),
        "terminal_accepted": terminal_accepted(printer_manager, printer_id, run.effect_generation)
        if run is not None
        else False,
        "owners": owners,
        "macro_selection": _registration(registrations["macro_selection"], _macro_selection_valid),
        "swap_config": _registration(registrations["swap_config"], _swap_config_valid),
        # The layer timelapse is found by printer alone: a session left open would take the next print's layers.
        "layer_timelapse": _ask(lambda: layer_timelapse.get_session(printer_id) is not None),
        "preheat_pinned": preheat.pinned(printer_id),
        "plate_gate": _ask(lambda: printer_manager.is_awaiting_plate_clear(printer_id)),
        # Shown beside a residual preheat; never part of the revision (they move every second).
        "temperatures": {k: temperatures.get(k) for k in _SHOWN_TEMPERATURES}
        if isinstance(temperatures, dict)
        else None,
    }


def repair_runtime_snapshot(printer_id, archive_id):
    from backend.app.services import swap_terminal
    from backend.app.services.archive_download_retry import archive_download_retry

    snapshot = runtime_snapshot(printer_id)
    snapshot.pop("temperatures", None)
    if archive_id is not None:
        snapshot["owners"]["swap_sending"] = _ask(lambda: swap_terminal.sending(archive_id))
        snapshot["owners"]["archive_download"] = _ask(lambda: archive_download_retry.in_progress(archive_id))
    return snapshot


def _runtime_age(key, f, issue) -> float:
    """Seconds this exact observation has been seen unchanged (spec r3 R3: two equal sightings 10 s apart)."""
    r = f.runtime
    current = r.get("current") or {}
    stamp = (
        key,
        issue["code"],
        tuple(issue["blocked_reasons"]),
        current.get("ref"),
        current.get("sequence"),
        (f.archive or {}).get("status"),
        r["telemetry"].get("state"),
        queue_sources.current_epoch(),
    )
    return _seen_for(_runtime_observations, stamp)


def unknown_inbound_refs() -> bool:
    allowed = {
        ("auto_queue_items", "assigned_to_item_id"),
        ("calibration_session", "print_queue_item_id"),
        ("print_state_recovery_actions", "item_id"),
    }
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if any(fk.target_fullname == "print_queue.id" for fk in column.foreign_keys):
                if (table.name, column.name) not in allowed:
                    return True
    return False


async def read_facts(db, printer_ids: list[int], targets: list[tuple[int, str, int]], *, locked=False):
    """One page, fixed query count; never load terminal archive history."""
    from backend.app.services.background_dispatch import attempt_unobserved

    if not printer_ids:
        return {}, {}, {}
    queues = list(
        (
            await db.scalars(
                select(PrinterQueue)
                .where(PrinterQueue.printer_id.in_(printer_ids))
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    qs = {q.printer_id: q for q in queues}
    qids = [q.id for q in queues]
    ranked = (
        select(
            PrintQueueItem.id,
            PrintQueueItem.queue_id,
            func.row_number().over(partition_by=PrintQueueItem.queue_id, order_by=PrintQueueItem.id).label("n"),
        )
        .where(PrintQueueItem.queue_id.in_(qids), PrintQueueItem.status == "printing")
        .subquery()
    )
    group_ids = set((await db.scalars(select(ranked.c.id).where(ranked.c.n <= 101))).all())
    target_ids = [key for _, kind, key in targets if kind == "item"]
    needed = group_ids | set(target_ids) | {q.current_item_id for q in queues if q.current_item_id}
    item_stmt = select(PrintQueueItem).where(PrintQueueItem.id.in_(needed)).execution_options(populate_existing=True)
    if locked:
        item_stmt = item_stmt.order_by(PrintQueueItem.id).with_for_update()
    items = {i.id: i for i in (await db.scalars(item_stmt)).all()}
    counts = dict(
        (
            await db.execute(
                select(PrintQueueItem.queue_id, func.count())
                .where(PrintQueueItem.queue_id.in_(qids), PrintQueueItem.status == "printing")
                .group_by(PrintQueueItem.queue_id)
            )
        ).all()
    )
    aid = {i.archive_id for i in items.values() if i.archive_id}
    # A runtime row names its run's execution archive: loaded by that id, never by scanning history.
    aid.update(key for _, kind, key in targets if kind == "archive" or (kind == "runtime" and key))
    archives = {
        a.id: a
        for a in (
            await db.scalars(
                select(PrintArchive).where(PrintArchive.id.in_(aid)).execution_options(populate_existing=True)
            )
        ).all()
    }
    acounts = dict(
        (
            await db.execute(
                select(PrintArchive.printer_id, func.count())
                .where(
                    PrintArchive.printer_id.in_(printer_ids),
                    PrintArchive.status == "printing",
                    PrintArchive.deleted_at.is_(None),
                )
                .group_by(PrintArchive.printer_id)
            )
        ).all()
    )
    users = dict(
        (
            await db.execute(
                select(PrintQueueItem.archive_id, func.count())
                .where(PrintQueueItem.archive_id.in_(aid))
                .group_by(PrintQueueItem.archive_id)
            )
        ).all()
    )
    refs = {key: set() for key in needed}
    for _model, col, reason in (
        (AutoQueueItem, AutoQueueItem.assigned_to_item_id, "auto_child"),
        (CalibrationSession, CalibrationSession.print_queue_item_id, "calibration"),
        (PrintCompletionReceipt, PrintCompletionReceipt.rearmed_queue_item_id, "receipt"),
    ):
        for value in (await db.scalars(select(col).where(col.in_(needed)))).all():
            refs[value].add(reason)
    printing_ids = [a.id for a in archives.values() if a.status == "printing"]
    completed_execution_ids = (
        set(
            (
                await db.scalars(
                    select(PrintCompletionReceipt.archive_id).where(PrintCompletionReceipt.archive_id.in_(printing_ids))
                )
            ).all()
        )
        if printing_ids
        else set()
    )
    active_users = dict(
        (
            await db.execute(
                select(PrintQueueItem.archive_id, func.count())
                .where(PrintQueueItem.archive_id.in_(aid), PrintQueueItem.status == "printing")
                .group_by(PrintQueueItem.archive_id)
            )
        ).all()
    )
    unknown_ref = unknown_inbound_refs()
    # The plate gate as stored, one read for the page (spec plate-gate-owner-requeue B2). Part of each row's
    # facts, so a preview names the gate it was made for and apply refuses a gate armed or answered since.
    gates = {
        row.id: {
            "armed": bool(row.awaiting_plate_clear),
            "archive_id": row.awaiting_plate_clear_archive_id,
            "token": row.awaiting_plate_clear_token,
        }
        for row in (
            await db.execute(
                select(
                    Printer.id,
                    Printer.awaiting_plate_clear,
                    Printer.awaiting_plate_clear_archive_id,
                    Printer.awaiting_plate_clear_token,
                ).where(Printer.id.in_(printer_ids))
            )
        ).all()
    }
    # Read once for the page: a boot is proof only while this process holds the owner lock (I-R2).
    current_boot = dispatch_phase.current_boot_id()
    result = {}
    now = datetime.now(timezone.utc)
    for pid, kind, key in targets:
        q = qs.get(pid)
        runtime = None
        if kind == "runtime":
            # A printer with no queue still has its row; it only cannot be repaired (R7.3).
            runtime = runtime_snapshot(pid)
            if ((runtime["current"] or {}).get("archive_id") or 0) != key:
                continue  # the run this row named is not the one held now
        elif q is None:
            continue
        item = items.get(key) if kind == "item" else None
        if kind == "item" and (item is None or item.queue_id != q.id):
            continue
        current = items.get(q.current_item_id) if q is not None else None
        if kind == "runtime":
            archive = archives.get(key) if key else None
        else:
            archive = (
                archives.get(item.archive_id)
                if item
                else archives.get(key)
                if kind == "archive"
                else (archives.get(current.archive_id) if current and current.queue_id == q.id else None)
            )
        if kind == "header" and key != q.id:
            continue
        if kind == "archive" and (archive is None or archive.printer_id != pid):
            continue
        members = tuple(
            project(r, _ITEM_FIELDS)
            for r in sorted(items.values(), key=lambda r: r.id)
            if q is not None and r.queue_id == q.id and r.status == "printing"
        )
        if kind == "header" and members and len({r["archive_id"] for r in members}) == 1:
            archive = archives.get(members[0]["archive_id"])
        repair_runtime = (
            repair_runtime_snapshot(pid, archive.id if archive else None) if members or kind == "archive" else None
        )
        live = live_snapshot(pid)
        if live["mode"] == "successor":
            successor = archives.get(live["archive_id"])
            if (
                current is None
                or current.status != "printing"
                or current.queue_id != q.id
                or current.id != live["item_id"]
                or current.archive_id != live["archive_id"]
                or successor is None
                or successor.status != "printing"
                or successor.printer_id != pid
                or successor.queue_id != q.id
                or utc(current.started_at) != utc(live["started_at"])
                or not successor.started_at
                or utc(successor.started_at) < utc(current.started_at)
                or acounts.get(pid, 0) != 1
            ):
                live = {**live, "mode": "unknown"}
        active = tuple(i.id for i in items.values() if q is not None and i.queue_id == q.id and i.status == "printing")
        if q is not None and counts.get(q.id, 0) > len(active):
            active += (-1,)
        references = set(refs.get(key, ())) if item else set()
        if item:
            if item.source_auto_item_id:
                references.add("auto_parent")
            if item.project_id or item.project_line_id:
                references.add("project")
            if unknown_ref:
                references.add("unknown_fk")
        elif (
            kind not in {"header", "archive"}
            and current is not None
            and (current.queue_id != q.id or current.status == "printing")
        ):
            references.add("ambiguous_owner")
        if kind in {"header", "archive"} or (
            len(members) > 1 and archive and all(m["archive_id"] == archive.id for m in members)
        ):
            for member in members:
                references.update(refs.get(member["id"], ()))
                if member.get("source_auto_item_id"):
                    references.add("auto_parent")
                if member.get("project_id") or member.get("project_line_id"):
                    references.add("project")
            if unknown_ref:
                references.add("unknown_fk")
        if (kind == "archive" or members) and live.get("idle") is True:
            stamp = (
                pid,
                archive.recovery_identity if archive else key,
                previews.digest(live["identity"]),
                queue_sources.current_epoch(),
            )
            live = {**live, "stable_inactive": _seen_for(_observations, stamp) >= 10}
        result[(pid, kind, key)] = RecoveryFacts(
            pid,
            project(q, _QUEUE_FIELDS),
            project(item, _ITEM_FIELDS),
            project(archive, _ARCHIVE_FIELDS),
            live,
            active,
            tuple(range(acounts.get(pid, 0))),
            users.get(archive.id, 0) if archive else 0,
            tuple(sorted(references)),
            now,
            owner_ended=owner_ended(current_boot, archive),
            unobserved=attempt_unobserved(archive.id) if archive is not None else False,
            runtime=runtime,
            members=members,
            members_complete=counts.get(q.id, 0) == len(members) if q else not members,
            foreign_active_users=max(
                0, active_users.get(archive.id, 0) - sum(m["archive_id"] == archive.id for m in members)
            )
            if archive
            else 0,
            repair_runtime=repair_runtime,
            target_kind=kind,
            has_completion_receipt=archive is not None and archive.id in completed_execution_ids,
            gate=gates.get(pid),
        )
    return result, qs, (items, archives)


def owner_ended(current_boot: str | None, archive) -> bool | None:
    """True when the attempt names another boot than the one holding the lock; None proves nothing."""
    boot = getattr(archive, "dispatch_boot_id", None) if archive is not None else None
    if current_boot is None or boot is None:
        return None
    return boot != current_boot


def revision(f: RecoveryFacts) -> str:
    data = asdict(f)
    data.pop("now")
    data["live"].pop("observed_at", None)
    if data.get("runtime"):
        data["runtime"].pop("temperatures", None)
    return previews.digest(data)


def _runtime_view(f: RecoveryFacts) -> dict:
    """What the operator reads beside a runtime row: the held run, the pause, the gate, a residual preheat."""
    r = f.runtime
    current = r.get("current")
    return {
        "context": None
        if current is None
        else {
            "archive_id": current["archive_id"],
            "queue_item_id": current["queue_item_id"],
            "origin": current["origin"],
        },
        "queue_paused": f.queue.get("is_paused") if f.queue else None,
        "plate_gate": r.get("plate_gate"),
        "preheat_pinned": r.get("preheat_pinned"),
        "temperatures": r.get("temperatures"),
    }


def public_issue(key, f):
    issue = classify(f)
    if issue is None:
        return None
    visible_archive = f.archive if f.archive and f.archive.get("printer_id") == f.printer_id else None
    return {
        **issue,
        "printer_id": f.printer_id,
        "queue_id": f.queue["id"] if f.queue else None,
        "kind": key[1],
        "target_id": key[2],
        "item_id": f.item["id"] if f.item else None,
        "archive_id": visible_archive["id"] if visible_archive else None,
        "members": [{"id": m["id"], "status": m["status"]} for m in f.members[:3]],
        "member_count": len(f.members) if f.members_complete else None,
        "claims": [
            {k: m.get(k) for k in ("id", "status", "started_at", "completed_at", "origin")} for m in f.members[:3]
        ],
        "claims_total": len(f.members) if f.members_complete else None,
        "claims_complete": f.members_complete,
        "observed_at": f.live.get("observed_at"),
        "live_mode": f.live["mode"],
        "started_at": (visible_archive or {}).get("started_at"),
        "completed_at": (visible_archive or {}).get("completed_at"),
        **(_runtime_view(f) if f.runtime is not None else {}),
    }


def encode_cursor(row):
    return base64.urlsafe_b64encode(previews.canonical(list(row))).decode()


def decode_cursor(value):
    try:
        if len(value) > 160:
            raise ValueError
        row = json.loads(base64.urlsafe_b64decode(value))
        if (
            not isinstance(row, list)
            or len(row) != 3
            or type(row[0]) is not int
            or row[1] not in {"header", "item", "archive", "repair", "runtime", "episode"}
            or type(row[2]) is not int
        ):
            raise ValueError
        return row
    except (ValueError, TypeError):
        raise ValueError("invalid_cursor") from None


async def list_issues(db, scope, *, printer_id=None, cursor=None, target=None):
    result, _ = await list_issues_page(db, scope, printer_id=printer_id, cursor=cursor, target=target)
    return result


def _episode_rows(pids, after) -> dict[tuple, object]:
    """One row per stalled job of this page (its latest stalled episode), keyed like every other row."""
    from backend.app.services import dispatch_stall_tracker

    rows = {}
    for pid in pids:
        for episode in dispatch_stall_tracker.snapshot(pid):
            if not (episode.stalled or episode.reached):
                continue
            row = (pid, "episode", episode.item_id or 0)
            if (after is None or row > tuple(after)) and (row not in rows or rows[row].last_seen < episode.last_seen):
                rows[row] = episode
    return rows


def _episode_issue(key, episode, item) -> dict:
    """R3 ``dispatch_no_progress``: the job, the attempts, the blocker -- and the row that explains it.

    One that went quiet (a repair, a pause, nothing tried since) is inactive, not resolved (R4): it stays, as an
    observation Health does not count, until progress or the job's end.
    """
    return {
        "code": "dispatch_no_progress",
        "actions": [],
        "blocked_reasons": [episode.detail or "unknown"],
        "printer_id": key[0],
        "queue_id": item.queue_id,
        "kind": "episode",
        "target_id": key[2],
        "item_id": episode.item_id,
        "archive_id": episode.blocker_archive_id,
        "observed_at": None,
        "live_mode": "unknown",
        "started_at": None,
        "completed_at": None,
        "attempts": episode.count,
        "span_seconds": int(episode.span),
        "active": episode.stalled,
        "quiet_seconds": int(episode.quiet),
        "observation": not episode.stalled,
        # Linked by the page once it knows the runtime row is shown (``_link_episodes``).
        "related": {"kind": "runtime", "target_id": episode.blocker_archive_id or 0},
    }


async def _last_repairs(db, printer_ids: list[int]) -> dict[int, dict]:
    """Each printer's latest in-memory repair and how it ended: why its queue may be paused (R8.6, A26).

    Two bounded reads for the page: each printer's own latest intent -- ranked per printer, so another printer's
    longer history can never crowd it out -- then their outcomes by their own ids.
    """
    if not printer_ids:
        return {}
    ranked = (
        select(
            PrintStateRecoveryAction.id.label("id"),
            func.row_number()
            .over(
                partition_by=PrintStateRecoveryAction.printer_id,
                order_by=(PrintStateRecoveryAction.created_at.desc(), PrintStateRecoveryAction.id.desc()),
            )
            .label("rank"),
        )
        .where(
            PrintStateRecoveryAction.printer_id.in_(printer_ids),
            PrintStateRecoveryAction.action == RUNTIME_ACTION,
            PrintStateRecoveryAction.result["phase"].as_string() == "intent",
        )
        .subquery()
    )
    rows = (
        await db.scalars(
            select(PrintStateRecoveryAction).where(
                PrintStateRecoveryAction.id.in_(select(ranked.c.id).where(ranked.c.rank == 1))
            )
        )
    ).all()
    latest = {row.printer_id: row for row in rows if isinstance(row.result, dict)}
    outcomes = {
        row.id: row
        for row in (
            await db.scalars(
                select(PrintStateRecoveryAction).where(
                    PrintStateRecoveryAction.id.in_([outcome_id(row.id) for row in latest.values()])
                )
            )
        ).all()
    }
    answer = {}
    for pid, intent in latest.items():
        outcome = outcomes.get(outcome_id(intent.id))
        result = outcome.result if outcome is not None and isinstance(outcome.result, dict) else {}
        answer[pid] = {
            "at": intent.created_at.isoformat() if intent.created_at else None,
            "archive_id": intent.archive_id,
            "queue_was_paused": bool((intent.result.get("before") or {}).get("queue_was_paused")),
            "outcome": result.get("outcome"),
            "reason": result.get("reason"),
            "process_ended": outcome is None and intent.result.get("incarnation") != previews.incarnation(),
        }
    return answer


def _repair_issue(key, repair: dict) -> dict:
    """RS-R3: the queue is still paused by an in-memory repair whose context is gone -- an observation, not a
    problem, until the operator resumes the queue."""
    return {
        "code": "queue_paused_by_repair",
        "actions": [],
        "blocked_reasons": [],
        "observation": True,
        "printer_id": key[0],
        "queue_id": None,
        "kind": "repair",
        "target_id": key[2],
        "item_id": None,
        "archive_id": repair["archive_id"],
        "observed_at": None,
        "live_mode": "unknown",
        "started_at": None,
        "completed_at": None,
        "queue_paused": True,
        "last_repair": repair,
    }


def _link_episodes(issues: list[dict]) -> None:
    """An episode points at the context that holds its printer only when that row is on this page."""
    shown = {(i["printer_id"], i["target_id"]) for i in issues if i["kind"] == "runtime"}
    for issue in issues:
        if issue["kind"] == "episode" and (issue["printer_id"], issue["related"]["target_id"]) not in shown:
            issue["related"] = None


async def list_issues_page(
    db, scope, *, printer_id=None, cursor=None, target=None, page_size=100, ceilings=None, capture=None
):
    """One page of the farm's print-state rows, and the printers it covered (the observer's cycle needs them)."""
    after = decode_cursor(cursor) if cursor else None
    stmt = select(Printer.id, Printer.name).order_by(Printer.id)
    if scope is not None:
        stmt = stmt.where(Printer.id.in_(scope))
    if printer_id is not None:
        stmt = stmt.where(Printer.id == printer_id)
    if ceilings is not None:
        stmt = stmt.where(Printer.id <= ceilings["printer"])
    if after:
        stmt = stmt.where(Printer.id >= after[0])
    printers = (await db.execute(stmt.limit(21))).all()
    page = printers[:20]
    pids = [p.id for p in page]
    names = {p.id: p.name for p in page}
    headers = select(
        PrinterQueue.printer_id.label("pid"), literal("header").label("kind"), PrinterQueue.id.label("target")
    ).where(
        PrinterQueue.printer_id.in_(pids),
        or_(
            PrinterQueue.status == "printing",
            PrinterQueue.id.in_(
                select(PrintQueueItem.queue_id)
                .where(
                    PrintQueueItem.status == "printing",
                    PrintQueueItem.queue_id.in_(select(PrinterQueue.id).where(PrinterQueue.printer_id.in_(pids))),
                )
                .group_by(PrintQueueItem.queue_id)
                .having(func.count() >= 1)
            ),
        ),
    )
    items = (
        select(PrinterQueue.printer_id, literal("item"), PrintQueueItem.id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .where(
            PrinterQueue.printer_id.in_(pids),
            or_(
                PrintQueueItem.status == "printing",
                and_(PrinterQueue.status == "printing", PrintQueueItem.id == PrinterQueue.current_item_id),
            ),
        )
    )
    archives = select(PrintArchive.printer_id, literal("archive"), PrintArchive.id).where(
        PrintArchive.printer_id.in_(pids),
        PrintArchive.status == "printing",
        PrintArchive.deleted_at.is_(None),
    )
    # Start with queue references, not the printer's archive history. SQLite
    # may scan queue rows; no archive-history JSON predicate is evaluated here.
    # Keep this separate from the printing branch so the JSON predicate is
    # evaluated only on addressed terminal archives.
    owed_archives = select(PrintArchive.printer_id, literal("archive"), PrintArchive.id).where(
        PrintArchive.id.in_(
            select(PrintQueueItem.archive_id)
            .join(PrinterQueue, PrintQueueItem.queue_id == PrinterQueue.id)
            .where(
                PrinterQueue.printer_id.in_(pids),
                or_(PrintQueueItem.status == "printing", PrintQueueItem.id == PrinterQueue.current_item_id),
            )
        ),
        PrintArchive.printer_id.in_(pids),
        PrintArchive.status == "completed",
        PrintArchive.extra_data["bamdude_terminal_effects"]["swap_change_table"]["stage"].as_string() == "owed",
        PrintArchive.deleted_at.is_(None),
    )
    # A queue an in-memory repair paused stays explained while it is paused, whether or not the run is still held
    # (RS-R3): its last release attempt is read from the audit. A queue never touched by one has no such row.
    paused_by_repair = select(PrinterQueue.printer_id, literal("repair"), literal(0)).where(
        PrinterQueue.printer_id.in_(pids),
        PrinterQueue.is_paused.is_(True),
        select(PrintStateRecoveryAction.id)
        .where(
            PrintStateRecoveryAction.printer_id == PrinterQueue.printer_id,
            PrintStateRecoveryAction.action == RUNTIME_ACTION,
        )
        .exists(),
    )
    # A pending row its printer's armed plate gate still waits on: an earlier Retry put it back in the queue and
    # every plate answer refuses (spec plate-gate-owner-requeue B1). A printing header's current row is already an
    # ``items`` candidate, so it is not listed twice.
    from backend.app.services.plate_hold import plate_gate_owner_predicate

    requeued_gate_owners = (
        select(PrinterQueue.printer_id, literal("item"), PrintQueueItem.id)
        .join(PrintQueueItem, PrintQueueItem.queue_id == PrinterQueue.id)
        .join(Printer, Printer.id == PrinterQueue.printer_id)
        .where(
            PrinterQueue.printer_id.in_(pids),
            PrintQueueItem.status == "pending",
            plate_gate_owner_predicate(),
            or_(
                PrinterQueue.status != "printing",
                PrinterQueue.current_item_id.is_(None),
                PrinterQueue.current_item_id != PrintQueueItem.id,
            ),
        )
    )
    candidates = union_all(headers, items, archives, owed_archives, paused_by_repair, requeued_gate_owners).subquery()
    query = select(candidates).order_by(candidates.c.pid, candidates.c.kind, candidates.c.target)
    if target is not None:
        query = query.where(candidates.c.kind == target[0], candidates.c.target == target[1])
    if ceilings is not None:
        query = query.where(
            or_(
                and_(candidates.c.kind == "header", candidates.c.target <= ceilings["header"]),
                and_(candidates.c.kind == "item", candidates.c.target <= ceilings["item"]),
                and_(candidates.c.kind == "archive", candidates.c.target <= ceilings["archive"]),
                candidates.c.kind == "repair",
            )
        )
    if after:
        pid, kind, last_target = after
        query = query.where(
            or_(
                candidates.c.pid > pid,
                and_(candidates.c.pid == pid, candidates.c.kind > kind),
                and_(candidates.c.pid == pid, candidates.c.kind == kind, candidates.c.target > last_target),
            )
        )
    sql_rows = [tuple(row) for row in (await db.execute(query.limit(page_size + 1))).all()]
    # In-process contexts are candidates too (spec r3 R2): one row per printer of this page, ordered
    # with the database rows by the same key, so the cursor walks both exactly once.
    from backend.app.services.print_run_binding import current_print_run, has_runtime_context
    from backend.app.services.printer_manager import printer_manager

    held = []
    for pid in pids:
        if has_runtime_context(printer_manager, pid):
            run = current_print_run(printer_manager, pid)
            row = (pid, "runtime", run.archive_id if run is not None else 0)
            if after is None or row > tuple(after):
                held.append(row)
    episodes = _episode_rows(pids, after)
    if target is not None:
        held = [row for row in held if row[1:] == target]
        episodes = {key: value for key, value in episodes.items() if key[1:] == target}
    if ceilings is not None:
        held = [row for row in held if row[2] <= ceilings["archive"]]
        episodes = {key: value for key, value in episodes.items() if key[2] <= ceilings["item"]}
    held_pids = {pid for pid in pids if has_runtime_context(printer_manager, pid)}
    # The context row explains its own pause; a separate one only once the context is gone.
    sql_rows = [row for row in sql_rows if row[1] != "repair" or row[0] not in held_pids]
    rows = sorted(set(sql_rows) | set(held) | set(episodes))
    targets = rows[:page_size]
    fact_targets = [t for t in targets if t[1] not in ("episode", "repair")]
    facts, _, _ = await read_facts(db, sorted({t[0] for t in fact_targets}), fact_targets)
    if capture is not None:
        capture.update(facts)
    repairs = await _last_repairs(db, sorted({t[0] for t in targets if t[1] in ("runtime", "repair")}))
    # A stalled job is shown only while it still waits: one addressed read of the jobs this page names.
    episode_items = [episodes[t].item_id for t in targets if t[1] == "episode" and episodes[t].item_id]
    waiting = (
        {
            row.id: row
            for row in (
                await db.execute(
                    select(PrintQueueItem.id, PrintQueueItem.queue_id, PrintQueueItem.status).where(
                        PrintQueueItem.id.in_(episode_items)
                    )
                )
            ).all()
            if row.status in ("pending", "printing")
        }
        if episode_items
        else {}
    )
    issues = []
    for key in targets:
        if key[1] == "episode":
            item = waiting.get(episodes[key].item_id)
            if item is not None:
                issue = _episode_issue(key, episodes[key], item)
                issues.append({**issue, "printer_name": names[key[0]]})
            continue
        if key[1] == "repair":
            if repairs.get(key[0]) is not None:
                issues.append({**_repair_issue(key, repairs[key[0]]), "printer_name": names[key[0]]})
            continue
        f = facts.get(key)
        if f is None:
            continue
        issue = public_issue(key, f)
        if issue and f.runtime is not None:
            # A runtime divergence is shown once seen unchanged twice, 10 s apart: a run between its
            # terminal and its finishing lease is not a problem. Display only; apply re-reads everything.
            age = _runtime_age(key, f, issue)
            if age < (LINGERING_SECONDS if issue.get("observation") else _RUNTIME_STABLE_SECONDS):
                continue
            issue = {**issue, "age_seconds": round(age)}
        if issue:
            issues.append({**issue, "printer_name": names[key[0]]})
    _link_episodes(issues)
    for issue in issues:
        if issue["kind"] == "runtime":
            issue["last_repair"] = repairs.get(issue["printer_id"])
    next_cursor = (
        encode_cursor(rows[page_size - 1])
        if len(rows) > page_size
        else (encode_cursor((printers[20].id, "archive", -1)) if len(printers) > 20 else None)
    )
    from backend.app.services import dispatch_stall_tracker

    return {
        "issues": issues,
        "next_cursor": next_cursor,
        "truncated": next_cursor is not None,
        "checked_at": datetime.now(timezone.utc),
        # What the stall records can vouch for: tracked since this process started, minus what was dropped.
        "stall_tracking": dispatch_stall_tracker.coverage_view(),
        # A page cut at its row limit has read nobody past the cut: those printers come with the next page.
    }, ([pid for pid in pids if pid <= rows[page_size - 1][0]] if len(rows) > page_size else pids)


def explain_facts(key, facts, action=None):
    """Information only from one policy snapshot; no confirmation, pin or token.

    A deletion-only dependency is context, not a veto on an allowed alignment.
    The operator's completion assertion is never supplied by a diagnostic reader.
    """
    issue = public_issue(key, facts)
    actions = issue["actions"] if issue else []
    blockers, after = [], None
    needs_completion = action == "fill_estimated_end_and_align" and action in actions
    if action is not None:
        if action not in actions:
            blockers = list((issue or {}).get("blocked_reasons", [])) or ["action_not_available"]
            if action not in {"cancel_terminal_claims", "settle_swap_by_operator"} and facts.target_kind == "item":
                from backend.app.services.print_state_recovery import _classify_single

                blockers = sorted(set(blockers) | set((_classify_single(facts) or {}).get("blocked_reasons", [])))
        elif needs_completion or action in {CLOSE_PRINT_ACTION, "cancel_terminal_claims", "settle_swap_by_operator"}:
            blockers = ["requires_operator_confirmation"]
        else:
            after = public_plan(plan_action(facts, action))
    archive = facts.archive if facts.archive and facts.archive.get("printer_id") == facts.printer_id else None
    extra = (archive or {}).get("extra_data")
    estimated = isinstance(extra, dict) and "operator_recovery_end" in extra
    return {
        "issue": issue,
        "requested_action": action,
        "policy_actions": actions,
        "blockers": blockers,
        "before": {
            "queue": {k: v for k, v in (facts.queue or {}).items() if k in {"status", "is_paused"}},
            "item": {k: v for k, v in (facts.item or {}).items() if k in {"id", "status", "completed_at"}},
            "archive": {k: v for k, v in (archive or {}).items() if k in {"id", "status", "completed_at"}},
        },
        "after": after,
        "time_provenance": {
            "completed_at_source": "slicer_estimate"
            if estimated
            else "recorded"
            if (archive or {}).get("completed_at")
            else "unknown",
            "requested_estimate_basis": "archive_start_plus_slicer_duration" if needs_completion else None,
            "completion_verified_by_mcp": False,
        },
        "archive_preserved": True,
        "requires_operator_confirmation": action is not None,
        "requires_completed_confirmation": needs_completion,
        "requires_idle_confirmation": action in {"cancel_terminal_claims", "settle_swap_by_operator"},
        "requires_manual_swap_confirmation": action == "settle_swap_by_operator",
        "can_execute_via_mcp": False,
    }


#: ``nonce -> (expires, the objects the preview named)``. A runtime revision names objects by ``id()``; holding
#: them for the preview's lifetime keeps Python from freeing one and handing its id to a newcomer with the same
#: content, which the compare-and-clear would then take for the object the operator saw (R7a, A24).
_preview_pins: dict[str, tuple[float, object]] = {}
_MAX_PREVIEW_PINS = 256


def _pin_runtime_objects(nonce: str, f: RecoveryFacts) -> None:
    from backend.app.services import runtime_context_release
    from backend.app.services.printer_manager import printer_manager

    now = previews.clock()
    for old, (expires, _objects) in list(_preview_pins.items()):
        if now > expires:
            _preview_pins.pop(old, None)
    while len(_preview_pins) >= _MAX_PREVIEW_PINS:
        _preview_pins.pop(next(iter(_preview_pins)))
    expected = runtime_context_release.capture(printer_manager, f.printer_id)
    r = f.runtime if f.runtime is not None else f.repair_runtime
    admission = r.get("admission")
    named = (
        (r.get("current") or {}).get("ref"),
        admission.get("ref") if isinstance(admission, dict) else None,
        (r.get("macro_selection") or {}).get("ref"),
        (r.get("swap_config") or {}).get("ref"),
    )
    held = (
        None
        if expected is None
        else (
            id(expected.run.current),
            None if expected.run.admission is None else id(expected.run.admission),
            *(id(value) if present else None for present, value, _content in expected.config),
        )
    )
    if held != named:
        raise ValueError("recovery_changed")  # it moved while the preview was read: no preview of a moving target
    _preview_pins[nonce] = (now + previews.TTL, expected)


def _unpin(nonce: str) -> None:
    _preview_pins.pop(nonce, None)


async def make_preview(
    db,
    printer_id,
    kind,
    target_id,
    action,
    actor,
    *,
    confirmed_completed=False,
    confirmed_idle=False,
    confirmed_manual_swap=False,
    operator_outcome=None,
    note="",
):
    key = (printer_id, kind, target_id)
    facts, _, _ = await read_facts(db, [printer_id], [key])
    f = facts.get(key)
    if f is None:
        raise LookupError
    plan = plan_action(
        f,
        action,
        confirmed_completed=confirmed_completed,
        confirmed_idle=confirmed_idle,
        confirmed_manual_swap=confirmed_manual_swap,
        operator_outcome=operator_outcome,
    )
    nonce = str(uuid4())
    body = {
        "version": 1,
        "nonce": nonce,
        "actor": actor,
        "printer_id": printer_id,
        "kind": kind,
        "target_id": target_id,
        "action": action,
        "confirmed_completed": confirmed_completed,
        "confirmed_idle": confirmed_idle,
        "confirmed_manual_swap": confirmed_manual_swap,
        "note": note,
        "revision": revision(f),
        "epoch": queue_sources.current_epoch(),
        "plan_hash": previews.digest(plan),
    }
    if operator_outcome is not None:
        body["operator_outcome"] = operator_outcome
    if action == RUNTIME_ACTION or (action == CLOSE_PRINT_ACTION and "runtime" in plan):
        _pin_runtime_objects(nonce, f)
    return {
        "preview_token": previews.sign(body),
        "action": action,
        "before": {
            "queue": f.queue,
            "item": {
                **{k: v for k, v in (f.item or {}).items() if k in {"id", "status", "completed_at"}},
                "waiting_reason": True if (f.item or {}).get("waiting_reason") else None,
            },
            "archive": {k: v for k, v in (f.archive or {}).items() if k in {"id", "status", "completed_at"}},
            "claims": [
                {k: m.get(k) for k in ("id", "status", "archive_id", "started_at", "completed_at", "origin")}
                for m in f.members
            ],
        },
        "after": {**public_plan(plan), "claims": public_plan(plan).get("members", [])},
        "estimate": {
            "started_at": f.archive["started_at"],
            "duration_seconds": f.archive["print_time_seconds"],
            "completed_at": plan["archive"]["completed_at"],
        }
        if plan["archive"] and not plan["archive"].get("close")
        else None,
        "members": [{"id": m["id"], "status": m["status"], "archive_id": m["archive_id"]} for m in f.members],
        "swap_mode_enabled": bool((await db.get(Printer, printer_id)).swap_mode_enabled)
        if action == "settle_swap_by_operator"
        else None,
        "archive_preserved": True,
        "expires_in": previews.TTL,
        # The pause, a pause that was there already, the gate and a residual preheat: what the operator
        # must know before an in-memory repair (R7b, R9). Absent for the database repairs.
        **({"runtime": _runtime_view(f)} if f.runtime is not None else {}),
    }


def public_plan(plan):
    """Only the changed time/provenance may leave the server, never historical extra_data."""
    if not plan["archive"]:
        return plan
    archive = plan["archive"]
    if archive.get("close"):
        return {
            **plan,
            "item": {"status": plan["item"]["status"], "error_code": plan["item"]["error_code"]},
            "archive": {"status": archive["status"], "failure_reason": archive["failure_reason"]},
        }
    return {**plan, "archive": {"completed_at": archive["completed_at"], "time_source": "slicer_estimate"}}


async def apply_preview(session_factory, payload, actor, actor_label, *, recheck=None):
    if payload.get("actor") != actor or payload.get("epoch") != queue_sources.current_epoch():
        raise ValueError("recovery_changed")
    action = payload.get("action")
    if action in {RUNTIME_ACTION, CLOSE_PRINT_ACTION}:
        # Shielded: a client that goes away after the first commit does not leave the protocol half done --
        # it runs to its outcome, which history then shows (R8.8).
        apply = _apply_runtime if action == RUNTIME_ACTION else _apply_rows
        operation = asyncio.ensure_future(apply(session_factory, payload, actor, actor_label, recheck))
        _runtime_operations.add(operation)
        operation.add_done_callback(_runtime_operations.discard)
        operation.add_done_callback(lambda _done: _unpin(payload.get("nonce")))
        operation.add_done_callback(_retrieve)
        return await asyncio.shield(operation)
    return await _apply_rows(session_factory, payload, actor, actor_label, recheck)


async def _apply_rows(session_factory, payload, actor, actor_label, recheck):
    key = (payload["printer_id"], payload["kind"], payload["target_id"])
    nonce = payload["nonce"]
    input_hash = previews.digest(payload)
    async with queue_sources.storage_mutation():
        async with session_factory() as preflight:
            if recheck:
                await recheck(payload["action"], preflight)
            prior = await preflight.get(PrintStateRecoveryAction, nonce)
            if prior:
                if prior.actor != actor or prior.input_hash != input_hash:
                    raise ValueError("recovery_changed")
                return prior.result
            facts, _, _ = await read_facts(preflight, [key[0]], [key])
            f = facts.get(key)
            if f is None:
                raise ValueError("recovery_changed")
            queue_id = f.queue["id"]
            archive_id = f.archive["id"] if f.archive else None
        async with session_factory() as db, AsyncExitStack() as guards:
            await guards.enter_async_context(claim_lanes(db, [queue_id], archive_id=archive_id))
            if archive_id is not None:
                await load_active_archive_for_write(db, archive_id)
            await db.scalar(select(PrinterQueue).where(PrinterQueue.id == queue_id).with_for_update())
            prior = await db.get(PrintStateRecoveryAction, nonce, populate_existing=True)
            if prior:
                if recheck:
                    await recheck(payload["action"], db)
                if prior.actor == actor and prior.input_hash == input_hash:
                    return prior.result
                raise ValueError("recovery_changed")
            facts, queues, (items, archives) = await read_facts(db, [key[0]], [key], locked=True)
            f = facts.get(key)
            if f is None or revision(f) != payload["revision"] or queue_sources.current_epoch() != payload["epoch"]:
                raise ValueError("recovery_changed")
            plan = plan_action(
                f,
                payload["action"],
                confirmed_completed=payload["confirmed_completed"],
                confirmed_idle=payload.get("confirmed_idle", False),
                confirmed_manual_swap=payload.get("confirmed_manual_swap", False),
                operator_outcome=payload.get("operator_outcome"),
            )
            if previews.digest(plan) != payload["plan_hash"]:
                raise ValueError("recovery_changed")
            if recheck:
                await recheck(payload["action"], db)
            if previews.clock() > payload["expires"]:
                raise ValueError("recovery_preview_expired")
            close_print = payload["action"] == CLOSE_PRINT_ACTION
            release_context = close_print and "runtime" in plan
            pin = _preview_pins.get(nonce) if release_context else None
            if release_context and (pin is None or previews.clock() > pin[0]):
                raise ValueError("recovery_changed")
            # Explicit allowlists, no setattr loop over client fields.
            item = items.get(f.item["id"]) if f.item else None
            archive = archives.get(archive_id)
            ended_at = datetime.now(timezone.utc)
            if "members" in plan:
                for member in plan["members"]:
                    row = items[member["id"]]
                    row.status, row.completed_at, row.waiting_reason = "cancelled", ended_at, None
                    row.error_code, row.error_message = "operator_cancelled_stale_claim", None
                from backend.app.services.queue_counters import update_queue_counters

                await db.flush()
                await update_queue_counters(db, queue_id)
            if "settlement" in plan:
                from backend.app.services.swap_terminal import settle_by_operator

                settle_by_operator(archive, action_identity=nonce, actor=actor, at=ended_at)
                queues[key[0]].is_paused = True
            if plan["archive"].get("close"):
                # Close the addressed job and archive without running any completion effects.
                ended_at = datetime.now(timezone.utc)
                archive.status = plan["archive"]["status"]
                archive.failure_reason = plan["archive"]["failure_reason"]
                archive.error_message = plan["archive"]["error_message"]
                archive.completed_at = ended_at
                item.status = plan["item"]["status"]
                item.error_code = plan["item"]["error_code"]
                item.error_message = plan["item"]["error_message"]
                item.completed_at = ended_at
                item.waiting_reason = None
                q = queues[key[0]]
                q.current_item_id = None
                q.status = plan["queue"]["status"]
                q.last_activity_at = ended_at
                if close_print:
                    q.is_paused = True
                    extra = plan["archive"]["extra_data"]
                    extra["operator_interrupted_print"]["action_id"] = nonce
                    archive.extra_data = extra
            elif plan["archive"]:
                archive.completed_at = plan["archive"]["completed_at"]
                extra = plan["archive"]["extra_data"]
                extra["operator_recovery_end"]["action_id"] = nonce
                archive.extra_data = json.loads(previews.canonical(extra))
            after = public_plan(plan)
            if plan["item"] and not plan["item"].get("close") and payload["action"] == GATE_OWNER_ACTION:
                # B4: the archive's own end, else this apply's one clock -- the row and the audit carry the same
                # moment. The waiting and error fields stay empty, as the preview showed; the archive is untouched.
                completed = ended_at if plan["item"]["completed_at"] == "apply" else plan["item"]["completed_at"]
                item.status = plan["item"]["status"]
                item.completed_at = completed
                item.waiting_reason = None
                item.error_code, item.error_message = None, None
                after = {**after, "item": {**plan["item"], "completed_at": completed}}
                # A pending row became finished again: the queue's counts follow it.
                from backend.app.services.queue_counters import update_queue_counters

                await db.flush()
                await update_queue_counters(db, queue_id)
            elif plan["item"] and not plan["item"].get("close"):
                item.status = plan["item"]["status"]
                item.completed_at = plan["item"]["completed_at"]
                item.waiting_reason = None
                item.error_code = row_class(item.status, archive.failure_reason if archive is not None else None)
            if plan["delete_item"]:
                await db.delete(item)
            if plan["queue"] and not plan["archive"].get("close") and "settlement" not in plan:
                q = queues[key[0]]
                q.current_item_id = None
                q.status = plan["queue"]["status"]
                q.is_paused = True
            result = json.loads(
                previews.canonical(
                    {
                        "id": nonce,
                        "action": payload["action"],
                        "printer_id": key[0],
                        "queue_id": queue_id,
                        "item_id": f.item["id"] if f.item else None,
                        "archive_id": archive_id,
                        "identities": {
                            "queue": f.queue["recovery_identity"],
                            "item": (f.item or {}).get("recovery_identity"),
                            "archive": (f.archive or {}).get("recovery_identity"),
                        },
                        "before": {
                            "item_status": (f.item or {}).get("status"),
                            "completed_at": (f.archive or {}).get("completed_at"),
                            "queue": f.queue,
                        },
                        "after": {k: v for k, v in after.items() if k != "runtime"},
                        "note": payload["note"],
                        "members": [
                            {
                                "id": m["id"],
                                "identity": m["recovery_identity"],
                                "archive_id": m["archive_id"],
                                "before_status": m["status"],
                                "after_status": item.status
                                if close_print
                                else "cancelled"
                                if "members" in plan
                                else m["status"],
                            }
                            for m in f.members
                        ],
                        "applied_at": ended_at,
                        "confirmed_idle": payload.get("confirmed_idle", False),
                        "confirmed_manual_swap": payload.get("confirmed_manual_swap", False),
                        **({"operator_outcome": payload["operator_outcome"]} if close_print else {}),
                        "reason_codes": classify(f)["blocked_reasons"],
                        "archive_preserved": True,
                        "estimate": {
                            "started_at": (f.archive or {}).get("started_at"),
                            "duration": (f.archive or {}).get("print_time_seconds"),
                        }
                        if plan["archive"] and not plan["archive"].get("close")
                        else None,
                    }
                )
            )
            db.add(
                PrintStateRecoveryAction(
                    id=nonce,
                    actor=actor,
                    actor_label=actor_label,
                    printer_id=key[0],
                    queue_id=queue_id,
                    item_id=f.item["id"] if f.item else None,
                    archive_id=archive_id,
                    action=payload["action"],
                    input_hash=input_hash,
                    result=result,
                )
            )
            try:
                await db.flush()
                # Observe again after DB awaits, including waits on a contended FK.
                latest = live_snapshot(key[0])
                if (
                    previews.digest(latest["identity"]) != previews.digest(f.live["identity"])
                    or latest["mode"] != f.live["mode"]
                    or queue_sources.current_epoch() != payload["epoch"]
                ):
                    raise ValueError("recovery_changed")
                if f.repair_runtime is not None:
                    latest_runtime = repair_runtime_snapshot(key[0], archive_id)
                    if previews.digest(latest_runtime) != previews.digest(f.repair_runtime):
                        raise ValueError("recovery_changed")
                if close_print and (not latest["idle"] or latest["observation_unproven"]):
                    raise ValueError("recovery_changed")
                await db.commit()
                if release_context:
                    _release_closed_print_context(key[0], archive_id, f, pin[1], nonce)
            except IntegrityError:
                await db.rollback()
                async with session_factory() as retry:
                    prior = await retry.get(PrintStateRecoveryAction, nonce)
                    if prior and prior.actor == actor and prior.input_hash == input_hash:
                        return prior.result
                raise ValueError("recovery_changed") from None
    logger.info("operator print-state recovery action=%s printer=%s target=%s outcome=applied", nonce, key[0], key[2])
    from backend.app.core.websocket import ws_manager

    try:
        await ws_manager.broadcast({"type": "queue_changed", "printer_id": key[0]})
        if plan["archive"]:
            await ws_manager.send_archive_updated({"id": archive_id})
    except Exception:
        logger.warning("operator recovery committed; event delivery failed action=%s", nonce)
    return result


def _release_closed_print_context(printer_id, archive_id, f, expected, nonce):
    """After the close commits, clear only the context the operator previewed, synchronously."""
    from backend.app.services import runtime_context_release
    from backend.app.services.printer_manager import printer_manager

    latest, runtime = live_snapshot(printer_id), repair_runtime_snapshot(printer_id, archive_id)
    if (
        latest["idle"]
        and not latest["observation_unproven"]
        and previews.digest(latest["identity"]) == previews.digest(f.live["identity"])
        and previews.digest(runtime) == previews.digest(f.repair_runtime)
        and runtime_context_release.release(printer_manager, expected)
    ):
        return
    # Leave a changed or successor context alone. A held terminal context is visible in diagnostics.
    logger.info("operator recovery committed; runtime context changed or unavailable action=%s", nonce)


# --- The in-memory repair (spec printer-runtime-stall r3 R6, R8) -------------------------------------------------
#
# Two commits, because a database transaction cannot hold process memory: the intent and the queue's pause
# first, then -- under the same guards taken again -- revalidation, the synchronous compare-and-clear and the
# outcome. Nothing in memory changes before the first commit is confirmed; a refusal after it keeps the pause;
# an outcome that could not be recorded is reported as unconfirmed and never retried. A nonce with an intent
# never clears twice.

_runtime_operations: set[asyncio.Task] = set()


def _retrieve(operation: asyncio.Future) -> None:
    """Read an operation's end even when its client left: a refusal is ordinary, anything else is logged."""
    if operation.cancelled():
        return
    error = operation.exception()
    if error is not None and not isinstance(error, ValueError):
        logger.warning("operator print-state recovery operation failed", exc_info=error)


async def drain_runtime_operations(timeout: float = 10.0) -> int:
    """Shutdown: let every in-memory repair in flight reach its outcome. Returns how many did not in time."""
    pending = [operation for operation in _runtime_operations if not operation.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    left = sum(1 for operation in pending if not operation.done())
    if left:
        logger.warning("operator print-state recovery: %d operation(s) still running at shutdown", left)
    return left


def outcome_id(nonce: str) -> str:
    """The outcome row's own id, derived from the intent's nonce: one per operation, found without a scan."""
    return str(uuid5(NAMESPACE_URL, f"bamdude:print-state-recovery:outcome:{nonce}"))


def _utc_now() -> datetime:
    # Python's UTC, never a server default (local time on a PostgreSQL whose zone is not UTC).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_previewed(f: RecoveryFacts, was_paused: bool) -> RecoveryFacts:
    """The facts with the pause this protocol set taken back out, to compare with what the preview saw."""
    return replace(f, queue={**f.queue, "is_paused": was_paused}) if f.queue else f


def _runtime_response(intent: dict, outcome: dict | None) -> dict:
    return {
        **{k: v for k, v in intent.items() if k not in ("phase", "incarnation", "digest")},
        "operation_id": intent["operation_id"],
        "outcome": outcome["outcome"] if outcome else "outcome_unconfirmed",
        "reason": outcome.get("reason") if outcome else None,
        "queue_paused": True,
        "queue_was_paused": intent["before"]["queue_was_paused"],
    }


async def _runtime_replay(db, prior, actor: str, input_hash: str) -> dict:
    """The known answer for a nonce that already has its intent -- never a second compare-and-clear."""
    if prior.actor != actor or prior.input_hash != input_hash:
        raise ValueError("recovery_changed")
    outcome = await db.get(PrintStateRecoveryAction, outcome_id(prior.id), populate_existing=True)
    return _runtime_response(prior.result, outcome.result if outcome else None)


def _final_release(key, f: RecoveryFacts, payload: dict, was_paused: bool) -> bool:
    """The last check and the compare-and-clear, with no await between them (R8.4).

    The runtime facts and the telemetry are read again here, synchronously; the revision then proves they are
    the very objects the preview saw, and the coordinator removes exactly those -- or nothing.
    """
    from backend.app.services import runtime_context_release
    from backend.app.services.printer_manager import printer_manager

    now = replace(f, runtime=runtime_snapshot(key[0]), live=live_snapshot(key[0]))
    issue = classify(now)
    if (
        issue is None
        or RUNTIME_ACTION not in issue["actions"]
        or revision(_as_previewed(now, was_paused)) != payload["revision"]
    ):
        return False
    expected = runtime_context_release.capture(printer_manager, key[0])
    current = now.runtime["current"]
    if expected is None or current is None or id(expected.run.current) != current["ref"]:
        return False
    return runtime_context_release.release(printer_manager, expected)


def _outcome_record(nonce: str, outcome: str, reason: str | None) -> dict:
    return json.loads(
        previews.canonical(
            {
                "id": outcome_id(nonce),
                "phase": "outcome",
                "operation_id": nonce,
                "action": RUNTIME_ACTION,
                "outcome": outcome,
                "reason": reason,
                "at": datetime.now(timezone.utc),
            }
        )
    )


def _audit_row(nonce_or_id, actor, actor_label, key, queue_id, archive_id, input_hash, result):
    return PrintStateRecoveryAction(
        id=nonce_or_id,
        created_at=_utc_now(),
        actor=actor,
        actor_label=actor_label,
        printer_id=key[0],
        queue_id=queue_id,
        item_id=None,
        archive_id=archive_id,
        action=RUNTIME_ACTION,
        input_hash=input_hash,
        result=result,
    )


async def _take_guards(db, guards, queue_id, archive_id):
    """Local archive lock, local lane lock, then the database's: the one order of every archive + lane writer."""
    await guards.enter_async_context(claim_lanes(db, [queue_id], archive_id=archive_id))
    if archive_id is not None:
        await load_active_archive_for_write(db, archive_id)
    await db.scalar(select(PrinterQueue).where(PrinterQueue.id == queue_id).with_for_update())


async def _apply_runtime(session_factory, payload, actor, actor_label, recheck):
    key = (payload["printer_id"], "runtime", payload["target_id"])
    nonce = payload["nonce"]
    input_hash = previews.digest(payload)
    async with queue_sources.storage_mutation():
        async with session_factory() as preflight:
            if recheck:
                await recheck(payload["action"], preflight)
            prior = await preflight.get(PrintStateRecoveryAction, nonce)
            if prior:
                return await _runtime_replay(preflight, prior, actor, input_hash)
            facts, _, _ = await read_facts(preflight, [key[0]], [key])
            f = facts.get(key)
            if f is None or f.queue is None:
                raise ValueError("recovery_changed")
            queue_id = f.queue["id"]
            archive_id = f.archive["id"] if f.archive else None

        # First commit: the intent and the pause. Until it is confirmed, memory is untouched.
        async with session_factory() as db, AsyncExitStack() as guards:
            await _take_guards(db, guards, queue_id, archive_id)
            prior = await db.get(PrintStateRecoveryAction, nonce, populate_existing=True)
            if prior:
                if recheck:
                    await recheck(payload["action"], db)
                return await _runtime_replay(db, prior, actor, input_hash)
            facts, queues, _ = await read_facts(db, [key[0]], [key], locked=True)
            f = facts.get(key)
            if f is None or revision(f) != payload["revision"] or queue_sources.current_epoch() != payload["epoch"]:
                raise ValueError("recovery_changed")
            plan = plan_action(f, payload["action"])
            if previews.digest(plan) != payload["plan_hash"]:
                raise ValueError("recovery_changed")
            if recheck:
                await recheck(payload["action"], db)
            if previews.clock() > payload["expires"]:
                raise ValueError("recovery_preview_expired")
            was_paused = bool(f.queue["is_paused"])
            current = f.runtime["current"]
            intent = json.loads(
                previews.canonical(
                    {
                        "id": nonce,
                        "phase": "intent",
                        "operation_id": nonce,
                        "action": RUNTIME_ACTION,
                        "printer_id": key[0],
                        "queue_id": queue_id,
                        "item_id": None,
                        "archive_id": archive_id,
                        "identities": {
                            "queue": f.queue["recovery_identity"],
                            "archive": (f.archive or {}).get("recovery_identity"),
                        },
                        "before": {
                            "queue": f.queue,
                            "queue_was_paused": was_paused,
                            "runtime": {
                                "archive_id": current["archive_id"],
                                "sequence": current["sequence"],
                                "origin": current["origin"],
                                **plan["runtime"],
                            },
                        },
                        "after": public_plan(plan),
                        "note": payload["note"],
                        "reason_codes": [],
                        "archive_preserved": True,
                        "incarnation": previews.incarnation(),
                        "digest": payload["revision"],
                        "at": datetime.now(timezone.utc),
                    }
                )
            )
            # The flag only: the queue's status says what the printer is doing and is not ours to change.
            queues[key[0]].is_paused = True
            db.add(_audit_row(nonce, actor, actor_label, key, queue_id, archive_id, input_hash, intent))
            await db.commit()
        logger.info("operator print-state recovery action=%s printer=%s phase=intent queue_paused=1", nonce, key[0])

        # Second commit: the guards again (PostgreSQL released them at the first commit), the facts, the rights
        # and the epoch afresh, the compare-and-clear, the outcome.
        outcome, reason, applied, record = "not_applied_changed", None, False, None
        in_cas = False
        try:
            async with session_factory() as db, AsyncExitStack() as guards:
                await _take_guards(db, guards, queue_id, archive_id)
                try:
                    if recheck:
                        await recheck(payload["action"], db)
                except HTTPException:  # a revoked or changed authority
                    reason = "rights_changed"
                if reason is None:
                    facts, _, _ = await read_facts(db, [key[0]], [key], locked=True)
                    f2 = facts.get(key)
                    if queue_sources.current_epoch() != payload["epoch"]:
                        reason = "restored"
                    elif previews.clock() > payload["expires"]:
                        reason = "preview_expired"
                    elif f2 is None or not f2.queue or not f2.queue["is_paused"]:
                        reason = "context_changed"
                    else:
                        # Last, and only when every check above held: the synchronous compare-and-clear.
                        in_cas = True
                        applied = _final_release(key, f2, payload, was_paused)
                        in_cas = False
                        if applied:
                            outcome = "applied"
                        else:
                            reason = "context_changed"
                record = _outcome_record(nonce, outcome, reason)
                db.add(_audit_row(outcome_id(nonce), actor, actor_label, key, queue_id, archive_id, input_hash, record))
                try:
                    await db.commit()
                except Exception:  # noqa: BLE001 - the outcome is unknown to history; what happened is not undone
                    logger.warning(
                        "operator print-state recovery action=%s printer=%s outcome=%s could not be recorded",
                        nonce,
                        key[0],
                        outcome,
                        exc_info=True,
                    )
                    if applied:
                        record = None
        except Exception:  # noqa: BLE001 - every failure of the second transaction is answered, never re-raised
            if in_cas or applied:
                # Memory may have changed and nothing recorded it: the result is unknown, not "not applied".
                logger.warning(
                    "operator print-state recovery action=%s printer=%s failed during the compare-and-clear",
                    nonce,
                    key[0],
                    exc_info=True,
                )
                record = None
            else:
                # Memory was never touched; the pause stays. Say so in history, from a session of its own.
                logger.warning(
                    "operator print-state recovery action=%s printer=%s could not be checked again; nothing released",
                    nonce,
                    key[0],
                    exc_info=True,
                )
                outcome, reason = "not_applied_changed", "unavailable"
                record = _outcome_record(nonce, outcome, reason)
                try:
                    async with session_factory() as db:
                        db.add(
                            _audit_row(
                                outcome_id(nonce), actor, actor_label, key, queue_id, archive_id, input_hash, record
                            )
                        )
                        await db.commit()
                except Exception:  # noqa: BLE001 - not recorded, but known: nothing was released
                    logger.warning(
                        "operator print-state recovery action=%s printer=%s outcome=%s could not be recorded",
                        nonce,
                        key[0],
                        outcome,
                        exc_info=True,
                    )
        logger.info(
            "operator print-state recovery action=%s printer=%s phase=outcome outcome=%s reason=%s",
            nonce,
            key[0],
            outcome if record else "outcome_unconfirmed",
            reason,
        )
    from backend.app.core.websocket import ws_manager

    try:
        await ws_manager.broadcast({"type": "queue_changed", "printer_id": key[0]})
    except Exception:
        logger.warning("operator recovery committed; event delivery failed action=%s", nonce)
    return _runtime_response(intent, record)
