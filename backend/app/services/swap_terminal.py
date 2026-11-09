"""What a finished run may do to a swap printer's plate (swap-source-lifecycle spec r4 §10, plan T7).

Two decisions, both taken once per run, and both asked by the live completion and by the connect-edge sweep:

* **baked clear** -- the source's own plate-swap sequence cleared the plate: only for a run BamDude can prove
  it addressed, completed, over a declaration strictly True, and never for an order auto-eject archive;
* **our change_table** -- the macro dispatch left owed: for the completed execution still owning its queue on a printer in
  swap mode, over a source KNOWN not to carry its own sequence (False). Anything else HOLDS: nothing runs, the
  event stays owed, the plate is not cleared, and the queue waits with a reason.

The reason says whether our swap cycle had begun -- the start macro's outcome dispatch recorded on the
execution archive. A list of requested events proves nothing.

Our change_table goes out at most once (Codex review r1): one durable claim per execution archive, beside the
other terminal effects the completion records, is compare-and-set to ``attempted`` and committed before the
command, under the dispatch lane's lock, by whichever of the live completion and the sweep gets there first. A
second owner stands down; a claim nobody alive is sending is held, never sent again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from backend.app.i18n import current_language, t
from backend.app.services import swap_compatibility
from backend.app.services.archive_write_scope import archive_write_scope, load_active_archive_for_write

#: The swap-start macro's outcome, recorded on the execution archive by dispatch: ``possibly_fired`` from the
#: moment the command may leave, ``fired`` once the printer acknowledged it.
START_EFFECT_KEY = "swap_start_effect"
START_POSSIBLY_FIRED = "possibly_fired"
START_FIRED = "fired"
SETTLED_BY_OPERATOR = "settled_by_operator"


def operator_settlement(extra, archive_identity=None):
    """Read only our complete addressed v1 assertion; never interpret it as a command answer."""
    if not isinstance(extra, Mapping):
        return None
    effects = extra.get("bamdude_terminal_effects")
    record = effects.get("swap_change_table") if isinstance(effects, Mapping) else None
    if not isinstance(record, dict) or record.get("stage") != SETTLED_BY_OPERATOR:
        return None
    try:
        if type(record.get("version")) is not int or record["version"] != 1:
            return None
        UUID(record["archive_identity"])
        UUID(record["action_identity"])
        datetime.fromisoformat(record["at"])
        if not isinstance(record["actor"], str) or not record["actor"]:
            return None
        previous = record["previous_effect"]
        if not isinstance(previous, dict) or previous.get("stage") != "owed" or previous.get("version", 1) != 1:
            return None
        hold = record["previous_hold"]
        if not isinstance(hold, dict) or type(hold.get("present")) is not bool or "value" not in hold:
            return None
        if archive_identity is not None and record["archive_identity"] != archive_identity:
            return None
    except (KeyError, ValueError, TypeError, AttributeError):
        return None
    return record


def unsettled_hold(extra, archive_identity=None):
    if not isinstance(extra, Mapping) or "swap_hold" not in extra:
        return False
    settled = operator_settlement(extra, archive_identity)
    return not (settled and settled["previous_hold"] == {"present": True, "value": extra["swap_hold"]})


def settle_by_operator(archive, *, action_identity, actor, at):
    """Caller proved idle/ownership, holds archive+lane/writer, and owns commit/audit."""
    extra = dict(archive.extra_data or {})
    effects = dict(extra.get("bamdude_terminal_effects") or {})
    previous = effects.get("swap_change_table")
    pending = extra.get("swap_macro_events_pending")
    if (
        not isinstance(previous, dict)
        or previous.get("stage") != "owed"
        or previous.get("version", 1) != 1
        or not isinstance(pending, list)
        or pending.count("swap_mode_change_table") != 1
    ):
        raise ValueError("recovery_changed")
    record = {
        "version": 1,
        "stage": SETTLED_BY_OPERATOR,
        "archive_identity": archive.recovery_identity,
        "action_identity": action_identity,
        "actor": actor,
        "at": at.isoformat(),
        "previous_effect": dict(previous),
        "previous_hold": {"present": "swap_hold" in extra, "value": extra.get("swap_hold")},
    }
    effects["swap_change_table"] = record
    extra["bamdude_terminal_effects"] = effects
    extra["swap_macro_events_pending"] = [e for e in pending if e != "swap_mode_change_table"]
    if operator_settlement(extra, archive.recovery_identity) is None:
        raise ValueError("recovery_changed")
    archive.extra_data = extra


#: What a held run records on its archive: the codes the archive card shows (never a sentence).
HOLD_KEY = "swap_hold"

CYCLE_STARTED = "started"
CYCLE_POSSIBLY_STARTED = "possibly_started"
CYCLE_NOT_STARTED = "not_started"

#: Why our change_table did not run. Each is a key of ``filament_routing`` and of the frontend's archive card.
HOLD_SOURCE_UNKNOWN = "swap_hold_source_unknown"
HOLD_SOURCE_BAKED = "swap_hold_source_baked"
HOLD_RUN_UNPROVEN = "swap_hold_run_unproven"
HOLD_NOT_COMPLETED = "swap_hold_not_completed"
HOLD_SWAP_OFF = "swap_hold_swap_off"
HOLD_OUTCOME_UNCERTAIN = "swap_hold_outcome_uncertain"
HOLD_MACRO_ERROR = "swap_hold_macro_error"
HOLD_ATTEMPT_UNKNOWN = "swap_hold_attempt_unknown"
HOLD_REASONS = frozenset(
    {
        HOLD_SOURCE_UNKNOWN,
        HOLD_SOURCE_BAKED,
        HOLD_RUN_UNPROVEN,
        HOLD_NOT_COMPLETED,
        HOLD_SWAP_OFF,
        HOLD_OUTCOME_UNCERTAIN,
        HOLD_MACRO_ERROR,
        HOLD_ATTEMPT_UNKNOWN,
    }
)

#: The completion's record of its non-replay-safe effects (``main._TERMINAL_EFFECTS_KEY``) and our change_table's
#: entry in it -- the claim. Stages: ``owed`` (the sweep closed a run that was still printing: nothing can have
#: attempted it), ``attempted`` (committed before the command), ``finished`` / ``failed`` (the answer, written with
#: the pending event's removal).
EFFECTS_KEY = "bamdude_terminal_effects"
CHANGE_TABLE_EFFECT = "swap_change_table"
CHANGE_TABLE_EVENT = "swap_mode_change_table"
CLAIM_OWED = "owed"
CLAIM_ATTEMPTED = "attempted"
CLAIM_FINISHED = "finished"
CLAIM_FAILED = "failed"
ANSWERED = frozenset({CLAIM_FINISHED, CLAIM_FAILED})

#: What :func:`claim_change_table` answers: the command may go out, another owner in this process is sending it,
#: the claim is not one this caller may take (attempted, finished, never owed), or the run no longer holds its
#: printer.
CLAIMED = "claimed"
CLAIM_BUSY = "busy"
CLAIM_TAKEN = "taken"
CLAIM_LOST = "lost"

#: Execution archives whose change_table this process is sending right now -- the live owner a claim belongs to.
#: Empty after a restart, so an ``attempted`` claim left by a dead process is nobody's: held, never sent.
_sending: set[int] = set()


def record_start_effect(archive, stage: str) -> None:
    """Dispatch's one writer of the start macro's outcome on the execution archive (a new dict, other keys kept)."""
    if stage not in (START_POSSIBLY_FIRED, START_FIRED):
        raise ValueError(f"not a swap-start outcome: {stage!r}")
    extra = getattr(archive, "extra_data", None)
    merged = dict(extra) if isinstance(extra, Mapping) else {}
    merged[START_EFFECT_KEY] = stage
    archive.extra_data = merged


def cycle_state(extra: Mapping | None) -> str:
    """Whether our swap cycle began for this run, from what dispatch recorded.

    An archive with a declaration record was created by a dispatch that records the start, so no record of the
    start means it was not called. One without (from before the fix) has no fact either way: may have started.
    """
    extra = extra if isinstance(extra, Mapping) else {}
    effect = extra.get(START_EFFECT_KEY)
    if effect == START_FIRED:
        return CYCLE_STARTED
    if effect == START_POSSIBLY_FIRED:
        return CYCLE_POSSIBLY_STARTED
    if swap_compatibility.CARRIER_KEY in extra:
        return CYCLE_NOT_STARTED
    return CYCLE_POSSIBLY_STARTED


def baked_clears(fact: Any, *, completed: bool, proven: bool, auto_eject: bool) -> bool:
    """D5: the source's own sequence cleared the plate -- this exact run, completed, strictly True, no auto-eject."""
    return completed and proven and fact is True and not auto_eject


