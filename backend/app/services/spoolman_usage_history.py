"""Per-print usage history of Spoolman spools — the ONE writer (m228, upstream 1d86cff5a).

Spoolman keeps a spool's totals and no per-print log, so BamDude records its
own row for every print charge that LANDED in Spoolman. The rows live in
``spoolman_usage_history``, a table of their own: built-in spool ids and
Spoolman spool ids are two independent numberings, and they never share a key
space (upstream 704919339 was exactly that collision).

**Which Spoolman.** A row carries a *source token*. The token is this
installation's name for "the Spoolman database at this address": it is stored
in Settings (``spoolman_usage_source``) together with the address it was made
for, created on first use, and ROTATED whenever the configured ``spoolman_url``
is a different address (compared trimmed, without a trailing slash, case
folded) — checked lazily by :func:`source_token`, so every door that changes
the address is covered without hooking any of them. The setting remembers the
token of every address it has named, so going back to one (A → B → A) finds
its history again instead of starting a third source. Readers show only the
current token's rows; a rotated-away token's rows stay in the table as history
of the previous Spoolman and are shown nowhere. Spoolman itself offers no
database identity to bind to (``GET /info`` names a version and paths), so two
cases need the operator: a host rename of the SAME database
(:func:`keep_previous_source` moves the previous token's rows to the current
one) and a new database behind the SAME address (:func:`rotate_source`).

**Which spool.** Inside one Spoolman an id can be handed to a new spool after a
delete. A row therefore also keeps Spoolman's ``registered`` stamp of the spool
it charged; :func:`belongs_to` is the one rule readers use: same id AND the
same stamp — or, for a row whose spool Spoolman did not hand over at charge
time, not older than the spool's registration (upstream's guard).

Nothing here ever writes to Spoolman: clearing or deleting history is
bookkeeping, and the spool's weight in Spoolman is not changed by it.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database
from backend.app.models.settings import Settings
from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory
from backend.app.services.spoolman_weights import spool_net_weight

logger = logging.getLogger(__name__)

SOURCE_SETTING = "spoolman_usage_source"


@dataclass(frozen=True)
class Charge:
    """One landed print charge, as ``spoolman_tracking._PrintCost.add`` observed it."""

    spool_id: int
    grams: float
    # Per gram, by the charge's own price rule; None for an unpriced spool.
    rate: float | None
    # The Spoolman spool row when the charge path had it; None when it did not.
    spool: dict | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_url(url: str | None) -> str:
    """The address a token is bound to: trimmed, no trailing slash, case folded."""
    return (url or "").strip().rstrip("/").casefold()


async def _read_source(db: AsyncSession) -> tuple[Settings | None, dict]:
    row = (await db.execute(select(Settings).where(Settings.key == SOURCE_SETTING))).scalar_one_or_none()
    if row is None:
        return None, {}
    try:
        data = json.loads(row.value or "")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
        data = {}
    return row, data


async def _write_source(db: AsyncSession, row: Settings | None, data: dict) -> None:
    value = json.dumps(data, sort_keys=True)
    if row is None:
        db.add(Settings(key=SOURCE_SETTING, value=value))
    else:
        row.value = value
    await db.commit()


async def _configured_url(db: AsyncSession) -> str:
    raw = (await db.execute(select(Settings.value).where(Settings.key == "spoolman_url"))).scalar_one_or_none()
    return normalize_url(raw)


async def source_token(db: AsyncSession) -> str | None:
    """The current Spoolman's token, creating or rotating it as the address demands.

    None only when no token exists yet and no address is configured — there is
    no Spoolman to name. A cleared address never rotates: the token stays bound
    to the last address, so switching Spoolman off and on again keeps the
    history. Commits on ``db`` only when it created or rotated the token.
    """
    row, data = await _read_source(db)
    url = await _configured_url(db)
    if data:
        if not url or url == data.get("url"):
            return data["token"]
        # A different address: a different Spoolman until the operator says
        # otherwise — unless it is an address this installation has named
        # before, whose token (and history) it gets back: A → B → A is a
        # mistyped host put right, not a third Spoolman.
        leaving = {"token": data["token"], "url": data.get("url") or ""}
        known = _known(data)
        if leaving["url"]:
            known[leaving["url"]] = leaving["token"]
        token = known.get(url) or uuid.uuid4().hex
        known.pop(url, None)
        known[url] = token
        # The one "previous" the Settings card can offer: the source just left,
        # unless it recorded nothing and an earlier one — not the one now
        # current again — still holds rows.
        previous = leaving
        earlier = data.get("previous") if isinstance(data.get("previous"), dict) else None
        if (
            earlier
            and earlier.get("token") not in (None, token, leaving["token"])
            and not await _row_count(db, leaving["token"])
            and await _row_count(db, earlier["token"])
        ):
            previous = earlier
        data = {"token": token, "url": url, "previous": previous, "known": _trimmed(known)}
        await _write_source(db, row, data)
        logger.info("[SPOOLMAN] The Spoolman address changed: usage history follows the address")
        return data["token"]
    if not url:
        return None
    data = {"token": uuid.uuid4().hex, "url": url, "known": {}}
    data["known"][url] = data["token"]
    try:
        await _write_source(db, row, data)
    except IntegrityError:
        # Two first uses at once: the other one named the source. Use its name.
        await db.rollback()
        _row, data = await _read_source(db)
        return data.get("token")
    return data["token"]


# How many addresses the setting remembers a token for; the oldest go first.
KNOWN_ADDRESSES = 20


def _known(data: dict) -> dict[str, str]:
    """address → token, for every address this installation has named."""
    known = data.get("known")
    if not isinstance(known, dict):
        return {}
    return {url: token for url, token in known.items() if isinstance(url, str) and isinstance(token, str) and token}


def _trimmed(known: dict[str, str]) -> dict[str, str]:
    return dict(list(known.items())[-KNOWN_ADDRESSES:])


async def _row_count(db: AsyncSession, token: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count()).select_from(SpoolmanUsageHistory).where(SpoolmanUsageHistory.source_token == token)
            )
        ).scalar_one()
    )


async def source_state(db: AsyncSession) -> dict:
    """What the Settings card needs: is there a previous source, and how much it holds."""
    token = await source_token(db)
    _row, data = await _read_source(db)
    previous = data.get("previous") if isinstance(data.get("previous"), dict) else None
    previous_rows = 0
    if previous and previous.get("token"):
        previous_rows = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(SpoolmanUsageHistory)
                    .where(SpoolmanUsageHistory.source_token == previous["token"])
                )
            ).scalar_one()
        )
    rows = 0
    if token:
        rows = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(SpoolmanUsageHistory)
                    .where(SpoolmanUsageHistory.source_token == token)
                )
            ).scalar_one()
        )
    return {
        "has_source": token is not None,
        "rows": rows,
        "previous_url": ((previous or {}).get("url") or None) if previous_rows else None,
        "previous_rows": previous_rows,
    }


async def keep_previous_source(db: AsyncSession) -> int:
    """ "The new address is the SAME Spoolman database": its history comes along.

    Moves the previous token's rows to the current one and forgets the previous
    source. Returns how many rows moved. Commits.
    """
    token = await source_token(db)
    row, data = await _read_source(db)
    previous = data.get("previous") if isinstance(data.get("previous"), dict) else None
    if token is None or not previous or not previous.get("token"):
        return 0
    result = await db.execute(
        update(SpoolmanUsageHistory)
        .where(SpoolmanUsageHistory.source_token == previous["token"])
        .values(source_token=token)
    )
    data.pop("previous", None)
    # The old address names the same database: going back to it finds the rows.
    data["known"] = {url: token if held == previous["token"] else held for url, held in _known(data).items()}
    await _write_source(db, row, data)
    return int(result.rowcount or 0)


async def rotate_source(db: AsyncSession) -> None:
    """ "This is a NEW Spoolman database behind the same address": history starts over.

    The rows recorded so far stay as the previous source's. Commits.
    """
    token = await source_token(db)
    if token is None:
        return
    row, data = await _read_source(db)
    url = data.get("url") or ""
    fresh = uuid.uuid4().hex
    known = _known(data)
    if url:
        # The address names the new database from now on, after a round trip too.
        known[url] = fresh
    data = {"token": fresh, "url": url, "previous": {"token": token, "url": url}, "known": _trimmed(known)}
    await _write_source(db, row, data)


def belongs_to(row_registered: str | None, row_created_at: datetime | None, spool_registered: str | None) -> bool:
    """Whether a row recorded under a spool id is THIS spool's, not an earlier holder's.

    The row's own ``registered`` stamp decides when it has one. Without one
    (Spoolman did not hand the spool over at charge time) a row from before the
    spool was registered is an earlier spool's — upstream's guard. With nothing
    to compare, the row stays.
    """
    if not spool_registered:
        return True
    registered = _instant(spool_registered)
    if registered is None:
        return True
    if row_registered:
        own = _instant(row_registered)
        return own is None or own == registered
    return row_created_at is None or row_created_at >= registered


def _instant(value: str | None) -> datetime | None:
    """A Spoolman timestamp as naive UTC, or None when it is not one."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _snapshot(spool: dict | None) -> dict:
    """The spool's SKU fields as the inventory list shows them, or nothing."""
    if not isinstance(spool, dict):
        return {}
    from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool  # noqa: PLC0415

    try:
        mapped = _map_spoolman_spool(spool)
    except (ValueError, TypeError, AttributeError):
        return {}

    def _text(value, limit: int) -> str | None:
        return str(value)[:limit] if value else None

    registered = spool.get("registered")
    return {
        "spool_registered": _text(registered, 40),
        "material": _text(mapped.get("material"), 100),
        "subtype": _text(mapped.get("subtype"), 200),
        "brand": _text(mapped.get("brand"), 200),
        "color_name": _text(mapped.get("color_name"), 200),
        "rgba": _text(mapped.get("rgba"), 8),
        "slicer_filament_name": _text(mapped.get("slicer_filament_name"), 200),
        "label_weight": spool_net_weight(spool),
    }


