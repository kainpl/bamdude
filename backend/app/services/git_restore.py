"""Restore BamDude data from a Git provider backup .

The backup side (``git_backup.py``) is push-only: it collects a handful of
JSON documents and commits them. This module is the read side — it walks the
backup repository's history, lets a caller inspect what a given commit contains,
and applies selected categories back into the local database (or, for
K-profiles, back onto the printers).

Design notes worth knowing before editing:

* **A restore never reuses the backup's primary keys.** ``spool.id`` and
  ``print_archives.id`` are bare autoincrement columns, so the ids in a backup
  taken weeks ago very likely belong to unrelated rows today. Rows are matched
  on execution UUID (narrow natural keys for legacy rows), inserted without an explicit id, and an
  ``old_id -> new_id`` map is threaded through so foreign keys in dependent
  tables (spool usage history) still line up.
  Archive links are revalidated by UUID after the archive category commits;
  a deleted/reused integer id cannot name a different print's usage.

  The printer-side ``cali_idx`` behaves the same way and gets the same
  treatment. Editing a K-profile in BamDude is a delete-then-add on a
  single-nozzle printer, which re-keys it, and ``extrusion_cali_set`` aimed at a
  slot that no longer exists is silently dropped — so the live index is read
  back and matched before writing, never taken from the backup.
* **Categories are applied archives -> spools -> settings -> kprofiles.**
  Archives first because spool usage history references ``archive_id``;
  K-profiles last because they leave the database and talk to hardware.
* **Cloud profiles are not restorable.** Restoring a preset means writing to a
  Bambu or Orca Cloud account, which is a different operation from everything
  else here — every other category lands in the local database or, for
  K-profiles, on a printer the instance already owns. Tracked separately from
  the restore feature. (The collector does write ``cloud_profiles/*.json`` as of #2717; the
  earlier claim that it did not is no longer true.)
"""

import asyncio
import json
import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field as dataclasses_field
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select, text as sql_text
from sqlalchemy.exc import MultipleResultsFound
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.database import async_session
from backend.app.models.archive import PrintArchive
from backend.app.models.git_backup import GitBackupConfig, GitBackupLog
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.schemas.git_backup import RestoreCategory
from backend.app.services import (
    archive_outcome,
    meter_watch,
    spoolman_activity,
    spoolman_source,
    spoolman_source_changes,
    swap_compatibility,
    wear_cost,
)
from backend.app.services.git_providers.factory import get_provider_backend
from backend.app.services.git_restore_fields import (
    INVALID,
    MISSING,
    NUMERIC_FIELDS,
    SPOOL_FIELDS,
    read_field,
    source_id,
)
from backend.app.services.git_restore_identity import (
    ArchiveIdentityConflict,
    candidates as archive_candidates,
    lock_printers as lock_restore_printers,
    parse_archive,
    prepare as prepare_archive_restore,
    printer_for,
    resolve as resolve_archive_identity,
    usage_basis,
)
from backend.app.services.git_restore_records import (
    ARCHIVE_FIELDS,
    INSERT_ONLY,
    OPERATOR_FIELDS,
    UNKNOWN_ENERGY,
    USAGE_FIELDS,
    ArchiveProof,
    PayloadShapeError,
    archive_rows,
    energy_bundle,
    operator_only,
    owner_value,
    parsed_fields,
    spool_rows,
    uuid_value,
    write_fields,
)
from backend.app.services.printer_manager import printer_manager

logger = logging.getLogger(__name__)

METADATA_PATH = "backup_metadata.json"
SETTINGS_PATH = "settings/app_settings.json"
SPOOLS_PATH = "spools/inventory.json"
SPOOL_USAGE_PATH = "spools/usage_history.json"


# The Spoolman usage history: a file of its own, as the table is — a Spoolman
# spool id and a built-in spool id are two numberings (written by
# ``git_backup._collect_spools`` through ``spoolman_usage_history.export_rows``).
SPOOLMAN_USAGE_PATH = "spools/spoolman_usage_history.json"
# ⚠️ Our collector writes this path; upstream's writes
# ``archives/print_history.json``. Reading theirs would find nothing in one of
# OUR backups and report the archives category as simply absent from the commit
# — a silent no-op rather than an error. Must stay in step with
# ``git_backup._collect_archives``.
ARCHIVES_PATH = "archives/print_archives.json"

# kprofiles/{printer_serial}/{nozzle_diameter}.json
_KPROFILE_PATH_RE = re.compile(r"^kprofiles/([^/]+)/([^/]+)\.json$")

# Settings keys the backup collector already refuses to write. Applied again on
# the read side because a backup taken before that denylist existed can still
# contain them, and a restore must not resurrect a stale credential.
_SENSITIVE_SETTING_KEYS = {"bambu_cloud_token", "auth_secret_key"}

# The primary refusal, not a backstop for the set above. The collector filters
# exactly bambu_cloud_token and auth_secret_key, so every other credential —
# mqtt_password, ldap_bind_password, ha_token, prometheus_token — is present in
# a current backup and is skipped only because its key matches a hint here.
# _COMPANION_CREDENTIALS sits downstream of that: it withholds a toggle when the
# credential it needs was refused, so shortening this tuple would both write a
# stale credential and quietly make that rule inert.
_SECRET_KEY_HINTS = ("token", "secret", "password", "access_code", "api_key", "passphrase")

# Settings the MQTT relay reads only when it is (re)configured, so restoring the
# rows is not enough on its own. Mirrors the set the settings PUT handler
# watches. mqtt_password is in here for the configure() payload's sake — the
# credential blocklist means a restore never writes it.
_MQTT_SETTING_KEYS = {
    "mqtt_enabled",
    "mqtt_broker",
    "mqtt_port",
    "mqtt_username",
    "mqtt_password",
    "mqtt_topic_prefix",
    "mqtt_use_tls",
    "mqtt_tls_verify",
}

# Keys that decide *who can reach the instance* rather than how it behaves. The
# backup collector writes them like any other Settings row, so a backup taken
# before auth was turned on carries auth_enabled=false — and a restore reaches
# the table directly, so honouring them would:
#
#   * disable authentication outright. ``set_auth_enabled`` pairs its write with
#     ``invalidate_auth_enabled_cache()``; we cannot, so the 30 s TTL in
#     core.auth is the only thing between the write and an open instance. That
#     cache is built to fail closed — writing the stored value behind its back
#     is what would make it fail open.
#   * bypass the lockout refusals ``update_settings`` enforces (a
#     ``local_login_enabled=false`` with no enabled OIDC provider, or with no
#     OIDC link on the caller, is a 400 there — #1589).
#   * cross a permission boundary: a restore would be a way to rewrite auth
#     config without SETTINGS_UPDATE. (The endpoint gates each category on the
#     permission owning its rows now, but that is settings:update — still not
#     the auth UI's own guards, which is what these keys actually need.)
#
# Auth is reconfigured through the auth UI, which has the guards. Restoring it
# from a snapshot has no safe reading.
_PROTECTED_SETTING_KEYS = {
    "auth_enabled",
    "advanced_auth_enabled",
    "local_login_enabled",
    "setup_completed",
}

# The LDAP family, refused for the same reason and by prefix rather than by
# name, so a key added to the schema later is refused by default.
#
# These are not "how the instance behaves" settings — together they name *which
# directory server decides who you are*. auth.py reads them live from this table
# on every login (see the ldap_keys list in _get_ldap_settings), so a restore
# that writes them substitutes the authentication source wholesale:
# ldap_server_url points at another directory, ldap_auto_provision creates a
# local account for whoever it vouches for, and ldap_default_group decides what
# that account gets — Administrators, if the backup says so.
#
# The companion rule does NOT cover this, which is the trap. ldap_enabled is
# paired with ldap_bind_password there, but an *anonymous* bind is a working
# config, so a backup that simply omits the password skips the refusal at the
# _COMPANION_EXPOSURE_TOGGLES check and the toggle is written. Omitting a
# credential is exactly what an attacker authoring this file would do — they own
# the directory being pointed at, so they need no bind credential from us.
_PROTECTED_SETTING_PREFIXES = ("ldap_",)

# Nozzle diameters the backup collector iterates. A path outside this set means
# the backup was written by a newer version, so accept it rather than dropping
# data, but keep the list for validation messages.
_KNOWN_NOZZLES = {"0.2", "0.4", "0.6", "0.8"}


# Spool bookkeeping a backup carries since the record was widened (upstream
# 71d4b2f70). Restored only when the backup names the key.
_SPOOL_LATE_FIELDS = (
    "weight_used_baseline",
    "category",
    "low_stock_threshold_pct",
    "storage_location",
    "purchase_location",
    "lot",
    "material_number",
    "extra_colors",
    "effect_type",
    "filament_diameter",
    "last_dried_temp",
    "last_dried_hours",
)
_SPOOL_LATE_DATES = ("purchase_date", "last_dried_at")


def _parse_dt(value) -> datetime | None:
    """Best-effort parse of a datetime the backup wrote via ``str(...)``.

    Normalised to naive UTC, because that is what every ``DateTime`` column
    here holds: the models write ``datetime.now(timezone.utc)`` into naive
    columns and both dialects drop the offset on the way in. Carrying an aware
    value through would store the wrong wall clock, and comparing one against a
    value read back out of a naive column raises ``TypeError``. The collector
    only ever writes naive strings, so this is a guard on hand-edited or
    foreign backups rather than a path BamDude takes itself.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _spool_composite_key(parsed: dict) -> tuple | None:
    """A parsed backup row's descriptive identity, not its current tag holder's."""
    keys = ("material", "brand", "subtype", "color_name")
    if not isinstance(parsed["created_at"], datetime) or any(parsed[key] is INVALID for key in keys):
        return None
    return (
        *(SPOOL_FIELDS[key].default if parsed[key] is MISSING else parsed[key] for key in keys),
        parsed["created_at"],
    )


def _created_at_matches(row, created_at: datetime | None) -> bool:
    """Does ``row.created_at`` equal a timestamp read out of a backup?

    Compared in Python, not in SQL, and that is the whole point. Every
    ``created_at`` these callers dedupe on is ``server_default=func.now()``, so
    SQLite fills it from ``CURRENT_TIMESTAMP``, which has second precision and
    stores ``'2026-08-02 11:28:41'``. SQLAlchemy binds a Python datetime as
    ``'2026-08-02 11:28:41.000000'``, and SQLite compares the two as strings —
    so ``Model.created_at == created_at`` never matches a row the application
    itself created, not even when handed that row's own value straight back.
    Every dedupe keyed on it misses, and the restore inserts a duplicate of
    everything instead of recognising what is already there.

    Reading the candidates back and comparing the parsed datetimes sidesteps
    the bind format entirely, and is equally correct on PostgreSQL (where the
    column keeps microseconds and the SQL comparison happened to work).
    """
    return created_at is not None and row.created_at == created_at


def _is_blocked_setting_key(key: str) -> bool:
    lowered = key.lower()
    return key in _SENSITIVE_SETTING_KEYS or any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _is_protected_setting_key(key: str) -> bool:
    from backend.app.core.credential_config import CREDENTIAL_SETTING_KEYS

    # Lowered for the prefix test for the same reason _is_blocked_setting_key
    # lowers: the key comes from the backup's JSON, not from our own writer, so
    # its casing is whatever the file says. An exact-match name stays exact —
    # those four are ours and are only ever written lowercase.
    return (
        key in _PROTECTED_SETTING_KEYS
        or key.lower() in CREDENTIAL_SETTING_KEYS
        or key.lower().startswith(_PROTECTED_SETTING_PREFIXES)
    )


# These edits use the owning Settings credential resolver against the retained
# local/default/environment configuration. Backup credential presence is never
# authority. Anonymous MQTT and VP proxy retain the owning editor's exceptions;
# LDAP and authentication policy are always outside partial restore.
_COMPANION_CREDENTIALS = {
    "prometheus_enabled": "prometheus_token",
    "mqtt_enabled": "mqtt_password",
    "ha_enabled": "ha_token",
    "virtual_printer_enabled": "virtual_printer_access_code",
}
_OWNING_GROUP_EDIT_GROUPS = (
    frozenset({"prometheus_enabled"}),
    frozenset({"mqtt_enabled"}),
    frozenset({"ha_enabled"}),
    frozenset({"virtual_printer_enabled", "virtual_printer_archive_name_source"}),
)
_OWNING_GROUP_EDIT_KEYS = frozenset().union(*_OWNING_GROUP_EDIT_GROUPS)

# Companion credentials a reader takes from the environment rather than from a
# Settings row. ha_token is the only one: get_homeassistant_settings prefers
# HA_TOKEN over the row, and auto-enables ha_enabled when HA_URL and HA_TOKEN are
# both set, so an env-configured instance has a usable credential and no row.
_COMPANION_CREDENTIAL_ENV = {"ha_token": "HA_TOKEN"}

# Metrics is an exposure toggle. Kept as explicit policy metadata for the
# service's direct predicate tests; actual admission is delegated to the owner.
_COMPANION_EXPOSURE_TOGGLES = frozenset({"prometheus_enabled"})


def _setting_value_is_true(value: object) -> bool:
    """Interpret an existing toggle for a read-only destination comparison.

    Restorable payload booleans separately use the owning schema and canonical
    storage encoding in _restore_setting_value.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() == "true"


