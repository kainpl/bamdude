"""Cloud Link store — the only writer of the three ``cloud_link_*`` tables.

Everything the link persists goes through here: the pairing row, the instance
secret, the allowlist of printers the portal may see, and the audit of what
crossed. A store rather than direct ORM use at each callsite because the
callers do not share a transaction — the settings route holds a request
session, the connect loop holds its own, and a command handler holds a third.
Each function therefore takes the session it should use and **commits its own
write**, so a fact is durable the moment the function returns and no caller has
to know who else was mid-flight.

Two consequences worth naming:

* Reads never write. ``get_secret`` and ``get_publish_set`` answer from what is
  there and create nothing — only ``get_config`` (and the writers that go
  through it) will materialise the singleton row.
* The secret is only ever handled here. It arrives as plaintext, goes to disk
  as Fernet ciphertext via ``core/encryption``, and comes back out through
  ``get_secret``. Nothing else in the codebase should touch
  ``instance_secret_encrypted``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.encryption import mfa_decrypt, mfa_encrypt
from backend.app.models.cloud_link import (
    CLOUD_LINK_ROW_ID,
    CloudLink,
    CloudLinkAudit,
    CloudLinkPrinter,
)

logger = logging.getLogger(__name__)

#: Schemes a portal may be reached over when it is not on this machine. Both
#: are TLS, and that is the whole rule: the link carries the instance secret
#: and every command the portal sends, so plain http across a network hands
#: both to anything in the path.
PUBLIC_SCHEMES = frozenset({"https", "wss"})

#: Hosts that ARE this machine. A developer running the portal on
#: ``http://localhost:3002`` is not crossing a network, so demanding TLS there
#: buys no secrecy and costs a self-signed certificate in every dev setup.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: How long the audit is kept by default. It is the operator's only record of
#: what the portal saw, so the sweep is bounded by time and by nothing else —
#: a row cap would throw away a busy day and keep a quiet month.
DEFAULT_AUDIT_RETENTION_DAYS = 30


def _utcnow() -> datetime:
    """Naive UTC, because the columns are naive.

    ``ts`` is ``DateTime`` without a timezone. Handing SQLAlchemy an aware
    value works in the application (the engine strips the offset in
    ``core/database``) but not on an engine built without that listener — such
    as the test harness — where SQLite would then string-compare
    ``'...+00:00'`` against a bare timestamp and quietly get the ordering
    wrong. Stripping here makes the comparison right on every engine.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------- config


async def get_config(session: AsyncSession) -> CloudLink:
    """The pairing row, created with the model's defaults if it is not there.

    Get-or-create because every caller needs the row and none of them owns
    making it: a fresh install, the settings page and the connect loop all
    arrive at an empty table. The defaults are deliberately NOT repeated here —
    they live on the model, so "an upgrade must never switch the link on" is
    stated in exactly one place.

    The ``IntegrityError`` branch is for two callers racing on a fresh
    install (the startup loop and the first settings request). The primary key
    is a fixed ``1``, so the loser of the race is told so by the database and
    simply reads what the winner wrote.
    """
    row = await session.get(CloudLink, CLOUD_LINK_ROW_ID)
    if row is not None:
        return row

    row = CloudLink(id=CLOUD_LINK_ROW_ID)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.get(CloudLink, CLOUD_LINK_ROW_ID)
        if existing is None:
            raise
        return existing
    return row


async def save_credentials(session: AsyncSession, instance_id: str, secret: str) -> CloudLink:
    """Store a freshly issued pairing, encrypting the secret at rest.

    ⚠️ This also clears ``revoked`` and ``last_error``: **a fresh pair is a
    fresh start.** Both fields describe the credential that was just replaced,
    and left standing they would tell the settings page the farm is revoked
    while it holds a brand-new working credential — sending the user off to
    re-pair a link that is already paired.

    ``enabled`` is deliberately untouched. Holding a credential and choosing to
    use it are two decisions, and pairing must not be a back door into the one
    the user makes with the switch.
    """
    row = await get_config(session)
    row.instance_id = instance_id
    row.instance_secret_encrypted = mfa_encrypt(secret)
    row.revoked = False
    row.last_error = None
    await session.commit()
    return row


