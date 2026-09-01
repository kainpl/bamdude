"""Was the slot holding filament when this journal row was written.

The journal reconstructs, after the fact, what fed a print — and two of its
questions have no answer in what it records today, so both are inferred:

* **"the mapped slot never held a spool"** — the rescue that names the backup
  which took over before the first layer (``journal_boundaries_for_tray``)
  deduces this from "every runout of the tray froze no spool id". True in
  practice, but it is a deduction from an absence, and it collapses the moment
  a spool id gets frozen onto the row for an unrelated reason — which happens
  as soon as an operator assigns a spool to the empty slot mid-print.
* **"this ``spool_loaded`` was a real refill"** — versus bookkeeping. Assigning
  a spool in the UI closes the tray's episode whether or not a reel was
  physically put in, and nothing downstream can tell the two apart.

``tray_exist_bits`` answers both directly: the AMS's own per-slot presence
sensor, decoded onto each tray as ``exists``. It is the ONLY independent
evidence a reel is physically there — ``tray_type`` is written by BamDude
itself via ``ams_filament_setting`` the moment a spool is assigned, so reading
that back is reading our own bookkeeping, and a slot holding an unlabelled reel
carries no type at all while being fully occupied.

NULL means "we had no reading", never "empty": every existing row, every event
carrying no tray, and any push that arrived without the bitfield. Readers must
treat NULL as unknown and keep their present behaviour — this column widens
what they can be sure of, it never narrows it.
"""

from backend.app.migrations.helpers import add_column

version = 161
name = "usage_event_slot_occupied"


async def upgrade(conn):
    # Deliberately NOT backfilled: the bit is a fact about a moment that has
    # already passed, and no value we could invent now would be that fact.
    # Boolean over an integer flag because both backends agree on it here, and
    # nullability is the whole point (see the docstring).
    await add_column(conn, "print_usage_events", "slot_occupied BOOLEAN")
