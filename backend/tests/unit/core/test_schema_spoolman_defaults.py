"""Only fixes m228's three exact constant defaults are compatible drift."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from backend.app.core.pg_schema_contract import default_key, retained_pairs, unexpected


def schema(defaults):
    return {
        "schema": {
            "spoolman_usage_history": {
                "columns": [
                    {"name": name, "default_raw": value, "default": default_key(value)}
                    for name, value in defaults.items()
                ],
                "pk": {},
                "foreign_keys": [],
                "unique": [],
                "check": [],
                "indexes": [],
            }
        },
        "extra": {},
        "null_residuals": {},
        "catalogue": {"foreign_keys": [], "indexes": [], "triggers": [], "functions": []},
    }


DEFAULTS = {"weight_used": "0", "percent_used": "0", "status": "'completed'::character varying"}


def test_only_three_exact_pairs_extend_the_frozen_b_contract():
    frozen = json.loads(
        (Path(__file__).parents[3] / "app/data/pg_schema_b_retained_drift_v1.json").read_text(encoding="utf-8")
    )
    assert len(frozen) == 152
    assert len(retained_pairs()) == len(frozen) + 3
    rows = [r for r in retained_pairs() if r["object"].startswith("spoolman_usage_history.")]
    assert {r["object"] for r in rows} == {f"spoolman_usage_history.column.{c}.default" for c in DEFAULTS}
    assert len(rows) == 3
    assert not unexpected(schema(dict.fromkeys(DEFAULTS)), schema(DEFAULTS))


@pytest.mark.parametrize(
    "column,value", [("weight_used", "1"), ("percent_used", "1"), ("status", "'pending'::character varying")]
)
def test_a_different_default_is_not_accepted(column, value):
    defaults = {**DEFAULTS, column: value}
    failures = unexpected(schema(dict.fromkeys(DEFAULTS)), schema(defaults))
    assert [r["object"] for r in failures] == [f"spoolman_usage_history.column.{column}.default"]


def test_missing_columns_changed_expected_default_and_other_fields_still_fail():
    reference = schema(dict.fromkeys(DEFAULTS))
    actual = schema(DEFAULTS)
    changed = deepcopy(actual)
    changed["schema"]["spoolman_usage_history"]["columns"].pop()
    assert unexpected(reference, changed)
    assert unexpected(schema({**dict.fromkeys(DEFAULTS), "weight_used": "2"}), actual)
    changed = deepcopy(actual)
    changed["schema"]["spoolman_usage_history"]["columns"][0]["nullable"] = True
    assert unexpected(reference, changed)
