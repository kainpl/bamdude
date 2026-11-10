"""Shared primitives for reading, parsing, and sanitizing the BamDude app log.

Used by the service-layer log-health scanner (``log_health.py``) so it can
reuse log reading and redaction without importing from the API layer, and by the
support-bundle routes in ``routes/support.py``, whose ``_sanitize_log_content``
is now a thin alias for the one here.

That alias replaced a byte-identical copy (2026-08-07). The duplication was not
theoretical: the bundle a user emails us went through the copy, the scanner
through this one, so the LDAP-DN redaction added for #2681 would have covered
the scanner and missed the bundle. This module is the single sanitizer.
"""

import base64
import html
import json
import logging
import os
import re
from typing import BinaryIO
from urllib.parse import quote, quote_plus, unquote, urlsplit

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.logging_filters import URL_CREDENTIALS_PATTERN, _unescape_log_text, redact_log_credentials
from backend.app.core.sensitive_settings import redact_setting_in_log, sensitive_settings_predicate
from backend.app.models.printer import Printer
from backend.app.models.settings import Settings
from backend.app.models.user import User

logger = logging.getLogger(__name__)

# Log line format: "2024-01-15 10:30:45,123 INFO [module.name] [trace_id] Message"
# The trace_id is left as part of the message group — callers that need it can
# parse it out; the log-health scanner does not.
LOG_LINE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2},\d{3})\s+(\w+)\s+\[([^\]]+)\]\s+(.*)$")

# LDAP Distinguished Names carry PII — the leaf ``CN=`` is the user's real name
# (upstream #2681), on par with the email address this file already redacts.
#
# This is the one redaction here that cannot be value-driven. Everything else is
# enumerable from the database (printer names, serials, access codes) and gets
# substituted by exact match; a DN is per-user, arrives from the directory, and
# was never ours to list. So it has to be matched by SHAPE.
#
# Two RDN components minimum, comma-joined, each an ``attr=value`` whose attr is a
# known LDAP attribute type. Requiring two is what stops an incidental
# ``key=value`` elsewhere in a log line from being clobbered. It catches DNs
# wherever they surface, not only the deliberate "auth successful" line: ldap3
# exception strings and group DNs carry them too, which is why removing the log
# statement is not on its own sufficient.
#
# The value class excludes ``<>;+`` because RFC 4514 requires those escaped inside
# a DN value — so an unescaped one marks the END of the DN. Without that, the
# final component (which has no trailing comma to stop it) greedily swallows the
# rest of the line, e.g. ``… -> GroupName``.
#
# Bias is deliberately toward redaction: over-redacting a rare debug line to
# ``[DN]`` is a recoverable annoyance, leaking somebody's real name into a file
# they email us is not.
_LDAP_RDN = r"(?:CN|OU|DC|UID|O|L|ST|C|SN|GN|DN|E|MAIL|STREET|GIVENNAME|SURNAME)=[^,\n<>;+]+"
_LDAP_DN_PATTERN = re.compile(rf"(?i)\b{_LDAP_RDN}(?:\s*,\s*{_LDAP_RDN})+")

# Shape rules complement known-value matching. No entropy heuristic: content
# hashes are useful diagnostic evidence and must survive.
# Numeric-dot boundaries matter: a four-component substring of an LDAP OID
# (1.2.840.113556.1.4.1941) is not an IP. Nor is a zero-padded firmware version.
IPV4_PATTERN = re.compile(
    r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]\d|\d)(?![\w.])"
)


def _known_value_spellings(value: str) -> set[str]:
    """Common log/transport spellings, not a heuristic for arbitrary prose."""
    return {
        value,
        quote(value, safe=""),
        quote_plus(value, safe=""),
        quote(quote(value, safe=""), safe=""),
        html.escape(value),
        json.dumps(value, ensure_ascii=True)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        base64.b64encode(value.encode()).decode(),
    }


def _known_value_replacer(sensitive_strings: dict[str, str]):
    variants: dict[str, str] = {}
    for value, label in sensitive_strings.items():
        if not value or (len(value) < 3 and label != "[REDACTED]"):
            continue
        for spelling in _known_value_spellings(value):
            variants[spelling] = label
    if not variants:
        return lambda content: content
    pattern = re.compile(
        "|".join(
            (r"(?<![\w.])" + re.escape(value) + r"(?![\w.])") if variants[value] == "[IP]" else re.escape(value)
            for value in sorted(variants, key=len, reverse=True)
        )
    )

    def replace(content):
        sanitized = pattern.sub(lambda match: variants[match[0]], content)
        candidate = sanitized
        changed = False
        for _ in range(4):
            decoded = _unescape_log_text(candidate)
            if decoded == candidate:
                break
            candidate = pattern.sub(lambda match: variants[match[0]], decoded)
            changed = changed or candidate != decoded
        return candidate if changed else sanitized

    return replace


