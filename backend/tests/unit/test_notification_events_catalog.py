"""The inbox event catalog agrees with the provider registry, the templates and the Telegram vocabulary.

Spec: vault 60-specs/notification-center-spec §3.2.
"""

import json
import re
from pathlib import Path

from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS
from backend.app.services.notification_events import (
    EVENT_CATALOG,
    GROUPS,
    IN_APP_ONLY_EVENTS,
    SEVERITIES,
    catalog_rows,
    default_inbox_events,
    event_meta,
    wants_inbox_event,
)
from backend.app.services.notification_service import NotificationService

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "backend" / "app" / "data"

# Two provider flags fan out over several templates each; the service map is the authority.
AGGREGATE_FLAGS = {
    "on_sensor_threshold": {"sensor_above_max", "sensor_below_min", "sensor_back_in_range"},
    "on_sensor_silent": {"sensor_silent", "sensor_speaking_again"},
}


def test_the_catalog_has_exactly_the_agreed_events():
    assert len(EVENT_CATALOG) == 37
    for key, meta in EVENT_CATALOG.items():
        assert meta.severity in SEVERITIES, key
        assert meta.group in GROUPS, key


def test_every_provider_flag_has_its_catalog_event():
    for flag in PROVIDER_EVENT_DEFAULTS:
        if flag in AGGREGATE_FLAGS:
            assert AGGREGATE_FLAGS[flag] <= set(EVENT_CATALOG), flag
            continue
        assert flag.removeprefix("on_") in EVENT_CATALOG, flag


def test_the_sensor_aliases_are_the_services_own_map():
    by_flag: dict[str, set[str]] = {}
    for template, flag in NotificationService._SENSOR_ALERT_FIELDS.items():
        by_flag.setdefault(flag, set()).add(template)
    assert by_flag == AGGREGATE_FLAGS


def test_every_catalog_event_has_both_templates():
    en = json.loads((DATA / "notification_templates_en.json").read_text(encoding="utf-8"))
    uk = json.loads((DATA / "notification_templates_uk.json").read_text(encoding="utf-8"))
    # The two AMS-HT events render through their non-HT siblings' templates (existing behaviour).
    rendered_by_sibling = {"ams_ht_humidity_high", "ams_ht_temperature_high"}
    for key in EVENT_CATALOG:
        if key in rendered_by_sibling:
            continue
        assert key in en and key in uk, key


def test_in_app_only_events_are_catalogued_and_never_have_a_provider_flag():
    assert IN_APP_ONLY_EVENTS <= set(EVENT_CATALOG)  # noqa: SIM300 — a subset test, not a Yoda condition
    provider_events = {f.removeprefix("on_") for f in PROVIDER_EVENT_DEFAULTS}
    provider_events |= set().union(*AGGREGATE_FLAGS.values())
    assert not (IN_APP_ONLY_EVENTS & provider_events)


def test_the_telegram_vocabulary_covers_every_external_event():
    src = (REPO / "frontend" / "src" / "components" / "AddTelegramChatModal.tsx").read_text(encoding="utf-8")
    block = src[src.index("EVENT_LABEL_KEYS") :]
    block = block[: block.index("};")]
    keys = set(re.findall(r"^\s*'?([a-z_]+)'?\s*:", block, re.M))
    missing = set(EVENT_CATALOG) - IN_APP_ONLY_EVENTS - keys
    assert not missing, sorted(missing)


def test_the_defaults_are_exactly_the_warnings_and_errors():
    expected = sorted(k for k, m in EVENT_CATALOG.items() if m.severity in ("warning", "error"))
    assert default_inbox_events() == expected
    assert "print_failed" in expected and "print_complete" not in expected
    assert "queue_job_waiting" in expected


def test_wants_inbox_event_follows_the_telegram_rule():
    assert wants_inbox_event(None, "print_failed") is True
    assert wants_inbox_event(None, "print_complete") is False
    assert wants_inbox_event([], "print_failed") is False
    assert wants_inbox_event(["print_complete"], "print_complete") is True
    assert wants_inbox_event(["print_complete"], "print_failed") is False


def test_unknown_event_types_fold_to_info_without_raising(caplog):
    meta = event_meta("no_such_event")
    assert (meta.severity, meta.group) == ("info", "printer")
    event_meta("no_such_event")  # the warning is logged once per type
    assert sum("no_such_event" in r.getMessage() for r in caplog.records) == 1


def test_catalog_rows_are_grouped_then_by_severity_then_by_key():
    rows = catalog_rows()
    assert [k for k, _ in rows][:3] == ["print_failed", "print_missing_spool_assignment", "print_paused"]
    group_order = [GROUPS.index(m.group) for _, m in rows]
    assert group_order == sorted(group_order)


def test_a_row_with_an_unknown_group_sorts_last_instead_of_raising(monkeypatch):
    """`catalog_rows` feeds the subscriptions page; a typo must not 500 it."""
    from backend.app.services import notification_events as ne

    patched = dict(EVENT_CATALOG)
    patched["made_up_event"] = ne.EventMeta("nonsense", "nowhere")
    monkeypatch.setattr(ne, "EVENT_CATALOG", patched)

    rows = ne.catalog_rows()
    assert len(rows) == len(patched)
    assert rows[-1][0] == "made_up_event"
