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

import asyncio
import base64
import html
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


# Each regex sees at most one 64 KiB logical entry, never a whole log file.
LOG_ENTRY_BUDGET = 64 * 1024
LOG_IO_CHUNK = 64 * 1024
_file_context = threading.local()


class LogReadUnavailable(RuntimeError):
    pass


def _file_checkpoint():
    cancelled = getattr(_file_context, "cancelled", None)
    deadline = getattr(_file_context, "deadline", None)
    if cancelled is not None and cancelled.is_set() or deadline is not None and time.monotonic() >= deadline:
        raise LogReadUnavailable("cancelled_or_timeout")


class LogFileWork:
    """One file-only job, no polling backlog; cancellation keeps the real slot.

    Workers cooperate between bounded reads/entries. A blocked OS read cannot
    be killed: the same future owns its slot until real exit, even after HTTP
    timeout/disconnect. No DB session is passed into this owner.
    """

    def __init__(self):
        self._slot = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="support-log")
        self._future = None

    async def run(self, function, *args, timeout=30.0, **kwargs):
        if not self._slot.acquire(blocking=False):
            raise LogReadUnavailable("busy")
        cancelled = threading.Event()

        def discard(result):
            snapshot = getattr(result, "snapshot", None)
            if snapshot is not None:
                snapshot.close()

        def work():
            _file_context.cancelled = cancelled
            _file_context.deadline = time.monotonic() + timeout
            try:
                result = function(*args, **kwargs)
                if cancelled.is_set():
                    discard(result)
                return result
            finally:
                _file_context.cancelled = None
                _file_context.deadline = None
                self._slot.release()

        try:
            future = self._executor.submit(work)
        except BaseException:
            self._slot.release()
            raise

        def finished(done):
            if cancelled.is_set() and not done.cancelled() and done.exception() is None:
                discard(done.result())

        future.add_done_callback(finished)
        self._future = future
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(wrapped), timeout)
        except (asyncio.CancelledError, TimeoutError):
            cancelled.set()
            if future.done() and not future.cancelled() and future.exception() is None:
                discard(future.result())
            raise

    async def drain(self):
        if self._future is not None:
            try:
                await asyncio.shield(asyncio.wrap_future(self._future))
            except Exception:
                pass


file_work = LogFileWork()


def _reverse_physical_lines(stream, upper, lower=0):
    """Yield bounded physical lines backwards; oversized prefixes stay private."""
    position, carry, carry_big, newest = upper, b"", False, True
    while position > lower:
        _file_checkpoint()
        count = min(LOG_IO_CHUNK, position - lower)
        position -= count
        stream.seek(position)
        block = stream.read(count)
        if len(block) != count:
            raise LogReadUnavailable("log_changed")
        pieces = (block if carry_big else block + carry).split(b"\n")
        for index in range(len(pieces) - 1, 0, -1):
            raw = pieces[index]
            oversized = carry_big and index == len(pieces) - 1 or len(raw) > LOG_ENTRY_BUDGET
            if newest and not raw and not oversized:
                newest = False  # trailing LF is not an extra physical line
                continue
            complete = not newest
            newest = False
            yield raw[:LOG_ENTRY_BUDGET] + (b"\n" if complete else b""), oversized, complete
        carry_big = carry_big and len(pieces) == 1 or len(pieces[0]) > LOG_ENTRY_BUDGET
        carry = pieces[0][:LOG_ENTRY_BUDGET]
        time.sleep(0)  # no long GIL-owning loop when a search matches nothing
    if lower == 0 and (carry or carry_big):
        yield carry + (b"\n" if not newest else b""), carry_big, not newest


def reverse_log_entries(stream, upper, lower=0):
    """Frame raw parents+continuations before sanitizing, newest first.

    A cut/incomplete/oversized record and orphan continuations are never
    exposed. None is an omission, not a truncated secret-bearing prefix.
    """
    pending, size, bad = [], 0, False
    for raw, oversized, _complete in _reverse_physical_lines(stream, upper, lower):
        _file_checkpoint()
        text = raw.decode("utf-8", errors="replace")
        parent = LOG_LINE_PATTERN.match(text) is not None
        size += len(raw)
        bad = bad or oversized or size > LOG_ENTRY_BUDGET
        if parent:
            yield None if bad else text + "".join(reversed(pending))
            pending, size, bad = [], 0, False
        elif not bad:
            pending.append(text)
        else:
            pending.clear()
    # Unparented prefix is deliberately dropped, also at a byte-tail boundary.


