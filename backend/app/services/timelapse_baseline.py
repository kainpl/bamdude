"""What was on the printer before this print recorded anything — kept with the archive (G10 C3).

The scan after a print attaches the recording that is NEW since the print
started. "New" needs a boundary: the list of recordings taken at the start. It
used to live in process memory only, so a restart during a print lost it, and
the scan then took a list AFTER the print had ended — a list that already holds
the print's own video, or, read off a card that did not answer, holds nothing
and makes every old video look new.

The boundary is now a fact of the archive: ``extra_data["timelapse_baseline"]``,
written when it is taken and read back by the scan when memory has none.

⚠️ **A stored boundary proves something only at its own address.** The record
names the archive incarnation it was taken for, the printer's immutable
identity and the storage epoch it was taken in (the durable one a restore
rotates — the process's own counter starts over at every boot and would make a
kept record useless). The scan reads it through :func:`read` with the address
it expects; a record of another incarnation, printer or epoch, and a record
from before the address existed, is no boundary. A row reused by a reprint has
its record removed with the rest of the row's reset (:func:`forget_in`), so a
record never outlives the execution it was taken for.

⚠️ **Recordings are told apart by medium and path, not by name.** The record
keeps one entry per recording — storage, path, size, the listing's own time —
and what each medium said (answered, unavailable, not asked). The same file
name on the card and in internal storage is two recordings. And what a medium
said decides about ITS recordings (:func:`media_of`): the listing stops at the
card when the card has videos, so internal storage may never have been asked —
a recording found there later is not known to be new, and the scan leaves it.

Three rules decide whether the scan may attach anything by itself
(:func:`automatic_attach_refusal`); where it may not, the operator's
**Scan for timelapse** stays, and nothing is deleted from the printer:

* **no boundary taken before the recording** — nothing is attached. A list
  taken after the print ended is not one, whatever it shows;
* **a boundary read off storage that did not answer** — nothing is attached,
  not even a single new recording: an unread card is not an empty card;
* a boundary taken **late**, while this same print was still running (after a
  restart), is a boundary: what existed then is not new. It may include the
  print's own unfinished file, in which case nothing is new and nothing is
  attached — the safe direction.

⚠️ **A later capture never destroys a proven one** (:func:`save`). The start
boundary that was read off answering storage stays when a capture after a
restart could not read the card, and when a late capture succeeds: the earlier
list is the stronger one. A late capture only fills a gap — no record, a
record of another address, or one that was never trusted.

A name that merely resembles the print is never a reason to attach.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY = "timelapse_baseline"
VERSION = 2

TAKEN_AT_START = "start"
#: Taken while the print was already running — after a restart of this process.
TAKEN_LATE = "late"

NO_BOUNDARY = "no_baseline"
UNREAD_BOUNDARY = "untrusted_baseline"

_ADDRESS_FIELDS = ("v", "archive_identity", "printer_identity", "storage_epoch")


@dataclass(frozen=True)
class Address:
    """Where a boundary belongs: the archive incarnation, the printer and the durable storage epoch."""

    archive_identity: str
    printer_identity: str
    storage_epoch: str | None


def record(
    names: set[str],
    *,
    trusted: bool,
    taken: str,
    entries: list[dict] | None = None,
    media: dict[str, str] | None = None,
    address: Address | None = None,
) -> dict:
    """The baseline as it is stored. Sorted, so two equal baselines are equal JSON.

    ``entries`` — the listed recordings (``path`` / ``name`` / ``size`` /
    ``mtime``); ``media`` — what each medium said. Without ``address`` the
    record is structurally a baseline and proves nothing to :func:`read` with
    an expected address.
    """
    from backend.app.services.timelapse_files import recording_key, recording_storage  # noqa: PLC0415

    stored: dict = {
        "names": sorted(n for n in names if isinstance(n, str)),
        "trusted": bool(trusted),
        "taken": taken,
        "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
    }
    if entries is not None:
        described = []
        for entry in entries:
            path = entry.get("path") or f"/timelapse/{entry.get('name', '')}"
            size = entry.get("size")
            mtime = entry.get("mtime")
            described.append(
                {
                    "key": recording_key(entry),
                    "storage": recording_storage(path),
                    "path": path,
                    "name": entry.get("name", ""),
                    "size": size if isinstance(size, int) and not isinstance(size, bool) else None,
                    "mtime": str(mtime) if mtime is not None else None,
                }
            )
        stored["entries"] = sorted(described, key=lambda e: e["key"])
        stored["media"] = dict(sorted((media or {}).items()))
    if address is not None:
        stored["v"] = VERSION
        stored["archive_identity"] = address.archive_identity
        stored["printer_identity"] = address.printer_identity
        stored["storage_epoch"] = address.storage_epoch
    return stored


def _parse(stored) -> tuple[set[str], bool] | None:
    if not isinstance(stored, dict):
        return None
    names = stored.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    if stored.get("taken") not in (TAKEN_AT_START, TAKEN_LATE) or not isinstance(stored.get("trusted"), bool):
        return None
    entries = stored.get("entries")
    if entries is None:
        return set(names), stored["trusted"]
    if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("key"), str) for e in entries):
        return None
    return {e["key"] for e in entries}, stored["trusted"]


def _addressed_to(stored: dict, address: Address) -> bool:
    return (
        stored.get("v") == VERSION
        and isinstance(stored.get("entries"), list)
        and "storage_epoch" in stored
        and stored.get("archive_identity") == address.archive_identity
        and stored.get("printer_identity") == address.printer_identity
        and stored.get("storage_epoch") == address.storage_epoch
    )


def read(extra_data: dict | None, *, address: Address | None = None) -> tuple[set[str], bool] | None:
    """``(recordings, trusted)`` from an archive's metadata, or ``None`` when it holds no usable baseline.

    ``recordings`` are :func:`timelapse_files.recording_key` values (bare names
    for a record from before entries were kept). With ``address`` — how the scan
    reads — the record must be addressed to exactly it; anything else, and
    anything malformed, is no baseline: a guess here would be a guess about
    which video is somebody's.
    """
    stored = (extra_data or {}).get(KEY)
    parsed = _parse(stored)
    if parsed is None:
        return None
    if address is not None and not _addressed_to(stored, address):
        return None
    return parsed


def media_of(extra_data: dict | None) -> dict | None:
    """What each medium said when the stored boundary was taken, or ``None`` for a record that does not say."""
    stored = (extra_data or {}).get(KEY)
    media = stored.get("media") if isinstance(stored, dict) else None
    if not isinstance(media, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in media.items()):
        return None
    return dict(media)


def automatic_attach_refusal(baseline: set[str] | None, trusted: bool) -> str | None:
    """Why the scan after a print must not attach anything by itself, or ``None`` when it may."""
    if baseline is None:
        return NO_BOUNDARY
    if not trusted:
        return UNREAD_BOUNDARY
    return None


async def storage_epoch(db) -> str | None:
    """The installation's durable storage epoch — rotated by a restore inside the restored database."""
    from sqlalchemy import select  # noqa: PLC0415

    from backend.app.models.settings import Settings  # noqa: PLC0415
    from backend.app.services.connector_submissions import STORAGE_EPOCH_KEY  # noqa: PLC0415

    return (await db.execute(select(Settings.value).where(Settings.key == STORAGE_EPOCH_KEY))).scalar_one_or_none()


