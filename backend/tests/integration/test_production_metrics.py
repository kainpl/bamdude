"""Production exporter reads the real ledgers without changing production."""

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import delete, event, select

from backend.app.api.routes.metrics import format_labels
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project_line import ProjectLine
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import order_metrics, production_metrics
from backend.tests.integration.test_workshop_stock_plan_regressions import archive, shop  # noqa: F401

pytestmark = pytest.mark.integration


def capture():
    samples = {}

    def emit(name, help_text, value, **labels):
        key = name, tuple(sorted(labels.items()))
        assert key not in samples, f"duplicate series: {key}"
        samples[key] = value

    def get(name, **labels):
        return samples[name, tuple(sorted(labels.items()))]

    return emit, get, samples


async def test_stock_and_rolling_usage_match_existing_accounting(db_session, printer_factory):
    printer = await printer_factory()
    now = datetime(2026, 11, 24, 18)
    spools = [
        Spool(material="PETG", label_weight=3000, weight_used=0),
        Spool(material="PETG", label_weight=3000, weight_used=0),
        Spool(material="PETG", label_weight=3000, weight_used=4000),
        Spool(material="PETG", label_weight=3000, weight_used=400, archived_at=now),
    ]
    db_session.add_all(spools)
    await db_session.flush()
    db_session.add(SpoolAssignment(spool_id=spools[2].id, printer_id=printer.id, ams_id=255, tray_id=0))
    for age, grams, status in (
        (0, 100, "completed"),
        (2, 30, "failed"),
        (8, 50, "legacy"),
        (31, 999, "completed"),
        (-1, 888, "completed"),
        (0, -15, "ams_sync"),
        (0, 20, "runout"),
    ):
        db_session.add(
            SpoolUsageHistory(
                spool_id=spools[2].id,
                printer_id=printer.id,
                weight_used=grams,
                status=status,
                created_at=now - timedelta(days=age),
            )
        )
    await db_session.commit()
    emit, get, _ = capture()
    await production_metrics.stock_metrics(db_session, emit)
    await production_metrics.usage_metrics(db_session, emit, now)
    assert get("stock_full_spools") == 2
    assert get("stock_full_grams") == 6000
    assert get("inventory_remaining_grams") == 6000
    assert get("inventory_overdrawn_spools") == 1
    assert get("assigned_spool_balance_grams", printer_id=str(printer.id), ams_id="255", tray_id="0") == -1000
    for window, expected in (("24h", 100), ("7d", 130), ("30d", 180)):
        assert (
            get("accounted_filament_window_grams", printer_id=str(printer.id), kind="print", window=window) == expected
        )
    assert (
        get("accounted_filament_window_grams", printer_id=str(printer.id), kind="service_adjustment", window="24h") == 5
    )
    assert get("accounted_filament_window_grams", printer_id="unassigned", kind="print", window="24h") == 0


async def test_orders_preserve_qc_recounts_ui_units_and_do_not_write(db_session, committing_client, shop):
    checked, _ = await archive(db_session, shop, status="completed", quantities=(12, 4), defects=(3, 1))
    checked.outcome_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await archive(db_session, shop, status="completed", quantities=(6, 2))
    await archive(db_session, shop, status="failed", quantities=(99, 99), defects=(2, 0))
    await archive(db_session, shop, status="printing", quantities=(3, 1))
    line = await db_session.get(ProjectLine, shop["line"])
    line.part_count_adjustments = {str(shop["a"].id): -1}
    await db_session.commit()
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            statements.append(statement)

    emit, get, _ = capture()
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe)
    try:
        await production_metrics.collect_production_metrics(
            db_session, emit, SimpleNamespace(get_client=lambda _: None)
        )
        assert not db_session.dirty
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert statements == []
    labels = {"order_id": str(shop["order"]), "line_id": str(shop["line"]), "part_id": str(shop["a"].id)}
    for quantity, expected in (("checked_net", 9), ("unchecked_net", 6), ("recount_adjustment", -1), ("ui_usable", 14)):
        assert get("order_part_pieces", **labels, quantity=quantity) == expected
    assert get("order_recorded_defective_pieces", order_id=str(shop["order"])) == 6
    assert get("order_unchecked_archives", order_id=str(shop["order"])) == 1
    ui = (await committing_client.get(f"/api/v1/projects/{shop['order']}")).json()
    for quantity, expected in (
        ("ui_printed", ui["lines"][0]["units_printed"]),
        ("ui_remaining", ui["figures"]["remaining"]),
    ):
        assert (
            get(
                "order_line_units",
                order_id=str(shop["order"]),
                line_id=str(shop["line"]),
                unit="product_units",
                quantity=quantity,
            )
            == expected
        )


async def test_pooled_quality_is_not_reattributed_twice(db_session, shop, monkeypatch):
    db_session.add(ProjectLine(project_id=shop["order"], product_id=shop["a"].product_id, quantity=10))
    await db_session.flush()
    first, _ = await archive(db_session, shop, status="completed", quantities=(30, 10))
    second, _ = await archive(db_session, shop, status="completed", quantities=(6, 2))
    first.project_line_id = second.project_line_id = None
    second.outcome_checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.commit()
    traced = Mock(wraps=order_metrics.attribute)
    monkeypatch.setattr(order_metrics, "attribute", traced)
    emit, _, samples = capture()
    await production_metrics.order_metrics_snapshot(db_session, emit)
    assert traced.call_count == 1
    assert (
        sum(
            v
            for (name, labels), v in samples.items()
            if name == "order_part_pieces" and dict(labels)["quantity"] in ("checked_net", "unchecked_net")
        )
        == 48
    )


