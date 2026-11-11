"""F1 display parity and async races, with committed patched archive metadata."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.app import main
from backend.app.api.routes import camwall, metrics, printers
from backend.app.services import monitor_snapshot
from backend.app.services.layer_projection import notify_current_print_source_ready, resolve_layer_pair
from backend.app.services.layer_source import write_layer_source
from backend.app.services.print_file_analysis import PrintFileAnalysis
from backend.app.services.print_run_binding import bind_prepared_print_run, claim_print_start
from backend.app.services.printer_manager import PrinterManager, printer_state_to_dict
from backend.app.services.usage_projection import compute_usage_projection
from backend.tests.integration.test_layer_source_writers import dispatch, storage  # noqa: F401 -- shared fixture
from backend.tests.unit.services.test_layer_projection import client, push


@pytest.fixture
async def live(db_session, printer_factory, storage, monkeypatch):
    p = await printer_factory()
    a, upload = await dispatch(db_session, p.id, storage)
    m, c = PrinterManager(), client()
    m._clients[p.id] = c
    for module in (main, printers, camwall, metrics, monitor_snapshot):
        monkeypatch.setattr(module, "printer_manager", m)
    monkeypatch.setattr(m, "get_status", lambda _: c.state)
    monkeypatch.setattr(m, "get_all_statuses", lambda: {p.id: c.state})
    monkeypatch.setattr(m, "peek_status", lambda _: (c.state, None, False))
    events = []
    c.on_print_start = events.append
    push(c, gcode_state="FINISH", subtask_id="100", layer_num=693, total_layer_num=693)
    push(c, gcode_state="IDLE", subtask_id="200")
    push(c, gcode_state="RUNNING", gcode_file="fixture.gcode", subtask_id="200", layer_num=0, mc_percent=10)
    data = events[-1]
    bind_prepared_print_run(
        m, printer_id=p.id, archive_id=a.id, archive_identity=a.recovery_identity, expected_submission_id="200"
    )
    assert resolve_layer_pair(m, p.id).source == "unknown"
    admission = claim_print_start(m, p.id, data)
    main._bind_print_file_analysis_context(
        p.id, a, expected_generation=admission.generation, origin="dispatch", observation=data["_print_observation"]
    )
    push(c, layer_num=14)
    assert resolve_layer_pair(m, p.id).total_layers == 693
    m._uses_print_file_analysis_service = False
    m._print_file_analysis_runner = lambda *_: PrintFileAnalysis(
        filament_usage=[{"slot_id": 1, "used_g": 6930}],
        # The extra zero/terminal sample is not the denominator.
        layer_usage={i: {} for i in range(694)},
        filament_properties={},
    )
    return p, a, m, c, upload


async def test_f01_f19_f22_all_surfaces_use_the_same_file_pair(live, db_session, monkeypatch):
    p, a, m, c, _ = live
    pair = resolve_layer_pair(m, p.id)
    # Single and batch use the same shaper, with and without preloaded ids.
    single = await printers._build_printer_status(p, db_session)
    batch = await printers._build_printer_status(p, db_session, archive_ids={p.id: a.id}, waiting_ids=set())
    overlay = await printers.get_overlay_status(p.id, db=db_session)
    wall = (await camwall.list_camwall_printers(db=db_session))[0]
    monkeypatch.setattr(
        monitor_snapshot.background_dispatch,
        "get_state",
        AsyncMock(return_value={"active_jobs": [], "dispatched_jobs": []}),
    )
    monitor = await monitor_snapshot.build_snapshot(db_session, "printers", monitor_snapshot.MonitorAccess())
    usage = await compute_usage_projection(db_session, p.id, m)
    ws = printer_state_to_dict(c.state, p.id, layer_pair=pair)
    for payload in (
        single.model_dump(),
        batch.model_dump(),
        overlay,
        wall,
        monitor.printers[0].model_dump(),
        usage,
        ws,
    ):
        assert (payload["layer_num"], payload["total_layers"], payload["total_layers_source"]) == (14, 693, "file")
    assert usage["slots"][0]["consumed_g"] == 140
    monkeypatch.setattr(metrics, "get_prometheus_settings", AsyncMock(return_value=(True, "")))
    response = await metrics.get_metrics(db=db_session, authorization=None)
    text = response.body.decode()
    assert any(line.startswith("bamdude_print_layer_total{") and line.endswith(" 693") for line in text.splitlines())
    assert c.state.total_layers == 0


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE"])
async def test_ri2_stable_unproven_file_retains_raw_filament_estimate(live, db_session, state):
    p, _, m, c, _ = live
    push(c, gcode_file="unproven.gcode", layer_num=15, total_layer_num=693)
    push(c, gcode_state=state)
    assert c._run_id_unresolved is True
    assert resolve_layer_pair(m, p.id).source == "mqtt"

    usage = await compute_usage_projection(db_session, p.id, m)

    assert usage["active"] is True
    assert (usage["layer_num"], usage["total_layers"], usage["total_layers_source"]) == (15, 693, "mqtt")
    assert usage["slots"][0]["consumed_g"] == 150


@pytest.mark.parametrize("mutation", ["source", "unproven", "boundary", "effect", "layer_only"])
async def test_f20_usage_rechecks_after_last_database_await(live, db_session, monkeypatch, mutation):
    from backend.app.services import print_usage_journal
    from backend.app.services.print_run_binding import revoke_print_effects

    p, a, m, c, _ = live
    entered, release = asyncio.Event(), asyncio.Event()
    original = print_usage_journal.load_events

    async def paused(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(print_usage_journal, "load_events", paused)
    task = asyncio.create_task(compute_usage_projection(db_session, p.id, m))
    await asyncio.wait_for(entered.wait(), 5)
    if mutation == "source":
        write_layer_source(a, [{"index": 1, "total_layers": 700}])
        notify_current_print_source_ready(m, p.id, a)
    elif mutation == "unproven":
        push(c, gcode_file="unproven.gcode")
    elif mutation == "boundary":
        push(c, subtask_id="300", gcode_file="B.gcode", layer_num=1)
    elif mutation == "effect":
        revoke_print_effects(m, p.id)
    else:
        push(c, layer_num=15)
    release.set()
    result = await task
    if mutation == "layer_only":
        assert result["layer_num"] == 15 and result["slots"][0]["consumed_g"] == 150
    else:
        assert result == {"active": False}


async def test_f07b_real_retry_source_ready_updates_an_authorized_grant(live, db_session, test_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import backend.app.services.printer_manager as manager_module
    from backend.app.services import archive_download_retry as retry

    p, a, m, c, upload = live
    a.file_path, a.extra_data = "", {}
    await db_session.commit()
    notify_current_print_source_ready(m, p.id, a)
    assert resolve_layer_pair(m, p.id).source == "unknown"
    monkeypatch.setattr(manager_module, "printer_manager", m)
    monkeypatch.setattr(retry, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(retry, "try_download_3mf", AsyncMock(return_value=(upload, "fixture.3mf")))
    monkeypatch.setattr(retry.ws_manager, "send_archive_updated", AsyncMock())
    assert await retry.archive_download_retry.retry_archive(a.id) == "recovered"
    assert resolve_layer_pair(m, p.id).total_layers == 693
    assert c.state.total_layers == 0


@pytest.mark.parametrize("unproven_during_await", [False, True])
async def test_f08_f10b_actual_exact_subtask_branch_uses_captured_revision(
    live, db_session, test_engine, monkeypatch, unproven_during_await
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.services.layer_projection import current_layer_grant
    from backend.app.services.print_run_binding import current_print_run

    p, a, _, c, _ = live
    a.subtask_id = "200"
    await db_session.commit()
    m = PrinterManager()
    m._clients[p.id] = c
    monkeypatch.setattr(main, "printer_manager", m)
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(main, "_expected_prints", {})
    monkeypatch.setattr(main, "_on_print_start_impl", AsyncMock(side_effect=AssertionError("not exact-subtask branch")))

    async def queue_binding(*_, **__):
        await asyncio.sleep(0)
        if unproven_during_await:
            push(c, gcode_file="unproven-during-bind.gcode")
        return True

    monkeypatch.setattr(main, "mark_queue_printing_for_printer", queue_binding)
    await main.on_print_start(
        p.id, {"filename": "fixture.gcode", "subtask_id": "200", "_print_observation": c._start_observation}
    )
    assert current_print_run(m, p.id).origin == "exact_subtask"
    assert current_layer_grant(m, p.id).address.revision == 0
    assert resolve_layer_pair(m, p.id).source == ("unknown" if unproven_during_await else "file")


async def test_f20_usage_fence_and_pair_read_the_same_completed_snapshot(live, db_session, monkeypatch):
    from backend.app.services import print_usage_journal

    p, _, m, c, _ = live
    armed = False
    original_events = print_usage_journal.load_events
    original_snapshot = c.get_layer_snapshot

    async def events(*args):
        nonlocal armed
        value = await original_events(*args)
        armed = True
        return value

    def snapshot():
        nonlocal armed
        value = original_snapshot()
        if armed:
            armed = False
            # A completed B lands immediately after the final reader copied A.
            push(c, subtask_id="300", gcode_file="B.gcode", layer_num=1, total_layer_num=42)
        return value

    monkeypatch.setattr(print_usage_journal, "load_events", events)
    monkeypatch.setattr(c, "get_layer_snapshot", snapshot)
    result = await compute_usage_projection(db_session, p.id, m)
    assert (result["layer_num"], result["total_layers"], result["slots"][0]["consumed_g"]) == (14, 693, 140)
    assert resolve_layer_pair(m, p.id).total_layers == 42


async def test_f22_actual_websocket_bootstrap_uses_file_projection(live, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import backend.app.services.printer_manager as manager_module
    from backend.app.api.routes import websocket as route
    from backend.app.core.websocket import ConnectionManager

    p, _, m, _, _ = live
    monkeypatch.setattr(route, "printer_manager", m)
    monkeypatch.setattr(manager_module, "printer_manager", m)
    monkeypatch.setattr(route, "ws_manager", ConnectionManager())
    monkeypatch.setattr(route, "authenticate_websocket_token", AsyncMock(return_value=(True, 7)))
    monkeypatch.setattr(route.background_dispatch, "get_state", AsyncMock(return_value={}))
    app = FastAPI()
    app.include_router(route.router)
    with TestClient(app) as http, http.websocket_connect("/ws?token=synthetic") as ws:
        payload = ws.receive_json()
        assert payload["printer_id"] == p.id
        assert (
            payload["data"]["layer_num"],
            payload["data"]["total_layers"],
            payload["data"]["total_layers_source"],
        ) == (14, 693, "file")
        assert ws.receive_json()["type"] == "initial_status_complete"