async def record_print(
    *,
    printer_id: int | None,
    archive_id: int | None,
    status: str,
    charges: list[Charge],
) -> int:
    """One row per landed charge of one print. Returns how many were written.

    Runs in a short transaction of its own, after every charge has landed: it
    observes them and can change none. The caller catches whatever this raises
    — a history that could not be written never fails or alters a charge.
    """
    charges = [c for c in charges if c.grams > 0 and c.spool_id]
    if not charges:
        return 0

    from backend.app.models.archive import PrintArchive  # noqa: PLC0415

    async with database.async_session() as db:
        token = await source_token(db)
        if token is None:
            return 0
        known_archive = archive_id if archive_id is not None and archive_id > 0 else None
        print_name = None
        if known_archive is not None:
            print_name = (
                await db.execute(select(PrintArchive.print_name).where(PrintArchive.id == known_archive))
            ).scalar_one_or_none()
        now = _utc_now()
        for charge in charges:
            net = spool_net_weight(charge.spool)
            db.add(
                SpoolmanUsageHistory(
                    source_token=token,
                    spoolman_spool_id=int(charge.spool_id),
                    printer_id=printer_id,
                    print_name=print_name,
                    archive_id=known_archive,
                    weight_used=charge.grams,
                    percent_used=int(round(charge.grams / net * 100)) if net else 0,
                    status=(status or "completed")[:20],
                    cost=round(charge.grams * charge.rate, 2) if charge.rate is not None else None,
                    created_at=now,
                    **_snapshot(charge.spool),
                )
            )
        await db.commit()
        return len(charges)