def change_table_hold(fact: Any, *, completed: bool, execution_owned: bool, swap_mode: bool) -> str | None:
    """An owed macro belongs to the persisted execution, not its recovered runtime origin."""
    if not completed:
        return HOLD_NOT_COMPLETED
    if not execution_owned:
        return HOLD_RUN_UNPROVEN
    if fact is True:
        return HOLD_SOURCE_BAKED
    if fact is not False:
        return HOLD_SOURCE_UNKNOWN
    if not swap_mode:
        return HOLD_SWAP_OFF
    return None


async def pending_fact(db, archive, execution_item) -> bool | None:
    """The declaration our owed change_table is decided on.

    The archive's own record answers. Only a record-less archive (from before the fix, whose False the reader
    calls unknown) may ask the job that ran it -- the exact execution-linked row the caller proved -- through
    that job's own snapshot and its §6 resolution; never a source archive of a reprint, never a name.
    """
    fact = swap_compatibility.archive_fact(archive)
    if fact.recorded or fact.invalid or fact.value is not None or execution_item is None:
        return fact.value
    from backend.app.services.filament_intake import execution_swap_fact

    # The run's own evidence only: never a source's later declaration (swap-source-declaration spec r3 §2).
    swap_fact = await execution_swap_fact(db, execution_item)
    return swap_fact.value if swap_fact is not None else None


