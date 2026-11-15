"""Frozen D4 index repair, independent of mutable ORM declarations.

Unknown same-name shapes are never dropped. Native upgrade records a bounded
warning/residual; an import owner must instead retain its hard repair debt.
Only canonical UNIQUE duplicate proofs permit a data residual.
"""

import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import inspect, text

from backend.app.core.schema_repair_state import RESIDUALS, VERSION, SchemaRepairPending, put_record, read_record

logger = logging.getLogger(__name__)
CALIBRATION = "ux_filament_cali_active"


def manifest():
    return json.loads((Path(__file__).parents[1] / "data/schema_indexes_v1.json").read_text(encoding="utf-8"))


def quote(value):
    return '"' + value.replace('"', '""') + '"'


async def duplicate_groups(conn, row):
    columns = ",".join(quote(c) for c in row["columns"])
    nonnull = " AND ".join(quote(c) + " IS NOT NULL" for c in row["columns"])
    predicate = row["predicate"] or "TRUE"
    return await conn.scalar(
        text(
            f"SELECT count(*) FROM (SELECT 1 FROM {quote(row['table'])} "
            f"WHERE ({predicate}) AND {nonnull} GROUP BY {columns} HAVING count(*)>1) duplicates"
        )
    )


async def _shape(conn, row):
    """missing / correct / invalid / unknown; never execute observed SQL."""
    if conn.dialect.name == "postgresql":
        indexes = await conn.run_sync(lambda s: inspect(s).get_indexes(row["table"]))
        actual = next((i for i in indexes if i["name"] == row["name"]), None)
        if actual is None:
            # A same-name relation of another kind is not a missing index.
            exists = await conn.scalar(text("SELECT to_regclass(:name)"), {"name": "public." + row["name"]})
            return "unknown" if exists is not None else "missing"
        actual = dict(actual)
        actual.pop("include_columns", None)
        options = dict(actual.get("dialect_options", {}))
        options.pop("postgresql_invalid", None)
        actual["dialect_options"] = options
        if actual != row["postgres_definition"]:
            return "unknown"
        valid = (
            await conn.execute(
                text("SELECT indisvalid,indisready FROM pg_index WHERE indexrelid=to_regclass(:name)"),
                {"name": "public." + row["name"]},
            )
        ).one()
        return "correct" if all(valid) else "invalid"
    actual = (
        await conn.execute(text("SELECT type,tbl_name,sql FROM sqlite_schema WHERE name=:name"), {"name": row["name"]})
    ).one_or_none()
    indexes = (await conn.execute(text(f"PRAGMA index_list({quote(row['table'])})"))).mappings().all()
    flags = next((i for i in indexes if i["name"] == row["name"]), None)
    keys = (await conn.execute(text(f"PRAGMA index_xinfo({quote(row['name'])})"))).mappings().all()
    return sqlite_index_shape(row, actual._mapping if actual is not None else None, flags, keys)


def sqlite_index_shape(row, actual, flags, keys):
    if actual is None:
        return "missing"
    if actual["type"] != "index" or actual["tbl_name"] != row["table"]:
        return "unknown"
    allowed = {row["sqlite_sql"]}
    if row["name"] == "ix_print_archives_effective_hash":
        allowed.add(
            "CREATE INDEX ix_print_archives_effective_hash ON print_archives "
            "((COALESCE(source_content_hash, content_hash)))"
        )
        # SQLAlchemy's released model compiler used this exact spelling;
        # captured by the real v061/pre144 SQLite producers.
        allowed.add(
            "CREATE INDEX ix_print_archives_effective_hash ON print_archives "
            "(COALESCE(source_content_hash, content_hash))"
        )
        # Expression xinfo has no operand names; allow only our frozen SQL.
        return "correct" if " ".join((actual["sql"] or "").split()) in allowed else "unknown"
    if flags is None or bool(flags["unique"]) != row["unique"] or bool(flags["partial"]) != bool(row["predicate"]):
        return "unknown"
    keys = [r for r in keys if r["key"]]
    if [r["name"] for r in keys] != row["columns"] or any(r["desc"] or r["coll"] != "BINARY" for r in keys):
        return "unknown"
    if row["predicate"]:
        parts = (actual["sql"] or "").rsplit("WHERE", 1)
        predicates = {row["predicate"]}
        if row["name"] == CALIBRATION:
            predicates.add("is_active = 1")  # Actual pre075 SQLite model compiler.
        if len(parts) != 2 or " ".join(parts[1].split()) not in predicates:
            return "unknown"
    return "correct"