async def clear_for_spool(db: AsyncSession, spool_id: int, spool_registered: str | None) -> int:
    """Forget this spool's rows of the current source. Spoolman's totals are untouched.

    Only the rows :func:`belongs_to` gives this spool — an earlier holder of
    the id keeps its history. The caller commits.
    """
    token = await source_token(db)
    if token is None:
        return 0
    rows = (
        await db.execute(
            select(SpoolmanUsageHistory.id, SpoolmanUsageHistory.spool_registered, SpoolmanUsageHistory.created_at)
            .where(SpoolmanUsageHistory.source_token == token)
            .where(SpoolmanUsageHistory.spoolman_spool_id == spool_id)
        )
    ).all()
    ids = [r.id for r in rows if belongs_to(r.spool_registered, r.created_at, spool_registered)]
    if ids:
        await db.execute(delete(SpoolmanUsageHistory).where(SpoolmanUsageHistory.id.in_(ids)))
    return len(ids)


async def delete_row(db: AsyncSession, spool_id: int, usage_id: int) -> bool:
    """Forget one row of the current source. Spoolman's totals are untouched. The caller commits."""
    token = await source_token(db)
    if token is None:
        return False
    result = await db.execute(
        delete(SpoolmanUsageHistory)
        .where(SpoolmanUsageHistory.id == usage_id)
        .where(SpoolmanUsageHistory.spoolman_spool_id == spool_id)
        .where(SpoolmanUsageHistory.source_token == token)
    )
    return bool(result.rowcount)


