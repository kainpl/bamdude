"""Queue rows an H3C / P3C saved before stage A go to review, nothing guessed (N01 stage B).

Before N01 stage A BamDude numbered an H3C's AMS 3H slots 128-136 (a P3C's AMS 3P
132-138), the AMS-HT range; stage A gave them their own numbers (28 + k / 37 + k).
A pending row that still holds such a value in ``ams_mapping`` would print it as an
AMS-HT ``{128 + k, 0}`` that does not exist — and 128-135 is also a real AMS-HT
bound to the rack, so the value cannot be translated (the owner, 2026-11-28: to
review, nothing guessed).

Scope: ``print_queue`` rows with ``status = 'pending'`` on the queue of an H3C /
P3C printer (the queue's id is the printer's own) whose ``ams_mapping`` names
128-138. ``printing`` and every finished state are not touched, nor any other
printer, nor a row without such a value.

* An automatic row — a routing intent in ``mode: auto``, not a calibration, not raw
  G-code: its ``ams_mapping`` is only the last plan, so it is cleared and the
  dispatcher plans the row from the current feed.
* Every other row — pinned, an old row without an intent (its mapping is its pins),
  a calibration, raw G-code: ``review_required: true`` is set in its
  ``filament_routing`` (an object holding only that when there was none). A pinned
  or old row then waits with ``mapping_review_required`` until the operator
  confirms it in the dialog; a calibration / raw G-code row, which has no dialog,
  waits with ``rack_mapping_recreate`` (``filament_preflight``) until re-created.

Run twice it changes nothing: a cleared row holds no such value, a marked row is
already marked. No DDL; the rows are named through a lightweight ``table``.
"""

import json
import logging

logger = logging.getLogger(__name__)

version = 275
name = "rack_queue_rows_review"

#: The model names an H3C / P3C row of ``printers.model`` carries, normalised
#: (upper case, no spaces or dashes) — frozen, as this migration was written.
_RACK_MODELS = frozenset({"H3C", "P3C", "N10", "N11", "BAMBULABH3C", "BAMBULABP3C"})
#: The numbers the AMS 3H / 3P slots had before stage A (128-136 / 132-138).
_OLD_RACK_TRAYS = range(128, 139)


async def upgrade(conn):
    """No DDL."""


def _normalised(model) -> str:
    return str(model or "").strip().upper().replace(" ", "").replace("-", "")


def _decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _names_old_rack_tray(mapping) -> bool:
    # ``type(v) is int``, not isinstance: a bool is no tray.
    return isinstance(mapping, list) and any(type(v) is int and v in _OLD_RACK_TRAYS for v in mapping)


async def seed(session_factory):
    from sqlalchemy import Boolean, Integer, String, Text, column, select, table, text

    printers = table("printers", column("id", Integer), column("model", String))
    queue = table(
        "print_queue",
        column("id", Integer),
        column("queue_id", Integer),
        column("status", String),
        column("ams_mapping", Text),
        column("filament_routing", Text),
        column("is_calibration", Boolean),
        column("source_auto_item_id", Integer),
        column("queue_source_id", Integer),
        column("archive_id", Integer),
        column("library_file_id", Integer),
    )
    sources = table("queue_sources", column("id", Integer), column("format", String))
    archives = table("print_archives", column("id", Integer), column("file_path", String))
    library = table("library_files", column("id", Integer), column("file_path", String))

    async with session_factory() as db:
        rack_printers = [
            row.id
            for row in (await db.execute(select(printers.c.id, printers.c.model)))
            if _normalised(row.model) in _RACK_MODELS
        ]
        if not rack_printers:
            return
        rows = (
            await db.execute(select(queue).where(queue.c.status == "pending", queue.c.queue_id.in_(rack_printers)))
        ).all()
        cleared = marked = 0
        for row in rows:
            if not _names_old_rack_tray(_decode(row.ams_mapping)):
                continue
            intent = _decode(row.filament_routing)
            raw_gcode = False
            if row.source_auto_item_id is None:
                if row.queue_source_id is not None:
                    fmt = (
                        await db.execute(select(sources.c.format).where(sources.c.id == row.queue_source_id))
                    ).scalar_one_or_none()
                    raw_gcode = fmt == "gcode"
                else:
                    path = None
                    if row.archive_id is not None:
                        path = (
                            await db.execute(select(archives.c.file_path).where(archives.c.id == row.archive_id))
                        ).scalar_one_or_none()
                    elif row.library_file_id is not None:
                        path = (
                            await db.execute(select(library.c.file_path).where(library.c.id == row.library_file_id))
                        ).scalar_one_or_none()
                    raw_gcode = str(path or "").lower().endswith(".gcode")
            automatic = isinstance(intent, dict) and intent.get("mode") == "auto"
            if automatic and not row.is_calibration and not raw_gcode:
                # A raw statement the queue-writer inventory reads (tests/queue_writers).
                await db.execute(text("UPDATE print_queue SET ams_mapping = NULL WHERE id = :id"), {"id": row.id})
                cleared += 1
                continue
            marked_intent = dict(intent) if isinstance(intent, dict) else {}
            if marked_intent.get("review_required") is True:
                continue
            marked_intent["review_required"] = True
            await db.execute(
                text("UPDATE print_queue SET filament_routing = :routing WHERE id = :id"),
                {"routing": json.dumps(marked_intent, separators=(",", ":")), "id": row.id},
            )
            marked += 1
        await db.commit()
        if cleared or marked:
            logger.info(
                "m275: H3C / P3C queue rows saved before stage A — %d plan(s) cleared, %d sent to review",
                cleared,
                marked,
            )
