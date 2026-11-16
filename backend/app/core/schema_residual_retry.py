"""Focused retry of persisted, canonical R5 objects; never a startup conform."""

import json
import logging

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.schema import AddConstraint

from backend.app.core.pg_schema_contract import canonical
from backend.app.core.schema_conform_repair import _check_sql, _duplicates, _sql, add_check
from backend.app.core.schema_index_repair import manifest, quote
from backend.app.core.schema_model_plan import expected_model_plan
from backend.app.core.schema_repair_state import (
    RESIDUALS,
    VERSION,
    SchemaRepairPending,
    put_record,
    read_record,
)

logger = logging.getLogger(__name__)


async def _guard_state(conn, table, kind, target, expected):
    """A remembered name/column set is insufficient proof of enforcement."""
    if not isinstance(expected, dict) or expected.get("name") != target:
        return "unknown"

    def read(sync):
        inspector = inspect(sync)
        methods = {
            "unique": inspector.get_unique_constraints,
            "check": inspector.get_check_constraints,
            "indexes": inspector.get_indexes,
            "foreign_keys": inspector.get_foreign_keys,
        }
        return next((r for r in methods[kind](table) if r["name"] == target), None)

    actual = await conn.run_sync(read)
    if actual is None:
        if kind in ("unique", "indexes"):
            collision = await conn.scalar(text("SELECT to_regclass(:name)"), {"name": "public." + target})
            if collision is not None:
                return "unknown"
        return "missing"
    if json.loads(json.dumps(actual, default=str)) != expected:
        return "unknown"
    if kind == "indexes":
        valid = (
            await conn.execute(
                text("SELECT indisvalid,indisready FROM pg_index WHERE indexrelid=to_regclass(:name)"),
                {"name": "public." + target},
            )
        ).one()
        return "correct" if all(valid) else "unknown"
    valid = await conn.scalar(
        text(
            "SELECT c.convalidated AND (c.conindid=0 OR EXISTS (SELECT 1 FROM pg_index i WHERE i.indexrelid=c.conindid AND i.indisvalid AND i.indisready)) "
            "FROM pg_constraint c WHERE c.conrelid=to_regclass(:table) AND c.conname=:name"
        ),
        {"table": "public." + table, "name": target},
    )
    return "correct" if valid else "unknown"


