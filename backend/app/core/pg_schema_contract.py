"""Shared exact PostgreSQL schema comparison. Never execute literal defaults.

Used by runtime recovery and the independent scenario gate.
"""

import decimal
import json
import re
from pathlib import Path

from sqlalchemy import inspect, text


def default_key(value):
    if value is None:
        return None
    value = value.strip()
    if value.lower() in ("now()", "current_timestamp", "transaction_timestamp()"):
        return ["timestamp", "transaction_start"]
    if value.lower() in ("true", "false"):
        return ["boolean", value.lower()]
    boolean = re.fullmatch(r"'(true|false)'::boolean", value, re.I)
    if boolean:
        return ["boolean", boolean[1].lower()]
    # Only literal numeric casts: never evaluate default SQL from a DB/file.
    numeric = re.fullmatch(
        r"([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)(?:::(?:integer|smallint|bigint|real|double precision|numeric|decimal))?",
        value,
        re.I,
    )
    if not numeric:
        numeric = re.fullmatch(
            r"'([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)'::(?:integer|smallint|bigint|real|double precision|numeric|decimal)",
            value,
            re.I,
        )
    if numeric:
        return ["numeric", str(decimal.Decimal(numeric[1]).normalize())]
    return ["sql", value]


def snapshot(sync, namespace=None):
    inspector = inspect(sync)
    out = {}
    names = (
        inspector.get_temp_table_names()
        if namespace is not None and namespace.startswith("pg_temp_")
        else inspector.get_table_names(schema=namespace)
    )
    for table in sorted(names):
        columns = []
        for column in inspector.get_columns(table, schema=namespace):
            typ = column["type"]
            columns.append(
                {
                    "name": column["name"],
                    "type": str(typ),
                    "length": getattr(typ, "length", None),
                    "precision": getattr(typ, "precision", None),
                    "scale": getattr(typ, "scale", None),
                    "timezone": getattr(typ, "timezone", None),
                    "nullable": column["nullable"],
                    "default": default_key(column.get("default")),
                    "default_raw": column.get("default"),
                    "identity": column.get("identity"),
                }
            )
        out[table] = {
            "columns": columns,
            "pk": inspector.get_pk_constraint(table, schema=namespace),
            "foreign_keys": sorted(
                inspector.get_foreign_keys(table, schema=namespace), key=lambda x: str(x.get("name"))
            ),
            "unique": sorted(
                inspector.get_unique_constraints(table, schema=namespace), key=lambda x: str(x.get("name"))
            ),
            "check": sorted(inspector.get_check_constraints(table, schema=namespace), key=lambda x: str(x.get("name"))),
            "indexes": sorted(inspector.get_indexes(table, schema=namespace), key=lambda x: str(x.get("name"))),
        }
    return out


def index_definition(index):
    """Drop SQLAlchemy 2.0's duplicate INCLUDE field when both lists agree."""
    options = index.get("dialect_options", {})
    if (
        "include_columns" in index
        and isinstance(options, dict)
        and "postgresql_include" in options
        and index["include_columns"] == options["postgresql_include"]
    ):
        return {key: value for key, value in index.items() if key != "include_columns"}
    return index


def canonical(snapshot):
    out = {}
    for table, schema in snapshot["schema"].items():
        for column in schema["columns"]:
            for field, value in column.items():
                # Parsed literals compare by value; unparsed SQL remains exact in default.
                if field not in ("name", "default_raw"):
                    if field == "default":
                        value = default_key(column.get("default_raw"))
                    out[f"{table}.column.{column['name']}.{field}"] = value
        out[f"{table}.primary_key"] = schema["pk"]
        for field in ("foreign_keys", "unique", "check", "indexes"):
            for obj in schema[field]:
                if field == "indexes":
                    obj = index_definition(obj)
                key = obj.get("name")
                if not key:
                    key = "unnamed:" + json.dumps(obj, sort_keys=True)
                out[f"{table}.{field}.{key}"] = obj
    for category, values in snapshot["extra"].items():
        for value in values:
            if category == "triggers":
                key = value["event_object_table"] + "." + value["trigger_name"] + "." + value["event_manipulation"]
            else:
                key = value.get("sequencename") or value.get("proname")
            if category == "functions" and key == "archive_search_vector_update":
                value = dict(value)
                # pg_get_functiondef keeps body boundary whitespace. This
                # known frozen PL/pgSQL body has identical tokens either way;
                # full body/identity/language/config are also checked below.
                value["definition"] = re.sub(r"\s*\$function\$\s*", "$function$", value["definition"])
            assert key, (category, value)
            target = f"{category}.{key}"
            assert target not in out, target
            out[target] = value
    return out


def compare(reference, actual):
    left, right = canonical(reference), canonical(actual)
    return [
        {
            "object": key,
            "fresh_present": key in left,
            "actual_present": key in right,
            "fresh": left.get(key),
            "actual": right.get(key),
        }
        for key in sorted(left.keys() | right.keys())
        if left.get(key) != right.get(key) or (key in left) != (key in right)
    ]