def redact_known_values(content: str, sensitive_strings: dict[str, str]) -> str:
    return _known_value_replacer(sensitive_strings)(content)


class LogEntry(BaseModel):
    """A single parsed log entry."""

    timestamp: str
    level: str
    logger_name: str
    message: str


def parse_log_line(line: str) -> LogEntry | None:
    """Parse a single log line into a LogEntry, or None if it is not a line start."""
    match = LOG_LINE_PATTERN.match(line.strip())
    if match:
        return LogEntry(
            timestamp=match.group(1),
            level=match.group(2),
            logger_name=match.group(3),
            message=match.group(4),
        )
    return None


def read_log_entries(
    limit: int = 200,
    level_filter: str | None = None,
    search: str | None = None,
    sensitive_strings: dict[str, str] | None = None,
    log_file=None,
) -> tuple[list[LogEntry], int]:
    """Read and parse log entries from ``bamdude.log``, newest first.

    Continuation lines (tracebacks etc.) are folded into the message of the
    entry they belong to. Returns ``(entries, total_lines_in_file)``.
    """
    log_file = log_file if log_file is not None else settings.log_dir / "bamdude.log"
    if not log_file.exists():
        return [], 0

    entries: list[LogEntry] = []
    total_lines = 0

    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            total_lines = len(lines)
            # Whole entries are sanitized before literal search, including
            # historical tracebacks and encoded credentials. Never search raw.
            lines = sanitize_log_content("".join(lines), sensitive_strings).splitlines(keepends=True)

            # Parse lines in reverse order (newest first)
            current_entry: LogEntry | None = None
            multi_line_buffer: list[str] = []

            for line in reversed(lines):
                parsed = parse_log_line(line)
                if parsed:
                    # Found a new log entry start
                    if current_entry:
                        # Apply filters and add previous entry (without multi_line_buffer - it belongs to new entry)
                        should_include = True

                        # Level filter
                        if level_filter and current_entry.level.upper() != level_filter.upper():
                            should_include = False

                        # Search filter (case-insensitive)
                        if search and should_include:
                            search_lower = search.lower()
                            if not (
                                search_lower in current_entry.message.lower()
                                or search_lower in current_entry.logger_name.lower()
                            ):
                                should_include = False

                        if should_include:
                            entries.append(current_entry)

                            if len(entries) >= limit:
                                break

                    # Set new entry and attach any accumulated multi-line content to it
                    # (in reverse order, continuation lines come before their parent entry)
                    current_entry = parsed
                    if multi_line_buffer:
                        current_entry.message += "\n" + "\n".join(reversed(multi_line_buffer))
                    multi_line_buffer = []
                elif line.strip():
                    # Continuation of multi-line log entry (will be attached to next parsed entry)
                    multi_line_buffer.append(line.rstrip())

            # Don't forget the last (oldest) entry
            # Note: any remaining multi_line_buffer would be orphaned lines before the first entry
            if current_entry and len(entries) < limit:
                should_include = True
                if level_filter and current_entry.level.upper() != level_filter.upper():
                    should_include = False
                if search and should_include:
                    search_lower = search.lower()
                    if not (
                        search_lower in current_entry.message.lower()
                        or search_lower in current_entry.logger_name.lower()
                    ):
                        should_include = False
                if should_include:
                    entries.append(current_entry)

    except Exception as e:
        logger.error("Error reading log file: %s", e)
        return [], 0

    # Entries are already in newest-first order
    return entries, total_lines


def sanitize_log_content(content: str, sensitive_strings: dict[str, str] | None = None) -> str:
    """Remove sensitive data from log content.

    ``sensitive_strings`` maps known exact values (printer names, serials, etc.)
    to replacement labels; pass the result of :func:`collect_sensitive_strings`.
    Regex passes additionally redact credentials in URLs, emails, serials, and
    IP addresses that were not captured by exact matching.
    """
    return _sanitize_log_content(content, _known_value_replacer(sensitive_strings or {}))


