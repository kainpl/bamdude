"""The highest printer number a database still remembers (P-ID, spec §B).

SQLite without ``AUTOINCREMENT`` gives the next row ``max(id) + 1``, so deleting
the newest printer handed its number to the next one, and every dict, set and row
still keyed by the old number belonged to the new printer at once (GH109: a deleted
printer's "current print" blocked the new printer's first dispatch). m262 makes
``printers`` an ``AUTOINCREMENT`` table; its counter has to start above every
number any current reader could still pick up, not only ``MAX(printers.id)``.

The registry below is FROZEN at the schema P-ID met. It is deliberately not
derived from the live models: the migration and the SQLite→PostgreSQL import both
read databases at OLDER levels, whose shape the models no longer describe. A
carrier added after P-ID needs no entry — its numbers come from a counter that
already never goes back (``tests/unit/test_printer_id_bound.py`` says so).

Presence: a carrier that is present always counts, whatever the migration level
says (``create_all`` can bring one before its migration). ``guaranteed`` is the
migration whose application proves the carrier exists — asked of the applied set,
never of ``MAX(version)``; ``None`` means only ``create_all`` makes it, so its
absence is never an error. ``removed`` names the migration that took a historical
carrier away: once it is applied the absence is history again, while a column an
old file still has keeps counting.

``settings`` is the baseline's table (m001). The row of one key is optional — an
absent row holds no number — but the table itself is checked: a present table
without its ``key`` and ``value`` columns, or with a ``value`` that is not text,
stops the caller with the reason named (acceptance PID-A01).

Values: what a reader would take as a printer number. SQLite keeps whatever was
written, so a value's own class decides (``typeof``), not the declared type. On
PostgreSQL an integer carrier of another type stops the caller: a bound computed by
skipping it would be a guess.

Domain: a printer number is 1..2³¹ − 1 — PostgreSQL's ``integer`` and inside
JavaScript's exact integers. A value outside it is not reserved and does not raise
the bound; numbers above the domain are outside P-ID's guarantee (owner, PID-S24).

Outside the registry on purpose: Telegram-chat and notification-provider scopes
(on a farm every printer and every chat is the operator's own — owner 2026-11-25),
numbers inside archive/journal JSON (history, nothing is addressed by them), files,
browser storage and VP numbers (their own space).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

MAX_PRINTER_ID = 2**31 - 1


@dataclass(frozen=True)
class Carrier:
    table: str
    column: str
    # "int"           — the column holds printer (or printer-queue) numbers
    # "ams_key"       — ``ams_labels.ams_serial_number``: only the positional key ``p{printer}a{ams}``
    # "api_key_scope" — ``api_keys.printer_ids``, read the way the API-key gate reads it
    # "resources"     — ``projects.printer_resources`` → ``printer_ids``
    # "plan_entries"  — an order working plan's entries → ``[].target.printer_id``
    # "plan_request"  — an order plan submission's request → ``parts[].entry.target.printer_id``
    # "setting_int"   — a ``settings`` row read as ``int(value)``
    # "setting_list"  — a ``settings`` row holding a JSON list of printer numbers
    kind: str
    guaranteed: int | None
    removed: int | None = None
    key: str | None = None  # the ``settings`` key, for the two setting kinds


def _int(table: str, column: str, guaranteed: int | None = None, removed: int | None = None) -> Carrier:
    return Carrier(table, column, "int", guaranteed, removed)


def _setting(key: str, kind: str) -> Carrier:
    return Carrier("settings", "value", kind, 1, key=key)


CARRIERS: tuple[Carrier, ...] = (
    # The printer itself, and its queue (a printer's queue has the printer's own number).
    _int("printers", "id"),
    _int("printer_queues", "id", 2),
    _int("printer_queues", "printer_id", 2),
    _int("print_queue", "queue_id", 2),
    _int("print_archives", "queue_id", 19),
    # Every other column with a foreign key to printers.id.
    _int("active_print_sessions", "printer_id", 221),
    _int("active_print_spoolman", "printer_id"),
    _int("ams_sensor_history", "printer_id"),
    _int("ams_setting_audit", "printer_id", 60),
    _int("calibration_audit", "printer_id", 62),
    _int("calibration_session", "printer_id", 62),
    _int("cloud_link_printers", "printer_id", 155),
    _int("drying_schedules", "printer_id", 184),
    _int("filament_calibration", "printer_id", 63),
    _int("firmware_batch_items", "printer_id"),
    _int("hms_muted_entries", "printer_id", 163),
    _int("notification_digest_queue", "printer_id"),
    _int("notification_logs", "printer_id"),
    _int("obico_events", "printer_id"),
    _int("print_archives", "printer_id"),
    _int("print_usage_events", "printer_id", 153),
    _int("printer_ha_sensors", "printer_id"),
    _int("printer_maintenance", "printer_id"),
    _int("printer_sensor_history", "printer_id", 92),
    _int("printer_setting_audit", "printer_id", 61),
    _int("printer_tag_links", "printer_id", 164),
    _int("scheduled_dryings", "printer_id", 184),
    _int("smart_plugs", "printer_id"),
    _int("smart_sensor_bindings", "printer_id"),
    _int("smart_sensors", "printer_id", 144),
    _int("spool_assignment", "printer_id"),
    _int("spool_k_profile", "printer_id"),
    _int("spool_usage_history", "printer_id"),
    _int("spoolman_k_profile", "printer_id", 53),
    _int("spoolman_slot_assignments", "printer_id", 53),
    _int("spoolman_usage_history", "printer_id", 228),
    _int("virtual_printers", "target_printer_id"),
    # Printer and queue numbers kept without a foreign key.
    _int("auto_queue_returns", "printer_id", 226),
    _int("auto_queue_returns", "old_queue_id", 226),
    _int("auto_queue_unsent_returns", "printer_id", 254),
    _int("auto_queue_unsent_returns", "old_queue_id", 254),
    _int("print_state_recovery_actions", "printer_id"),
    _int("print_state_recovery_actions", "queue_id"),
    _int("user_notifications", "printer_id", 176),
    # Text, JSON and settings.
    Carrier("ams_labels", "ams_serial_number", "ams_key", None),
    Carrier("api_keys", "printer_ids", "api_key_scope", None),
    Carrier("projects", "printer_resources", "resources", 209),
    Carrier("order_working_plans", "entries", "plan_entries", 206),
    Carrier("order_working_plans", "saved_entries", "plan_entries", 206),
    Carrier("order_plan_submissions", "request", "plan_request", 206),
    _setting("default_printer_id", "setting_int"),
    _setting("virtual_printer_target_printer_id", "setting_int"),
    _setting("obico_enabled_printers", "setting_list"),
    # Historical: columns the models no longer have, read while an old file still holds them.
    _int("print_queue", "printer_id", removed=2),
    _int("filament_calibration", "calibrated_on_printer_id", removed=63),
    _int("kprofile_notes", "printer_id", removed=65),
    _int("slicer_pipelines", "target_printer_id", removed=112),
    _int("pipeline_jobs", "assigned_printer_id", 102, removed=112),  # owed only while m112 is not applied
)


class PrinterIdBoundError(RuntimeError):
    """A carrier the bound must read is missing or has a shape this registry does not know,
    or no printer number is left in the domain."""


def in_domain(value: int) -> bool:
    return 1 <= value <= MAX_PRINTER_ID


def _domain(values) -> list[int]:
    return [v for v in values if v is not None and in_domain(v)]


_INVALID = object()


def _json(raw):
    """The decoded value, or ``_INVALID`` for something that is not JSON text."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return _INVALID
    return raw


