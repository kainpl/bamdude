"""The electricity price over time (m245): the price of a moment, the cost of counter steps, the one
writer of the history, and the Home Assistant reader -- with Home Assistant mocked, never asked."""

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from backend.app.models.energy_price_history import EnergyPriceHistory
from backend.app.models.settings import Settings
from backend.app.services import energy_price
from backend.app.services.energy_price import (
    BASELINE_AT,
    PricePoint,
    PriceReadFailed,
    PriceTarget,
    average_price,
    cost_of_steps,
    price_at,
    price_from_state,
    price_of,
)

T0 = datetime(2026, 3, 1, 0, 0, 0)
HISTORY = [PricePoint(BASELINE_AT, 0.10), PricePoint(T0 + timedelta(hours=10), 0.30)]


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)


# ---- the price of a moment ---------------------------------------------------------------------


def test_the_price_of_a_moment_is_the_newest_change_at_or_before_it():
    assert price_at(HISTORY, at(9.99)) == 0.10
    assert price_at(HISTORY, at(10)) == 0.30  # the change's own instant already costs the new price
    assert price_at(HISTORY, at(500)) == 0.30
    assert price_at(HISTORY, datetime(1999, 1, 1)) == 0.10  # everything before the upgrade: the baseline


def test_a_moment_before_the_first_point_takes_the_first_price_and_no_history_the_default():
    assert price_at([PricePoint(at(5), 0.4)], at(1)) == 0.4
    assert price_at([], at(1)) == energy_price.DEFAULT_PRICE


def test_the_average_price_of_a_window_is_weighted_by_time():
    assert average_price(HISTORY, at(8), at(12)) == pytest.approx(0.20)  # two hours each side
    assert average_price(HISTORY, at(9), at(13)) == pytest.approx(0.25)  # one hour at 0.10, three at 0.30
    assert average_price(HISTORY, at(1), at(2)) == pytest.approx(0.10)
    assert average_price(HISTORY, at(12), at(12)) == 0.30  # an empty window: the price at its start


# ---- the cost of counter steps -----------------------------------------------------------------


def test_each_step_costs_the_price_in_effect_when_it_started():
    points = [(at(8), 100.0), (at(9), 101.0), (at(11), 103.0), (at(12), 104.0)]
    steps = cost_of_steps(points, HISTORY)
    # 100->101 and 101->103 start before the change (0.10); 103->104 starts after it (0.30).
    assert steps.kwh == pytest.approx(4.0)
    assert steps.cost == pytest.approx(1 * 0.10 + 2 * 0.10 + 1 * 0.30)


def test_a_backward_step_is_a_reset_and_contributes_neither_energy_nor_cost():
    points = [(at(1), 100.0), (at(2), 101.0), (at(3), 0.5), (at(4), 1.5)]
    steps = cost_of_steps(points, HISTORY)
    assert steps.kwh == pytest.approx(2.0)  # 100->101 and 0.5->1.5; the fall to 0.5 is nothing
    assert steps.cost == pytest.approx(0.20)


def test_one_point_or_none_is_no_step():
    assert cost_of_steps([], HISTORY) == energy_price.NO_STEPS
    assert cost_of_steps([(at(1), 5.0)], HISTORY) == energy_price.NO_STEPS


def test_a_figure_is_costed_at_the_average_price_of_its_steps_or_the_fallback():
    steps = cost_of_steps([(at(9), 0.0), (at(11), 1.0), (at(12), 2.0)], HISTORY)
    assert price_of(steps, 9.9) == pytest.approx(0.20)
    assert price_of(energy_price.NO_STEPS, 0.33) == 0.33


def test_an_unreadable_price_reads_as_the_default_and_a_negative_one_as_free():
    assert energy_price.clean_price("0.25") == 0.25
    assert energy_price.clean_price("None") == energy_price.DEFAULT_PRICE
    assert energy_price.clean_price("nan") == energy_price.DEFAULT_PRICE
    assert energy_price.clean_price(-0.05) == 0.0


# ---- the one writer ----------------------------------------------------------------------------


async def _rows(db):
    return (
        await db.execute(
            select(
                EnergyPriceHistory.price_per_kwh, EnergyPriceHistory.source, EnergyPriceHistory.effective_at
            ).order_by(EnergyPriceHistory.effective_at, EnergyPriceHistory.id)
        )
    ).all()


@pytest.mark.asyncio
async def test_a_change_is_appended_only_when_the_price_differs_from_the_standing_one(db_session):
    db_session.add(EnergyPriceHistory(effective_at=BASELINE_AT, price_per_kwh=0.15, source="baseline"))
    await db_session.commit()

    assert await energy_price.record_change(db_session, 0.15, "manual") is False
    assert await energy_price.record_change(db_session, 0.20, "manual", at=at(1)) is True
    assert await energy_price.record_change(db_session, 0.20, "api", at=at(2)) is False  # the standing price
    assert await energy_price.record_change(db_session, 0.15, "homeassistant", at=at(3)) is True

    assert [(price, source) for price, source, _at in await _rows(db_session)] == [
        (0.15, "baseline"),
        (0.20, "manual"),
        (0.15, "homeassistant"),
    ]


