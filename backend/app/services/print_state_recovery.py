"""Pure, conservative plans for addressed operator print-state recovery.

No driver, accounting, queue cleanup or completion callbacks belong here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

OUTCOMES = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "aborted": "cancelled",
    "stopped": "cancelled",
}
#: The one in-memory repair (spec printer-runtime-stall-diagnostics r3 R6): a finished run's context.
RUNTIME_ACTION = "clear_terminal_runtime_context"
ACTIONS = (
    "align_terminal_item",
    "fill_estimated_end_and_align",
    "release_orphan_header",
    "delete_stale_item",
    "close_interrupted_attempt",
    RUNTIME_ACTION,
    "cancel_terminal_claims",
    "settle_swap_by_operator",
    "restore_gate_owner_row",
)
#: The row a plate gate waits on, put back in the queue by an earlier Retry (spec plate-gate-owner-requeue B).
GATE_OWNER_ACTION = "restore_gate_owner_row"
#: Every in-process owner that can still use a run, read by ``print_state_recovery_store.runtime_snapshot``.
#: A missing or ``None`` answer is unknown, never "no owner".
RUNTIME_OWNERS = (
    "dispatch_in_flight",
    "dispatch_work",
    "completion_scope",
    "live_completion",
    "swap_sending",
    "macro_tasks",
    "plug_off",
    "bed_cooldown",
    "archive_download",
)
_IDLE_STATES = ("IDLE", "FINISH", "FAILED")
#: Dispatch phases our dispatch never saw running (``services/dispatch_phase.UNACCEPTED``).
UNACCEPTED_PHASES = ("prepared", "sending", "sent")
_INTERRUPTED = "The dispatch was interrupted"


@dataclass(frozen=True)
class RecoveryFacts:
    printer_id: int
    queue: dict
    item: dict | None
    archive: dict | None
    live: dict
    active_item_ids: tuple[int, ...]
    printing_archive_ids: tuple[int, ...]
    archive_users: int = 0
    references: tuple[str, ...] = ()
    now: datetime | None = None
    #: Whether the attempt's owner process has ended: True only while this process holds the
    #: installation's owner lock and the archive names another boot; False for this boot; None
    #: when nothing proves either (no lock held, no boot recorded) — post-rebuild I-R2.
    owner_ended: bool | None = None
    #: This process's own runner stopped watching the attempt's sent start without the
    #: printer's verdict (``background_dispatch.attempt_unobserved``, D-R4). The live owner
    #: says so itself; it is not a proof that any owner ended.
    unobserved: bool = False
    #: The printer's in-process context as plain data (``print_state_recovery_store.runtime_snapshot``);
    #: set only for the ``runtime`` kind, whose ``item`` is always None.
    runtime: dict | None = None
    members: tuple[dict, ...] = ()
    members_complete: bool = True
    foreign_active_users: int = 0
    repair_runtime: dict | None = None
    target_kind: str | None = None
    #: The printer's plate gate as the database holds it (``armed``, ``archive_id``, ``token``),
    #: read beside the rows so a preview names the gate it was made for.
    gate: dict | None = None


def utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def effects_pending(archive: dict | None, *, intents: bool = True) -> bool:
    """An unfinished effect recorded on the archive — a veto, never permission to erase it.

    ``intents=False`` leaves out the pending swap-macro list: for an attempt that never
    started that list is only what was asked, and the swap start's own fact is read
    through ``swap_terminal.cycle_state`` (post-rebuild I-R3).
    """
    if archive is None:
        return False
    extra = archive.get("extra_data")
    if extra is None:
        return False
    if not isinstance(extra, dict):
        return True
    pending = extra.get("swap_macro_events_pending")
    if intents and pending is not None and (not isinstance(pending, list) or bool(pending)):
        return True
    # This generation does not own newer completion carriers. Until its accessor
    # is integrated, seeing one is a veto, never permission to erase it.
    return any(
        key in extra
        for key in (
            "bamdude_terminal_effects",
            "swap_hold",
            "auto_eject_pending",
            "cleanup_pending",
            "completion_effects",
        )
    )


#: Stages of a completion effect's diagnostic record (``main._record_terminal_effect_stage``) that end it:
#: completion never replays printer I/O, so a failed effect owes nothing either, and finish macros handed to
#: their tasks are watched as a live owner (``macro_trigger.has_event_tasks``) while they run.
_SETTLED_EFFECT_STAGES = frozenset({"finished", "skipped", "failed", "scheduled"})


def runtime_effect_debt(extra, archive_identity=None) -> list[str]:
    """What a finished run's archive still owes, read strictly (spec r3 R7.5, R7a).

    Unlike :func:`effects_pending` it understands the swap claim: ``finished`` is settled, any other stage is
    debt, and a stage it cannot read is unreadable -- never "nothing owed". Another completion effect is
    settled only at a stage that ended it; in flight, uncertain or an unknown stage vetoes.
    """
    if extra is None:
        return []
    if not isinstance(extra, dict):
        return ["effects_unreadable"]
    from backend.app.services import swap_terminal

    settled = swap_terminal.operator_settlement(extra, archive_identity)
    debt = set()
    # Only an absent key or a list proves anything; a present null is a carrier this reader cannot read.
    if "swap_macro_events_pending" in extra:
        pending = extra["swap_macro_events_pending"]
        if not isinstance(pending, list):
            debt.add("effects_unreadable")
        elif pending:
            debt.add("swap_debt")
    if "bamdude_terminal_effects" in extra:
        effects = extra["bamdude_terminal_effects"]
        if not isinstance(effects, dict):
            debt.add("effects_unreadable")
        else:
            for name, record in effects.items():
                stage = record.get("stage") if isinstance(record, dict) else None
                if name != "swap_change_table":
                    if not isinstance(stage, str):
                        debt.add("effects_unreadable")  # no record, no stage, or a stage of another type
                    elif stage not in _SETTLED_EFFECT_STAGES:
                        debt.add("effects_pending")  # in flight, uncertain, or a stage this reader does not know
                    continue
                if stage == "finished" or (stage == swap_terminal.SETTLED_BY_OPERATOR and settled):
                    continue
                debt.add("swap_debt" if stage in ("owed", "attempted", "failed") else "effects_unreadable")
    if swap_terminal.unsettled_hold(extra, archive_identity) or any(
        key in extra for key in ("auto_eject_pending", "cleanup_pending", "completion_effects")
    ):
        debt.add("effects_pending")
    return sorted(debt)


def _nonzero_id(value) -> str | None:
    text = str(value or "").strip()
    return text if text and text != "0" else None


def _admission_reasons(admission, current: dict) -> list[str]:
    """Only a processed start of THIS run may go with it: a copied archive id or generation proves nothing."""
    if admission is None:
        return []
    if not isinstance(admission, dict):
        return ["admission_unproven"]
    if admission.get("phase") == "processing":
        return ["start_in_progress"]
    run_ids = {_nonzero_id(current.get("observed_subtask_id")), _nonzero_id(current.get("expected_submission_id"))}
    run_ids.discard(None)
    started = _nonzero_id(admission.get("subtask_id"))
    if (
        admission.get("phase") != "processed"
        or admission.get("archive_id") != current.get("archive_id")
        or admission.get("generation") != current.get("effect_generation")
        or started is None
        or started not in run_ids
    ):
        return ["admission_unproven"]
    return []


def _registration_reasons(registration) -> list[str]:
    if not isinstance(registration, dict) or not isinstance(registration.get("present"), bool):
        return ["registration_unreadable"]
    if registration["present"] and registration.get("valid") is not True:
        return ["registration_unreadable"]
    return []


def _an_owner_explains(r: dict) -> bool:
    """A live owner positively holds the run -- a dispatch, a completion, a start still being resolved."""
    owners = r.get("owners") if isinstance(r.get("owners"), dict) else {}
    return any(owners.get(name) is True for name in RUNTIME_OWNERS) or r.get("resolution") is True


def _classify_runtime(f: RecoveryFacts) -> dict | None:
    """A printer's in-process context against its database rows (spec r3 R3, R7).

    Shown whenever the two disagree; released only when every precondition holds. The plate gate and a
    residual preheat are facts for the operator, never a veto (R7b, A21).
    """
    r = f.runtime
    current = r.get("current")
    if current is None:
        lingering = set()
        if r.get("resolution") or r.get("admission") is not None:
            lingering.add("start_in_progress")
        if r.get("finishing"):
            lingering.add("finishing_in_progress")
        if r.get("pending_terminal"):
            lingering.add("terminal_buffered")
        if not lingering:
            return None
        # A start or a completion still under way is a phase, not a contradiction: observed with its age once it
        # outlives its own budget (R3), never repaired and never counted as a problem.
        return {
            "code": "runtime_phase_lingering",
            "actions": [],
            "blocked_reasons": sorted(lingering),
            "observation": True,
        }

    q, a = f.queue, f.archive
    telemetry = r.get("telemetry") if isinstance(r.get("telemetry"), dict) else {}
    state = telemetry.get("state")
    fresh_idle = telemetry.get("fresh") is True and state in _IDLE_STATES
    reasons = set()
    code = "terminal_runtime_context"
    if a is None or a.get("deleted_at") is not None:
        code = "runtime_context_unresolved"
        reasons.add("archive_missing")
    elif a.get("printer_id") != f.printer_id or (q is not None and a.get("queue_id") != q["id"]):
        code = "runtime_context_unresolved"
        reasons.add("archive_foreign")
    elif a.get("status") not in OUTCOMES:
        if not fresh_idle or _an_owner_explains(r):
            # A print in progress, a printer we cannot see, or a start its own dispatch still owns (the printer
            # has not left FINISH yet): nothing contradicts the run.
            return None
        code = "runtime_context_unresolved"
        reasons.add("archive_not_terminal")
    if a is not None:
        extra = a.get("extra_data")
        if isinstance(extra, dict) and extra.get("recovered_outcome_uncertain"):
            reasons.add("outcome_unknown")
        if not a.get("recovery_identity"):
            reasons.add("identity_missing")
        reasons.update(runtime_effect_debt(extra, a.get("recovery_identity")))

    if not fresh_idle:
        printing = telemetry.get("fresh") is True and state in ("RUNNING", "PAUSE")
        reasons.add("printer_printing" if printing else "live_unknown")

    owners = r.get("owners") if isinstance(r.get("owners"), dict) else {}
    for name in RUNTIME_OWNERS:
        value = owners.get(name)
        if value is True:
            reasons.add("context_owner_active")
        elif value is not False:
            reasons.add("context_owner_unknown")
    for name, busy_reason in (("resolution", "start_in_progress"), ("pending_terminal", "terminal_buffered")):
        value = r.get(name)
        if value is True:
            reasons.add(busy_reason)
        elif value is not False:
            reasons.add("context_owner_unknown")
    timelapse = r.get("layer_timelapse")
    if timelapse is True:
        reasons.add("timelapse_open")  # it would take the next print's layers: kept until handled on its own
    elif timelapse is not False:
        reasons.add("context_owner_unknown")
    finishing = r.get("finishing")
    if type(finishing) is not int:
        reasons.add("context_owner_unknown")
    elif finishing:
        reasons.add("finishing_in_progress")
    if not isinstance(r.get("terminal_accepted"), bool):
        reasons.add("context_owner_unknown")
    generation = r.get("effect_generation")
    if type(generation) is not int:
        reasons.add("context_owner_unknown")
    elif generation != current.get("effect_generation"):
        reasons.add("activity_changed")
    reasons.update(_admission_reasons(r.get("admission"), current))
    reasons.update(_registration_reasons(r.get("macro_selection")))
    reasons.update(_registration_reasons(r.get("swap_config")))

    if q is None:
        reasons.add("queue_missing")
    else:
        if not q.get("recovery_identity"):
            reasons.add("identity_missing")
        if (
            q.get("status") == "printing"
            or q.get("current_item_id") is not None
            or f.active_item_ids
            or f.printing_archive_ids
        ):
            reasons.add("queue_busy")

    actions = [RUNTIME_ACTION] if code == "terminal_runtime_context" and not reasons else []
    return {"code": code, "actions": actions, "blocked_reasons": sorted(reasons)}


def estimated_end(f: RecoveryFacts):
    a = f.archive or {}
    start = utc(a.get("started_at"))
    duration = a.get("print_time_seconds")
    if start is None or type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0:
        return None
    try:
        result = start + timedelta(seconds=duration)
    except (OverflowError, ValueError):
        return None
    claim = utc((f.item or {}).get("started_at"))
    return result if claim and claim < result <= utc(f.now) else None


def interrupted_close(a: dict) -> tuple[str, str]:
    """The failure code and detail an Info close writes for this attempt (post-rebuild I-R4).

    A ``prepared`` attempt never sent its start; from ``sending`` on the command may have
    reached the printer, and nothing acknowledged it — the detail says so and promises nothing.
    """
    from backend.app.services import swap_terminal
    from backend.app.utils import failure_reasons

    if a.get("dispatch_phase") == "prepared":
        code = failure_reasons.DISPATCH_PREPARATION_ERROR
        detail = f"{_INTERRUPTED} before the start command was sent."
    else:
        code = failure_reasons.DISPATCH_NO_ACKNOWLEDGEMENT
        detail = (
            f"{_INTERRUPTED} after the start command may have been sent; the printer's answer was never "
            "observed. Check the printer before Retry."
        )
    if swap_terminal.cycle_state(a.get("extra_data")) != swap_terminal.CYCLE_NOT_STARTED:
        detail += " The swap start macro ran or may have: check the plate."
    return code, detail


def _interrupted_dispatch(f: RecoveryFacts) -> dict | None:
    """An attempt our dispatch never saw running, on a job still claimed for it (I-R4).

    Only an operator closes it here — never a filename, a timer or a guess. The close needs the
    proof the automatic requeue needs, for every phase: an owner proven ended, a fresh idle
    printer, no other running archive or claim, no other unfinished effect.
    """
    q, i, a = f.queue, f.item, f.archive
    if not (
        i
        and a
        and i.get("status") == "printing"
        and a.get("status") == "printing"
        and a.get("dispatch_phase") in UNACCEPTED_PHASES
        and a.get("printer_id") == f.printer_id
        and a.get("queue_id") == q["id"]
    ):
        return None
    if f.owner_ended is False and f.live.get("busy"):
        return None  # this process's dispatch, still in flight: its runner ends it
    reasons = []
    # This process's runner gave up watching the sent start (D-R4): no restart is needed for
    # the operator to end it, on a fresh idle printer whose only bound run is its own.
    given_up = f.owner_ended is False and f.unobserved
    if f.owner_ended is not True and not given_up:
        reasons.append("owner_alive" if f.owner_ended is False else "owner_unknown")
    own_run_idle = given_up and f.live.get("idle") is True and f.live.get("archive_id") in (None, a.get("id"))
    if f.live.get("mode") != "inactive" and not own_run_idle:
        reasons.append("live_unknown")
    if f.live.get("busy"):
        reasons.append("live_busy")
    # A pending swap-macro list is only what was asked; the start's own fact is in the detail.
    if effects_pending(a, intents=False) or f.live.get("effects"):
        reasons.append("effects_pending")
    if not q.get("recovery_identity") or not i.get("recovery_identity") or not a.get("recovery_identity"):
        reasons.append("identity_missing")
    if (
        i.get("archive_id") != a.get("id")
        or i.get("queue_id") != q["id"]
        or q.get("printer_id") != f.printer_id
        or a.get("deleted_at") is not None
        or f.archive_users != 1
    ):
        reasons.append("execution_unproven")
    if (
        len(f.printing_archive_ids) != 1
        or len(f.active_item_ids) != 1
        or q.get("current_item_id") not in (None, i["id"])
    ):
        reasons.append("ambiguous_owner")
    return {
        "code": "interrupted_dispatch",
        "actions": [] if reasons else ["close_interrupted_attempt"],
        "blocked_reasons": sorted(set(reasons)),
    }


def _classify_single(f: RecoveryFacts) -> dict | None:
    if f.runtime is not None:
        return _classify_runtime(f)
    q, i, a = f.queue, f.item, f.archive
    interrupted = _interrupted_dispatch(f)
    if interrupted is not None:
        return interrupted
    if i is None and a and a.get("status") == "printing":
        if f.live.get("stable_inactive"):
            return {"code": "active_archive_without_current_run", "actions": [], "blocked_reasons": ["outcome_unknown"]}
        return None
    if i is not None and i.get("status") != "printing":
        if q.get("status") == "printing" and q.get("current_item_id") == i.get("id"):
            return {
                "code": "header_without_active_item",
                "actions": [],
                "blocked_reasons": ["effects_pending" if effects_pending(a) else "ambiguous_owner"],
            }
        return None
    if i:
        if a is None:
            code = "active_item_missing_archive"
        elif a.get("printer_id") != f.printer_id or a.get("queue_id") != q["id"]:
            code = "archive_owner_mismatch"
        elif a.get("status") in OUTCOMES:
            code = "terminal_archive_active_item"
        else:
            return None
    elif len(f.active_item_ids) > 1:
        return {"code": "multiple_active_items", "actions": [], "blocked_reasons": ["ambiguous_owner"]}
    elif q.get("status") == "printing" and not f.active_item_ids:
        code = "header_without_active_item"
    else:
        return None

    reasons = []
    mode = f.live.get("mode")
    if mode not in {"inactive", "successor"}:
        reasons.append("live_unknown")
    if f.live.get("busy"):
        reasons.append("live_busy")
    if effects_pending(a) or f.live.get("effects"):
        reasons.append("effects_pending")
    if not q.get("recovery_identity") or (i and not i.get("recovery_identity")):
        reasons.append("identity_missing")
    if i is None:
        if mode != "inactive" or f.active_item_ids or f.printing_archive_ids or f.references:
            reasons.append("ambiguous_owner")
        return {
            "code": code,
            "actions": [] if reasons else ["release_orphan_header"],
            "blocked_reasons": sorted(set(reasons)),
        }

    if not a or not a.get("recovery_identity"):
        reasons.append("execution_unproven")
    else:
        extra = a.get("extra_data")
        snapshot = i.get("source_snapshot")
        if snapshot is not None and not isinstance(snapshot, dict):
            reasons.append("execution_unproven")
        else:
            if (
                snapshot
                and "version" in snapshot
                and (type(snapshot["version"]) is not int or snapshot["version"] != 1)
            ):
                reasons.append("execution_unproven")
            provenance = (snapshot or {}).get("provenance")
            if (
                provenance is not None
                and not isinstance(provenance, dict)
                or provenance
                and (provenance.get("kind") not in {"archive", "library_file"} or type(provenance.get("id")) is not int)
            ):
                reasons.append("execution_unproven")
            elif provenance and provenance.get("kind") == "archive" and provenance.get("id") == a["id"]:
                reasons.append("source_archive")
        if (
            a.get("printer_id") != f.printer_id
            or a.get("queue_id") != q["id"]
            or i.get("queue_id") != q["id"]
            or q.get("printer_id") != f.printer_id
            or a.get("deleted_at") is not None
            or f.archive_users != 1
        ):
            reasons.append("execution_unproven")
        if extra is not None and (not isinstance(extra, dict) or extra.get("recovered_outcome_uncertain")):
            reasons.append("outcome_unknown")
        claim, start, end = utc(i.get("started_at")), utc(a.get("started_at")), utc(a.get("completed_at"))
        if not claim or not start or start < claim:
            reasons.append("invalid_run_times")
        if a.get("status") not in OUTCOMES:
            reasons.append("outcome_unknown")
        if mode == "inactive":
            if f.printing_archive_ids or q.get("current_item_id") not in (None, i["id"]):
                reasons.append("ambiguous_owner")
        elif mode == "successor":
            successor_start = utc(f.live.get("started_at"))
            if (
                not end
                or not successor_start
                or successor_start <= end
                or q.get("current_item_id") != f.live.get("item_id")
                or i["id"] == f.live.get("item_id")
            ):
                reasons.append("ambiguous_owner")
        if end and (not claim or not start or end <= max(claim, start) or end > utc(f.now)):
            reasons.append("invalid_run_times")
        if end is None and not (a.get("status") == "completed" and mode == "inactive" and estimated_end(f)):
            reasons.append("missing_run_times")

    actions = []
    if not reasons:
        if a.get("completed_at") is None:
            actions.append("fill_estimated_end_and_align")
        else:
            actions.append("align_terminal_item")
            extra = a.get("extra_data") or {}
            if a["status"] == "completed" and "operator_recovery_end" not in extra and not f.references:
                actions.append("delete_stale_item")
    if f.references:
        reasons.append("delete_has_dependencies")
    if a and isinstance(a.get("extra_data"), dict) and "operator_recovery_end" in a["extra_data"]:
        reasons.append("estimated_time")
    return {"code": code, "actions": actions, "blocked_reasons": sorted(set(reasons))}


def _addressed(f: RecoveryFacts):
    from backend.app.services import swap_terminal

    a, q = f.archive, f.queue
    settlement = f.target_kind == "archive" and a and swap_terminal.claim_stage(a.get("extra_data")) == "owed"
    if not a or (not settlement and not f.members):
        return None
    if not settlement and a.get("status") not in OUTCOMES:
        return None
    reasons = set()
    members = f.members
    if not f.members_complete or len(members) > 100:
        reasons.add("group_overflow")
    if (
        not q
        or q.get("printer_id") != f.printer_id
        or a.get("printer_id") != f.printer_id
        or a.get("queue_id") != q.get("id")
    ):
        reasons.add("archive_foreign")
    if (
        not a.get("recovery_identity")
        or not q
        or not q.get("recovery_identity")
        or any(not r.get("recovery_identity") for r in members)
    ):
        reasons.add("identity_missing")
    if a.get("deleted_at") is not None:
        reasons.add("archive_missing")
    extra = a.get("extra_data")
    if (
        not isinstance(extra, dict)
        or extra.get("recovered_outcome_uncertain")
        or extra.get("terminal_effects_uncertain")
    ):
        reasons.add("outcome_unknown")
    if any(r.get("archive_id") != a["id"] or r.get("queue_id") != q.get("id") for r in members):
        reasons.add("ambiguous_owner")
    if (
        (q and q.get("current_item_id") not in ({None} | {r["id"] for r in members}))
        or f.printing_archive_ids
        or f.foreign_active_users
    ):
        reasons.add("ambiguous_owner")
    if f.references:
        reasons.add("recovery_has_dependencies")
    live, runtime = f.live, f.repair_runtime or {}
    if not live.get("stable_inactive") or live.get("idle") is not True:
        reasons.add("live_unknown")
    if live.get("busy"):
        reasons.add("live_busy")
    owners = runtime.get("owners", {})
    if any(owners.get(name) is not False for name in RUNTIME_OWNERS):
        reasons.add(
            "context_owner_active" if any(owners.get(n) is True for n in RUNTIME_OWNERS) else "context_owner_unknown"
        )
    for key in ("resolution", "pending_terminal", "layer_timelapse"):
        if runtime.get(key) is not False:
            reasons.add("context_owner_unknown")
    if runtime.get("finishing") != 0:
        reasons.add("finishing_in_progress")
    current = runtime.get("current")
    if current and (
        current.get("archive_id") != a["id"]
        or current.get("archive_identity") != a.get("recovery_identity")
        or current.get("queue_item_id") not in ({None} | {m["id"] for m in members})
    ):
        reasons.add("ambiguous_owner")
    for key in ("macro_selection", "swap_config"):
        if key in runtime:
            reasons.update(_registration_reasons(runtime[key]))
    admission = runtime.get("admission")
    if admission is not None and (
        not isinstance(admission, dict)
        or admission.get("phase") != "processed"
        or admission.get("archive_id") != a["id"]
    ):
        reasons.add("admission_unproven")
    if settlement:
        pending = extra.get("swap_macro_events_pending") if isinstance(extra, dict) else None
        effect = (
            (extra.get("bamdude_terminal_effects") or {}).get("swap_change_table") if isinstance(extra, dict) else None
        )
        if (
            a.get("status") != "completed"
            or not isinstance(effect, dict)
            or effect.get("version", 1) != 1
            or not isinstance(pending, list)
            or pending.count("swap_mode_change_table") != 1
        ):
            reasons.add("effects_unreadable")
        action, code = "settle_swap_by_operator", "swap_settlement_required"
    else:
        reasons.update(runtime_effect_debt(extra, a.get("recovery_identity")))
        action, code = "cancel_terminal_claims", "terminal_archive_conflicting_claims"
    return {"code": code, "actions": [] if reasons else [action], "blocked_reasons": sorted(reasons)}


def is_requeued_gate_owner(f: RecoveryFacts) -> bool:
    """A pending item whose printer's armed plate gate waits on its archive (spec plate-gate-owner-requeue B1)."""
    i, gate = f.item, f.gate or {}
    return bool(
        f.target_kind == "item"
        and i
        and i.get("status") == "pending"
        and gate.get("armed")
        and gate.get("archive_id") is not None
        and i.get("archive_id") == gate.get("archive_id")
    )


