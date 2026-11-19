"""Every door that changes ``energy_cost_per_kwh`` records the change (m245), and the settings save's
dealings with a Home Assistant price sensor -- Home Assistant itself mocked, never asked."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.api.routes import settings as settings_routes
from backend.app.models.energy_price_history import EnergyPriceHistory
from backend.app.models.settings import Settings
from backend.app.schemas.settings import AppSettingsUpdate
from backend.app.services import energy_price
from backend.app.services.energy_price import PriceReadFailed, PriceTarget

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN = SimpleNamespace(id=1, is_admin=True)


@pytest.fixture(autouse=True)
def _no_ha_environment(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)


async def _history(db):
    return [
        (price, source)
        for price, source in (
            await db.execute(
                select(EnergyPriceHistory.price_per_kwh, EnergyPriceHistory.source).order_by(
                    EnergyPriceHistory.effective_at, EnergyPriceHistory.id
                )
            )
        ).all()
    ]


async def _stored(db, key):
    return await db.scalar(select(Settings.value).where(Settings.key == key).execution_options(populate_existing=True))


async def _seed(db, values: dict):
    for key, value in values.items():
        db.add(Settings(key=key, value=value))
    await db.commit()


async def _save(db, **fields):
    return await settings_routes.update_settings(AppSettingsUpdate(**fields), db, ADMIN)


HA_CONNECTION = {"ha_enabled": "true", "ha_url": "http://ha.test:8123", "ha_token": "secret-token-canary"}


# ---- the doors ---------------------------------------------------------------------------------


async def test_a_settings_save_records_the_change_and_a_repeat_of_the_same_price_does_not(async_client, db_session):
    for _ in range(2):
        response = await async_client.put("/api/v1/settings/", json={"energy_cost_per_kwh": 0.20})
        assert response.status_code == 200, response.text
    response = await async_client.patch("/api/v1/settings", json={"energy_cost_per_kwh": 0.25})
    assert response.status_code == 200, response.text

    # The baseline is the price that stood before the first change (here the default).
    assert await _history(db_session) == [(0.15, "baseline"), (0.20, "manual"), (0.25, "manual")]


async def test_a_save_of_other_settings_records_nothing(async_client, db_session):
    assert (await async_client.put("/api/v1/settings/", json={"currency": "EUR"})).status_code == 200
    assert await _history(db_session) == []


async def test_the_external_push_records_its_change_as_the_apis(async_client, db_session):
    response = await async_client.post("/api/v1/settings/electricity-price", json={"energy_cost_per_kwh": 0.31})
    assert response.status_code == 200, response.text
    assert response.json()["energy_cost_per_kwh"] == 0.31
    assert await _history(db_session) == [(0.15, "baseline"), (0.31, "api")]


async def test_a_reset_records_the_return_to_the_default_and_keeps_the_history(async_client, db_session):
    assert (await async_client.put("/api/v1/settings/", json={"energy_cost_per_kwh": 0.40})).status_code == 200

    with patch.object(settings_routes, "_apply_mqtt_runtime", AsyncMock()):
        await settings_routes.reset_settings(db_session, ADMIN)

    assert await _stored(db_session, "energy_cost_per_kwh") is None
    assert await _history(db_session) == [(0.15, "baseline"), (0.40, "manual"), (0.15, "manual")]


async def test_a_git_restore_of_the_price_records_its_change_as_the_restores(db_session):
    from backend.app.services.git_restore import _CategoryTally, git_restore_service

    await _seed(db_session, {"energy_cost_per_kwh": "0.20"})

    await git_restore_service._restore_settings(
        db_session,
        {"settings": {"energy_cost_per_kwh": "0.55", "currency": "EUR"}},
        overwrite=True,
        tally=_CategoryTally(),
    )
    await db_session.commit()

    assert await _stored(db_session, "energy_cost_per_kwh") == "0.55"
    assert await _history(db_session) == [(0.20, "baseline"), (0.55, "restore")]


async def test_a_git_restore_that_keeps_the_local_price_records_nothing(db_session):
    from backend.app.services.git_restore import _CategoryTally, git_restore_service

    await _seed(db_session, {"energy_cost_per_kwh": "0.20"})

    await git_restore_service._restore_settings(
        db_session, {"settings": {"energy_cost_per_kwh": "0.55"}}, overwrite=False, tally=_CategoryTally()
    )
    await db_session.commit()

    assert await _stored(db_session, "energy_cost_per_kwh") == "0.20"
    assert await _history(db_session) == []


# ---- the price from Home Assistant, at the settings save ---------------------------------------


async def test_choosing_the_sensor_reads_it_at_once_and_saves_its_price(db_session):
    await _seed(db_session, {**HA_CONNECTION, "energy_cost_per_kwh": "0.20"})
    seen = {}

    async def fetch(target):
        # Nothing of the save is written, and no transaction is open, while the network is asked.
        seen.update(target=target, in_transaction=db_session.in_transaction())
        return 0.37

    with patch.object(energy_price, "fetch_price", fetch):
        answer = await _save(db_session, energy_price_source="homeassistant", energy_price_ha_entity="sensor.price")

    assert seen == {
        "target": PriceTarget(url="http://ha.test:8123", token="secret-token-canary", entity="sensor.price"),
        "in_transaction": False,
    }
    assert (answer.energy_price_source, answer.energy_price_ha_entity, answer.energy_cost_per_kwh) == (
        "homeassistant",
        "sensor.price",
        0.37,
    )
    assert await _history(db_session) == [(0.20, "baseline"), (0.37, "homeassistant")]


@pytest.mark.parametrize(
    ("reason", "status"),
    [("timeout", 504), ("unreachable", 502), ("unusable", 422), ("no_such_entity", 422)],
)
async def test_a_read_that_fails_refuses_the_save_and_nothing_is_written(db_session, reason, status):
    await _seed(db_session, {**HA_CONNECTION, "energy_cost_per_kwh": "0.20"})

    failing = patch.object(energy_price, "fetch_price", AsyncMock(side_effect=PriceReadFailed(reason)))
    with failing, pytest.raises(HTTPException) as refusal:
        await _save(
            db_session, energy_price_source="homeassistant", energy_price_ha_entity="sensor.price", currency="EUR"
        )
    await db_session.rollback()

    assert refusal.value.status_code == status
    assert refusal.value.detail["error"] == "energy_price_read_failed"
    assert await _stored(db_session, "energy_price_source") is None
    assert await _stored(db_session, "currency") is None
    assert await _stored(db_session, "energy_cost_per_kwh") == "0.20"
    assert await _history(db_session) == []


async def test_the_sensor_cannot_be_chosen_without_a_home_assistant_connection_or_an_entity(db_session):
    fetch = AsyncMock()
    with patch.object(energy_price, "fetch_price", fetch):
        with pytest.raises(HTTPException) as no_connection:
            await _save(db_session, energy_price_source="homeassistant", energy_price_ha_entity="sensor.price")
        await db_session.rollback()
        await _seed(db_session, HA_CONNECTION)
        with pytest.raises(HTTPException) as no_entity:
            await _save(db_session, energy_price_source="homeassistant")
        await db_session.rollback()

    assert (no_connection.value.status_code, no_entity.value.status_code) == (400, 422)
    assert no_connection.value.detail["error"] == no_entity.value.detail["error"] == "energy_price_read_failed"
    fetch.assert_not_awaited()


async def test_an_unchanged_sensor_is_not_read_again_by_an_ordinary_save(db_session):
    await _seed(
        db_session,
        {**HA_CONNECTION, "energy_price_source": "homeassistant", "energy_price_ha_entity": "sensor.price"},
    )
    fetch = AsyncMock()
    with patch.object(energy_price, "fetch_price", fetch):
        # The page sends its whole form back, the source and the sensor included.
        await _save(
            db_session, energy_price_source="homeassistant", energy_price_ha_entity="sensor.price", currency="EUR"
        )
    fetch.assert_not_awaited()
    assert await _stored(db_session, "currency") == "EUR"


async def test_a_manual_price_is_refused_while_the_sensor_is_the_source(async_client, db_session):
    await _seed(
        db_session,
        {
            **HA_CONNECTION,
            "energy_price_source": "homeassistant",
            "energy_price_ha_entity": "sensor.price",
            "energy_cost_per_kwh": "0.20",
        },
    )

    response = await async_client.put("/api/v1/settings/", json={"energy_cost_per_kwh": 0.99, "currency": "EUR"})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "energy_price_from_homeassistant" and detail["message"]
    assert await _stored(db_session, "energy_cost_per_kwh") == "0.20"
    assert await _stored(db_session, "currency") is None
    assert await _history(db_session) == []


async def test_the_standing_price_sent_back_with_a_full_form_is_dropped_not_refused(async_client, db_session):
    await _seed(
        db_session,
        {
            **HA_CONNECTION,
            "energy_price_source": "homeassistant",
            "energy_price_ha_entity": "sensor.price",
            "energy_cost_per_kwh": "0.20",
        },
    )
    response = await async_client.put("/api/v1/settings/", json={"energy_cost_per_kwh": 0.20, "currency": "EUR"})
    assert response.status_code == 200, response.text
    assert await _history(db_session) == []


async def test_an_external_push_stays_allowed_while_the_sensor_is_the_source(async_client, db_session):
    await _seed(
        db_session,
        {
            **HA_CONNECTION,
            "energy_price_source": "homeassistant",
            "energy_price_ha_entity": "sensor.price",
            "energy_cost_per_kwh": "0.20",
        },
    )
    response = await async_client.post("/api/v1/settings/electricity-price", json={"energy_cost_per_kwh": 0.26})
    assert response.status_code == 200, response.text
    assert await _history(db_session) == [(0.20, "baseline"), (0.26, "api")]


async def test_going_back_to_a_fixed_price_takes_a_manual_price_again(db_session):
    await _seed(
        db_session,
        {
            **HA_CONNECTION,
            "energy_price_source": "homeassistant",
            "energy_price_ha_entity": "sensor.price",
            "energy_cost_per_kwh": "0.20",
        },
    )
    answer = await _save(db_session, energy_price_source="fixed", energy_cost_per_kwh=0.5)
    assert (answer.energy_price_source, answer.energy_cost_per_kwh) == ("fixed", 0.5)
    assert await _history(db_session) == [(0.20, "baseline"), (0.5, "manual")]


async def test_the_source_fields_take_no_null_and_no_entity_outside_their_shape(async_client):
    for body in (
        {"energy_price_source": None},
        {"energy_price_ha_entity": None},
        {"energy_price_source": "cloud"},
        {"energy_price_ha_entity": "sensor.price/../../config"},
        {"energy_price_ha_entity": "switch.heater"},
    ):
        response = await async_client.put("/api/v1/settings/", json=body)
        assert response.status_code == 422, (body, response.text)


async def test_a_non_admin_cannot_point_the_server_at_a_sensor(db_session):
    await _seed(db_session, HA_CONNECTION)
    operator = SimpleNamespace(id=2, is_admin=False)
    fetch = AsyncMock()
    with patch.object(energy_price, "fetch_price", fetch), pytest.raises(HTTPException) as refusal:
        await settings_routes.update_settings(
            AppSettingsUpdate(energy_price_source="homeassistant", energy_price_ha_entity="sensor.price"),
            db_session,
            operator,
        )
    assert refusal.value.status_code == 403
    fetch.assert_not_awaited()
