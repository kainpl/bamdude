"""LDAP authentication service for BamDude.

Supports:
- LDAP bind authentication (simple bind with user's credentials)
- StartTLS, LDAPS, and explicitly configured legacy plaintext connections
- User search with configurable filter
- Group membership resolution for role mapping
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from ldap3 import ALL, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

logger = logging.getLogger(__name__)

LDAP_ERROR_MESSAGES = {
    "starttls_refused": "LDAP StartTLS operation refused by the server",
    "tls_failed": "LDAP TLS certificate or handshake failed",
    "bind_failed": "LDAP account bind failed",
    "connection_failed": "LDAP connection failed",
    "search_failed": "LDAP directory search failed",
    "groups_incomplete": "LDAP group lookup incomplete",
    "configuration_failed": "LDAP transport configuration is invalid",
}


class LDAPOperationError(LDAPException):
    """Safe category only: never carry a driver message, URL or credential."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(LDAP_ERROR_MESSAGES[code])


class LDAPGroupLookupError(LDAPOperationError):
    def __init__(self):
        super().__init__("groups_incomplete")


class LDAPReferralError(LDAPOperationError):
    def __init__(self):
        super().__init__("search_failed")


@dataclass
class LDAPUserInfo:
    """User information retrieved from LDAP after successful authentication."""

    username: str
    email: str | None
    display_name: str | None
    groups: list[str]  # List of group DNs the user belongs to
    groups_complete: bool = True


@dataclass
class LDAPSearchResult:
    """A directory user returned by the admin search endpoint (no auth performed).

    Distinct from ``LDAPUserInfo`` because the manual-provision UI only
    needs a few fields to render the picker — groups + email-verified
    semantics belong in the actual ``LDAPUserInfo`` path used at
    provision-time. Upstream Bambuddy #1298.
    """

    username: str
    email: str | None
    display_name: str | None
    dn: str


@dataclass
class LDAPConfig:
    """LDAP configuration parsed from settings."""

    server_url: str
    bind_dn: str
    bind_password: str
    search_base: str
    user_filter: str  # e.g. "(sAMAccountName={username})"
    security: str  # "none", "starttls", "ldaps"
    group_mapping: dict[str, str]  # LDAP group DN -> BamDude group name
    auto_provision: bool
    ca_cert_path: str  # Path to CA certificate file (empty = skip verification)
    default_group: str  # Fallback BamDude group assigned when user has no mapped groups (empty = no fallback)


def parse_ldap_config(settings: dict[str, str]) -> LDAPConfig | None:
    """Parse LDAP config from settings key-value pairs. Returns None if LDAP not enabled."""
    if settings.get("ldap_enabled", "false").lower() != "true":
        return None

    server_url = settings.get("ldap_server_url", "").strip()
    if not server_url:
        return None

    group_mapping_raw = settings.get("ldap_group_mapping", "")
    try:
        group_mapping = json.loads(group_mapping_raw) if group_mapping_raw else {}
    except json.JSONDecodeError:
        group_mapping = {}

    return LDAPConfig(
        server_url=server_url,
        bind_dn=settings.get("ldap_bind_dn", "").strip(),
        bind_password=settings.get("ldap_bind_password", ""),
        search_base=settings.get("ldap_search_base", "").strip(),
        user_filter=settings.get("ldap_user_filter", "(sAMAccountName={username})").strip(),
        security=settings.get("ldap_security", "starttls").strip(),
        group_mapping=group_mapping if isinstance(group_mapping, dict) else {},
        auto_provision=settings.get("ldap_auto_provision", "false").lower() == "true",
        ca_cert_path=settings.get("ldap_ca_cert_path", "").strip(),
        default_group=settings.get("ldap_default_group", "").strip(),
    )


