"""Reviewed JSON metadata boundaries, independent of database coercion."""

import math
from datetime import datetime

import pytest

from backend.app.services.git_restore import _parse_dt
from backend.app.services.git_restore_fields import (
    INVALID,
    MISSING,
    NUMERIC_FIELDS,
    SPOOL_FIELDS,
    read_field,
    source_id,
)


@pytest.mark.parametrize("key", list(NUMERIC_FIELDS))
@pytest.mark.parametrize("bad", [True, False, "12.5", [], {}, -1, float("nan"), float("inf"), -float("inf"), 10**400])
def test_numeric_rules_reject_coercion_and_nonfinite_values(key, bad):
    assert read_field({"v": bad}, "v", NUMERIC_FIELDS[key], _parse_dt) is INVALID


@pytest.mark.parametrize("key", list(NUMERIC_FIELDS))
def test_numeric_presence_zero_and_endpoints(key):
    rule = NUMERIC_FIELDS[key]
    assert read_field({}, "v", rule, _parse_dt) is MISSING
    assert read_field({"v": None}, "v", rule, _parse_dt) is (None if rule.nullable else INVALID)
    zero = read_field({"v": -0.0}, "v", rule, _parse_dt)
    assert zero == 0 and math.copysign(1, zero) == 1
    assert read_field({"v": rule.limit}, "v", rule, _parse_dt) == rule.limit
    above = rule.limit + 1 if type(rule.limit) is int else math.nextafter(rule.limit, math.inf)
    assert read_field({"v": above}, "v", rule, _parse_dt) is INVALID
    parsed = read_field({"v": 1.0}, "v", rule, _parse_dt)
    assert parsed == 1 and type(parsed) is (int if rule.kind == "integer" else float)
    fraction = read_field({"v": 1.5}, "v", rule, _parse_dt)
    assert fraction is INVALID if rule.kind == "integer" else fraction == 1.5


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.0, "1", None, [], {}, 2147483648, 10**400])
def test_source_ids_never_coerce(bad):
    assert source_id(bad) is INVALID


@pytest.mark.parametrize("good", [1, 2147483647])
def test_source_ids_fit_both_database_dialects(good):
    assert source_id(good) == good


@pytest.mark.parametrize("key", [k for k, r in SPOOL_FIELDS.items() if r.kind == "str"])
def test_string_width_unicode_and_presence(key):
    rule = SPOOL_FIELDS[key]
    assert read_field({}, key, rule, _parse_dt) is MISSING
    assert read_field({key: "Ж" * rule.limit}, key, rule, _parse_dt) == "Ж" * rule.limit
    for bad in ["x" * (rule.limit + 1), "nul\0inside", "\ud800", 1, True, [], {}]:
        assert read_field({key: bad}, key, rule, _parse_dt) is INVALID
    assert read_field({key: None}, key, rule, _parse_dt) is (None if rule.nullable else INVALID)
    empty = read_field({key: ""}, key, rule, _parse_dt)
    assert empty is INVALID if rule.nonempty else empty == ""


@pytest.mark.parametrize("key", ["weight_locked"])
def test_boolean_is_not_truthiness(key):
    rule = SPOOL_FIELDS[key]
    for good in [False, True]:
        assert read_field({key: good}, key, rule, _parse_dt) is good
    for bad in [None, 0, 1, "false", "true", [], {}]:
        assert read_field({key: bad}, key, rule, _parse_dt) is INVALID


@pytest.mark.parametrize(
    "stamp", ["2026-10-02T13:00:00", "2026-10-02T15:00:00+02:00", "2026-10-02T09:30:00-03:30", "2026-10-02T13:00:00Z"]
)
def test_datetime_keeps_utc_instant(stamp):
    assert read_field({"last_used": stamp}, "last_used", SPOOL_FIELDS["last_used"], _parse_dt) == datetime(
        2026, 10, 2, 13
    )


@pytest.mark.parametrize("stamp", ["", "garbage", "0001-01-01T00:00:00+14:00", 12, {}, [], datetime(2026, 1, 1)])
def test_datetime_invalid_is_not_clear_or_exception(stamp):
    assert read_field({"last_used": stamp}, "last_used", SPOOL_FIELDS["last_used"], _parse_dt) is INVALID


def test_numeric_inventory_matches_reviewed_models_and_literal_defaults():
    from sqlalchemy import Float, Integer

    from backend.app.models.archive import PrintArchive
    from backend.app.models.spool import Spool
    from backend.app.models.spool_usage_history import SpoolUsageHistory

    models = {"archives": PrintArchive, "spools": Spool, "usage": SpoolUsageHistory}
    assert len(NUMERIC_FIELDS) == 22
    for (domain, key), rule in NUMERIC_FIELDS.items():
        column = models[domain].__table__.c[key]
        assert isinstance(column.type, Integer if rule.kind == "integer" else Float)
        assert column.nullable is rule.nullable
    assert {k: r.default for (d, k), r in NUMERIC_FIELDS.items() if not r.nullable} == {
        "file_size": 0,
        "quantity": 1,
        "label_weight": 1000,
        "core_weight": 250,
        "weight_used": 0,
        "weight_used_baseline": 0,
        "percent_used": 0,
    }
