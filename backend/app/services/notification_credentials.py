"""Notification-specific credential projection and effective sender scopes.

Storage/runtime configs keep their real values. Public projections are new
dictionaries, and draft/save share the installation credential merge primitive.
"""

import json
from functools import wraps
from urllib.parse import parse_qsl, unquote, urlsplit

from fastapi import HTTPException

from backend.app.core.credential_config import canonical_url, merge_secret_fields, secret_presence
from backend.app.core.logging_filters import redact_log_credentials
from backend.app.services.log_reader import redact_known_values

SECRET_FIELDS = {
    "callmebot": {"apikey"},
    "ntfy": {"auth_token", "topic"},
    "pushover": {"user_key", "app_token"},
    "bark": {"device_key"},
    "telegram": {"bot_token"},
    "email": {"password"},
    "discord": {"webhook_url"},
    "webhook": {"webhook_url", "auth_header"},
    "homeassistant": set(),
    "signal": {"auth_header"},
}
PUBLIC_FIELDS = {
    "callmebot": {"phone"},
    "ntfy": {"server", "event_priorities", "auth_mode"},
    "pushover": {"priority", "retry", "expire"},
    "bark": {"server", "group", "sound", "level"},
    "telegram": set(),
    "email": {"smtp_server", "smtp_port", "username", "security", "auth_enabled", "from_email", "to_email"},
    "discord": set(),
    "webhook": {"payload_format", "field_title", "field_message", "auth_mode"},
    "homeassistant": {"service", "data"},
    "signal": {"server", "sender_number", "recipient_type", "numbers", "group_id", "auth_mode"},
}
REQUIRED_SECRETS = {
    "callmebot": {"apikey"},
    "ntfy": {"topic"},
    "pushover": {"user_key", "app_token"},
    "bark": {"device_key"},
    "telegram": {"bot_token"},
    "discord": {"webhook_url"},
    "webhook": {"webhook_url"},
}


def config_dict(value) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def provider_secret_fields(kind: str, *configs: dict) -> set[str]:
    fields = set(SECRET_FIELDS.get(kind, ()))
    # Legacy base URLs can embed Basic credentials/capabilities too. Their
    # endpoint becomes opaque instead of returning a stripped, writable URL.
    endpoint_fields = ("server",) if kind in {"ntfy", "bark", "signal"} else ("smtp_server",) if kind == "email" else ()
    for config in configs:
        for key in endpoint_fields:
            value = config.get(key)
            if not isinstance(value, str):
                continue
            try:
                parts = urlsplit(value)
                malformed_url = key == "server" and (
                    parts.scheme.lower() not in {"http", "https"} or not parts.hostname
                )
                if (
                    malformed_url
                    or "@" in unquote(value)
                    or parts.username is not None
                    or parts.password is not None
                    or parts.query
                    or parts.fragment
                ):
                    fields.add(key)
            except ValueError:
                fields.add(key)
    return fields


def provider_projection(kind: str, value) -> tuple[dict, dict[str, bool], dict[str, str]]:
    config = config_dict(value)
    fields = provider_secret_fields(kind, config)
    safe = {key: value for key, value in config.items() if key in PUBLIC_FIELDS.get(kind, ()) and key not in fields}
    display = {}
    for key in ("webhook_url", "server", "smtp_server"):
        if key in fields and isinstance(config.get(key), str):
            try:
                host = urlsplit(config[key]).hostname
                if host:
                    display[key] = host
            except ValueError:
                pass
    return safe, secret_presence(config, fields), display


def ntfy_server(config: dict) -> str:
    """Match the sender default without activating broken legacy endpoints.

    Only an absent key selects the public service. Explicit empty/null legacy
    values stay unusable, and cannot be rebound there with retained secrets.
    """
    value = config.get("server", "https://ntfy.sh")
    return value.rstrip("/") if isinstance(value, str) else ""


def email_auth_enabled(config: dict) -> bool:
    return str(config.get("auth_enabled", "true")).lower() == "true"


def _scope(kind: str, field: str, config: dict):
    kind = config.get("_provider_type", kind)

    def text(name, default=""):
        return str(config.get(name) or default).strip()

    if kind == "callmebot":
        return kind, text("phone")
    if kind == "ntfy":
        server = canonical_url(ntfy_server(config))
        return (kind, server) if field == "topic" else (kind, server, text("topic"))
    if kind == "bark":
        return kind, canonical_url(config.get("server"), default="https://api.day.app")
    if kind == "email":
        try:
            port = int(config.get("smtp_port", 587))
        except (ValueError, TypeError):
            raise HTTPException(422, "smtp_port: invalid port") from None
        return (
            kind,
            text("smtp_server").lower(),
            port,
            config.get("security", "starttls"),
            text("username"),
        )
    if kind in {"webhook", "discord"}:
        return kind, canonical_url(config.get("webhook_url"), strip_trailing_slash=False), "POST"
    if kind == "signal":
        server = text("server").rstrip("/")
        for suffix in ("/v2/send", "/v1/send"):
            if server.lower().endswith(suffix):
                server = server[: -len(suffix)]
                break
        return kind, canonical_url(server), text("sender_number")
    # Fixed remote API for Telegram and Pushover. Each Pushover field is an
    # independently replaceable credential sent only to that fixed API.
    return (kind,)