async def test_missing_part_rows_are_unknown_and_reader_limit_omits_details(db_session, shop, monkeypatch):
    row, _ = await archive(db_session, shop, status="completed", quantities=(3, 1))
    await db_session.execute(delete(PrintArchivePart).where(PrintArchivePart.archive_id == row.id))
    await db_session.commit()
    emit, get, _ = capture()
    await production_metrics.order_metrics_snapshot(db_session, emit)
    assert get("order_quality_coverage_complete", order_id=str(shop["order"])) == 0
    assert math.isnan(get("order_recorded_defective_pieces", order_id=str(shop["order"])))
    assert math.isnan(
        get(
            "order_part_pieces",
            order_id=str(shop["order"]),
            line_id=str(shop["line"]),
            part_id=str(shop["a"].id),
            quantity="checked_net",
        )
    )
    monkeypatch.setattr(production_metrics, "MAX_ARCHIVES", 0)
    emit, get, samples = capture()
    await production_metrics.order_metrics_snapshot(db_session, emit)
    assert get("order_details_available") == 0
    assert get("active_orders") == 1
    assert not any(name == "order_line_units" for name, _ in samples)


async def test_both_queue_tiers_pause_maintenance_and_unknown_telemetry(db_session, shop):
    queue = await db_session.scalar(select(PrinterQueue).where(PrinterQueue.printer_id == shop["printer"].id))
    queue.is_paused = True
    shop["printer"].is_active = False
    shop["printer"].awaiting_plate_clear = True
    item = PrintQueueItem(queue_id=queue.id, position=1, status="pending")
    db_session.add(item)
    await db_session.flush()
    db_session.add_all([AutoQueueItem(status="pending"), AutoQueueItem(status="assigned", assigned_to_item_id=item.id)])
    await db_session.commit()
    emit, get, samples = capture()
    await production_metrics.queue_metrics(
        db_session, emit, SimpleNamespace(get_client=lambda _: None), datetime(2026, 11, 24)
    )
    assert get("pending_jobs", tier="auto") == get("pending_jobs", tier="printer") == 1
    for field in ("printer_maintenance", "printer_queue_paused", "printer_awaiting_plate_clear"):
        assert get(field, printer_id=str(shop["printer"].id)) == 1
    assert get("printer_telemetry_timestamp_known", printer_id=str(shop["printer"].id)) == 0
    assert not any(name == "printer_telemetry_age_seconds" for name, _ in samples)


async def test_metrics_auth_escaping_types_and_no_secret_output(async_client, db_session, printer_factory):
    await printer_factory(name='quoted " printer \\ line\nbreak')
    await async_client.put("/api/v1/settings/", json={"prometheus_enabled": True, "prometheus_token": "scrape-token"})
    denied = await async_client.get("/api/v1/metrics", headers={"Authorization": "Bearer wrong"})
    assert denied.status_code == 401
    response = await async_client.get("/api/v1/metrics", headers={"Authorization": "Bearer scrape-token"})
    assert response.status_code == 200, response.text
    assert "scrape-token" not in response.text
    families = list(text_string_to_metric_families(response.text))
    assert len({family.name for family in families}) == len(families)
    identities = [
        (sample.name, tuple(sorted(sample.labels.items()))) for family in families for sample in family.samples
    ]
    assert len(identities) == len(set(identities))
    for name in (
        "prints_total",
        "printer_prints_total",
        "filament_used_grams",
        "print_time_seconds",
        "stock_full_spools",
        "accounted_filament_window_grams",
    ):
        assert f"# TYPE bamdude_{name} gauge" in response.text
    assert format_labels(name='a"b\\c\nd') == '{name="a\\"b\\\\c\\nd"}'
    token = await db_session.scalar(select(Settings).where(Settings.key == "prometheus_token"))
    token.value = ""
    await db_session.commit()
    assert (await async_client.get("/api/v1/metrics")).status_code == 503


async def test_current_order_links_and_freshness_are_explicit(db_session, shop):
    linked, _ = await archive(db_session, shop, status="printing", quantities=(3, 1))
    linked.printer_id = shop["printer"].id
    unknown, _ = await archive(db_session, shop, status="printing", quantities=(3, 1))
    unknown.printer_id = shop["printer"].id
    unknown.project_id = unknown.project_line_id = None
    await db_session.commit()
    now = datetime(2026, 11, 24)
    client = SimpleNamespace(status_received_at=now.replace(tzinfo=timezone.utc).timestamp() - 12)
    emit, get, _ = capture()
    await production_metrics.queue_metrics(db_session, emit, SimpleNamespace(get_client=lambda _: client), now)
    assert (
        get(
            "running_order_archives",
            printer_id=str(shop["printer"].id),
            order_id=str(shop["order"]),
            line_id=str(shop["line"]),
        )
        == 1
    )
    assert get("running_order_archives", printer_id=str(shop["printer"].id), order_id="unknown", line_id="unknown") == 1
    assert get("printer_telemetry_age_seconds", printer_id=str(shop["printer"].id)) == 12


async def test_unknown_quality_is_valid_prometheus_nan(committing_client, db_session, shop):
    row, _ = await archive(db_session, shop, status="completed", quantities=(3, 1))
    await db_session.execute(delete(PrintArchivePart).where(PrintArchivePart.archive_id == row.id))
    await db_session.commit()
    await committing_client.put(
        "/api/v1/settings/", json={"prometheus_enabled": True, "prometheus_token": "scrape-token"}
    )
    response = await committing_client.get("/api/v1/metrics", headers={"Authorization": "Bearer scrape-token"})
    assert response.status_code == 200
    families = list(text_string_to_metric_families(response.text))
    defects = next(f for f in families if f.name == "bamdude_order_recorded_defective_pieces")
    assert math.isnan(defects.samples[0].value)
