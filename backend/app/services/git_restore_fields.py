"""Literal Git metadata rules; missing, invalid, null and zero stay distinct.

Archive and usage writers apply these rules only after proving row identity
and deciding precedence. Spool matching has its own reviewed identity rules.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

MISSING = object()
INVALID = object()


@dataclass(frozen=True)
class FieldRule:
    kind: Literal["integer", "float", "str", "bool", "datetime"]
    limit: int | float | None = None
    nullable: bool = True
    default: object = None
    nonempty: bool = False


# Bounds/defaults are reviewed literals, not inherited from future ORM models.
NUMERIC_FIELDS = {
    ("archives", "file_size"): FieldRule("integer", 2147483647, False, 0),
    ("archives", "print_time_seconds"): FieldRule("integer", 2147483647),
    ("archives", "total_layers"): FieldRule("integer", 2147483647),
    ("archives", "bed_temperature"): FieldRule("integer", 2147483647),
    ("archives", "nozzle_temperature"): FieldRule("integer", 2147483647),
    ("archives", "quantity"): FieldRule("integer", 10000, False, 1),
    ("archives", "filament_used_grams"): FieldRule("float", 100000),
    ("archives", "layer_height"): FieldRule("float", 1.7976931348623157e308),
    ("archives", "nozzle_diameter"): FieldRule("float", 1.7976931348623157e308),
    ("archives", "cost"): FieldRule("float", 1.7976931348623157e308),
    ("archives", "energy_kwh"): FieldRule("float", 1.7976931348623157e308),
    ("archives", "energy_cost"): FieldRule("float", 1.7976931348623157e308),
    ("spools", "label_weight"): FieldRule("integer", 100000, False, 1000),
    ("spools", "core_weight"): FieldRule("integer", 100000, False, 250),
    ("spools", "weight_used"): FieldRule("float", 100000, False, 0),
    ("spools", "weight_used_baseline"): FieldRule("float", 100000, False, 0),
    ("spools", "nozzle_temp_min"): FieldRule("integer", 2147483647),
    ("spools", "nozzle_temp_max"): FieldRule("integer", 2147483647),
    ("spools", "cost_per_kg"): FieldRule("float", 1.7976931348623157e308),
    ("usage", "weight_used"): FieldRule("float", 100000, False, 0),
    ("usage", "percent_used"): FieldRule("integer", 2147483647, False, 0),
    ("usage", "cost"): FieldRule("float", 1.7976931348623157e308),
}

SPOOL_FIELDS = {
    **{name: rule for (domain, name), rule in NUMERIC_FIELDS.items() if domain == "spools"},
    "material": FieldRule("str", 50, False, "PLA", nonempty=True),
    "subtype": FieldRule("str", 50),
    "color_name": FieldRule("str", 100),
    "rgba": FieldRule("str", 8),
    "brand": FieldRule("str", 100),
    "slicer_filament": FieldRule("str", 50),
    "slicer_filament_name": FieldRule("str", 100),
    "note": FieldRule("str", 500),
    "tag_uid": FieldRule("str", 32),
    "tray_uuid": FieldRule("str", 32),
    "data_origin": FieldRule("str", 20),
    "tag_type": FieldRule("str", 20),
    "weight_locked": FieldRule("bool", nullable=False, default=False),
    "archived_at": FieldRule("datetime"),
    "last_used": FieldRule("datetime"),
    "created_at": FieldRule("datetime", nullable=False),
}


def source_id(value):
    """IDs require an actual int, unlike an integral numeric measurement."""
    return value if type(value) is int and 1 <= value <= 2147483647 else INVALID


def read_field(entry: dict, name: str, rule: FieldRule, date_parser: Callable[[str], datetime | None]):
    value = entry.get(name, MISSING)
    if value is MISSING:
        return MISSING
    if value is None:
        return None if rule.nullable else INVALID
    if rule.kind in ("integer", "float"):
        # Compare before converting: float(huge_int) itself could overflow.
        # Inclusive finite limits also reject NaN and either infinity.
        if type(value) not in (int, float) or not 0 <= value <= rule.limit:
            return INVALID
        if rule.kind == "integer":
            return int(value) if type(value) is int or value.is_integer() else INVALID
        return 0.0 if value == 0 else float(value)
    if rule.kind == "bool":
        return value if type(value) is bool else INVALID
    if rule.kind == "str":
        if not isinstance(value, str) or "\0" in value or (rule.nonempty and not value):
            return INVALID
        if rule.limit is not None and len(value) > rule.limit:
            return INVALID
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return INVALID
        return value
    if not isinstance(value, str):
        return INVALID
    try:
        parsed = date_parser(value)
    except (ValueError, OverflowError):
        return INVALID
    return parsed if parsed is not None else INVALID