def _classify_requeued_gate_owner(f: RecoveryFacts) -> dict:
    """Return the row to its run's outcome only on full proof (B3); the gate itself is never touched.

    Once the row is finished again the plate answer finds it, and Repeat print on the card does what the
    earlier Retry meant to do. The archive must be THIS row's run, not a source it was queued from.
    """
    q, i, a = f.queue, f.item, f.archive
    reasons = []
    if not q.get("recovery_identity") or not i.get("recovery_identity") or not (a or {}).get("recovery_identity"):
        reasons.append("identity_missing")
    if (
        a is None
        or a.get("printer_id") != f.printer_id
        or a.get("queue_id") != q["id"]
        or i.get("queue_id") != q["id"]
        or q.get("printer_id") != f.printer_id
        or a.get("deleted_at") is not None
    ):
        reasons.append("execution_unproven")
    elif a.get("status") not in OUTCOMES:
        reasons.append("outcome_unknown")
    snapshot = i.get("source_snapshot")
    provenance = snapshot.get("provenance") if isinstance(snapshot, dict) else None
    if (
        isinstance(provenance, dict)
        and provenance.get("kind") == "archive"
        and provenance.get("id") == (a or {}).get("id")
    ):
        reasons.append("source_archive")
    claim, start = utc(i.get("started_at")), utc((a or {}).get("started_at"))
    if not claim or not start or start < claim:
        reasons.append("invalid_run_times")
    # A missing archive end is no veto: the row then takes the apply's own clock, recorded in the audit (B4).
    if i.get("dispatch_attempts") != 0:
        reasons.append("dispatch_started")
    if (
        q.get("status") == "printing"
        or q.get("current_item_id") == i["id"]
        or f.active_item_ids
        or f.printing_archive_ids
    ):
        reasons.append("ambiguous_owner")
    if f.live.get("busy"):
        reasons.append("live_busy")
    if f.live.get("mode") != "inactive":
        reasons.append("live_unknown")
    if effects_pending(a) or f.live.get("effects"):
        reasons.append("effects_pending")
    return {
        "code": "plate_gate_owner_requeued",
        "actions": [] if reasons else [GATE_OWNER_ACTION],
        "blocked_reasons": sorted(set(reasons)),
    }


