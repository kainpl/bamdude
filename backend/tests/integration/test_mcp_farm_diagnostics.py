"""Real HTTP/SDK coverage of passive B1 readers, with scoped SQL hydration."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import event, select, update

from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.user import User
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401


async def rpc(http, url, token, name, arguments=None, *, modern=False):
    headers = {"Accept": "application/json, text/event-stream", "X-API-Key": token}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    if modern:
        headers.update({"MCP-Protocol-Version": "2026-07-28", "MCP-Method": "tools/call", "MCP-Name": name})
        payload["params"]["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    response = await http.post(url + "/mcp", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def data(result):
    assert not result.get("isError"), result
    return result["structuredContent"]


def error(result):
    assert result["isError"], result
    return json.loads(result["content"][0]["text"])["code"]


async def test_dispatch_monitor_projection_is_bounded_and_omits_other_jobs():
    from types import SimpleNamespace

    from backend.app.services.background_dispatch import BackgroundDispatchService

    dispatch = BackgroundDispatchService()
    dispatch._active_jobs = {
        i: SimpleNamespace(
            job=SimpleNamespace(printer_id=pid, source_name="SECRET"),
            phase="uploading",
            upload_total_bytes=100,
            upload_bytes=50,
        )
        for i, pid in enumerate([1, 1, 2])
    }
    dispatch._queued_jobs.extend(SimpleNamespace(printer_id=pid, source_name="SECRET") for pid in [1, 1, 2])
    state = await dispatch.get_monitor_state(frozenset({1}))
    assert state == {
        "active_jobs": [{"printer_id": 1, "phase": "uploading", "upload_progress_pct": 50}],
        "dispatched_jobs": [{"printer_id": 1}],
    }


@pytest.fixture
def telemetry(monkeypatch):
    state = PrinterState(connected=True, state="RUNNING", subtask_id="run-1", current_print="private active filename")
    received = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr(printer_manager, "peek_status", Mock(return_value=(state, received, False)))
    monkeypatch.setattr(printer_manager, "is_awaiting_plate_clear", lambda _: False)
    monkeypatch.setattr(printer_manager, "get_status", Mock(side_effect=AssertionError("active printer poll")))
    return state


async def queued(db, printer, *, printing=False, name="allowed job.3mf"):
    queue = PrinterQueue(id=printer.id + 1000, printer_id=printer.id, status="printing" if printing else "idle")
    file = LibraryFile(filename=name, file_path="private original path", file_size=1, file_type="gcode")
    db.add_all([queue, file])
    await db.flush()
    job = PrintQueueItem(
        queue_id=queue.id, library_file_id=file.id, status="printing" if printing else "pending", position=1
    )
    db.add(job)
    await db.flush()
    if printing:
        queue.current_item_id = job.id
    await db.commit()
    return queue, job


@pytest.mark.parametrize("modern", [False, True])
@pytest.mark.parametrize(
    "scenario",
    [
        "matched",
        "multiple_disagreeing_header",
        "multiple_matching_header",
        "single_empty_header",
        "single_other_header",
    ],
)
async def test_current_job_does_not_claim_identity_when_queue_rows_disagree(
    async_client, runtime, printer_factory, db_session, telemetry, modern, scenario
):  # noqa: F811
    from backend.app.services.monitor_snapshot import MonitorAccess, build_snapshot

    p = await printer_factory()
    queue, old = await queued(db_session, p, printing=True, name="legacy display row")
    multiple = scenario.startswith("multiple_")
    if multiple or scenario == "single_other_header":
        other = PrintQueueItem(queue_id=queue.id, status="printing" if multiple else "pending", position=2)
        db_session.add(other)
        await db_session.flush()
        if scenario != "multiple_matching_header":
            queue.current_item_id = other.id
    elif scenario == "single_empty_header":
        queue.current_item_id = None
    await db_session.commit()
    # Establish each branch's actual precondition using the real monitor, not
    # a fake DTO. Two printing rows can agree with its display choice; one
    # printing row can disagree with a valid same-queue or absent header.
    monitor = await build_snapshot(
        db_session, "queues", MonitorAccess(queue_read=True, read_all=True, printer_ids=frozenset({p.id}))
    )
    assert monitor.printers[0].current_job.item_id == old.id
    if scenario == "multiple_matching_header":
        assert queue.current_item_id == old.id
    elif scenario in {"single_empty_header", "single_other_header"}:
        assert queue.current_item_id != old.id
    created = await key(async_client, scope=[p.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        for tool, args in [
            ("bamdude_get_farm_overview", {}),
            ("bamdude_inspect_printer", {"printer_id": p.id}),
        ]:
            result = data(await rpc(http, url, created["key"], tool, args, modern=modern))
            facts = result["data"]["printers"][0] if "printers" in result["data"] else result["data"]
            warnings = [f for f in result["findings"] if f["code"] == "queue_current_job_ambiguous"]
            if scenario != "matched":
                assert facts["printer"]["current_job"] is None
                assert facts["claim"]["current_item_id"] == queue.current_item_id
                assert facts["claim"]["printing_row_count"] == (2 if multiple else 1)
                assert facts["claim"]["header_link_valid"] is (None if scenario == "single_empty_header" else True)
                assert not facts["coverage"]["complete"]
                assert "current_job_identity_unresolved" in facts["coverage"]["missing"]
                assert warnings and warnings[0]["certainty"] == "observed"
                assert warnings[0]["next_steps"][0]["action_kind"] == "open_ui"
            else:
                assert facts["printer"]["current_job"]["item_id"] == old.id
                assert not warnings


@pytest.mark.parametrize("modern", [False, True])
async def test_overview_inspect_and_scope_are_real_read_only_tools(
    async_client, runtime, printer_factory, db_session, telemetry, modern
):  # noqa: F811
    a = await printer_factory(name="visible A")
    b = await printer_factory(name="secret B")
    await queued(db_session, a, printing=True)
    await queued(db_session, b, name="secret B job")
    created = await key(async_client, scope=[a.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        overview = data(await rpc(http, url, created["key"], "bamdude_get_farm_overview", modern=modern))
        assert [row["printer"]["printer_id"] for row in overview["data"]["printers"]] == [a.id]
        assert "secret B" not in json.dumps(overview)
        assert overview["collection_started_at"] <= overview["collection_finished_at"]
        inspected = data(
            await rpc(http, url, created["key"], "bamdude_inspect_printer", {"printer_id": a.id}, modern=modern)
        )
        assert inspected["data"]["printer"]["current_job"]["name"] == "allowed job.3mf"
        assert inspected["findings"][0]["code"] == "printer_busy"
        assert inspected["data"]["camera"]["captured_at"] is None
        for target in (b.id, 999999):
            assert (
                error(
                    await rpc(
                        http, url, created["key"], "bamdude_inspect_printer", {"printer_id": target}, modern=modern
                    )
                )
                == "not_found"
            )
        assert error(await rpc(http, url, created["key"], "bamdude_get_system_health", modern=modern)) == "forbidden"
        assert (
            error(
                await rpc(
                    http,
                    url,
                    created["key"],
                    "bamdude_inspect_printer",
                    {"printer_id": a.id, "path": "forbidden"},
                    modern=modern,
                )
            )
            == "invalid_arguments"
        )


async def test_printer_scope_precedes_pagination(async_client, runtime, printer_factory):  # noqa: F811
    # Hidden lower IDs must not consume the visible page or create a continuation.
    hidden = [await printer_factory(name=f"hidden {i}") for i in range(2)]
    visible = await printer_factory(name="visible")
    assert all(printer.id < visible.id for printer in hidden)
    scoped, empty = await key(async_client, scope=[visible.id]), await key(async_client, scope=[])
    async with live_app() as url, httpx.AsyncClient() as http:
        page = data(await rpc(http, url, scoped["key"], "bamdude_get_farm_overview", {"limit": 1}))
        assert [item["printer"]["printer_id"] for item in page["data"]["printers"]] == [visible.id]
        assert page["next_cursor"] is None
        assert not page["coverage"]["truncated"]
        nothing = data(await rpc(http, url, empty["key"], "bamdude_get_farm_overview", {"limit": 1}))
        assert nothing["data"]["printers"] == []
        assert nothing["next_cursor"] is None
        assert not nothing["coverage"]["truncated"]


async def test_empty_scope_is_not_unrestricted_and_cursor_never_widens_access(async_client, runtime, printer_factory):  # noqa: F811
    printers = [await printer_factory(name=f"printer {i}") for i in range(3)]
    wide, empty, other = await key(async_client), await key(async_client, scope=[]), await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        first = data(await rpc(http, url, wide["key"], "bamdude_get_farm_overview", {"limit": 1}))
        token = first["next_cursor"]
        assert token
        assert data(await rpc(http, url, empty["key"], "bamdude_get_farm_overview"))["data"]["printers"] == []
        assert (
            error(await rpc(http, url, other["key"], "bamdude_get_farm_overview", {"limit": 1, "cursor": token}))
            == "invalid_cursor"
        )
        added = await printer_factory(name="added after upper bound")
        ids = [first["data"]["printers"][0]["printer"]["printer_id"]]
        while token:
            page = data(await rpc(http, url, wide["key"], "bamdude_get_farm_overview", {"limit": 1, "cursor": token}))
            ids += [item["printer"]["printer_id"] for item in page["data"]["printers"]]
            token = page["next_cursor"]
        assert ids == [p.id for p in printers] and added.id not in ids
        await async_client.patch(f"/api/v1/api-keys/{wide['id']}", json={"printer_ids": [printers[0].id]})
        assert (
            error(
                await rpc(
                    http, url, wide["key"], "bamdude_get_farm_overview", {"limit": 1, "cursor": first["next_cursor"]}
                )
            )
            == "invalid_cursor"
        )


async def test_owner_all_permissions_are_required_and_refreshed_on_every_call(
    async_client, runtime, db_session, printer_factory, telemetry
):  # noqa: F811
    printer = await printer_factory()
    await queued(db_session, printer, name="SECRET queue filename")
    created = await key(async_client)
    group = Group(name="diagnostic status reader", permissions=["printers:read", "queue:read_own", "archives:read_own"])
    owner = User(username="diagnostic reader", role="user", is_active=True, groups=[group])
    db_session.add_all([group, owner])
    await db_session.flush()
    await db_session.execute(update(APIKey).where(APIKey.id == created["id"]).values(user_id=owner.id))
    await db_session.commit()
    async with live_app() as url, httpx.AsyncClient() as http:
        inspected = data(await rpc(http, url, created["key"], "bamdude_inspect_printer", {"printer_id": printer.id}))
        assert inspected["data"]["queue_availability"] == "unavailable_for_actor"
        assert inspected["data"]["printer"]["queue"] is None
        assert inspected["data"]["plugs"]["availability"] == "unavailable_for_actor"
        assert "SECRET" not in json.dumps(inspected) and "private active filename" not in json.dumps(inspected)
        owner.is_active = False
        await db_session.commit()
        response = await http.post(url + "/mcp", headers={"X-API-Key": created["key"]}, json={})
        assert response.status_code == 403


@pytest.mark.parametrize("modern", [False, True])
async def test_catalog_discovery_and_direct_calls_share_request_authority(async_client, runtime, modern):  # noqa: F811
    wide, empty = await key(async_client), await key(async_client, scope=[])
    async with live_app() as url, httpx.AsyncClient() as http:

        async def discover(token):
            method = "server/discover" if modern else "tools/list"
            params = (
                {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                }
                if modern
                else {}
            )
            headers = {"Accept": "application/json, text/event-stream", "X-API-Key": token}
            if modern:
                headers.update({"MCP-Protocol-Version": "2026-07-28", "MCP-Method": method})
            return await http.post(
                url + "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params}
            )

        allowed, hidden = await asyncio.gather(discover(wide["key"]), discover(empty["key"]))
        assert allowed.status_code == hidden.status_code == 200
        assert "bamdude_get_system_health" in allowed.text
        assert "bamdude_get_system_health" not in hidden.text
        assert error(await rpc(http, url, empty["key"], "bamdude_get_system_health", modern=modern)) == "forbidden"


async def test_page_is_bounded_before_hydration_and_query_count_does_not_grow(
    async_client, runtime, printer_factory, db_session, test_engine, telemetry
):  # noqa: F811
    for i in range(3):
        printer = await printer_factory(name=f"initial {i}")
        await queued(db_session, printer)
    created = await key(async_client)
    queries = []

    def observed(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append((statement, parameters))

    # Call the shared production reader, avoiding the auth's unrelated queries.
    from backend.app.mcp_server.contracts import DiagnosticAccess
    from backend.app.services.farm_diagnostics.contracts import PageInput
    from backend.app.services.farm_diagnostics.overview import overview
    from backend.app.services.queue_sources import current_epoch

    access = DiagnosticAccess(created["id"], 1, frozenset(), True, None, False, current_epoch())
    event.listen(test_engine.sync_engine, "before_cursor_execute", observed)
    try:
        await overview(db_session, access, PageInput(limit=2), "small")
        small = list(queries)
        for i in range(8):
            printer = await printer_factory(name=f"extra {i}")
            await queued(db_session, printer)
        queries.clear()
        result = await overview(db_session, access, PageInput(limit=2), "larger")
        assert len(queries) == len(small)
        assert len(result.data["printers"]) == 2 and result.next_cursor
        assert any("LIMIT" in sql and "printers.id" in sql for sql, _ in queries)
        assert not any("printers.access_code" in sql or "printers.ip_address" in sql for sql, _ in queries)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", observed)


@pytest.mark.parametrize("change", ["run", "queue", "epoch"])
async def test_changed_run_or_restore_does_not_publish_mixed_facts(
    async_client, runtime, printer_factory, db_session, monkeypatch, telemetry, change
):  # noqa: F811
    from backend.app.services import queue_sources
    from backend.app.services.farm_diagnostics import overview as module

    printer = await printer_factory()
    queue, _ = await queued(db_session, printer, printing=True)
    original = module.build_snapshot

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        if change == "run":
            telemetry.subtask_id = "run-2"
        elif change == "queue":
            queue.current_item_id = None
            await db_session.commit()
        else:
            queue_sources.invalidate_receipts()
        return result

    monkeypatch.setattr(module, "build_snapshot", changed)
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        response = await http.post(
            url + "/mcp",
            headers={"Accept": "application/json, text/event-stream", "X-API-Key": created["key"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "bamdude_get_farm_overview"}},
        )
    if change == "epoch":
        assert response.status_code == 503 and response.json()["code"] == "state_changed"
    else:
        assert error(response.json()["result"]) == "state_changed"


@pytest.mark.parametrize("stale", [False, True])
async def test_orphan_unknown_telemetry_and_wait_timestamp_do_not_prove_stale_claim(
    async_client, runtime, printer_factory, db_session, monkeypatch, stale
):  # noqa: F811
    printer = await printer_factory()
    await queued(db_session, printer, printing=True)
    observation = (
        (PrinterState(connected=True, state="RUNNING"), datetime.now(timezone.utc).timestamp() - 3600, True)
        if stale
        else (None, None, False)
    )
    monkeypatch.setattr(printer_manager, "peek_status", lambda _: observation)
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        result = data(await rpc(http, url, created["key"], "bamdude_inspect_printer", {"printer_id": printer.id}))
    assert result["findings"][0]["code"] == "queue_claim_requires_inspection"
    assert result["findings"][0]["certainty"] == "insufficient_evidence"
    assert result["data"]["freshness"] == ("stale" if stale else "unknown")
    assert result["data"]["printer"]["connected"] is None
    assert not result["coverage"]["complete"]


async def test_reads_never_probe_devices_and_health_is_allowlisted(
    async_client, runtime, printer_factory, smart_plug_factory, db_session, telemetry, monkeypatch
):  # noqa: F811
    from backend.app.api.routes import system
    from backend.app.services import (
        camera_metrics,
        camera_runtime,
        diagnostic_snapshot,
        part_render_runtime,
        printer_diagnostic,
    )
    from backend.app.services.print_state_observer import print_state_observer
    from backend.app.services.smart_plug_manager import smart_plug_manager

    printer = await printer_factory()
    await queued(db_session, printer)
    await smart_plug_factory(
        printer_id=printer.id,
        last_state="ON",
        last_checked=datetime.now(timezone.utc),
        schedule_enabled=True,
        schedule_on_time="08:00",
        ip_address="SECRET plug address",
    )
    blocked = Mock(side_effect=AssertionError("active probe"))
    monkeypatch.setattr(system, "scan_logs", blocked)
    monkeypatch.setattr(system, "get_system_health", blocked)
    monkeypatch.setattr(diagnostic_snapshot, "collect_diagnostic_snapshot", blocked)
    monkeypatch.setattr(printer_diagnostic, "run_connection_diagnostic", blocked)
    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", blocked)
    monkeypatch.setattr(camera_runtime, "capture", blocked)
    monkeypatch.setattr(camera_runtime, "get_camera_runtime", blocked)
    monkeypatch.setattr(smart_plug_manager, "get_service_for_plug", blocked)
    monkeypatch.setattr(print_state_observer, "public_summary", Mock(side_effect=RuntimeError("SECRET observer error")))
    monkeypatch.setattr(
        part_render_runtime,
        "get_part_render_health",
        lambda **_: {"state": "unavailable", "reason": "SECRET arbitrary reason", "last": {"path": "SECRET file"}},
    )
    monkeypatch.setattr(
        camera_metrics,
        "for_printer",
        lambda _: {"frame_age_ms": 10000, "consecutive_failures": 1, "url": "SECRET camera"},
    )
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        before = (await db_session.scalars(select(PrintQueueItem.id))).all()
        overview = data(await rpc(http, url, created["key"], "bamdude_get_farm_overview"))
        inspected = data(await rpc(http, url, created["key"], "bamdude_inspect_printer", {"printer_id": printer.id}))
        health = data(await rpc(http, url, created["key"], "bamdude_get_system_health"))
        assert inspected["data"]["plugs"]["items"][0]["cached_state"] == "ON"
        assert inspected["data"]["camera"]["captured_at"] is None
        assert inspected["data"]["camera"]["recent_capture_failed"]
        assert health["data"]["components"]["part_render"]["reason"] == "unknown_reason"
        assert health["data"]["print_state_observer"]["availability"] == "source_unavailable"
        assert not health["coverage"]["complete"]
        assert "SECRET" not in json.dumps([overview, inspected, health])
        assert before == (await db_session.scalars(select(PrintQueueItem.id))).all()
    blocked.assert_not_called()


async def test_claim_observation_is_bounded_and_does_not_follow_a_foreign_header(
    async_client, runtime, printer_factory, db_session, monkeypatch
):  # noqa: F811
    a, b = await printer_factory(name="A"), await printer_factory(name="B")
    aq, _ = await queued(db_session, a, printing=True)
    _, foreign = await queued(db_session, b, printing=True, name="SECRET foreign")
    aq.current_item_id = foreign.id
    aq.status = "idle"
    await db_session.commit()
    monkeypatch.setattr(printer_manager, "peek_status", lambda _: (None, None, False))
    created = await key(async_client, scope=[a.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        result = data(await rpc(http, url, created["key"], "bamdude_inspect_printer", {"printer_id": a.id}))
    assert result["data"]["claim"] == {"current_item_id": None, "header_link_valid": False, "printing_row_count": 1}
    assert result["data"]["printer"]["current_job"] is None
    assert any(f["code"] == "queue_current_job_ambiguous" for f in result["findings"])
    assert result["findings"][0]["code"] == "queue_claim_requires_inspection"
    assert "SECRET foreign" not in json.dumps(result)


async def test_system_health_uses_cached_part_render_state_without_opening_manifest(async_client, runtime, monkeypatch):  # noqa: F811
    from backend.app.services import part_render_runtime, render_runtime

    cached = object.__new__(part_render_runtime.PartRenderRuntime)
    for name, value in {
        "uncertain": False,
        "closed": False,
        "ready": True,
        "staging_over": False,
        "node_reason": None,
        "node": None,
        "bundle_sha256": None,
        "queue": {},
        "last": None,
        "stats": {},
    }.items():
        setattr(cached, name, value)
    monkeypatch.setattr(part_render_runtime, "runtime", cached)
    blocked = Mock(side_effect=AssertionError("manifest filesystem read"))
    monkeypatch.setattr(render_runtime, "load_manifest", blocked)
    created = await key(async_client)
    async with live_app() as url, httpx.AsyncClient() as http:
        result = data(await rpc(http, url, created["key"], "bamdude_get_system_health"))
    assert result["data"]["components"]["part_render"]["state"] == "ready"
    blocked.assert_not_called()
