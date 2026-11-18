"""Import-only, typed conformance and final verification under durable debt.

Data exceptions are proved from canonical definitions AND a missing guard.
An SQLSTATE, log line, imported residual or a remembered process flag is never
evidence. Unknown schema and technical failures leave the installation closed.
"""

import json
import logging
from uuid import uuid4

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.schema import AddConstraint, CreateColumn

from backend.app.core.pg_schema_contract import canonical, capture, retained_pairs
from backend.app.core.schema_index_repair import CALIBRATION, quote
from backend.app.core.schema_model_plan import expected_model_plan
from backend.app.core.schema_repair_state import SchemaRepairPending, manifest_hash

logger = logging.getLogger(__name__)


def _target(conn, name):
    return ("public." if conn.dialect.name == "postgresql" else "") + quote(name)


def _sql(expression, conn):
    return str(expression.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True}))


async def _inventory(conn):
    return await conn.run_sync(
        lambda s: {
            name: {
                "columns": {c["name"]: c for c in inspect(s).get_columns(name)},
                "unique": inspect(s).get_unique_constraints(name),
                "indexes": inspect(s).get_indexes(name),
                "checks": inspect(s).get_check_constraints(name),
                "fks": inspect(s).get_foreign_keys(name),
            }
            for name in inspect(s).get_table_names(schema="public" if conn.dialect.name == "postgresql" else None)
        }
    )


def _full_unique(have, columns):
    return any(u["column_names"] == columns for u in have["unique"]) or any(
        i["unique"]
        and i["column_names"] == columns
        and not i.get("expressions")
        and i.get("dialect_options", {}).get("postgresql_where") is None
        and i.get("dialect_options", {}).get("sqlite_where") is None
        and not i.get("dialect_options", {}).get("postgresql_invalid")
        for i in have["indexes"]
    )


async def _duplicates(conn, table, expressions, *, predicate="TRUE"):
    columns = ", ".join(expressions)
    nonnull = " AND ".join(f"({c}) IS NOT NULL" for c in expressions)
    return await conn.scalar(
        text(
            f"SELECT count(*) FROM (SELECT 1 FROM {_target(conn, table)} WHERE ({predicate}) "
            f"AND {nonnull} GROUP BY {columns} HAVING count(*)>1) duplicates"
        )
    )


def _proof(object_id, reason, count, metadata):
    return {
        "object": object_id,
        "reason": reason,
        "count": count,
        "token": str(uuid4()),
        "manifest": manifest_hash(metadata),
        "provenance": "restore",
        "contract": "d4-data-v1",
    }


def _check_sql(con):
    from backend.app.migrations.m031_oidc_email_claim import _CHECK_FORMULA, _CONSTRAINT_NAME

    if con.table.name == "oidc_providers" and con.name == _CONSTRAINT_NAME:
        return _CHECK_FORMULA
    return str(con.sqltext)


async def add_check(conn, con):
    # m031 owns the post-chain canonical spelling of its boolean CHECK.
    # Use that frozen formula when a later import must recreate it too.
    await conn.execute(
        text(f"ALTER TABLE public.{quote(con.table.name)} ADD CONSTRAINT {quote(con.name)} CHECK ({_check_sql(con)})")
    )


async def columns(conn, metadata):
    have = await _inventory(conn)
    for table in metadata.tables.values():
        if table.name not in have:
            raise SchemaRepairPending("missing_model_table")
        for column in table.c:
            if column.info.get("migration_shim"):
                continue
            actual = have[table.name]["columns"].get(column.name)
            target = f"public.{quote(table.name)}"
            if actual is None:
                # Existing import behaviour: never manufacture a value using
                # a Python default, nor strip a failed new column's NOT NULL.
                await conn.execute(text(f"ALTER TABLE {target} ADD COLUMN {_sql(CreateColumn(column), conn)}"))
            elif bool(actual["nullable"]) != bool(column.nullable) and not column.primary_key:
                if not column.nullable:
                    count = await conn.scalar(text(f"SELECT count(*) FROM {target} WHERE {quote(column.name)} IS NULL"))
                    if count:
                        continue  # Final verifier re-proves this exact residual.
                verb = "DROP" if column.nullable else "SET"
                await conn.execute(text(f"ALTER TABLE {target} ALTER COLUMN {quote(column.name)} {verb} NOT NULL"))


