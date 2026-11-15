"""Repair only G02 B r2's observed historical PostgreSQL drift.

Frozen literals, not a startup diff against mutable ORM metadata. Preserve
unknown NULLs, duplicate identities and native-upgrade orphans as named/count
residuals. Import conform retains its separate, existing orphan policy.
"""

import logging

from sqlalchemy import inspect, text

from backend.app.migrations._pg_schema_228_contract import REPAIRS
from backend.app.migrations._pg_schema_228_fts import ensure_archive_fts

version = 228
name = "postgres_historical_schema"
logger = logging.getLogger(__name__)


def _q(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _table(identifier):
    return "public." + _q(identifier)


async def _columns(conn):
    def read(sync):
        inspector = inspect(sync)
        return {table: {c["name"]: c for c in inspector.get_columns(table)} for table in inspector.get_table_names()}

    return await conn.run_sync(read)


async def _repair_fk(conn, row, columns, residuals):
    table = row["object"].split(".")[0]
    target = row["definition"]
    names = target["constrained_columns"]
    parent = target["referred_table"]
    refs = target["referred_columns"]
    action = row["policy"]["target_on_delete"]
    if table not in columns or parent not in columns or not set(names) <= columns[table].keys():
        residuals.append((row["object"], "missing_columns", 0))
        return
    actual = await conn.run_sync(lambda sync: inspect(sync).get_foreign_keys(table))
    candidates = [fk for fk in actual if fk["constrained_columns"] == names]
    if len(candidates) > 1 or any(
        fk["referred_table"] != parent
        or fk["referred_columns"] != refs
        or fk.get("referred_schema") not in (None, "public")
        for fk in candidates
    ):
        residuals.append((row["object"], "unknown_fk_shape", 0))
        return
    if any(
        {k: v for k, v in fk.get("options", {}).items() if k not in ("ondelete", "not_valid")}
        != {k: v for k, v in target.get("options", {}).items() if k not in ("ondelete", "not_valid")}
        for fk in candidates
    ):
        residuals.append((row["object"], "unknown_fk_options", 0))
        return
    actual_fk = candidates[0] if candidates else None
    observed = row.get("observed_actual")
    known_names = {target["name"]}
    if observed:
        known_names.add(observed["name"])
    if actual_fk and actual_fk["name"] not in known_names:
        residuals.append((row["object"], "unknown_fk_name", 0))
        return
    validated = False
    if actual_fk:
        validated = await conn.scalar(
            text("""
            SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND conname=:name
        """),
            {"table": "public." + table, "name": actual_fk["name"]},
        )
        if validated and actual_fk.get("options", {}).get("ondelete", "NO ACTION") == action:
            return  # A valid FK proves absence of orphans; do not rescan its rows.
    conditions = " AND ".join(f"c.{_q(c)} IS NOT NULL" for c in names)
    join = " AND ".join(f"p.{_q(p)}=c.{_q(c)}" for c, p in zip(names, refs, strict=True))
    dangling = f"{conditions} AND NOT EXISTS (SELECT 1 FROM {_table(parent)} p WHERE {join})"
    count = 0 if validated else await conn.scalar(text(f"SELECT count(*) FROM {_table(table)} c WHERE {dangling}"))
    if count:
        if row["policy"]["orphan_outcome"] == "detach" and all(columns[table][c]["nullable"] for c in names):
            assignments = ", ".join(f"{_q(c)}=NULL" for c in names)
            await conn.execute(text(f"UPDATE {_table(table)} c SET {assignments} WHERE {dangling}"))
        else:
            residuals.append((row["object"], "native_orphan_before_import_conform", count))
            return
    if actual_fk and actual_fk.get("options", {}).get("ondelete", "NO ACTION") == action:
        if not validated:
            await conn.execute(text(f"ALTER TABLE {_table(table)} VALIDATE CONSTRAINT {_q(actual_fk['name'])}"))
        return
    if actual_fk:
        allowed = {"NO ACTION", action}
        if observed:
            allowed.add(observed.get("options", {}).get("ondelete", "NO ACTION"))
        if actual_fk.get("options", {}).get("ondelete", "NO ACTION") not in allowed:
            residuals.append((row["object"], "unknown_fk_action", 0))
            return
        await conn.execute(text(f"ALTER TABLE {_table(table)} DROP CONSTRAINT {_q(actual_fk['name'])}"))
    child = ", ".join(_q(c) for c in names)
    parent_cols = ", ".join(_q(c) for c in refs)
    await conn.execute(
        text(
            f"ALTER TABLE {_table(table)} ADD CONSTRAINT {_q(target['name'])} FOREIGN KEY ({child}) "
            f"REFERENCES {_table(parent)} ({parent_cols}) ON DELETE {action}"
        )
    )


async def _repair_index(conn, row, residuals):
    table = row["object"].split(".")[0]
    target = row["definition"]
    if target["name"] == "idx_archives_search_vector":
        return  # Owned by the guarded FTS helper.
    indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes(table))
    actual = next((idx for idx in indexes if idx["name"] == target["name"]), None)
    if actual:
        comparable = dict(actual)
        # Reflection may supply an empty include list outside dialect_options.
        comparable.pop("include_columns", None)
        if comparable != target:
            without_sort = dict(comparable)
            sort = without_sort.pop("column_sorting", None)
            if (
                row["action"] != "repair_exact_index_order"
                or sort != {"created_at": ("desc",)}
                or without_sort != target
            ):
                residuals.append((row["object"], "unknown_index_shape", 0))
                return
            await conn.execute(text(f"DROP INDEX public.{_q(target['name'])}"))
            actual = None
        else:
            valid = (
                await conn.execute(
                    text("""
                SELECT indisvalid,indisready FROM pg_index WHERE indexrelid=to_regclass(:name)
            """),
                    {"name": "public." + target["name"]},
                )
            ).one()
            if all(valid):
                return
            await conn.execute(text(f"DROP INDEX public.{_q(target['name'])}"))
            actual = None
    if actual is None:
        expressions = target.get("expressions") or [_q(c) for c in target["column_names"]]
        await conn.execute(text(f"CREATE INDEX {_q(target['name'])} ON {_table(table)} ({', '.join(expressions)})"))