def element_id(value) -> int | None:
    """One list element as a membership reader could match it — conservatively.

    ``true == 1`` in Python, so a boolean true is printer 1; a float is truncated by
    ``int()``; a numeric string counts although the readers compare integers.
    """
    if isinstance(value, bool):
        return 1 if value else None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def list_ids(value) -> list[int]:
    """The printer numbers of a JSON list; anything that is not a list holds none."""
    if not isinstance(value, list):
        return []
    return _domain(element_id(item) for item in value)


def api_key_scope_ids(raw) -> list[int]:
    """What the API-key gate would allow for one stored ``printer_ids`` value.

    ``core/api_key_scope._allowed`` does ``int(pid) for pid in api_key.printer_ids``:
    a list gives its elements, an object its keys, a string its characters. Every
    element ``int()`` takes is reserved — even where another element makes the gate
    refuse the whole scope (a conservative superset). NULL is "every printer": none.
    """
    value = _json(raw)
    if value is _INVALID or value is None or isinstance(value, (bool, int, float)):
        return []
    found = []
    for item in value:
        try:
            found.append(int(item))
        except (TypeError, ValueError, OverflowError):
            continue
    return _domain(found)


_AMS_POSITION_KEY = re.compile(r"p([1-9][0-9]*)a(0|-?[1-9][0-9]*)")


def ams_key_id(raw) -> int | None:
    """The printer number of a positional AMS key ``p{printer}a{ams}``, as the writer and
    readers build it (``f"p{int}a{int}"``). A real AMS serial or another spelling: none."""
    if not isinstance(raw, str):
        return None
    match = _AMS_POSITION_KEY.fullmatch(raw)
    if match is None:
        return None
    value = int(match.group(1))
    return value if in_domain(value) else None