async def unique(conn, metadata):
    have = await _inventory(conn)
    for table in metadata.tables.values():
        for con in table.constraints:
            if not isinstance(con, UniqueConstraint):
                continue
            cols = [c.name for c in con.columns]
            if _full_unique(have[table.name], cols):
                continue
            if await _duplicates(conn, table.name, [quote(c) for c in cols]):
                continue
            async with conn.begin_nested():
                await conn.execute(AddConstraint(con))


async def foreign_keys(conn, metadata):
    """Preserve existing import detach/purge BEFORE data classification (R7)."""
    have = await _inventory(conn)
    for table in metadata.tables.values():
        for fk in table.foreign_key_constraints:
            cols = [c.name for c in fk.columns]
            parent = fk.referred_table.name
            refs = [e.column.name for e in fk.elements]
            candidates = [f for f in have[table.name]["fks"] if f["constrained_columns"] == cols]
            if len(candidates) > 1:
                raise SchemaRepairPending("ambiguous_foreign_key")
            current = candidates[0] if candidates else None
            expected_actions = ((fk.ondelete or "NO ACTION").upper(), (fk.onupdate or "NO ACTION").upper())
            same = current is not None and (
                current["referred_table"] == parent
                and current["referred_columns"] == refs
                and current.get("referred_schema") in (None, "public")
                and tuple((current.get("options", {}).get(a) or "NO ACTION").upper() for a in ("ondelete", "onupdate"))
                == expected_actions
            )
            if same:
                valid = await conn.scalar(
                    text("SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname=:name"),
                    {"table": "public." + table.name, "name": current["name"]},
                )
                if valid:
                    continue
            on = " AND ".join(f"p.{quote(p)}=c.{quote(c)}" for c, p in zip(cols, refs, strict=True))
            notnull = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in cols)
            orphan = f"{notnull} AND NOT EXISTS (SELECT 1 FROM public.{quote(parent)} p WHERE {on})"
            count = await conn.scalar(text(f"SELECT count(*) FROM public.{quote(table.name)} c WHERE {orphan}"))
            if count:
                if expected_actions[0] == "SET NULL":
                    assigns = ",".join(f"{quote(c)}=NULL" for c in cols)
                    statement = f"UPDATE public.{quote(table.name)} c SET {assigns} WHERE {orphan}"
                else:
                    statement = f"DELETE FROM public.{quote(table.name)} c WHERE {orphan}"
                await conn.execute(text(statement))
                logger.warning("Import FK repair %s: %d orphan rows", table.name, count)
            if same:
                await conn.execute(
                    text(f"ALTER TABLE public.{quote(table.name)} VALIDATE CONSTRAINT {quote(current['name'])}")
                )
                continue
            if current:
                await conn.execute(
                    text(f"ALTER TABLE public.{quote(table.name)} DROP CONSTRAINT {quote(current['name'])}")
                )
            async with conn.begin_nested():
                await conn.execute(AddConstraint(fk))


async def model_indexes(conn, metadata, *, accepted_calibration=False):
    have = await _inventory(conn)
    for table in metadata.tables.values():
        names = {i["name"] for i in have[table.name]["indexes"]}
        for index in table.indexes:
            if index.name in names:
                continue  # Definition/valid/ready is checked independently.
            if index.unique:
                predicate = index.dialect_options["postgresql"].get("where")
                count = await _duplicates(
                    conn,
                    table.name,
                    [_sql(e, conn) for e in index.expressions],
                    predicate="TRUE" if predicate is None else _sql(predicate, conn),
                )
                if count:
                    if index.name == CALIBRATION and not accepted_calibration:
                        raise SchemaRepairPending("calibration_confirmation_required")
                    continue
            async with conn.begin_nested():
                await conn.run_sync(lambda s, index=index: index.create(s))


async def checks(conn, metadata):
    have = await _inventory(conn)
    for table in metadata.tables.values():
        names = {c["name"] for c in have[table.name]["checks"]}
        for con in table.constraints:
            if not isinstance(con, CheckConstraint) or not con.name or con.name in names:
                continue
            if await conn.scalar(
                text(f"SELECT count(*) FROM public.{quote(table.name)} WHERE NOT ({_check_sql(con)})")
            ):
                continue
            async with conn.begin_nested():
                await add_check(conn, con)


