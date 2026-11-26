import asyncio
import logging
import math
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from anyio import CancelScope
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message, Receive, Scope, Send

from backend.app.core import query_timing
from backend.app.core.auth import (
    RequestCredentials,
    RequireAdmin,
    RequireAnyPermission,
    RequirePermission,
    get_current_user_optional,
    request_credentials,
    require_energy_cost_update,
    require_permission,
)
from backend.app.core.config import settings as app_settings
from backend.app.core.credential_config import canonical_url, lock_credential_config, merge_secret_fields
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.core.schema_repair_state import SchemaRepairPending
from backend.app.core.tasks import spawn_background_task
from backend.app.i18n.api_errors import json_error
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.schemas.settings import (
    SERVICE_CREDENTIAL_URL_FIELDS,
    AppSettings,
    AppSettingsRead,
    AppSettingsUpdate,
    opaque_service_url,
    storable_text,
)
from backend.app.services import energy_price

logger = logging.getLogger(__name__)

_settings_read_gate = require_permission(Permission.SETTINGS_READ)

router = APIRouter(prefix="/settings", tags=["settings"])

# Default settings
DEFAULT_SETTINGS = AppSettings()

# Auth policy, credential/destination pairs and deployment/backup controls are
# administrative even when a custom group has settings:update. Other preferences
# keep that permission's existing behavior. Unchanged full-form values may pass.
ADMIN_SETTING_FIELDS = frozenset(
    {
        "local_login_enabled",
        "session_max_hours",
        "telegram_registration_open",
        "mcp_enabled",
        "mcp_allow_http",
        "mcp_endpoint_url",
        "mcp_allowed_hosts",
        "mcp_allowed_origins",
        "mqtt_enabled",
        "mqtt_broker",
        "mqtt_port",
        "mqtt_username",
        "mqtt_password",
        "mqtt_topic_prefix",
        "mqtt_use_tls",
        "mqtt_tls_verify",
        "ldap_enabled",
        "ldap_server_url",
        "ldap_bind_dn",
        "ldap_bind_password",
        "ldap_search_base",
        "ldap_user_filter",
        "ldap_security",
        "ldap_group_mapping",
        "ldap_auto_provision",
        "ldap_default_group",
        "ha_enabled",
        "ha_url",
        "ha_token",
        # Choosing the sensor makes the server ask Home Assistant with the stored token.
        "energy_price_source",
        "energy_price_ha_entity",
        "prometheus_enabled",
        "prometheus_token",
        "local_backup_enabled",
        "local_backup_schedule",
        "local_backup_time",
        "local_backup_retention",
        "local_backup_path",
        "virtual_printer_enabled",
        "virtual_printer_access_code",
        "virtual_printer_mode",
        "virtual_printer_archive_name_source",
        "docker_compose_dir",
        "external_url",
        "zigbee_enabled",
        "zigbee_transport",
        "zigbee_path",
        "spoolman_enabled",
        "spoolman_url",
        "spoolman_sync_mode",
        "orcaslicer_api_url",
        "bambu_studio_api_url",
        "obico_enabled",
        "obico_ml_url",
        "obico_snapshot_base_url",
        "obico_ml_token",
    }
)


async def _enforce_admin_settings(db: AsyncSession, changes: dict, user: User | None) -> None:
    protected = ADMIN_SETTING_FIELDS & changes.keys()
    if not protected or (user is not None and user.is_admin):
        return
    # A write-only value has no legitimate equal-value full-form baggage.
    # Refuse before reading/comparing it, including empty and null guesses.
    if protected & SETTINGS_SECRETS:
        raise HTTPException(status_code=403, detail="Requires admin role for protected settings")
    current = (await _load_settings(db, effective_env=False)).model_dump()
    if any(key in protected and opaque_service_url(current.get(key)) for key in SERVICE_CREDENTIAL_URL_FIELDS):
        raise HTTPException(status_code=403, detail="Requires admin role for protected settings")
    if any(changes[key] != current.get(key) for key in protected):
        raise HTTPException(status_code=403, detail="Requires admin role for protected settings")
    # Equal protected fields are accepted as read-only full-form baggage. Do
    # not persist them: another administrator may change policy after our read.
    for key in protected:
        changes.pop(key)


async def _refuse_by_role(db: AsyncSession, changes: dict, actions, user: User | None) -> None:
    """A save's refusals that depend on the caller's role alone (403); equal protected baggage is dropped."""
    if actions and not (user is not None and user.is_admin):
        raise HTTPException(403, "Requires admin role for protected settings")
    await _enforce_admin_settings(db, changes, user)


