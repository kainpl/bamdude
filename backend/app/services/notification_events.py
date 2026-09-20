"""The event catalog the in-app inbox reads from: severity and group per event type.

Spec: vault 60-specs/notification-center-spec §3. ONE source for what the inbox
knows about an event; the provider registry (``PROVIDER_EVENT_DEFAULTS``) and the
templates stay what they are, and a test pins the three against each other.

``IN_APP_ONLY_EVENTS`` is deliberately empty today. An event listed there is in
the catalog and reaches the inbox through ``NotificationService.notify_in_app``,
but never gets an ``on_*`` provider flag, so no external channel can be
subscribed to it (§3.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SEVERITIES: tuple[str, ...] = ("info", "warning", "error")
GROUPS: tuple[str, ...] = ("print", "printer", "filament", "ams", "queue", "inventory", "sensors")


@dataclass(frozen=True, slots=True)
class EventMeta:
    severity: str
    group: str


def _e(severity: str, group: str) -> EventMeta:
    return EventMeta(severity, group)


EVENT_CATALOG: dict[str, EventMeta] = {
    # print
    "print_start": _e("info", "print"),
    "print_complete": _e("info", "print"),
    "print_failed": _e("error", "print"),
    "print_stopped": _e("warning", "print"),
    "print_paused": _e("warning", "print"),
    "print_resumed": _e("info", "print"),
    "print_progress": _e("info", "print"),
    "print_missing_spool_assignment": _e("warning", "print"),
    "first_layer_complete": _e("info", "print"),
    "bed_cooled": _e("info", "print"),
    # printer
    "printer_error": _e("error", "printer"),
    "printer_offline": _e("error", "printer"),
    "ai_failure_detection": _e("error", "printer"),
    "plate_not_empty": _e("warning", "printer"),
    "maintenance_due": _e("warning", "printer"),
    # filament
    "filament_runout": _e("error", "filament"),
    "filament_low": _e("warning", "filament"),
    "filament_deficit": _e("warning", "filament"),
    # ams
    "ams_temperature_high": _e("error", "ams"),
    "ams_ht_temperature_high": _e("error", "ams"),
    "ams_humidity_high": _e("error", "ams"),
    "ams_ht_humidity_high": _e("error", "ams"),
    "ams_drying_suspended": _e("warning", "ams"),
    # queue
    "queue_job_failed": _e("error", "queue"),
    "queue_job_skipped": _e("warning", "queue"),
    "queue_job_waiting": _e("warning", "queue"),
    "queue_job_added": _e("info", "queue"),
    "queue_job_started": _e("info", "queue"),
    "queue_completed": _e("info", "queue"),
    "printer_queue_completed": _e("info", "queue"),
    # inventory
    "stock_break_alert": _e("error", "inventory"),
    "stock_reorder_alert": _e("warning", "inventory"),
    # sensors
    "sensor_above_max": _e("error", "sensors"),
    "sensor_below_min": _e("error", "sensors"),
    "sensor_silent": _e("warning", "sensors"),
    "sensor_back_in_range": _e("info", "sensors"),
    "sensor_speaking_again": _e("info", "sensors"),
}

# Events shown only inside BamDude — never offered to Telegram / email / push
# providers. Empty on purpose; grow it together with the sender that raises the
# event through ``NotificationService.notify_in_app`` (spec §3.3).
IN_APP_ONLY_EVENTS: frozenset[str] = frozenset()

# Two provider flags each govern several catalog events. The service owns the
# behaviour (which column gates which message); the map lives here because the
# provider-events endpoint needs it too, and a second copy is how the frontend
# lists drifted in the first place. ``NotificationService._SENSOR_ALERT_FIELDS``
# is this object, so the test that pins the aliases still reads the authority.
SENSOR_ALERT_FIELDS: dict[str, str] = {
    "sensor_above_max": "on_sensor_threshold",
    "sensor_below_min": "on_sensor_threshold",
    "sensor_back_in_range": "on_sensor_threshold",
    "sensor_silent": "on_sensor_silent",
    "sensor_speaking_again": "on_sensor_silent",
}


def events_of_flag(flag: str) -> list[str]:
    """Catalog events a provider flag governs, in catalog order.

    One for an ordinary flag (``on_print_start`` -> ``print_start``), several
    for the two sensor aggregates. An empty list means the flag names no
    catalogued event — impossible today (a test pins it) and rendered by the
    frontend under a derived label rather than hidden.
    """
    aggregated = [e for e, f in SENSOR_ALERT_FIELDS.items() if f == flag]
    if aggregated:
        return [e for e, _ in catalog_rows() if e in set(aggregated)]
    event = flag.removeprefix("on_")
    return [event] if event in EVENT_CATALOG else []


_UNKNOWN = EventMeta("info", "printer")
_warned_unknown: set[str] = set()
_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


def event_meta(event_type: str) -> EventMeta:
    """Severity and group of an event; an uncatalogued type folds to info and warns once."""
    meta = EVENT_CATALOG.get(event_type)
    if meta is not None:
        return meta
    if event_type not in _warned_unknown:
        _warned_unknown.add(event_type)
        logger.warning("Inbox: event type %r is not in the catalog; treating it as info", event_type)
    return _UNKNOWN


def default_inbox_events() -> list[str]:
    """What a user with ``inbox_events IS NULL`` receives: every warning and error."""
    return sorted(k for k, m in EVENT_CATALOG.items() if m.severity in ("warning", "error"))


def wants_inbox_event(inbox_events: list[str] | None, event_type: str) -> bool:
    """The Telegram rule (``TelegramChat.should_notify``): NULL = defaults, [] = nothing, [...] = exactly these."""
    events = inbox_events if inbox_events is not None else default_inbox_events()
    return event_type in events


def catalog_rows() -> list[tuple[str, EventMeta]]:
    """The catalog in display order: group, then error → warning → info, then key.

    A row whose group or severity is not in the known tuples sorts last rather
    than raising: this feeds ``GET /inbox/subscriptions``, and a typo in a new
    catalog entry should cost a misplaced card, not a 500 on the page where a
    person edits their own subscriptions. ``event_meta`` degrades the same way
    for the same class of drift.
    """
    return sorted(
        EVENT_CATALOG.items(),
        key=lambda kv: (
            GROUPS.index(kv[1].group) if kv[1].group in GROUPS else len(GROUPS),
            _SEVERITY_RANK.get(kv[1].severity, len(_SEVERITY_RANK)),
            kv[0],
        ),
    )