async def data_residuals(conn, metadata, *, accepted_calibration=False):
    """Re-prove only where canonical enforcement is actually absent/weaker."""
    have = await _inventory(conn)
    residuals = []
    for table in metadata.tables.values():
        if table.name not in have:
            continue  # Final structural comparison rejects it.
        actual = have[table.name]
        for column in table.c:
            col = actual["columns"].get(column.name)
            if col is not None and col["nullable"] and not column.nullable and not column.primary_key:
                count = await conn.scalar(
                    text(f"SELECT count(*) FROM {_target(conn, table.name)} WHERE {quote(column.name)} IS NULL")
                )
                if count:
                    residuals.append(
                        _proof(f"{table.name}.column.{column.name}.nullable", "null_rows", count, metadata)
                    )
        for con in table.constraints:
            if isinstance(con, UniqueConstraint):
                names = [c.name for c in con.columns]
                if _full_unique(actual, names):
                    continue
                count = await _duplicates(conn, table.name, [quote(c) for c in names])
                if count:
                    # Server names of unnamed constraints come from the
                    # expected plan, resolved by columns in verify().
                    residuals.append(
                        _proof(
                            f"{table.name}.unique.columns:" + ",".join(names),
                            "duplicate_identity_groups",
                            count,
                            metadata,
                        )
                    )
                    residuals[-1]["columns"] = names
            elif isinstance(con, CheckConstraint) and con.name:
                if any(c["name"] == con.name for c in actual["checks"]):
                    continue
                count = await conn.scalar(
                    text(f"SELECT count(*) FROM {_target(conn, table.name)} WHERE NOT ({_check_sql(con)})")
                )
                if count:
                    residuals.append(_proof(f"{table.name}.check.{con.name}", "check_rows", count, metadata))
        for fk in table.foreign_key_constraints:
            cols = [c.name for c in fk.columns]
            if any(f["constrained_columns"] == cols for f in actual["fks"]):
                continue
            parent = fk.referred_table.name
            refs = [e.column.name for e in fk.elements]
            on = " AND ".join(f"p.{quote(p)}=c.{quote(c)}" for c, p in zip(cols, refs, strict=True))
            nonnull = " AND ".join(f"c.{quote(c)} IS NOT NULL" for c in cols)
            count = await conn.scalar(
                text(
                    f"SELECT count(*) FROM {_target(conn, table.name)} c WHERE {nonnull} "
                    f"AND NOT EXISTS (SELECT 1 FROM {_target(conn, parent)} p WHERE {on})"
                )
            )
            if count:
                residuals.append(
                    _proof(f"{table.name}.foreign_keys.columns:" + ",".join(cols), "orphan_rows", count, metadata)
                )
                residuals[-1]["columns"] = cols
        indexes = {i["name"]: i for i in actual["indexes"]}
        for index in table.indexes:
            if not index.unique or index.name in indexes:
                continue
            predicate = index.dialect_options[conn.dialect.name].get("where")
            count = await _duplicates(
                conn,
                table.name,
                [_sql(e, conn) for e in index.expressions],
                predicate="TRUE" if predicate is None else _sql(predicate, conn),
            )
            if count:
                if index.name == CALIBRATION and not accepted_calibration:
                    raise SchemaRepairPending("calibration_confirmation_required")
                residuals.append(
                    _proof(f"{table.name}.indexes.{index.name}", "duplicate_identity_groups", count, metadata)
                )
    return residuals