@pytest.mark.asyncio
async def test_an_empty_table_gets_its_baseline_from_the_price_that_stands(db_session):
    db_session.add(Settings(key="energy_cost_per_kwh", value="0.42"))
    await db_session.commit()

    assert await energy_price.record_change(db_session, 0.50, "manual", at=at(1)) is True

    rows = await _rows(db_session)
    assert [(price, source) for price, source, _at in rows] == [(0.42, "baseline"), (0.50, "manual")]
    assert rows[0][2] == BASELINE_AT
    history = await energy_price.load_history(db_session)
    assert price_at(history, at(0)) == 0.42 and price_at(history, at(2)) == 0.50


@pytest.mark.asyncio
async def test_a_change_dated_before_the_newest_row_is_placed_right_after_it(db_session):
    db_session.add(EnergyPriceHistory(effective_at=BASELINE_AT, price_per_kwh=0.15, source="baseline"))
    db_session.add(EnergyPriceHistory(effective_at=at(5), price_per_kwh=0.20, source="manual"))
    await db_session.commit()

    await energy_price.record_change(db_session, 0.30, "manual", at=at(4))  # the clock went back

    rows = await _rows(db_session)
    assert [price for price, _source, _at in rows] == [0.15, 0.20, 0.30]
    assert rows[2][2] == at(5) + timedelta(microseconds=1)


@pytest.mark.asyncio
async def test_a_baseline_or_an_unknown_source_is_not_a_change(db_session):
    for source in ("baseline", "somebody"):
        with pytest.raises(ValueError):
            await energy_price.record_change(db_session, 0.2, source)


@pytest.mark.asyncio
async def test_an_empty_history_reads_as_the_stored_price_for_every_moment(db_session):
    db_session.add(Settings(key="energy_cost_per_kwh", value="0.33"))
    await db_session.commit()
    assert await energy_price.load_history(db_session) == [PricePoint(BASELINE_AT, 0.33)]


@pytest.mark.asyncio
async def test_set_price_records_the_change_and_stores_the_price(db_session):
    assert await energy_price.set_price(db_session, 0.27, "homeassistant") == 0.27
    await db_session.commit()
    assert await energy_price.stored_price(db_session) == 0.27
    assert [(price, source) for price, source, _at in await _rows(db_session)] == [
        (0.15, "baseline"),
        (0.27, "homeassistant"),
    ]


# ---- the price from Home Assistant -------------------------------------------------------------


def _state(value, unit="EUR/kWh"):
    return {"state": value, "attributes": {"unit_of_measurement": unit}}


def test_a_sensor_price_is_scaled_to_per_kwh_by_its_unit():
    assert price_from_state(_state("0.25")) == 0.25
    assert price_from_state(_state("250", "EUR/MWh")) == pytest.approx(0.25)
    assert price_from_state(_state("0.00025", "€/Wh")) == pytest.approx(0.25)
    assert price_from_state(_state("0.25", "UAH")) == 0.25  # no "/unit": taken as per kWh
    assert price_from_state({"state": "0.25"}) == 0.25


def test_a_negative_sensor_price_is_taken_as_free():
    assert price_from_state(_state("-0.04")) == 0.0
    assert price_from_state(_state("-40", "EUR/MWh")) == 0.0


@pytest.mark.parametrize("value", ["unavailable", "unknown", "", None, "abc", "nan", "inf", "-inf", True])
def test_a_state_that_is_no_finite_number_is_a_failed_read(value):
    with pytest.raises(PriceReadFailed) as error:
        price_from_state(_state(value))
    assert error.value.reason == "unusable"


def test_no_state_object_is_a_failed_read():
    for state in (None, [], "0.2"):
        with pytest.raises(PriceReadFailed):
            price_from_state(state)


TARGET = PriceTarget(url="http://ha.test:8123", token="secret-token-canary", entity="sensor.price")


@pytest.mark.asyncio
async def test_the_read_goes_through_the_home_assistant_client_with_a_five_second_timeout():
    read = AsyncMock(return_value=_state("300", "EUR/MWh"))
    with patch("backend.app.services.homeassistant.homeassistant_service.read_state", read):
        assert await energy_price.fetch_price(TARGET) == pytest.approx(0.30)
    read.assert_awaited_once_with("http://ha.test:8123", "secret-token-canary", "sensor.price", timeout=5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (httpx.ReadTimeout("timed out"), "timeout"),
        (httpx.ConnectError("refused http://ha.test:8123"), "unreachable"),
        (
            httpx.HTTPStatusError("404", request=httpx.Request("GET", "http://ha.test"), response=httpx.Response(404)),
            "no_such_entity",
        ),
        (
            httpx.HTTPStatusError("401", request=httpx.Request("GET", "http://ha.test"), response=httpx.Response(401)),
            "unreachable",
        ),
        (ValueError("Invalid Home Assistant URL"), "unreachable"),
    ],
)
async def test_a_transport_failure_is_a_failed_read_with_a_closed_reason(raised, reason):
    failing = patch(
        "backend.app.services.homeassistant.homeassistant_service.read_state", AsyncMock(side_effect=raised)
    )
    with failing, pytest.raises(PriceReadFailed) as error:
        await energy_price.fetch_price(TARGET)
    assert error.value.reason == reason
    assert "ha.test" not in str(error.value) and "secret" not in repr(TARGET)