def _create_server(config: LDAPConfig) -> Server:
    """Create an ldap3 Server instance from config.

    The explicit legacy ``none`` transport remains compatible. A requested
    StartTLS/LDAPS transport never falls back to it after a TLS failure.
    """
    import ssl

    if config.security not in {"none", "starttls", "ldaps"}:
        raise LDAPOperationError("configuration_failed")
    use_ssl = config.security == "ldaps" or config.server_url.startswith("ldaps://")

    if config.ca_cert_path:
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=config.ca_cert_path)
    else:
        tls = Tls(validate=ssl.CERT_NONE)

    return Server(config.server_url, use_ssl=use_ssl, tls=tls, get_info=ALL, connect_timeout=10)


def _open_service_connection(config: LDAPConfig, server: Server, *, check_names: bool = True) -> Connection:
    """Open and bind a service-account LDAP connection. Raises on failure.

    ``check_names`` toggles ldap3's client-side attribute-name validation.
    The default keeps it on so typos in ``user_filter`` fail loudly. The
    fuzzy directory search disables it because its fixed OR filter spans
    both AD-only (``sAMAccountName`` / ``displayName``) and OpenLDAP-only
    attribute names — without this bypass ldap3 throws
    ``LDAPAttributeError`` before any request is sent on a directory whose
    schema doesn't define one of the names. Upstream Bambuddy commit
    d6364646 / #1298.
    """
    return _open_bound_connection(config, server, config.bind_dn, config.bind_password, check_names=check_names)


def _close_connection(conn: Connection) -> None:
    try:
        conn.unbind()
    except Exception:
        logger.warning("LDAP connection cleanup failed")


def _open_bound_connection(
    config: LDAPConfig,
    server: Server,
    user: str,
    password: str,
    *,
    check_names: bool = True,
    refresh_schema: bool = True,
) -> Connection:
    conn = Connection(
        server,
        user=user,
        password=password,
        auto_bind=False,
        # Keep every operation on the configured, explicitly authenticated
        # transport. Hidden range/referral requests cannot prove completeness.
        auto_range=False,
        auto_referrals=False,
        raise_exceptions=True,
        read_only=True,
        check_names=check_names,
    )
    stage = "connection"
    try:
        # Suppress every anonymous metadata read. Some directories reveal their
        # schema only after the service account has authenticated over TLS.
        conn.open(read_server_info=False)
        if config.security == "starttls" and not server.ssl:
            stage = "tls"
            if not conn.start_tls(read_server_info=False):
                raise LDAPOperationError("starttls_refused")
        stage = "bind"
        if not conn.bind(read_server_info=False):
            raise LDAPOperationError("bind_failed")
        if refresh_schema:
            try:
                conn.refresh_server_info()
            except LDAPException:
                # Unknown/unavailable schema is a supported capability state.
                logger.info("LDAP authenticated schema is unavailable")
        return conn
    except Exception as exc:
        _close_connection(conn)
        if isinstance(exc, LDAPOperationError):
            raise
        if stage == "tls":
            result = conn.result if isinstance(conn.result, dict) else {}
            code = "starttls_refused" if result.get("result") not in (None, 0) else "tls_failed"
        elif stage == "connection" and server.ssl:
            code = "tls_failed"
        else:
            code = "bind_failed" if stage == "bind" else "connection_failed"
        raise LDAPOperationError(code) from None


def _entry_values(entry, name: str) -> list[str]:
    try:
        attributes = getattr(entry, "entry_attributes_as_dict", None)
        if isinstance(attributes, Mapping):
            value = next((value for key, value in attributes.items() if key.casefold() == name.casefold()), None)
        else:
            value = getattr(entry, name, None)
    except (AttributeError, LDAPException):
        return []
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None and str(item)]
    return [str(value)] if str(value) else []


def _schema_keys(conn: Connection, name: str) -> set[str] | None:
    schema = getattr(conn.server, "schema", None)
    values = getattr(schema, name, None)
    return {str(key).casefold() for key in values} if isinstance(values, Mapping) and values else None


def _user_attributes(conn: Connection) -> list[str]:
    attributes = _schema_keys(conn, "attribute_types")
    if attributes is None:
        return ["*", "+"]
    return ["*", "memberOf"] if "memberof" in attributes else ["*"]