def resolve_provider_config(
    kind: str,
    changes: dict | None,
    actions: dict | None,
    *,
    stored=None,
    stored_type=None,
    creating=False,
    enabled=True,
    validate_required=False,
) -> dict:
    old = config_dict(stored)
    type_changed = stored_type is not None and stored_type != kind
    retired_fields = provider_secret_fields(stored_type, old) if type_changed else set()
    # A different adapter has no authority to retain any old adapter secret.
    # Explicit new credentials may create its new tuple; no secret is migrated.
    if type_changed:
        old = {}
    replace_config = changes is not None
    changes = dict(changes or {})
    fields = provider_secret_fields(kind, old, changes)
    for field in fields:
        if field in changes and changes[field] is not None and not isinstance(changes[field], str):
            raise HTTPException(422, f"{field}: expected a string")
    # These senders trim headers before deciding whether to authenticate.
    # Resolve that same value before validating an anonymous transition.
    for field in ("auth_token", "auth_header"):
        if field in fields and isinstance(changes.get(field), str):
            changes[field] = changes[field].strip()
    if kind == "email" and "auth_enabled" in changes:
        mode = str(changes["auth_enabled"]).lower()
        if mode not in {"true", "false"}:
            raise HTTPException(422, "auth_enabled: invalid authentication mode")
        changes["auth_enabled"] = mode
    resolved = merge_secret_fields(
        {**old, "_provider_type": kind},
        {**changes, "_provider_type": kind},
        fields,
        actions,
        creating=creating,
        replace_config=replace_config,
        scope={field: lambda config, field=field: _scope(kind, field, config) for field in fields},
    )
    resolved.pop("_provider_type", None)
    if type_changed:
        for field in retired_fields - provider_secret_fields(kind, changes):
            resolved.pop(field, None)
    # Anonymous mode must be explicit when removing an existing credential.
    for field in ("auth_token", "auth_header"):
        if (
            field in fields
            and (old.get(field) or (actions or {}).get(field) == "clear")
            and not str(resolved.get(field) or "").strip()
            and enabled
        ):
            if resolved.get("auth_mode") != "anonymous":
                raise HTTPException(422, f"{field}: select anonymous authentication explicitly")
    required = set(REQUIRED_SECRETS.get(kind, ()))
    auth_field = "auth_token" if kind == "ntfy" else "auth_header" if kind in {"signal", "webhook"} else None
    mode = resolved.get("auth_mode")
    if auth_field and mode is not None and ("auth_mode" in changes or validate_required or creating):
        if not isinstance(mode, str) or mode not in {"anonymous", "authenticated"}:
            raise HTTPException(422, "auth_mode: invalid authentication mode")
        if enabled and mode == "authenticated":
            required.add(auth_field)
        if enabled and mode == "anonymous" and str(resolved.get(auth_field) or "").strip():
            raise HTTPException(422, f"{auth_field}: clear the credential to select anonymous authentication")
    if kind == "email" and email_auth_enabled(resolved):
        required.add("password")
    # Existing malformed/disabled legacy rows remain editable in metadata.
    credential_edit = (
        creating
        or type_changed
        or validate_required
        or bool(actions)
        or bool({"auth_mode", "auth_enabled"} & changes.keys())
        or any(field in changes for field in fields)
    )
    if enabled and credential_edit:
        for field in required:
            if not isinstance(resolved.get(field), str) or not resolved[field].strip():
                raise HTTPException(422, f"{field}: credential is required while enabled")
    if kind == "discord" and resolved.get("webhook_url") and (creating or "webhook_url" in changes):
        if not resolved["webhook_url"].startswith(
            ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
        ):
            raise HTTPException(422, "webhook_url: invalid Discord endpoint")
    return resolved


def safe_provider_message(message: str, kind: str, config: dict, *, extra_secrets=()) -> str:
    values = {value: "[REDACTED]" for value in extra_secrets if isinstance(value, str) and value}
    for field in provider_secret_fields(kind, config):
        value = config.get(field)
        if isinstance(value, str) and value:
            values[value] = "[REDACTED]"
            normalized = value.strip()
            if normalized:
                values[normalized] = "[REDACTED]"
            header = normalized.split(None, 1)
            if field == "auth_header" and len(header) == 2 and header[0].lower() in {"bearer", "basic"}:
                values[header[1]] = "[REDACTED]"
            if field == "bot_token" and ":" in value:
                values[value.split(":", 1)[1]] = "[REDACTED]"
            try:
                parts = urlsplit(value)
                for part in (parts.username, parts.password):
                    if part:
                        values[unquote(part)] = "[REDACTED]"
                if parts.scheme and parts.netloc:
                    for part in parts.path.split("/"):
                        # The complete URL is always confidential. Generic
                        # short path words are not standalone credentials:
                        # masking /print/a would destroy ordinary diagnostics.
                        decoded = unquote(part)
                        if len(decoded) >= 12:
                            values[decoded] = "[REDACTED]"
                    for _, part in parse_qsl(parts.query):
                        if part:
                            values[part] = "[REDACTED]"
            except ValueError:
                pass
    return redact_log_credentials(redact_known_values(str(message), values))


def credential_safe_sender(kind: str):
    """Sanitize remote/driver error echoes before status, test or log storage."""

    def decorate(function):
        @wraps(function)
        async def wrapped(self, config, *args, **kwargs):
            try:
                success, message = await function(self, config, *args, **kwargs)
            except Exception as exc:
                success, message = False, str(exc)
            return success, safe_provider_message(message, kind, config)

        return wrapped

    return decorate