async def _unproved_unique_guards(conn, left, right, retained, residual_keys, catalogue):
    """An allowed missing constraint still needs its exact, active substitute.

    The retained pairs describe catalogue spelling, not permission to lose
    uniqueness. A partner is either canonical or one of those exact pairs;
    an unrelated same-column index does not widen the contract.
    """
    constraints = {
        (r.table_name, r.constraint_name): r.active
        for r in await conn.execute(
            text(
                "SELECT t.relname AS table_name,c.conname AS constraint_name,"
                "(c.convalidated AND i.indisvalid AND i.indisready) AS active "
                "FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace "
                "JOIN pg_index i ON i.indexrelid=c.conindid "
                "WHERE n.nspname='public' AND c.contype='u'"
            )
        )
    }
    active_indexes = {
        f"{r['table_name']}.indexes.{r['index_name']}": r["indisvalid"] and r["indisready"]
        for r in catalogue["indexes"]
    }
    partners = {k: v for k, v in left.items() if ".indexes." in k}
    partners.update({r["object"]: r["actual"] for r in retained if ".indexes." in r["object"] and r["actual_present"]})
    failures = []
    for key, definition in left.items():
        if ".unique." not in key or key in residual_keys:
            continue
        table, name = key.split(".unique.", 1)
        if key in right:
            if not constraints.get((table, name)):
                failures.append(key)
            continue
        if not any(
            k.startswith(table + ".indexes.")
            and v.get("unique")
            and v.get("column_names") == definition["column_names"]
            and not v.get("expressions")
            and not v.get("dialect_options", {}).get("postgresql_where")
            and right.get(k) == v
            and active_indexes.get(k)
            for k, v in partners.items()
        ):
            failures.append(key)
    return failures


async def verify(conn, metadata, *, accepted_calibration=False, fresh=False):
    """No writes to public objects; called with debt-clear in one transaction."""
    actual = await capture(conn)
    expected = await expected_model_plan(conn, metadata)
    left, right = canonical(expected), canonical(actual)
    retained = [] if fresh else retained_pairs()
    allowed = {json.dumps(r, sort_keys=True) for r in retained}
    residuals = [] if fresh else await data_residuals(conn, metadata, accepted_calibration=accepted_calibration)
    keys = set(left) | {r["object"] for r in retained}
    residual_keys = {r["object"] for r in residuals}
    # Bind unnamed UNIQUE proofs to the exact server-normalised model object.
    for proof in residuals:
        category = next((c for c in ("unique", "foreign_keys") if "." + c + ".columns:" in proof["object"]), None)
        if category:
            table, cols = proof["object"].split("." + category + ".columns:")
            field = "column_names" if category == "unique" else "constrained_columns"
            matches = [
                k for k, v in left.items() if k.startswith(table + "." + category + ".") and v[field] == cols.split(",")
            ]
            if len(matches) != 1:
                raise SchemaRepairPending("ambiguous_unique_plan")
            proof["object"] = matches[0]
            residual_keys.add(matches[0])
            if category == "unique":
                constraint_name = matches[0].split(".unique.", 1)[1]
                residual_keys.update(
                    k
                    for k, v in left.items()
                    if k.startswith(table + ".indexes.") and v.get("duplicates_constraint") == constraint_name
                )
    failures = await _unproved_unique_guards(conn, left, right, retained, residual_keys, actual["catalogue"])
    for proof in residuals:
        if proof["object"] in left:
            proof["definition"] = left[proof["object"]]
    for key in sorted(keys):
        if key in left and key in right and left[key] == right[key]:
            continue
        if key not in left and key not in right:
            continue
        row = {
            "object": key,
            "fresh_present": key in left,
            "actual_present": key in right,
            "fresh": left.get(key),
            "actual": right.get(key),
        }
        if json.dumps(row, sort_keys=True) in allowed:
            continue
        if key in residual_keys and (
            (key.endswith(".nullable") and right.get(key) is True and left.get(key) is False)
            or (key not in right and key in left)
        ):
            continue
        failures.append(key)
    # Expected objects must be active/validated, not just reflect correctly.
    for category, field, flags in (
        ("foreign_keys", "constraint_name", ("convalidated",)),
        ("indexes", "index_name", ("indisvalid", "indisready")),
    ):
        for row in actual["catalogue"][category]:
            key = f"{row['table_name']}.{category}.{row[field]}"
            if key in left and not all(row[f] for f in flags):
                failures.append(key)
    if "print_archives" in metadata.tables:
        from backend.app.core.schema_fts_verify import verify_archive_fts

        await verify_archive_fts(conn)
    if failures:
        logger.warning("Schema verification unresolved objects: %s", failures)
        raise SchemaRepairPending("unverified_schema_objects")
    return residuals