def _is_usable_credential(value: object) -> bool:
    """True if a credential value is present and not blank.

    A present-but-*blank* ``prometheus_token`` row counts as unusable, because an
    empty token is exactly the ``if token:`` hole the companion rule exists to
    stop a restore from opening.
    """
    return value is not None and bool(str(value).strip())


# Deliberately explicit: adding a future Settings editor field must not silently
# grant an old Git backup permission to change it. Validation still belongs to
# the current editor schema, rather than a second set of restore validators.
_RESTORABLE_SETTING_KEYS = frozenset(
    [
        "save_thumbnails",
        "capture_finish_photo",
        "camera_light_auto",
        "camera_light_auto_obico",
        "delete_timelapse_after_attach",
        "archive_3mf_retention_enabled",
        "archive_3mf_retention_days",
        "log_retention_days",
        "slow_query_ms",
        "slow_request_ms",
        "runout_zero_point_enabled",
        "runout_archive_spool_enabled",
        "ams_sync_bidirectional",
        "runout_purge_grams",
        "usage_events_retention_hours",
        "default_filament_cost",
        "currency",
        "energy_cost_per_kwh",
        "energy_price_source",
        "energy_price_ha_entity",
        "document_supplier_name",
        "document_supplier_address",
        "document_supplier_phone",
        "document_supplier_code",
        "document_supplier_iban",
        "spoolman_enabled",
        "zigbee_enabled",
        "device_labels_enabled",
        "zigbee_transport",
        "zigbee_path",
        "spoolman_url",
        "spoolman_sync_mode",
        "spoolman_disable_weight_sync",
        "spoolman_report_partial_usage",
        "auto_add_unknown_rfid",
        "disable_filament_warnings",
        "spool_display_template",
        "check_updates",
        "check_printer_firmware",
        "include_beta_updates",
        "telemetry_enabled",
        "language",
        "bed_cooled_threshold",
        "ams_humidity_good",
        "ams_humidity_fair",
        "ams_temp_good",
        "ams_temp_fair",
        "ams_temp_alarm",
        "ams_history_retention_days",
        "inbox_retention_days",
        "plug_power_history_retention_days",
        "sensor_history_retention_days",
        "plug_power_sample_seconds",
        "printer_sensor_history_retention_days",
        "prefer_lowest_filament",
        "queue_shortest_first",
        "auto_queue_rebalance_models",
        "auto_queue_compatible_models",
        "model_dispatch_order",
        "auto_queue_return_failed",
        "auto_order_for_batches",
        "default_force_color_match",
        "preheat_enabled",
        "preheat_filament_targets",
        "preheat_max_wait_seconds",
        "preheat_soak_seconds",
        "queue_drying_enabled",
        "queue_drying_block",
        "ambient_drying_enabled",
        "print_drying_enabled",
        "auto_drying_sustained_minutes",
        "drying_presets",
        "ams_humidity_thresholds",
        "zigbee_sensor_reporting",
        "zigbee_sensor_stale_multiplier",
        "zigbee_sensor_poll_seconds",
        "stagger_enabled",
        "stagger_concurrent",
        "stagger_interval_minutes",
        "stagger_wait_for_bed",
        "stagger_strict_for_direct_dispatch",
        "forecast_upload_seconds",
        "forecast_operator_absence_enabled",
        "forecast_operator_absence_start",
        "forecast_operator_absence_end",
        "forecast_operator_timezone",
        "forecast_plate_clear_minutes",
        "stagger_split_by_tags",
        "stagger_group_tag_ids",
        "stagger_split_by_location",
        "stagger_group_location_ids",
        "stagger_tag_limits",
        "stagger_location_limits",
        "per_printer_mapping_expanded",
        "date_format",
        "time_format",
        "default_printer_id",
        "virtual_printer_enabled",
        "virtual_printer_mode",
        "virtual_printer_archive_name_source",
        "dark_style",
        "dark_background",
        "dark_accent",
        "light_style",
        "light_background",
        "light_accent",
        "ftp_retry_enabled",
        "ftp_retry_count",
        "ftp_retry_delay",
        "ftp_timeout",
        "mqtt_enabled",
        "mqtt_topic_prefix",
        "external_url",
        "docker_compose_dir",
        "ha_enabled",
        "library_disk_warning_gb",
        "library_all_files_recursive",
        "firmware_batch_concurrency",
        "camera_view_mode",
        "slice_engine",
        "preferred_slicer",
        "open_in_slicer",
        "use_slicer_api",
        "orcaslicer_api_url",
        "bambu_studio_api_url",
        "slicer_stall_timeout_minutes",
        "prometheus_enabled",
        "low_stock_threshold",
        "forecast_global_lead_time_days",
        "location_sensor_poll_interval",
        "location_sensor_alert_defaults",
        "user_notifications_enabled",
        "local_backup_enabled",
        "local_backup_schedule",
        "local_backup_time",
        "local_backup_retention",
        "local_backup_path",
        "obico_enabled",
        "obico_save_detections",
        "obico_sensitivity",
        "obico_action",
        "obico_poll_interval",
        "obico_enabled_printers",
        # ``octoeverywhere_enabled`` is not here: a restore never starts sending frames to the cloud.
        "octoeverywhere_confidence",
        "octoeverywhere_action",
        "octoeverywhere_poll_interval",
        "octoeverywhere_enabled_printers",
        "default_sidebar_order",
        "gcode_snippets",
    ]
)

_RESTORE_AUTH_POLICY_KEYS = frozenset(
    {"session_max_hours", "telegram_registration_open", "connector_enabled", "connector_max_upload_bytes"}
)
_RESTORE_RUNTIME_KEYS = frozenset({"secret_actions", "ha_url_from_env", "ha_token_from_env", "ha_env_managed"})
_FROZEN_DESTINATION_KEYS = frozenset(
    {
        "mqtt_topic_prefix",
        "spoolman_url",
        "orcaslicer_api_url",
        "bambu_studio_api_url",
        "external_url",
        "docker_compose_dir",
        "local_backup_path",
        "zigbee_transport",
        "zigbee_path",
        "virtual_printer_mode",
        "preferred_slicer",
        "slice_engine",
        "use_slicer_api",
    }
)
_URL_DESTINATION_KEYS = frozenset({"spoolman_url", "orcaslicer_api_url", "bambu_studio_api_url", "external_url"})
_OPERATOR_SETTING_KEYS = frozenset(
    {
        "forecast_operator_absence_enabled",
        "forecast_operator_absence_start",
        "forecast_operator_absence_end",
        "forecast_operator_timezone",
    }
)
_SETTINGS_REFUSAL_BUCKETS = (
    "blocked",
    "protected",
    "companion",
    "unknown",
    "invalid",
    "runtime",
    "destination",
    "masked",
)
_MASKED_SETTING_VALUE = re.compile(r"(?:\*{3,}|[•●]{3,}|\[redacted\]|<redacted>|__redacted__)", re.IGNORECASE)


def _restore_setting_value(key: str, value: object) -> str:
    """Validate the backup's storage representation through the owning schema.

    Update fields are optional to support PATCH; null is only restorable when
    the *effective* settings field is nullable too. Booleans use the storage
    reader's exact vocabulary, avoiding lax Pydantic ``on``/``1`` coercion.
    """
    from backend.app.schemas.settings import AppSettings, AppSettingsUpdate

    field = AppSettings.model_fields[key]
    if field.annotation is bool:
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        elif not isinstance(value, bool):
            raise ValueError("Invalid stored boolean")
    elif isinstance(value, bool):
        raise ValueError("Boolean is not a numeric or string setting")
    if value == "None" and type(None) in getattr(field.annotation, "__args__", ()):
        value = None
    updated = AppSettingsUpdate.model_validate({key: value})
    validated = TypeAdapter(field.rebuild_annotation()).validate_python(getattr(updated, key))
    if isinstance(validated, float) and not math.isfinite(validated):
        raise ValueError("Non-finite setting")
    if isinstance(validated, bool):
        return "true" if validated else "false"
    return str(validated)


def _local_destination(key: str, local: dict[str, str], defaults) -> str:
    """Resolve only destinations, matching their existing owning readers."""
    from backend.app.core.config import settings

    value = local.get(key, getattr(defaults, key))
    if key == "use_slicer_api":
        return "true" if _setting_value_is_true(value) else "false"
    if key == "preferred_slicer":
        return value or defaults.preferred_slicer
    if key == "orcaslicer_api_url":
        return value or settings.slicer_api_url
    if key == "bambu_studio_api_url":
        return value or settings.bambu_studio_api_url
    if key == "external_url":
        return value or os.environ.get("APP_URL", "http://localhost:5173")
    if key == "local_backup_path":
        return value or str(settings.base_dir / "backups")
    return value


@dataclass(frozen=True)
class _SettingsPlan:
    """Which keys of a settings payload will not be written, and why.

    Built once, before anything is added to the session, and shared by the
    preview and the restore so the two cannot disagree about what a commit will
    change. The companion bucket is why this needs a session at all: unlike the
    two name-based buckets it depends on local database state.

    The refusal buckets are disjoint — a key is classified once, in order.
    Accepted values are already validated and encoded for the settings reader.
    Destination no-ops remain counted but preserve the local row verbatim.
    """

    blocked: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    companion: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    invalid: tuple[str, ...] = ()
    runtime: tuple[str, ...] = ()
    destination: tuple[str, ...] = ()
    masked: tuple[str, ...] = ()
    accepted: dict[str, str] = dataclasses_field(default_factory=dict)
    no_op: frozenset[str] = frozenset()

    @property
    def refused(self) -> frozenset[str]:
        return frozenset().union(*(getattr(self, name) for name in _SETTINGS_REFUSAL_BUCKETS))

    @property
    def refused_count(self) -> int:
        return sum(len(getattr(self, name)) for name in _SETTINGS_REFUSAL_BUCKETS)


@dataclass(frozen=True)
class _Detail:
    """A preview caveat, as a translation code plus its English rendering.

    Same contract as a note: the client translates ``code`` with ``params`` and
    falls back to ``message``.
    """

    code: str
    message: str
    params: dict[str, str | int] = dataclasses_field(default_factory=dict)


def _restore_failure_message(error: BaseException) -> str:
    """What a failed restore's log says (C047 S43).

    A coded refusal — ``{"error", "message"}``, e.g. ``spoolman_print_in_progress`` with
    the text of its reason — says its own sentence; any other HTTP refusal came from
    the actor's guard and keeps the authorization text; anything else its own text.
    """
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, dict) and isinstance(detail.get("error"), str) and isinstance(detail.get("message"), str):
            return detail["message"][:1000]
        return "Restore authorization is no longer valid"
    return str(error)[:1000]


class _CategoryTally:
    """Mutable accumulator matching ``GitHubRestoreCategoryResult``."""

    def __init__(self) -> None:
        self.restored = 0
        self.skipped = 0
        self.failed = 0
        self.notes: list[dict] = []

    def note(self, code: str, message: str, **params) -> None:
        """Record a note as a translation code, its params and an English fallback.

        Deduped on ``(code, params)`` rather than on the rendered text, which is
        the same thing today but keeps two notes that differ only in a printer
        name from collapsing into one. Bounded for the reason it always was: the
        UI renders every note, so a large backup must not emit one per row.
        """
        if any(existing["code"] == code and existing["params"] == params for existing in self.notes):
            return
        if len(self.notes) >= 20:
            return
        self.notes.append({"code": code, "params": params, "message": message})

    def as_dict(self) -> dict:
        return {"restored": self.restored, "skipped": self.skipped, "failed": self.failed, "notes": self.notes}