def _physical_line_count(stream, upper):
    count, remaining, last = 0, upper, b""
    stream.seek(0)
    while remaining:
        _file_checkpoint()
        block = stream.read(min(LOG_IO_CHUNK, remaining))
        if not block:
            raise LogReadUnavailable("log_changed")
        remaining -= len(block)
        count += block.count(b"\n")
        last = block[-1:]
        time.sleep(0)
    return count + bool(last and last != b"\n")


def read_log_entries(
    limit: int = 200,
    level_filter: str | None = None,
    search: str | None = None,
    sensitive_strings: dict[str, str] | None = None,
    log_file=None,
) -> tuple[list[LogEntry], int]:
    """Read only needed bounded logical entries; sanitize before filtering."""
    path = log_file if log_file is not None else settings.log_dir / "bamdude.log"
    replace_known = _known_value_replacer(sensitive_strings or {})
    entries = []
    try:
        with open(path, "rb") as stream:
            upper = os.fstat(stream.fileno()).st_size
            total = _physical_line_count(stream, upper)  # cheap bytes.count, no regex/decoded copy
            for raw in reverse_log_entries(stream, upper):
                if raw is None:
                    continue
                header, *continuation = raw.split("\n", 1)
                match = LOG_LINE_PATTERN.match(header.rstrip("\r"))
                # Parse structural fields before replacements can alter [logger].
                continued = continuation[0].rstrip("\r\n") if continuation else ""
                message = match[4] + ("\n" + continued if continued else "")
                message = _sanitize_bounded_entry(message, replace_known)
                name = _sanitize_bounded_entry(match[3], replace_known)
                entry = LogEntry(timestamp=match[1], level=match[2], logger_name=name, message=message)
                if level_filter and entry.level.upper() != level_filter.upper():
                    continue
                if search and search.casefold() not in (name + "\n" + message).casefold():
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
            return entries, total
    except FileNotFoundError:
        return [], 0


def read_sanitized_log_tail(path, sensitive_strings=None, *, max_entries=None, max_bytes=10 * 1024 * 1024):
    """Bounded chronological tail, with a strict sanitized-output byte cap."""
    replace_known = _known_value_replacer(sensitive_strings or {})
    selected, output_size = [], 0
    try:
        with open(path, "rb") as stream:
            upper = os.fstat(stream.fileno()).st_size
            lower = max(0, upper - max_bytes)
            for raw in reverse_log_entries(stream, upper, lower):
                safe = (
                    "[omitted oversized or incomplete log entry]\n"
                    if raw is None
                    else _sanitize_bounded_entry(raw, replace_known)
                )
                encoded = safe.encode("utf-8")
                if output_size + len(encoded) > max_bytes:
                    break
                selected.append(encoded)
                output_size += len(encoded)
                if max_entries is not None and len(selected) >= max_entries:
                    break
    except FileNotFoundError:
        return b""
    return b"".join(reversed(selected))


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


class _EntryTooLarge(Exception):
    pass


def _sanitize_bounded_entry(content, replace_known):
    """Short secrets/invalid UTF-8 must not expand a regex input without bound."""

    def bounded_replace(value):
        result = replace_known(value)
        if len(result.encode("utf-8")) > LOG_ENTRY_BUDGET:
            raise _EntryTooLarge
        return result

    if len(content.encode("utf-8")) > LOG_ENTRY_BUDGET:
        return "[omitted oversized log entry]\n"
    try:
        result = _sanitize_log_content(content, bounded_replace)
    except _EntryTooLarge:
        return "[omitted oversized sanitized log entry]\n"
    return result if len(result.encode("utf-8")) <= LOG_ENTRY_BUDGET else "[omitted oversized sanitized log entry]\n"


def write_sanitized_log_snapshot(
    source: BinaryIO, target: BinaryIO, size: int, sensitive_strings: dict[str, str]
) -> int:
    """Sanitize a finite file snapshot by complete bounded logical entries.

    File size is captured by the owner before this call. A traceback stays
    with its timestamped parent; overlong physical/logical entries are omitted
    in full, never cut before a secret's terminator and then exposed.
    An unparented prefix is one bounded logical entry too, so a multiline
    credential there cannot be separated from its opener. Ordinary small
    plaintext downloads retain their content.
    No database session or network transfer is owned by this file-only phase.
    """
    physical_budget = LOG_ENTRY_BUDGET
    entry_budget = LOG_ENTRY_BUDGET
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
            target.write(_sanitize_bounded_entry("".join(entry), replace_known).encode("utf-8"))
        entry, entry_size, omitted = [], 0, False

    while remaining:
        _file_checkpoint()
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