async def address_of(db, archive, printer) -> Address | None:
    """The address a boundary of this archive on this printer has now, or ``None`` when either has no identity."""
    archive_identity = getattr(archive, "recovery_identity", None)
    printer_identity = getattr(printer, "connector_identity", None)
    if not isinstance(archive_identity, str) or not archive_identity:
        return None
    if not isinstance(printer_identity, str) or not printer_identity:
        return None
    return Address(archive_identity, printer_identity, await storage_epoch(db))


def forget_in(archive) -> None:
    """Drop the record INSIDE the caller's transaction — a row reused by a reprint starts a new execution."""
    extra = archive.extra_data
    if isinstance(extra, dict) and KEY in extra:
        archive.extra_data = {k: v for k, v in extra.items() if k != KEY}


def _keeps(existing, new: dict) -> bool:
    """Whether what is stored must stay: a later capture never destroys a proven boundary."""
    if _parse(existing) is None or not existing["trusted"]:
        return False
    if not new.get("trusted"):
        return True  # an unread card proves nothing against a list that was read
    if new.get("taken") != TAKEN_LATE or existing.get("taken") != TAKEN_AT_START:
        return False
    # A late list replaces a start list only of another address (an older record of this row).
    return all(existing.get(k) == new.get(k) for k in _ADDRESS_FIELDS)


async def save(
    db,
    archive_id: int,
    expected_identity: str | None,
    baseline: dict,
    *,
    expected_epoch: int | None = None,
    may_run: Callable[[], bool] | None = None,
) -> bool:
    """Write the baseline into the archive's metadata, under its write scope. Never raises to the print path.

    Checked under the scope, after every wait: the archive is the incarnation
    the list was taken for, is still printing and still current; the process
    epoch is the one the listing started in (``expected_epoch`` — a restore that
    won meanwhile must not have an old observation written into its storage);
    the durable epoch in the record is the database's; the caller still owns
    its run (``may_run``); and nothing proven is destroyed (:func:`_keeps`).
    """
    from backend.app.services import queue_sources  # noqa: PLC0415
    from backend.app.services.archive_write_scope import (  # noqa: PLC0415
        archive_write_scope,
        load_active_archive_for_write,
    )
    from backend.app.services.print_execution import NONCURRENT  # noqa: PLC0415

    try:
        async with queue_sources.storage_mutation(), archive_write_scope(db, archive_id):
            archive = await load_active_archive_for_write(db, archive_id)
            if (
                archive is None
                or (expected_identity is not None and archive.recovery_identity != expected_identity)
                or (expected_epoch is not None and queue_sources.current_epoch() != expected_epoch)
                or (may_run is not None and not may_run())
                or archive.status != "printing"
                or NONCURRENT in (archive.extra_data or {})
                or ("storage_epoch" in baseline and baseline["storage_epoch"] != await storage_epoch(db))
                or _keeps((archive.extra_data or {}).get(KEY), baseline)
            ):
                await db.rollback()
                return False
            # A new dict: an in-place change of a JSON column is not seen as a change.
            archive.extra_data = {**(archive.extra_data or {}), KEY: baseline}
            await db.commit()
            return True
    except Exception as exc:  # noqa: BLE001 — a baseline that could not be kept costs an automatic attach, never a print
        logger.warning("[TIMELAPSE] Could not keep the baseline with archive %s: %s", archive_id, exc)
        return False
