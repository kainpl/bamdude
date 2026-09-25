"""Scheduled drying routes: one-shot runs, rules, refusals, printer removal (spec §API)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.scheduled_drying import DryingSchedule, ScheduledDrying


def _state(module_type="n3f"):
    return SimpleNamespace(
        state="IDLE",
        firmware_version="01.11.00.00",  # X1C dries from 01.09
        raw_data={"ams": [{"id": 0, "module_type": module_type, "tray": [{"tray_type": "PLA"}], "dry_sf_reason": []}]},
    )


@pytest.fixture
def live_printer():
    pm = MagicMock()
    pm.get_status.return_value = _state()
    pm.send_drying_command.return_value = True
    with patch("backend.app.services.scheduled_drying.printer_manager", pm):
        yield pm


def _future(hours=2):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


@pytest.mark.asyncio
async def test_a_one_shot_run_is_listed_and_cancelled(async_client, printer_factory, live_printer):
    printer = await printer_factory(model="X1C")
    created = await async_client.post(
        "/api/v1/scheduled-dryings",
        json={"printer_id": printer.id, "ams_id": 0, "temp": 55, "duration_hours": 8, "start_after": _future()},
    )
    assert created.status_code == 200, created.text
    run = created.json()
    assert run["status"] == "pending" and run["start_after"].endswith("Z")

    listed = await async_client.get(f"/api/v1/scheduled-dryings?printer_id={printer.id}")
    assert [r["id"] for r in listed.json()] == [run["id"]]

    cancelled = await async_client.delete(f"/api/v1/scheduled-dryings/{run['id']}")
    assert cancelled.json()["status"] == "cancelled"
    assert (await async_client.get(f"/api/v1/scheduled-dryings?printer_id={printer.id}")).json() == []


@pytest.mark.asyncio
async def test_a_run_in_the_past_is_refused(async_client, printer_factory, live_printer):
    printer = await printer_factory(model="X1C")
    r = await async_client.post(
        "/api/v1/scheduled-dryings",
        json={"printer_id": printer.id, "ams_id": 0, "temp": 55, "duration_hours": 8, "start_after": _future(-1)},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_a_screen_only_printer_is_refused(async_client, printer_factory, live_printer):
    printer = await printer_factory(model="P1S")
    r = await async_client.post(
        "/api/v1/scheduled-dryings", json={"printer_id": printer.id, "ams_id": 0, "temp": 55, "duration_hours": 8}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_temperature_over_the_units_ceiling_is_refused(async_client, printer_factory, live_printer):
    printer = await printer_factory(model="X1C")
    r = await async_client.post(
        "/api/v1/scheduled-dryings", json={"printer_id": printer.id, "ams_id": 0, "temp": 80, "duration_hours": 8}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_a_rule_round_trip_and_its_timezone(async_client, printer_factory, live_printer):
    printer = await printer_factory(model="X1C")
    body = {
        "printer_id": printer.id,
        "ams_id": 0,
        "temp": 55,
        "duration_hours": 8,
        "start_time": "01:00",
        "weekdays": 127,
    }
    created = await async_client.post("/api/v1/drying-schedules", json=body)
    assert created.status_code == 200, created.text
    rule = created.json()

    listed = (await async_client.get(f"/api/v1/drying-schedules?printer_id={printer.id}")).json()
    assert listed["server_timezone"]
    assert [s["id"] for s in listed["schedules"]] == [rule["id"]]

    patched = await async_client.patch(f"/api/v1/drying-schedules/{rule['id']}", json={"enabled": False})
    assert patched.json()["enabled"] is False
    assert (await async_client.delete(f"/api/v1/drying-schedules/{rule['id']}")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"start_time": "1:00"}, {"weekdays": 0}, {"latest_start": "25:00"}])
async def test_a_malformed_rule_is_refused(async_client, printer_factory, live_printer, bad):
    printer = await printer_factory(model="X1C")
    body = {
        "printer_id": printer.id,
        "ams_id": 0,
        "temp": 55,
        "duration_hours": 8,
        "start_time": "01:00",
        "weekdays": 127,
    }
    r = await async_client.post("/api/v1/drying-schedules", json={**body, **bad})
    assert r.status_code in (400, 422)


@pytest.mark.asyncio
async def test_editing_a_rule_replaces_its_pending_run_but_not_a_running_one(db_session, printer_factory, live_printer):
    from backend.app.services import scheduled_drying as sd

    printer = await printer_factory(model="X1C")
    rule = DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00", weekdays=127)
    db_session.add(rule)
    await db_session.flush()
    pending = ScheduledDrying(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, schedule_id=rule.id)
    running = ScheduledDrying(
        printer_id=printer.id, ams_id=1, temp=55, duration_hours=8, schedule_id=rule.id, status="running"
    )
    db_session.add_all([pending, running])
    await db_session.commit()

    await sd.update_schedule(db_session, rule.id, {"temp": 60})

    rows = (await db_session.execute(select(ScheduledDrying))).scalars().all()
    assert {r.status for r in rows} == {"running"}  # the pending run is gone; the next tick re-creates it


@pytest.mark.asyncio
async def test_archiving_a_printer_cancels_runs_and_disables_rules(db_session, printer_factory, live_printer):
    from backend.app.services import scheduled_drying as sd

    printer = await printer_factory(model="X1C")
    db_session.add(DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00"))
    db_session.add(ScheduledDrying(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8))
    await db_session.commit()

    await sd.forget_printer(db_session, printer.id, archived=True)

    assert (await db_session.execute(select(DryingSchedule.enabled))).scalar_one() is False
    assert (await db_session.execute(select(ScheduledDrying.status))).scalar_one() == "cancelled"


@pytest.mark.asyncio
async def test_deleting_a_printer_removes_its_rules_and_runs(db_session, printer_factory, live_printer):
    from backend.app.services import scheduled_drying as sd

    printer = await printer_factory(model="X1C")
    db_session.add(DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00"))
    db_session.add(ScheduledDrying(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8))
    await db_session.commit()

    await sd.forget_printer(db_session, printer.id, archived=False)

    assert (await db_session.execute(select(DryingSchedule))).first() is None
    assert (await db_session.execute(select(ScheduledDrying))).first() is None
