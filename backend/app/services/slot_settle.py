"""Ask the printer again until it has actually reported a slot we just configured.

``ams_filament_setting`` lands quickly — the printer echoes the filament id back
within about a tenth of a second, which is what
``register_assignment_verification`` keys on. ``tray_type`` follows later, and
that is the field the printer card renders and the field ``on_ams_change``
compares the assignment fingerprint against. Both call sites that configure a
slot force exactly one ``pushall`` immediately after publishing, so our one
forced push captures precisely the moment before the type lands; after that
nothing asks again and the card waits for the printer's own reporting cadence.
On the external holder that gap was tens of seconds, and it looked like the
assignment had silently failed — the catalogue said assigned, BambuStudio showed
the new spool, and our card did not.

⚠️ **Settling is not verification, and this must not be folded into it.**
``_check_assignment_verifications`` succeeds on the filament id alone by design:
a slot can be correctly configured without our ever seeing a ``tray_type``, and
``test_match_fires_verified`` pins that. Requiring a type there would convert
today's successes into timeout failures. This module answers the different
question "has the printer finished describing the slot", and answers it without
touching what "verified" means.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Roughly a second of total budget, front-loaded: the printer usually settles on
# the first re-ask, and the later gaps only cover a slow one. ⚠️ Bounded on
# purpose — a slot the printer never describes is a real state (an empty holder),
# not something to nudge for ever.
DEFAULT_DELAYS: tuple[float, ...] = (0.3, 0.6, 1.2)


async def nudge_until_slot_reports_type(
    client,
    ams_id: int,
    tray_id: int,
    *,
    delays: tuple[float, ...] = DEFAULT_DELAYS,
) -> bool:
    """Re-ask ``client``'s printer for a full push until the slot reports a type.

    Returns whether the slot ended up describing itself. Never raises: this runs
    after a configuration that already succeeded, and a failure to settle is a
    slower card, not a failed assignment.
    """
    if client is None:
        return False

    for delay in delays:
        if client.slot_reported_filament_type(ams_id, tray_id):
            return True
        await asyncio.sleep(delay)
        try:
            if not client.request_status_update():
                # Not connected. The periodic reconnect path will resync; sleeping
                # out the rest of the budget here buys nothing.
                return False
        except Exception:
            logger.debug("Slot settle: status request failed for AMS%s-T%s", ams_id, tray_id, exc_info=True)
            return False

    return bool(client.slot_reported_filament_type(ams_id, tray_id))
