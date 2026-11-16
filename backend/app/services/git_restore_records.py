"""Typed metadata input for Git restore; no DB, file or printer side effects.

The record writers decide identity and precedence before using these values.
Missing, explicit null and invalid input are deliberately different tokens.
"""

import re
from dataclasses import dataclass, field
from uuid import UUID

from backend.app.services.git_restore_fields import INVALID, MISSING, NUMERIC_FIELDS, FieldRule, read_field

ARCHIVE_FIELDS = {
    **{name: rule for (domain, name), rule in NUMERIC_FIELDS.items() if domain == "archives"},
    "filename": FieldRule("str", 255, False, "restored-from-backup", nonempty=True),
    "print_name": FieldRule("str", 255),
    "filament_type": FieldRule("str", 50),
    "filament_color": FieldRule("str", 50),
    "sliced_for_model": FieldRule("str", 50),
    "status": FieldRule("str", 20, False, "cancelled", nonempty=True),
    "failure_reason": FieldRule("str", 100),
    "makerworld_url": FieldRule("str", 500),
    "designer": FieldRule("str", 255),
    "external_url": FieldRule("str", 500),
    "notes": FieldRule("str"),
    "tags": FieldRule("str"),
    "is_favorite": FieldRule("bool", nullable=False, default=False),
    "started_at": FieldRule("datetime"),
    "completed_at": FieldRule("datetime"),
    "created_at": FieldRule("datetime", nullable=False),
    "deleted_at": FieldRule("datetime"),
}
USAGE_FIELDS = {
    **{name: rule for (domain, name), rule in NUMERIC_FIELDS.items() if domain == "usage"},
    "print_name": FieldRule("str", 500),
    "status": FieldRule("str", 20, False, "unknown", nonempty=True),
    "created_at": FieldRule("datetime", nullable=False),
}
OPERATOR_FIELDS = frozenset({"notes", "tags", "is_favorite", "external_url"})
INSERT_ONLY = frozenset({"filename", "file_size", "created_at", "started_at"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted", "stopped", "cancelled", "skipped", "archived"})
INFLIGHT_STATUSES = frozenset({"printing", "paused"})
UNKNOWN_ENERGY = {
    "energy_provenance": {"version": 1, "kind": "unavailable", "unavailable_reason": "insufficient_history"}
}


class PayloadShapeError(ValueError):
    """A category is refused before any of its writes, with no backup values in the message."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _same_json(a, b):
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same_json(value, b[key]) for key, value in a.items())
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_json(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def archive_rows(payload):
    if payload is MISSING:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("archives"), list):
        raise PayloadShapeError("archivesInvalidPayload")
    return payload["archives"]


def spool_rows(inventory, usage):
    if inventory is MISSING and usage is MISSING:
        return None, []
    if not isinstance(inventory, dict) or not isinstance(inventory.get("spools"), list):
        raise PayloadShapeError("spoolsInvalidPayload")
    if usage is MISSING:
        return inventory["spools"], []
    if not isinstance(usage, dict):
        raise PayloadShapeError("spoolUsageInvalidPayload")
    a, b = usage.get("history", MISSING), usage.get("usage_history", MISSING)
    if a is MISSING and b is MISSING:
        raise PayloadShapeError("spoolUsageInvalidPayload")
    if any(value is not MISSING and not isinstance(value, list) for value in (a, b)):
        raise PayloadShapeError("spoolUsageInvalidPayload")
    if a is not MISSING and b is not MISSING and not _same_json(a, b):
        raise PayloadShapeError("spoolUsageAliasConflict")
    return inventory["spools"], a if a is not MISSING else b if b is not MISSING else []


def uuid_value(value):
    if value is MISSING or value is None:
        return None
    if not isinstance(value, str) or len(value) != 36:
        return INVALID
    try:
        canonical = str(UUID(value))
    except ValueError:
        return INVALID
    return canonical if canonical == value.lower() else INVALID


def hash_value(value):
    if value is MISSING or value is None:
        return value
    return value.lower() if isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{64}", value) else INVALID


def unknown_outcome(values, uncertainty):
    return values["status"] not in TERMINAL_STATUSES or uncertainty is INVALID or uncertainty is True


def operator_only(row, incoming_unknown):
    extra = row.extra_data
    return (
        incoming_unknown
        or row.status in INFLIGHT_STATUSES
        or (extra is not None and not isinstance(extra, dict))
        or bool((extra or {}).get("recovered_outcome_uncertain"))
    )


def parsed_fields(entry, rules, date_parser):
    return {name: read_field(entry, name, rule, date_parser) for name, rule in rules.items()}


def write_fields(values, rules, *, insert, allowed=None):
    result = {}
    for name, value in values.items():
        if allowed is not None and name not in allowed:
            continue
        if value is MISSING or value is INVALID:
            if insert:
                result[name] = rules[name].default
        else:
            result[name] = value
    return result


def owner_value(entry, users_by_name, valid_users, date_parser):
    name = read_field(entry, "created_by_username", FieldRule("str", 100), date_parser)
    if name is INVALID:
        return MISSING, "archivesOwnerUnmatched", 1
    if isinstance(name, str) and name:
        return (users_by_name[name], None, 0) if name in users_by_name else (MISSING, "archivesOwnerUnmatched", 0)
    if "created_by_id" not in entry:
        return MISSING, "archivesOwnerUnknown", 0
    value = entry["created_by_id"]
    if value is None:
        return None, "archivesOwnerUnknown", 0
    from backend.app.services.git_restore_fields import source_id

    key = source_id(value)
    if key is INVALID or key not in valid_users:
        return MISSING, "archivesOwnerCleared", int(key is INVALID)
    return key, None, 0


@dataclass(frozen=True)
class ArchiveProof:
    local_id: int
    identity: str | None
    printer_id: int | None


@dataclass
class EnergyBundle:
    kwh: float | None = None
    cost: float | None = None
    record: dict = field(default_factory=dict)
    complete: bool = False
    clear: bool = False
    no_energy: bool = False
    invalid_fields: set[str] = field(default_factory=set)


def _finite(value, *, limit=1.7976931348623157e308):
    return type(value) in (int, float) and 0 <= value <= limit


def _record(value, date_parser):
    """Sanitise only known provenance keys; IDs here never resolve a live owner."""
    if not isinstance(value, dict):
        return INVALID
    result = {}
    for flag in ("energy_is_approximate", "energy_start_partial"):
        if flag in value:
            if type(value[flag]) is not bool:
                return INVALID
            result[flag] = value[flag]
    if "energy_reconstructed_from" in value:
        if value["energy_reconstructed_from"] != "power_history":
            return INVALID
        result["energy_reconstructed_from"] = "power_history"
    if "energy_provenance" not in value:
        return result
    p = value["energy_provenance"]
    if not isinstance(p, dict) or p.get("kind") not in ("measured", "estimated", "unavailable"):
        return INVALID
    output = {"kind": p["kind"]}
    if "version" in p:
        if type(p["version"]) is not int or p["version"] != 1:
            return INVALID
        output["version"] = 1
    if "final" in p:
        if type(p["final"]) is not bool:
            return INVALID
        output["final"] = p["final"]
    strings = {"reason", "source_kind", "source_id", "start", "end", "unavailable_reason"}
    integers = {"plug_id", "incident_id", "samples"}
    numbers = {
        "counter_delta_kwh",
        "tariff",
        "gap_limit_s",
        "expected_interval_s",
        "max_gap_s",
        "integral_kwh",
        "coverage",
    }
    for key in strings | integers | numbers | {"quality_reasons"}:
        if key not in p:
            continue
        item = p[key]
        if key in strings:
            if item is not None and read_field({key: item}, key, FieldRule("str"), date_parser) is INVALID:
                return INVALID
            if key in ("start", "end") and item is not None:
                if read_field({key: item}, key, FieldRule("datetime"), date_parser) is INVALID:
                    return INVALID
        elif key in integers:
            if type(item) is not int or item < (0 if key == "samples" else 1):
                return INVALID
        elif key in numbers:
            if not _finite(item, limit=1 if key == "coverage" else 1.7976931348623157e308):
                return INVALID
        elif not isinstance(item, list) or any(
            read_field({"v": x}, "v", FieldRule("str", nullable=False), date_parser) is INVALID for x in item
        ):
            return INVALID
        output[key] = item
    if p["kind"] == "estimated" and ("coverage" not in output or result.get("energy_is_approximate") is False):
        return INVALID
    if p["kind"] == "unavailable" and not output.get("unavailable_reason"):
        return INVALID
    result["energy_provenance"] = output
    return result


def energy_bundle(entry, date_parser):
    bundle = EnergyBundle(complete=all(key in entry for key in ("energy_kwh", "energy_cost", "energy_record")))
    if bundle.complete and all(entry[key] is None for key in ("energy_kwh", "energy_cost", "energy_record")):
        bundle.clear = True
        return bundle
    for key, attr in (("energy_kwh", "kwh"), ("energy_cost", "cost")):
        value = read_field(entry, key, NUMERIC_FIELDS["archives", key], date_parser)
        if value is INVALID:
            bundle.invalid_fields.add(key)
        elif value is not MISSING:
            setattr(bundle, attr, value)
    record = entry.get("energy_record", MISSING)
    if record is not MISSING:
        record = _record(record, date_parser)
        if record is INVALID:
            bundle.invalid_fields.add("energy_record")
        else:
            bundle.record = record
    kind = bundle.record.get("energy_provenance", {}).get("kind")
    if bundle.kwh is None and (bundle.cost is not None or kind in ("estimated", "measured")):
        bundle.invalid_fields.add("energy_record")
    if kind == "unavailable" and (bundle.kwh is not None or bundle.cost is not None):
        bundle.invalid_fields.add("energy_record")
    if kind is not None and not bundle.complete:
        bundle.invalid_fields.add("energy_record")
    bundle.no_energy = bundle.kwh is None and bundle.cost is None and kind is None and not bundle.clear
    return bundle