def _sanitize_log_content(content: str, replace_known) -> str:
    # First, replace known sensitive values (database-aware exact matching)
    # This catches printer names, usernames, and other arbitrary user-chosen strings
    # that regex patterns cannot detect
    content = replace_known(content)

    # Replace credentials in URLs (e.g. http://user:pass@host, rtsps://bblp:code@host).
    #
    # Shares its pattern with the log-pipeline redaction in ``core.logging_filters``
    # so the two cannot drift. The bundle drops the username as well, where the
    # live log keeps it for diagnosis — that is the only difference, and the
    # named groups are what let one pattern serve both.
    #
    # The hand-rolled version this replaces was wrong twice over: its scheme
    # allowlist was ``https?|rtsps?``, so **``ftp://`` and ``ftps://`` were never
    # redacted at all** — and FTPS-to-printer carries the same ``bblp:<access
    # code>``. And its secret class stopped at the FIRST ``@``, so a password
    # containing one (legal, and plausible in an external camera URL) left its
    # tail in the output.
    content = URL_CREDENTIALS_PATTERN.sub(r"\g<scheme>[CREDENTIALS]@", content)
    content = redact_log_credentials(content)
    # The shape pass can decode spelling of another opaque saved credential.
    content = replace_known(content)

    # Replace email addresses
    content = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL]", content)

    # Replace LDAP Distinguished Names — PII on par with email (#2681).
    content = _LDAP_DN_PATTERN.sub("[DN]", content)

    # Replace Bambu Lab printer serial numbers (format: 00M/01D/01S/01P/03W + alphanumeric, 12-16 chars total)
    content = re.sub(r"\b0[0-3][A-Z0-9][A-Z0-9]{9,13}\b", "[SERIAL]", content, flags=re.IGNORECASE)

    # Replace IPv4 addresses (skip firmware versions like 01.09.01.00 which have leading zeros)
    content = IPV4_PATTERN.sub("[IP]", content)

    # Replace paths with usernames
    content = re.sub(r"/home/[^/\s]+/", "/home/[user]/", content)
    content = re.sub(r"/Users/[^/\s]+/", "/Users/[user]/", content)
    content = re.sub(r"/opt/[^/\s]+/", "/opt/[user]/", content)
    content = re.sub(r"(?i)[A-Z]:[\\/]Users[\\/][^\\/\r\n\"']+", r"[HOME]", content)
    content = re.sub(r"(/(?:home|Users)/)[^/\r\n\"']+(?=/|$)", r"\1[user]", content)

    return content


def write_sanitized_log_snapshot(
    source: BinaryIO, target: BinaryIO, size: int, sensitive_strings: dict[str, str]
) -> int:
    """Sanitize a finite file snapshot by complete bounded logical entries.

    File size is captured by the owner before this call. A traceback stays
    with its timestamped parent; overlong physical/logical entries are omitted
    in full, never cut before a secret's terminator and then exposed. Plain
    non-log text is processed by physical line, preserving ordinary downloads.
    No database session or network transfer is owned by this file-only phase.
    """
    physical_budget = 64 * 1024
    entry_budget = 256 * 1024
    remaining = size
    entry: list[str] = []
    entry_size = 0
    omitted = False
    discarded_line = False
    replace_known = _known_value_replacer(sensitive_strings)

    def flush():
        nonlocal entry, entry_size, omitted
        if omitted:
            target.write(b"[omitted oversized log entry]\n")
        elif entry:
            target.write(_sanitize_log_content("".join(entry), replace_known).encode("utf-8"))
        entry, entry_size, omitted = [], 0, False

    while remaining:
        raw = source.readline(min(physical_budget + 1, remaining))
        if not raw:
            raise ValueError("log_snapshot_changed")
        remaining -= len(raw)
        complete = raw.endswith(b"\n") or remaining == 0
        if discarded_line:
            discarded_line = not complete
            continue
        text = raw.decode("utf-8", errors="replace")
        begins_entry = LOG_LINE_PATTERN.match(text) is not None
        if begins_entry:
            flush()
        if len(raw) > physical_budget:
            # Do not publish even the prefix of this entry.
            if not entry:
                entry = [""]
            omitted = True
            discarded_line = not complete
            continue
        if not entry and not begins_entry and not omitted:
            target.write(_sanitize_log_content(text, replace_known).encode("utf-8"))
            continue
        entry_size += len(raw)
        if entry_size > entry_budget:
            entry.clear()
            omitted = True
        elif not omitted:
            entry.append(text)
    flush()
    return target.tell()


