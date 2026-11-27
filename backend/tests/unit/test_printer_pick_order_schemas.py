"""The two writes C01 adds: a printer's priority and the AutoQueue printer order setting (upstream #2915)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.schemas.printer import PrinterCreate, PrinterResponse, PrinterUpdate
from backend.app.schemas.settings import AppSettings, AppSettingsUpdate
from backend.app.services import git_restore

ORDERS = ("fewest_hours", "most_hours", "least_recently_used", "priority")


def test_priority_bounds():
    assert PrinterUpdate(dispatch_priority=0).dispatch_priority == 0
    assert PrinterUpdate(dispatch_priority=100000).dispatch_priority == 100000
    for value in (-1, 100001):
        with pytest.raises(ValidationError):
            PrinterUpdate(dispatch_priority=value)


def test_a_null_priority_clears_it():
    body = PrinterUpdate.model_validate({"dispatch_priority": None})
    assert body.model_dump(exclude_unset=True) == {"dispatch_priority": None}


def test_create_has_no_priority_and_the_response_shows_it():
    assert "dispatch_priority" not in PrinterCreate.model_fields
    assert "dispatch_priority" in PrinterResponse.model_fields
    assert PrinterResponse.model_fields["dispatch_priority"].default is None


def test_the_order_setting_accepts_four_values():
    assert AppSettings().model_dispatch_order == "fewest_hours"
    for order in ORDERS:
        assert AppSettings(model_dispatch_order=order).model_dispatch_order == order
        assert AppSettingsUpdate(model_dispatch_order=order).model_dispatch_order == order


def test_the_order_setting_refuses_another_and_null():
    with pytest.raises(ValidationError):
        AppSettingsUpdate(model_dispatch_order="newest")
    with pytest.raises(ValidationError):
        AppSettingsUpdate.model_validate({"model_dispatch_order": None})
    assert "model_dispatch_order" not in AppSettingsUpdate().model_dump(exclude_unset=True)


def test_the_order_setting_is_restored_from_git():
    assert "model_dispatch_order" in git_restore._RESTORABLE_SETTING_KEYS