def _search_entries(conn: Connection, *, groups: bool = False, allow_size_limit: bool = False, **kwargs) -> list:
    try:
        conn.search(**kwargs)
        result = conn.result if isinstance(conn.result, dict) else {}
        if result.get("result") == 10 or any(
            isinstance(item, dict) and item.get("type") == "searchResRef"
            for item in (getattr(conn, "response", None) or [])
        ):
            raise LDAPReferralError()
        # False is also the successful empty result. ldap3 does not raise for
        # time/size limits or referrals, so inspect the result independently.
        if result.get("result") not in ({0, 4} if allow_size_limit else {0}):
            raise LDAPGroupLookupError() if groups else LDAPOperationError("search_failed")
        return list(conn.entries)
    except LDAPOperationError:
        raise
    except Exception:
        raise LDAPGroupLookupError() if groups else LDAPOperationError("search_failed") from None


def _group_search_base(conn: Connection, config: LDAPConfig, user_dn: str) -> str:
    contexts = getattr(getattr(conn.server, "info", None), "naming_contexts", None)
    if isinstance(contexts, (list, tuple)):
        matches = [
            value
            for value in contexts
            if isinstance(value, str)
            and (user_dn.casefold() == value.casefold() or user_dn.casefold().endswith("," + value.casefold()))
        ]
        if matches:
            return max(matches, key=len)
    try:
        parts = parse_dn(user_dn)
        for index, (name, _, _) in enumerate(parts):
            if name.casefold() == "dc":
                return "".join(name + "=" + value + separator for name, value, separator in parts[index:])
    except LDAPException:
        pass
    return config.search_base


def _pick_canonical_username(entry, fallback: str) -> str:
    """Prefer ``sAMAccountName``, then ``uid``, then the supplied fallback."""
    for name in ("sAMAccountName", "uid"):
        values = _entry_values(entry, name)
        if values:
            return values[0]
    return fallback


def _extract_user_info(
    service_conn: Connection, config: LDAPConfig, user_entry, fallback_username: str
) -> LDAPUserInfo:
    """Build an ``LDAPUserInfo`` from an already-fetched directory entry.

    A partial lookup never returns an apparently complete list: callers must
    not use it to select a privileged default or remove managed groups.
    """
    email = next(iter(_entry_values(user_entry, "mail")), None)
    display_name = next(iter(_entry_values(user_entry, "displayName")), None)
    returned = getattr(user_entry, "entry_attributes_as_dict", {})
    if isinstance(returned, Mapping) and any(key.casefold().startswith("memberof;range=") for key in returned):
        raise LDAPGroupLookupError()
    groups = _entry_values(user_entry, "memberOf")
    canonical_username = _pick_canonical_username(user_entry, fallback_username)
    attributes = _schema_keys(service_conn, "attribute_types")
    classes = _schema_keys(service_conn, "object_classes")
    # AD's authenticated memberOf is authoritative. Do not query the whole
    # domain for POSIX/forward membership that its backlink already describes.
    ad_backlink = bool(_entry_values(user_entry, "sAMAccountName")) and (
        bool(groups) or (attributes is not None and "memberof" in attributes)
    )
    if not ad_backlink:
        base = _group_search_base(service_conn, config, str(user_entry.entry_dn))

        def supported(object_class: str, attribute: str) -> bool:
            return (classes is None or object_class.casefold() in classes) and (
                attributes is None or attribute.casefold() in attributes
            )

        filters = []
        dn = _ldap_escape(str(user_entry.entry_dn))
        for object_class, attribute in (("groupOfNames", "member"), ("groupOfUniqueNames", "uniqueMember")):
            if supported(object_class, attribute):
                filters.append(f"(&(objectClass={object_class})({attribute}={dn}))")
        if supported("posixGroup", "memberUid"):
            filters.append(f"(&(objectClass=posixGroup)(memberUid={_ldap_escape(canonical_username)}))")
        primary_gid = next(iter(_entry_values(user_entry, "gidNumber")), None)
        if primary_gid is not None and supported("posixGroup", "gidNumber"):
            filters.append(f"(&(objectClass=posixGroup)(gidNumber={_ldap_escape(primary_gid)}))")
        # Separate queries let every result be checked before consuming any
        # groups; an unsupported class proven absent in schema is simply skipped.
        for search_filter in filters:
            entries = _search_entries(
                service_conn,
                groups=True,
                search_base=base,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=["1.1"],
            )
            groups.extend(str(entry.entry_dn) for entry in entries)

    # Dedupe group DNs (user may be in a group via both memberUid and
    # primary gidNumber). LDAP DNs are case-insensitive by spec.
    seen_lower: set[str] = set()
    deduped_groups: list[str] = []
    for g in groups:
        key = g.casefold()
        if key not in seen_lower:
            seen_lower.add(key)
            deduped_groups.append(g)

    return LDAPUserInfo(
        username=canonical_username,
        email=email,
        display_name=display_name,
        groups=deduped_groups,
    )


