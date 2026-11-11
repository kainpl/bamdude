"""Write-only credential edits, bound to their effective destination.

Callers own their field schemas and required/anonymous authentication modes.
Take the installation anchor before reading a pair, resolve the whole edit,
then commit (save) or roll back (draft test) before any network operation.
"""

from collections.abc import Callable, Collection, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from backend.app.models.settings import Settings

CONFIG_GUARD_KEY = "_credential_config_guard"
SecretScope = Callable[[Mapping[str, Any]], Any]

# Partial imports cannot rebind local credentials outside their owning editor.
# Full database restore is different: it replaces whole pairs and reinitializes
# the anchor before admission. E extends this seam to the full editor policy.
CREDENTIAL_SETTING_KEYS = frozenset(
    {
        CONFIG_GUARD_KEY,
        "mqtt_broker",
        "mqtt_port",
        "mqtt_use_tls",
        "mqtt_username",
        "mqtt_password",
        "ha_url",
        "ha_token",
        "obico_ml_url",
        "obico_ml_token",
        "ldap_server_url",
        "ldap_security",
        "ldap_bind_dn",
        "ldap_bind_password",
        "prometheus_token",
        "virtual_printer_access_code",
        "smtp_host",
        "smtp_port",
        "smtp_use_tls",
        "smtp_security",
        "smtp_auth_enabled",
        "smtp_username",
        "smtp_password",
    }
)


async def initialize_credential_config_anchor(conn: AsyncConnection) -> None:
    """Bootstrap/restore only, never repair a missing anchor during a request."""
    await conn.execute(
        text("INSERT INTO settings (key, value) VALUES (:key, '1') ON CONFLICT (key) DO NOTHING"),
        {"key": CONFIG_GUARD_KEY},
    )


async def lock_credential_config(db: AsyncSession) -> None:
    """Lock the pre-existing anchor, including absent/disjoint pair writers.

    SQLite reserves its writer before authoritative reads. PostgreSQL locks
    this one row before adapter rows. Repeated acquisition within the same
    transaction is harmless; a missing bootstrap/restore anchor fails closed.
    """
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(
            update(Settings)
            .where(Settings.key == CONFIG_GUARD_KEY)
            .values(value=Settings.value, updated_at=Settings.updated_at)
        )
    anchor = (
        await db.execute(select(Settings.id).where(Settings.key == CONFIG_GUARD_KEY).with_for_update())
    ).scalar_one_or_none()
    if anchor is None:
        raise HTTPException(status_code=503, detail="Credential configuration is not ready")


def canonical_url(value: str | None, *, default: str = "", strip_trailing_slash: bool = True) -> str:
    """Compare effective endpoints without dropping base paths or account data.

    This is comparison, not URL admission/SSRF validation. Each adapter keeps
    its own transport policy. Query and fragment are deliberately retained.
    """
    # Runtime schemas validate new edits; a restored/hand-edited legacy JSON
    # config may still carry another scalar type. Comparison must not turn a
    # harmless metadata edit into an AttributeError or perform URL admission.
    raw = str(value or default).strip()
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        if not parsed.scheme or not host:
            return raw
        port = parsed.port
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != {"http": 80, "https": 443}.get(parsed.scheme.lower()):
            host += f":{port}"
        if "@" in parsed.netloc:
            host = parsed.netloc.rsplit("@", 1)[0] + "@" + host
        path = parsed.path.rstrip("/") if strip_trailing_slash else parsed.path
        return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, parsed.fragment))
    except ValueError:
        return raw


def merge_secret_fields(
    stored: Mapping[str, Any],
    changes: Mapping[str, Any],
    fields: Collection[str],
    actions: Mapping[str, str] | None = None,
    *,
    creating: bool = False,
    legacy_blank_keep: Collection[str] = (),
    replace_config: bool = False,
    scope: SecretScope | Mapping[str, SecretScope] | None = None,
    clear_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve explicit actions and compatible legacy edits without mask magic.

    Omitted values keep stored secrets on update, even for a full config
    replacement. A supplied legacy nonempty value replaces. Only explicitly
    named legacy adapters interpret blank/null as keep. Required-secret and
    anonymous-mode checks belong to the adapter, after this merge.

    ``scope`` compares the old/new effective destination for each retained
    credential. A per-field map supports e.g. ntfy's topic/token dependencies.
    It must include effective defaults and environment precedence itself.
    """
    fields = frozenset(fields)
    actions = dict(actions or {})
    if any(key not in fields or action not in {"keep", "clear", "replace"} for key, action in actions.items()):
        raise HTTPException(status_code=422, detail="Invalid secret action")
    resolved = {} if replace_config else dict(stored)
    resolved.update(changes)
    retained = set()
    for field in fields:
        action = actions.get(field)
        supplied = field in changes
        value = changes.get(field)
        if action in {"keep", "clear"} and supplied:
            raise HTTPException(status_code=422, detail=f"{field}: action requires an omitted value")
        if action == "replace" and (
            not supplied or value is None or value == "" or value == {} or isinstance(value, str) and not value.strip()
        ):
            raise HTTPException(status_code=422, detail=f"{field}: replacement must not be empty")
        keep = action == "keep" or (
            action is None and (not supplied or field in legacy_blank_keep and value in (None, ""))
        )
        if keep:
            old = stored.get(field)
            if action == "keep" and (creating or old is None or old == "" or old == {}):
                raise HTTPException(status_code=400, detail=f"{field}: no stored credential to keep")
            if old is not None:
                resolved[field] = old
                if old != "" and old != {}:
                    retained.add(field)
            else:
                resolved.pop(field, None)
        elif action == "clear":
            resolved[field] = (clear_values or {}).get(field, "")
    for field in retained:
        field_scope = scope.get(field) if isinstance(scope, Mapping) else scope
        if field_scope is not None and field_scope(stored) != field_scope(resolved):
            raise HTTPException(
                status_code=400, detail=f"{field}: replace or clear credential when changing destination"
            )
    return resolved


def secret_presence(config: Mapping[str, Any], fields: Collection[str]) -> dict[str, bool]:
    """Presence only; never synthesize a mask that an editor could write back."""
    return {field: bool(config.get(field)) for field in fields}