def classify(f: RecoveryFacts) -> dict | None:
    if f.live.get("observation_unproven") and (not f.archive or f.archive.get("status") == "printing"):
        return {"code": "run_identity_unproven", "actions": [], "blocked_reasons": ["run_identity_unproven"]}
    if is_requeued_gate_owner(f):
        return _classify_requeued_gate_owner(f)
    if f.target_kind == "archive":
        addressed = _addressed(f)
        if addressed and addressed["code"] == "swap_settlement_required":
            return {**addressed, "actions": [a for a in addressed["actions"] if a == "settle_swap_by_operator"]}
        # Action filtering must not hide an archive that vetoes the group.
        single = _classify_single(f)
        return {**single, "actions": []} if single else None
    single = _classify_single(f)
    if f.runtime is not None:
        return single
    if (
        f.target_kind == "item"
        and f.item
        and single
        and single["actions"]
        and sum(m.get("archive_id") == f.item.get("archive_id") for m in f.members) <= 1
    ):
        return single
    if f.target_kind == "header" and len(f.members) == 1:
        singleton = _classify_single(replace(f, item=f.members[0], target_kind="item"))
        if singleton and singleton["actions"]:
            return None  # The ordinary addressed alignment remains the sole action.
    addressed = _addressed(f)
    if addressed and f.target_kind == "item":
        return {**addressed, "actions": [], "related": {"kind": "header", "target_id": f.queue["id"]}}
    if addressed and (f.target_kind == "header" or len(f.members) > 1 or not single or not single["actions"]):
        return addressed
    return single


