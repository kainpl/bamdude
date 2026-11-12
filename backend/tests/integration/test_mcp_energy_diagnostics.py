"""Persisted energy evidence, scoped before page/count and never provider I/O."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import event, update

from backend.app.core.auth import generate_api_key
from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.models.api_key import APIKey
from backend.app.models.group import Group
from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot
from backend.app.models.smart_plug_meter_incident import SmartPlugMeterIncident
from backend.app.models.smart_plug_power_history import SmartPlugPowerHistory
from backend.app.models.user import User
from backend.app.services.farm_diagnostics.access import REQUIREMENTS, DiagnosticError
from backend.app.services.farm_diagnostics.contracts import EnergyInput
from backend.app.services.queue_sources import current_epoch
from backend.tests.auth_helpers import bind_test_key
from backend.tests.integration.test_mcp_base_transport import key, live_app, runtime  # noqa: F401
from backend.tests.integration.test_mcp_farm_diagnostics import data, error, rpc

START = datetime(2026, 10, 24, 20, tzinfo=timezone.utc)
END = START + timedelta(hours=12)
ENERGY_PERMISSIONS = frozenset({"printers:read", "archives:read_all", "smart_plugs:read", "stats:read"})


def args(pid, **kwargs):
    return EnergyInput.model_validate({"printer_id": pid, "from": START, "to": END, **kwargs})


def actor(pid=None):
    return DiagnosticAccess(
        1,
        1,
        REQUIREMENTS["bamdude_inspect_energy"],
        False,
        None if pid is None else frozenset({pid}),
        False,
        current_epoch(),
        key_identity="00000000-0000-0000-0000-000000000001",
        owner_identity="00000000-0000-0000-0000-000000000002",
    )


async def inspect(db, pid, access=None, **kwargs):
    from backend.app.services.farm_diagnostics.energy import inspect_energy

    return await inspect_energy(db, access or actor(pid), args(pid, **kwargs), "energy-test")


async def points(db, plug, values):
    db.add_all(
        [
            SmartPlugEnergySnapshot(
                plug_id=plug.id, recorded_at=at.astimezone(timezone.utc).replace(tzinfo=None), lifetime_kwh=kwh
            )
            for at, kwh in values
        ]
    )
    await db.commit()


@pytest.mark.parametrize("provider", ["tasmota", "homeassistant", "mqtt", "rest"])
async def test_energy_growth_and_missing_prints_over_real_http(
    async_client, runtime, printer_factory, smart_plug_factory, archive_factory, db_session, monkeypatch, provider
):  # noqa: F811
    from backend.app.services import meter_watch
    from backend.app.services.smart_plug_manager import (
        homeassistant_service,
        mqtt_smart_plug_service,
        rest_smart_plug_service,
        smart_plug_manager,
        tasmota_service,
    )
    from backend.app.services.statistics import energy as statistics_energy

    p = await printer_factory()
    plug = await smart_plug_factory(
        printer_id=p.id,
        plug_type=provider,
        measurement_source_id="a" * 32,
        measurement_source_since=(START - timedelta(days=1)).replace(tzinfo=None),
        mqtt_energy_total_multiplier=0.001,
        rest_energy_total_multiplier=0.001,
    )
    await points(db_session, plug, [(START, 12), (START + timedelta(hours=2), 12.5)])
    archive = await archive_factory(p.id, created_at=START.replace(tzinfo=None), energy_kwh=None)
    blocked = AsyncMock(side_effect=AssertionError("active energy work"))
    monkeypatch.setattr(smart_plug_manager, "get_service_for_plug", blocked)
    monkeypatch.setattr(meter_watch, "assess_print_window", blocked)
    monkeypatch.setattr(meter_watch, "read_history", blocked)
    monkeypatch.setattr(statistics_energy, "sum_live_plug_totals", blocked)
    for service in (homeassistant_service, mqtt_smart_plug_service, rest_smart_plug_service, tasmota_service):
        monkeypatch.setattr(service, "get_energy", blocked)
    created = await key(async_client, scope=[p.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        results = [
            data(
                await rpc(
                    http,
                    url,
                    created["key"],
                    "bamdude_inspect_energy",
                    args(p.id).model_dump(mode="json", by_alias=True),
                    modern=modern,
                )
            )
            for modern in (False, True)
        ]
    for result in results:
        d = result["data"]
        assert d["prints"][0]["archive_id"] == archive.id
        assert d["print_summary"]["null_count"] == 1
        assert d["plugs"][0]["counter_delta_kwh"] == 0.5  # already normalized kWh, no rescaling
        assert d["plugs"][0]["basis"] == "currently_bound_plug_after_source_cutoff"
        assert result["findings"][0]["code"] == "energy_counter_grows_print_accounting_missing"
        assert "not attribution" in result["findings"][0]["explanation"]
    blocked.assert_not_called()


async def test_energy_status_buckets_zero_legacy_and_allowlisted_provenance(
    printer_factory, archive_factory, db_session
):
    p = await printer_factory()
    samples = [
        (0, {"energy_provenance": {"version": 1, "kind": "measured"}}, "measured"),
        (0.2, {"energy_provenance": {"kind": "measured"}, "energy_is_approximate": True}, "approximate"),
        (0.3, {"energy_provenance": {"kind": "estimated", "coverage": 0.98}}, "estimated"),
        (None, {"energy_provenance": {"kind": "unavailable", "unavailable_reason": "source_changed"}}, "unavailable"),
        (0.4, {}, "legacy"),
        (None, {}, "missing_legacy"),
        (0.8, {"energy_provenance": {"kind": "future"}}, "unknown"),
        (None, {"energy_provenance": {"kind": "unavailable", "unavailable_reason": "private secret"}}, "unavailable"),
    ]
    for i, (number, extra, _) in enumerate(samples):
        extra["private_data"] = "private secret"
        if "energy_provenance" in extra:
            extra["energy_provenance"].update(secret="private secret", start=START.isoformat(), end=END.isoformat())
        await archive_factory(
            p.id,
            created_at=(START + timedelta(seconds=i)).replace(tzinfo=None),
            energy_kwh=number,
            energy_cost=number,
            energy_start_kwh=0 if i == 0 else None,
            extra_data=extra,
        )
    r = await inspect(db_session, p.id)
    assert [a["energy_status"] for a in r.data["prints"]] == [s[2] for s in samples]
    summary = r.data["print_summary"]
    assert summary["measured_print_kwh"] == 0 and summary["approximate_print_kwh"] == 0.2
    assert summary["estimated_print_kwh"] == 0.3 and summary["legacy_print_kwh"] == 0.4
    assert summary["null_count"] == 3 and summary["zero_count"] == 1 and summary["estimate_count"] == 1
    assert summary["start_counter_count"] == 1 and summary["scope"] == "this_page"
    assert summary["unusable_count"] == 4 and summary["unknown_count"] == 1
    assert summary["known_print_kwh"] == pytest.approx(0.9)  # future-kind figure isn't trusted
    assert "private secret" not in r.model_dump_json()
    assert not r.coverage.complete


@pytest.mark.parametrize(
    "values,expected,reset", [([], None, False), ([10], None, False), ([10, 11], 1, False), ([10, 2, 3], None, True)]
)
async def test_energy_unknown_baseline_and_counter_decrease(
    printer_factory, smart_plug_factory, db_session, values, expected, reset
):
    p = await printer_factory()
    plug = await smart_plug_factory(
        printer_id=p.id,
        measurement_source_id="a" * 32,
        measurement_source_since=(START - timedelta(days=1)).replace(tzinfo=None),
    )
    # A lone point after the range start does not supply a baseline.
    times = (
        [START + timedelta(hours=1)] if len(values) == 1 else [START + timedelta(hours=i) for i in range(len(values))]
    )
    await points(db_session, plug, list(zip(times, values, strict=True)))
    d = (await inspect(db_session, p.id)).data["plugs"][0]
    assert d["counter_delta_kwh"] == expected
    assert bool(d["decreases"]) == reset
    if expected is None:
        assert not d["coverage"]["complete"]


async def test_energy_rebind_excludes_foreign_history_but_keeps_archive_owned_energy(
    printer_factory, smart_plug_factory, archive_factory, db_session
):
    a, b = await printer_factory(), await printer_factory()
    cutoff = START + timedelta(hours=3)
    plug = await smart_plug_factory(
        printer_id=b.id, measurement_source_id="b" * 32, measurement_source_since=cutoff.replace(tzinfo=None)
    )
    foreign = await smart_plug_factory(printer_id=a.id, measurement_source_id="f" * 32)
    await points(
        db_session,
        plug,
        [(START, 999), (cutoff, 1000), (cutoff + timedelta(hours=1), 1), (cutoff + timedelta(hours=2), 2)],
    )
    await points(db_session, foreign, [(START, 4444)])
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id="a" * 32,
            opened_at=START.replace(tzinfo=None),
            flat_since=START.replace(tzinfo=None),
            flat_until=START.replace(tzinfo=None),
            total_kwh=999,
        )
    )
    await db_session.commit()
    old = await archive_factory(a.id, energy_kwh=0.7, created_at=START.replace(tzinfo=None))
    await archive_factory(b.id, energy_kwh=0.2, created_at=START.replace(tzinfo=None))
    own = await inspect(db_session, a.id)
    assert own.data["prints"][0]["archive_id"] == old.id
    new = await inspect(db_session, b.id)
    wire = new.model_dump_json()
    assert "999" not in wire and "1000" not in wire and "4444" not in wire
    assert [s["lifetime_kwh"] for s in new.data["plugs"][0]["snapshots"]] == [1, 2]
    assert not new.data["plugs"][0]["incidents"]
    assert new.data["print_summary"]["legacy_print_kwh"] == 0.2
    with pytest.raises(DiagnosticError, match="not_found"):
        await inspect(db_session, b.id, actor(a.id))
    with pytest.raises(DiagnosticError, match="not_found"):
        await inspect(db_session, a.id, replace(actor(), printer_ids=frozenset()))


async def test_energy_missing_source_omits_history_and_preserves_unknown(
    printer_factory, smart_plug_factory, db_session
):
    p = await printer_factory()
    plug = await smart_plug_factory(printer_id=p.id)
    await points(db_session, plug, [(START, 12345), (END - timedelta(seconds=1), 12346)])
    r = await inspect(db_session, p.id)
    assert r.data["plugs"][0]["snapshots"] == []
    assert r.data["plugs"][0]["counter_delta_kwh"] is None
    assert "source_cutoff_unknown" in r.data["plugs"][0]["coverage"]["missing"]
    assert "12345" not in r.model_dump_json()


async def test_energy_measured_zero_is_not_a_gap_and_late_inserted_baseline_is_retained(
    printer_factory, smart_plug_factory, archive_factory, db_session
):
    p = await printer_factory()
    plug = await smart_plug_factory(
        printer_id=p.id,
        measurement_source_id="a" * 32,
        measurement_source_since=(START - timedelta(days=1)).replace(tzinfo=None),
    )
    # Insert the earlier baseline last: its ID exceeds the in-range sample's.
    await points(db_session, plug, [(START + timedelta(hours=1), 5.5), (START - timedelta(hours=1), 5)])
    archive = await archive_factory(
        p.id,
        created_at=START.replace(tzinfo=None),
        energy_kwh=0,
        extra_data={"energy_provenance": {"kind": "measured"}},
    )
    r = await inspect(db_session, p.id)
    assert r.data["plugs"][0]["counter_delta_kwh"] == 0.5
    assert r.data["print_summary"]["zero_count"] == 1 and not r.findings
    archive.energy_kwh = None
    archive.extra_data = {"energy_provenance": {"kind": "unavailable", "unavailable_reason": "source_changed"}}
    await db_session.commit()
    r = await inspect(db_session, p.id)
    assert r.findings and r.data["print_summary"]["unavailable_count"] == 1
    assert r.data["print_summary"]["missing_legacy_count"] == 0


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-10-24T23:30:00+03:00", "2026-10-25T04:30:00+02:00"),  # Kyiv DST fall-back
        ("2026-10-25T00:00:00+02:00", "2026-10-25T03:00:00+02:00"),
        ("2026-10-24T12:00:00Z", "2026-10-24T14:00:00Z"),
    ],
)
async def test_energy_utc_half_open_range_daily_fields_are_not_lifetime(
    printer_factory, smart_plug_factory, archive_factory, db_session, start, end
):
    lo, hi = datetime.fromisoformat(start), datetime.fromisoformat(end)
    p = await printer_factory()
    plug = await smart_plug_factory(
        printer_id=p.id,
        plug_type="rest",
        measurement_source_id="a" * 32,
        measurement_source_since=(lo - timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None),
        rest_energy_path="daily_wh",
        rest_energy_multiplier=0.001,
    )
    # Persisted lifetime snapshots, not live daily values. The adapter never reads the latter.
    await points(db_session, plug, [(lo, 5), (hi - timedelta(seconds=1), 5.3), (hi, 99)])
    await archive_factory(p.id, created_at=lo.astimezone(timezone.utc).replace(tzinfo=None), energy_kwh=0)
    await archive_factory(p.id, created_at=hi.astimezone(timezone.utc).replace(tzinfo=None), energy_kwh=999)
    r = await inspect(db_session, p.id, **{"from": lo, "to": hi})
    assert len(r.data["prints"]) == 1
    assert r.data["plugs"][0]["counter_delta_kwh"] == pytest.approx(0.3)
    assert not r.data["plugs"][0]["decreases"]


async def test_energy_pages_and_summaries_are_scoped_bounded_and_not_snapshots(
    printer_factory, smart_plug_factory, archive_factory, db_session, test_engine
):
    p, hidden = await printer_factory(), await printer_factory()
    plug = await smart_plug_factory(
        printer_id=p.id,
        measurement_source_id="a" * 32,
        measurement_source_since=(START - timedelta(days=1)).replace(tzinfo=None),
    )
    await points(db_session, plug, [(START + timedelta(minutes=i), i) for i in range(7)])
    for _ in range(3):
        await archive_factory(hidden.id, created_at=START.replace(tzinfo=None), energy_kwh=9999)
    expected = [
        (await archive_factory(p.id, created_at=START.replace(tzinfo=None), energy_kwh=0.1)).id for _ in range(5)
    ]
    await archive_factory(
        p.id, created_at=START.replace(tzinfo=None), energy_kwh=888, deleted_at=START.replace(tzinfo=None)
    )
    statements = []

    def sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", sql)
    try:
        first = await inspect(db_session, p.id, limit=2)
        count_small = len(statements)
        statements.clear()
        for _ in range(30):
            await archive_factory(p.id, created_at=START.replace(tzinfo=None), energy_kwh=0.2)
        statements.clear()
        again = await inspect(db_session, p.id, limit=2)
        assert len(statements) == count_small
        assert all("LIMIT" in s.upper() for s in statements if "print_archives.energy_kwh" in s.lower())
        assert not any("print_archives.extra_data \n" in s for s in statements)
        wide = await inspect(db_session, p.id, actor(), limit=2)
        assert wide.data["print_summary"]["known_print_kwh"] == 0.2
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", sql)
    assert again.data["print_summary"]["scope"] == "this_page"
    assert first.coverage.truncated and first.next_cursor and len(first.next_cursor) <= 2048
    assert first.data["print_summary"]["known_print_kwh"] == 0.2
    seen, snapshots, cursor = [], [], None
    page = first
    while True:
        seen.extend(a["archive_id"] for a in page.data["prints"])
        snapshots.extend(s["id"] for plug in page.data["plugs"] for s in plug["snapshots"])
        cursor = page.next_cursor
        if not cursor:
            break
        page = await inspect(db_session, p.id, limit=2, cursor=cursor)
    assert seen == expected and len(snapshots) == len(set(snapshots)) == 7
    with pytest.raises(DiagnosticError, match="invalid_cursor"):
        await inspect(db_session, p.id, replace(actor(p.id), key_id=2), limit=2, cursor=first.next_cursor)


async def test_energy_incidents_quality_and_source_change_fence(
    printer_factory, smart_plug_factory, db_session, monkeypatch
):
    from backend.app.services.farm_diagnostics import energy

    p = await printer_factory()
    plug = await smart_plug_factory(
        printer_id=p.id,
        measurement_source_id="a" * 32,
        measurement_source_since=(START - timedelta(days=1)).replace(tzinfo=None),
    )
    db_session.add(
        SmartPlugMeterIncident(
            plug_id=plug.id,
            source_id="a" * 32,
            opened_at=START.replace(tzinfo=None),
            flat_since=START.replace(tzinfo=None),
            flat_until=END.replace(tzinfo=None),
            total_kwh=1,
            evidence={"private": "secret payload"},
        )
    )
    db_session.add_all(
        [
            SmartPlugPowerHistory(
                plug_id=plug.id, power=0, recorded_at=START.replace(tzinfo=None), unusable_reason=reason
            )
            for reason in ("buffer_loss", "private reason")
        ]
    )
    await db_session.commit()
    r = await inspect(db_session, p.id)
    facts = r.data["plugs"][0]
    assert facts["incidents"][0]["meaning"] == "persisted_meter_suspicion"
    assert facts["quality_counts"] == {"buffer_loss": 1, "unknown_recorded_reason": 1}
    assert "secret payload" not in r.model_dump_json() and "private reason" not in r.model_dump_json()
    original = energy._bindings
    calls = 0

    async def rebind(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            return []
        return result

    monkeypatch.setattr(energy, "_bindings", rebind)
    with pytest.raises(DiagnosticError, match="state_changed"):
        await inspect(db_session, p.id)


@pytest.mark.parametrize("removed", sorted(ENERGY_PERMISSIONS))
async def test_energy_permission_gates_apply_to_direct_call(
    async_client, runtime, printer_factory, db_session, removed
):  # noqa: F811
    p = await printer_factory()
    assert REQUIREMENTS["bamdude_inspect_energy"] == ENERGY_PERMISSIONS
    created = await key(async_client, scope=[p.id])
    async with live_app() as url, httpx.AsyncClient() as http:
        assert not (
            await rpc(
                http, url, created["key"], "bamdude_inspect_energy", args(p.id).model_dump(mode="json", by_alias=True)
            )
        ).get("isError")
        group = Group(name="energy reader", permissions=sorted(ENERGY_PERMISSIONS - {removed}))
        owner = User(username="energy reader", role="user", is_active=True, groups=[group])
        db_session.add_all([group, owner])
        await db_session.flush()
        # A key's owner incarnation is immutable: issue a fresh key for this owner.
        raw, hashed, prefix = generate_api_key()
        owned_key = APIKey(
            name="energy owner",
            key_hash=hashed,
            key_prefix=prefix,
            can_access_mcp=True,
            can_read_status=True,
            can_queue=False,
            can_control_printer=False,
            printer_ids=[p.id],
        )
        await bind_test_key(db_session, owned_key, owner)
        db_session.add(owned_key)
        await db_session.commit()
        created = {"id": owned_key.id, "key": raw}
        if removed == "printers:read":
            # The existing MCP entrance already requires this right, so it
            # refuses before SDK dispatch rather than returning a tool error.
            response = await http.post(url + "/mcp", headers={"X-API-Key": created["key"]}, json={})
            assert response.status_code == 403
            return
        for modern in (False, True):
            result = await rpc(
                http,
                url,
                created["key"],
                "bamdude_inspect_energy",
                args(p.id).model_dump(mode="json", by_alias=True),
                modern=modern,
            )
            assert error(result) == "forbidden"
