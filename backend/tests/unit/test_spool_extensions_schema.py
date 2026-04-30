"""Schema-level smoke tests for the B.1 + B.8 spool fields.

We're not exercising the migration here (covered structurally by the unique
``add_column`` helper) — we're verifying the Pydantic boundary so a stray
typo or wrong range doesn't slip through silently. The four new fields are:

* ``extra_colors`` (str | None, max 255)
* ``effect_type`` (str | None, max 20)
* ``category`` (str | None, max 50)
* ``low_stock_threshold_pct`` (int | None, 1..99)
"""

import pytest
from pydantic import ValidationError

from backend.app.schemas.spool import SpoolCreate, SpoolUpdate


def _base_payload(**overrides):
    payload = {
        "material": "PLA",
        "subtype": "Basic",
        "color_name": "Jade White",
        "rgba": "EAF5EAFF",
        "brand": "Polymaker",
    }
    payload.update(overrides)
    return payload


def test_spool_create_accepts_new_fields_when_set():
    s = SpoolCreate.model_validate(
        _base_payload(
            extra_colors="EAF5EA,FF6347AA,1234CD",
            effect_type="sparkle",
            category="Production",
            low_stock_threshold_pct=15,
        )
    )
    assert s.extra_colors == "EAF5EA,FF6347AA,1234CD"
    assert s.effect_type == "sparkle"
    assert s.category == "Production"
    assert s.low_stock_threshold_pct == 15


def test_spool_create_defaults_new_fields_to_none():
    s = SpoolCreate.model_validate(_base_payload())
    assert s.extra_colors is None
    assert s.effect_type is None
    assert s.category is None
    assert s.low_stock_threshold_pct is None


@pytest.mark.parametrize("bad", [0, 100, -5, 1000])
def test_spool_create_rejects_low_stock_out_of_range(bad):
    with pytest.raises(ValidationError):
        SpoolCreate.model_validate(_base_payload(low_stock_threshold_pct=bad))


def test_spool_update_accepts_partial_with_new_fields():
    u = SpoolUpdate.model_validate({"category": "Prototype", "low_stock_threshold_pct": 50})
    assert u.category == "Prototype"
    assert u.low_stock_threshold_pct == 50
    # Untouched fields stay None / unset
    assert u.material is None


def test_spool_create_enforces_max_length_on_string_fields():
    long_category = "x" * 51
    with pytest.raises(ValidationError):
        SpoolCreate.model_validate(_base_payload(category=long_category))

    long_effect = "y" * 21
    with pytest.raises(ValidationError):
        SpoolCreate.model_validate(_base_payload(effect_type=long_effect))

    long_extra = "z" * 256
    with pytest.raises(ValidationError):
        SpoolCreate.model_validate(_base_payload(extra_colors=long_extra))
