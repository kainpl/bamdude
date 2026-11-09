"""Every authorization fact is required independently; dates never authorize a claim repair."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.app.services import swap_terminal
from backend.app.services.print_state_recovery import (
    RUNTIME_OWNERS,
    RecoveryFacts,
    classify,
    plan_action,
    runtime_effect_debt,
)


def facts():
    now = datetime.now(timezone.utc)
    return RecoveryFacts(
        1,
        {"id": 1, "printer_id": 1, "recovery_identity": str(uuid4()), "status": "idle", "current_item_id": 3},
        None,
        {
            "id": 2,
            "printer_id": 1,
            "queue_id": 1,
            "recovery_identity": str(uuid4()),
            "status": "completed",
            "extra_data": {},
            "started_at": now - timedelta(days=2),
            "completed_at": now - timedelta(days=1),
        },
        {"stable_inactive": True, "idle": True, "busy": False},
        (3, 4),
        (),
        members=tuple(
            {
                "id": i,
                "queue_id": 1,
                "archive_id": 2,
                "status": "printing",
                "recovery_identity": str(uuid4()),
                "started_at": now,
            }
            for i in (3, 4)
        ),
        repair_runtime={
            "owners": dict.fromkeys(RUNTIME_OWNERS, False),
            "resolution": False,
            "pending_terminal": False,
            "layer_timelapse": False,
            "finishing": 0,
            "current": None,
            "admission": None,
        },
        target_kind="header",
        now=now,
    )


def test_reverse_claim_times_do_not_block_cancellation():
    f = facts()
    assert classify(f)["actions"] == ["cancel_terminal_claims"]
    with pytest.raises(ValueError):
        plan_action(f, "cancel_terminal_claims")
    plan = plan_action(f, "cancel_terminal_claims", confirmed_idle=True)
    assert len(plan["members"]) == 2 and plan["archive"] == {}
    assert plan["queue"]["is_paused"] is True


@pytest.mark.parametrize("owner", RUNTIME_OWNERS)
@pytest.mark.parametrize("answer", [True, None])
def test_every_owner_must_positively_answer_false(owner, answer):
    f = facts()
    f = replace(f, repair_runtime={**f.repair_runtime, "owners": {**f.repair_runtime["owners"], owner: answer}})
    assert classify(f)["actions"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"members_complete": False},
        {"foreign_active_users": 1},
        {"printing_archive_ids": (99,)},
        {"references": ("project",)},
        {"live": {"idle": True, "stable_inactive": False}},
        {"live": {"stable_inactive": True, "idle": False}},
        {"live": {"stable_inactive": True, "idle": True, "busy": True}},
    ],
)
def test_group_refuses_partial_foreign_or_unobserved_facts(change):
    assert classify(replace(facts(), **change))["actions"] == []


@pytest.mark.parametrize(
    "extra",
    [
        {"recovered_outcome_uncertain": True},
        {"swap_hold": {}},
        {"swap_macro_events_pending": ["swap_mode_change_table"]},
        {"bamdude_terminal_effects": "unknown"},
    ],
)
def test_unknown_outcome_or_effects_never_authorize_cancel(extra):
    f = facts()
    f = replace(f, archive={**f.archive, "extra_data": extra})
    assert classify(f)["actions"] == []


@pytest.mark.parametrize("field", ["resolution", "pending_terminal", "layer_timelapse"])
def test_processing_and_layer_owners_veto(field):
    f = facts()
    assert classify(replace(f, repair_runtime={**f.repair_runtime, field: True}))["actions"] == []


def test_operator_settlement_is_addressed_and_does_not_hide_later_hold():
    from types import SimpleNamespace

    a = SimpleNamespace(
        recovery_identity=str(uuid4()),
        extra_data={
            "swap_hold": {"reason": "old"},
            "swap_start_effect": "fired",
            "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed"}},
            "swap_macro_events_pending": ["swap_mode_change_table"],
        },
    )
    swap_terminal.settle_by_operator(a, action_identity=str(uuid4()), actor="user:1", at=datetime.now(timezone.utc))
    assert a.extra_data["swap_hold"] == {"reason": "old"}
    assert a.extra_data["swap_start_effect"] == "fired"
    assert runtime_effect_debt(a.extra_data, a.recovery_identity) == []
    assert runtime_effect_debt(a.extra_data, str(uuid4()))
    a.extra_data["swap_hold"] = {"reason": "new"}
    assert runtime_effect_debt(a.extra_data, a.recovery_identity)


@pytest.mark.parametrize("stage", ["attempted", "failed", "finished", "unknown", None])
def test_only_owed_can_be_manually_settled(stage):
    from types import SimpleNamespace

    a = SimpleNamespace(
        recovery_identity=str(uuid4()),
        extra_data={
            "bamdude_terminal_effects": {"swap_change_table": {"stage": stage}},
            "swap_macro_events_pending": ["swap_mode_change_table"],
        },
    )
    before = a.extra_data
    with pytest.raises(ValueError):
        swap_terminal.settle_by_operator(a, action_identity=str(uuid4()), actor="user:1", at=datetime.now(timezone.utc))
    assert a.extra_data is before


def test_settlement_roundtrip_keeps_foreign_events_and_old_reader_veto(monkeypatch):
    import json
    from types import SimpleNamespace

    a = SimpleNamespace(
        recovery_identity=str(uuid4()),
        extra_data={
            "swap_hold": {"reason": "old"},
            "swap_start_effect": "fired",
            "swap_macro_events_pending": ["other-event", "swap_mode_change_table"],
            "bamdude_terminal_effects": {"swap_change_table": {"stage": "owed"}},
        },
    )
    swap_terminal.settle_by_operator(a, action_identity=str(uuid4()), actor="user:1", at=datetime.now(timezone.utc))
    payload = json.dumps(a.extra_data, sort_keys=True)
    restored = json.loads(payload)
    assert restored["swap_macro_events_pending"] == ["other-event"]
    assert restored["swap_start_effect"] == "fired" and restored["swap_hold"] == {"reason": "old"}
    assert swap_terminal.claim_stage(restored) not in swap_terminal.ANSWERED
    # Model the older reader: it cannot recognize this assertion as settlement.
    monkeypatch.setattr(swap_terminal, "operator_settlement", lambda *_args: None)
    assert runtime_effect_debt(restored, a.recovery_identity)
    assert json.dumps(restored, sort_keys=True) == payload


@pytest.mark.parametrize("kind", ["item", "archive"])
def test_group_cancellation_cannot_use_another_target_kind(kind):
    f = facts()
    f = replace(f, target_kind=kind, item=f.members[0] if kind == "item" else None)
    assert "cancel_terminal_claims" not in (classify(f) or {}).get("actions", [])
    with pytest.raises(ValueError):
        plan_action(f, "cancel_terminal_claims", confirmed_idle=True)
    if kind == "item":
        assert classify(f)["related"] == {"kind": "header", "target_id": f.queue["id"]}


def test_valid_singleton_keeps_ordinary_alignment_without_a_second_group_action():
    f = facts()
    member = {**f.members[0], "started_at": f.archive["started_at"]}
    f = replace(
        f,
        members=(member,),
        active_item_ids=(member["id"],),
        archive_users=1,
        live={**f.live, "mode": "inactive"},
        queue={**f.queue, "current_item_id": member["id"]},
    )
    assert classify(f) is None
    item = replace(f, target_kind="item", item=member)
    assert "align_terminal_item" in classify(item)["actions"]
    assert "cancel_terminal_claims" not in classify(item)["actions"]
