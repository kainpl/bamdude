"""Offline-only upgrade of the one known constant-default verification plan.

No DDL, budget refill, generic manifest adoption or debt clearing. The exact
old/new full-plan hashes bind this compatibility step to the composed plan.
"""

import json
from pathlib import Path

from sqlalchemy import text

from backend.app.core.schema_repair_state import (
    DEBT,
    SchemaRepairPending,
    encode,
    manifest_hash,
    read_record,
    validate_debt,
)


async def prepare_known_retry(conn, metadata):
    transition = json.loads(
        (Path(__file__).parents[1] / "data/pg_schema_spoolman_manifest_transition_v1.json").read_text(encoding="utf-8")
    )
    current = manifest_hash(metadata)
    if current != transition["to"]:
        return False  # A later/different model or repair plan is not compatible.
    previous = await read_record(conn, DEBT)
    if previous is None or previous.get("manifest") != transition["from"]:
        return False
    updated = {**previous, "manifest": current}
    validate_debt(updated, metadata)  # Validate all other fields before writing.
    result = await conn.execute(
        text('UPDATE settings SET value=:new WHERE "key"=:key AND value=:old'),
        {"key": DEBT, "old": encode(previous), "new": encode(updated)},
    )
    if result.rowcount != 1:
        raise SchemaRepairPending("debt_changed")
    return True