def hold_text(reason: str, cycle: str, lang: str | None = None) -> str:
    """The sentence the operator reads -- the cycle's when it may have begun, the reason's otherwise."""
    lang = lang or current_language()
    if cycle == CYCLE_STARTED:
        return t(lang, "filament_routing", "swap_cycle_started")
    if cycle == CYCLE_POSSIBLY_STARTED:
        return t(lang, "filament_routing", "swap_cycle_possibly_started")
    return t(lang, "filament_routing", reason)


def hold_record(reason: str, cycle: str) -> dict:
    return {"reason": reason, "cycle": cycle}


def claim_stage(extra: Mapping | None) -> str | None:
    """The stage of our change_table's claim on this archive, or ``None`` when none is recorded."""
    effects = extra.get(EFFECTS_KEY) if isinstance(extra, Mapping) else None
    effect = effects.get(CHANGE_TABLE_EFFECT) if isinstance(effects, Mapping) else None
    stage = effect.get("stage") if isinstance(effect, Mapping) else None
    return stage if isinstance(stage, str) else None


def sending(archive_id: int) -> bool:
    """Whether a live owner in this process is sending this archive's change_table now."""
    return archive_id in _sending


def release(archive_id: int) -> None:
    """The owner is done with the command and its bookkeeping, or stopped: the claim is no longer live."""
    _sending.discard(archive_id)


def _with_stage(extra: Mapping, stage: str, *, owner: str) -> dict:
    merged = dict(extra)
    effects = merged.get(EFFECTS_KEY)
    effects = dict(effects) if isinstance(effects, Mapping) else {}
    effects[CHANGE_TABLE_EFFECT] = {"stage": stage, "owner": owner, "at": datetime.now(timezone.utc).isoformat()}
    merged[EFFECTS_KEY] = effects
    return merged


