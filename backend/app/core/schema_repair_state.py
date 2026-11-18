"""Durable restore repair ownership; records live in the replaced database.

No process flag, migration marker or imported approval can complete a debt.
All mutation functions use the caller's transaction. Public settings and Git
settings export must exclude the entire reserved namespace.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, func, inspect, text

PREFIX = "_bamdude."
DEBT = PREFIX + "schema_repair_debt"
RESIDUALS = PREFIX + "schema_repair_residuals"
PORTABLE = PREFIX + "portable_schema_residuals"
AUDIT = PREFIX + "schema_restore_audit."
VERSION = 1


class SchemaRepairPending(RuntimeError):
    """Safe reason code only: the web process must remain closed maintenance."""

    def __init__(self, reason="repair_failed"):
        self.reason = reason
        super().__init__("Database schema repair is pending; stop the service and run schema_repair retry. " + reason)


def private_setting(key):
    return isinstance(key, str) and key.startswith(PREFIX)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_hash(metadata):
    """Stable across interpreters; no Python-default invocation or set order."""
    from backend.app.core.pg_schema_contract import retained_pairs
    from backend.app.core.schema_index_repair import manifest
    from backend.app.migrations._pg_schema_228_contract import REPAIRS
    from backend.app.migrations.m031_oidc_email_claim import _CHECK_FORMULA

    tables = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.name):
        columns = [
            [
                c.name,
                str(c.type),
                c.nullable,
                c.primary_key,
                str(c.server_default.arg) if c.server_default is not None else None,
            ]
            for c in table.c
            if not c.info.get("migration_shim")
        ]
        constraints = []
        for con in table.constraints:
            row = [type(con).__name__, con.name, [c.name for c in con.columns]]
            if hasattr(con, "sqltext"):
                row.append(str(con.sqltext))
            if hasattr(con, "elements"):
                row.extend([[e.target_fullname for e in con.elements], con.ondelete, con.onupdate])
            constraints.append(row)
        indexes = [
            [
                i.name,
                i.unique,
                [str(e) for e in i.expressions],
                str(i.dialect_options["sqlite"].get("where")),
                str(i.dialect_options["postgresql"].get("where")),
            ]
            for i in table.indexes
        ]
        tables.append([table.name, columns, sorted(constraints, key=encode), sorted(indexes, key=encode)])
    payload = {
        "version": VERSION,
        "models": tables,
        "B": REPAIRS,
        "D4": manifest(),
        "accepted": retained_pairs(),
        "m031": _CHECK_FORMULA,
    }
    return hashlib.sha256(encode(payload).encode()).hexdigest()


async def has_settings(conn):
    return await conn.run_sync(lambda sync: inspect(sync).has_table("settings"))


async def read_raw(conn, key):
    if not await has_settings(conn):
        return None
    rows = (await conn.execute(text('SELECT value FROM settings WHERE "key"=:key'), {"key": key})).scalars().all()
    if len(rows) > 1:
        raise SchemaRepairPending("duplicate_private_metadata")
    return rows[0] if rows else None


async def read_record(conn, key):
    raw = await read_raw(conn, key)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise SchemaRepairPending("invalid_private_metadata") from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION:
        raise SchemaRepairPending("unknown_private_contract")
    return value


async def put_record(conn, key, value):
    # Operational control data is additive after the AS-IS load. This frozen
    # shape also permits low-level imports of minimal historical test schemas.
    if not await has_settings(conn):
        table = Table(
            "settings",
            MetaData(),
            Column("id", Integer, primary_key=True),
            Column("key", String(100), nullable=False, unique=True),
            Column("value", Text, nullable=False),
            Column("created_at", DateTime, server_default=func.now()),
            Column("updated_at", DateTime, server_default=func.now()),
        )
        await conn.run_sync(lambda sync: table.create(sync))
    present = await conn.run_sync(lambda sync: {c["name"] for c in inspect(sync).get_columns("settings")})
    values = {"key": key, "value": encode(value)}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    values.update({c: now for c in ("created_at", "updated_at") if c in present})
    await conn.execute(text('DELETE FROM settings WHERE "key"=:key'), {"key": key})
    columns = ",".join('"' + c + '"' for c in values)
    params = ",".join(":" + c for c in values)
    await conn.execute(text(f"INSERT INTO settings({columns}) VALUES({params})"), values)


async def write_pending(conn, metadata, *, acceptance=None):
    """Must run inside the replacement transaction / prepared SQLite image."""
    identity = str(uuid4())
    state = {
        "version": VERSION,
        "id": identity,
        "manifest": manifest_hash(metadata),
        "automatic_attempts_remaining": 2,
        "last_failure": None,
        "acceptance": None,
    }
    if acceptance is not None:
        audit_key = AUDIT + identity
        await put_record(conn, audit_key, {**acceptance, "version": VERSION, "restore_id": identity})
        state["acceptance"] = audit_key
    await put_record(conn, DEBT, state)
    # A source report is diagnostic, never target repair authority. Phases may
    # run before final verification (and may encounter older source markers),
    # so start their target-owned report afresh in this same replacement txn.
    await put_record(conn, RESIDUALS, {"version": VERSION, "entries": {}})
    return state


def validate_debt(value, metadata):
    allowed = {"version", "id", "manifest", "automatic_attempts_remaining", "last_failure", "acceptance"}
    try:
        if set(value) != allowed or type(value["version"]) is not int or value["version"] != VERSION:
            raise ValueError
        if not isinstance(value["id"], str) or not isinstance(value["manifest"], str):
            raise ValueError
        UUID(value["id"])
        if value["manifest"] != manifest_hash(metadata):
            raise SchemaRepairPending("unknown_manifest")
        budget = value["automatic_attempts_remaining"]
        if type(budget) is not int or not 0 <= budget <= 2:
            raise ValueError
        if value["last_failure"] is not None and not isinstance(value["last_failure"], str):
            raise ValueError
        if value["acceptance"] is not None and value["acceptance"] != AUDIT + value["id"]:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise SchemaRepairPending("invalid_debt") from exc


async def claim_attempt(conn, metadata, *, automatic=True):
    state = await read_record(conn, DEBT)
    if state is None:
        return None
    validate_debt(state, metadata)
    if automatic and state["automatic_attempts_remaining"] == 0:
        raise SchemaRepairPending("manual_retry_required")
    updated = {**state, "automatic_attempts_remaining": state["automatic_attempts_remaining"] - 1 if automatic else 0}
    result = await conn.execute(
        text('UPDATE settings SET value=:new WHERE "key"=:key AND value=:old'),
        {"key": DEBT, "old": encode(state), "new": encode(updated)},
    )
    if result.rowcount != 1:
        raise SchemaRepairPending("debt_changed")
    return updated


async def fail_attempt(conn, claimed, reason):
    updated = {**claimed, "automatic_attempts_remaining": 0, "last_failure": reason}
    result = await conn.execute(
        text('UPDATE settings SET value=:new WHERE "key"=:key AND value=:old'),
        {"key": DEBT, "old": encode(claimed), "new": encode(updated)},
    )
    if result.rowcount != 1:
        raise SchemaRepairPending("debt_changed")


async def finish_attempt(conn, claimed, residuals):
    """Caller has verified the COMPLETE plan in this same transaction."""
    # This owner has just reverified the complete canonical plan. Imported or
    # formerly missing objects that are now enforced cannot leave stale debt.
    entries = {r["object"]: r for r in residuals}
    await put_record(conn, RESIDUALS, {"version": VERSION, "entries": entries})
    result = await conn.execute(
        text('DELETE FROM settings WHERE "key"=:key AND value=:expected'),
        {"key": DEBT, "expected": encode(claimed)},
    )
    if result.rowcount != 1:
        # Raising rolls back residual publication as well; a new owner wins.
        raise SchemaRepairPending("debt_changed")