def residual(row, reason, count, provenance):
    return {
        "object": row["table"] + ".indexes." + row["name"],
        "reason": reason,
        "count": count,
        "token": str(uuid4()),
        "provenance": provenance,
        "contract": "d4-indexes-v1",
        "manifest": hashlib.sha256(json.dumps(manifest(), sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }


async def repair_indexes(conn, *, native=False, accepted_calibration=False, only=None):
    results = []
    tables = await conn.run_sync(lambda s: set(inspect(s).get_table_names()))
    for row in manifest():
        object_id = row["table"] + ".indexes." + row["name"]
        if only is not None and object_id not in only:
            continue
        columns = (
            await conn.run_sync(lambda s, t=row["table"]: {c["name"] for c in inspect(s).get_columns(t)})
            if row["table"] in tables
            else set()
        )
        needed = set(row["columns"])
        if row["name"] == "ix_print_archives_effective_hash":
            needed.add("content_hash")
        shape = await _shape(conn, row) if needed <= columns else "unknown"
        if shape == "correct":
            continue
        if shape == "unknown":
            if not native:
                raise SchemaRepairPending("unknown_index_shape")
            results.append(residual(row, "unknown_index_shape", 0, "native"))
            continue
        # Data proof must come before DDL, avoiding expected server ERRORs.
        count = await duplicate_groups(conn, row) if row["unique"] else 0
        if count:
            if row["name"] == CALIBRATION and not (native or accepted_calibration):
                raise SchemaRepairPending("calibration_confirmation_required")
            if shape == "invalid":
                # A known invalid index provides no protection and may prevent
                # portable copy; remove only after exact shape/violation proof.
                await conn.execute(text("DROP INDEX " + quote(row["name"])))
            results.append(
                residual(row, "duplicate_identity_groups", count, "native" if native else "accepted-restore")
            )
            continue
        if shape == "invalid":
            await conn.execute(text("DROP INDEX " + quote(row["name"])))
        await conn.execute(text(row["postgres_sql"] if conn.dialect.name == "postgresql" else row["sqlite_sql"]))
    if results:
        logger.warning("D4 index residuals: %s", [(r["object"], r["reason"], r["count"]) for r in results])
    return results


async def native_upgrade(conn):
    results = await repair_indexes(conn, native=True)
    if results:
        previous = await read_record(conn, RESIDUALS)
        entries = {} if previous is None else previous.get("entries", {})
        if not isinstance(entries, dict):
            raise SchemaRepairPending("invalid_residuals")
        await put_record(
            conn, RESIDUALS, {"version": VERSION, "entries": {**entries, **{r["object"]: r for r in results}}}
        )


async def retry_index_residuals(conn):
    """No generic per-boot schema diff: revisit only exact persisted objects."""
    state = await read_record(conn, RESIDUALS)
    if state is None:
        return
    entries = state.get("entries")
    if not isinstance(entries, dict):
        raise SchemaRepairPending("invalid_residuals")
    known = {r["table"] + ".indexes." + r["name"] for r in manifest()}
    wanted = set(entries) & known
    if not wanted:
        return
    results = await repair_indexes(conn, native=True, only=wanted)
    for result in results:
        old = entries[result["object"]]
        if old.get("provenance") == "accepted-restore" and isinstance(old.get("acceptance"), str):
            # Preserve the initial operator's audit when the generic PG model
            # proof is re-proved by this frozen targeted helper. This pointer
            # is history, never authority for another restore or new count.
            result.update(provenance="accepted-restore", acceptance=old["acceptance"])
        if (old.get("reason"), old.get("count"), old.get("manifest")) == (
            result["reason"],
            result["count"],
            result["manifest"],
        ):
            result.update(old)
    updated = {k: v for k, v in entries.items() if k not in wanted}
    updated.update({r["object"]: r for r in results})
    if updated != entries:
        await put_record(conn, RESIDUALS, {"version": VERSION, "entries": updated})