async def archives_with_usage(db: AsyncSession, archive_ids: list[int]) -> set[int]:
    """Which of these archives have a usage row here — a yes/no per print, no row is shown.

    For the archive list's «this cost is one full print's» mark: a print with a row
    in either ledger was measured. Not fenced to Spoolman mode on purpose — the
    question is whether the run was ever measured, whatever inventory is in use now.
    """
    if not archive_ids:
        return set()
    rows = await db.execute(
        select(SpoolmanUsageHistory.archive_id).where(SpoolmanUsageHistory.archive_id.in_(archive_ids)).distinct()
    )
    return {archive_id for archive_id in rows.scalars() if archive_id is not None}


async def detach_archive(db: AsyncSession, archive_id: int) -> None:
    """A deleted archive leaves its rows without a link (SQLite runs no FK actions,
    and on PostgreSQL the reference would block the delete). The caller commits."""
    await detach_archives(db, [archive_id])


async def detach_archives(db: AsyncSession, archive_ids: list[int]) -> None:
    """The same for a bulk archive delete, which never passes ``delete_archive``
    (a user deleted with their items, the trash sweeper's leftover delete). The
    caller commits."""
    ids = [int(i) for i in archive_ids]
    for start in range(0, len(ids), 500):
        await db.execute(
            update(SpoolmanUsageHistory)
            .where(SpoolmanUsageHistory.archive_id.in_(ids[start : start + 500]))
            .values(archive_id=None)
        )


async def detach_printer(db: AsyncSession, printer_id: int) -> None:
    """A deleted printer leaves its rows without one. ``SET NULL`` is the
    PostgreSQL backstop; SQLite runs no FK actions and hands the id to the next
    printer, which would be shown as these charges' printer. The caller commits."""
    await db.execute(
        update(SpoolmanUsageHistory).where(SpoolmanUsageHistory.printer_id == printer_id).values(printer_id=None)
    )


# --- The Git backup's two doors (upstream 1d86cff5a) -------------------------
#
# The rows travel in a portable form: no row id, no local printer or archive
# id as authority, no user, and no source token — the token is this
# installation's own name for its Spoolman and means nothing anywhere else. A
# row says only whether it belonged to the exporting side's CURRENT Spoolman;
# a restore binds such rows to the restoring side's current token.

_SNAPSHOT_TEXT = {
    "material": 100,
    "subtype": 200,
    "brand": 200,
    "color_name": 200,
    "rgba": 8,
    "slicer_filament_name": 200,
}


async def _current_token_without_writing(db: AsyncSession) -> str | None:
    """The token :func:`source_token` would answer, without creating or rotating one.

    A backup observes: it must not be the thing that names or rotates a source.
    An address changed and not used yet answers the token that address is
    already known by, or None — the stored rows are then the ones of the
    address that was left.
    """
    _row, data = await _read_source(db)
    if not data:
        return None
    url = await _configured_url(db)
    if not url or url == data.get("url"):
        return data["token"]
    return _known(data).get(url)