def mark_owed(extra: Mapping) -> dict:
    """The sweep closes a run that was still printing, our change_table pending, no finish accepted: nothing can
    have attempted it (the live completion does only after accepting the finish, the sweep only after this close).
    Recorded as ``owed`` -- the proof a later sweep needs to send it -- never over a claim already there."""
    if claim_stage(extra) is not None:
        return dict(extra)
    return _with_stage(extra, CLAIM_OWED, owner="sweep")


async def claim_change_table(
    session_factory,
    archive_id: int,
    *,
    queue_id: int | None,
    owner: str,
    allow_absent: bool,
    still_ours: Callable[[Any], Awaitable[bool]] | None = None,
) -> str:
    """Compare-and-set this run's change_table claim to ``attempted`` and commit it -- before the command.

    Under the dispatch lane's lock (``queue_claim_scope``) and the print's own write scope, in a short transaction
    with no I/O: the archive is read again, the event must still be pending, the claim ``owed`` (or absent, for the
    live completion, whose accepted finish is exclusive), and ``still_ours`` -- the caller's run proof against the
    queue claim -- must hold. Returns :data:`CLAIMED`, or why the command must not go out. The archive is marked
    live before the commit, so no other owner can find it attempted and nobody's; a failed commit unmarks it.
    """
    from backend.app.services.queue_ops import claim_lanes

    # The print, then the lane -- the one order of every archive + lane writer (``claim_lanes``): every in-process
    # lock before SQLite's writer, and on PostgreSQL the archive's guard before the lane's, then the archive row.
    lanes = [queue_id] if queue_id is not None else []
    async with session_factory() as db, claim_lanes(db, lanes, archive_id=archive_id):
        archive = await load_active_archive_for_write(db, archive_id)
        if archive is None:
            return CLAIM_LOST
        extra = archive.extra_data if isinstance(archive.extra_data, Mapping) else {}
        if CHANGE_TABLE_EVENT not in (extra.get("swap_macro_events_pending") or []):
            return CLAIM_TAKEN
        stage = claim_stage(extra)
        if stage == CLAIM_ATTEMPTED and sending(archive_id):
            return CLAIM_BUSY
        if not (stage == CLAIM_OWED or (stage is None and allow_absent)):
            return CLAIM_TAKEN
        if still_ours is not None and not await still_ours(db):
            return CLAIM_LOST
        archive.extra_data = _with_stage(extra, CLAIM_ATTEMPTED, owner=owner)
        _sending.add(archive_id)
        try:
            await db.commit()
        except BaseException:
            _sending.discard(archive_id)
            raise
    return CLAIMED


async def finish_change_table(session_factory, archive_id: int, *, ok: bool) -> None:
    """The command's answer, in one transaction: the claim ``finished`` / ``failed``, the event no longer pending
    (it went out -- never again), and on success a hold an earlier decision recorded is cleared."""
    from backend.app.services.archive import remove_swap_pending_event

    async with session_factory() as db, archive_write_scope(db, archive_id):
        archive = await load_active_archive_for_write(db, archive_id)
        if archive is None:
            return
        extra = archive.extra_data if isinstance(archive.extra_data, Mapping) else {}
        effects = extra.get(EFFECTS_KEY) if isinstance(extra.get(EFFECTS_KEY), Mapping) else {}
        effect = effects.get(CHANGE_TABLE_EFFECT) if isinstance(effects.get(CHANGE_TABLE_EFFECT), Mapping) else {}
        archive.extra_data = _with_stage(
            extra, CLAIM_FINISHED if ok else CLAIM_FAILED, owner=str(effect.get("owner") or "unknown")
        )
        remove_swap_pending_event(archive, CHANGE_TABLE_EVENT)
        if ok:
            clear_hold(archive)
        await db.commit()


def clear_hold(archive) -> bool:
    """Our table swap ran: what an earlier decision recorded as held no longer stands (a new dict, other keys
    kept). Returns whether there was a hold to clear."""
    extra = getattr(archive, "extra_data", None)
    if not isinstance(extra, Mapping) or HOLD_KEY not in extra:
        return False
    archive.extra_data = {key: value for key, value in extra.items() if key != HOLD_KEY}
    return True
