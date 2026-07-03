"""Auto-sync virtual-printer access codes from their target printer.

Non-proxy VPs (Archive / Review / Queue) with a target printer set up a
live-mirror bridge that forwards the slicer's MQTT / RTSPS auth bytes through to
the real printer, so the VP's ``access_code`` MUST equal the target's — earlier
UIs let them diverge, producing a VP the slicer could bind but whose bridge
silently failed to authenticate against the real printer at the second hop.

The route layer now auto-inherits the target's code on every create/update; this
one-time backfill corrects any rows that pre-date that change.

Scope / safety:
- Only non-proxy VPs with a target printer whose ``access_code`` differs from
  (or is NULL versus) the target's.
- Correlated subquery ``UPDATE`` — portable across SQLite and PostgreSQL, no
  driver-specific syntax.
- Idempotent: the WHERE clause excludes already-synced rows, so re-running is a
  no-op.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

version = 87
name = "vp_access_code_sync"


async def upgrade(conn):
    # Log one INFO line per row we're about to correct, for the audit trail.
    mismatch_result = await conn.execute(
        text(
            "SELECT vp.id AS vp_id, vp.name AS vp_name, p.name AS target_name "
            "FROM virtual_printers vp "
            "JOIN printers p ON vp.target_printer_id = p.id "
            "WHERE vp.mode != 'proxy' "
            "  AND (vp.access_code IS NULL OR vp.access_code != p.access_code)"
        )
    )
    for row in mismatch_result.fetchall():
        logger.info(
            "VP %r (id=%s) access code synced from target printer %r",
            row.vp_name,
            row.vp_id,
            row.target_name,
        )

    await conn.execute(
        text(
            "UPDATE virtual_printers "
            "SET access_code = ("
            "    SELECT access_code FROM printers WHERE printers.id = virtual_printers.target_printer_id"
            ") "
            "WHERE virtual_printers.target_printer_id IS NOT NULL "
            "  AND virtual_printers.mode != 'proxy' "
            "  AND (virtual_printers.access_code IS NULL OR virtual_printers.access_code != ("
            "      SELECT access_code FROM printers WHERE printers.id = virtual_printers.target_printer_id"
            "  ))"
        )
    )
