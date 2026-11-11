import asyncio
import logging
import math
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import query_timing
from backend.app.core.auth import RequireAdmin, RequirePermission, get_current_user_optional, require_energy_cost_update
from backend.app.core.config import settings as app_settings
from backend.app.core.credential_config import canonical_url, lock_credential_config, merge_secret_fields
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
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
)

logger = logging.getLogger(__name__)

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


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    """Set a single setting value (dialect-aware upsert)."""
    from backend.app.core.db_dialect import upsert_setting

    await upsert_setting(db, Settings, key, value)


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
            # Parse the value based on the expected type
            if setting.key in [
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
                "ha_enabled",
                "per_printer_mapping_expanded",
                "prometheus_enabled",
                "user_notifications_enabled",
                "prefer_lowest_filament",
                "queue_shortest_first",
                "forecast_operator_absence_enabled",
                "auto_queue_rebalance_models",
                "auto_queue_compatible_models",
                "auto_order_for_batches",
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
                settings_dict[setting.key] = setting.value.lower() == "true"
            elif setting.key in ["runout_zero_point_enabled", "ams_sync_bidirectional"]:
                # On unless the row says "false" — exactly how usage_tracker and
                # main.on_ams_change read these two, so a stored "None" (a PUT
                # with null) neither 500s here nor reads off while acting on.
                settings_dict[setting.key] = setting.value.lower() != "false"
            elif setting.key in [
                "default_filament_cost",
                "energy_cost_per_kwh",
                "ams_temp_good",
                "ams_temp_fair",
                "library_disk_warning_gb",
                "low_stock_threshold",
            ]:
                settings_dict[setting.key] = float(setting.value)
            elif setting.key == "ams_temp_alarm":
                # Nullable float. Storage stringifies None to "None", so it cannot
                # join the list above — float("None") would take the whole
                # settings response down with it (upstream #2905). Garbage reads
                # as unset, the same fallback the alarm itself applies — a
                # non-finite value too, which JSON could not carry anyway.
                try:
                    value = float(setting.value)
                except (TypeError, ValueError):
                    value = None
                settings_dict[setting.key] = value if value is not None and math.isfinite(value) else None
            elif setting.key in [
                "ams_humidity_good",
                "ams_humidity_fair",
                "ams_history_retention_days",
                "inbox_retention_days",
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
                "location_sensor_poll_interval",
                "forecast_upload_seconds",
                "forecast_plate_clear_minutes",
                "firmware_batch_concurrency",
                "session_max_hours",
                "connector_max_upload_bytes",
            ]:
                settings_dict[setting.key] = int(setting.value)
            elif setting.key == "default_printer_id":
                # Handle nullable integer
                settings_dict[setting.key] = int(setting.value) if setting.value and setting.value != "None" else None
            elif setting.key == "open_in_slicer":
                # None means "inherit from preferred_slicer" (#1329). The PUT path
                # serializes None as the literal string "None"; strip it back so
                # the frontend sees a true null and falls back as intended.
                settings_dict[setting.key] = setting.value if setting.value and setting.value != "None" else None
            elif setting.key in {"mcp_allowed_hosts", "mcp_allowed_origins"}:
                import json

                settings_dict[setting.key] = json.loads(setting.value)
            else:
                settings_dict[setting.key] = setting.value

    # Get Home Assistant settings (with environment variable overrides)
    if effective_env:
        settings_dict.update(await get_homeassistant_settings(db))

    return AppSettings(**settings_dict)


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
    try:
        AppSettings(**merged)
    except ValidationError:
        raise HTTPException(422, "Invalid effective settings configuration") from None
    return merged


@router.put("/", response_model=AppSettingsRead)
async def update_settings(
    settings_update: AppSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Update application settings."""
    update_data = settings_update.model_dump(exclude_unset=True)
    actions = update_data.pop("secret_actions", None)
    await lock_credential_config(db)
    if actions and not (current_user is not None and current_user.is_admin):
        raise HTTPException(403, "Requires admin role for protected settings")
    await _enforce_admin_settings(db, update_data, current_user)
    merged = await resolve_settings_credentials(db, update_data, actions)
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
    }
    mqtt_updated = bool(mqtt_keys & set(update_data.keys()))

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

    await db.commit()
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
    await set_setting(db, "energy_cost_per_kwh", str(payload.energy_cost_per_kwh))
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
    """Reset all settings to defaults."""
    from backend.app.core.credential_config import CONFIG_GUARD_KEY

    await lock_credential_config(db)
    await _lock_mcp_http_policy(db)
    # The installation transaction anchor is infrastructure, not a preference.
    # Keep it locked and present across reset; a later editor must never lazily
    # create its own independent serialization row.
    result = await db.execute(select(Settings).where(Settings.key != CONFIG_GUARD_KEY).order_by(Settings.key))
    for setting in result.scalars().all():
        await db.delete(setting)

    await db.commit()

    await _apply_mqtt_runtime(db)
    return await get_settings(db=db, _=None)


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
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Get Spoolman integration settings."""
    spoolman_enabled = await get_setting(db, "spoolman_enabled") or "false"
    spoolman_url = await get_setting(db, "spoolman_url") or ""
    spoolman_sync_mode = await get_setting(db, "spoolman_sync_mode") or "auto"
    spoolman_disable_weight_sync = await get_setting(db, "spoolman_disable_weight_sync") or "false"
    spoolman_report_partial_usage = await get_setting(db, "spoolman_report_partial_usage") or "true"
    auto_add_unknown_rfid = await get_setting(db, "auto_add_unknown_rfid") or "true"

    return {
        "spoolman_enabled": spoolman_enabled,
        "spoolman_url": spoolman_url,
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


@router.put("/spoolman", dependencies=[RequireAdmin()])
async def update_spoolman_settings(
    settings: dict,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    """Update Spoolman integration settings."""
    if "spoolman_enabled" in settings:
        old_val = await get_setting(db, "spoolman_enabled") or "false"
        new_val = settings["spoolman_enabled"]
        await set_setting(db, "spoolman_enabled", new_val)

        # Switching to Spoolman: clear built-in inventory slot assignments
        if old_val.lower() != "true" and new_val.lower() == "true":
            from backend.app.models.spool_assignment import SpoolAssignment
            from backend.app.services import ams_advertised_overlay as overlay

            result = await db.execute(delete(SpoolAssignment))
            # Every overlay entry was born of an assignment row; with the whole
            # registry gone, so is every spool the masks stood for. Cleared
            # BEFORE this handler's commit, like every ``remember`` on the
            # assignment routes: an in-memory store that forgot too early is
            # inert (the rebuild refills it from the registries), while one that
            # still claims a spool whose row is gone would answer routing with
            # an inventory item nobody can see.
            overlay.forget_all()
            logger.info("Cleared %d spool assignments on switch to Spoolman mode", result.rowcount)
        # Switching back to internal mode: clear Spoolman slot assignments — the
        # symmetric counterpart of the clear above. Without this, stale
        # spoolman_slot_assignments rows linger and would wrongly count as
        # "assigned" in any mode-agnostic check (e.g. the missing-spool-
        # assignment notification, which unions both tables — #1473).
        elif old_val.lower() == "true" and new_val.lower() != "true":
            from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
            from backend.app.services import ams_advertised_overlay as overlay

            result = await db.execute(delete(SpoolmanSlotAssignment))
            overlay.forget_all()  # same reasoning as the switch above
            logger.info("Cleared %d Spoolman slot assignments on switch to internal mode", result.rowcount)
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

    spoolman_changed = "spoolman_enabled" in settings or "spoolman_url" in settings

    await db.commit()
    db.expire_all()

    # When the integration was toggled on or repointed, import Spoolman's
    # distinct location strings into the local catalog so the new store's
    # locations show up immediately (upstream #1505).
    if spoolman_changed:
        from backend.app.services.location_service import maybe_sync_spoolman_locations

        if await maybe_sync_spoolman_locations(db):
            await db.commit()

    # Return updated settings
    return await get_spoolman_settings(db)


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
        write_manifest,
        write_zip,
    )
    from backend.app.services.obico_history import obico_history
    from backend.app.services.obico_history_files import stage_history

    filename = f"bamdude-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    with tempfile.TemporaryDirectory() as temp_dir:
        staging = Path(temp_dir)
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
            zip_file = output_path / filename  # SEC-PATH-OK: trusted output directory and server-generated filename.
        else:
            fd, temporary = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            zip_file = Path(temporary)
        try:
            await _file_work(write_zip, staging, zip_file)
        except BaseException:
            if output_path is None:
                zip_file.unlink(missing_ok=True)
            raise
    return zip_file, filename


@router.get("/backup", dependencies=[RequireAdmin()])
async def create_backup(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_BACKUP),
):
    """Create a complete backup (database + all files) as a ZIP.

    Streams from a temp file rather than loading the whole ZIP into memory,
    so multi-gigabyte backups don't OOM the process.
    """
    import os as _os

    from fastapi import BackgroundTasks

    from backend.app.services.backup_files import BackupBusyError

    try:
        zip_file, filename = await create_backup_zip()

        # Schedule cleanup once the response has been sent
        bg = BackgroundTasks()
        bg.add_task(lambda p=zip_file: _os.unlink(p) if p.exists() else None)

        return FileResponse(
            path=str(zip_file),
            media_type="application/zip",
            filename=filename,
            background=bg,
        )
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
):
    """Validate and stage all files, then replace files and the portable database."""
    from backend.app.services.backup_files import BackupBusyError, exclusive_operation

    try:
        async with exclusive_operation():
            return await _restore_backup(file, db)
    except BackupBusyError as exc:
        raise HTTPException(409, "A backup or restore is already running") from exc


async def _quiesce_part_render() -> None:
    """Spec §9.6 and consilium E3-R4: the part render queue is stopped and its last attempt proven over, or the
    restore ends here -- before any other service is quiesced and before the database is touched."""
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


async def _restore_backup(file: UploadFile, db: AsyncSession):
    import asyncio
    import tempfile

    from backend.app.core.database import Base, close_all_connections, init_db, reinitialize_database
    from backend.app.core.db_dialect import is_sqlite
    from backend.app.core.db_portable import (
        _file_work,
        _snapshot_sqlite,
        import_sqlite_to_postgres,
        validate_sqlite_backup,
    )
    from backend.app.core.paths import resolve_data_dir
    from backend.app.services import queue_sources
    from backend.app.services.backup_files import FileRestore, extract_zip, validate_staged_queue_spool
    from backend.app.services.connector_runtime import connector_runtime
    from backend.app.services.farm_startup_guard import farm_startup_guard

    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Invalid backup file: must be a .zip file")

    with tempfile.TemporaryDirectory() as temp_dir:
        staging = Path(temp_dir)
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
            await _file_work(validate_sqlite_backup, backup_db, require_app_schema=True)
            zigbee_db = staging / "zigbee/zigbee.db"
            if zigbee_db.exists():
                await _file_work(validate_sqlite_backup, zigbee_db)
            await _file_work(validate_staged_queue_spool, staging, backup_db)
            from backend.app.services.obico_history_files import validate_history

            await _file_work(validate_history, staging, backup_db)
        except ValueError as exc:
            logger.warning("Restore preflight rejected database: %s", exc)
            raise HTTPException(400, "Invalid backup database: damaged or incompatible. Check server logs.") from exc

        try:
            files = FileRestore(staging, app_settings, resolve_data_dir())
            try:
                # Quiesce file owners before staging Zigbee and moving old files.
                # Shutdown failure is fatal; a delay cannot prove a writer stopped.
                from backend.app.services.background_dispatch import background_dispatch
                from backend.app.services.notification_service import notification_service
                from backend.app.services.print_scheduler import scheduler as print_scheduler
                from backend.app.services.smart_plug_manager import smart_plug_manager
                from backend.app.services.virtual_printer import virtual_printer_manager
                from backend.app.services.zigbee.coordinator import zigbee_coordinator
                from backend.app.services.zigbee.poller import zigbee_poller
                from backend.app.services.zigbee.supervisor import zigbee_supervisor

                farm_startup_guard.hold("restore")
                from backend.app.services.farm_startup_guard import notify_change

                await notify_change()
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
                if virtual_printer_manager.is_enabled:
                    await virtual_printer_manager.configure(enabled=False)
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
                            _snapshot_sqlite(backup_db, db_path)
                            files.committed = True

                        await _file_work(replace_sqlite)
                    else:
                        from backend.app.core.database import engine

                        await import_sqlite_to_postgres(engine, Base.metadata, backup_db)
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
            await connector_runtime.finish_restore()
            await mcp_runtime.reload(after_restore=True)
            logger.info("Restore complete - restart required")
            return {
                "success": True,
                "message": "Backup restored successfully. Please restart BamDude for changes to take effect.",
            }
        except HTTPException:
            raise
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
    """Update virtual printer settings and restart services if needed.

    For proxy mode with SSDP proxy (dual-homed setup):
    - remote_interface_ip: IP of interface on slicer's network (LAN B)
    - Local interface is auto-detected based on target printer IP
    """
    from sqlalchemy import select

    from backend.app.models.printer import Printer
    from backend.app.services.virtual_printer import (
        DEFAULT_VIRTUAL_PRINTER_MODEL,
        VIRTUAL_PRINTER_MODELS,
        virtual_printer_manager,
    )

    if "access_code" in request.query_params:
        raise HTTPException(422, "Credentials must be submitted in the JSON body")
    enabled, access_code, mode, model = payload.enabled, payload.access_code, payload.mode, payload.model
    target_printer_id, remote_interface_ip = payload.target_printer_id, payload.remote_interface_ip
    archive_name_source = payload.archive_name_source
    changes = {
        "virtual_printer_" + key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if key != "secret_actions" and (value is not None or key == "access_code")
    }
    actions = payload.secret_actions or {}
    if any(key != "access_code" for key in actions):
        raise HTTPException(422, "Invalid secret action")
    await lock_credential_config(db)
    if (
        mode is not None
        and mode != "proxy"
        and enabled is None
        and access_code is None
        and "access_code" not in actions
        and not await get_setting(db, "virtual_printer_access_code")
    ):
        # Preserve the existing mode-switch auto-disable when no credential
        # existed. An explicit credential clear must still refuse while on.
        changes["virtual_printer_enabled"] = False
    resolved = await resolve_settings_credentials(db, changes, {"virtual_printer_" + k: v for k, v in actions.items()})

    # Get current values
    current_enabled = await get_setting(db, "virtual_printer_enabled") == "true"
    current_mode = await get_setting(db, "virtual_printer_mode") or "file_manager"
    current_model = await get_setting(db, "virtual_printer_model") or DEFAULT_VIRTUAL_PRINTER_MODEL
    current_target_id_str = await get_setting(db, "virtual_printer_target_printer_id")
    current_target_id = int(current_target_id_str) if current_target_id_str else None
    current_remote_iface = await get_setting(db, "virtual_printer_remote_interface_ip") or ""

    # Apply updates
    new_enabled = enabled if enabled is not None else current_enabled
    new_access_code = resolved["virtual_printer_access_code"]
    new_mode = mode if mode is not None else current_mode
    new_model = model if model is not None else current_model
    new_target_id = target_printer_id if target_printer_id is not None else current_target_id
    new_remote_iface = remote_interface_ip if remote_interface_ip is not None else current_remote_iface

    # Validate mode. Three supported modes:
    #   "print_queue"  — archive + push to a printer queue
    #   "file_manager" — save to library (review-style flow)
    #   "proxy"        — transparent TCP proxy to a real printer
    # m002 already migrated legacy ``immediate``/``review``/``queue`` rows.
    if new_mode not in ("print_queue", "file_manager", "proxy"):
        return json_error(400, "Mode must be 'print_queue', 'file_manager', or 'proxy'")

    # Validate model
    if model is not None and model not in VIRTUAL_PRINTER_MODELS:
        return json_error(400, f"Invalid model. Must be one of: {', '.join(VIRTUAL_PRINTER_MODELS.keys())}")

    # Validate archive_name_source (B.14 / #1152): only two values are
    # meaningful — anything else would silently fall back to 'metadata' on
    # the read side and confuse the user about whether their toggle stuck.
    if archive_name_source is not None and archive_name_source not in ("metadata", "filename"):
        return json_error(400, "archive_name_source must be 'metadata' or 'filename'")

    # Mode-specific validation and printer lookup
    target_printer_ip = ""
    target_printer_serial = ""
    if new_mode == "proxy":
        # Proxy mode requires target printer when enabling
        if new_enabled and not new_target_id:
            # If just switching to proxy mode (not explicitly enabling), auto-disable
            if enabled is None:
                new_enabled = False
            else:
                return json_error(400, "Target printer is required for proxy mode")

        # Look up printer IP and serial if we have a target
        if new_target_id:
            result = await db.execute(select(Printer).where(Printer.id == new_target_id))
            printer = result.scalar_one_or_none()
            if not printer:
                return json_error(400, f"Printer with ID {new_target_id} not found")
            target_printer_ip = printer.ip_address
            target_printer_serial = printer.serial_number
        # Access code not required for proxy mode
    else:
        # Non-proxy modes require access code when enabling
        if new_enabled and not new_access_code:
            # If just switching modes (not explicitly enabling), auto-disable
            if enabled is None:
                new_enabled = False
            else:
                return json_error(400, "Access code is required when enabling virtual printer")

        # Validate access code length (Bambu Studio requires exactly 8 characters)
        if access_code is not None and access_code and len(access_code) != 8:
            return json_error(400, "Access code must be exactly 8 characters")

    # Save settings
    await set_setting(db, "virtual_printer_enabled", "true" if new_enabled else "false")
    if access_code is not None or "access_code" in actions:
        await set_setting(db, "virtual_printer_access_code", new_access_code)
    await set_setting(db, "virtual_printer_mode", new_mode)
    if model is not None:
        await set_setting(db, "virtual_printer_model", model)
    if target_printer_id is not None:
        await set_setting(db, "virtual_printer_target_printer_id", str(target_printer_id))
    if remote_interface_ip is not None:
        await set_setting(db, "virtual_printer_remote_interface_ip", remote_interface_ip)
    if archive_name_source is not None:
        await set_setting(db, "virtual_printer_archive_name_source", archive_name_source)
    await db.commit()
    db.expire_all()

    # Reconfigure virtual printer
    try:
        await virtual_printer_manager.configure(
            enabled=new_enabled,
            access_code=new_access_code,
            mode=new_mode,
            model=new_model,
            target_printer_ip=target_printer_ip,
            target_printer_serial=target_printer_serial,
            remote_interface_ip=new_remote_iface,
        )
    except ValueError as e:
        logger.warning("Virtual printer configuration validation error: %s", e)
        return json_error(400, "Invalid virtual printer configuration. Check the provided values.")
    except Exception as e:
        logger.error("Failed to configure virtual printer: %s", e, exc_info=True)
        return json_error(500, "Failed to configure virtual printer. Check server logs for details.")

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
