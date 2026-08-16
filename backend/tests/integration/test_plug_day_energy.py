""" "Today" and "yesterday" for plugs whose protocol has neither.

Tasmota keeps ``Today``/``Yesterday`` registers. Zigbee cannot: the Metering
cluster exposes one cumulative counter and nothing else, so a per-day figure
does not exist to be read. BamDude derives both from
``smart_plug_energy_snapshots`` instead.

Reported from a live all-Zigbee farm: the energy summary said **Yesterday: 0**
after a day of printing. "Today" was already derived this way; "yesterday"
never got the same treatment and fell through to the plug's own (absent)
field, which the card renders as zero — a claim that nothing was used, not an
admission that nothing can be read.

⚠️ Boundaries are the CLIENT's day, and are taken from ``day_bounds`` rather
than by subtracting 24 hours: across a DST change those differ by an hour of a
farm's consumption. The tests below fix the timezone explicitly and compute the
expected instants with ``zoneinfo`` directly, so a wrong boundary cannot be
masked by the code under test choosing the same wrong one.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from backend.app.api.routes.smart_plugs import _today_from_snapshots, _yesterday_from_snapshots
from backend.app.models.smart_plug import SmartPlug
from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

pytestmark = pytest.mark.asyncio

KYIV = ZoneInfo("Europe/Kyiv")


def _request(tz_name: str | None = "Europe/Kyiv"):
    headers = {"X-Client-Timezone": tz_name} if tz_name else {}
    return SimpleNamespace(headers=headers)


def _local_midnight(days_ago: int, tz=KYIV) -> datetime:
    """The UTC instant a local day began, computed independently of the code."""
    day = (datetime.now(tz) - timedelta(days=days_ago)).date()
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(timezone.utc)


async def _plug(db_session, name: str = "Zigbee Plug") -> int:
    plug = SmartPlug(name=name, plug_type="zigbee", enabled=True)
    db_session.add(plug)
    await db_session.commit()
    await db_session.refresh(plug)
    return plug.id


async def _snapshots(db_session, plug_id: int, points: list[tuple[datetime, float]]) -> None:
    for at, kwh in points:
        db_session.add(SmartPlugEnergySnapshot(plug_id=plug_id, recorded_at=at, lifetime_kwh=kwh))
    await db_session.commit()


class TestYesterday:
    async def test_it_is_the_counter_moved_between_the_two_midnights(self, db_session):
        plug_id = await _plug(db_session)
        y_start, t_start = _local_midnight(1), _local_midnight(0)
        await _snapshots(
            db_session,
            plug_id,
            [
                (y_start - timedelta(hours=1), 10.0),  # last reading before yesterday
                (y_start + timedelta(hours=6), 11.5),  # mid-day, must not be an endpoint
                (t_start - timedelta(minutes=20), 13.0),  # last reading of yesterday
                (t_start + timedelta(minutes=30), 14.2),  # today — belongs to today
            ],
        )

        assert await _yesterday_from_snapshots(db_session, plug_id, _request()) == 3.0

    async def test_extra_snapshots_do_not_change_the_answer(self, db_session):
        """The figure is endpoint minus baseline, never a sum of pairs — which
        is what lets prints add snapshots of their own at any density."""
        plug_id = await _plug(db_session)
        y_start, t_start = _local_midnight(1), _local_midnight(0)
        dense = [(y_start + timedelta(hours=h), 10.0 + h * 0.1) for h in range(1, 23)]
        await _snapshots(
            db_session,
            plug_id,
            [(y_start - timedelta(hours=1), 10.0), *dense, (t_start - timedelta(minutes=5), 13.0)],
        )

        assert await _yesterday_from_snapshots(db_session, plug_id, _request()) == 3.0

    async def test_a_plug_with_no_reading_before_yesterday_answers_nothing(self, db_session):
        """⚠️ None, not 0. A plug adopted this morning did not use zero
        kilowatt-hours yesterday — it was not there."""
        plug_id = await _plug(db_session)
        await _snapshots(db_session, plug_id, [(_local_midnight(0) + timedelta(hours=1), 5.0)])

        assert await _yesterday_from_snapshots(db_session, plug_id, _request()) is None

    async def test_a_counter_reset_reads_as_zero_rather_than_negative(self, db_session):
        plug_id = await _plug(db_session)
        y_start, t_start = _local_midnight(1), _local_midnight(0)
        await _snapshots(
            db_session,
            plug_id,
            [(y_start - timedelta(hours=1), 40.0), (t_start - timedelta(minutes=10), 2.0)],
        )

        assert await _yesterday_from_snapshots(db_session, plug_id, _request()) == 0.0

    async def test_the_day_belongs_to_the_client_timezone(self, db_session):
        """The same snapshots read differently from Kyiv and from UTC, because
        the three hours after UTC midnight are still yesterday in Kyiv."""
        plug_id = await _plug(db_session)
        kyiv_today = _local_midnight(0, KYIV)
        utc_today = _local_midnight(0, timezone.utc)
        await _snapshots(
            db_session,
            plug_id,
            [
                (_local_midnight(1, KYIV) - timedelta(hours=2), 10.0),
                (_local_midnight(1, timezone.utc) - timedelta(hours=2), 10.0),
                (utc_today - timedelta(minutes=10), 12.0),
                (kyiv_today - timedelta(minutes=10), 15.0),
            ],
        )

        from_kyiv = await _yesterday_from_snapshots(db_session, plug_id, _request("Europe/Kyiv"))
        from_utc = await _yesterday_from_snapshots(db_session, plug_id, _request("UTC"))

        assert from_kyiv != from_utc, "the boundary ignored the client's timezone"


class TestTheEndpointActuallyAsksForIt:
    """⚠️ The helper existing is not the fix. For "today" the helper existed
    AND was wired; for "yesterday" neither did. A test that only exercises the
    function would pass with the route untouched — which is the bug that was
    reported."""

    async def test_status_carries_a_derived_yesterday(self, async_client, db_session, monkeypatch):
        from unittest.mock import AsyncMock

        plug_id = await _plug(db_session, name="Wired Zigbee")
        y_start, t_start = _local_midnight(1), _local_midnight(0)
        await _snapshots(
            db_session,
            plug_id,
            [(y_start - timedelta(hours=1), 100.0), (t_start - timedelta(minutes=5), 104.0)],
        )

        service = SimpleNamespace(
            get_status=AsyncMock(return_value={"reachable": True, "state": "ON", "device_name": "zp"}),
            # Exactly what the Zigbee driver returns: no today, no yesterday.
            get_energy=AsyncMock(return_value={"power": 12.0, "total": 105.0}),
        )
        monkeypatch.setattr(
            "backend.app.api.routes.smart_plugs._get_service_for_plug",
            AsyncMock(return_value=service),
        )

        resp = await async_client.get(
            f"/api/v1/smart-plugs/{plug_id}/status",
            headers={"X-Client-Timezone": "Europe/Kyiv"},
        )

        assert resp.status_code == 200, resp.text
        energy = resp.json()["energy"]
        assert energy["yesterday"] == 4.0, f"the route did not derive yesterday: {energy}"


class TestTodayStillWorks:
    """Guards the shared ``_snapshot_at_or_before`` helper both now use."""

    async def test_it_is_the_live_counter_minus_this_midnight(self, db_session):
        plug_id = await _plug(db_session)
        t_start = _local_midnight(0)
        await _snapshots(
            db_session,
            plug_id,
            [(t_start - timedelta(minutes=15), 20.0), (t_start + timedelta(hours=1), 20.5)],
        )

        assert await _today_from_snapshots(db_session, plug_id, 22.5, _request()) == 2.5

    async def test_without_a_live_counter_there_is_no_answer(self, db_session):
        plug_id = await _plug(db_session)
        await _snapshots(db_session, plug_id, [(_local_midnight(0) - timedelta(minutes=5), 20.0)])

        assert await _today_from_snapshots(db_session, plug_id, None, _request()) is None