async def retry_data_residuals(conn, metadata):
    state = await read_record(conn, RESIDUALS)
    if state is None:
        return
    entries = state.get("entries")
    if not isinstance(entries, dict):
        raise SchemaRepairPending("invalid_residuals")
    frozen_indexes = {r["table"] + ".indexes." + r["name"] for r in manifest()}
    updated = dict(entries)
    expected = None
    for key, entry in entries.items():
        if key in frozen_indexes:
            continue  # Frozen native helper owns these separately.
        if (
            not isinstance(entry, dict)
            or entry.get("object") != key
            or entry.get("contract") not in (None, "d4-data-v1")
            or type(entry.get("count")) is not int
            or entry["count"] <= 0
        ):
            logger.warning("Schema residual has an unknown contract; retained without mutation")
            continue
        if expected is None:
            expected = canonical(await expected_model_plan(conn, metadata))
        # The global manifest records the original import, not this object's
        # lifetime. Re-derive its own server-normalised contract so unrelated
        # model changes cannot orphan a corrected guard. Legacy v1 nullable
        # proofs represented exactly SET NOT NULL and did not store False.
        definition = entry.get("definition")
        if (
            "definition" not in entry
            and entry.get("reason") == "null_rows"
            and key.endswith(".nullable")
            and entry.get("contract") is None
        ):
            definition = False
        if key not in expected or json.dumps(definition, sort_keys=True) != json.dumps(expected[key], sort_keys=True):
            logger.warning("Schema residual object contract changed; retained without mutation")
            continue
        name, kind, target = key.split(".", 2)
        table = metadata.tables.get(name)
        if table is None:
            continue
        if kind in ("unique", "check", "indexes", "foreign_keys"):
            shape = await _guard_state(conn, name, kind, target, entry.get("definition"))
            if shape == "correct":
                updated.pop(key)
                continue
            if shape == "unknown":
                logger.warning("Schema residual guard is unproven; retained without mutation")
                continue
        repaired = False
        if kind == "column" and target.endswith(".nullable"):
            column = table.c.get(target.removesuffix(".nullable"))
            if column is not None and not column.nullable and not column.primary_key:
                count = await conn.scalar(
                    text(f"SELECT count(*) FROM public.{quote(name)} WHERE {quote(column.name)} IS NULL")
                )
                if not count:
                    await conn.execute(
                        text(f"ALTER TABLE public.{quote(name)} ALTER COLUMN {quote(column.name)} SET NOT NULL")
                    )
                    repaired = True
        elif kind == "unique":
            columns = entry.get("columns")
            matches = [
                c
                for c in table.constraints
                if isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == columns
            ]
            if len(matches) == 1 and not await _duplicates(conn, name, [quote(c) for c in columns]):
                existing = await conn.run_sync(lambda s, name=name: inspect(s).get_unique_constraints(name))
                if not any(c["column_names"] == columns for c in existing):
                    await conn.execute(AddConstraint(matches[0]))
                repaired = True
        elif kind == "check":
            matches = [c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == target]
            if len(matches) == 1:
                count = await conn.scalar(
                    text(f"SELECT count(*) FROM public.{quote(name)} WHERE NOT ({_check_sql(matches[0])})")
                )
                if not count:
                    existing = await conn.run_sync(lambda s, name=name: inspect(s).get_check_constraints(name))
                    if not any(c["name"] == target for c in existing):
                        await add_check(conn, matches[0])
                        repaired = True
                    # An existing same-name CHECK could have a different
                    # expression. Do not clear a report using its name alone.
        elif kind == "indexes":
            matches = [i for i in table.indexes if i.name == target and i.unique]
            if len(matches) == 1:
                index = matches[0]
                predicate = index.dialect_options["postgresql"].get("where")
                count = await _duplicates(
                    conn,
                    name,
                    [_sql(e, conn) for e in index.expressions],
                    predicate="TRUE" if predicate is None else _sql(predicate, conn),
                )
                existing = await conn.run_sync(lambda s, name=name: inspect(s).get_indexes(name))
                if not count and not any(i["name"] == target for i in existing):
                    await conn.run_sync(lambda s, index=index: index.create(s))
                    repaired = True
        elif kind == "foreign_keys":
            columns = entry.get("columns")
            matches = [fk for fk in table.foreign_key_constraints if [c.name for c in fk.columns] == columns]
            if len(matches) == 1:
                fk = matches[0]
                parent = fk.referred_table.name
                on = " AND ".join(f"p.{quote(e.column.name)}=c.{quote(e.parent.name)}" for e in fk.elements)
                nonnull = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in columns)
                count = await conn.scalar(
                    text(
                        f"SELECT count(*) FROM public.{quote(name)} c WHERE {nonnull} "
                        f"AND NOT EXISTS (SELECT 1 FROM public.{quote(parent)} p WHERE {on})"
                    )
                )
                existing = await conn.run_sync(lambda s, name=name: inspect(s).get_foreign_keys(name))
                if not count and not any(f["constrained_columns"] == columns for f in existing):
                    await conn.execute(AddConstraint(fk))
                    repaired = True
        if repaired:
            if (
                kind in ("unique", "check", "indexes", "foreign_keys")
                and await _guard_state(conn, name, kind, target, entry.get("definition")) != "correct"
            ):
                raise SchemaRepairPending("unverified_residual_repair")
            updated.pop(key)
    if updated != entries:
        await put_record(conn, RESIDUALS, {"version": VERSION, "entries": updated})