class GitRestoreService:
    """Reads a backup repository and applies selected categories locally."""

    def __init__(self) -> None:
        self._running_restore: bool = False
        self._progress: str | None = None
        self._http_client: httpx.AsyncClient | None = None
        # Guards the check-then-set on ``_running_restore``. Without it two
        # concurrent POSTs can both observe False before either sets it.
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=60.0)
        return self._http_client

    @property
    def is_running(self) -> bool:
        return self._running_restore

    @property
    def progress(self) -> str | None:
        return self._progress

    # --- Repository reads --------------------------------------------------

    async def list_commits(self, config: GitBackupConfig, limit: int = 20) -> dict:
        """List recent commits on the configured branch."""
        backend = get_provider_backend(config.provider)
        client = await self._get_client()
        result = await backend.list_commits(
            repo_url=config.repository_url,
            token=config.access_token,
            branch=config.branch,
            client=client,
            limit=limit,
        )
        result["branch"] = config.branch
        return result

    async def _resolve_ref(self, config: GitBackupConfig, ref: str) -> tuple[str | None, str, dict | None]:
        """Turn ``HEAD`` into a concrete commit SHA.

        Done once up front so a preview and the restore that follows it act on
        the same commit even if a scheduled backup lands in between.

        The third element is the commit entry, when resolving already fetched
        one. ``preview`` displays it, and taking it from here means the ``HEAD``
        case — by far the common one — costs one ``list_commits`` call rather
        than two.
        """
        if ref and ref.upper() != "HEAD":
            return ref, "", None
        result = await self.list_commits(config, limit=1)
        if not result.get("success"):
            return None, result.get("message") or "Could not read the backup repository", None
        commits = result.get("commits") or []
        if not commits:
            return None, f"Branch '{config.branch}' has no commits to restore from", None
        return commits[0]["sha"], "", commits[0]

    async def _describe_commit(self, config: GitBackupConfig, resolved: str) -> dict | None:
        """Find the display metadata for one commit SHA.

        Two things used to leave ``commit: null`` in a preview, and the second is
        the one that bit in practice:

        * the commit is older than the 20 the picker lists, so it is not in the
          scan at all — that is what ``get_commit`` is for;
        * ``REF_PATTERN`` accepts a 7-character ref while providers return the
          full 40, so an exact ``==`` never matched an abbreviated SHA *even when
          the commit was in the window*. Hence the prefix comparison.

        Best-effort throughout: this is a subject line and a date, so a failure
        returns None and the preview renders without them rather than failing.
        """
        commits = (await self.list_commits(config, limit=20)).get("commits") or []
        for entry in commits:
            sha = entry.get("sha") or ""
            if sha == resolved or sha.startswith(resolved) or resolved.startswith(sha):
                return entry

        backend = get_provider_backend(config.provider)
        client = await self._get_client()
        result = await backend.get_commit(
            repo_url=config.repository_url, token=config.access_token, ref=resolved, client=client
        )
        return result.get("commit") if result.get("success") else None

    def _category_paths(self, category: RestoreCategory, available: list[str]) -> list[str]:
        """Return the paths in ``available`` that belong to ``category``."""
        if category == RestoreCategory.SETTINGS:
            return [p for p in (SETTINGS_PATH,) if p in available]
        if category == RestoreCategory.SPOOLS:
            return [p for p in (SPOOLS_PATH, SPOOL_USAGE_PATH, SPOOLMAN_USAGE_PATH) if p in available]
        if category == RestoreCategory.ARCHIVES:
            return [p for p in (ARCHIVES_PATH,) if p in available]
        if category == RestoreCategory.KPROFILES:
            return sorted(p for p in available if _KPROFILE_PATH_RE.match(p))
        return []

    @staticmethod
    def _parse_json_files(raw: dict[str, str]) -> tuple[dict[str, object], list[str]]:
        """Parse each fetched file, collecting paths that failed to parse."""
        parsed: dict[str, object] = {}
        bad: list[str] = []
        for path, text in raw.items():
            try:
                parsed[path] = json.loads(text)
            except (ValueError, TypeError):
                bad.append(path)
        return parsed, bad

    @staticmethod
    async def _plan_settings(db: AsyncSession, values: dict, *, overwrite: bool = True) -> _SettingsPlan:
        """One fail-closed classifier for preview and guarded apply.

        Apply owns the credential anchor before this read. Always refresh the
        identity map: a previously generated preview is not current authority.
        The result contains canonical storage values, never backup raw values.
        """
        from backend.app.core.credential_config import CREDENTIAL_SETTING_KEYS, canonical_url
        from backend.app.schemas.settings import AppSettings

        buckets = {name: [] for name in _SETTINGS_REFUSAL_BUCKETS}
        accepted: dict[str, str] = {}
        no_op: set[str] = set()
        wanted = (
            _FROZEN_DESTINATION_KEYS
            | CREDENTIAL_SETTING_KEYS
            | _OWNING_GROUP_EDIT_KEYS
            | _OPERATOR_SETTING_KEYS
            | set(_COMPANION_CREDENTIALS)
            | set(_COMPANION_CREDENTIALS.values())
        )
        rows = await db.execute(
            select(Settings).where(Settings.key.in_(wanted)).execution_options(populate_existing=True)
        )
        local = {row.key: row.value for row in rows.scalars().all()}
        defaults = AppSettings()
        for key, value in values.items():
            if not isinstance(key, str) or not key:
                buckets["invalid"].append(key)
                continue
            if key in _RESTORE_RUNTIME_KEYS or key.endswith(("_configured", "_from_env")):
                reason = "runtime"
            elif _is_blocked_setting_key(key):
                reason = "blocked"
            elif _is_protected_setting_key(key) or key in _RESTORE_AUTH_POLICY_KEYS or key.startswith("mcp_"):
                reason = "protected"
            elif key.startswith("_"):
                reason = "runtime"
            elif key not in _RESTORABLE_SETTING_KEYS:
                reason = "unknown"
            elif isinstance(value, str) and _MASKED_SETTING_VALUE.fullmatch(value.strip()):
                reason = "masked"
            else:
                try:
                    normalized = _restore_setting_value(key, value)
                except (ValidationError, ValueError, TypeError, KeyError):
                    buckets["invalid"].append(key)
                    continue
                if key in _FROZEN_DESTINATION_KEYS:
                    current = _local_destination(key, local, defaults)
                    incoming = _local_destination(key, {**local, key: normalized}, defaults)
                    if key in _URL_DESTINATION_KEYS:
                        current, incoming = canonical_url(current), canonical_url(incoming)
                    if current != incoming:
                        buckets["destination"].append(key)
                        continue
                    # Even an equivalent address is not written: retain opaque
                    # legacy tuples and never copy environment defaults to DB.
                    no_op.add(key)
                accepted[key] = normalized
                continue
            buckets[reason].append(key)
        for group in _OWNING_GROUP_EDIT_GROUPS:
            actual_edits = {
                key: accepted[key] == "true" if key in _COMPANION_CREDENTIALS else accepted[key]
                for key in sorted(group & accepted.keys())
                if overwrite or key not in local
            }
            if not actual_edits:
                continue
            from backend.app.api.routes.settings import resolve_settings_credentials

            # Validate each complete actual adapter edit against retained
            # DB/default/environment credentials. A paired disable+metadata
            # change must see the disabled final state. Refused backup secrets
            # can neither satisfy nor trigger the owning credential rule.
            try:
                await resolve_settings_credentials(db, actual_edits)
            except HTTPException as exc:
                if exc.status_code not in {400, 422}:
                    raise
                for key in actual_edits:
                    buckets["companion"].append(key)
                    accepted.pop(key)
        operator_edits = {key for key in _OPERATOR_SETTING_KEYS & accepted.keys() if overwrite or key not in local}
        if operator_edits:
            # Owning PUT validates this effective pair in addition to its field
            # schemas. Existing rows skipped by overwrite=False are not edits:
            # a valid backup pair must not become an invalid partial write.
            effective = {
                key: accepted[key]
                if key in accepted and (overwrite or key not in local)
                else local.get(key, getattr(defaults, key))
                for key in ("forecast_operator_absence_start", "forecast_operator_absence_end")
            }
            if effective["forecast_operator_absence_start"] == effective["forecast_operator_absence_end"]:
                for key in sorted(operator_edits):
                    buckets["invalid"].append(key)
                    accepted.pop(key)
        return _SettingsPlan(
            **{name: tuple(keys) for name, keys in buckets.items()}, accepted=accepted, no_op=frozenset(no_op)
        )

    async def preview(self, db: AsyncSession, config: GitBackupConfig, ref: str = "HEAD") -> dict:
        """Report which categories a commit contains, and how much is in each.

        Takes a session because the settings count depends on local state — see
        ``_plan_settings``. ``ref`` stays keyword-friendly for callers.
        """
        resolved, error, commit_info = await self._resolve_ref(config, ref)
        if resolved is None:
            return {"success": False, "message": error, "ref": ref, "categories": []}

        backend = get_provider_backend(config.provider)
        client = await self._get_client()

        tree = await backend.list_tree(
            repo_url=config.repository_url, token=config.access_token, ref=resolved, client=client
        )
        if not tree.get("success"):
            return {"success": False, "message": tree.get("message") or "Could not list the commit", "ref": resolved}
        available: list[str] = tree.get("paths") or []

        # One batched read covers metadata plus every category payload.
        wanted = [METADATA_PATH] if METADATA_PATH in available else []
        for category in RestoreCategory:
            wanted.extend(self._category_paths(category, available))

        fetched = await backend.fetch_files(
            repo_url=config.repository_url,
            token=config.access_token,
            ref=resolved,
            paths=wanted,
            client=client,
            # The listing above already built this map; without it the GitHub
            # family would GET the same recursive tree a second time.
            blob_shas=tree.get("blob_shas") or None,
        )
        if not fetched.get("success"):
            return {
                "success": False,
                "message": fetched.get("message") or "Could not read the commit contents",
                "ref": resolved,
            }
        parsed, bad_paths = self._parse_json_files(fetched.get("files") or {})

        metadata = parsed.get(METADATA_PATH)
        metadata_version = metadata.get("version") if isinstance(metadata, dict) else None

        categories = []
        for category in RestoreCategory:
            paths = self._category_paths(category, available)
            if not paths:
                categories.append(
                    self._category_entry(category, False, 0, _Detail("notPresent", "Not present in this backup commit"))
                )
                continue
            unreadable = [p for p in paths if p in bad_paths]
            if unreadable:
                joined = ", ".join(unreadable)
                categories.append(
                    self._category_entry(
                        category,
                        False,
                        0,
                        _Detail("unreadableJson", f"Unreadable JSON: {joined}", {"paths": joined}),
                    )
                )
                continue
            count, detail = await self._count_items(db, category, parsed)
            valid_shape = detail is None or detail.code not in {
                "archivesInvalidPayload",
                "spoolsInvalidPayload",
                "spoolUsageInvalidPayload",
                "spoolUsageAliasConflict",
            }
            categories.append(self._category_entry(category, valid_shape, count, detail))

        if commit_info is None:
            commit_info = await self._describe_commit(config, resolved)

        return {
            "success": True,
            "message": "OK",
            "ref": resolved,
            "commit": commit_info,
            "metadata_version": metadata_version,
            "categories": categories,
        }

    @staticmethod
    def _category_entry(category: RestoreCategory, available: bool, item_count: int, detail: _Detail | None) -> dict:
        """Shape one ``GitHubRestorePreviewCategory``, translated detail included."""
        return {
            "category": category,
            "available": available,
            "item_count": item_count,
            "detail": detail.message if detail else None,
            "detail_code": detail.code if detail else None,
            "detail_params": detail.params if detail else {},
        }

    async def _count_items(
        self, db: AsyncSession, category: RestoreCategory, parsed: dict
    ) -> tuple[int, _Detail | None]:
        """Count restorable items for ``category`` and describe any caveat."""
        if category == RestoreCategory.SETTINGS:
            payload = parsed.get(SETTINGS_PATH)
            values = payload.get("settings") if isinstance(payload, dict) else None
            if not isinstance(values, dict):
                return 0, _Detail("settingsNoPayload", "No settings in payload")
            # Every refusal is subtracted so the count matches what the restore
            # actually writes. The wording calls out the credential ones (what a
            # user might expect to come back) and the companion ones (a
            # behaviour change worth explaining before it happens); the auth
            # policy keys stay unmentioned on purpose.
            plan = await self._plan_settings(db, values)
            detail = None
            if any(getattr(plan, name) for name in _SETTINGS_REFUSAL_BUCKETS[3:]) or plan.protected:
                params = {name: len(getattr(plan, name)) for name in _SETTINGS_REFUSAL_BUCKETS}
                params["credentials"] = params.pop("blocked")
                params["count"] = plan.refused_count
                detail = _Detail(
                    "settingsPolicyWillSkip",
                    f"{plan.refused_count} setting(s) cannot be restored "
                    f"(credentials: {params['credentials']}; protected: {params['protected']}; "
                    f"integration settings: {params['companion']}; unknown: {params['unknown']}; "
                    f"invalid: {params['invalid']}; runtime: {params['runtime']}; "
                    f"local destinations: {params['destination']}; masks: {params['masked']})",
                    params,
                )
            elif plan.companion and not plan.blocked:
                # An exposure toggle becomes a candidate whether or not the
                # backup carried its credential, so this commit can refuse a
                # switch without having a single credential-like key to skip —
                # "0 credential-like key(s) will be skipped" would read as noise.
                detail = _Detail(
                    "settingsCompanionOnlyWillSkip",
                    f"{len(plan.companion)} integration setting(s) will be skipped — retained local credentials "
                    "do not satisfy current Settings checks",
                    {"companion": len(plan.companion)},
                )
            elif plan.companion:
                detail = _Detail(
                    "settingsCompanionWillSkip",
                    f"{len(plan.blocked)} credential-like key(s) will be skipped, and "
                    f"{len(plan.companion)} integration setting(s) that fail local credential checks will be skipped",
                    {"count": len(plan.blocked), "companion": len(plan.companion)},
                )
            elif plan.blocked:
                detail = _Detail(
                    "settingsCredentialsWillSkip",
                    f"{len(plan.blocked)} credential-like keys will be skipped",
                    {"count": len(plan.blocked)},
                )
            return len(values) - plan.refused_count, detail

        if category == RestoreCategory.SPOOLS:
            try:
                spools, usage = spool_rows(parsed.get(SPOOLS_PATH, MISSING), parsed.get(SPOOL_USAGE_PATH, MISSING))
            except PayloadShapeError as exc:
                return 0, _Detail(exc.code, "Spool metadata has an invalid shape or conflicting history lists")
            # Usage records are counted here, not just described in the detail:
            # _restore_spool_usage increments this category's tally, so counting
            # only the spools broke restored + skipped + failed == item_count —
            # the invariant the settings count is careful to hold. The detail
            # breaks the total down rather than adding to it.
            count = len(spools) if isinstance(spools, list) else 0
            detail = None
            # The Spoolman usage rows are usage records of this category too,
            # and every one of them ends in its tally (restored, skipped or
            # failed), so they are counted with the built-in ones.
            records = (len(usage) if isinstance(usage, list) else 0) + len(
                self._spoolman_usage_entries(parsed.get(SPOOLMAN_USAGE_PATH))
            )
            if records:
                count += records
                detail = _Detail("spoolsUsageCount", f"including {records} usage records", {"count": records})
            return count, detail

        if category == RestoreCategory.ARCHIVES:
            try:
                archives = archive_rows(parsed.get(ARCHIVES_PATH, MISSING))
            except PayloadShapeError as exc:
                return 0, _Detail(exc.code, "Archive metadata must contain an archives list")
            count = len(archives) if isinstance(archives, list) else 0
            return count, _Detail(
                "archivesMetadataOnly", "Metadata only — 3MF files and thumbnails are not in a Git backup"
            )

        if category == RestoreCategory.KPROFILES:
            total = 0
            serials = set()
            for path, payload in parsed.items():
                match = _KPROFILE_PATH_RE.match(path)
                if not match or not isinstance(payload, dict):
                    continue
                serials.add(match.group(1))
                profiles = payload.get("profiles")
                if isinstance(profiles, list):
                    total += len(profiles)
            detail = None
            if serials:
                detail = _Detail("kprofilesPrinterCount", f"across {len(serials)} printer(s)", {"count": len(serials)})
            return total, detail

        return 0, None

    # --- Restore -----------------------------------------------------------

    async def run_restore(
        self, config_id, ref, categories, overwrite_existing=False, *, authority=None, required_permissions=()
    ):
        from backend.app.services.backup_files import BackupBusyError, exclusive_operation
        from backend.app.services.git_backup import git_backup_service

        if git_backup_service.is_running:
            return {
                "success": False,
                "message": "A backup is currently running. Wait for it to finish before restoring.",
                "results": {},
            }
        try:
            async with exclusive_operation():
                return await self._run_restore_owned(
                    config_id,
                    ref,
                    categories,
                    overwrite_existing,
                    authority=authority,
                    required_permissions=required_permissions,
                )
        except BackupBusyError:
            return {"success": False, "message": "A restore is already running", "results": {}}

    async def _run_restore_owned(
        self,
        config_id: int,
        ref: str,
        categories: list[RestoreCategory],
        overwrite_existing: bool = False,
        *,
        authority=None,
        required_permissions=(),
    ) -> dict:
        """Own HTTP restores through remote work and every final publication.

        Direct trusted Python callers may omit authority. HTTP always passes
        the original browser capture; it never resolves a new credential.
        """
        if authority is not None:
            from backend.app.core.auth_runtime import auth_runtime

            async with auth_runtime.operation():
                return await self._run_restore(
                    config_id,
                    ref,
                    categories,
                    overwrite_existing,
                    authority=authority,
                    required_permissions=required_permissions,
                )
        return await self._run_restore(config_id, ref, categories, overwrite_existing)

    @staticmethod
    async def _guard_restore_actor(db, authority, required_permissions):
        if authority is None:
            return
        from backend.app.core.auth_lifetime import guard_accounts, validate_captured
        from backend.app.core.credential_config import lock_credential_config
        from backend.app.core.permissions import Permission

        if authority.kind != "session":
            raise HTTPException(403, "A signed-in administrator is required")
        # Shared B anchor precedes the C account lock and all domain rows.
        await lock_credential_config(db)
        proof = await guard_accounts(db, [authority])
        principal = await validate_captured(
            db, authority, required_permissions=(Permission.GIT_RESTORE, *required_permissions), proof=proof
        )
        if not principal.user.is_admin:
            raise HTTPException(403, "Administrator required")

    async def _run_restore(
        self,
        config_id: int,
        ref: str,
        categories: list[RestoreCategory],
        overwrite_existing: bool = False,
        *,
        authority=None,
        required_permissions=(),
    ) -> dict:
        """Apply selected categories from one backup commit."""
        # Import locally to avoid a module-level cycle: the backup service takes
        # the mirror-image lock against us.
        from backend.app.services.farm_startup_guard import farm_startup_guard
        from backend.app.services.git_backup import git_backup_service
        from backend.app.services.virtual_printer import virtual_printer_manager
        from backend.app.services.virtual_printer.lifecycle import VPDrainRefused, drain_warning

        # The outer shared operation guard excludes ZIP/Git backups and restores.
        # Keep this service's state transition await-free under its own lock too.
        async with self._lock:
            if self._running_restore:
                return {"success": False, "message": "A restore is already running", "results": {}}
            if git_backup_service.is_running:
                return {
                    "success": False,
                    "message": "A backup is currently running. Wait for it to finish before restoring.",
                    "results": {},
                }
            self._running_restore = True

        log_id = None
        try:
            async with async_session() as db:
                result = await db.execute(select(GitBackupConfig).where(GitBackupConfig.id == config_id))
                config = result.scalar_one_or_none()
                if not config:
                    return {"success": False, "message": "Configuration not found", "results": {}}
                config = SimpleNamespace(
                    **{
                        key: getattr(config, key)
                        for key in ("id", "provider", "repository_url", "access_token", "branch", "api_base_url")
                    }
                )
                await self._guard_restore_actor(db, authority, required_permissions)
                await db.rollback()

                self._progress = "Resolving commit..."
                resolved, error, _ = await self._resolve_ref(config, ref)
                if resolved is None:
                    return {"success": False, "message": error, "results": {}}

                log = GitBackupLog(config_id=config_id, status="running", trigger="restore", commit_sha=resolved)
                db.add(log)
                await db.commit()
                log_id = log.id

                # Owned here rather than by _apply so the failure path can see
                # the categories that were already committed when the raise
                # happened. _apply records a tally only after its category's
                # commit, so every entry present is on disk.
                results: dict[str, _CategoryTally] = {}
                try:
                    await self._guard_restore_actor(db, authority, required_permissions)
                    await db.rollback()
                    payload, error = await self._read_categories(config, resolved, categories)
                    if error:
                        raise RuntimeError(error)

                    settings_keys_written: set[str] = set()
                    if authority is not None:
                        # The operational hold is itself a privileged effect;
                        # a revoked download must not stop an untouched farm.
                        await self._guard_restore_actor(db, authority, required_permissions)
                    # Release the read snapshot even for trusted internal calls.
                    await db.rollback()
                    farm_startup_guard.hold("restore")
                    from backend.app.services.farm_startup_guard import notify_change

                    await notify_change()
                    drain = await virtual_printer_manager.drain_for_restore()
                    await self._apply(
                        db,
                        payload,
                        categories,
                        overwrite_existing,
                        settings_keys_written,
                        results=results,
                        authority=authority,
                        required_permissions=required_permissions,
                    )
                    await db.commit()

                    # After the commit: this reconnects the relay, which is not
                    # something to do on values that could still roll back.
                    settings_tally = results.get(RestoreCategory.SETTINGS.value)
                    if settings_tally is not None:
                        self._progress = "Reconnecting the MQTT relay..."
                        await self._reconfigure_mqtt_relay(db, settings_keys_written, settings_tally)

                    total_restored = sum(tally.restored for tally in results.values())
                    any_failed = any(tally.failed for tally in results.values())

                    log.status = "failed" if any_failed and total_restored == 0 else "success"
                    log.completed_at = datetime.now(timezone.utc)
                    log.files_changed = total_restored
                    if any_failed:
                        log.error_message = "Some items could not be restored — see the restore result for detail"
                    await db.commit()

                    from backend.app.i18n.api_errors import current_language, translate_sentence

                    return {
                        "success": True,
                        "message": (
                            translate_sentence("Restored {} item(s) from {}", current_language()).format(
                                total_restored, resolved[:7]
                            )
                            + " "
                            + drain_warning(drain, git=True)
                        ).strip(),
                        "log_id": log_id,
                        "ref": resolved,
                        "results": {name: tally.as_dict() for name, tally in results.items()},
                    }

                except VPDrainRefused as exc:
                    await db.rollback()
                    refused_log = await db.get(GitBackupLog, log_id)
                    if refused_log:
                        refused_log.status = "failed"
                        refused_log.completed_at = datetime.now(timezone.utc)
                        refused_log.files_changed = 0
                        refused_log.error_message = str(exc)
                        await db.commit()
                    raise
                except Exception as e:
                    # Rolls back the category that was mid-flight. Every category
                    # already in ``results`` committed as it finished (see
                    # _apply), so those rows survive this — and reporting an
                    # empty result over them would tell the user nothing was
                    # restored while their archives and spools are on disk.
                    if not isinstance(e, HTTPException):
                        logger.exception("Restore failed for config %s ref %s", config_id, resolved)
                    await db.rollback()
                    committed = sum(tally.restored for tally in results.values())
                    log.status = "failed"
                    log.completed_at = datetime.now(timezone.utc)
                    log.files_changed = committed
                    log.error_message = _restore_failure_message(e)
                    await db.commit()
                    if isinstance(e, HTTPException):
                        raise
                    return {
                        "success": False,
                        "message": str(e),
                        "log_id": log_id,
                        "ref": resolved,
                        "results": {name: tally.as_dict() for name, tally in results.items()},
                    }

        finally:
            self._running_restore = False
            self._progress = None

    async def _read_categories(
        self, config: GitBackupConfig, ref: str, categories: list[RestoreCategory]
    ) -> tuple[dict, str]:
        """Fetch and parse just the files the requested categories need."""
        backend = get_provider_backend(config.provider)
        client = await self._get_client()

        self._progress = "Listing backup contents..."
        tree = await backend.list_tree(
            repo_url=config.repository_url, token=config.access_token, ref=ref, client=client
        )
        if not tree.get("success"):
            return {}, tree.get("message") or "Could not list the commit"
        available: list[str] = tree.get("paths") or []

        wanted: list[str] = []
        for category in categories:
            wanted.extend(self._category_paths(category, available))
        if not wanted:
            return {}, "None of the selected categories are present in that commit"

        self._progress = "Downloading backup files..."
        fetched = await backend.fetch_files(
            repo_url=config.repository_url,
            token=config.access_token,
            ref=ref,
            paths=wanted,
            client=client,
            blob_shas=tree.get("blob_shas") or None,
        )
        if not fetched.get("success"):
            return {}, fetched.get("message") or "Could not read the commit contents"

        parsed, bad = self._parse_json_files(fetched.get("files") or {})
        if bad:
            return {}, f"Backup contains unreadable JSON: {', '.join(sorted(bad))}"
        return parsed, ""

    async def _apply(
        self,
        db: AsyncSession,
        payload: dict,
        categories: list[RestoreCategory],
        overwrite: bool,
        settings_keys_written: set[str] | None = None,
        results: dict[str, _CategoryTally] | None = None,
        *,
        authority=None,
        required_permissions=(),
    ) -> dict[str, _CategoryTally]:
        """Apply categories in dependency order and return per-category tallies.

        ``settings_keys_written``, if given, collects the setting keys actually
        written, for the caller's post-commit side effects (see
        ``_reconfigure_mqtt_relay``).

        ``results``, if given, is the caller's own dict rather than a fresh one.
        Each category is committed before it is recorded there, so on a raise
        the caller can report exactly what is already on disk — see the
        per-category commit below.
        """
        results = {} if results is None else results
        archive_id_map: dict[int, int] = {}
        archive_proofs: dict[int, ArchiveProof] = {}

        # Every database category commits before the next one starts, and only
        # then is its tally recorded. Two reasons:
        #
        #  * SQLite has one writer. Each category is a long run of one SELECT per
        #    row or per key — _find_archive, _find_spool, the usage dedupe,
        #    _restore_settings — interleaved with autoflushed INSERTs, all inside
        #    the open write transaction. A few thousand archives plus a full
        #    usage history plausibly passes the 15 s busy_timeout
        #    (core/database.py), at which point every concurrent writer in the
        #    app fails with "database is locked". This is the same hold the
        #    K-profile phase had, arriving by volume rather than by awaiting a
        #    sulking printer.
        #  * The ordering tolerates it: the only cross-category state is
        #    archive_id_map and spool_id_map, both plain dicts in memory, and
        #    the session is expire_on_commit=False so nothing reloads.
        #
        # The cost is that a later failure no longer rolls back an earlier
        # category — which is why the tally is recorded after the commit, so
        # run_restore's failure path reports the rows that really landed instead
        # of claiming nothing was restored.

        # Archives first: spool usage history references archive_id.
        if RestoreCategory.ARCHIVES in categories:
            await self._guard_restore_actor(db, authority, required_permissions)
            self._progress = "Restoring print archives..."
            tally = _CategoryTally()
            archive_rows(payload.get(ARCHIVES_PATH, MISSING))  # Shape refusal before category writes.
            await self._begin_record_category(db)
            await self._restore_archives(
                db, payload.get(ARCHIVES_PATH, MISSING), overwrite, tally, archive_id_map, identity_map=archive_proofs
            )
            await db.commit()
            results[RestoreCategory.ARCHIVES.value] = tally

        if RestoreCategory.SPOOLS in categories:
            self._progress = "Restoring spool inventory..."
            tally = _CategoryTally()
            spool_rows(payload.get(SPOOLS_PATH, MISSING), payload.get(SPOOL_USAGE_PATH, MISSING))
            # The category owns the proof of the Spoolman source to its commit (C047 S29, S30):
            # clean session -> source guard -> category transaction -> actor guard -> rows -> commit.
            async with spoolman_source.guard(db), spoolman_activity.arecord("git_spools"):
                try:
                    await self._begin_record_category(db)
                    await self._guard_restore_actor(db, authority, required_permissions)
                    await self._restore_spools(
                        db,
                        payload.get(SPOOLS_PATH, MISSING),
                        payload.get(SPOOL_USAGE_PATH, MISSING),
                        overwrite,
                        tally,
                        archive_id_map,
                        archive_proofs=archive_proofs,
                    )
                    await self._restore_spoolman_usage(
                        db, payload.get(SPOOLMAN_USAGE_PATH), tally, archive_id_map, archive_proofs=archive_proofs
                    )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
            results[RestoreCategory.SPOOLS.value] = tally

        if RestoreCategory.SETTINGS in categories:
            self._progress = "Restoring app settings..."
            tally = _CategoryTally()
            # The Spoolman mode is part of the source (C047 §C1): the same order as SPOOLS, and
            # the actor's proof inside the category's own transaction until its commit (S30).
            async with spoolman_source_changes.owning(db) as source_change:
                # Before the actor's anchor: a change of the mode waits there for the slot-ledger
                # writers, which a door holding SQLite's writer could only refuse (C047 A12). The
                # run proved the actor before its first category; only one revoked since is
                # refused after this wait (A14).
                await source_change.wait_before_anchor(
                    db, enabled=await self._restored_mode(db, payload.get(SETTINGS_PATH), overwrite)
                )
                await self._guard_restore_actor(db, authority, required_permissions)
                await self._restore_settings(
                    db,
                    payload.get(SETTINGS_PATH),
                    overwrite,
                    tally,
                    keys_written=settings_keys_written,
                    source_change=source_change,
                )
                await db.commit()
            results[RestoreCategory.SETTINGS.value] = tally

        # Last, because it leaves the database and publishes over MQTT.
        if RestoreCategory.KPROFILES in categories:
            # The database categories are already committed by the loop above,
            # and that is load-bearing here rather than tidiness:
            # _restore_kprofiles awaits get_kprofiles per printer per nozzle,
            # which is timeout=5.0 * max_retries=3, i.e. up to ~15 s each against
            # an unresponsive printer. Holding SQLite's writer across that would
            # pass the 15 s busy_timeout on a farm with a couple of sulking
            # printers.
            #
            # The cost is that a K-profile failure no longer rolls back the
            # categories that already succeeded. That is the correct trade
            # anyway: extrusion_cali_set has left for the printer by then and
            # cannot be rolled back either, so a rollback would only have made
            # the database disagree with the hardware.

            self._progress = "Sending K-profiles to printers..."
            tally = _CategoryTally()
            try:
                await self._guard_restore_actor(db, authority, required_permissions)
                if authority is not None:
                    await db.rollback()
                await self._restore_kprofiles(
                    db, payload, tally, authority=authority, required_permissions=required_permissions
                )
            except HTTPException:
                raise
            except Exception as e:
                # Everything above is committed and cannot be un-committed, so
                # letting this reach run_restore's handler would report
                # "nothing was restored" over durable archive, spool and
                # settings rows — and skip the post-commit MQTT reconfigure,
                # leaving the relay on the pre-restore broker. The K-profile
                # phase is the last thing that runs, so containing it here is
                # what keeps the result honest about what actually landed.
                logger.exception("The K-profile step failed after the database categories were committed")
                # Discards the phase's own read transaction. The rows above went
                # in at the commit two statements up; this only stops a session
                # left in a failed state by a database error from turning the
                # caller's commit into that same false report.
                await db.rollback()
                outstanding = self._kprofile_profile_count(
                    content for path, content in payload.items() if _KPROFILE_PATH_RE.match(path)
                )
                outstanding -= tally.restored + tally.skipped + tally.failed
                tally.failed += max(outstanding, 0)
                tally.note(
                    "kprofilesStepFailed",
                    f"The K-profile step could not be completed: {e}",
                    reason=str(e)[:200],
                )
            results[RestoreCategory.KPROFILES.value] = tally

        return results

    # --- Per-category appliers --------------------------------------------

    @staticmethod
    async def _begin_record_category(db: AsyncSession) -> None:
        # Only this category owner ends a caller's earlier read transaction.
        # Never commit unrelated pending writes merely to acquire SQLite's writer.
        if db.in_transaction():
            if db.new or db.dirty or db.deleted:
                raise RuntimeError("Metadata restore requires a clean category transaction")
            await db.commit()
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(sql_text("BEGIN IMMEDIATE"))

    async def _restore_archives(
        self,
        db: AsyncSession,
        payload,
        overwrite: bool,
        tally: _CategoryTally,
        id_map: dict[int, int],
        *,
        identity_map: dict[int, ArchiveProof] | None = None,
    ) -> None:
        # Direct legacy callers use None for no file. The category owner checks
        # present JSON null strictly, before calling any writer.
        archives = archive_rows(MISSING if payload is None else payload)
        if archives is None:
            tally.note("noData", "No data of this kind in this backup")
            return
        old_ids = Counter(e["id"] for e in archives if isinstance(e, dict) and source_id(e.get("id")) is not INVALID)
        uuids = Counter(
            v
            for e in archives
            if isinstance(e, dict) and isinstance(v := uuid_value(e.get("recovery_identity", MISSING)), str)
        )
        identity_skipped = legacy_artifacts = invalid_fields = energy_kept = energy_unknown = 0
        failed_before, skipped_before = tally.failed, tally.skipped
        records = []
        for entry in archives:
            if not isinstance(entry, dict):
                tally.failed += 1
                continue
            old_id = entry.get("id")
            if old_id is not None and (source_id(old_id) is INVALID or old_ids[old_id] > 1):
                tally.failed += 1
                continue
            record = parse_archive(entry, _parse_dt)
            if record.identity is INVALID or (record.identity is not None and uuids[record.identity] > 1):
                tally.skipped += 1
                identity_skipped += 1
                continue
            if (
                record.identity is None
                and record.values["started_at"] is None
                and not isinstance(record.content_hash, str)
                and not record.unknown
            ):
                legacy_artifacts += 1
                identity_skipped += 1
                tally.skipped += 1
                continue
            if record.identity is None and record.unknown:
                identity_skipped += 1
                tally.skipped += 1
                continue
            records.append(record)
        planned, printers, (users_by_name, valid_users) = await prepare_archive_restore(db, records)
        for record in records:
            entry, values = record.entry, record.values
            try:
                existing = await self._find_archive(
                    db, entry, record.started_at, record=record, printers=printers, allowed_ids=planned
                )
            except ArchiveIdentityConflict:
                tally.skipped += 1
                identity_skipped += 1
                continue
            if (
                existing is not None
                and record.identity is not None
                and (
                    record.started_at is not None
                    and record.started_at != existing.started_at
                    or entry.get("printer_id") is not None
                    and printer_for(entry, printers) != existing.printer_id
                )
            ):
                tally.note(
                    "archivesIdentityKept",
                    "Some UUID-matched archives had different start or printer metadata — the local execution identity was kept",
                )
            if record.unknown:
                tally.note(
                    "archivesOutcomeUncertain",
                    "Snapshots without a proven outcome cannot replace local print results; any new execution is marked uncertain",
                )
            old_id = entry.get("id")
            if existing is not None and not overwrite:
                if old_id is not None:
                    id_map[old_id] = existing.id
                    if identity_map is not None:
                        identity_map[old_id] = ArchiveProof(
                            existing.id, uuid_value(existing.recovery_identity), existing.printer_id
                        )
                tally.skipped += 1
                continue
            insert = existing is None
            protected = not insert and operator_only(existing, record.unknown)
            fields = write_fields(values, ARCHIVE_FIELDS, insert=insert, allowed=OPERATOR_FIELDS if protected else None)
            if not insert:
                for key in INSERT_ONLY:
                    fields.pop(key, None)
            if protected and not fields:
                if old_id is not None:
                    id_map[old_id] = existing.id
                    if identity_map is not None:
                        identity_map[old_id] = ArchiveProof(
                            existing.id, uuid_value(existing.recovery_identity), existing.printer_id
                        )
                tally.skipped += 1
                continue
            eligible = OPERATOR_FIELDS if protected else ARCHIVE_FIELDS.keys() - (INSERT_ONLY if not insert else set())
            corrected = {key for key in eligible if values[key] is INVALID}
            if not protected:
                # The energy writer owns these three together, never setattr
                # the numbers while retaining somebody else's provenance.
                fields.pop("energy_kwh", None)
                fields.pop("energy_cost", None)
                corrected.difference_update(("energy_kwh", "energy_cost"))
                owner, owner_note, owner_invalid = owner_value(entry, users_by_name, valid_users, _parse_dt)
                if owner is not MISSING:
                    fields["created_by_id"] = owner
                if insert and owner in (MISSING, None):
                    code = owner_note or "archivesOwnerUnknown"
                    messages = {
                        "archivesOwnerUnknown": "Some archives were restored without an owner — they are visible only to users with archives:read_all until an admin reassigns them",
                        "archivesOwnerCleared": "Some archives referenced an unresolved owner — owner cleared; visible only to users with archives:read_all until an admin reassigns them",
                        "archivesOwnerUnmatched": "Some archives name an owner this instance does not have — owner cleared rather than guessed from the backup user id; visible only to users with archives:read_all until an admin reassigns them",
                    }
                    tally.note(code, messages[code])
                if owner_invalid:
                    corrected.add("created_by_username" if "created_by_username" in entry else "created_by_id")
            if insert:
                fields["file_path"] = ""  # Every JSON value, including null, is ignored.
                fields["printer_id"] = printer_for(entry, printers)
                if entry.get("printer_id") is not None and fields["printer_id"] is None:
                    tally.note(
                        "archivesPrinterUnproven",
                        "Some archive printer references could not be proven by ID and serial — left unlinked",
                    )
                if record.identity is not None:
                    fields["recovery_identity"] = record.identity
                for key, value in (("content_hash", record.content_hash), ("source_content_hash", record.source_hash)):
                    if value is not MISSING and value is not INVALID:
                        fields[key] = value
                    elif value is INVALID:
                        corrected.add(key)
                if entry.get("project_id") is not None or entry.get("project_line_id") is not None:
                    tally.note(
                        "archivesOrderLinksIgnored",
                        "Order links in metadata backups are not portable — restored archives remain outside orders",
                    )
                if record.unknown:
                    fields.update(
                        status="cancelled",
                        failure_reason="noStatusUpdate",
                        completed_at=None,
                        print_time_seconds=None,
                        filament_used_grams=None,
                        cost=None,
                    )
                row = PrintArchive(**fields)
                tally.note(
                    "archivesMetadataOnly",
                    "Restored archives carry metadata only — the 3MF and thumbnail files are not in a Git backup",
                )
            else:
                row = existing
                if existing.deleted_at is not None and fields.get("deleted_at", MISSING) is None:
                    tally.note(
                        "archivesUndeleted", "Archive(s) deleted since the backup are visible again — overwrite was on"
                    )
                for key, value in fields.items():
                    setattr(row, key, value)
            if not protected:
                bundle = energy_bundle(entry, _parse_dt)
                if insert and record.unknown:
                    row.energy_kwh, row.energy_cost = None, None
                    row.extra_data = meter_watch.with_energy_record(
                        row.extra_data, {"energy_provenance": dict(UNKNOWN_ENERGY["energy_provenance"])}
                    )
                    row.extra_data = {**row.extra_data, "recovered_outcome_uncertain": True}
                    energy_unknown += 1
                elif bundle.invalid_fields:
                    corrected.update(bundle.invalid_fields)
                    if insert:
                        row.energy_kwh, row.energy_cost = None, None
                        row.extra_data = meter_watch.with_energy_record(
                            row.extra_data, {"energy_provenance": dict(UNKNOWN_ENERGY["energy_provenance"])}
                        )
                        energy_unknown += 1
                    else:
                        energy_kept += 1
                elif insert or (bundle.complete and not bundle.no_energy):
                    row.energy_kwh, row.energy_cost = bundle.kwh, bundle.cost
                    row.extra_data = meter_watch.with_energy_record(row.extra_data, bundle.record)
                    if bundle.clear:
                        tally.note(
                            "archivesEnergyCleared", "Some archive energy bundles were explicitly cleared by the backup"
                        )
                elif any(key in entry for key in ("energy_kwh", "energy_cost", "energy_record")):
                    energy_kept += 1
                # Wear is a fact of the run: the backup's pair as recorded, for the same row and
                # under the same identity checks as its energy -- never recomputed, never over a
                # local figure, and never for an execution whose outcome the backup does not prove.
                if not (insert and record.unknown):
                    wear_pair = (entry.get("wear_cost"), entry.get("wear_rate"))
                    carried = wear_cost.carry_restored(row, *wear_pair)
                    if insert and not carried and any(value is not None for value in wear_pair):
                        corrected.add("wear_cost")
                write, declared = swap_compatibility.restored_declaration(entry, None if insert else row)
                if insert or write:
                    swap_compatibility.write_archive_fact(row, declared)
            # «Result checked» is an operator's mark: carried for a protected row too, never
            # over a local mark, and a restored print without one comes back as checked —
            # history must not light up as unchecked or be reminded about.
            archive_outcome.carry_restored(row, _parse_dt(entry.get("outcome_checked_at")), insert=insert)
            if insert:
                db.add(row)
            await db.flush()
            planned.add(row.id)  # Rows inserted by this category are already owned.
            if not protected:
                await self._restore_source_declaration(db, entry, row, tally)
                await db.flush()
            if old_id is not None:
                id_map[old_id] = row.id
                if identity_map is not None:
                    identity_map[old_id] = ArchiveProof(row.id, uuid_value(row.recovery_identity), row.printer_id)
            tally.restored += 1
            invalid_fields += len(corrected)
        if invalid_fields or identity_skipped or tally.failed > failed_before or energy_kept or energy_unknown:
            previous, tally.notes = tally.notes, []
            tally.note(
                "archivesInputSummary",
                "Archive restore: invalid fields kept or defaulted: "
                f"{invalid_fields}; failed rows: {tally.failed - failed_before}; skipped rows: {tally.skipped - skipped_before}; "
                f"identity conflicts: {identity_skipped}; legacy source-only rows skipped: {legacy_artifacts}; "
                f"energy bundles kept: {energy_kept}; unknown energy: {energy_unknown}.",
                invalid_fields=invalid_fields,
                failed_rows=tally.failed - failed_before,
                skipped_rows=tally.skipped - skipped_before,
                identity_skipped=identity_skipped,
                legacy_source_artifacts_skipped=legacy_artifacts,
                energy_kept=energy_kept,
                energy_unknown=energy_unknown,
            )
            tally.notes.extend(previous[:19])

    async def _restore_source_declaration(self, db: AsyncSession, entry: dict, archive, tally) -> None:
        """The source's declaration for a future reprint (swap-source-declaration spec r3 §9): a restored record
        with its audit line, or a tombstone the reader answers unknown; nothing at all when the backup has no field.
        """
        from backend.app.services.swap_source_declarations import restored_audit

        write, record = swap_compatibility.restored_source_declaration(entry, archive)
        if not write:
            return
        swap_compatibility.restore_source_declaration(archive, record)
        if record == swap_compatibility.INVALID_DECLARATION:
            tally.note(
                "archivesSwapDeclarationUnreadable",
                "Some archives' swap declarations in the backup could not be read — those sources are unknown "
                "until declared again",
            )
            return
        await db.flush()
        db.add(restored_audit("archive", archive.id, record))

    async def _find_archive(
        self,
        db: AsyncSession,
        entry: dict,
        started_at: datetime | None,
        *,
        record=None,
        printers=None,
        allowed_ids=None,
    ) -> PrintArchive | None:
        """Identity lookup; the category owner plans, locks and rereads before writes."""
        record = record or parse_archive(entry, _parse_dt)
        record.values["started_at"] = started_at
        rows = await archive_candidates(db, record)
        if allowed_ids is not None and any(row.id not in allowed_ids for row in rows):
            raise RuntimeError("Archive identity changed during restore planning")
        if printers is None:
            printers = await lock_restore_printers(db, [entry.get("printer_id"), *(row.printer_id for row in rows)])
        return resolve_archive_identity(record, rows, printers)

    async def _restore_spools(
        self,
        db: AsyncSession,
        inventory,
        usage_payload,
        overwrite: bool,
        tally: _CategoryTally,
        archive_id_map: dict[int, int],
        *,
        archive_proofs=None,
    ) -> None:
        spools, usage = spool_rows(
            MISSING if inventory is None else inventory, MISSING if usage_payload is None else usage_payload
        )
        if spools is None:
            tally.note("noData", "No data of this kind in this backup")
            return

        printers, accepted_archive_proofs = await usage_basis(db, usage, archive_id_map, archive_proofs)

        spool_id_map: dict[int, int] = {}
        claimed_by: dict[int, tuple | None] = {}
        tags_kept = 0
        invalid_fields = reactivated = baseline_above_used = identity_unavailable = 0
        failed_before, skipped_before = tally.failed, tally.skipped
        source_ids = Counter(
            entry["id"] for entry in spools if isinstance(entry, dict) and source_id(entry.get("id")) is not INVALID
        )
        tag_counts = {column: Counter() for column in ("tag_uid", "tray_uuid")}
        for entry in spools:
            if not isinstance(entry, dict):
                continue
            for column, counts in tag_counts.items():
                tag = read_field(entry, column, SPOOL_FIELDS[column], _parse_dt)
                if isinstance(tag, str) and tag:
                    counts[tag] += 1

        def carries_tag(entry):
            return isinstance(entry, dict) and any(
                isinstance(tag := read_field(entry, column, SPOOL_FIELDS[column], _parse_dt), str) and tag
                for column in ("tag_uid", "tray_uuid")
            )

        # Claim physical tags before an untagged composite can consume their row.
        for entry in sorted(spools, key=carries_tag, reverse=True):
            if not isinstance(entry, dict):
                tally.failed += 1
                continue

            old_id = entry.get("id")
            if old_id is not None and (source_id(old_id) is INVALID or source_ids[old_id] > 1):
                tally.failed += 1
                continue

            parsed = {key: read_field(entry, key, rule, _parse_dt) for key, rule in SPOOL_FIELDS.items()}
            if any(
                isinstance(parsed[column], str) and parsed[column] and counts[parsed[column]] > 1
                for column, counts in tag_counts.items()
            ):
                identity_unavailable += 1
                tally.skipped += 1
                continue
            tagged = any(isinstance(parsed[key], str) and parsed[key] for key in ("tag_uid", "tray_uuid"))
            composite_valid = isinstance(parsed["created_at"], datetime) and all(
                parsed[key] is not INVALID for key in ("material", "brand", "subtype", "color_name")
            )
            if not tagged and not composite_valid:
                identity_unavailable += 1
                tally.skipped += 1
                continue
            try:
                existing, matched_on = await self._find_spool(db, entry, claimed=claimed_by)
            except MultipleResultsFound:
                identity_unavailable += 1
                tally.skipped += 1
                continue
            if existing is not None and existing.id in claimed_by:
                identity_unavailable += 1
                tally.skipped += 1
                continue

            fields = {}
            for key, value in parsed.items():
                if key == "created_at":
                    continue  # Natural-key input and insert-only, never overwrite.
                if value is MISSING or value is INVALID:
                    if existing is None:
                        fields[key] = SPOOL_FIELDS[key].default
                else:
                    fields[key] = value

            # Preserve the newer inventory bookkeeping alongside the reviewed
            # S1 identity and presence rules. Structured location links stay local.
            for key in _SPOOL_LATE_FIELDS:
                if key != "weight_used_baseline" and key in entry:
                    fields[key] = entry[key]
            for key in _SPOOL_LATE_DATES:
                if key in entry:
                    fields[key] = _parse_dt(entry[key])
            if fields.get("filament_diameter") not in ("1.75", "2.85"):
                fields.pop("filament_diameter", None)

            if existing is not None:
                if not overwrite:
                    claimed_by[existing.id] = _spool_composite_key(parsed)
                    if old_id is not None:
                        spool_id_map[old_id] = existing.id
                    tally.skipped += 1
                    continue
                tags_kept += await self._guard_tag_overwrite(db, existing, fields, matched_on)
                if existing.location_id is not None:
                    # Filed under a structured location here: locations are not
                    # backed up, so the backup's text must not contradict the link.
                    fields.pop("storage_location", None)
                made_active = (
                    existing.archived_at is not None and "archived_at" in fields and fields["archived_at"] is None
                )
                for key, value in fields.items():
                    setattr(existing, key, value)
                row = existing
            else:
                made_active = False
                row = Spool(**fields)
                if isinstance(parsed["created_at"], datetime):
                    row.created_at = parsed["created_at"]
                db.add(row)
            await db.flush()
            claimed_by[row.id] = _spool_composite_key(parsed)
            if old_id is not None:
                spool_id_map[old_id] = row.id
            tally.restored += 1
            invalid_fields += sum(value is INVALID for value in parsed.values())
            reactivated += made_active
            baseline = read_field(
                {"weight_used_baseline": row.weight_used_baseline},
                "weight_used_baseline",
                NUMERIC_FIELDS["spools", "weight_used_baseline"],
                _parse_dt,
            )
            used = read_field(
                {"weight_used": row.weight_used},
                "weight_used",
                NUMERIC_FIELDS["spools", "weight_used"],
                _parse_dt,
            )
            if baseline is not INVALID and used is not INVALID and baseline > used:
                baseline_above_used += 1

        usage_invalid, usage_anomaly = await self._restore_spool_usage(
            db,
            {"history": usage},
            tally,
            spool_id_map,
            archive_id_map,
            archive_proofs=accepted_archive_proofs,
            printers=printers,
        )
        invalid_fields += usage_invalid
        if (
            invalid_fields
            or tally.failed > failed_before
            or identity_unavailable
            or reactivated
            or baseline_above_used
            or usage_anomaly
        ):
            # Reserve a slot for exact aggregate counts even with a pre-filled tally.
            previous_notes, tally.notes = tally.notes, []
            tally.note(
                "spoolInputSummary",
                "Spool restore: invalid fields kept or defaulted: "
                f"{invalid_fields}; failed rows: {tally.failed - failed_before}; "
                f"skipped rows: {tally.skipped - skipped_before}; reactivated spools: {reactivated}; "
                f"reset points above used weight retained: {baseline_above_used}.",
                invalid_fields=invalid_fields,
                failed_rows=tally.failed - failed_before,
                skipped_rows=tally.skipped - skipped_before,
                reactivated=reactivated,
                baseline_above_used=baseline_above_used,
            )
            tally.notes.extend(previous_notes[:19])
        if identity_unavailable:
            tally.note(
                "spoolIdentityUnavailable",
                f"{identity_unavailable} spool row(s) skipped: their identity was missing or ambiguous.",
                count=identity_unavailable,
            )

        if tags_kept:
            tally.note(
                "spoolTagKept",
                f"{tags_kept} spool tag(s) left as they are — the backup would have cleared a tag that "
                "has since been scanned, or moved one onto a second spool.",
                count=tags_kept,
            )

    async def _find_spool(
        self, db: AsyncSession, entry: dict, *, claimed: dict[int, tuple | None] | None = None
    ) -> tuple[Spool | None, str | None]:
        """Match a backed-up spool to a local row, and say which key matched.

        Physical identity first (an RFID/Bambu tag is the spool), then a
        descriptive composite including ``created_at`` so two otherwise
        identical spools added at different times stay distinct.

        Claimed rows stay visible to tag lookups so a second tag alias is
        refused. Composite lookup excludes only claims by a row with the same
        valid composite: distinct bulk siblings may share every field, but a
        recycled tag can claim a differently described spool that still exists.

        The second element names the column that matched — ``"tag_uid"``,
        ``"tray_uuid"`` or ``None`` for the composite. ``_guard_tag_overwrite``
        needs it: the matched column holds the incoming value by definition, so
        it is the *other* one that overwrite can corrupt.
        """
        parsed = {key: read_field(entry, key, rule, _parse_dt) for key, rule in SPOOL_FIELDS.items()}
        for column in ("tag_uid", "tray_uuid"):
            value = parsed[column]
            if isinstance(value, str) and value:
                result = await db.execute(
                    select(Spool)
                    .where(getattr(Spool, column) == value)
                    .order_by(Spool.id)
                    .with_for_update(read=True, key_share=True)
                )
                row = result.scalars().one_or_none()
                if row is not None:
                    return row, column

        created_at = parsed["created_at"]
        if not isinstance(created_at, datetime) or any(
            parsed[key] is INVALID for key in ("material", "brand", "subtype", "color_name")
        ):
            return None, None
        # created_at is filtered in Python, not here — see _created_at_matches.
        result = await db.execute(
            select(Spool)
            .where(
                *(
                    getattr(Spool, key) == (SPOOL_FIELDS[key].default if parsed[key] is MISSING else parsed[key])
                    for key in ("material", "brand", "subtype", "color_name")
                ),
            )
            .order_by(Spool.id)
            .with_for_update(read=True, key_share=True)
        )
        key = _spool_composite_key(parsed)
        sibling_claims = {local for local, owner in (claimed or {}).items() if owner is not None and owner == key}
        candidates = [
            row for row in result.scalars() if row.id not in sibling_claims and _created_at_matches(row, created_at)
        ]
        if len(candidates) > 1:
            raise MultipleResultsFound("Ambiguous spool composite")
        return (candidates[0], None) if candidates else (None, None)

    @staticmethod
    async def _guard_tag_overwrite(db: AsyncSession, existing: Spool, fields: dict, matched_on: str | None) -> int:
        """Remove tag columns from ``fields`` that an overwrite would corrupt.

        ``tag_uid`` and ``tray_uuid`` are both in ``fields`` and overwrite is a
        blanket ``setattr`` loop, so a spool matched on one key gets the backup's
        *other* key written onto it. Neither column has a unique constraint
        (``models/spool.py``, and no unique index in the migrations), so nothing
        errors — a duplicate tag simply appears, after which ``_find_spool``'s
        ``.first()`` is non-deterministic and an AMS tag lookup resolves to an
        arbitrary one of the two spools. The same loop can also *clear* a tag the
        user has scanned since the backup was taken, when the backup entry holds
        ``None``.

        Two refusals, and the row is otherwise overwritten as normal:

        * the incoming value is empty and the local row has one — the backup
          predates the scan, so the local tag is the newer fact;
        * the incoming value is already held by a different local spool — writing
          it would create the duplicate described above.

        Returns how many columns were left alone, so the caller can say so in the
        tally rather than doing it silently.
        """
        kept = 0
        for column in ("tag_uid", "tray_uuid"):
            if column not in fields:
                continue
            # The column we matched on already holds the incoming value.
            if column == matched_on:
                continue

            incoming = fields.get(column)
            current = getattr(existing, column)
            if incoming == current:
                continue

            if not incoming:
                if current:
                    fields.pop(column)
                    kept += 1
                continue

            clash = await db.execute(
                select(Spool.id).where(getattr(Spool, column) == incoming, Spool.id != existing.id)
            )
            if clash.scalars().first() is not None:
                fields.pop(column)
                kept += 1
        return kept

    async def _restore_spool_usage(
        self,
        db: AsyncSession,
        usage_payload,
        tally: _CategoryTally,
        spool_id_map: dict[int, int],
        archive_id_map: dict[int, int],
        *,
        archive_proofs=None,
        printers=None,
    ) -> tuple[int, bool]:
        if usage_payload is None or usage_payload is MISSING:
            return 0, False
        _, usage = spool_rows({"spools": []}, usage_payload)
        if not usage:
            return 0, False
        if printers is None:
            printers, archive_proofs = await usage_basis(db, usage, archive_id_map, archive_proofs)
        invalid_fields = unresolved = unlinked = no_time = unproven_printer = unknown_status = 0
        failed_before = tally.failed
        for entry in usage:
            if not isinstance(entry, dict):
                tally.failed += 1
                continue
            old_spool = source_id(entry.get("spool_id"))
            spool_id = spool_id_map.get(old_spool) if old_spool is not INVALID else None
            if spool_id is None:
                unresolved += 1
                tally.skipped += 1
                continue
            values = parsed_fields(entry, USAGE_FIELDS, _parse_dt)
            created = values["created_at"]
            if not isinstance(created, datetime):
                no_time += 1
                tally.skipped += 1
                continue
            fields = write_fields(values, USAGE_FIELDS, insert=True)
            existing = await db.scalars(
                select(SpoolUsageHistory).where(
                    SpoolUsageHistory.spool_id == spool_id,
                    SpoolUsageHistory.weight_used == fields["weight_used"],
                    SpoolUsageHistory.print_name == fields["print_name"],
                )
            )
            if any(_created_at_matches(row, created) for row in existing):
                tally.skipped += 1
                continue  # Existing links, including NULL, are never retrofitted.
            old_archive = source_id(entry.get("archive_id"))
            proof = (archive_proofs or {}).get(old_archive) if old_archive is not INVALID else None
            archive_id = (
                proof.local_id if proof is not None and archive_id_map.get(old_archive) == proof.local_id else None
            )
            if archive_id is None and entry.get("archive_id") is not None:
                unlinked += 1
            printer = printer_for(entry, printers)
            if (
                printer is not None
                and proof is not None
                and proof.printer_id is not None
                and printer != proof.printer_id
            ):
                printer = None
            if printer is None and entry.get("printer_id") is not None:
                unproven_printer += 1
            db.add(SpoolUsageHistory(spool_id=spool_id, archive_id=archive_id, printer_id=printer, **fields))
            await db.flush()
            tally.restored += 1
            invalid_fields += sum(value is INVALID for value in values.values())
            unknown_status += int(values["status"] is MISSING or values["status"] is INVALID)
        if unresolved:
            tally.note(
                "spoolUsageUnresolved",
                f"{unresolved} usage record(s) skipped — no accepted spool identity map was available.",
                count=unresolved,
            )
        if unlinked:
            tally.note(
                "spoolUsageUnlinked",
                f"{unlinked} usage record(s) restored without a proven print-history link — select both categories and retain the same execution identity.",
                count=unlinked,
            )
        if no_time:
            tally.note(
                "spoolUsageTimeUnknown",
                f"{no_time} usage record(s) skipped — their event time was missing or invalid.",
                count=no_time,
            )
        if unproven_printer:
            tally.note(
                "spoolUsagePrinterUnproven",
                f"{unproven_printer} usage printer reference(s) were not proven by ID and serial — left unlinked.",
                count=unproven_printer,
            )
        if unknown_status:
            tally.note(
                "spoolUsageOutcomeUnknown",
                f"{unknown_status} usage record(s) have an unknown outcome — no successful print was inferred.",
                count=unknown_status,
            )
        return invalid_fields, bool(
            unresolved
            or unlinked
            or no_time
            or unproven_printer
            or unknown_status
            or tally.failed > failed_before
            or invalid_fields
        )

    @staticmethod
    def _spoolman_usage_entries(payload) -> list:
        usage = payload.get("usage_history") if isinstance(payload, dict) else None
        return usage if isinstance(usage, list) else []

    async def _restore_spoolman_usage(
        self,
        db: AsyncSession,
        payload,
        tally: _CategoryTally,
        archive_id_map: dict[int, int],
        *,
        archive_proofs=None,
    ) -> None:
        """The Spoolman usage history, through its one writer.

        Restored whatever the inventory mode — as the built-in file beside it
        is — and whether or not the backup carried built-in spools: the rows
        name Spoolman's spools. The writer binds them to THIS installation's
        current Spoolman, recognises the ones already here and counts what it
        could not read; this only resolves the two references a row may carry
        (a printer by its serial, an archive through the archives this restore
        brought) and reports.
        """
        entries = self._spoolman_usage_entries(payload)
        if not entries:
            return

        from backend.app.services import spoolman_usage_history

        # The preceding archive category committed. A reused numeric id cannot
        # become this history's print: share S2's UUID proof and lock order.
        _, accepted_proofs = await usage_basis(db, entries, archive_id_map, archive_proofs)
        accepted_archive_ids = {old: proof.local_id for old, proof in accepted_proofs.items()}
        printers = (await db.execute(select(Printer.serial_number, Printer.id))).all()
        # Staged in this category's transaction under the source guard the category holds:
        # one commit for the token and the rows, or one rollback of both (C047 S29, S30).
        token = await spoolman_usage_history.persist_source(db)
        outcome = await spoolman_usage_history.restore_rows(
            db,
            entries,
            archive_ids=accepted_archive_ids,
            printer_ids={serial: printer_id for serial, printer_id in printers if serial},
            source_token=token,
        )
        tally.restored += outcome.restored
        tally.failed += outcome.malformed
        tally.skipped += outcome.present + outcome.other_source + outcome.no_source
        if outcome.no_source:
            tally.note(
                "spoolmanUsageNoSource",
                f"{outcome.no_source} Spoolman usage record(s) skipped — no Spoolman address is set on this "
                "installation, so there is no Spoolman to attach them to.",
                count=outcome.no_source,
            )
        if outcome.other_source:
            tally.note(
                "spoolmanUsageOtherSource",
                f"{outcome.other_source} Spoolman usage record(s) skipped — they belong to a Spoolman the backed-up "
                "installation had already stopped using.",
                count=outcome.other_source,
            )
        if outcome.unlinked:
            tally.note(
                "spoolUsageUnlinked",
                f"{outcome.unlinked} usage record(s) restored without a proven print-history link — "
                "select both categories and retain the same execution identity.",
                count=outcome.unlinked,
            )

    @staticmethod
    async def _restored_mode(db: AsyncSession, payload, overwrite: bool) -> str | None:
        """The Spoolman mode the settings category will write, read before its anchor (C047 A12).

        The value and the condition ``_restore_settings`` hands to ``decide``: the backup's
        canonical ``spoolman_enabled``, written when overwriting or when the row is missing.
        None — the category leaves the mode alone, or its plan refuses the value.
        """
        values = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(values, dict) or "spoolman_enabled" not in values:
            return None
        try:
            mode = _restore_setting_value("spoolman_enabled", values["spoolman_enabled"])
        except (ValidationError, ValueError, TypeError, KeyError):
            return None
        if not overwrite:
            existing = await db.scalar(select(Settings.id).where(Settings.key == "spoolman_enabled"))
            if existing is not None:
                return None
        return mode

    async def _restore_settings(
        self,
        db: AsyncSession,
        payload,
        overwrite: bool,
        tally: _CategoryTally,
        keys_written: set[str] | None = None,
        source_change=None,
    ) -> None:
        values = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(values, dict):
            tally.note("noData", "No data of this kind in this backup")
            return

        # Planned before the first write, so the companion rule reads genuinely
        # pre-restore local state, and so the preview and this run classify the
        # payload identically.
        from backend.app.core.credential_config import lock_credential_config
        from backend.app.services import energy_price

        await lock_credential_config(db)
        plan = await self._plan_settings(db, values, overwrite=overwrite)

        if source_change is not None and "spoolman_enabled" in plan.accepted and "spoolman_enabled" not in plan.no_op:
            existing_mode = (
                await db.execute(select(Settings.id).where(Settings.key == "spoolman_enabled"))
            ).scalar_one_or_none()
            if overwrite or existing_mode is None:
                # Git never moves the address (a frozen destination); the mode is a first-tier change.
                # ``_restored_mode`` mirrors this condition for the wait before the anchor (A12).
                await source_change.decide(db, enabled=plan.accepted["spoolman_enabled"])

        for key, value in sorted(plan.accepted.items()):
            if key in plan.no_op:
                tally.skipped += 1
                continue
            result = await db.execute(
                select(Settings).where(Settings.key == key).execution_options(populate_existing=True)
            )
            existing = result.scalar_one_or_none()
            if existing is not None and not overwrite:
                tally.skipped += 1
                continue
            if key == energy_price.SETTING_KEY:
                # The one setting with a history (m245): the change is recorded before the row moves.
                await energy_price.note_setting_write(db, value, "restore")
            if existing is not None:
                existing.value = value
            else:
                db.add(Settings(key=key, value=value))
            tally.restored += 1
            if keys_written is not None:
                keys_written.add(key)

        if plan.blocked:
            tally.note(
                "settingsCredentialsSkipped",
                f"{len(plan.blocked)} credential-like key(s) skipped — re-enter secrets manually",
                count=len(plan.blocked),
            )
        if plan.protected:
            tally.note(
                "settingsAuthSkipped",
                f"{len(plan.protected)} authentication/integration setting(s) skipped — use the owning Settings "
                "editor so lockout and credential checks still run",
                count=len(plan.protected),
            )
        if plan.companion:
            keys = ", ".join(sorted(plan.companion))
            tally.note(
                "settingsCompanionSkipped",
                f"{keys} not restored — retained local credentials do not satisfy current Settings checks; "
                "review the owning Settings editor",
                keys=keys,
                count=len(plan.companion),
            )

        for name, label in (
            ("unknown", "unknown setting(s) skipped — use the current Settings editor"),
            ("invalid", "malformed setting(s) skipped — current validation failed"),
            ("runtime", "runtime or per-user setting(s) skipped"),
            (
                "destination",
                "local destination setting(s) skipped — retained local connections and paths are preserved",
            ),
            ("masked", "masked setting value(s) skipped — masks cannot restore credentials"),
        ):
            count = len(getattr(plan, name))
            if count:
                tally.note(f"settings{name.title()}Skipped", f"{count} {label}", count=count)

    async def _reconfigure_mqtt_relay(self, db: AsyncSession, keys_written: set[str], tally: _CategoryTally) -> None:
        """Push restored mqtt_* settings into the live relay.

        The relay reads its broker config once, at configure() time — the
        settings PUT handler reconfigures it for exactly this reason
        (api/routes/settings.py). Writing the rows alone left the relay on the
        pre-restore broker until the next backend restart while the UI showed
        the restored values, which is the one way a restore could look applied
        and not be.

        Called after the commit, never before: configure() tears the connection
        down and rebuilds it, so it must not run against values a later failure
        could roll back. Only mqtt_password can't come back this way (the
        credential blocklist skips it) — the row already in the database is
        reused, so an unchanged broker keeps working.
        """
        if not _MQTT_SETTING_KEYS & keys_written:
            return

        try:
            from backend.app.api.routes.settings import _apply_mqtt_runtime

            await _apply_mqtt_runtime(db)
        except Exception:
            # Same call is best-effort in the settings PUT handler: the rows are
            # committed either way, and a broker that refuses the new config
            # must not turn a successful restore into a failed one. Noted rather
            # than swallowed silently, so the user knows to restart.
            # Sender exceptions can contain a retained broker URI or password.
            logger.warning("Could not reconfigure the MQTT relay after a settings restore")
            tally.note(
                "settingsMqttRelayFailed",
                "MQTT settings restored, but the relay could not be reconnected — restart BamDude",
            )

    async def _restore_kprofiles(
        self, db: AsyncSession, payload: dict, tally: _CategoryTally, *, authority=None, required_permissions=()
    ) -> None:
        by_serial: dict[str, list[tuple[str, dict]]] = {}
        for path, content in payload.items():
            match = _KPROFILE_PATH_RE.match(path)
            if not match or not isinstance(content, dict):
                continue
            by_serial.setdefault(match.group(1), []).append((match.group(2), content))

        if not by_serial:
            tally.note("noData", "No data of this kind in this backup")
            return

        result = await db.execute(select(Printer))
        printers = {p.serial_number: p for p in result.scalars().all() if p.serial_number}
        if authority is not None:
            printers = {serial: SimpleNamespace(id=p.id, name=p.name) for serial, p in printers.items()}
            await db.rollback()

        # Overwrite is not offered for K-profiles: extrusion_cali_set replaces
        # the profile occupying a slot, so writing is always an overwrite on the
        # printer side.
        tally.note("kprofilesAlwaysOverwrite", "K-profiles always overwrite the matching slot on the printer")
        # A refusal is now believed and counted failed (#2718 made the ack worth
        # reading), but silence still counts restored, so the caveat stands —
        # narrowed to what is actually left uncertain.
        tally.note(
            "kprofilesAckUnreliable",
            "A printer that does not answer still counts as restored — verify the profiles on the printer",
        )

        for serial, entries in sorted(by_serial.items()):
            profile_total = self._kprofile_profile_count(c for _, c in entries)

            printer = printers.get(serial)
            if printer is None:
                tally.skipped += profile_total
                tally.note("kprofilesPrinterMissing", f"No printer with serial {serial} — skipped", serial=serial)
                continue

            client = printer_manager.get_client(printer.id)
            if not client or not client.state.connected:
                tally.skipped += profile_total
                tally.note(
                    "kprofilesPrinterOffline",
                    f"{printer.name} ({serial}) is not connected — skipped",
                    printer=printer.name,
                    serial=serial,
                )
                continue

            for nozzle, content in sorted(entries):
                profiles = content.get("profiles")
                if not isinstance(profiles, list) or not profiles:
                    continue
                if nozzle not in _KNOWN_NOZZLES:
                    tally.note(
                        "kprofilesUnknownNozzle",
                        f"Unexpected nozzle diameter {nozzle} for {serial} — sent as-is",
                        nozzle=nozzle,
                        serial=serial,
                    )

                # The backup's slot_id is a cali_idx, and cali_idx is as
                # unstable as the autoincrement ids we already refuse to reuse
                # for spools and archives: editing a profile in BamDude is a
                # delete-then-add on a single-nozzle printer, which re-keys it.
                # Addressing extrusion_cali_set at a slot that no longer exists
                # is a silent no-op — the printer drops it and we would still
                # report the profile restored. So resolve the live index first.
                current = await self._current_kprofile_index(client, nozzle, serial)
                if current is None:
                    # Not read is not "holds nothing" (upstream 080d866fb). With
                    # no live table nothing can be matched, and sending the
                    # nozzle anyway adds every profile as a NEW one beside its
                    # original — duplicates that were tallied restored. The
                    # nozzle is left as it is and its profiles are counted
                    # failed; the other nozzles and printers go on.
                    tally.failed += len(profiles)
                    tally.note(
                        "kprofilesUnread",
                        f"{printer.name} ({serial}) did not answer for nozzle {nozzle} — its profiles were not restored",
                        nozzle=nozzle,
                        printer=printer.name,
                        serial=serial,
                    )
                    continue

                profile_dicts = []
                unmatched = 0
                # A live profile can only stand in for one backed-up entry. Two
                # entries resolving to the same cali_idx both go into the batch,
                # the second overwrites the first on the printer, and the tally
                # counts two restored where one landed.
                claimed: set[int] = set()
                for p in profiles:
                    if not isinstance(p, dict):
                        # Counted, not dropped. _kprofile_profile_count includes
                        # it, so the offline and printer-missing paths already
                        # count the same entry skipped and the failure path
                        # counts it outstanding — leaving the tally here was the
                        # one place a profile could vanish from
                        # restored + skipped + failed entirely.
                        #
                        # failed here against skipped there is not a
                        # disagreement about the entry. The three counters say
                        # what happened to an item on this run, not whether it
                        # was ever usable: an offline printer skips everything it
                        # holds, well-formed or not, because nothing was
                        # attempted, while here the entry was reached and could
                        # not be used.
                        tally.failed += 1
                        continue
                    match = self._match_kprofile(p, current, claimed)
                    if match is None:
                        unmatched += 1
                    else:
                        claimed.add(match.slot_id)
                    entry = {
                        "filament_id": p.get("filament_id", ""),
                        "name": p.get("name", ""),
                        "k_value": p.get("k_value", "0.020000"),
                        "extruder_id": p.get("extruder_id", 0),
                        # Prefer the live setting_id when we matched: it is
                        # what the printer currently associates with the slot.
                        "setting_id": (match.setting_id if match else None) or p.get("setting_id"),
                        # cali_idx -1 tells the printer to add a new profile
                        # rather than address a slot that isn't there.
                        "cali_idx": match.slot_id if match else -1,
                        # Only consulted for the generated-setting_id
                        # fallback; cali_idx above takes precedence.
                        "slot_id": 0,
                    }

                    # Same precedence as setting_id, and set only when known.
                    # nozzle_id encodes the fitted nozzle's type and diameter
                    # ("HS00-0.4"), so the live value beats the backup's: the
                    # user may have swapped the nozzle since. When neither knows,
                    # the key has to be *absent* — set_kprofiles_batch supplies
                    # HS00-{diameter} via p.get(..., default), which a key
                    # present-and-None defeats, publishing a null nozzle_id.
                    # Printers that omit it are the reason the default is there
                    # (#1748), so it has to be reachable.
                    nozzle_id = (getattr(match, "nozzle_id", None) if match else None) or p.get("nozzle_id")
                    if nozzle_id:
                        entry["nozzle_id"] = nozzle_id
                    profile_dicts.append(entry)
                if not profile_dicts:
                    continue
                if unmatched:
                    tally.note(
                        "kprofilesUnmatched",
                        f"{unmatched} profile(s) for {nozzle} had no counterpart on {printer.name} "
                        "— added as new profiles",
                        count=unmatched,
                        nozzle=nozzle,
                        printer=printer.name,
                    )

                await self._guard_restore_actor(db, authority, required_permissions)
                if authority is not None:
                    # This final read admits the send under the ordinary
                    # permission linearization contract. Release SQL before
                    # MQTT I/O; a revoke overlapping an admitted send may
                    # finish, while every later delayed batch rechecks.
                    await db.rollback()
                try:
                    seq = client.set_kprofiles_batch(profile_dicts, nozzle)
                except Exception as e:
                    logger.warning("K-profile restore failed for %s nozzle %s: %s", serial, nozzle, e)
                    seq = None

                if not seq:
                    tally.failed += len(profile_dicts)
                    tally.note(
                        "kprofilesSendFailed",
                        f"Failed to send {nozzle} profiles to {printer.name} ({serial})",
                        nozzle=nozzle,
                        printer=printer.name,
                        serial=serial,
                    )
                    continue

                # What came back is the sequence_id the command was published
                # under, not a verdict (#2718) — a truthy string only means the
                # command left the building. The printer answers separately, and
                # every other caller of this API now reads that answer; without
                # this the restore would be the one path left that reports a
                # refused write as saved.
                ok, detail = await self._kprofile_ack(client, seq, serial, nozzle)
                if ok:
                    tally.restored += len(profile_dicts)
                else:
                    tally.failed += len(profile_dicts)
                    tally.note(
                        "kprofilesRefused",
                        f"{printer.name} ({serial}) refused the {nozzle} profiles: {detail}",
                        nozzle=nozzle,
                        printer=printer.name,
                        serial=serial,
                        reason=detail,
                    )

    @staticmethod
    def _kprofile_profile_count(contents) -> int:
        """Count the profiles across parsed K-profile files.

        Defensive on purpose. A hand-edited or truncated backup can carry a
        ``profiles`` value that is not a list, and this count runs *before* the
        per-call guards in the loop below — after ``_apply`` has already
        committed the database categories. A malformed file has to be a skipped
        category, not an exception thrown over committed rows.
        """
        total = 0
        for content in contents:
            profiles = content.get("profiles") if isinstance(content, dict) else None
            if isinstance(profiles, list):
                total += len(profiles)
        return total

    @staticmethod
    async def _kprofile_ack(client, seq: str, serial: str, nozzle: str) -> tuple[bool, str]:
        """Read the printer's verdict on one batch write.

        ``await_cali_ack`` already treats silence as success — no answer is not
        evidence of refusal, and firmware that predates the ack never answers at
        all. An exception reading it is the same situation one layer up, so it
        degrades the same way rather than turning a write that most likely
        landed into a reported failure.
        """
        try:
            ok, detail = await client.await_cali_ack(seq)
            return bool(ok), str(detail or "")
        except Exception as e:
            logger.warning("Could not read the K-profile ack for %s nozzle %s: %s", serial, nozzle, e)
            return True, ""

    @staticmethod
    async def _current_kprofile_index(client, nozzle: str, serial: str) -> list | None:
        """Read the printer's live profiles for one nozzle.

        ``None`` when the table could not be read — the caller skips that
        nozzle. An answered empty table is ``[]``: there every profile is
        rightly an add. A read failure never aborts the restore.
        """
        try:
            return list(await client.get_kprofiles(nozzle_diameter=nozzle) or [])
        except Exception as e:
            logger.warning("Could not read live K-profiles for %s nozzle %s: %s", serial, nozzle, e)
            return None

    @staticmethod
    def _match_kprofile(entry: dict, current: list, claimed: set[int]):
        """Find the live profile a backed-up entry corresponds to.

        ``setting_id`` is the filament preset the profile was calibrated for and
        is the strongest signal; a delete-then-add edit regenerates it, so fall
        back to the display name, which BamDude's own editor preserves.
        Both are scoped by ``filament_id`` — the same preset on a different
        filament is a different profile — and by ``extruder_id``, because on a
        dual-nozzle printer the same preset on the other extruder is a different
        profile too.

        ``claimed`` holds the slot ids already taken by earlier entries in this
        nozzle's loop, and no live profile may be claimed twice. Without it, two
        backed-up entries sharing a ``filament_id`` and matching on neither
        ``setting_id`` nor ``name`` both fell through to the single-candidate
        arm and both took the same slot — reachable whenever the user has since
        deleted one of a pair, because the delete-then-add re-key is what strips
        the ``setting_id`` match. Returning None for the displaced entry means
        ``cali_idx: -1``, i.e. add-as-new, which is the safe outcome.
        """
        filament_id = entry.get("filament_id")
        if not filament_id:
            return None

        candidates = [c for c in current if c.filament_id == filament_id]

        # The live index is read per nozzle *diameter*, so on an H2D both
        # extruders' profiles come back together. With the same filament
        # calibrated on both — the ordinary case on a dual-nozzle printer, not an
        # exotic one — filament_id alone lets extruder 0's backed-up entry match
        # extruder 1's live profile, and the batch then carries
        # {extruder_id: 0, cali_idx: <extruder-1 slot>}: one extruder's
        # calibration written over the other's, counted restored.
        #
        # Conditional on both sides saying which extruder they mean. A pre-the restore feature
        # backup carries no extruder_id, and a live index that reports none must
        # not turn every entry into an add.
        extruder_id = entry.get("extruder_id")
        if isinstance(extruder_id, int) and any(getattr(c, "extruder_id", None) is not None for c in candidates):
            candidates = [c for c in candidates if getattr(c, "extruder_id", None) == extruder_id]

        available = [c for c in candidates if c.slot_id not in claimed]
        if not available:
            return None

        setting_id = entry.get("setting_id")
        if setting_id:
            for c in available:
                if c.setting_id == setting_id:
                    return c

        name = entry.get("name")
        if name:
            for c in available:
                if c.name == name:
                    return c

        # Exactly one profile for this filament and no better discriminator:
        # treat it as the same profile rather than duplicating it. Judged
        # against every candidate rather than the unclaimed ones, because two
        # live profiles for one filament are ambiguous whether or not another
        # entry has already taken one of them.
        return available[0] if len(candidates) == 1 else None


# Singleton instance
git_restore_service = GitRestoreService()
