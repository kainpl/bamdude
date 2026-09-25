"""Correct the tare of RFID-added spools that took the wrong catalogue row (upstream #2909).

A Bambu roll ships on the 250 g Low Temp plastic spool, but the lookup that
gave an auto-added spool its ``core_weight`` took the first spool-catalogue row
whose name starts "Bambu Lab" — with no ORDER BY. There are three (High Temp
216, Low Temp 250, White 253); SQLite answers in insertion order, so installs
recorded 216 g, and PostgreSQL promises no order at all once the table has seen
an update. ``spool_tag_matcher.create_spool_from_tray`` now asks for the row by
name; this repairs what the old lookup already wrote.

Which rows: ``data_origin = 'rfid_auto'`` (spools that code path created) whose
``core_weight`` equals one of the OTHER "Bambu Lab" catalogue weights — read
from the catalogue, not hardcoded, so an edited catalogue stays correct. They
get the Low Temp row's weight and id (the documented 250 g and no id when the
row is gone).

``weight_used`` is corrected only for a spool that has been weighed
(``last_weighed_at`` set — a database imported from Bambuddy, whose SpoolBuddy
wrote it; BamDude itself never does): a scale reading minus a tare 34 g light
credited 34 g of filament that was not there, a constant every later print
added on top of, so adding the difference back is exact. It is clamped to
``[0, label_weight]``. A spool never weighed has ``weight_used`` from the AMS
remain %, which the tare never touched.

One case cannot be told apart and is stated, not hidden: a user who moved an
RFID roll onto a real High Temp spool and set 216 g by hand looks identical to
a row the lookup got wrong, and is normalised with them. The migration runs
once (the ``_migrations`` ledger), so a tare set after it stays.
"""

from __future__ import annotations

import logging

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

version = 183
name = "rfid_core_weight_repair"


async def upgrade(conn):
    """No schema change — the repair is data, in ``seed``."""


async def seed(session_factory):
    # The same two values the creating path uses, imported rather than
    # repeated: a repair that looked for a different row than the code writes
    # would leave in place the tare it exists to correct.
    from backend.app.services.spool_tag_matcher import (
        BAMBU_PLASTIC_SPOOL_CATALOG_NAME,
        BAMBU_PLASTIC_SPOOL_CORE_WEIGHT,
    )

    async with session_factory() as db:
        correct = (
            await db.execute(
                text("SELECT id, weight FROM spool_catalog WHERE LOWER(name) = :name ORDER BY id LIMIT 1"),
                {"name": BAMBU_PLASTIC_SPOOL_CATALOG_NAME.lower()},
            )
        ).first()
        correct_id = correct.id if correct else None
        correct_weight = correct.weight if correct else BAMBU_PLASTIC_SPOOL_CORE_WEIGHT

        bambu_weights = {
            row.weight
            for row in (
                await db.execute(
                    text("SELECT weight FROM spool_catalog WHERE LOWER(name) LIKE :prefix"),
                    {"prefix": "bambu lab%"},
                )
            ).all()
        }
        wrong_weights = sorted(bambu_weights - {correct_weight})
        if not wrong_weights:
            return

        rows = (
            await db.execute(
                text(
                    "SELECT id, core_weight, label_weight, weight_used, last_weighed_at FROM spool "
                    "WHERE data_origin = 'rfid_auto' AND core_weight IN :wrong"
                ).bindparams(bindparam("wrong", expanding=True)),
                {"wrong": wrong_weights},
            )
        ).all()
        reweighed = 0
        for row in rows:
            weight_used = row.weight_used or 0.0
            if row.last_weighed_at is not None:
                delta = correct_weight - row.core_weight
                weight_used = min(max(0.0, weight_used + delta), float(row.label_weight or 0))
                reweighed += 1
            await db.execute(
                text(
                    "UPDATE spool SET core_weight = :cw, core_weight_catalog_id = :cid, weight_used = :wu WHERE id = :id"
                ),
                {"cw": correct_weight, "cid": correct_id, "wu": weight_used, "id": row.id},
            )
        if rows:
            await db.commit()
            logger.info(
                "m183: corrected the tare of %d RFID-added spool(s) to %d g; %d weighed one(s) had used weight adjusted",
                len(rows),
                correct_weight,
                reweighed,
            )
