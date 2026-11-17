"""What was on the printer before this print recorded anything — kept with the archive (G10 C3).

The scan after a print attaches the recording that is NEW since the print
started. "New" needs a boundary: the list of recordings taken at the start. It
used to live in process memory only, so a restart during a print lost it, and
the scan then took a list AFTER the print had ended — a list that already holds
the print's own video, or, read off a card that did not answer, holds nothing
and makes every old video look new.

The boundary is now a fact of the archive: ``extra_data["timelapse_baseline"]``,
written when it is taken and read back by the scan when memory has none.

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

A name that merely resembles the print is never a reason to attach.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY = "timelapse_baseline"

TAKEN_AT_START = "start"
#: Taken while the print was already running — after a restart of this process.
TAKEN_LATE = "late"

NO_BOUNDARY = "no_baseline"
UNREAD_BOUNDARY = "untrusted_baseline"


def record(names: set[str], *, trusted: bool, taken: str) -> dict:
    """The baseline as it is stored. Names sorted, so two equal baselines are equal JSON."""
    return {
        "names": sorted(n for n in names if isinstance(n, str)),
        "trusted": bool(trusted),
        "taken": taken,
        "at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
    }


def read(extra_data: dict | None) -> tuple[set[str], bool] | None:
    """``(names, trusted)`` from an archive's metadata, or ``None`` when it holds no usable baseline.

    Anything malformed is no baseline: a guess here would be a guess about
    which video is somebody's.
    """
    stored = (extra_data or {}).get(KEY)
    if not isinstance(stored, dict):
        return None
    names = stored.get("names")
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        return None
    if stored.get("taken") not in (TAKEN_AT_START, TAKEN_LATE) or not isinstance(stored.get("trusted"), bool):
        return None
    return set(names), stored["trusted"]


def automatic_attach_refusal(baseline: set[str] | None, trusted: bool) -> str | None:
    """Why the scan after a print must not attach anything by itself, or ``None`` when it may."""
    if baseline is None:
        return NO_BOUNDARY
    if not trusted:
        return UNREAD_BOUNDARY
    return None


async def save(db, archive_id: int, expected_identity: str | None, baseline: dict) -> bool:
    """Write the baseline into the archive's metadata, under its write scope. Never raises to the print path."""
    from backend.app.services import queue_sources  # noqa: PLC0415
    from backend.app.services.archive_write_scope import (  # noqa: PLC0415
        archive_write_scope,
        load_active_archive_for_write,
    )

    try:
        async with queue_sources.storage_mutation(), archive_write_scope(db, archive_id):
            archive = await load_active_archive_for_write(db, archive_id)
            if archive is None or (expected_identity is not None and archive.recovery_identity != expected_identity):
                await db.rollback()
                return False
            # A new dict: an in-place change of a JSON column is not seen as a change.
            archive.extra_data = {**(archive.extra_data or {}), KEY: baseline}
            await db.commit()
            return True
    except Exception as exc:  # noqa: BLE001 — a baseline that could not be kept costs an automatic attach, never a print
        logger.warning("[TIMELAPSE] Could not keep the baseline with archive %s: %s", archive_id, exc)
        return False