async def get_setting(db: AsyncSession, key: str) -> str | None:
    """Get a single setting value by key."""
    result = await db.execute(select(Settings).where(Settings.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def get_external_login_url(db: AsyncSession) -> str:
    """Get the external URL for the login page.

    Uses external_url from settings if available, otherwise falls back to APP_URL env var.

    Args:
        db: Database session

    Returns:
        Full URL to the login page
    """
    import os

    external_url = await get_setting(db, "external_url")
    if external_url:
        external_url = external_url.rstrip("/")
    else:
        external_url = os.environ.get("APP_URL", "http://localhost:5173")
    return external_url + "/login"


async def set_setting(db: AsyncSession, key: str, value: str, *, price_source: str = "manual") -> None:
    """Set a single setting value (dialect-aware upsert).

    The electricity price is the one row with a history (m245): its change is recorded here, in the
    same transaction and before the row is written, so no door that saves it can forget to.
    ``price_source`` says who changed it and matters for that key alone.
    """
    from backend.app.core.db_dialect import upsert_setting

    if key == energy_price.SETTING_KEY:
        await energy_price.note_setting_write(db, value, price_source)
    await upsert_setting(db, Settings, key, value)


def _effective_ha_connection(merged: dict) -> dict:
    """The Home Assistant connection a save would leave in effect (environment first, as everywhere)."""
    url_env, token_env = os.environ.get("HA_URL"), os.environ.get("HA_TOKEN")
    return {
        "ha_enabled": True if url_env and token_env else bool(merged.get("ha_enabled")),
        "ha_url": url_env or merged.get("ha_url") or "",
        "ha_token": token_env or merged.get("ha_token") or "",
    }


def _energy_price_refusal(reason: str) -> HTTPException:
    """Why the price could not be read from Home Assistant, as the administrator is told it."""
    if reason == "not_configured":
        return HTTPException(
            400,
            detail={
                "error": "energy_price_read_failed",
                "message": "Home Assistant is not connected. Set its address and token first, "
                "then choose it as the electricity price source.",
            },
        )
    if reason == "no_entity":
        return HTTPException(
            422,
            detail={
                "error": "energy_price_read_failed",
                "message": "Choose the Home Assistant sensor that reports the electricity price.",
            },
        )
    if reason == "no_such_entity":
        return HTTPException(
            422,
            detail={
                "error": "energy_price_read_failed",
                "message": "Home Assistant has no such sensor for the electricity price.",
            },
        )
    if reason == "unusable":
        return HTTPException(
            422,
            detail={
                "error": "energy_price_read_failed",
                "message": "The Home Assistant sensor gives no usable electricity price right now.",
            },
        )
    if reason == "timeout":
        return HTTPException(
            504,
            detail={
                "error": "energy_price_read_failed",
                "message": "Home Assistant did not answer in time; the electricity price source was not changed.",
            },
        )
    return HTTPException(
        502,
        detail={
            "error": "energy_price_read_failed",
            "message": "Home Assistant could not be reached; the electricity price source was not changed.",
        },
    )


async def _energy_price_plan(db: AsyncSession, update_data: dict, merged: dict):
    """What a save means for the electricity price; the caller holds the credential anchor.

    Returns the Home Assistant destination to read BEFORE the save is written -- when the save makes
    the sensor the source, or names another sensor -- or None. While the sensor is the source the
    price is its last reading: a different price typed into the save is refused, an equal one (a
    full form sent back) is dropped.
    """
    if merged.get(energy_price.SOURCE_SETTING_KEY) != energy_price.PRICE_SOURCE_HOMEASSISTANT:
        return None
    if energy_price.SETTING_KEY in update_data:
        sent = update_data[energy_price.SETTING_KEY]
        if sent is None or energy_price.clean_price(sent) != await energy_price.stored_price(db):
            raise HTTPException(
                409,
                detail={
                    "error": "energy_price_from_homeassistant",
                    "message": "The electricity price is read from Home Assistant and cannot be set by hand. "
                    "Switch the price source to a fixed price to enter it.",
                },
            )
        update_data.pop(energy_price.SETTING_KEY)
    stored = {
        key: await get_setting(db, key) for key in (energy_price.SOURCE_SETTING_KEY, energy_price.ENTITY_SETTING_KEY)
    }
    entity = merged.get(energy_price.ENTITY_SETTING_KEY) or ""
    if stored[energy_price.SOURCE_SETTING_KEY] == energy_price.PRICE_SOURCE_HOMEASSISTANT and entity == (
        stored[energy_price.ENTITY_SETTING_KEY] or ""
    ):
        return None  # nothing about the source changes: the hourly read keeps the price
    try:
        return energy_price.target_from(_effective_ha_connection(merged), entity)
    except energy_price.PriceReadFailed as exc:
        raise _energy_price_refusal(exc.reason) from None


async def _lock_mcp_http_policy(db: AsyncSession) -> None:
    """Serialize policy writers until commit or rollback, even before the first setting row exists."""
    if db.get_bind().dialect.name == "postgresql":
        # Dedicated settings lock; the two-int namespace is separate from bigint locks.
        await db.execute(text("SELECT pg_advisory_xact_lock(215, 1)"))
    # The credential anchor is already SQLite's writer reservation.


SETTINGS_SECRETS = frozenset(
    {
        "mqtt_password",
        "ha_token",
        "ldap_bind_password",
        "obico_ml_token",
        "prometheus_token",
        "virtual_printer_access_code",
    }
)
_mqtt_apply_lock = asyncio.Lock()


async def _apply_mqtt_runtime(db: AsyncSession, *, smart_plug_only: bool = False) -> None:
    from backend.app.services.mqtt_relay import mqtt_relay

    # Re-read after taking the service lock so a queued apply cannot restore
    # an older writer's pair. The database lock is released before network I/O.
    async with _mqtt_apply_lock:
        await lock_credential_config(db)
        snapshot = (await _load_settings(db, effective_env=False)).model_dump()
        mqtt_settings = {key: value for key, value in snapshot.items() if key.startswith("mqtt_")}
        await db.rollback()
        if smart_plug_only:
            # Plug subscriptions are independently enabled by their owning
            # adapter even when outbound printer-status publishing is off.
            mqtt_settings["mqtt_enabled"] = True
            await mqtt_relay.smart_plug_service.configure(mqtt_settings)
        else:
            await mqtt_relay.configure(mqtt_settings)


def _ldap_credential_scope(config: dict) -> tuple:
    from ldap3.core.exceptions import LDAPException

    from backend.app.services.ldap_service import _create_server, parse_ldap_config

    url = str(config.get("ldap_server_url") or "").strip()
    if opaque_service_url(url) or ("://" in url and url.split("://", 1)[0].lower() not in {"ldap", "ldaps"}):
        # Preserve opaque/malformed legacy tuples for unrelated metadata edits.
        # Any destination/security change still differs and requires reissue.
        return (
            config.get("ldap_server_url"),
            config.get("ldap_security"),
            str(config.get("ldap_bind_dn") or "").strip(),
        )
    ldap = parse_ldap_config(
        {
            "ldap_enabled": "true",
            "ldap_server_url": config.get("ldap_server_url") or "",
            "ldap_security": config.get("ldap_security") or "starttls",
        }
    )
    if ldap is None:
        return ("", None, None, str(config.get("ldap_bind_dn") or "").strip())
    try:
        server = _create_server(ldap)  # Parses the sender's defaults; no connection or I/O.
    except (ValueError, TypeError, LDAPException):
        # Malformed pre-existing tuples may still be disabled or renamed.
        # A newly supplied/tested destination is validated separately below.
        return (url, config.get("ldap_security"), str(config.get("ldap_bind_dn") or "").strip())
    transport = (
        "ldaps"
        if server.ssl
        else "starttls"
        if ldap.security == "starttls" and not ldap.server_url.startswith("ldaps://")
        else "plain"
    )
    return (server.host.lower(), server.port, transport, str(config.get("ldap_bind_dn") or "").strip())


def _validate_ldap_destination(config: dict) -> None:
    """Validate a new/tested destination without weakening legacy metadata edits."""
    from ldap3.core.exceptions import LDAPException

    from backend.app.services.ldap_service import _create_server, parse_ldap_config

    url = str(config.get("ldap_server_url") or "").strip()
    if "://" in url and url.split("://", 1)[0].lower() not in {"ldap", "ldaps"}:
        raise HTTPException(422, "Invalid LDAP destination")
    if opaque_service_url(url):
        # Only a retained legacy value can reach this path: new opaque base
        # URL writes are refused by the resolver. Keep its sender semantics.
        return
    try:
        ldap = parse_ldap_config(
            {
                "ldap_enabled": "true",
                "ldap_server_url": url,
                "ldap_security": str(config.get("ldap_security") or "starttls"),
            }
        )
        if ldap:
            _create_server(ldap)
    except (ValueError, TypeError, LDAPException):
        raise HTTPException(422, "Invalid LDAP destination") from None


@router.get("", response_model=AppSettingsRead)
@router.get("/", response_model=AppSettingsRead)
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get settings without exposing stored or environment credentials."""
    raw = (await _load_settings(db)).model_dump()
    presence = {f"{key}_configured": bool(raw.get(key)) for key in SETTINGS_SECRETS}
    for key in SERVICE_CREDENTIAL_URL_FIELDS:
        presence[f"{key}_configured"] = bool(raw.get(key))
        presence[f"{key}_opaque"] = opaque_service_url(raw.get(key))
        if presence[f"{key}_opaque"]:
            raw[key] = ""
    return AppSettingsRead(**raw, **presence)


async def _load_settings(db: AsyncSession, *, effective_env: bool = True) -> AppSettings:
    """Private typed snapshot; never use this as an HTTP response model."""
    settings_dict = DEFAULT_SETTINGS.model_dump()

    # Load saved settings from database
    result = await db.execute(select(Settings))
    db_settings = result.scalars().all()

    for setting in db_settings:
        if setting.key in settings_dict:
            try:
                settings_dict[setting.key] = _parse_stored(setting.key, setting.value)
            except (TypeError, ValueError, AttributeError):
                _warn_unreadable(setting.key)

    # Get Home Assistant settings (with environment variable overrides)
    if effective_env:
        settings_dict.update(await get_homeassistant_settings(db))

    return _validated_snapshot(settings_dict)


_unreadable_reported: set[str] = set()


def _warn_unreadable(key: str) -> None:
    # Once per key and process: this runs on every settings read.
    if key not in _unreadable_reported:
        _unreadable_reported.add(key)
        logger.warning("Stored setting %r cannot be read; using its default until it is saved again", key)


def _validated_snapshot(settings_dict: dict) -> AppSettings:
    """A stored row outside its field's bounds reads as the default (upstream #2518).

    One bad row used to fail every settings read, ``ui-preferences`` and every save -- so the row
    could not be repaired from the interface. The value on disk is left alone; saving the field
    replaces it.
    """
    try:
        return AppSettings(**settings_dict)
    except ValidationError as exc:
        defaults = DEFAULT_SETTINGS.model_dump()
        bad = {str(error["loc"][0]) for error in exc.errors() if error.get("loc")}
        if not bad or not bad <= defaults.keys():
            raise
        for key in bad:
            _warn_unreadable(key)
            settings_dict[key] = defaults[key]
        return AppSettings(**settings_dict)


def _parse_stored(key: str, value: str):
    """One stored row as its field's type; raises for a row that is not readable as that type."""
    # Parse the value based on the expected type
    if key in [
        "save_thumbnails",
        "mcp_enabled",
        "mcp_allow_http",
        "capture_finish_photo",
        "camera_light_auto",
        "camera_light_auto_obico",
        "archive_3mf_retention_enabled",
        "spoolman_enabled",
        "spoolman_disable_weight_sync",
        "spoolman_report_partial_usage",
        "auto_add_unknown_rfid",
        "disable_filament_warnings",
        "check_updates",
        "check_printer_firmware",
        "include_beta_updates",
        "telemetry_enabled",
        "virtual_printer_enabled",
        "connector_enabled",
        "ftp_retry_enabled",
        "mqtt_enabled",
        "mqtt_use_tls",
        "mqtt_tls_verify",
        "ha_enabled",
        "per_printer_mapping_expanded",
        "prometheus_enabled",
        "user_notifications_enabled",
        "prefer_lowest_filament",
        "queue_shortest_first",
        "forecast_operator_absence_enabled",
        "auto_queue_rebalance_models",
        "auto_queue_return_failed",
        "auto_queue_compatible_models",
        "auto_order_for_batches",
        "default_force_color_match",
        "queue_drying_enabled",
        "queue_drying_block",
        "ambient_drying_enabled",
        "print_drying_enabled",
        "stagger_enabled",
        "stagger_wait_for_bed",
        "stagger_strict_for_direct_dispatch",
        "stagger_split_by_tags",
        "stagger_split_by_location",
        "ldap_enabled",
        "ldap_auto_provision",
        "library_all_files_recursive",
        "local_login_enabled",
        "runout_archive_spool_enabled",
    ]:
        return value.lower() == "true"
    elif key in ["runout_zero_point_enabled", "ams_sync_bidirectional"]:
        # On unless the row says "false" — exactly how usage_tracker and
        # main.on_ams_change read these two, so a stored "None" (a PUT
        # with null) neither 500s here nor reads off while acting on.
        return value.lower() != "false"
    elif key in [
        "default_filament_cost",
        "energy_cost_per_kwh",
        "ams_temp_good",
        "ams_temp_fair",
        "library_disk_warning_gb",
        "low_stock_threshold",
    ]:
        return float(value)
    elif key == "ams_temp_alarm":
        # Nullable float. Storage stringifies None to "None", so it cannot
        # join the list above — float("None") would take the whole
        # settings response down with it (upstream #2905). Garbage reads
        # as unset, the same fallback the alarm itself applies — a
        # non-finite value too, which JSON could not carry anyway.
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None
    elif key in [
        "ams_humidity_good",
        "ams_humidity_fair",
        "ams_history_retention_days",
        "inbox_retention_days",
        "pause_warning_minutes",
        "outcome_reminder_after_minutes",
        "printer_sensor_history_retention_days",
        "archive_3mf_retention_days",
        "log_retention_days",
        "slow_query_ms",
        "slow_request_ms",
        "ftp_retry_count",
        "ftp_retry_delay",
        "ftp_timeout",
        "mqtt_port",
        "stagger_concurrent",
        "stagger_interval_minutes",
        "forecast_global_lead_time_days",
        "auto_drying_sustained_minutes",
        "location_sensor_poll_interval",
        "forecast_upload_seconds",
        "forecast_plate_clear_minutes",
        "firmware_batch_concurrency",
        "session_max_hours",
        "connector_max_upload_bytes",
    ]:
        return int(value)
    elif key == "default_printer_id":
        # Handle nullable integer
        return int(value) if value and value != "None" else None
    elif key == "open_in_slicer":
        # None means "inherit from preferred_slicer" (#1329). The PUT path
        # serializes None as the literal string "None"; strip it back so
        # the frontend sees a true null and falls back as intended.
        return value if value and value != "None" else None
    elif key in {"mcp_allowed_hosts", "mcp_allowed_origins"}:
        import json

        return json.loads(value)
    else:
        return value


async def resolve_settings_credentials(db: AsyncSession, changes: dict, actions: dict | None = None) -> dict:
    """Resolve a locked raw draft, comparing the effective sender destination.

    Callers own the anchor transaction and release it before any network I/O.
    Environment values are used only in comparison and never copied to storage.
    """
    stored = (await _load_settings(db, effective_env=False)).model_dump()
    touched = changes.keys() | (actions or {}).keys()
    if any(key in changes and changes[key] is None for key in SETTINGS_SECRETS):
        raise HTTPException(422, "Credential values must be strings; use an explicit clear action")

    def ha_scope(config):
        return canonical_url(os.environ.get("HA_URL") or config.get("ha_url"))

    scopes = {
        "mqtt_password": lambda c: (
            str(c.get("mqtt_broker") or "").strip().lower(),
            c.get("mqtt_port"),
            c.get("mqtt_use_tls"),
            # Part of the destination: with the check off, a kept password could be sent to whoever answers.
            bool(c.get("mqtt_tls_verify")),
            c.get("mqtt_username") or "",
        ),
        "ha_token": ha_scope,
        "ldap_bind_password": _ldap_credential_scope,
        "obico_ml_token": lambda c: canonical_url(c.get("obico_ml_url")),
    }
    opaque_fields = {key for key in SERVICE_CREDENTIAL_URL_FIELDS if opaque_service_url(stored.get(key))}
    for key in SERVICE_CREDENTIAL_URL_FIELDS & changes.keys():
        if opaque_service_url(changes[key]):
            raise HTTPException(422, "Base URL must not contain credentials, query parameters or fragments")
    effective_actions = dict(actions or {})
    # An environment credential is a real retained source, but it must never
    # be materialized into the DB snapshot just to satisfy explicit keep.
    if os.environ.get("HA_TOKEN") and effective_actions.get("ha_token") == "keep" and "ha_token" not in changes:
        effective_actions.pop("ha_token")
    merged = merge_secret_fields(stored, changes, SETTINGS_SECRETS | opaque_fields, effective_actions, scope=scopes)
    if any(key in touched and merged.get(key) != stored.get(key) for key in ("ldap_server_url", "ldap_security")):
        _validate_ldap_destination(merged)
    if os.environ.get("HA_TOKEN") and ha_scope(stored) != ha_scope(merged):
        raise HTTPException(400, "ha_token: environment credential requires an unchanged effective destination")
    effective_ha_token = os.environ.get("HA_TOKEN") or merged.get("ha_token")
    if (
        {"ha_enabled", "ha_url", "ha_token"} & touched
        and merged.get("ha_enabled")
        and (not effective_ha_token or not (os.environ.get("HA_URL") or merged.get("ha_url")))
    ):
        raise HTTPException(400, "ha_token: enabled integration requires a credential")
    if (
        any(k.startswith("mqtt_") for k in touched)
        and merged.get("mqtt_enabled")
        and merged.get("mqtt_username")
        and not merged.get("mqtt_password")
    ):
        raise HTTPException(400, "mqtt_password: authenticated MQTT requires a credential")
    if (
        any(k.startswith("ldap_") for k in touched)
        and merged.get("ldap_enabled")
        and merged.get("ldap_bind_dn")
        and not merged.get("ldap_bind_password")
    ):
        raise HTTPException(400, "ldap_bind_password: authenticated LDAP requires a credential")
    if (
        {"prometheus_enabled", "prometheus_token"} & touched
        and merged.get("prometheus_enabled")
        and not str(merged.get("prometheus_token") or "").strip()
    ):
        raise HTTPException(400, "prometheus_token: enabled metrics requires a credential")
    code = merged.get("virtual_printer_access_code")
    if "virtual_printer_access_code" in touched and code and len(code) != 8:
        raise HTTPException(400, "virtual_printer_access_code: access code must be exactly 8 characters")
    if (
        any(k.startswith("virtual_printer_") for k in touched)
        and merged.get("virtual_printer_enabled")
        and merged.get("virtual_printer_mode") != "proxy"
        and not code
    ):
        raise HTTPException(400, "virtual_printer_access_code: enabled integration requires a credential")
    # These three legacy nullable toggles already have explicit stored-"None" semantics
    # in _load_settings and their consumers. Validate that effective value, without
    # relaxing credential nulls or accepting None for unrelated non-nullable fields.
    effective = dict(merged)
    for key in ("runout_archive_spool_enabled", "runout_zero_point_enabled", "ams_sync_bidirectional"):
        if effective.get(key) is None:
            effective[key] = getattr(DEFAULT_SETTINGS, key)
    try:
        AppSettings(**effective)
    except ValidationError:
        raise HTTPException(422, "Invalid effective settings configuration") from None
    return merged


@router.put("/", response_model=AppSettingsRead)
async def update_settings(
    settings_update: AppSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Update application settings.

    A save that names the Spoolman mode or address may change the Spoolman source
    (C047 §C1): the source guard is taken before the credential anchor, released
    for the Home Assistant read and taken again before the second anchor, and
    released right after the commit.
    """
    from backend.app.services import spoolman_source_changes

    names_source = spoolman_source_changes.names_source(settings_update.model_dump(exclude_unset=True))
    async with spoolman_source_changes.owning(db, when=names_source) as change:
        return await _update_settings(settings_update, db, current_user, change)


async def _update_settings(settings_update: AppSettingsUpdate, db: AsyncSession, current_user, change):
    update_data = settings_update.model_dump(exclude_unset=True)
    actions = update_data.pop("secret_actions", None)
    if change.held:
        # A change of the mode waits for the slot-ledger writers before the anchor (C047 A12).
        # The caller's role needs no anchor: a save it refuses is refused before that wait and
        # holds no writer (A14). A copy, so equal protected baggage is dropped only under the
        # anchor, which asks again.
        await _refuse_by_role(db, dict(update_data), actions, current_user)
        await change.wait_before_anchor(db, enabled=update_data.get("spoolman_enabled"))
    await lock_credential_config(db)
    await _refuse_by_role(db, update_data, actions, current_user)
    merged = await resolve_settings_credentials(db, update_data, actions)
    # The electricity price from Home Assistant (m245). The read is network I/O, so it happens with
    # no transaction open: the destination is frozen under the anchor, the anchor is released, the
    # sensor is asked, and the save starts again from the top -- on a destination that must still
    # be the one that was asked. A read that fails refuses the save: nothing was written.
    ha_price = None
    price_target = await _energy_price_plan(db, update_data, merged)
    if price_target is not None:
        await db.rollback()
        # The source guard is not held over the network (C047 S30): taken again before the next anchor.
        source_held = change.held
        await change.release()
        try:
            ha_price = await energy_price.fetch_price(price_target)
        except energy_price.PriceReadFailed as exc:
            raise _energy_price_refusal(exc.reason) from None
        if source_held:
            await change.acquire(db)
            await change.wait_before_anchor(db, enabled=update_data.get("spoolman_enabled"))
        await lock_credential_config(db)
        await _enforce_admin_settings(db, update_data, current_user)
        merged = await resolve_settings_credentials(db, update_data, actions)
        if await _energy_price_plan(db, update_data, merged) != price_target:
            raise HTTPException(409, "Settings changed while the electricity price was being read. Try again.")
    for key in (SETTINGS_SECRETS | SERVICE_CREDENTIAL_URL_FIELDS) & (update_data.keys() | (actions or {}).keys()):
        update_data[key] = merged[key]
    risk_ack = update_data.pop("mcp_http_risk_acknowledged", False)
    network_ack = update_data.pop("mcp_http_trusted_network_acknowledged", False)
    if {"mcp_allow_http", "mcp_endpoint_url"} & update_data.keys():
        from backend.app.mcp_server.config import canonical_endpoint

        await _lock_mcp_http_policy(db)
        was_allowed = await get_setting(db, "mcp_allow_http") == "true"
        allow_http = update_data.get("mcp_allow_http", was_allowed)
        if allow_http and not was_allowed and not (risk_ack and network_ack):
            raise HTTPException(422, "Enabling MCP HTTP requires both explicit risk acknowledgements")
        endpoint = update_data.get("mcp_endpoint_url")
        if endpoint is None:
            endpoint = await get_setting(db, "mcp_endpoint_url") or ""
        try:
            canonical_endpoint(endpoint, allow_http=allow_http)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    if any(key.startswith("forecast_operator_") for key in update_data):
        current = (await get_settings(db, None)).model_dump()
        operator = {**current, **update_data}
        if (
            any(
                operator[key] is None
                for key in (
                    "forecast_operator_absence_enabled",
                    "forecast_operator_absence_start",
                    "forecast_operator_absence_end",
                    "forecast_operator_timezone",
                )
            )
            or operator["forecast_operator_absence_start"] == operator["forecast_operator_absence_end"]
        ):
            raise HTTPException(status_code=422, detail="Operator absence start and end must differ.")

    # #1589: safety refusals on disabling local login. Two failure modes would
    # otherwise lock everyone out of the install:
    #   1. No enabled OIDC provider exists — nobody could authenticate.
    #   2. The caller has no UserOIDCLink — they would lock themselves out even
    #      if other admins are linked.
    # Either case returns HTTP 400 instead of silently saving. The
    # BAMDUDE_LOCAL_LOGIN=true env-var bypass on /auth/login is a separate
    # recovery path; the refusals here protect the *default* configuration
    # where the env var is absent. API-key callers have current_user=None, so
    # the link check is skipped for them (matching upstream).
    if update_data.get("local_login_enabled") is False:
        from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink

        enabled_count = await db.scalar(select(func.count(OIDCProvider.id)).where(OIDCProvider.is_enabled.is_(True)))
        if not enabled_count:
            raise HTTPException(
                status_code=400,
                detail="Cannot disable local login: no OIDC provider is enabled.",
            )
        if current_user is not None:
            caller_links = await db.scalar(
                select(func.count(UserOIDCLink.id)).where(UserOIDCLink.user_id == current_user.id)
            )
            if not caller_links:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot disable local login: your account has no OIDC link, so you would lock yourself out.",
                )

    # Check if any MQTT settings are being updated
    mqtt_keys = {
        "mqtt_enabled",
        "mqtt_broker",
        "mqtt_port",
        "mqtt_username",
        "mqtt_password",
        "mqtt_topic_prefix",
        "mqtt_use_tls",
        "mqtt_tls_verify",
    }
    mqtt_updated = bool(mqtt_keys & set(update_data.keys()))

    if change.held:
        # After every validation, before the first write (C047 §C1, S44).
        await change.decide(db, enabled=update_data.get("spoolman_enabled"), url=update_data.get("spoolman_url"))

    for key, value in update_data.items():
        # Convert value to string for storage
        if isinstance(value, bool):
            str_value = "true" if value else "false"
        elif value is None:
            str_value = "None"
        elif key in {"mcp_allowed_hosts", "mcp_allowed_origins"}:
            import json

            str_value = json.dumps(value)
        else:
            str_value = str(value)
        await set_setting(db, key, str_value)
    if ha_price is not None:
        # The reading that proved the source works is its first price, in the save's own transaction.
        await energy_price.set_price(db, ha_price, "homeassistant")

    await db.commit()
    # The source is settled with the commit; the side effects below do not hold it.
    await change.release()
    # Expire all objects to ensure fresh reads after commit
    db.expire_all()

    if "obico_save_detections" in update_data:
        from backend.app.services.obico_history import obico_history

        obico_history.set_enabled(bool(update_data["obico_save_detections"]))

    if {"mcp_enabled", "mcp_allowed_hosts", "mcp_allowed_origins"} & update_data.keys():
        from backend.app.mcp_server.runtime import mcp_runtime

        await mcp_runtime.reload()

    # Opt-out: when telemetry is turned off, ask the relay to erase this install.
    if update_data.get("telemetry_enabled") is False:
        from backend.app.services.telemetry import forget_telemetry

        spawn_background_task(forget_telemetry(), name="forget-telemetry")

    # If log retention changed, push the new value into the live
    # rotating-file handler so the next midnight rotation honours it
    # without a backend restart. Old archives beyond the new limit are
    # purged on the next rotation, not immediately — operators wanting
    # an instant cleanup can delete files from the System page UI.
    if "log_retention_days" in update_data and update_data["log_retention_days"] is not None:
        from backend.app.core.logging_state import update_log_retention

        update_log_retention(int(update_data["log_retention_days"]))

    # Same idea for the timing thresholds: the listeners are attached to the
    # engine long before a database exists, so they read module state that this
    # applies at once — no restart to start (or stop) measuring.
    for _key, _apply in (
        ("slow_query_ms", query_timing.set_query_threshold_ms),
        ("slow_request_ms", query_timing.set_request_threshold_ms),
    ):
        if update_data.get(_key) is not None:
            _apply(int(update_data[_key]))

    # Update DB templates when system language changes
    locale_lang = update_data.get("language")
    if locale_lang:
        try:
            from backend.app.core.database import async_session
            from backend.app.services.locale_updater import update_locale_data

            logger.info("Updating locale data to '%s'...", locale_lang)
            async with async_session() as locale_db:
                result = await update_locale_data(locale_db, locale_lang)
                logger.info("Locale update result: %s", result)
        except Exception as e:
            logger.error("Failed to update locale data: %s", e, exc_info=True)

    # Reconfigure MQTT relay if any MQTT settings changed
    if mqtt_updated:
        try:
            await _apply_mqtt_runtime(db)
        except Exception:
            pass  # Don't fail the settings update if MQTT reconfiguration fails

    # Return updated settings
    return await get_settings(db)


@router.patch("/", response_model=AppSettingsRead)
@router.patch("", response_model=AppSettingsRead)
async def patch_settings(
    settings_update: AppSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Partially update application settings (same as PUT, for REST compatibility)."""
    return await update_settings(settings_update, db, _)


class ElectricityPriceUpdate(BaseModel):
    """Payload for ``POST /settings/electricity-price`` (upstream Bambuddy
    #1356 / commit ae29a7dc).

    Plain non-negative float; tariffs can go as low as 0.0 in some markets
    (e.g. free hours). Field name matches the existing
    ``energy_cost_per_kwh`` row in ``AppSettings`` so Home Assistant
    ``rest_command`` examples docs reference the same name end-to-end.
    """

    energy_cost_per_kwh: float = Field(ge=0)


@router.post("/electricity-price", response_model=AppSettingsRead)
async def update_electricity_price(
    payload: ElectricityPriceUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(require_energy_cost_update()),
):
    """Update the per-kWh electricity cost used by the energy-tracking pipeline.

    A narrowly-scoped settings-write endpoint for the Home-Assistant
    dynamic-tariff use case. JWT users still need ``SETTINGS_UPDATE``;
    API keys need the ``can_update_energy_cost`` opt-in flag on their
    row (default False so existing keys never silently gain settings-
    write capability on upgrade). The general ``PATCH /settings`` route
    is intentionally left alone — we expose this endpoint as a cleaner
    alternative URL rather than as a workaround for a block (BamDude's
    API-key auth is more permissive than upstream's by design).

    See ``wiki/features/energy.md`` and upstream Bambuddy #1356 for the
    full rationale.
    """
    # An external push stays allowed while the source is Home Assistant: it is the same price
    # arriving another way, and its change is recorded as the API's.
    await set_setting(db, "energy_cost_per_kwh", str(payload.energy_cost_per_kwh), price_source="api")
    await db.commit()
    db.expire_all()
    # Return the full settings response so the HA integration can confirm
    # the new value applied. Build via the same path PATCH /settings uses.
    return await get_settings(db, _)


@router.post("/reset", response_model=AppSettingsRead, dependencies=[RequireAdmin()])
async def reset_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Reset all settings to defaults.

    Reset deletes the Spoolman mode, address and history-source rows too: a
    change of the source (C047 §C1), owned under the source guard taken before
    the credential anchor and refused while a print is being charged. Without
    its row the mode reads as the default, so a reset in Spoolman mode moves the
    mode like any other door (acceptance A09): it waits for the slot-ledger
    writers before its anchor and empties the Spoolman slot ledger.
    """
    from backend.app.services import spoolman_source_changes

    async with spoolman_source_changes.owning(db) as change:
        await change.wait_before_anchor(db, enabled=DEFAULT_SETTINGS.spoolman_enabled)
        await _reset_settings(db, change)
    await _apply_mqtt_runtime(db)
    return await get_settings(db=db, _=None)


async def _reset_settings(db: AsyncSession, change) -> None:
    from backend.app.core.credential_config import CONFIG_GUARD_KEY

    await lock_credential_config(db)
    await _lock_mcp_http_policy(db)
    source_rows = (
        await db.execute(
            select(func.count())
            .select_from(Settings)
            .where(Settings.key.in_(("spoolman_enabled", "spoolman_url", "spoolman_usage_source")))
        )
    ).scalar_one()
    if source_rows:
        # The mode the rows' deletion leaves is the default one (A09).
        await change.decide(db, enabled=DEFAULT_SETTINGS.spoolman_enabled, record_changes=True)
    # The installation transaction anchor is infrastructure, not a preference.
    # Keep it locked and present across reset; a later editor must never lazily
    # create its own independent serialization row.
    result = await db.execute(select(Settings).where(Settings.key != CONFIG_GUARD_KEY).order_by(Settings.key))
    from backend.app.core.schema_repair_state import private_setting

    # The price goes back to its default with its row: a change like any other, recorded before
    # the row is deleted (the history is not a setting and survives the reset).
    await energy_price.note_setting_write(db, None, "manual")
    for setting in result.scalars().all():
        if not private_setting(setting.key):
            await db.delete(setting)

    await db.commit()


# Curated, non-sensitive UI-rendering settings exposed without SETTINGS_READ
# (#1293). A non-admin operator with printers access but no settings:read used
# to get a 403 on GET /settings, so the Printers page silently lost camera view
# mode, time/date format, drying presets, AMS humidity/temp thresholds, etc.
# ONLY these explicitly-listed fields are returned — never a credential or any
# other setting. Adding a sensitive key here would fail the leak-canary test.
_UI_PREFERENCE_FIELDS: tuple[str, ...] = (
    "camera_view_mode",
    "time_format",
    "date_format",
    # The Workshop's prices (WS-13 E13 T17): its readers are not settings readers.
    "currency",
    "spool_display_template",
    "spool_sort_order",
    "check_printer_firmware",
    "use_slicer_api",
    "drying_presets",
    "ams_humidity_thresholds",
    "ams_humidity_good",
    "ams_humidity_fair",
    "ams_temp_good",
    "ams_temp_fair",
    "language",
    "dark_style",
    "dark_background",
    "dark_accent",
    "light_style",
    "light_background",
    "light_accent",
    "open_in_slicer",
    "preferred_slicer",
    "disable_filament_warnings",
    "prefer_lowest_filament",
    "per_printer_mapping_expanded",
    "default_filament_cost",
    "auto_order_for_batches",
    "default_force_color_match",
    "queue_shortest_first",
    "low_stock_threshold",
    "library_all_files_recursive",
    "library_disk_warning_gb",
    "device_labels_enabled",
    "user_notifications_enabled",
    "check_updates",
    # ams_temp_alarm deliberately NOT here: this endpoint serves the colour
    # bands without SETTINGS_READ, and the alarm threshold colours nothing —
    # only the settings page reads it, behind the settings permissions.
)


@router.get("/ui-preferences")
async def get_ui_preferences(
    db: AsyncSession = Depends(get_db),
    _: User | None = Depends(get_current_user_optional),
):
    """Curated UI-rendering settings that pages must render correctly even for
    users without SETTINGS_READ (#1293). Reuses the full settings load, then
    returns only the whitelisted non-sensitive fields."""
    full = await get_settings(db=db, _=None)
    return {field: getattr(full, field, None) for field in _UI_PREFERENCE_FIELDS}


@router.get("/default-sidebar-order")
async def get_default_sidebar_order(
    db: AsyncSession = Depends(get_db),
):
    """Get the admin-set default sidebar order.

    Intentionally unauthenticated: non-admin users need to read this value to apply
    the default sidebar order, but may lack SETTINGS_READ permission.
    The value is non-sensitive (sidebar item IDs only).
    """
    value = await get_setting(db, "default_sidebar_order")
    return {"default_sidebar_order": value or ""}


@router.get("/check-ffmpeg")
async def check_ffmpeg():
    """Check if ffmpeg is installed and available."""
    from backend.app.services.camera import get_ffmpeg_path

    ffmpeg_path = get_ffmpeg_path()

    return {
        "installed": ffmpeg_path is not None,
        "path": ffmpeg_path,
    }


@router.get("/spoolman")
async def get_spoolman_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequireAnyPermission(Permission.SETTINGS_READ, Permission.INVENTORY_READ),
    creds: RequestCredentials = Depends(request_credentials),
):
    """Get Spoolman integration settings.

    The inventory and printers pages decide by ``spoolman_enabled`` which inventory they show,
    so an inventory reader gets the mode; the server address stays with ``settings:read``.
    """
    return await _spoolman_settings(db, with_address=await creds.allows(_settings_read_gate))


async def _spoolman_settings(db: AsyncSession, *, with_address: bool) -> dict:
    spoolman_enabled = await get_setting(db, "spoolman_enabled") or "false"
    spoolman_url = await get_setting(db, "spoolman_url") or ""
    spoolman_sync_mode = await get_setting(db, "spoolman_sync_mode") or "auto"
    spoolman_disable_weight_sync = await get_setting(db, "spoolman_disable_weight_sync") or "false"
    spoolman_report_partial_usage = await get_setting(db, "spoolman_report_partial_usage") or "true"
    auto_add_unknown_rfid = await get_setting(db, "auto_add_unknown_rfid") or "true"

    return {
        "spoolman_enabled": spoolman_enabled,
        "spoolman_url": spoolman_url if with_address else "",
        "spoolman_sync_mode": spoolman_sync_mode,
        "spoolman_disable_weight_sync": spoolman_disable_weight_sync,
        "spoolman_report_partial_usage": spoolman_report_partial_usage,
        "auto_add_unknown_rfid": auto_add_unknown_rfid,
    }


@router.get("/spoolman/mode-switch-preview")
async def spoolman_mode_switch_preview(
    enable: bool,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """What switching the inventory mode would take away (audit D4, upstream #2812).

    The switch below clears every slot assignment of the mode being left — the
    live tables hold only the active mode, so no reader can be answered by a row
    of the other one. The settings page asks first with this: how many
    assignments go, and which printers are printing now (their filament reaches
    neither inventory). Asking for the mode already on takes nothing.
    """
    from sqlalchemy import func, select

    from backend.app.models.printer import Printer
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
    from backend.app.services.printer_manager import printer_manager

    current = (await get_setting(db, "spoolman_enabled") or "false").lower() == "true"
    if enable == current:
        return {"assignments": 0, "printing": []}
    cleared = SpoolAssignment if enable else SpoolmanSlotAssignment
    assignments = await db.scalar(select(func.count()).select_from(cleared))
    printers = (
        await db.execute(select(Printer.id, Printer.name).where(Printer.archived.is_(False)).order_by(Printer.name))
    ).all()
    return {
        "assignments": int(assignments or 0),
        "printing": [name for printer_id, name in printers if printer_manager.is_print_active(printer_id)],
    }


async def clear_stray_builtin_assignments(db: AsyncSession) -> int:
    """Remove built-in slot assignments an install in Spoolman mode still holds.

    The switch below clears them, so in Spoolman mode the table is empty — except
    on an install where the built-in assign route wrote one while Spoolman was
    enabled but unreachable (closed since; upstream 62fad59fd). Nothing clears
    such a row afterwards and mode-agnostic readers show and use it, so start-up
    removes them once. A setting decides it, hence not a migration.
    """
    from backend.app.models.spool_assignment import SpoolAssignment

    if (await get_setting(db, "spoolman_enabled") or "false").lower() != "true":
        return 0
    result = await db.execute(delete(SpoolAssignment))
    await db.commit()
    removed = int(result.rowcount or 0)
    if removed:
        # What was advertised for those rows goes with them, as on the switch.
        from backend.app.services import ams_advertised_overlay as overlay

        overlay.forget_all()
    return removed


@router.put("/spoolman", dependencies=[RequireAdmin()])
async def update_spoolman_settings(
    settings: dict,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Update Spoolman integration settings.

    The mode and the address are the Spoolman *source* (C047 §C1): their change
    is owned under the source guard, refused while a print is being charged, and
    a new address is refused while any print accounts in Spoolman.
    """
    from backend.app.services import spoolman_source_changes

    values = _spoolman_route_values(settings)
    async with spoolman_source_changes.owning(db, when=spoolman_source_changes.names_source(values)) as change:
        if change.held:
            # The mode and the address as they will be stored (acceptance A13).
            await change.decide(db, enabled=values.get("spoolman_enabled"), url=values.get("spoolman_url"))
        await _write_spoolman_settings(db, values)
        await db.commit()
    db.expire_all()

    spoolman_changed = "spoolman_enabled" in values or "spoolman_url" in values
    # When the integration was toggled on or repointed, import Spoolman's
    # distinct location strings into the local catalog so the new store's
    # locations show up immediately (upstream #1505).
    if spoolman_changed:
        from backend.app.services.location_service import maybe_sync_spoolman_locations

        if await maybe_sync_spoolman_locations(db):
            await db.commit()

    # Return updated settings
    return await _spoolman_settings(db, with_address=True)


_SPOOLMAN_ROUTE_KEYS = (
    "spoolman_enabled",
    "spoolman_url",
    "spoolman_sync_mode",
    "spoolman_disable_weight_sync",
    "spoolman_report_partial_usage",
    "auto_add_unknown_rfid",
)


def _spoolman_route_values(settings: dict) -> dict[str, str]:
    """The route's values in the form they are stored, each validated before any is decided or written.

    Each key takes its type from ``AppSettings``, as in the general save, and a switch is
    stored there as ``"true"`` / ``"false"``. ``SourceChange.decide`` is given exactly what
    ``set_setting`` then writes, so the mode it decides is the mode the row holds: a raw JSON
    ``true`` was decided as Spoolman and stored as ``1``, which every reader takes for the
    built-in inventory (C047 acceptance A13). A null, a value of another type or text no
    database can hold (``storable_text``, A16) is a 422 before the source guard; a key the
    route does not own is ignored, as before.
    """
    values: dict[str, str] = {}
    for key in _SPOOLMAN_ROUTE_KEYS:
        if key not in settings:
            continue
        kind = AppSettings.model_fields[key].annotation
        try:
            value = TypeAdapter(kind).validate_python(settings[key])
        except ValidationError:
            if kind is bool:
                raise HTTPException(422, f"{key}: expected true or false") from None
            raise HTTPException(422, f"{key}: expected a string") from None
        if kind is bool:
            value = "true" if value else "false"
        elif not storable_text(value):
            raise HTTPException(422, f"{key}: contains a character that cannot be stored")
        values[key] = value
    return values


async def _write_spoolman_settings(db: AsyncSession, settings: dict) -> None:
    """The Spoolman settings route's writes, in its transaction (the caller commits)."""
    if "spoolman_enabled" in settings:
        # A switch of the mode empties the ledger it leaves inside ``SourceChange.decide``,
        # for every door that moves the mode (C047 acceptance A08).
        await set_setting(db, "spoolman_enabled", settings["spoolman_enabled"])
    if "spoolman_url" in settings:
        await set_setting(db, "spoolman_url", settings["spoolman_url"])
    if "spoolman_sync_mode" in settings:
        await set_setting(db, "spoolman_sync_mode", settings["spoolman_sync_mode"])
    if "spoolman_disable_weight_sync" in settings:
        await set_setting(db, "spoolman_disable_weight_sync", settings["spoolman_disable_weight_sync"])
    if "spoolman_report_partial_usage" in settings:
        await set_setting(db, "spoolman_report_partial_usage", settings["spoolman_report_partial_usage"])
    if "auto_add_unknown_rfid" in settings:
        await set_setting(db, "auto_add_unknown_rfid", settings["auto_add_unknown_rfid"])


async def get_homeassistant_settings(db: AsyncSession) -> dict:
    """
    Get Home Assistant integration settings.
    Environment variables (HA_URL, HA_TOKEN) take precedence over database settings.
    """
    import os

    # Check environment variables first
    ha_url_env = os.environ.get("HA_URL")
    ha_token_env = os.environ.get("HA_TOKEN")

    # Fall back to database values
    result = await db.execute(select(Settings).where(Settings.key.in_(("ha_url", "ha_token", "ha_enabled"))))
    snapshot = {row.key: row.value for row in result.scalars().all()}
    ha_url = ha_url_env or snapshot.get("ha_url") or ""
    ha_token = ha_token_env or snapshot.get("ha_token") or ""
    ha_enabled_db = snapshot.get("ha_enabled") or "false"

    # Track which settings come from environment
    ha_url_from_env = bool(ha_url_env)
    ha_token_from_env = bool(ha_token_env)
    ha_env_managed = ha_url_from_env and ha_token_from_env

    # Auto-enable when both env vars are set, otherwise use database value
    if ha_url_env and ha_token_env:
        ha_enabled = True
    else:
        ha_enabled = ha_enabled_db.lower() == "true"

    return {
        "ha_enabled": ha_enabled,
        "ha_url": ha_url,
        "ha_token": ha_token,
        "ha_url_from_env": ha_url_from_env,
        "ha_token_from_env": ha_token_from_env,
        "ha_env_managed": ha_env_managed,
    }


PICTURE_PAUSE_DRAIN = 30.0  # an attach is one short transaction and one PNG copy


async def _stage_files_and_pictures(data_dir: Path, staging: Path) -> None:
    """Every managed directory strictly, the library's pictures delegated; then those pictures, last and strictly,
    under a short pause of the library preview publications (Codex A-R5). The database snapshot was taken before:
    a picture it names is never deleted by an attach, so it is among those copied."""
    from backend.app.core.db_portable import _file_work
    from backend.app.services import backup_files, library_preview

    await _file_work(backup_files.stage_files, app_settings, data_dir, staging)
    async with library_preview.publication.pause(PICTURE_PAUSE_DRAIN):
        await _file_work(backup_files.stage_library_thumbnails, Path(app_settings.library_dir), staging / "library")


async def create_backup_zip(output_path: Path | None = None) -> tuple[Path, str]:
    """Portable DB plus verified files; manual and scheduled backups share a lock."""
    from backend.app.services.backup_files import exclusive_operation

    async with exclusive_operation():
        return await _create_backup_zip(output_path)


async def _create_backup_zip(output_path: Path | None) -> tuple[Path, str]:
    import tempfile

    from backend.app.core.database import Base, engine
    from backend.app.core.db_portable import _file_work, dump_to_sqlite
    from backend.app.core.paths import resolve_data_dir
    from backend.app.services import queue_sources
    from backend.app.services.backup_files import (
        stage_queue_spool,
        stage_zigbee_db,
        staging_directory,
        write_manifest,
        write_zip,
    )
    from backend.app.services.obico_history import obico_history
    from backend.app.services.obico_history_files import stage_history

    filename = f"bamdude-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    zip_file = None
    try:
        async with staging_directory() as staging:
            # The pin starts before the DB export and remains until every referenced
            # object was copied. GC therefore cannot unlink a row the portable
            # snapshot still names while this backup is being assembled.
            async with queue_sources.pin_backup(), obico_history.pin_backup():
                await dump_to_sqlite(engine, Base.metadata, staging / "bamdude.db")
                data_dir = resolve_data_dir()
                await _stage_files_and_pictures(data_dir, staging)
                await _file_work(stage_zigbee_db, data_dir, staging)
                await _file_work(stage_queue_spool, data_dir, staging, staging / "bamdude.db")
                await _file_work(stage_history, data_dir, staging, staging / "bamdude.db")
                await _file_work(write_manifest, staging)
            if output_path is not None:
                zip_file = (
                    output_path / filename
                )  # SEC-PATH-OK: trusted output directory and server-generated filename.
            else:
                fd, temporary = tempfile.mkstemp(suffix=".zip")
                os.close(fd)
                zip_file = Path(temporary)
            await _file_work(write_zip, staging, zip_file)
            return zip_file, filename
    except BaseException:
        # Cancellation while staging is being removed can still prevent the
        # response from taking ownership of a successfully written temp ZIP.
        if output_path is None and zip_file is not None:
            await _file_work(zip_file.unlink, missing_ok=True)
        raise


async def _remove_backup_download(path: Path) -> None:
    from backend.app.core.db_portable import _file_work

    with CancelScope(shield=True):
        try:
            await _file_work(path.unlink, missing_ok=True)
        except OSError:
            logger.exception("Could not remove the temporary backup download")


class _BackupDeliveryStopped(Exception):
    """Unwind FileResponse's disconnect listener as well as its sender."""


class _BackupZipResponse(FileResponse):
    """Own the temporary ZIP until its reader has closed."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        extensions = scope.get("extensions", {})
        if "http.response.pathsend" in extensions:
            # A server may defer reading a delegated path until after send returns.
            local_extensions = {key: value for key, value in extensions.items() if key != "http.response.pathsend"}
            scope = {**scope, "extensions": local_extensions}

        stopping = False
        send_scope: CancelScope | None = None

        async def send_chunk(message: Message) -> None:
            nonlocal send_scope
            if stopping:
                raise _BackupDeliveryStopped
            with CancelScope() as active_send:
                send_scope = active_send
                await send(message)
            if active_send.cancel_called:
                raise _BackupDeliveryStopped

        # Interrupt the transport only. A cancelled open, read or close can
        # still hold a file handle in a worker thread when Windows tries unlink.
        delivery = asyncio.create_task(super().__call__(scope, receive, send_chunk))
        try:
            await asyncio.shield(delivery)
        except asyncio.CancelledError:
            stopping = True
            if send_scope is not None:
                send_scope.cancel()
            with CancelScope(shield=True):
                joined = asyncio.gather(delivery, return_exceptions=True)
                while not joined.done():
                    try:
                        await asyncio.shield(joined)
                    except asyncio.CancelledError:
                        pass
            raise
        finally:
            await _remove_backup_download(Path(self.path))


@router.get("/backup", dependencies=[RequireAdmin()])
async def create_backup(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_BACKUP),
):
    """Create a complete backup (database + all files) as a ZIP.

    Streams from a temp file rather than loading the whole ZIP into memory,
    so multi-gigabyte backups don't OOM the process.
    """
    from backend.app.core.db_portable import _file_work
    from backend.app.core.schema_portable_residuals import zip_warning_count
    from backend.app.services.backup_files import BackupBusyError

    try:
        zip_file, filename = await create_backup_zip()
        try:
            count = await _file_work(zip_warning_count, zip_file)
            return _BackupZipResponse(
                path=zip_file,
                media_type="application/zip",
                filename=filename,
                headers={"X-BamDude-Schema-Residual-Objects": str(count)},
            )
        except BaseException:
            await _remove_backup_download(zip_file)
            raise
    except BackupBusyError as exc:
        raise HTTPException(409, "A backup or restore is already running") from exc
    except Exception as e:
        logger.error("Backup failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Backup failed. Check server logs for details."},
        )


@router.post("/restore", dependencies=[RequireAdmin()])
async def restore_backup(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_RESTORE),
    request: Request = None,
    accept_calibration_residual: bool = Form(False),
    calibration_receipt: str | None = Form(None),
):
    """Validate and stage all files, then replace files and the portable database."""
    from backend.app.services.backup_files import BackupBusyError, exclusive_operation

    # HTTP always supplies Request. Trusted internal/test callers retain the
    # existing helper contract, without pretending to possess a browser grant.
    authority = None
    if request is not None:
        from backend.app.core.auth import capture_request_authority

        authority = await capture_request_authority(request)

    try:
        async with exclusive_operation():
            return await _restore_backup(
                file,
                db,
                authority=authority,
                accept_calibration_residual=accept_calibration_residual is True,
                calibration_receipt=calibration_receipt if isinstance(calibration_receipt, str) else None,
            )
    except BackupBusyError as exc:
        raise HTTPException(409, "A backup or restore is already running") from exc


async def _quiesce_part_render() -> None:
    """Spec §9.6 and consilium E3-R4: the part render queue is stopped and its last attempt proven over, or the
    restore ends here -- before library preview/storage work and database replacement."""
    from backend.app.services import part_render_scheduler

    if not await part_render_scheduler.stop_part_render_scheduler("restore"):
        raise HTTPException(
            409,
            "The part thumbnail worker could not prove its last attempt ended. Restart BamDude, then restore.",
        )


async def _quiesce_library_previews() -> None:
    """STEP spec §4.3: every library preview writer -- the STEP jobs, a handed-off attach, the legacy STL/OBJ
    and sliced-source publications -- is closed and proven quiet before the storage guard and any file work,
    or the restore ends here with nothing replaced."""
    from backend.app.services import library_preview_jobs
    from backend.app.services.library_file_runtime import upload_writes

    if not await upload_writes.quiesce():
        raise HTTPException(409, "Library upload writers have not stopped; retry restore after they finish")
    if not await library_preview_jobs.quiesce_library_previews_for_restore():
        raise HTTPException(409, {"error": "preview_quiesce_unproven", "message": PREVIEW_QUIESCE_UNPROVEN})


PREVIEW_QUIESCE_UNPROVEN = "Library previews could not prove they stopped. Restart BamDude, then restore."


async def _restore_backup(
    file: UploadFile,
    db: AsyncSession,
    *,
    authority=None,
    accept_calibration_residual=False,
    calibration_receipt=None,
):
    import asyncio
    from contextlib import AsyncExitStack

    from backend.app.core.database import Base, close_all_connections, init_db, reinitialize_database
    from backend.app.core.db_dialect import is_sqlite
    from backend.app.core.db_portable import (
        _file_work,
        _snapshot_sqlite,
        import_sqlite_to_postgres,
        prepared_import_source,
        validate_sqlite_backup,
    )
    from backend.app.core.paths import resolve_data_dir
    from backend.app.core.schema_source_indexes import (
        SOURCE_INDEX_MESSAGE,
        SourceIndexShapeError,
        validate_source_indexes,
    )
    from backend.app.core.sqlite_import_preflight import (
        SOURCE_COLLISION_MESSAGE,
        SOURCE_VALUES_MESSAGE,
        SqliteSourceCollisionError,
        SqliteSourceValuesError,
    )
    from backend.app.core.sqlite_import_source import (
        SOURCE_LEVEL_MESSAGE,
        SOURCE_READ_MESSAGE,
        SqliteSourceLevelError,
        SqliteSourceReadError,
    )
    from backend.app.services import queue_sources
    from backend.app.services.backup_files import (
        FileRestore,
        extract_zip,
        normalize_managed_restore_paths,
        staging_directory,
        validate_staged_queue_spool,
    )
    from backend.app.services.connector_runtime import connector_runtime
    from backend.app.services.farm_startup_guard import farm_startup_guard

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Invalid backup file: must be a .zip file")

    async with AsyncExitStack() as source_stack:
        staging = await source_stack.enter_async_context(staging_directory())
        # UploadFile is already spooled: don't duplicate a multi-GB ZIP in RAM.
        await file.seek(0)
        try:
            await _file_work(extract_zip, file.file, staging)
        except zipfile.BadZipFile as exc:
            raise HTTPException(400, "Invalid backup file: not a valid ZIP") from exc
        except ValueError as exc:
            logger.warning("Restore preflight rejected ZIP: %s", exc)
            raise HTTPException(
                400, "Invalid backup files: unsafe paths or failed integrity check. Check server logs."
            ) from exc

        backup_db = staging / "bamdude.db"
        if not backup_db.exists():
            backup_db = staging / "bambuddy.db"
        if not backup_db.exists():
            raise HTTPException(400, "Invalid backup: missing database file (bamdude.db or bambuddy.db)")
        try:
            prepared = await source_stack.enter_async_context(
                prepared_import_source(backup_db, Base.metadata, postgres=not is_sqlite())
            )
            checked_db = prepared.path
            await _file_work(validate_sqlite_backup, checked_db, require_app_schema=True)
            if is_sqlite():
                await _file_work(validate_source_indexes, checked_db)
            zigbee_db = staging / "zigbee/zigbee.db"
            if zigbee_db.exists():
                await _file_work(validate_sqlite_backup, zigbee_db)
            await _file_work(validate_staged_queue_spool, staging, checked_db)
            from backend.app.services.obico_history_files import validate_history

            await _file_work(validate_history, staging, checked_db)
            await _file_work(normalize_managed_restore_paths, checked_db, staging, app_settings)
        except SqliteSourceLevelError as exc:
            raise HTTPException(400, {"error": exc.code, "message": SOURCE_LEVEL_MESSAGE}) from exc
        except SqliteSourceReadError as exc:
            raise HTTPException(400, {"error": exc.code, "message": SOURCE_READ_MESSAGE}) from exc
        except SqliteSourceValuesError as exc:
            raise HTTPException(
                400,
                {
                    "error": exc.code,
                    "message": SOURCE_VALUES_MESSAGE,
                    "violations": exc.violations[:20],
                    "violation_fields": len(exc.violations),
                },
            ) from exc
        except SqliteSourceCollisionError as exc:
            raise HTTPException(400, {"error": exc.code, "message": SOURCE_COLLISION_MESSAGE}) from exc
        except SourceIndexShapeError as exc:
            raise HTTPException(400, {"error": exc.code, "message": SOURCE_INDEX_MESSAGE}) from exc
        except ValueError as exc:
            logger.warning("Restore preflight rejected database: %s", exc)
            raise HTTPException(400, "Invalid backup database: damaged or incompatible. Check server logs.") from exc

        # This decision precedes ANY hold/drain. The source metadata cannot
        # authorize its own missing active-calibration UNIQUE.
        from backend.app.core.schema_restore_consent import (
            CONFIRMATION_MESSAGE,
            binding,
            calibration_groups,
            receipts,
            zip_digest,
        )
        from backend.app.services.connector_submissions import read_server_identity

        acceptance = None
        count = await _file_work(calibration_groups, prepared.path)
        if accept_calibration_residual is True and not count:
            raise HTTPException(
                400,
                {
                    "error": "calibration_restore_confirmation_invalid",
                    "message": "Restore confirmation expired or no longer matches. Select the backup again.",
                },
            )
        if count:
            target = (*await read_server_identity(db), farm_startup_guard.boot_id())
            source_hash = await _file_work(zip_digest, file.file)
            expected = binding(authority, source_hash, prepared, target, count)
            if accept_calibration_residual is True:
                acceptance = receipts.consume(calibration_receipt, expected)
                if acceptance is None:
                    raise HTTPException(
                        400,
                        {
                            "error": "calibration_restore_confirmation_invalid",
                            "message": "Restore confirmation expired or no longer matches. Select the backup again.",
                        },
                    )
            else:
                receipt = receipts.issue(expected) if authority is not None and all(target) else None
                raise HTTPException(
                    400,
                    {
                        "error": "calibration_restore_confirmation_required",
                        "message": CONFIRMATION_MESSAGE,
                        "duplicate_groups": count,
                        "receipt": receipt,
                    },
                )

        if is_sqlite():
            from sqlalchemy.ext.asyncio import create_async_engine

            from backend.app.core.schema_repair_state import write_pending

            image = create_async_engine(f"sqlite+aiosqlite:///{prepared.path}")
            try:
                async with image.begin() as conn:
                    await write_pending(conn, Base.metadata, acceptance=acceptance)
            finally:
                await image.dispose()

        try:
            files = FileRestore(staging, app_settings, resolve_data_dir())
            try:
                # Quiesce file owners before staging Zigbee and moving old files.
                # Shutdown failure is fatal; a delay cannot prove a writer stopped.
                from backend.app.core.auth_runtime import auth_runtime
                from backend.app.services import library_scan
                from backend.app.services.auto_queue_return_runtime import auto_queue_return_runtime
                from backend.app.services.background_dispatch import background_dispatch
                from backend.app.services.cloud_credentials import close_admission as close_cloud_admission
                from backend.app.services.credential_rollout import credential_rollout
                from backend.app.services.git_backup import git_backup_service
                from backend.app.services.notification_service import notification_service
                from backend.app.services.print_scheduler import scheduler as print_scheduler
                from backend.app.services.slice_dispatch import slice_dispatch
                from backend.app.services.smart_plug_manager import smart_plug_manager
                from backend.app.services.virtual_printer import virtual_printer_manager
                from backend.app.services.zigbee.coordinator import zigbee_coordinator
                from backend.app.services.zigbee.poller import zigbee_poller
                from backend.app.services.zigbee.supervisor import zigbee_supervisor

                # Close all admission before yielding to any shutdown/drain.
                # A failed drain refuses replacement and stays held until restart.
                auth_runtime.close_admission()
                close_cloud_admission()
                slice_dispatch.close_admission()
                library_scan.close_admission()
                auto_queue_return_runtime.close_admission()
                # Spoolman deletes AND print charges share one gate and one drain (C047 S35).
                from backend.app.services.spoolman_delete import delete_runtime as spoolman_delete_runtime

                spoolman_delete_runtime.close_admission()
                git_backup_service.close_admission()
                farm_startup_guard.hold("restore")
                from backend.app.services.farm_startup_guard import notify_change

                await notify_change()
                # Accepted VP workflows still need the owners below to finish
                # library and queue publication. Keep G01 admission closed, but
                # prove VP completion before any asynchronous quiesce joins.
                from backend.app.services.virtual_printer.lifecycle import VPDrainRefused, drain_warning

                await db.rollback()
                try:
                    vp_drain = await virtual_printer_manager.drain_for_restore()
                except VPDrainRefused as exc:
                    raise HTTPException(409, str(exc)) from exc
                if not await spoolman_delete_runtime.quiesce_for_restore():
                    raise HTTPException(409, "Spoolman delete or charge work has not stopped; restart before restoring")
                if not await auto_queue_return_runtime.quiesce_for_restore():
                    raise HTTPException(409, "AutoQueue return work has not stopped; restart before restoring")
                if not await git_backup_service.quiesce_for_restore():
                    raise HTTPException(409, "Git backup work has not stopped; restart before restoring")
                if not await slice_dispatch.quiesce_for_restore():
                    raise HTTPException(409, "Slicing work has not stopped; restart before restoring")
                from backend.app.services.assembly.runtime import assembly_quiesced

                # an assembly job is a slice-dispatch job, so it was cancelled just above; what is asked here is
                # whether its packing child was PROVEN gone before storage moves
                if not assembly_quiesced():
                    raise HTTPException(409, "Project assembly work has not stopped; restart before restoring")
                if not await library_scan.quiesce_for_restore():
                    raise HTTPException(409, "Library scan work has not stopped; restart before restoring")
                if not await credential_rollout.quiesce_for_restore(10):
                    raise HTTPException(409, "Credential rollout work has not stopped; restart before restoring")
                if not await auth_runtime.quiesce_for_restore():
                    raise HTTPException(409, "Authentication work has not stopped; restart before restoring")
                await _quiesce_part_render()
                await _quiesce_library_previews()
                from backend.app.mcp_server.runtime import mcp_runtime

                if not await mcp_runtime.quiesce_for_restore():
                    raise HTTPException(409, "MCP readers have not stopped; retry restore after they finish")
                # Admission closes before the first await; submissions and uploads drain before any storage moves.
                if not await connector_runtime.quiesce_for_restore():
                    raise HTTPException(409, "Slicer connector work has not stopped; retry restore after it finishes")
                from backend.app.services.obico_detection import obico_detection_service

                try:
                    obico_stopped = await obico_detection_service.quiesce()
                except Exception:
                    logger.exception("Obico shutdown failed before restore")
                    obico_stopped = False
                if not obico_stopped:
                    raise HTTPException(
                        409, "Obico readers and writers have not stopped; retry restore after they finish"
                    )
                # Download jobs read printers and write the scratch volume (G10 D): admission
                # closes, every job is stopped and its worker's end proven, and what was
                # prepared is dropped -- it belongs to the storage being replaced.
                from backend.app.services.media_downloads import media_download_runtime

                if not await media_download_runtime.quiesce():
                    raise HTTPException(409, "Downloads have not stopped; retry restore after they finish")
                # Printer file transfers own sessions and local files until their threads end
                # (G10 A): admission closes first, then every operation is asked to stop and
                # its end is proven, or the restore is refused.
                from backend.app.services.printer_file_lanes import printer_file_runtime

                if not await printer_file_runtime.quiesce():
                    raise HTTPException(409, "Printer file transfers have not stopped; retry restore after they finish")
                # A layer recording writes frames and its stitch reads them and publishes through
                # the seam below (G10 F): capture closes, a running stitch is stopped with its
                # frames retained, and the end of every writer and encoder is proven.
                from backend.app.services import layer_timelapse

                if not await layer_timelapse.quiesce():
                    raise HTTPException(409, "Layer timelapse work has not stopped; retry restore after it finishes")
                # An edit's encoder writes beside the archive's video and publishes through the
                # seam below (G10 E): admission closes, every job is stopped, and the end of
                # each encoder is PROVEN -- an unreaped ffmpeg refuses the restore.
                from backend.app.services.timelapse_editor import timelapse_edit_runtime

                if not await timelapse_edit_runtime.quiesce():
                    raise HTTPException(409, "Video edits have not stopped; retry restore after they finish")
                # Attaching, converting or removing an archive's video writes the files and
                # rows this restore replaces (G10 C4): admission closes, and every such
                # operation is waited for to its real end, or the restore is refused.
                from backend.app.services.archive_media_runtime import archive_media_runtime

                if not await archive_media_runtime.quiesce():
                    raise HTTPException(409, "Archive video work has not stopped; retry restore after it finishes")
                print_scheduler.stop()
                await smart_plug_manager.stop_scheduler()
                # Nothing buffered belongs to the storage that replaces this one, and no
                # energy writer may estimate from it (spec plug-meter-watchdog W2, W10).
                from backend.app.services.measurement_buffer import measurement_buffer

                measurement_buffer.retire()
                notification_service.stop_digest_scheduler()
                await background_dispatch.stop()
                await zigbee_supervisor.stop()
                await zigbee_poller.stop()
                await zigbee_coordinator.stop(strict=True)
                # Let the cancelled timer iterations release their DB sessions.
                await asyncio.sleep(1)

                # Own the transaction BEFORE entering a worker: cancellation waits
                # for the worker, then finally can undo every completed move.
                # A restore changes both the database references and the spool
                # tree. The same guard used by capture/publication prevents a
                # dispatcher from observing half of that change; receipts that
                # started before the swap are invalidated only after all file
                # staging has succeeded.
                async with queue_sources.storage_mutation():
                    if authority is not None:
                        from backend.app.core.auth_lifetime import validate_restore_actor

                        # All previous auth work has drained. This short read
                        # is owned by restore while ordinary admission remains
                        # closed; release its guard before filesystem I/O.
                        await validate_restore_actor(db, authority)
                        if acceptance is not None:
                            target_now = (*await read_server_identity(db), farm_startup_guard.boot_id())
                            if list(target_now) != acceptance["target"]:
                                raise HTTPException(400, "Restore confirmation no longer matches this installation")
                        await db.rollback()
                    await _file_work(files.prepare)
                    queue_sources.invalidate_receipts()
                    # Durable, outside the storage being replaced: until the restored database has a new
                    # storage epoch, no start may answer the connector's old namespace (spec r2 §8.4).
                    await _file_work(connector_runtime.write_restore_fence)
                    await db.close()
                    await close_all_connections()
                    await _file_work(files.apply)

                    logger.info("Restoring database from backup...")
                    if is_sqlite():
                        db_path = Path(app_settings.database_url.replace("sqlite+aiosqlite:///", ""))

                        def replace_sqlite():
                            # Online backup writes through the live DB's WAL. Mark
                            # commit inside the worker even if its await is cancelled.
                            prepared.assert_unchanged()
                            _snapshot_sqlite(prepared.path, db_path)
                            files.committed = True

                        await _file_work(replace_sqlite)
                    else:
                        from backend.app.core.database import engine

                        await import_sqlite_to_postgres(engine, Base.metadata, prepared, acceptance=acceptance)
                        files.committed = True  # No await between DB success and this flag.
            finally:
                await _file_work(files.finish)
                if not files.committed:
                    # Nothing was replaced: the old storage and its storage epoch stand.
                    await _file_work(connector_runtime.clear_restore_fence)

            import backend.app.core.encryption as _enc_mod

            _enc_mod._fernet_instance = None
            _enc_mod._key_source = None
            _enc_mod._warn_shown = False
            # Later migrations operate on matching new files + DB. A migration
            # failure must not put old files beside the already replaced database.
            await reinitialize_database()
            await init_db()
            # The restored database's epoch is new before anything reopens; a failure keeps the fence for the
            # next start. The connector itself stays closed until that restart.
            if not await connector_runtime.finish_restore():
                raise HTTPException(503, "Restore finalization is pending; restart BamDude before resuming work")
            await mcp_runtime.reload(after_restore=True)
            logger.info("Restore complete - restart required")
            from backend.app.i18n.api_errors import current_language, translate_sentence

            return {
                "success": True,
                "message": (
                    translate_sentence(
                        "Backup restored successfully. Please restart BamDude for changes to take effect.",
                        current_language(),
                    )
                    + " "
                    + drain_warning(vp_drain)
                ).strip(),
            }
        except HTTPException:
            raise
        except SchemaRepairPending:
            from backend.app.core import schema_repair_maintenance

            schema_repair_maintenance.active = True
            return json_error(
                503,
                {
                    "error": "schema_repair_pending",
                    "message": "Database schema repair is pending. Stop BamDude and run schema_repair retry.",
                },
            )
        except Exception:
            logger.exception("Restore failed; restart BamDude before resuming work")
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Restore failed. Check server logs for details."},
            )


@router.post("/optimize-db")
async def optimize_database(
    _: User | None = RequirePermission(Permission.SETTINGS_BACKUP),
):
    """Run the maintenance the active backend actually supports.

    ⚠️ This used to run ANALYZE, then ``PRAGMA wal_checkpoint(TRUNCATE)``, then
    VACUUM with no dialect branch — so on **PostgreSQL the PRAGMA is a syntax
    error** and the button had never done anything but return a failure there.
    It also derived a file path by stripping the SQLite URL prefix, which on any
    other URL produces nonsense.

    On PostgreSQL only ANALYZE is run: VACUUM cannot execute inside a
    transaction block, and reclaiming space is autovacuum's job. On SQLite all
    three still run — measured 2026-09-08, they do work through the async
    engine.
    """
    from sqlalchemy import text

    from backend.app.core.database import engine
    from backend.app.core.db_dialect import is_postgres
    from backend.app.services import db_health

    postgres = is_postgres()
    try:
        async with engine.connect() as conn:
            # Update query planner statistics — valid on both backends.
            await conn.execute(text("ANALYZE"))
            if not postgres:
                # Flush WAL journal to main database file
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
                # Rebuild and compact database (defragmentation)
                await conn.execute(text("VACUUM"))
            await conn.commit()

            # Sizes off the SAME connection: opening a second session here
            # would be one more thing that can fail after the work succeeded.
            if postgres:
                db_size = int((await conn.execute(text("SELECT pg_database_size(current_database())"))).scalar() or 0)
                wal_size = 0
            else:
                main, wal, shm = db_health._sqlite_files()
                db_size = sum(db_health._size_of(p) for p in (main, wal, shm))
                wal_size = db_health._size_of(wal)

        return {
            "success": True,
            "mode": "postgresql" if postgres else "sqlite",
            "message": (
                "Statistics updated (ANALYZE). PostgreSQL reclaims space with autovacuum."
                if postgres
                else "Database optimized successfully"
            ),
            "db_size": db_size,
            "wal_size": wal_size,
        }
    except Exception as e:
        logger.error("Database optimization failed: %s", e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Optimization failed: {e}"},
        )


@router.get("/network-interfaces")
async def get_network_interfaces(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get available network interfaces with all IPs (primary + aliases)."""
    from backend.app.services.network_utils import get_all_interface_ips

    interfaces = get_all_interface_ips()
    return {"interfaces": interfaces}


@router.get("/virtual-printer/models")
async def get_virtual_printer_models(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get available virtual printer models."""
    from backend.app.services.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL,
        VIRTUAL_PRINTER_MODELS,
    )

    return {
        "models": VIRTUAL_PRINTER_MODELS,
        "default": DEFAULT_VIRTUAL_PRINTER_MODEL,
    }


@router.get("/virtual-printer")
async def get_virtual_printer_settings(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get virtual printer settings and status."""
    from backend.app.services.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL,
        virtual_printer_manager,
    )

    enabled = await get_setting(db, "virtual_printer_enabled")
    access_code = await get_setting(db, "virtual_printer_access_code")
    mode = await get_setting(db, "virtual_printer_mode")
    model = await get_setting(db, "virtual_printer_model")
    target_printer_id = await get_setting(db, "virtual_printer_target_printer_id")
    remote_interface_ip = await get_setting(db, "virtual_printer_remote_interface_ip")
    archive_name_source = await get_setting(db, "virtual_printer_archive_name_source")

    return {
        "enabled": enabled == "true" if enabled else False,
        "access_code_set": bool(access_code),
        "mode": mode or "file_manager",
        "model": model or DEFAULT_VIRTUAL_PRINTER_MODEL,
        "target_printer_id": int(target_printer_id) if target_printer_id else None,
        "remote_interface_ip": remote_interface_ip or "",
        "archive_name_source": archive_name_source if archive_name_source in ("metadata", "filename") else "metadata",
        "status": virtual_printer_manager.get_status(),
    }


class GlobalVirtualPrinterUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool | None = None
    access_code: str | None = None
    mode: str | None = None
    model: str | None = None
    target_printer_id: int | None = None
    remote_interface_ip: str | None = None
    archive_name_source: str | None = None
    secret_actions: dict[str, Literal["keep", "clear", "replace"]] | None = None


@router.put("/virtual-printer", dependencies=[RequireAdmin()])
async def update_virtual_printer_settings(
    payload: GlobalVirtualPrinterUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Set the one installation-wide Virtual Printer setting: where an archive takes its name from.

    This route once configured THE virtual printer. Each virtual printer has had its own row and its
    own route (``PUT /virtual-printers/{id}``) for a long time, and nothing reads the old keys any
    more -- yet the route went on accepting ``enabled`` / ``access_code`` / ``mode`` / ``model`` /
    ``target_printer_id`` / ``remote_interface_ip``, stored them and answered 200 as if a virtual
    printer had changed. It now refuses them and says where they are set. The stored old values are
    neither read nor judged here, so they cannot block the setting this route still owns.
    """
    if "access_code" in request.query_params:
        raise HTTPException(422, "Credentials must be submitted in the JSON body")
    named = payload.model_fields_set - {"archive_name_source"}
    if named:
        return json_error(
            400,
            "This setting belongs to each virtual printer now. Change it on the Virtual Printers page.",
        )

    archive_name_source = payload.archive_name_source
    # Validate archive_name_source (B.14 / #1152): only two values are
    # meaningful — anything else would silently fall back to 'metadata' on
    # the read side and confuse the user about whether their toggle stuck.
    if archive_name_source is not None and archive_name_source not in ("metadata", "filename"):
        return json_error(400, "archive_name_source must be 'metadata' or 'filename'")
    if archive_name_source is not None:
        await set_setting(db, "virtual_printer_archive_name_source", archive_name_source)
        await db.commit()
        db.expire_all()

    return await get_virtual_printer_settings(db)


# =============================================================================
# MQTT Relay Settings
# =============================================================================


@router.get("/mqtt/status")
async def get_mqtt_status(
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get MQTT relay connection status."""
    from backend.app.services.mqtt_relay import mqtt_relay

    return mqtt_relay.get_status()
