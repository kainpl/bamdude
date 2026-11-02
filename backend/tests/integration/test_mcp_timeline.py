"""Real MCP requests for archive identity, retained ancestry and gated evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import event as sql_event, select

from backend.app.models.print_state_recovery_action import PrintStateRecoveryAction
from backend.app.models.print_usage_event import PrintUsageEvent
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services.farm_diagnostics.contracts import TimelineInput
from backend.app.services.farm_diagnostics.timeline import timeline
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import data, error, queued, rpc


@pytest.mark.parametrize("modern", [False, True])
async def test_timeline_positive_identity_scope_and_soft_delete(
    async_client, runtime, printer_factory, archive_factory, db_session, modern
):  # noqa: F811
    a, b = await printer_factory(), await printer_factory()
    now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    own = await archive_factory(
        a.id,
        started_at=now,
        completed_at=now + timedelta(seconds=2),
        extra_data={"operator_recovery_end": {"source": "slicer_estimate"}},
    )
    foreign = await archive_factory(b.id, started_at=now)
    same_printer_other_run = await archive_factory(a.id, started_at=now)
    assert own.filename == foreign.filename
    db_session.add_all(
        [
            PrintUsageEvent(printer_id=p, archive_id=arc, layer_num=1, event="runout", kind="pause", created_at=now)
            for p, arc in [(a.id, own.id), (b.id, foreign.id), (a.id, same_printer_other_run.id)]
        ]
    )
    await db_session.commit()
    scoped = await key(async_client, scope=[a.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        page = data(
            await rpc(http, url, scoped["key"], "bamdude_get_print_timeline", {"archive_id": own.id}, modern=modern)
        )
        assert page["data"]["archive_id"] == own.id
        usage = [e for e in page["data"]["events"] if e["source"] == "usage"]
        assert len(usage) == 1
        assert any(e["source"] == "archive.end" and e["basis"] == "slicer_estimate" for e in page["data"]["events"])
        assert not page["coverage"]["complete"]
        for hidden in (foreign.id, 999999):
            assert (
                error(await rpc(http, url, scoped["key"], "bamdude_get_print_timeline", {"archive_id": hidden}))
                == "not_found"
            )
        own.deleted_at = now
        await db_session.commit()
        assert (
            error(await rpc(http, url, scoped["key"], "bamdude_get_print_timeline", {"archive_id": own.id}))
            == "not_found"
        )


async def test_timeline_filters_stale_refills_before_from_and_page(
    async_client, runtime, printer_factory, archive_factory, db_session
):  # noqa: F811
    p = await printer_factory()
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    archive = await archive_factory(p.id, created_at=at)
    events = []
    # The backup, replacement, and repeated runout straddle from/page boundaries.
    facts = [
        ("runout", "autoswitch", 0, 1, True),
        ("tray_change", None, 1, 3, True),
        ("spool_loaded", None, 0, 2, True),
        ("runout", "autoswitch", 0, 2, True),
        ("tray_change", None, 0, 2, True),
        ("runout", "autoswitch", 0, 2, True),
    ]
    for index, (event, kind, tray, spool, occupied) in enumerate(facts):
        row = PrintUsageEvent(
            printer_id=p.id,
            archive_id=archive.id,
            event=event,
            kind=kind,
            global_tray_id=tray,
            spool_id=spool,
            slot_occupied=occupied,
            layer_num=10,
            created_at=at + timedelta(seconds=index),
        )
        db_session.add(row)
        events.append(row)
    await db_session.commit()
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        query = {
            "archive_id": archive.id,
            "from": (at + timedelta(seconds=2)).replace(tzinfo=timezone.utc).isoformat(),
            "limit": 1,
        }
        ids = []
        while True:
            result = data(await rpc(http, url, created["key"], "bamdude_get_print_timeline", query))
            ids.extend(e["source_id"] for e in result["data"]["events"] if e["source"] == "usage")
            if not result["next_cursor"]:
                break
            query["cursor"] = result["next_cursor"]
        assert str(events[3].id) not in ids
        assert str(events[5].id) in ids  # observed real feed return admits the next runout
        assert (await db_session.scalars(select(PrintUsageEvent.id))).all() == [row.id for row in events]


async def test_timeline_ties_upper_bound_and_inventory_masking(
    db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.mcp_server.contracts import DiagnosticAccess
    from backend.app.services import print_usage_journal
    from backend.app.services.queue_sources import current_epoch

    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=2)
    p = await printer_factory()
    archive = await archive_factory(p.id, created_at=at)
    for _ in range(3):
        db_session.add(
            PrintUsageEvent(
                printer_id=p.id, archive_id=archive.id, layer_num=1, event="start", created_at=at, spool_id=17
            )
        )
    await db_session.commit()
    access = DiagnosticAccess(
        1, 1, frozenset({"printers:read", "archives:read_all"}), False, None, False, current_epoch()
    )
    blocked = Mock(side_effect=AssertionError("journal writer"))
    monkeypatch.setattr(print_usage_journal, "record_runout", blocked)
    args = TimelineInput(archive_id=archive.id, limit=1)
    first = await timeline(db_session, access, args, "test")
    assert first.next_cursor
    db_session.add(
        PrintUsageEvent(printer_id=p.id, archive_id=archive.id, layer_num=2, event="runout", created_at=at, spool_id=99)
    )
    await db_session.commit()
    seen, token = first.data["events"], first.next_cursor
    while token:
        page = await timeline(db_session, access, args.model_copy(update={"cursor": token}), "test")
        seen += page.data["events"]
        token = page.next_cursor
    assert len({row["evidence_id"] for row in seen}) == len(seen) == 4
    assert all("spool_id" not in row for row in seen)
    assert "inventory_identity_and_amounts_unavailable_for_actor" in first.coverage.missing
    blocked.assert_not_called()
    with pytest.raises(Exception, match="invalid_cursor"):
        await timeline(
            db_session,
            replace(access, epoch=access.epoch + 1),
            args.model_copy(update={"cursor": first.next_cursor}),
            "test",
        )


async def test_timeline_queue_audit_usage_and_unknown_feed_are_archive_owned(
    db_session, printer_factory, archive_factory
):
    from backend.app.mcp_server.contracts import DiagnosticAccess
    from backend.app.models.spool import Spool
    from backend.app.services.queue_sources import current_epoch
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    p, foreign = await printer_factory(), await printer_factory()
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    archive = await archive_factory(p.id, created_at=at)
    _, own_item = await queued(db_session, p)
    _, wrong_item = await queued(db_session, foreign)
    own_item.archive_id = wrong_item.archive_id = archive.id
    own_item.created_at = wrong_item.created_at = at
    spool = Spool(material="PLA")
    db_session.add(spool)
    await db_session.flush()
    db_session.add_all(
        [
            PrintStateRecoveryAction(
                id="known-audit",
                created_at=at,
                actor="SECRET actor",
                actor_label="SECRET actor",
                printer_id=p.id,
                queue_id=own_item.queue_id,
                item_id=own_item.id,
                archive_id=archive.id,
                action="align_terminal_item",
                input_hash="hash",
                result={"SECRET": "must never serialize"},
            ),
            SpoolUsageHistory(spool_id=spool.id, printer_id=p.id, archive_id=archive.id, created_at=at, weight_used=18),
            PrintUsageEvent(
                printer_id=p.id,
                archive_id=archive.id,
                created_at=at,
                layer_num=8,
                event="tray_change",
                global_tray_id=UNKNOWN_FEED,
            ),
            PrintUsageEvent(
                printer_id=p.id,
                archive_id=archive.id,
                created_at=at,
                layer_num=8,
                event="tray_change",
                global_tray_id=255,
            ),
        ]
    )
    await db_session.commit()
    access = DiagnosticAccess(1, 1, frozenset(), True, None, False, current_epoch())
    result = await timeline(db_session, access, TimelineInput(archive_id=archive.id), "audit")
    entries = result.data["events"]
    assert [e["source_id"] for e in entries if e["source"].startswith("queue.")] == [str(own_item.id)]
    assert any(e.get("action") == "align_terminal_item" for e in entries)
    assert any(e.get("grams") == 18 and e.get("spool_id") == spool.id for e in entries)
    assert [e["feed_unknown"] for e in entries if e["source"] == "usage"] == [True, False]
    assert "SECRET" not in result.model_dump_json()


async def test_timeline_query_count_is_constant_and_ancestry_is_bounded(
    db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.mcp_server.contracts import DiagnosticAccess
    from backend.app.services.farm_diagnostics import timeline as module
    from backend.app.services.queue_sources import current_epoch

    p = await printer_factory()
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    archive = await archive_factory(p.id, created_at=at)
    access = DiagnosticAccess(
        1, 1, frozenset({"printers:read", "archives:read_all"}), False, None, False, current_epoch()
    )
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    async def measure(amount):
        db_session.add_all(
            [
                PrintUsageEvent(printer_id=p.id, archive_id=archive.id, created_at=at, layer_num=1, event="start")
                for _ in range(amount)
            ]
        )
        await db_session.commit()
        statements.clear()
        engine = db_session.bind.sync_engine
        sql_event.listen(engine, "before_cursor_execute", capture)
        try:
            value = await timeline(db_session, access, TimelineInput(archive_id=archive.id, limit=2), "bound")
            return value, len(statements)
        finally:
            sql_event.remove(engine, "before_cursor_execute", capture)

    _, small = await measure(3)
    _, large = await measure(30)
    assert small == large
    monkeypatch.setattr(module, "HISTORY_BUDGET", 2)
    limited = await timeline(db_session, access, TimelineInput(archive_id=archive.id), "cap")
    assert not any(row["source"] == "usage" for row in limited.data["events"])
    assert "usage_ancestry_budget_exceeded" in limited.coverage.missing


async def test_timeline_queue_and_recovery_require_read_all(db_session, printer_factory, archive_factory):
    from backend.app.mcp_server.contracts import DiagnosticAccess
    from backend.app.services.queue_sources import current_epoch

    printer = await printer_factory()
    at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    archive = await archive_factory(printer.id, created_at=at)
    queue, item = await queued(db_session, printer)
    item.archive_id, item.created_at = archive.id, at
    db_session.add(
        PrintStateRecoveryAction(
            id="queue-gate-audit",
            created_at=at,
            actor="operator",
            actor_label="operator",
            printer_id=printer.id,
            queue_id=queue.id,
            item_id=item.id,
            archive_id=archive.id,
            action="align_terminal_item",
            input_hash="hash",
            result={},
        )
    )
    await db_session.commit()
    access = DiagnosticAccess(
        1, 1, frozenset({"printers:read", "archives:read_all"}), False, None, False, current_epoch()
    )
    args = TimelineInput(archive_id=archive.id)
    limited = await timeline(db_session, access, args, "limited")
    assert all(
        not event["source"].startswith("queue.") and event["source"] != "recovery" for event in limited.data["events"]
    )
    assert "queue_evidence_unavailable_for_actor" in limited.coverage.missing
    allowed = await timeline(
        db_session, replace(access, permissions=access.permissions | {"queue:read_all"}), args, "allowed"
    )
    assert any(event["source"].startswith("queue.") for event in allowed.data["events"])
    assert any(event["source"] == "recovery" for event in allowed.data["events"])
    assert "queue_evidence_unavailable_for_actor" not in allowed.coverage.missing
