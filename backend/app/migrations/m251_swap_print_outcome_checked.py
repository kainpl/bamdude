"""Prints whose plate the swap changed by itself count as checked -- the history too.

From this release a print is marked «checked, no defects» the moment our table-change macro answers success
or the file's own swap sequence clears its plate (``archive_outcome.mark_checked_by_swap``): nobody takes such
a print off the plate, so nobody is asked to grade it (owner, 2026-11-20). A swap that failed or was held
leaves the print on the plate, and the person who has to come to it grades it there.

The prints that finished before this release still read «Not checked». The ones whose table change answered
success carry the record of it -- ``extra_data["bamdude_terminal_effects"]["swap_change_table"]["stage"] ==
"finished"`` -- and are stamped here, at their own end (else their creation, else now). Only completed prints
outside the trash, never a calibration print, and never one already marked. A file's own sequence left no
durable record of the clear, so those stay for the operator (the Unchecked filter, then «No defects»).

A repeated run (``DEBUG`` re-runs the latest migration) marks only prints that already carry a finished swap,
which the live code marks anyway: nothing is marked that the rule would not.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, column, func, select, table, update

version = 251
name = "swap_print_outcome_checked"

_ARCHIVES = table(
    "print_archives",
    column("id", Integer),
    column("status", String),
    column("completed_at", DateTime),
    column("created_at", DateTime),
    column("deleted_at", DateTime),
    column("is_calibration", Boolean),
    column("outcome_checked_at", DateTime),
    # Read as text, parsed here: a row whose JSON is broken is skipped instead of failing the upgrade.
    column("extra_data", Text),
)

_CHUNK = 500


def _swapped(extra) -> bool:
    """Whether the archive records our table change as answered success."""
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            return False
    if not isinstance(extra, Mapping):
        return False
    effects = extra.get("bamdude_terminal_effects")
    effect = effects.get("swap_change_table") if isinstance(effects, Mapping) else None
    return isinstance(effect, Mapping) and effect.get("stage") == "finished"


async def upgrade(conn):
    unchecked = (
        _ARCHIVES.c.status == "completed",
        _ARCHIVES.c.outcome_checked_at.is_(None),
        _ARCHIVES.c.deleted_at.is_(None),
        _ARCHIVES.c.is_calibration.is_not(True),
    )
    rows = (await conn.execute(select(_ARCHIVES.c.id, _ARCHIVES.c.extra_data).where(*unchecked))).all()
    ids = [row[0] for row in rows if _swapped(row[1])]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for start in range(0, len(ids), _CHUNK):
        await conn.execute(
            update(_ARCHIVES)
            .where(_ARCHIVES.c.id.in_(ids[start : start + _CHUNK]), *unchecked)
            .values(outcome_checked_at=func.coalesce(_ARCHIVES.c.completed_at, _ARCHIVES.c.created_at, now))
        )
