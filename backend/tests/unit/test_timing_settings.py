"""Both timing thresholds through the full settings path.

⚠️ The int list in ``get_settings`` is the step everyone forgets. Skipping it is
why ``plug_power_sample_seconds`` still comes back as a string to this day — it
is an int in both schemas and works only because its one consumer re-parses
defensively. A new int tunable that skips the list fails exactly as quietly.
"""

import inspect

import pytest

from backend.app.api.routes import settings as settings_routes
from backend.app.core import query_timing as qt
from backend.app.schemas.settings import AppSettings, AppSettingsUpdate


@pytest.fixture(autouse=True)
def _thresholds_off():
    qt.set_query_threshold_ms(0)
    qt.set_request_threshold_ms(0)
    yield
    qt.set_query_threshold_ms(0)
    qt.set_request_threshold_ms(0)


def test_the_schemas_carry_both_thresholds_and_default_to_off():
    assert AppSettings().slow_query_ms == 0
    assert AppSettings().slow_request_ms == 0
    assert AppSettingsUpdate(slow_query_ms=250).slow_query_ms == 250
    assert AppSettingsUpdate().slow_query_ms is None


@pytest.mark.parametrize("field", ["slow_query_ms", "slow_request_ms"])
def test_a_negative_threshold_is_refused(field):
    with pytest.raises(ValueError):
        AppSettingsUpdate(**{field: -1})


@pytest.mark.parametrize("key", ["slow_query_ms", "slow_request_ms"])
def test_both_keys_are_parsed_as_integers_not_strings(key):
    """The rows are stored as text; without a place in the int list the API
    would answer a string and the number input would silently misbehave."""
    source = inspect.getsource(settings_routes.get_settings)
    int_block = source.split("float(setting.value)", 1)[1]
    assert f'"{key}"' in int_block.split("int(setting.value)", 1)[0]


@pytest.mark.asyncio
async def test_saving_applies_the_thresholds_without_a_restart(async_client):
    response = await async_client.put("/api/v1/settings/", json={"slow_query_ms": 250, "slow_request_ms": 3000})
    assert response.status_code == 200
    assert qt.query_threshold_ms() == 250
    assert qt.request_threshold_ms() == 3000


@pytest.mark.asyncio
async def test_zero_is_a_real_value_and_turns_it_back_off(async_client):
    """⚠️ 0 must survive the round trip. The falsy-zero trap has bitten this
    codebase before — a stagger interval of 0 became 5 because of `|| 5`."""
    await async_client.put("/api/v1/settings/", json={"slow_query_ms": 250})
    assert qt.query_threshold_ms() == 250

    response = await async_client.put("/api/v1/settings/", json={"slow_query_ms": 0})
    assert response.status_code == 200
    assert qt.query_threshold_ms() == 0
    assert (await async_client.get("/api/v1/settings/")).json()["slow_query_ms"] == 0


@pytest.mark.asyncio
async def test_the_stored_value_comes_back_as_an_integer(async_client):
    await async_client.put("/api/v1/settings/", json={"slow_request_ms": 1500})
    value = (await async_client.get("/api/v1/settings/")).json()["slow_request_ms"]
    assert value == 1500
    assert isinstance(value, int)