def resources_ids(raw) -> list[int]:
    value = _json(raw)
    if not isinstance(value, dict):
        return []
    return list_ids(value.get("printer_ids"))


def _target_id(entry) -> int | None:
    """``entry.target.printer_id``, whatever ``target.kind`` says: the schema allows a number
    only for ``kind == "printer"``, so taking it everywhere is the conservative reading."""
    if not isinstance(entry, dict):
        return None
    target = entry.get("target")
    if not isinstance(target, dict):
        return None
    return element_id(target.get("printer_id"))


def plan_entries_ids(raw) -> list[int]:
    value = _json(raw)
    if not isinstance(value, list):
        return []
    return _domain(_target_id(entry) for entry in value)


def plan_request_ids(raw) -> list[int]:
    value = _json(raw)
    parts = value.get("parts") if isinstance(value, dict) else None
    if not isinstance(parts, list):
        return []
    return _domain(_target_id(part.get("entry")) for part in parts if isinstance(part, dict))


def setting_int_id(raw) -> int | None:
    """``int(value)``, as the settings readers take it; ``""`` and ``"None"`` are no printer."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw  # a driver may hand a numeric text back as a number; int(value) reads it the same
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError:
            return None
    else:
        return None
    return value if in_domain(value) else None


def setting_list_ids(raw) -> list[int]:
    value = _json(raw)
    return [] if value is _INVALID else list_ids(value)


def text_ids(kind: str, raw) -> list[int]:
    """The printer numbers one stored value of a non-integer carrier holds."""
    if raw is None:
        return []
    if kind == "ams_key":
        found = ams_key_id(raw)
        return [] if found is None else [found]
    if kind == "api_key_scope":
        return api_key_scope_ids(raw)
    if kind == "resources":
        return resources_ids(raw)
    if kind == "plan_entries":
        return plan_entries_ids(raw)
    if kind == "plan_request":
        return plan_request_ids(raw)
    if kind == "setting_int":
        found = setting_int_id(raw)
        return [] if found is None else [found]
    if kind == "setting_list":
        return setting_list_ids(raw)
    raise ValueError(f"unknown carrier kind {kind!r}")


def _missing(carrier: Carrier, applied: set[int], what: str) -> None:
    """Stop when the applied set proves the carrier exists: its migration is applied and the one
    that removed it (a historical carrier) is not."""
    owed = carrier.guaranteed is not None and carrier.guaranteed in applied
    if owed and (carrier.removed is None or carrier.removed not in applied):
        raise PrinterIdBoundError(
            f"{carrier.table}.{carrier.column}: {what}, although migration {carrier.guaranteed} creates it"
        )


def _settings_schema(carrier: Carrier, types: dict[str, str], text_type) -> None:
    """A present ``settings`` table must have ``key`` and a text ``value``, whatever the level."""
    for column in ("key", "value"):
        if column not in types:
            raise PrinterIdBoundError(f"settings.{column}: column is missing, so {carrier.key!r} cannot be read")
    if not text_type(types["value"]):
        raise PrinterIdBoundError(
            f"settings.value: unexpected type {types['value']!r}; a setting is stored as text ({carrier.key!r})"
        )


def _sqlite_text_affinity(declared: str | None) -> bool:
    declared = (declared or "").upper()
    return "INT" not in declared and any(token in declared for token in ("CHAR", "CLOB", "TEXT"))


def _rows(conn, sql: str, params=()) -> list[tuple]:
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        return list(cursor.fetchall())
    finally:
        cursor.close()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _lossless_int(value) -> int | None:
    """A SQLite ``real`` or ``text`` value in an integer column that ``int()`` takes without loss."""
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _best(best: int | None, values: list[int]) -> int | None:
    if not values:
        return best
    top = max(values)
    return top if best is None else max(best, top)


def _sqlite_int_values(conn, table: str, column: str) -> list[int]:
    t, c = _quote(table), _quote(column)
    # One scan: the largest real integer in the domain, and whether any real/text value needs a closer look.
    top, others = _rows(
        conn,
        f"SELECT MAX(CASE WHEN typeof({c}) = 'integer' AND {c} BETWEEN 1 AND ? THEN {c} END),"  # noqa: S608
        f" SUM(CASE WHEN typeof({c}) IN ('real', 'text') THEN 1 ELSE 0 END) FROM {t}",
        (MAX_PRINTER_ID,),
    )[0]
    values = [top] if top is not None else []
    if others:
        rows = _rows(conn, f"SELECT DISTINCT {c} FROM {t} WHERE typeof({c}) IN ('real', 'text')")  # noqa: S608
        values += _domain(_lossless_int(raw) for (raw,) in rows)
    return values


def sqlite_bound(conn, applied: set[int]) -> int | None:
    """The bound over a SQLite database, through any DB-API connection (sqlite3 or the aiosqlite adapter).

    Reads only. ``applied`` is the database's own ``_migrations`` versions.
    """
    best: int | None = None
    for carrier in CARRIERS:
        columns = {row[1]: row[2] for row in _rows(conn, f"PRAGMA table_xinfo({_quote(carrier.table)})")}
        if not columns:
            _missing(carrier, applied, "table is missing")
            continue
        if carrier.key is not None:
            _settings_schema(carrier, columns, _sqlite_text_affinity)
        if carrier.column not in columns:
            _missing(carrier, applied, "column is missing")
            continue
        if carrier.kind == "int":
            best = _best(best, _sqlite_int_values(conn, carrier.table, carrier.column))
            continue
        table, column = _quote(carrier.table), _quote(carrier.column)
        if carrier.key is not None:
            rows = _rows(conn, f'SELECT {column} FROM {table} WHERE "key" = ?', (carrier.key,))  # noqa: S608
        else:
            rows = _rows(conn, f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")  # noqa: S608
        best = _best(best, [v for (raw,) in rows for v in text_ids(carrier.kind, raw)])
    return best


def sqlite_allocator(conn) -> int | None:
    """The highest number SQLite itself would continue from: ``MAX(printers.id)`` and the
    ``sqlite_sequence`` counter, domain or not. Reads only."""
    found = []
    if _rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'printers'"):
        (top,) = _rows(conn, "SELECT MAX(id) FROM printers WHERE typeof(id) = 'integer'")[0]
        found.append(top)
    if _rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'sqlite_sequence'"):
        found += [seq for (seq,) in _rows(conn, "SELECT seq FROM sqlite_sequence WHERE name = 'printers'")]
    numbers = [v for v in found if isinstance(v, int) and not isinstance(v, bool)]
    return max(numbers) if numbers else None


def require_next_number(*highest: int | None) -> None:
    """Stop when no printer number is left at or below ``MAX_PRINTER_ID`` after ``highest``."""
    numbers = [v for v in highest if v is not None]
    if numbers and max(numbers) >= MAX_PRINTER_ID:
        raise PrinterIdBoundError(
            f"printers: the id counter is exhausted — {max(numbers)} is already remembered and no printer"
            f" number is left up to {MAX_PRINTER_ID}"
        )


_PG_INT_TYPES = {"smallint", "integer", "bigint"}
_PG_TEXT_TYPES = {"json", "jsonb", "text", "character varying", "character"}
_PG_SETTING_TYPES = {"text", "character varying", "character"}


async def postgres_bound(conn, applied: set[int]) -> int | None:
    """The bound over a PostgreSQL database, on an SQLAlchemy async connection. Reads only."""
    from sqlalchemy import text

    best: int | None = None
    for carrier in CARRIERS:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": carrier.table},
        )
        types = dict(result.tuples().all())
        if not types:
            _missing(carrier, applied, "table is missing")
            continue
        if carrier.key is not None:
            _settings_schema(carrier, types, _PG_SETTING_TYPES.__contains__)
        if carrier.column not in types:
            _missing(carrier, applied, "column is missing")
            continue
        wanted = _PG_INT_TYPES if carrier.kind == "int" else _PG_TEXT_TYPES
        if types[carrier.column] not in wanted:
            raise PrinterIdBoundError(
                f"{carrier.table}.{carrier.column}: unexpected type {types[carrier.column]!r} for a {carrier.kind} carrier"
            )
        table, column = _quote(carrier.table), _quote(carrier.column)
        if carrier.kind == "int":
            value = (
                await conn.execute(
                    text(f"SELECT MAX({column}) FROM {table} WHERE {column} BETWEEN 1 AND :top"),  # noqa: S608
                    {"top": MAX_PRINTER_ID},
                )
            ).scalar()
            best = _best(best, [value] if value is not None else [])
            continue
        if carrier.key is not None:
            rows = await conn.execute(
                text(f'SELECT {column}::text FROM {table} WHERE "key" = :key'),  # noqa: S608
                {"key": carrier.key},
            )
        else:
            rows = await conn.execute(text(f"SELECT {column}::text FROM {table} WHERE {column} IS NOT NULL"))  # noqa: S608
        best = _best(best, [v for (raw,) in rows.all() for v in text_ids(carrier.kind, raw)])
    return best


def sqlite_applied_versions(conn) -> set[int]:
    """The ``_migrations`` versions of a SQLite database; none when it has no such table."""
    if not _rows(conn, "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = '_migrations'"):
        return set()
    return {row[0] for row in _rows(conn, "SELECT version FROM _migrations")}
