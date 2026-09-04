"""Hide one ``hms[]`` entry on one printer until the printer drops it.

The firmware owns the stack. "Clear" sends ``clean_print_error``, which empties
the scalar ``print_error`` register and nothing else; an entry the printer keeps
re-sending in ``hms[]`` cannot be removed from here at all. A P2S farm carried
``0500_0600_0002_0070`` — a code Bambu ships with no text in any language, no
wiki page, and no line on the printer's own screen — in every push for weeks
(2026-09-04). Prints ran; the card's red pip could not be answered.

So the answer is local and narrow, like BambuStudio's ``skip_print_error``:

- one printer, one FULL 16-char code — the short form ``0500_0070`` names two
  different entries, and hiding by description or by "unknown" is how a real
  fault vanished on an X2D once (``HMSErrorModal.filterKnownHMSErrors``);
- the MQTT client does the hiding when it rebuilds the stack, so every reader
  of ``state.hms_errors`` — card badge, modal, notifications, the MQTT relay —
  goes quiet together, and the hidden entries travel in ``state.hms_muted`` so
  the modal can show and un-hide them;
- the mute expires the moment the entry leaves the stack (``on_hms_mute_expired``
  → :func:`forget`), so the same code later is a new incident and is shown;
- persisted here, because the whole point is not answering the same untextured
  code again after every restart; loaded into the client before its first push.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.hms_mute import HMSMutedEntry

logger = logging.getLogger(__name__)


async def load_muted_codes(db: AsyncSession, printer_id: int) -> set[str]:
    rows = (await db.execute(select(HMSMutedEntry.full_code).where(HMSMutedEntry.printer_id == printer_id))).scalars()
    return {code.upper() for code in rows}


async def remember(db: AsyncSession, printer_id: int, full_code: str) -> None:
    """Idempotent — the second Hide of the same entry is not an error. Flushes; the caller commits."""
    code = full_code.upper()
    existing = await db.scalar(
        select(HMSMutedEntry.id).where(HMSMutedEntry.printer_id == printer_id, HMSMutedEntry.full_code == code)
    )
    if existing is not None:
        return
    db.add(HMSMutedEntry(printer_id=printer_id, full_code=code))
    await db.flush()


async def forget(db: AsyncSession, printer_id: int, codes: set[str]) -> int:
    """Drop the rows for ``codes`` on this printer. Returns how many went. Flushes; the caller commits."""
    if not codes:
        return 0
    result = await db.execute(
        delete(HMSMutedEntry).where(
            HMSMutedEntry.printer_id == printer_id,
            HMSMutedEntry.full_code.in_({c.upper() for c in codes}),
        )
    )
    return result.rowcount or 0
