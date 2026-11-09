"""Prove execution identity before publication; never adopt by downloaded bytes."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_sources
from backend.app.services.print_claims import ClaimResult
from backend.app.services.print_observation import PrintObservation, run_id

NONCURRENT = "noncurrent_execution"
STALE_CLAIM_CODES = frozenset({"operator_cancelled_stale_claim", "noncurrent_execution_claim"})


class StaleClaimRepeat(queue_sources.QueueSourceError):
    reason = "stale_execution_claim"
    http_status = 409

    def __init__(self, archive_id=None):
        self.archive_id = archive_id
        super().__init__()


async def require_repeatable(db, row, *, whole_batch=False):
    if row.error_code in STALE_CLAIM_CODES:
        raise StaleClaimRepeat(row.archive_id)
    if whole_batch and row.batch_id:
        stale = await db.scalar(
            select(PrintQueueItem)
            .where(PrintQueueItem.batch_id == row.batch_id, PrintQueueItem.error_code.in_(STALE_CLAIM_CODES))
            .order_by(PrintQueueItem.id)
            .limit(1)
        )
        if stale is not None:
            raise StaleClaimRepeat(stale.archive_id)


class ExecutionChanged(ValueError):
    pass


@dataclass(frozen=True)
class StartPublication(ClaimResult):
    # Durability and permission to bind the current runtime are separate facts.
    current: bool = True


def durable_id(archive):
    extra = archive.extra_data or {}
    if not isinstance(extra, dict):
        raise ExecutionChanged("unreadable execution")
    intent = extra.get("dispatch_intent")
    sent = None
    if intent is not None:
        if not isinstance(intent, dict) or type(intent.get("version")) is not int or intent["version"] != 1:
            raise ExecutionChanged("unreadable dispatch intent")
        sent = run_id(intent.get("submission_id"))
    observed = run_id(archive.subtask_id)
    if observed and sent and observed != sent:
        raise ExecutionChanged("contradictory run ids")
    return observed or sent


@dataclass(frozen=True)
class StartDecision:
    current_id: int | None
    current_identity: str | None
    old: tuple[tuple[int, str, str | None], ...]
    reason: str
    epoch: int
    observation: PrintObservation | None
    printer_lifetime: int = 0
    current_run_id: str | None = None
    current_status: str | None = None
    attempts: tuple[tuple[int, str, str | None], ...] = ()


async def decide_start(
    db, printer_id, data, *, catch_up=False, prepared_archive_id=None, prepared_archive_identity=None
):
    epoch = queue_sources.current_epoch()
    observation = data.get("_print_observation")
    if observation is not None and not isinstance(observation, PrintObservation):
        raise ExecutionChanged("untrusted observation")
    if observation and observation.verdict == "file_change_unproven":
        raise ExecutionChanged("file_change_unproven")
    live_id = run_id(data.get("subtask_id"))
    archives = list(
        (
            await db.scalars(
                select(PrintArchive)
                .where(
                    PrintArchive.printer_id == printer_id,
                    PrintArchive.status == "printing",
                    PrintArchive.deleted_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    # A slow dispatch may have been failed by its watchdog before firmware
    # finally reports it running. Only its exact prepared UUID and submitted
    # nonzero id may recover that attempt; an alias cannot reopen history.
    if prepared_archive_id is not None and prepared_archive_id not in {a.id for a in archives}:
        prepared = await db.get(PrintArchive, prepared_archive_id, populate_existing=True)
        if (
            prepared is not None
            and prepared_archive_identity is not None
            and prepared.recovery_identity == prepared_archive_identity
            and prepared.printer_id == printer_id
            and prepared.deleted_at is None
            and prepared.status == "failed"
            and prepared.dispatch_phase in {"prepared", "sending", "sent"}
            and live_id
            and durable_id(prepared) == live_id
        ):
            archives.append(prepared)
    ids = {a.id: durable_id(a) for a in archives}
    matched = [a for a in archives if live_id and ids[a.id] == live_id]
    if not matched and prepared_archive_id is not None:
        matched = [
            a
            for a in archives
            if a.id == prepared_archive_id
            and (prepared_archive_identity is None or a.recovery_identity == prepared_archive_identity)
            and not (live_id and ids[a.id] and live_id != ids[a.id])
        ]
    if not matched and catch_up:
        from backend.app.services.print_reconciliation import _name_matches_subtask

        name = data.get("subtask_name") or ""
        matched = [
            a
            for a in archives
            if not (live_id and ids[a.id] and live_id != ids[a.id]) and (not name or _name_matches_subtask(a, name))
        ]
    if len(matched) > 1:
        raise ExecutionChanged("ambiguous execution")
    current = matched[0] if matched else None
    old = []
    attempts = []
    reason = "observed_new_start" if observation and observation.verdict == "observed_new_start" else "different_run_id"
    for archive in archives:
        if archive is current:
            continue
        if archive.dispatch_phase in {"prepared", "sending", "sent"} and (
            catch_up or (observation is not None and observation.new_run)
        ):
            # An upload is an attempt, not evidence of a physical run that B
            # superseded. Leave its claim for the dispatcher's bind-conflict
            # deferral; it must remain repeatable and retain its own evidence.
            attempts.append((archive.id, archive.recovery_identity, ids[archive.id]))
            continue
        different = bool(live_id and ids[archive.id] and live_id != ids[archive.id])
        client_proven = bool(observation and observation.new_run)
        if not (reason == "observed_new_start" or (different and (client_proven or catch_up))):
            raise ExecutionChanged("execution difference unproven")
        if not archive.recovery_identity:
            raise ExecutionChanged("missing incarnation")
        old.append((archive.id, archive.recovery_identity, ids[archive.id]))
    if epoch != queue_sources.current_epoch():
        raise ExecutionChanged("storage changed during proof")
    return StartDecision(
        current.id if current else None,
        current.recovery_identity if current else None,
        tuple(old),
        reason,
        epoch,
        observation,
        data.get("_bamdude_printer_life", 0),
        durable_id(current) if current else None,
        current.status if current else None,
        tuple(attempts),
    )


def valid_noncurrent(archive):
    extra = archive.extra_data if isinstance(archive.extra_data, dict) else {}
    marker = extra.get(NONCURRENT)
    if not isinstance(marker, dict) or type(marker.get("version")) is not int or marker.get("version") != 1:
        return False
    try:
        for key in ("archive_identity", "current_archive_identity", "decision_identity"):
            UUID(marker[key])
        if (
            marker["archive_identity"] != archive.recovery_identity
            or marker["current_archive_identity"] == archive.recovery_identity
            or type(marker["printer_lifetime"]) is not int
            or type(marker["storage_epoch"]) is not int
        ):
            return False
        proof = marker["proof"]
        observation = proof["observation"]
        if marker["reason"] == "observed_new_start":
            return (
                observation["verdict"] == "observed_new_start"
                and observation["previous_state"] in {"IDLE", "FINISH", "FAILED"}
                and type(observation["generation"]) is int
                and observation["generation"] > 0
            )
        if marker["reason"] != "different_run_id":
            return False
        if not (
            run_id(proof["old_id"]) and run_id(proof["live_id"]) and run_id(proof["old_id"]) != run_id(proof["live_id"])
        ):
            return False
        return observation == {"catch_up": True} or (
            observation["verdict"] in {"different_client_run_id", "observed_new_start"}
            and type(observation["generation"]) is int
            and observation["generation"] > 0
            and run_id(observation["current_id"]) == run_id(proof["live_id"])
            and (
                observation["verdict"] == "observed_new_start"
                or (
                    run_id(observation["previous_id"])
                    and run_id(observation["previous_id"]) != run_id(observation["current_id"])
                )
            )
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


async def close_noncurrent(db, archive):
    """Only its own claims/header. Caller owns archive/lanes/writer and commit."""
    from backend.app.services.queue_counters import update_queue_counters

    if not valid_noncurrent(archive):
        raise ExecutionChanged("invalid noncurrent proof")
    rows = list(
        (
            await db.scalars(
                select(PrintQueueItem)
                .where(PrintQueueItem.archive_id == archive.id, PrintQueueItem.status == "printing")
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).all()
    )
    if any(row.queue_id != archive.printer_id for row in rows):
        raise ExecutionChanged("foreign claim")
    now = datetime.now(timezone.utc)
    extra = dict(archive.extra_data)
    previous = extra.get("noncurrent_close")
    if archive.status == "printing":
        archive.status = "cancelled"
        archive.completed_at = now
    elif archive.status != "cancelled" or not extra.get("recovered_outcome_uncertain"):
        raise ExecutionChanged("already terminal")
    extra["recovered_outcome_uncertain"] = True
    extra["noncurrent_close"] = previous or {
        "decision_identity": extra[NONCURRENT]["decision_identity"],
        "archive_identity": archive.recovery_identity,
        "at": now.isoformat(),
        "rows": [{"id": r.id, "identity": r.recovery_identity} for r in rows],
    }
    archive.extra_data = extra
    for row in rows:
        row.status, row.completed_at, row.waiting_reason = "cancelled", now, None
        row.error_code = "noncurrent_execution_claim"
        # Display uses the shared localized code catalogue.
        row.error_message = None
    queue = await db.get(PrinterQueue, archive.printer_id, populate_existing=True)
    if queue:
        queue.is_paused = True
        if queue.current_item_id in {r.id for r in rows}:
            queue.current_item_id, queue.status = None, "paused"
        await db.flush()
        await update_queue_counters(db, queue.id)


async def _check_candidates(db, live, decision):
    """An unaccepted attempt may be closed by its own dispatcher, never accepted silently."""
    attempts = {row[0] for row in decision.attempts}
    expected = set(decision.old)
    if decision.current_id is not None and decision.current_status == "printing":
        expected.add((decision.current_id, decision.current_identity, decision.current_run_id))
    if {(a.id, a.recovery_identity, durable_id(a)) for a in live if a.id not in attempts} != expected:
        raise ExecutionChanged("candidate set changed")
    for aid, identity, rid in decision.attempts:
        attempt = await db.get(PrintArchive, aid, populate_existing=True, with_for_update=True)
        if attempt is not None and (
            attempt.recovery_identity != identity
            or durable_id(attempt) != rid
            or attempt.dispatch_phase not in {"prepared", "sending", "sent"}
        ):
            raise ExecutionChanged("dispatch attempt was accepted or replaced")


async def recheck_start(db, printer_id, decision, *, still_current):
    """Refuse a changed proof before admission can run start effects."""
    from backend.app.services.queue_ops import claim_lanes

    await db.commit()
    ids = [row[0] for row in (*decision.old, *decision.attempts)]
    if decision.current_id is not None:
        ids.append(decision.current_id)
    async with queue_sources.storage_mutation(), claim_lanes(db, [printer_id], archive_ids=ids):
        if decision.epoch != queue_sources.current_epoch() or not still_current():
            raise ExecutionChanged("stale start")
        live = list(
            (
                await db.scalars(
                    select(PrintArchive)
                    .where(
                        PrintArchive.printer_id == printer_id,
                        PrintArchive.status == "printing",
                        PrintArchive.deleted_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            ).all()
        )
        await _check_candidates(db, live, decision)
        if decision.current_id is not None:
            current = await db.get(PrintArchive, decision.current_id, populate_existing=True, with_for_update=True)
            if (
                current is None
                or current.recovery_identity != decision.current_identity
                or current.status != decision.current_status
                or durable_id(current) != decision.current_run_id
            ):
                raise ExecutionChanged("adopted execution changed")
        if await db.scalar(
            select(PrintQueueItem.id)
            .where(
                PrintQueueItem.archive_id.in_([row[0] for row in decision.old]),
                PrintQueueItem.status == "printing",
                PrintQueueItem.queue_id != printer_id,
            )
            .limit(1)
        ):
            raise ExecutionChanged("foreign claim")
        await db.commit()


async def publish_start(db, archive, decision, *, options=None, still_current=None):
    """The FIRST visible archive commit includes all old markers and closes."""
    from backend.app.services.print_claims import observe_claim
    from backend.app.services.queue_ops import claim_lanes

    # Release only the preflight read snapshot, before taking process guards.
    await db.commit()
    async with (
        queue_sources.storage_mutation(),
        claim_lanes(
            db,
            [archive.printer_id],
            archive_ids=[r[0] for r in (*decision.old, *decision.attempts)]
            + ([decision.current_id] if decision.current_id else []),
        ),
    ):
        if decision.epoch != queue_sources.current_epoch() or (still_current and not still_current()):
            raise ExecutionChanged("stale start")
        all_live = list(
            (
                await db.scalars(
                    select(PrintArchive)
                    .where(
                        PrintArchive.printer_id == archive.printer_id,
                        PrintArchive.status == "printing",
                        PrintArchive.deleted_at.is_(None),
                    )
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
            ).all()
        )
        await _check_candidates(db, all_live, decision)
        if decision.current_id is not None:
            current = await db.get(PrintArchive, decision.current_id, populate_existing=True, with_for_update=True)
            if (
                current is None
                or current.id != archive.id
                or current.recovery_identity != decision.current_identity
                or current.status != decision.current_status
                or durable_id(current) != decision.current_run_id
            ):
                raise ExecutionChanged("adopted execution changed")
            archive = current
            if archive.status == "failed":
                archive.status, archive.started_at = "printing", datetime.now(timezone.utc)
        else:
            db.add(archive)
            await db.flush()
        all_live = [a for a in all_live if a.id in {old[0] for old in decision.old}]
        decision_id = str(uuid4())
        for old in all_live:
            old.extra_data = {
                **(old.extra_data or {}),
                NONCURRENT: {
                    "version": 1,
                    "archive_identity": old.recovery_identity,
                    "current_archive_identity": archive.recovery_identity,
                    "current_archive_id": archive.id,
                    "reason": decision.reason,
                    "decision_identity": decision_id,
                    "storage_epoch": decision.epoch,
                    "printer_lifetime": decision.printer_lifetime,
                    "proof": {
                        "observation": asdict(decision.observation) if decision.observation else {"catch_up": True},
                        "old_id": durable_id(old),
                        "live_id": run_id(archive.subtask_id) or decision.current_run_id,
                    },
                },
            }
            await close_noncurrent(db, old)
        claim = await observe_claim(db, archive.printer_id, archive_id=archive.id, options=options)
        if not claim or (still_current and not still_current()) or decision.epoch != queue_sources.current_epoch():
            raise ExecutionChanged("claim/start changed")
        published_id, published_identity = archive.id, archive.recovery_identity
        try:
            await db.commit()
        except Exception:
            # A commit acknowledgement can be lost after durability. Never
            # leave the old runtime binding when the new receipt actually won.
            await db.rollback()
            from backend.app.core import database

            async with database.async_session() as verify:
                current = await verify.get(PrintArchive, published_id)
                row = await verify.get(PrintQueueItem, claim.item_id)
                committed = (
                    current is not None
                    and current.recovery_identity == published_identity
                    and row is not None
                    and row.recovery_identity == claim.item_identity
                    and row.archive_id == published_id
                )
                for old_id, old_identity, _ in decision.old:
                    old = await verify.get(PrintArchive, old_id)
                    committed = committed and old is not None and old.recovery_identity == old_identity
                    if committed:
                        marker = (old.extra_data or {}).get(NONCURRENT, {})
                        committed = valid_noncurrent(old) and marker.get("decision_identity") == decision_id
                if not committed:
                    raise ExecutionChanged("publication not confirmed")
        return StartPublication(
            claim.state,
            claim.item_id,
            claim.item_identity,
            claim.archive_identity,
            current=decision.epoch == queue_sources.current_epoch() and (still_current is None or still_current()),
        )