class _Sessions:
    """``async with factory() as db`` over the test's own session, counting what is open."""

    def __init__(self, session):
        self.session = session
        self.open = 0
        self.opened = 0

    def __call__(self):
        return self

    async def __aenter__(self):
        self.open += 1
        self.opened += 1
        return self.session

    async def __aexit__(self, *exc):
        self.open -= 1
        await self.session.rollback()  # what closing a real session does to an open transaction
        return False


async def _homeassistant_source(db, *, price="0.20", entity="sensor.price"):
    for key, value in {
        "energy_cost_per_kwh": price,
        "energy_price_source": "homeassistant",
        "energy_price_ha_entity": entity,
        "ha_enabled": "true",
        "ha_url": "http://ha.test:8123",
        "ha_token": "secret-token-canary",
    }.items():
        db.add(Settings(key=key, value=value))
    db.add(EnergyPriceHistory(effective_at=BASELINE_AT, price_per_kwh=float(price), source="baseline"))
    await db.commit()


@pytest.mark.asyncio
async def test_a_scheduled_read_stores_the_price_and_records_its_change(db_session, monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await _homeassistant_source(db_session)
    sessions = _Sessions(db_session)
    seen = {}

    async def read(url, token, entity, *, timeout):
        # The destination was frozen and its transaction closed BEFORE the network is asked.
        seen.update(open=sessions.open, in_transaction=db_session.in_transaction(), target=(url, token, entity))
        return _state("0.31")

    with patch("backend.app.services.homeassistant.homeassistant_service.read_state", read):
        assert await energy_price.refresh_from_homeassistant(sessions) == 0.31

    assert seen == {
        "open": 0,
        "in_transaction": False,
        "target": ("http://ha.test:8123", "secret-token-canary", "sensor.price"),
    }
    assert sessions.opened == 2  # one short read, one short write
    assert await energy_price.stored_price(db_session) == 0.31
    assert [(price, source) for price, source, _at in await _rows(db_session)] == [
        (0.20, "baseline"),
        (0.31, "homeassistant"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [httpx.ReadTimeout("timed out"), httpx.ConnectError("down"), _state("unavailable"), _state("unknown")],
    ids=["timeout", "unreachable", "unavailable", "unknown"],
)
async def test_a_failed_read_keeps_the_last_price_and_warns_once_without_url_or_token(
    db_session, monkeypatch, caplog, answer
):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await _homeassistant_source(db_session)
    read = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer)

    with (
        caplog.at_level(logging.DEBUG),
        patch("backend.app.services.homeassistant.homeassistant_service.read_state", read),
    ):
        assert await energy_price.refresh_from_homeassistant(_Sessions(db_session)) is None

    assert await energy_price.stored_price(db_session) == 0.20
    assert len(await _rows(db_session)) == 1
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ha.test" not in caplog.text and "secret-token-canary" not in caplog.text


@pytest.mark.asyncio
async def test_a_fixed_price_never_asks_home_assistant(db_session):
    db_session.add(Settings(key="energy_price_source", value="fixed"))
    db_session.add(Settings(key="energy_price_ha_entity", value="sensor.price"))
    await db_session.commit()
    read = AsyncMock()
    with patch("backend.app.services.homeassistant.homeassistant_service.read_state", read):
        assert await energy_price.refresh_from_homeassistant(_Sessions(db_session)) is None
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_source_reconfigured_during_the_read_gets_no_price_from_it(db_session, monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await _homeassistant_source(db_session)

    async def read(url, token, entity, *, timeout):
        row = (await db_session.execute(select(Settings).where(Settings.key == "energy_price_ha_entity"))).scalar_one()
        row.value = "sensor.another"
        await db_session.commit()
        return _state("0.99")

    with patch("backend.app.services.homeassistant.homeassistant_service.read_state", read):
        assert await energy_price.refresh_from_homeassistant(_Sessions(db_session)) is None
    assert await energy_price.stored_price(db_session) == 0.20
    assert len(await _rows(db_session)) == 1


@pytest.mark.asyncio
async def test_the_hourly_loop_reads_the_price_before_that_ticks_snapshots(monkeypatch):
    from backend.app.services import smart_plug_manager as manager_module

    order = []

    async def refresh():
        order.append("price")

    async def capture():
        order.append("snapshots")

    class _Stop(BaseException):
        pass

    async def sleep(seconds):
        if seconds == 3600:
            raise _Stop

    monkeypatch.setattr(energy_price, "refresh_from_homeassistant", refresh)
    monkeypatch.setattr(manager_module.smart_plug_manager, "_capture_energy_snapshots", capture)
    monkeypatch.setattr(manager_module.asyncio, "sleep", sleep)
    with pytest.raises(_Stop):
        await manager_module.smart_plug_manager._snapshot_loop()
    assert order == ["price", "snapshots"]
