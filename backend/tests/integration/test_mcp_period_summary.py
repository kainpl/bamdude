"""Period facts use SQL scope, normal outcomes and explicit incomplete coverage."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import event, insert

from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.app.services.farm_diagnostics.access import DiagnosticError
from backend.app.services.farm_diagnostics.contracts import PeriodInput
from backend.app.services.queue_sources import current_epoch
from backend.app.services.statistics import outcomes
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import data, error, rpc

START = datetime(2026, 10, 25, tzinfo=timezone.utc)
READ = frozenset({"printers:read", "archives:read_all", "stats:read"})


def actor(scope=None):
    return DiagnosticAccess(
        1,
        1,
        READ,
        False,
        scope,
        False,
        current_epoch(),
        key_identity="00000000-0000-0000-0000-000000000001",
        owner_identity="00000000-0000-0000-0000-000000000002",
    )


def args(**kw):
    return PeriodInput.model_validate({"from": START, "to": START + timedelta(days=1), "timezone": "Europe/Kyiv", **kw})


async def summary(db, access=None, request=None):
    from backend.app.services.farm_diagnostics.summary import period_summary

    return await period_summary(db, access or actor(), request or args(), "period-test")


async def test_period_outcomes_match_ui_and_exclude_trash_and_sources(db_session, printer_factory, archive_factory):
    p = await printer_factory()
    # Keep excluded rows below the ID ceiling; otherwise a broken counted()
    # filter could accidentally pass because the ceiling hides the fixtures.
    await archive_factory(p.id, status="archived", created_at=START)
    await archive_factory(p.id, deleted_at=START, created_at=START)
    for status, reason in [
        ("completed", None),
        ("failed", None),
        ("aborted", None),
        ("failed", "dispatchStartRefused"),
        ("stopped", None),
        ("cancelled", None),
        ("skipped", None),
        ("printing", None),
    ]:
        await archive_factory(p.id, status=status, failure_reason=reason, created_at=START)
    expected = await outcomes.outcome_counts(
        db_session, PrintArchive.created_at >= START, PrintArchive.created_at < START + timedelta(days=1)
    )
    result = await summary(db_session)
    c = result.data["totals"]["outcomes"]
    assert c == {
        "total": expected.total,
        "completed": expected.completed,
        "physical_failures": expected.physical_failures,
        "dispatch_failures": expected.dispatch_failures,
        "stopped": expected.stopped,
        "other": 1,
        "physical_outcome_denominator": expected.completed + expected.physical_failures,
        "success_rate_percent": expected.success_rate,
    }
    assert c["total"] == 8 and c["dispatch_failures"] == 1 and c["physical_failures"] == 2


async def test_period_duration_provenance_missing_invalid_and_overlap(db_session, printer_factory, archive_factory):
    p = await printer_factory()
    for begin, end, extra in [
        (0, 2, None),
        (1, 3, None),
        (4, 5, {"recovered_by_cleanup": True}),
        (6, 7, {"operator_recovery_end": {"source": "slicer_estimate"}}),
        (8, 9, {"recovered_by_startup_sweep": True}),
        (10, 11, {"operator_recovery_end": {}}),
    ]:
        await archive_factory(
            p.id,
            created_at=START,
            started_at=START + timedelta(hours=begin),
            completed_at=START + timedelta(hours=end),
            extra_data=extra,
        )
    await archive_factory(p.id, created_at=START, print_time_seconds=1800)
    await archive_factory(p.id, created_at=START, print_time_seconds=None)
    await archive_factory(p.id, created_at=START, started_at=START + timedelta(hours=2), completed_at=START)
    r = (await summary(db_session)).data
    t = r["totals"]["time"]
    assert t["measured_seconds"] == pytest.approx(14400, abs=0.01)
    assert t["estimated_seconds"] == pytest.approx(16200, abs=0.01)
    assert (t["measured_count"], t["estimated_count"], t["missing_count"], t["invalid_interval_count"]) == (2, 5, 1, 1)
    assert t["overlapping_interval_count"] == 1
    assert r["utilization"] is None and r["idle_seconds"] is None and r["oee"] is None
    assert "operator" not in str(r["totals"])


async def test_period_energy_status_cost_coverage_and_known_zero(db_session, printer_factory, archive_factory):
    p = await printer_factory()
    for kind, value, approximate in [
        ("measured", 0, False),
        ("measured", 0.2, True),
        ("estimated", 0.3, False),
        (None, 0.4, False),
        ("unavailable", 999, False),
        ("future", 999, False),
        (None, None, False),
    ]:
        extra = {"energy_provenance": {"kind": kind}} if kind else {}
        if approximate:
            extra["energy_is_approximate"] = True
        await archive_factory(p.id, created_at=START, energy_kwh=value, energy_cost=2, cost=5, extra_data=extra)
    m = (await summary(db_session)).data["totals"]
    assert m["energy"]["known_kwh"] == pytest.approx(0.9)
    assert m["energy"]["known_count"] == 4 and m["energy"]["missing_count"] == 3
    assert m["energy"]["by_status"]["measured"] == {"count": 1, "known_count": 1, "kwh": 0.0}
    assert m["energy"]["by_status"]["approximate"]["kwh"] == 0.2
    assert m["energy"]["by_status"]["estimated"]["kwh"] == 0.3
    assert m["energy"]["by_status"]["legacy"]["kwh"] == 0.4
    assert m["energy"]["known_energy_cost"] == 8 and m["recorded_cost"]["value"] == 35
    assert m["energy"]["cost_known_count"] == 4


async def test_period_scope_precedes_totals_and_printer_page(db_session, printer_factory, archive_factory):
    hidden = await printer_factory(name="hidden")
    visible = await printer_factory(name="visible")
    await archive_factory(hidden.id, created_at=START, energy_kwh=100)
    await archive_factory(visible.id, created_at=START, energy_kwh=1)
    r = await summary(db_session, actor(frozenset({visible.id})), args(limit=1))
    assert [p["printer_id"] for p in r.data["printers"]] == [visible.id]
    assert r.data["totals"]["outcomes"]["total"] == 1 and r.data["totals"]["energy"]["known_kwh"] == 1
    assert r.next_cursor is None and not r.coverage.truncated
    empty = await summary(db_session, actor(frozenset()), args(limit=1))
    assert empty.data["printers"] == [] and empty.data["totals"]["outcomes"]["total"] == 0
    assert empty.data["totals"]["energy"]["known_kwh"] is None and empty.next_cursor is None


async def test_period_explicit_subset_orphans_and_creation_not_completion(db_session, printer_factory, archive_factory):
    p1, p2 = await printer_factory(), await printer_factory()
    await archive_factory(p1.id, created_at=START, started_at=START, completed_at=START + timedelta(days=2), cost=0)
    await archive_factory(p1.id, created_at=START - timedelta(days=1), completed_at=START + timedelta(hours=1))
    await archive_factory(p2.id, created_at=START, cost=100)
    await archive_factory(None, created_at=START, cost=100)
    r = await summary(db_session, request=args(printer_ids=[p1.id]))
    assert r.data["totals"]["outcomes"]["total"] == 1
    assert r.data["totals"]["time"]["measured_seconds"] == 172800
    assert r.data["totals"]["recorded_cost"]["value"] == 0
    assert [p["printer_id"] for p in r.data["printers"]] == [p1.id]
    assert "orphan_archives_without_visible_printer_excluded" in r.coverage.missing


async def test_period_nested_overlaps_use_prior_maximum_end(db_session, printer_factory, archive_factory):
    p = await printer_factory()
    for begin, end in [(0, 10), (1, 2), (3, 4)]:
        await archive_factory(
            p.id, created_at=START, started_at=START + timedelta(hours=begin), completed_at=START + timedelta(hours=end)
        )
    r = await summary(db_session)
    assert r.data["totals"]["time"]["overlapping_interval_count"] == 2


@pytest.mark.parametrize("ids", [["hidden"], [999999], ["visible", "hidden"]])
async def test_period_explicit_foreign_or_missing_ids_refused_whole(db_session, printer_factory, ids):
    hidden = await printer_factory()
    visible = await printer_factory()
    resolved = [{"hidden": hidden.id, "visible": visible.id}.get(i, i) for i in ids]
    with pytest.raises(DiagnosticError, match="not_found"):
        await summary(db_session, actor(frozenset({visible.id})), args(printer_ids=resolved))


async def test_period_exact_offset_bounds_and_dst(db_session, printer_factory, archive_factory):
    p = await printer_factory()
    # Europe/Kyiv autumn transition: the same wall-clock hour occurs twice.
    begin = datetime.fromisoformat("2026-10-25T03:00:00+03:00")
    end = datetime.fromisoformat("2026-10-25T03:00:00+02:00")
    for moment in [begin - timedelta(seconds=1), begin, end - timedelta(seconds=1), end]:
        await archive_factory(p.id, created_at=moment.astimezone(timezone.utc).replace(tzinfo=None))
    r = (await summary(db_session, request=args(**{"from": begin, "to": end}))).data
    assert r["totals"]["outcomes"]["total"] == 2 and r["selection_basis"] == "archive.created_at"
    assert r["range"]["elapsed_seconds"] == 3600 and r["range"]["timezone"] == "Europe/Kyiv"
    assert r["range"]["bounds"] == "[from,to)"


async def test_period_cursor_full_totals_page_details_and_new_rows(db_session, printer_factory, archive_factory):
    p1, p2 = await printer_factory(), await printer_factory()
    for p in (p1, p2):
        await archive_factory(p.id, created_at=START)
    first = await summary(db_session, request=args(limit=1))
    assert len(first.data["printers"]) == 1 and first.next_cursor and first.coverage.truncated
    assert first.data["totals"]["outcomes"]["total"] == 2
    p3 = await printer_factory()
    await archive_factory(p3.id, created_at=START)
    await archive_factory(p2.id, created_at=START)
    second = await summary(db_session, request=args(limit=1, cursor=first.next_cursor))
    assert second.data["totals"] == first.data["totals"]
    assert [p["printer_id"] for p in second.data["printers"]] == [p2.id]
    assert second.next_cursor is None and second.data["pagination_is_snapshot"] is False
    for access, request in [
        (replace(actor(), key_id=2), args(limit=1, cursor=first.next_cursor)),
        (actor(), args(limit=1, timezone="UTC", cursor=first.next_cursor)),
    ]:
        with pytest.raises(DiagnosticError, match="invalid_cursor"):
            await summary(db_session, access, request)


@pytest.mark.parametrize("permission", sorted(READ))
async def test_period_each_permission_is_required(db_session, permission):
    with pytest.raises(DiagnosticError, match="forbidden"):
        await summary(db_session, replace(actor(), permissions=READ - {permission}))


async def test_period_restore_fences_results(db_session, printer_factory, monkeypatch):
    from backend.app.services.farm_diagnostics import summary as reader

    await printer_factory()
    epoch = current_epoch()
    monkeypatch.setattr(reader, "current_epoch", lambda: epoch + 1)
    with pytest.raises(DiagnosticError, match="state_changed"):
        await summary(db_session, replace(actor(), epoch=epoch))


async def test_period_bounded_sql_constant_queries_and_index_plan(db_session, test_engine, printer_factory, capsys):
    p = await printer_factory()
    captured = []

    def observed(conn, cursor, sql, params, context, many):
        if sql.lstrip().upper().startswith("SELECT"):
            captured.append((sql, params))

    counts = []
    for size in (3, 150):
        await db_session.execute(
            insert(PrintArchive),
            [
                {
                    "printer_id": p.id,
                    "filename": "x",
                    "file_path": "x",
                    "file_size": 1,
                    "created_at": START,
                    "status": "completed",
                    "extra_data": {},
                }
                for _ in range(size)
            ],
        )
        await db_session.commit()
        captured.clear()
        event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
        try:
            r = await summary(db_session, request=args(limit=1))
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", observed)
        counts.append(len(captured))
        assert len(r.data["printers"]) == 1 and len(db_session.identity_map) <= 1
    assert counts[0] == counts[1] and counts[1] <= 6
    assert any("sum(" in sql.lower() and "group by" in sql.lower() for sql, _ in captured)
    assert all("print_archives.filename" not in sql and "print_archives.file_path" not in sql for sql, _ in captured)
    # Record actual plans on both dialects, without requiring a planner to use an
    # index for a deliberately tiny fixture where a sequential scan is cheaper.
    conn = await db_session.connection()
    prefix = "EXPLAIN " if test_engine.dialect.name == "postgresql" else "EXPLAIN QUERY PLAN "
    sql, params = next((s, p) for s, p in captured if "sum(" in s.lower())
    plan = (await conn.exec_driver_sql(prefix + sql, params)).all()
    print("PERIOD_AGGREGATE_PLAN", test_engine.dialect.name, plan)
    assert plan


@pytest.mark.parametrize("modern", [False, True])
async def test_period_read_only_real_transport(
    async_client, runtime, db_session, test_engine, printer_factory, archive_factory, monkeypatch, modern
):  # noqa: F811
    from unittest.mock import AsyncMock

    from backend.app.api.routes import statistics
    from backend.app.services.statistics import energy

    forbidden = AsyncMock(side_effect=AssertionError("unscoped statistics or live device read"))
    monkeypatch.setattr(statistics, "get_archive_stats", forbidden)
    monkeypatch.setattr(energy, "sum_live_plug_totals", forbidden)
    p = await printer_factory()
    await archive_factory(p.id, created_at=START)
    created = await key(async_client, scope=[p.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        r = data(
            await rpc(
                http,
                url,
                created["key"],
                "bamdude_get_period_summary",
                args().model_dump(mode="json", by_alias=True),
                modern=modern,
            )
        )
        assert r["data"]["totals"]["outcomes"]["total"] == 1
        denied = await rpc(
            http,
            url,
            created["key"],
            "bamdude_get_period_summary",
            args(printer_ids=[999999]).model_dump(mode="json", by_alias=True),
            modern=modern,
        )
        assert error(denied) == "not_found"
    forbidden.assert_not_called()


@pytest.mark.parametrize("modern", [False, True])
async def test_period_maximum_page_fits_transport_and_resumes_without_skips(async_client, runtime, db_session, modern):  # noqa: F811
    await db_session.execute(
        insert(Printer),
        [
            {"name": f"p{i}", "serial_number": f"period-{i}", "ip_address": "127.0.0.1", "access_code": "test"}
            for i in range(100)
        ],
    )
    await db_session.commit()
    created = await key(async_client)
    ids, cursor = [], None
    async with live_app() as url, httpx.AsyncClient() as http:
        for _ in range(10):
            result = data(
                await rpc(
                    http,
                    url,
                    created["key"],
                    "bamdude_get_period_summary",
                    args(limit=100, cursor=cursor).model_dump(mode="json", by_alias=True),
                    modern=modern,
                )
            )
            ids.extend(p["printer_id"] for p in result["data"]["printers"])
            cursor = result["next_cursor"]
            if not cursor:
                break
    assert len(ids) == 100 and len(set(ids)) == 100 and ids == sorted(ids) and cursor is None