async def export_rows(db: AsyncSession) -> list[dict]:
    """Every row in its portable form, oldest first. Reads only.

    A printer is named by its serial number. ``archive_id`` is the archive's id
    INSIDE the backup — a reference into the backup's own archive list, which a
    restore resolves to the archive that list became locally; it is never read
    as a local id.
    """
    from backend.app.models.printer import Printer  # noqa: PLC0415

    current = await _current_token_without_writing(db)
    serials = dict((await db.execute(select(Printer.id, Printer.serial_number))).all())
    rows = (
        (
            await db.execute(
                select(SpoolmanUsageHistory).order_by(SpoolmanUsageHistory.created_at, SpoolmanUsageHistory.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "spoolman_spool_id": row.spoolman_spool_id,
            "spool_registered": row.spool_registered,
            "current_source": current is not None and row.source_token == current,
            "printer_serial": serials.get(row.printer_id) if row.printer_id is not None else None,
            "archive_id": row.archive_id,
            "print_name": row.print_name,
            "weight_used": row.weight_used,
            "percent_used": row.percent_used,
            "status": row.status,
            "cost": row.cost,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "material": row.material,
            "subtype": row.subtype,
            "brand": row.brand,
            "color_name": row.color_name,
            "rgba": row.rgba,
            "slicer_filament_name": row.slicer_filament_name,
            "label_weight": row.label_weight,
        }
        for row in rows
    ]


@dataclass
class RestoreOutcome:
    """What a restore of exported rows did, entry by entry."""

    restored: int = 0
    # Already there: the same charge, by :func:`_identity`.
    present: int = 0
    # Not an entry this table can hold (no spool id, no moment, no weight).
    malformed: int = 0
    # Rows of a Spoolman the exporting side had already left: nobody's here.
    other_source: int = 0
    # Skipped because this installation has no Spoolman to name.
    no_source: int = 0
    # Restored, but the archive the row named did not come with the restore.
    unlinked: int = 0


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _identity(spool_id: int, registered: str | None, created_at: datetime, grams: float, status: str, name) -> tuple:
    """What makes a restored charge "the one already here", within one source.

    The spool (its id and Spoolman's registration stamp), the moment the writer
    stamped, the grams, the outcome and the print's name. Two charges equal in
    all of these are counted, not merged (see :func:`restore_rows`).
    """
    return (spool_id, registered or None, created_at, round(grams, 6), status, name or None)


def _restorable(entry) -> dict | None:
    """The columns an exported entry gives, or None when it is not a charge."""
    if not isinstance(entry, dict):
        return None
    spool_id = entry.get("spoolman_spool_id")
    if isinstance(spool_id, bool) or not isinstance(spool_id, int) or spool_id <= 0:
        return None
    raw_moment = entry.get("created_at")
    created_at = _instant(raw_moment) if isinstance(raw_moment, str) else None
    grams = _number(entry.get("weight_used"))
    if created_at is None or grams is None or grams < 0:
        return None

    def text(key: str, limit: int) -> str | None:
        value = entry.get(key)
        return value[:limit] if isinstance(value, str) and value else None

    percent = entry.get("percent_used")
    return {
        "spoolman_spool_id": spool_id,
        "spool_registered": text("spool_registered", 40),
        "print_name": text("print_name", 500),
        "weight_used": grams,
        "percent_used": percent if isinstance(percent, int) and not isinstance(percent, bool) else 0,
        "status": text("status", 20) or "completed",
        "cost": _number(entry.get("cost")),
        "created_at": created_at,
        "label_weight": _number(entry.get("label_weight")),
        **{key: text(key, limit) for key, limit in _SNAPSHOT_TEXT.items()},
    }


async def restore_rows(
    db: AsyncSession,
    entries: list,
    *,
    archive_ids: dict[int, int],
    printer_ids: dict[str, int],
) -> RestoreOutcome:
    """Write exported rows under THIS installation's current source. The caller commits.

    ``archive_ids`` maps an archive's id inside the backup to the local archive
    the same restore resolved it to; ``printer_ids`` maps a serial number to
    the local printer. A reference neither names stays empty — a row is never
    attached by a number carried over from another database.

    A row already here is not written again: rows of the current source are
    counted by :func:`_identity`, and each incoming entry spends one — so two
    equal charges come back as two, and a second restore adds none. A malformed
    entry is counted and skipped; nothing here raises for one.
    """
    outcome = RestoreOutcome()
    token = await source_token(db)
    if token is None:
        outcome.no_source = len(entries)
        return outcome

    here: dict[tuple, int] = {}
    existing = await db.execute(
        select(
            SpoolmanUsageHistory.spoolman_spool_id,
            SpoolmanUsageHistory.spool_registered,
            SpoolmanUsageHistory.created_at,
            SpoolmanUsageHistory.weight_used,
            SpoolmanUsageHistory.status,
            SpoolmanUsageHistory.print_name,
        ).where(SpoolmanUsageHistory.source_token == token)
    )
    for row in existing.all():
        key = _identity(*row)
        here[key] = here.get(key, 0) + 1

    for entry in entries:
        fields = _restorable(entry)
        if fields is None:
            outcome.malformed += 1
            continue
        if entry.get("current_source") is not True:
            outcome.other_source += 1
            continue
        key = _identity(
            fields["spoolman_spool_id"],
            fields["spool_registered"],
            fields["created_at"],
            fields["weight_used"],
            fields["status"],
            fields["print_name"],
        )
        if here.get(key):
            here[key] -= 1
            outcome.present += 1
            continue
        serial = entry.get("printer_serial")
        named = entry.get("archive_id")
        named = named if isinstance(named, int) and not isinstance(named, bool) else None
        archive_id = archive_ids.get(named) if named is not None else None
        if named is not None and archive_id is None:
            outcome.unlinked += 1
        db.add(
            SpoolmanUsageHistory(
                source_token=token,
                printer_id=printer_ids.get(serial) if isinstance(serial, str) else None,
                archive_id=archive_id,
                **fields,
            )
        )
        outcome.restored += 1
    return outcome