def authenticate_ldap_user(config: LDAPConfig, username: str, password: str) -> LDAPUserInfo | None:
    """Authenticate a user via LDAP bind.

    1. Bind with service account to search for the user DN
    2. Attempt bind with the user's DN and provided password
    3. On success, retrieve user attributes and group memberships

    Returns LDAPUserInfo on success, None on failure.
    """
    if not password:
        return None

    try:
        server = _create_server(config)
        service_conn = _open_service_connection(config, server)
    except Exception:
        logger.warning("LDAP service account connection failed")
        return None

    try:
        # Search for the user
        search_filter = config.user_filter.replace("{username}", _ldap_escape(username))
        entries = _search_entries(
            service_conn,
            search_base=config.search_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=_user_attributes(service_conn),
        )

        if not entries:
            logger.info("LDAP user not found: %s", username)
            return None

        user_entry = entries[0]
        user_dn = str(user_entry.entry_dn)

        # Step 2: Bind as the user to verify password
        try:
            user_conn = _open_bound_connection(config, server, user_dn, password, refresh_schema=False)
            _close_connection(user_conn)
        except Exception:
            logger.info("LDAP user credential verification failed")
            return None

        info = _extract_user_info(service_conn, config, user_entry, username)
        # The raw DN stays out of the log: its leaf CN is the user's real name
        # (#2681). Username + group count is everything this line was for. The
        # support-bundle sanitizer redacts DNs that surface anyway — an ldap3
        # exception string above, for one — but not writing PII to disk in the
        # first place is the primary hygiene, not the fallback.
        logger.info(
            "LDAP authentication successful for user: %s (groups: %d)",
            info.username,
            len(info.groups),
        )
        return info
    finally:
        _close_connection(service_conn)


def lookup_ldap_user(config: LDAPConfig, username: str) -> LDAPUserInfo | None:
    """Look up a directory user by exact username via the service-account bind.

    Performs no password verification — intended for the admin manual-
    provision flow, where the caller has already been authenticated as a
    BamDude admin and now needs the directory attributes (email, display
    name, group DNs) to create the user. Uses the same ``user_filter``
    template that the login path uses, so anything that logs in
    successfully via auto-provision is also resolvable here. Upstream
    Bambuddy #1298 / commit d6364646.
    """
    server = _create_server(config)

    try:
        service_conn = _open_service_connection(config, server)
    except Exception:
        logger.warning("LDAP service account connection failed during lookup")
        raise

    try:
        search_filter = config.user_filter.replace("{username}", _ldap_escape(username))
        entries = _search_entries(
            service_conn,
            search_base=config.search_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=_user_attributes(service_conn),
        )
        if not entries:
            logger.info("LDAP lookup: user not found: %s", username)
            return None
        return _extract_user_info(service_conn, config, entries[0], username)
    finally:
        _close_connection(service_conn)


