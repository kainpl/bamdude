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

    await db_session.refresh(pending)
    await db_session.refresh(running)
    # The waiting run keeps its night and takes the new temperature; the running one finishes as it started.
    assert (pending.status, pending.temp) == ("pending", 60)
    assert (running.status, running.temp) == ("running", 55)


@pytest.mark.asyncio
async def test_editing_a_rule_inside_tonights_window_keeps_tonight(db_session, printer_factory, live_printer):
    """Focus 3: an edit at 02:00 of a 01:00-05:00 rule whose run waits must not move it to tomorrow."""
    from zoneinfo import ZoneInfo

    from backend.app.services import scheduled_drying as sd

    printer = await printer_factory(model="X1C")
    rule = DryingSchedule(
        printer_id=printer.id,
        ams_id=0,
        temp=55,
        duration_hours=8,
        start_time="01:00",
        weekdays=127,
        latest_start="05:00",
    )
    db_session.add(rule)
    await db_session.flush()
    tonight = datetime(2026, 9, 25, 22, 0)  # 01:00 Kyiv
    pending = ScheduledDrying(
        printer_id=printer.id,
        ams_id=0,
        temp=55,
        duration_hours=8,
        schedule_id=rule.id,
        start_after=tonight,
        latest_start=datetime(2026, 9, 26, 2, 0),
        reason="printer_busy",
    )
    db_session.add(pending)
    await db_session.commit()

    with patch.object(sd, "server_timezone", lambda: ZoneInfo("Europe/Kyiv")):
        await sd.update_schedule(db_session, rule.id, {"temp": 60, "latest_start": "06:00"})

    await db_session.refresh(pending)
    assert (pending.status, pending.temp, pending.start_after) == ("pending", 60, tonight)
    assert pending.latest_start == datetime(2026, 9, 26, 3, 0)  # 06:00 Kyiv


@pytest.mark.asyncio
async def test_moving_a_rules_time_replaces_its_waiting_run(db_session, printer_factory, live_printer):
    from backend.app.services import scheduled_drying as sd

    printer = await printer_factory(model="X1C")
    rule = DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00", weekdays=127)
    db_session.add(rule)
    await db_session.flush()
    db_session.add(ScheduledDrying(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, schedule_id=rule.id))
    await db_session.commit()

    await sd.update_schedule(db_session, rule.id, {"start_time": "02:00"})

    assert (await db_session.execute(select(ScheduledDrying))).first() is None  # the next tick re-creates it


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


# ------------------------------------------------ review fixes (whole-branch review)


def _not_reporting():
    return SimpleNamespace(state="IDLE", firmware_version=None, raw_data={})


@pytest.mark.asyncio
async def test_a_run_can_be_planned_for_a_printer_that_is_not_reporting(async_client, printer_factory, live_printer):
    """Firmware "when known", the unit "checked again before start" (spec §API)."""
    printer = await printer_factory(model="X1C")
    live_printer.get_status.return_value = _not_reporting()
    response = await async_client.post(
        "/api/v1/scheduled-dryings",
        json={"printer_id": printer.id, "ams_id": 0, "temp": 55, "duration_hours": 8, "start_after": _future()},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_an_ams_ht_rule_is_allowed_its_own_ceiling_while_the_unit_is_not_seen(
    async_client, printer_factory, live_printer
):
    printer = await printer_factory(model="X1C")
    live_printer.get_status.return_value = _not_reporting()
    response = await async_client.post(
        "/api/v1/drying-schedules",
        json={"printer_id": printer.id, "ams_id": 128, "temp": 80, "duration_hours": 8, "start_time": "01:00"},
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_pausing_a_rule_does_not_revalidate_its_target(async_client, db_session, printer_factory, live_printer):
    printer = await printer_factory(model="X1C")
    rule = DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00")
    db_session.add(rule)
    await db_session.commit()
    live_printer.get_status.return_value = SimpleNamespace(
        state="IDLE",
        firmware_version="01.08.00.00",
        raw_data={},  # too old to dry: the target would be refused
    )
    response = await async_client.patch(f"/api/v1/drying-schedules/{rule.id}", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["ams_id", "temp", "duration_hours", "filament", "rotate_tray", "start_time", "weekdays", "enabled"]
)
async def test_a_rule_field_cannot_be_nulled(async_client, db_session, printer_factory, live_printer, field):
    printer = await printer_factory(model="X1C")
    rule = DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00")
    db_session.add(rule)
    await db_session.commit()
    response = await async_client.patch(f"/api/v1/drying-schedules/{rule.id}", json={field: None})
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("weekdays", [-1, 128, 255])
async def test_weekdays_out_of_the_mask_are_refused(async_client, printer_factory, live_printer, weekdays):
    printer = await printer_factory(model="X1C")
    response = await async_client.post(
        "/api/v1/drying-schedules",
        json={
            "printer_id": printer.id,
            "ams_id": 0,
            "temp": 55,
            "duration_hours": 8,
            "start_time": "01:00",
            "weekdays": weekdays,
        },
    )
    assert response.status_code == 422, response.text


def test_rule_and_run_timestamps_are_stamped_in_python():
    """UTC whatever the database server's TimeZone: _materialise reads updated_at as UTC."""
    for column in (
        DryingSchedule.__table__.c.created_at,
        DryingSchedule.__table__.c.updated_at,
        ScheduledDrying.__table__.c.created_at,
    ):
        assert column.default is not None, column
        assert column.nullable is False, column


@pytest.mark.asyncio
async def test_deleting_a_printer_leaves_the_commit_to_the_route(db_session, printer_factory, live_printer):
    """The printer's delete is one transaction: forget_printer must not commit half of it."""
    from backend.app.services import scheduled_drying as sd

    printer = await printer_factory(model="X1C")
    db_session.add(DryingSchedule(printer_id=printer.id, ams_id=0, temp=55, duration_hours=8, start_time="01:00"))
    await db_session.commit()

    await sd.forget_printer(db_session, printer.id, archived=False, commit=False)
    await db_session.rollback()

    assert (await db_session.execute(select(DryingSchedule))).first() is not None