async def get_secret(session: AsyncSession) -> str | None:
    """The decrypted instance secret, or ``None`` when this farm is not paired.

    Reads the row directly rather than through :func:`get_config` so that
    asking "are we paired" never writes — this runs on the connect path, which
    can fire while a migration holds the database.

    A decryption failure is raised, not swallowed: ``mfa_decrypt`` only fails
    when the encryption key changed under a stored secret, and returning
    ``None`` there would read as "not paired" and send the user to re-pair
    without ever telling them the key is the problem.
    """
    row = await session.get(CloudLink, CLOUD_LINK_ROW_ID)
    if row is None or not row.instance_secret_encrypted:
        return None
    return mfa_decrypt(row.instance_secret_encrypted)


async def clear_credentials(session: AsyncSession) -> CloudLink:
    """Forget the pairing — there is nothing left to reconnect with.

    Only the two credential fields are wiped. ``revoked`` and ``last_error``
    stay: they are the record of *why* the link ended, and they are what the
    settings page shows the user after an unpair they did not initiate.
    """
    row = await get_config(session)
    row.instance_id = None
    row.instance_secret_encrypted = None
    await session.commit()
    return row


# -------------------------------------------------------------- publish set


async def set_publish_set(session: AsyncSession, printer_ids: list[int]) -> set[int]:
    """Replace the allowlist with exactly these printers.

    Replace, not merge: the set the user saved IS the set. A merge would mean
    a printer can only ever be added, and unticking one on the settings page
    would silently do nothing — the worst possible outcome for a control whose
    entire job is to keep a machine off the internet.

    Duplicates in the caller's list are collapsed. ``printer_id`` is the
    primary key, so a repeated id would otherwise be an ``IntegrityError``
    surfacing as a 500 for what is a harmless UI slip.
    """
    wanted = set(printer_ids)
    await session.execute(delete(CloudLinkPrinter))
    session.add_all([CloudLinkPrinter(printer_id=printer_id) for printer_id in sorted(wanted)])
    await session.commit()
    return wanted


async def get_publish_set(session: AsyncSession) -> set[int]:
    """The printer ids the portal may be told about. Empty means none."""
    result = await session.execute(select(CloudLinkPrinter.printer_id))
    return set(result.scalars())


# -------------------------------------------------------------------- audit


async def write_audit(
    session: AsyncSession,
    direction: str,
    kind: str,
    summary: str,
    ok: bool = True,
) -> CloudLinkAudit:
    """Record one notable message. ``direction`` is ``"up"`` or ``"down"``.

    The row is refreshed after the insert so the returned object carries its
    server-stamped ``ts``. Without it the attribute is expired and touching it
    would emit a lazy load from async code — a ``MissingGreenlet`` in whichever
    caller happened to log the timestamp. One extra SELECT per audit row, on a
    path that writes a handful of rows a minute.

    The stamp comes from the database rather than from Python on purpose:
    every row is then on one clock, so the ordering holds even when the rows
    were written by different processes.
    """
    entry = CloudLinkAudit(direction=direction, kind=kind, summary=summary, ok=ok)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def prune_audit(session: AsyncSession, older_than_days: int = DEFAULT_AUDIT_RETENTION_DAYS) -> int:
    """Drop audit rows older than the window. Returns how many went."""
    cutoff = _utcnow() - timedelta(days=older_than_days)
    result = await session.execute(delete(CloudLinkAudit).where(CloudLinkAudit.ts < cutoff))
    await session.commit()
    deleted = result.rowcount or 0
    if deleted:
        logger.info("Cloud Link: pruned %d audit row(s) older than %d day(s)", deleted, older_than_days)
    return deleted


# --------------------------------------------------------------- portal URL


def validate_portal_url(url: str) -> str:
    """Check a portal URL and return it normalised. Raises ``ValueError``.

    Normalisation is deliberately minimal — surrounding whitespace and a
    trailing slash, nothing more. The stored value is what every caller
    concatenates a path onto, so ``.../portal/`` and ``.../portal`` must not be
    two different portals; rewriting anything else (case, default ports) would
    hand the user back a URL they did not type for no gain.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ValueError("Portal URL must not be empty")

    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Portal URL must be absolute, with a scheme and a host — got {candidate!r}")

    host = (parsed.hostname or "").lower()
    if host not in LOOPBACK_HOSTS and parsed.scheme not in PUBLIC_SCHEMES:
        raise ValueError(
            f"Portal URL must use https:// or wss:// — got {parsed.scheme!r}. "
            "Plain http is accepted only for a portal on this machine "
            f"({', '.join(sorted(LOOPBACK_HOSTS))})."
        )

    return candidate.rstrip("/")