def search_ldap_users(config: LDAPConfig, query: str, limit: int = 25) -> list[LDAPSearchResult]:
    """Fuzzy search the directory for users matching ``query``.

    Uses a fixed OR filter across ``sAMAccountName``, ``uid``, ``mail``,
    ``displayName`` and ``cn`` — covering both Active Directory and
    OpenLDAP layouts. The query is RFC-4515 escaped so a typed ``*``
    doesn't enumerate the whole directory. Returns up to ``limit``
    results (default 25). Service-bind failures raise so the caller can
    surface a 503; "no matches" returns an empty list.

    Callers should enforce a minimum query length (≥2 chars) — short
    queries against a large directory are wasteful and effectively
    unbounded.
    """
    query = query.strip()
    if len(query) < 2:
        return []

    escaped = _ldap_escape(query)
    search_filter = (
        f"(|(sAMAccountName=*{escaped}*)(uid=*{escaped}*)(mail=*{escaped}*)(displayName=*{escaped}*)(cn=*{escaped}*))"
    )

    server = _create_server(config)

    try:
        # check_names=False so OpenLDAP directories (no sAMAccountName /
        # displayName in schema) don't reject the cross-schema OR filter
        # — see helper docstring.
        service_conn = _open_service_connection(config, server, check_names=False)
    except Exception:
        logger.warning("LDAP service account connection failed during search")
        raise

    try:
        # attributes=["*"] requests all user attributes. We can't enumerate
        # the AD/OpenLDAP-specific names (sAMAccountName, displayName)
        # explicitly because ldap3 validates the attribute list against the
        # server schema even with check_names=False — and OpenLDAP rejects
        # the AD names. The ``*`` wildcard is hardcoded in ldap3's
        # ``ATTRIBUTES_EXCLUDED_FROM_CHECK`` so it bypasses that validation,
        # and the server returns whatever it has.
        entries = _search_entries(
            service_conn,
            allow_size_limit=True,
            search_base=config.search_base,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=["*"],
            size_limit=limit,
        )
        results: list[LDAPSearchResult] = []
        for entry in entries:
            username = _pick_canonical_username(entry, "")
            if not username and _entry_values(entry, "cn"):
                # Last resort — some OpenLDAP layouts only have cn
                username = _entry_values(entry, "cn")[0]
            if not username:
                continue
            email = next(iter(_entry_values(entry, "mail")), None)
            display_name = next(iter(_entry_values(entry, "displayName")), None)
            results.append(
                LDAPSearchResult(
                    username=username,
                    email=email,
                    display_name=display_name,
                    dn=str(entry.entry_dn),
                )
            )
        logger.info("LDAP directory search for %r returned %d result(s)", query, len(results))
        return results
    finally:
        _close_connection(service_conn)


def resolve_group_mapping(ldap_groups: list[str], group_mapping: dict[str, str]) -> list[str]:
    """Map LDAP group DNs to BamDude group names.

    Returns list of BamDude group names that the user should be added to.
    Comparison is case-insensitive on the LDAP group DN.
    """
    if not group_mapping:
        return []

    # Build case-insensitive lookup
    mapping_lower = {k.casefold(): v for k, v in group_mapping.items()}
    result = []
    for ldap_group in ldap_groups:
        bamdude_group = mapping_lower.get(ldap_group.casefold())
        if bamdude_group and bamdude_group not in result:
            result.append(bamdude_group)
    return result


def test_ldap_connection(config: LDAPConfig) -> tuple[bool, str]:
    """Test LDAP connection and service account bind.

    Returns (success, message).
    """
    try:
        server = _create_server(config)
        conn = _open_service_connection(config, server)
        try:
            # A bounded connectivity probe is not a complete group lookup.
            _search_entries(
                conn,
                allow_size_limit=True,
                search_base=config.search_base,
                search_filter="(objectClass=*)",
                search_scope=SUBTREE,
                size_limit=1,
            )
        finally:
            _close_connection(conn)
        return True, "LDAP connection successful"
    except LDAPOperationError as exc:
        return False, str(exc)
    except Exception:
        return False, LDAP_ERROR_MESSAGES["connection_failed"]


def _ldap_escape(value: str) -> str:
    """Escape special characters in LDAP search filter values (RFC 4515)."""
    return escape_filter_chars(value)