async def collect_sensitive_strings(db: AsyncSession) -> dict[str, str]:
    """Collect known sensitive values from the database for log redaction.

    Covers printer names, serial numbers, IP addresses, access codes, auth
    usernames, and the Bambu Cloud email. Pass the result to
    :func:`sanitize_log_content`.
    """
    sensitive_strings: dict[str, str] = {}

    # Printer names, serial numbers, IP addresses, and access codes
    result = await db.execute(select(Printer.name, Printer.serial_number, Printer.ip_address, Printer.access_code))
    for name, serial, ip_address, access_code in result.all():
        if name:
            sensitive_strings[name] = "[PRINTER]"
        if serial:
            sensitive_strings[serial] = "[SERIAL]"
        if ip_address:
            sensitive_strings[ip_address] = "[IP]"
        if access_code:
            sensitive_strings[access_code] = "[ACCESS_CODE]"

    # Auth usernames
    result = await db.execute(select(User.username))
    for (username,) in result.all():
        if username:
            sensitive_strings[username] = "[USER]"

    # Bambu Cloud email
    result = await db.execute(select(Settings.value).where(Settings.key == "bambu_cloud_email"))
    cloud_email = result.scalar_one_or_none()
    if cloud_email:
        sensitive_strings[cloud_email] = "[EMAIL]"

    return sensitive_strings


# Settings use the shared support-bundle policy. Other credential stores use
# explicit column/config projections, never ORM dumps or recovered API-key hashes.
USER_CREDENTIAL_FIELDS = (
    "cloud_token",
    "cloud_refresh_token",
    "orca_cloud_token",
    "orca_cloud_refresh_token",
    "orca_cloud_pending_verifier",
    "orca_cloud_pending_state",
)
PROVIDER_SECRET_FIELDS = frozenset(
    {
        "bot_token",
        "webhook_url",
        "password",
        "username",
        "auth_token",
        "apikey",
        "api_key",
        "user_key",
        "app_token",
        "device_key",
    }
)


async def collect_diagnostic_sensitive_strings(db: AsyncSession) -> dict[str, str]:
    from backend.app.core.encryption import mfa_decrypt
    from backend.app.models.cloud_link import CloudLink
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.oidc_provider import OIDCProvider
    from backend.app.models.smart_plug import SmartPlug

    values = await collect_sensitive_strings(db)

    def add(value):
        if isinstance(value, str) and value:
            # Do not discard short exact credentials like the general PII pass.
            values[value] = "[REDACTED]"

    def add_url(value):
        add(value)
        if value:
            try:
                parts = urlsplit(value)
                add(unquote(parts.username or ""))
                add(unquote(parts.password or ""))
            except ValueError:
                pass  # The malformed full value is still protected.

    def add_payload(value):
        if isinstance(value, str) and redact_setting_in_log("rest_config", value):
            add(value)
        if not value:
            return
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return  # A non-JSON body is a valid existing plug configuration.

        def walk(node):
            if isinstance(node, str):
                if redact_setting_in_log("rest_config", node):
                    add(node)
            elif isinstance(node, dict):
                for item in node.values():
                    walk(item)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(parsed)

    for key, value in (
        await db.execute(select(Settings.key, Settings.value).where(sensitive_settings_predicate(Settings.key)))
    ).all():
        if isinstance(value, str) and redact_setting_in_log(key, value):
            add(value)
    for row in (await db.execute(select(*(getattr(User, name) for name in USER_CREDENTIAL_FIELDS)))).all():
        for value in row:
            add(value)
    for encrypted in (await db.scalars(select(CloudLink.instance_secret_encrypted))).all():
        if encrypted:
            add(mfa_decrypt(encrypted))
    add(os.environ.get("HA_TOKEN"))  # existing settings reader's only credential env override
    for username, password in (await db.execute(select(SmartPlug.username, SmartPlug.password))).all():
        add(username)
        add(password)
    for row in (
        await db.execute(
            select(
                SmartPlug.rest_on_url,
                SmartPlug.rest_off_url,
                SmartPlug.rest_status_url,
                SmartPlug.rest_power_url,
                SmartPlug.rest_energy_url,
                SmartPlug.rest_headers,
                SmartPlug.rest_on_body,
                SmartPlug.rest_off_body,
            )
        )
    ).all():
        for value in row[:5]:
            add_url(value)
        for value in row[5:]:
            add_payload(value)
    for row in (await db.execute(select(Printer.external_camera_url, Printer.external_camera_snapshot_url))).all():
        for value in row:
            add_url(value)
    for raw in (await db.scalars(select(NotificationProvider.config))).all():
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("invalid_secret_configuration")
        for name in PROVIDER_SECRET_FIELDS:
            add(config.get(name))
        # ntfy topic is a capability path; redact the configured complete URL too.
        if config.get("server") and config.get("topic"):
            add(str(config["server"]).rstrip("/") + "/" + str(config["topic"]))
    for encrypted in (await db.scalars(select(OIDCProvider._client_secret_enc))).all():
        add(mfa_decrypt(encrypted) if encrypted.startswith("fernet:") else encrypted)
    return values