async def _repair(conn, *, check_fts_data):
    if conn.dialect.name != "postgresql":
        return
    columns = await _columns(conn)
    residuals = []
    # Missing server defaults must be fixed even if already NOT NULL.
    for row in REPAIRS:
        if not row["object"].endswith(".server_default"):
            continue
        table, column, _ = row["object"].split(".")
        value = columns.get(table, {}).get(column)
        if value is None:
            residuals.append((row["object"], "missing_column", 0))
        elif value["default"] is None:
            await conn.execute(text(f"ALTER TABLE {_table(table)} ALTER COLUMN {_q(column)} SET DEFAULT now()"))
        elif value["default"].lower() not in ("now()", "current_timestamp", "transaction_timestamp()"):
            residuals.append((row["object"], "unknown_default", 0))

    baseline = columns.get("spool", {}).get("weight_used_baseline")
    if baseline and str(baseline["type"]).upper() == "REAL":
        previous = await conn.scalar(text("SHOW extra_float_digits"))
        await conn.execute(text("SET LOCAL extra_float_digits=1"))
        await conn.execute(
            text("""
            ALTER TABLE public.spool ALTER COLUMN weight_used_baseline TYPE DOUBLE PRECISION
            USING weight_used_baseline::text::double precision
        """)
        )
        await conn.execute(text("SELECT set_config('extra_float_digits', :previous, true)"), {"previous": previous})

    for row in REPAIRS:
        action = row["action"]
        if action == "repair_or_named_null_residual":
            table, column = row["object"].split(".")
            value = columns.get(table, {}).get(column)
            if value is None:
                residuals.append((row["object"], "missing_column", 0))
                continue
            if not value["nullable"]:
                continue
            if row["fill_allowed"]:
                await conn.execute(
                    text(f"UPDATE {_table(table)} SET {_q(column)}=:fill WHERE {_q(column)} IS NULL"),
                    {"fill": row["fill"]},
                )
            count = await conn.scalar(text(f"SELECT count(*) FROM {_table(table)} WHERE {_q(column)} IS NULL"))
            if count:
                residuals.append((row["object"], "unknown_null", count))
            elif not any(r[0] == row["object"] + ".server_default" for r in residuals):
                await conn.execute(text(f"ALTER TABLE {_table(table)} ALTER COLUMN {_q(column)} SET NOT NULL"))
        elif ".foreign_key." in row["object"] or row["object"] == "spool_usage_history.archive_id.ondelete":
            await _repair_fk(conn, row, columns, residuals)
        elif ".indexes." in row["object"]:
            await _repair_index(conn, row, residuals)
        elif action == "repair_or_named_duplicate_residual":
            table = row["object"].split(".")[0]
            names = row["columns"]
            uniques = await conn.run_sync(lambda sync, table=table: inspect(sync).get_unique_constraints(table))
            indexes = await conn.run_sync(lambda sync, table=table: inspect(sync).get_indexes(table))
            if any(u["column_names"] == names for u in uniques) or any(
                i.get("unique")
                and i["column_names"] == names
                and not i.get("dialect_options", {}).get("postgresql_where")
                for i in indexes
            ):
                continue
            sql_columns = ", ".join(_q(c) for c in names)
            not_null = " AND ".join(f"{_q(c)} IS NOT NULL" for c in names)
            count = await conn.scalar(
                text(
                    f"SELECT count(*) FROM (SELECT 1 FROM {_table(table)} WHERE {not_null} "
                    f"GROUP BY {sql_columns} HAVING count(*)>1) duplicates"
                )
            )
            if count:
                residuals.append((row["object"], "duplicate_identity_groups", count))
            else:
                constraint = "printers_serial_number_key"  # The one frozen UNIQUE repair target.
                await conn.execute(
                    text(f"ALTER TABLE {_table(table)} ADD CONSTRAINT {_q(constraint)} UNIQUE ({sql_columns})")
                )
    await ensure_archive_fts(conn, check_data=check_fts_data)
    if residuals:
        # Count-only, exact object names; never log row data, grants or paths.
        logger.warning("m228 PG residuals before any import-only conform: %s", residuals)


async def upgrade(conn):
    await _repair(conn, check_fts_data=True)


async def replay(conn):
    """DEBUG head replay: inspect components, never scan correct FTS data again."""
    await _repair(conn, check_fts_data=False)