def plan_action(
    f: RecoveryFacts, action: str, *, confirmed_completed=False, confirmed_idle=False, confirmed_manual_swap=False
) -> dict:
    if f.target_kind == "archive" and action != "settle_swap_by_operator":
        raise ValueError("recovery_not_allowed")
    issue = classify(f)
    if issue is None or action not in issue["actions"]:
        raise ValueError("recovery_not_allowed")
    q, i, a = f.queue, f.item, f.archive
    plan = {"item": {}, "archive": {}, "queue": {}, "delete_item": False}
    if action == GATE_OWNER_ACTION:
        # The row only: the gate, the archive, the queue's pause and every other row stay as they are (B4, B5).
        # Without an archive end the row takes the apply's one clock ("apply", as a stale-claim group's members),
        # outside the plan so the preview's hash holds; the waiting and error fields are left empty.
        plan["item"] = {
            "status": OUTCOMES[a["status"]],
            "completed_at": a["completed_at"] or "apply",
            "waiting_reason": None,
            "error_code": None,
            "error_message": None,
        }
        return plan
    if action in {"cancel_terminal_claims", "settle_swap_by_operator"}:
        if f.target_kind != ("header" if action == "cancel_terminal_claims" else "archive"):
            raise ValueError("recovery_not_allowed")
        if confirmed_idle is not True or (action == "settle_swap_by_operator" and confirmed_manual_swap is not True):
            raise ValueError("recovery_not_allowed")
        plan["queue"] = {"is_paused": True}
        if action == "cancel_terminal_claims":
            plan["members"] = [
                {
                    "id": m["id"],
                    "recovery_identity": m["recovery_identity"],
                    "status": "cancelled",
                    "completed_at": "apply",
                    "error_code": "operator_cancelled_stale_claim",
                }
                for m in f.members
            ]
            plan["queue"].update(current_item_id=None, status="paused")
        else:
            plan["settlement"] = {
                "stage": "settled_by_operator",
                "archive_id": a["id"],
                "archive_identity": a["recovery_identity"],
            }
        return plan
    if action == RUNTIME_ACTION:
        # Rows are not touched: the queue is paused (the flag alone, never its status) and the run's context
        # goes from memory. The operator resumes the queue once the printer is checked (spec r3 R7, R8).
        r = f.runtime
        plan["queue"] = {"is_paused": True}
        plan["runtime"] = {
            "archive_id": r["current"]["archive_id"],
            "admission": r["admission"] is not None,
            "macro_selection": r["macro_selection"]["present"],
            "swap_config": r["swap_config"]["present"],
        }
        return plan
    if action == "close_interrupted_attempt":
        # The end time is the apply's own clock, outside the plan, so the preview's plan hash holds.
        code, detail = interrupted_close(a)
        plan["archive"] = {"close": True, "status": "failed", "failure_reason": code, "error_message": detail}
        plan["item"] = {"close": True, "status": "failed", "error_code": code, "error_message": detail}
        plan["queue"] = {"current_item_id": None, "status": "error"}
        return plan
    if action == "fill_estimated_end_and_align":
        if confirmed_completed is not True:
            raise ValueError("recovery_not_allowed")
        plan["archive"] = {
            "completed_at": estimated_end(f),
            "extra_data": {
                **(a.get("extra_data") or {}),
                "operator_recovery_end": {
                    "version": 1,
                    "source": "slicer_estimate",
                    "estimated_at": estimated_end(f),
                },
            },
        }
    if i:
        if action == "delete_stale_item":
            plan["delete_item"] = True
        else:
            plan["item"] = {
                "status": OUTCOMES[a["status"]],
                "completed_at": plan["archive"].get("completed_at", a.get("completed_at")),
                "waiting_reason": None,
            }
    if f.live["mode"] == "inactive" and q.get("current_item_id") in (None, (i or {}).get("id")):
        plan["queue"] = {
            "current_item_id": None,
            "status": "error" if q["status"] == "error" else "paused",
            "is_paused": True,
        }
    elif action == "release_orphan_header":
        plan["queue"] = {"current_item_id": None, "status": "paused", "is_paused": True}
    return plan