async def catalogue_state(conn, namespace="public"):
    queries = {
        "functions": """SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS identity_arguments,
        p.prosrc,l.lanname,p.provolatile::text AS provolatile,p.prosecdef,p.proconfig
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname=:namespace AND p.prokind='f' ORDER BY p.proname,identity_arguments""",
        "foreign_keys": """SELECT c.relname AS table_name, k.conname AS constraint_name, k.convalidated
        FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:namespace AND k.contype='f' ORDER BY c.relname,k.conname""",
        "triggers": """SELECT c.relname AS table_name,t.tgname AS trigger_name,t.tgenabled::text AS tgenabled
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:namespace AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
        "indexes": """SELECT t.relname AS table_name,c.relname AS index_name,i.indisvalid,i.indisready
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_class t ON t.oid=i.indrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=:namespace ORDER BY t.relname,c.relname""",
    }
    return {
        k: [dict(r._mapping) for r in (await conn.execute(text(q), {"namespace": namespace})).all()]
        for k, q in queries.items()
    }


async def capture(conn, *, namespace="public"):
    schema = await conn.run_sync(lambda sync: snapshot(sync, None if namespace == "public" else namespace))
    extra = {}
    for key, query in {
        "sequences": "select schemaname,sequencename,data_type,start_value,min_value,max_value,increment_by,cycle from pg_sequences where schemaname=:namespace order by sequencename",
        "triggers": "select event_object_table,trigger_name,event_manipulation,action_statement from information_schema.triggers where trigger_schema=:namespace order by event_object_table,trigger_name,event_manipulation",
        "functions": "select proname,pg_get_functiondef(p.oid) as definition from pg_proc p join pg_namespace n on p.pronamespace=n.oid where n.nspname=:namespace and p.prokind='f' order by proname",
    }.items():
        extra[key] = [dict(r._mapping) for r in (await conn.execute(text(query), {"namespace": namespace})).all()]
    for function in extra["functions"]:
        if function["proname"] == "archive_search_vector_update":
            function["definition"] = re.sub(r"\s+", " ", function["definition"]).strip()
    nulls = {}
    from backend.app.migrations._pg_schema_228_contract import REPAIRS

    for obj in [
        r["object"] for r in REPAIRS if r["action"] == "repair_or_named_null_residual" and not r["fill_allowed"]
    ]:
        table, column = obj.split(".")
        if table in schema and any(c["name"] == column and c["nullable"] for c in schema[table]["columns"]):
            count = await conn.scalar(text(f'SELECT count(*) FROM "{namespace}"."{table}" WHERE "{column}" IS NULL'))
            if count:
                nulls[obj] = count
    result = {
        "schema": schema,
        "extra": extra,
        "catalogue": await catalogue_state(conn, namespace),
        "null_residuals": nulls,
    }
    for function in result["catalogue"]["functions"]:
        if function["proname"] == "archive_search_vector_update":
            function["prosrc"] = re.sub(r"\s+", " ", function["prosrc"]).strip()
    # A JSON roundtrip makes SQLAlchemy's tuple/list representations stable.
    return json.loads(json.dumps(result, default=str))


def retained_pairs():
    directory = Path(__file__).parents[1] / "data"
    # The B whitelist stays frozen. Composing with fixes' m228 adds exactly
    # three constant defaults equivalent to the history writer's defaults.
    return json.loads((directory / "pg_schema_b_retained_drift_v1.json").read_text(encoding="utf-8")) + json.loads(
        (directory / "pg_schema_spoolman_defaults_v1.json").read_text(encoding="utf-8")
    )


def unexpected(reference, actual, accepted=None):
    """Every exemption is an exact old/new pair; never a table/name wildcard."""
    allowed = {json.dumps(r, sort_keys=True) for r in (retained_pairs() if accepted is None else accepted)}
    for obj, count in actual["null_residuals"].items():
        assert count > 0
        table, column = obj.split(".")
        row = {
            "object": f"{table}.column.{column}.nullable",
            "fresh_present": True,
            "actual_present": True,
            "fresh": False,
            "actual": True,
        }
        allowed.add(json.dumps(row, sort_keys=True))
    differences = [r for r in compare(reference, actual) if json.dumps(r, sort_keys=True) not in allowed]
    for category, flags in [("foreign_keys", ["convalidated"]), ("indexes", ["indisvalid", "indisready"])]:
        for row in actual["catalogue"][category]:
            if not all(row[flag] for flag in flags):
                differences.append({"object": "catalogue." + category, "actual": row})
    for row in actual["catalogue"]["triggers"]:
        if row["tgenabled"] != "O":
            differences.append({"object": "catalogue.triggers", "actual": row})
    functions = {(r["proname"], r["identity_arguments"]): r for r in reference["catalogue"]["functions"]}
    for row in actual["catalogue"]["functions"]:
        key = row["proname"], row["identity_arguments"]
        if key in functions and row != functions[key]:
            differences.append({"object": "catalogue.functions." + key[0], "actual": row, "fresh": functions[key]})
    return differences
