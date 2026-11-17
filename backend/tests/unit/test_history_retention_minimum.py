"""A history retention can never be less than one day.

Zero or a negative number put the sweep's cutoff at or after now, and the daily
sweep deleted the whole AMS / printer-sensor history (upstream 346b58ed8).
"""

import pytest
from pydantic import ValidationError

from backend.app.main import _retention_days
from backend.app.schemas.settings import AppSettingsUpdate


@pytest.mark.parametrize("raw", ["0", "-5", 0, -1, "", "abc", None])
def test_a_value_that_would_wipe_history_reads_as_the_default(raw):
    assert _retention_days(raw, 30) == 30


@pytest.mark.parametrize(("raw", "days"), [("1", 1), ("90", 90), (7, 7), (" 14 ", 14)])
def test_a_real_retention_is_kept(raw, days):
    assert _retention_days(raw, 30) == days


@pytest.mark.parametrize("field", ["ams_history_retention_days", "printer_sensor_history_retention_days"])
@pytest.mark.parametrize("value", [0, -1])
def test_a_save_below_one_day_is_refused(field, value):
    with pytest.raises(ValidationError):
        AppSettingsUpdate(**{field: value})


@pytest.mark.parametrize("field", ["ams_history_retention_days", "printer_sensor_history_retention_days"])
def test_one_day_is_accepted(field):
    assert getattr(AppSettingsUpdate(**{field: 1}), field) == 1
