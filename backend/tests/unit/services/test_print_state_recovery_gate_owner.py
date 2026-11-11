"""The row a plate gate waits on, put back in the queue by an earlier Retry (spec plate-gate-owner-requeue B, T6).

Restoring it is offered only with full proof that the gate's archive is THIS row's run and that nothing
has used the row since; every veto is a reason, never a forced action. The gate is never part of the plan.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services.print_state_recovery import GATE_OWNER_ACTION, RecoveryFacts, classify, plan_action


def facts(**changes) -> RecoveryFacts:
    now = datetime.now(timezone.utc)
    claim = now - timedelta(hours=2)
    base = {
        "printer_id": 7,
        "queue": {
            "id": 7,
            "printer_id": 7,
            "recovery_identity": "q",
            "status": "paused",
            "current_item_id": None,
            "is_paused": True,
        },
        "item": {
            "id": 834,
            "queue_id": 7,
            "recovery_identity": "i",
            "status": "pending",
            "archive_id": 1184,
            "started_at": claim,
            "source_snapshot": None,
            "dispatch_attempts": 0,
        },
        "archive": {
            "id": 1184,
            "printer_id": 7,
            "queue_id": 7,
            "recovery_identity": "a",
            "status": "cancelled",
            "started_at": claim + timedelta(seconds=1),
            "completed_at": claim + timedelta(minutes=45),
            "deleted_at": None,
            "print_time_seconds": 8216,
            "extra_data": None,
        },
        "live": {"mode": "inactive", "identity": (1, "IDLE"), "observed_at": 10.0},
        "active_item_ids": (),
        "printing_archive_ids": (),
        "archive_users": 1,
        "references": ("receipt",),  # Repeat re-armed this row before: a receipt names it, which is fine here
        "now": now,
        "target_kind": "item",
        "gate": {"armed": True, "archive_id": 1184, "token": "gate-token"},
    }
    for path, value in changes.items():
        if "__" in path:
            part, field = path.split("__", 1)
            base[part] = {**base[part], field: value}
        else:
            base[path] = value
    return RecoveryFacts(**base)


@pytest.mark.parametrize(
    "outcome,row_status", [("cancelled", "cancelled"), ("failed", "failed"), ("aborted", "cancelled")]
)
def test_the_requeued_row_can_be_returned_to_its_runs_outcome(outcome, row_status):
    f = facts(archive__status=outcome)
    issue = classify(f)
    assert issue == {"code": "plate_gate_owner_requeued", "actions": [GATE_OWNER_ACTION], "blocked_reasons": []}
    plan = plan_action(f, GATE_OWNER_ACTION)
    assert plan == {
        "item": {"status": row_status, "completed_at": f.archive["completed_at"], "waiting_reason": None},
        "archive": {},
        "queue": {},
        "delete_item": False,
    }


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"queue__recovery_identity": None}, "identity_missing"),
        ({"item__recovery_identity": None}, "identity_missing"),
        ({"archive__recovery_identity": None}, "identity_missing"),
        ({"archive": None}, "execution_unproven"),
        ({"archive__printer_id": 8}, "execution_unproven"),
        ({"archive__queue_id": 9}, "execution_unproven"),
        ({"archive__deleted_at": datetime(2026, 11, 11, tzinfo=timezone.utc)}, "execution_unproven"),
        ({"archive__status": "printing"}, "outcome_unknown"),
        ({"item__source_snapshot": {"version": 1, "provenance": {"kind": "archive", "id": 1184}}}, "source_archive"),
        ({"item__started_at": None}, "invalid_run_times"),
        ({"archive__started_at": None}, "invalid_run_times"),
        ({"item__started_at": datetime.now(timezone.utc)}, "invalid_run_times"),  # the archive began before its claim
        ({"archive__completed_at": None}, "missing_run_times"),
        ({"item__dispatch_attempts": 1}, "dispatch_started"),
        ({"item__dispatch_attempts": None}, "dispatch_started"),
        ({"queue__status": "printing"}, "ambiguous_owner"),
        ({"queue__current_item_id": 834}, "ambiguous_owner"),
        ({"active_item_ids": (900,)}, "ambiguous_owner"),
        ({"printing_archive_ids": (0,)}, "ambiguous_owner"),
        ({"live": {"mode": "inactive", "busy": True, "identity": (1,), "observed_at": 1.0}}, "live_busy"),
        ({"live": {"mode": "unknown", "identity": (1,), "observed_at": 1.0}}, "live_unknown"),
        ({"live": {"mode": "successor", "identity": (1,), "observed_at": 1.0}}, "live_unknown"),
        ({"live": {"mode": "inactive", "effects": True, "identity": (1,), "observed_at": 1.0}}, "effects_pending"),
        ({"archive__extra_data": {"swap_macro_events_pending": ["swap_mode_change_table"]}}, "effects_pending"),
    ],
)
def test_every_missing_proof_is_a_reason_and_no_action(changes, reason):
    f = facts(**changes)
    issue = classify(f)
    assert issue["code"] == "plate_gate_owner_requeued"
    assert issue["actions"] == []
    assert reason in issue["blocked_reasons"]
    with pytest.raises(ValueError, match="recovery_not_allowed"):
        plan_action(f, GATE_OWNER_ACTION)


@pytest.mark.parametrize(
    "changes",
    [
        {"gate": {"armed": False, "archive_id": None, "token": None}},
        {"gate": {"armed": True, "archive_id": 999, "token": "other"}},
        {"gate": None},
        {"item__status": "cancelled"},
        {"item__archive_id": None},
        {"target_kind": "header"},
    ],
)
def test_without_a_gate_waiting_on_this_pending_row_there_is_no_such_issue(changes):
    issue = classify(facts(**changes))
    assert issue is None or issue["code"] != "plate_gate_owner_requeued"
    if issue is None:
        with pytest.raises(ValueError, match="recovery_not_allowed"):
            plan_action(facts(**changes), GATE_OWNER_ACTION)
