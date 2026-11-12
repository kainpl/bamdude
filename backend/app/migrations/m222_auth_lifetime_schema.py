"""Give accounts durable lifetimes and stage the complete credential contract.

This is additive schema only. Account/token identities are random per row;
numeric ids and timestamps are never used to reconstruct historical ownership.
The API key's existing connector_identity remains its only immutable identity.
Legacy credential columns default to contract zero, including new rows inserted
by an older runtime. Only guarded current issuers explicitly write version one.

Definitions below are frozen SQL, independent of current models/services. The
following migration records retirement facts; notifications are runtime work.
"""

import uuid

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, text

from backend.app.migrations.helpers import add_column, column_exists, table_exists

version = 222
name = "auth_lifetime_schema"


async def _identity(conn, table, column):
    await add_column(conn, table, f"{column} VARCHAR(36)")
    rows = (await conn.execute(text(f"SELECT id FROM {table} WHERE {column} IS NULL OR {column} = ''"))).all()
    for (row_id,) in rows:
        await conn.execute(
            text(f"UPDATE {table} SET {column} = :identity WHERE id = :id"),
            {"identity": str(uuid.uuid4()), "id": row_id},
        )
    await conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))
    if conn.dialect.name == "postgresql":
        await conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"))


def _tables():
    metadata = MetaData()
    Table("users", metadata, Column("id", Integer, primary_key=True))
    sessions = Table(
        "auth_sessions",
        metadata,
        Column("sid", String(32), primary_key=True),
        Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        Column("account_identity", String(36), nullable=False, index=True),
        Column("generation", BigInteger, nullable=False),
        Column("auth_source", String(20), nullable=False),
        Column("remember_me", Boolean, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("expires_at", DateTime, nullable=False),
        Column("revoked_at", DateTime, nullable=True),
        Column("revoked_reason", String(64), nullable=True),
    )
    episodes = Table(
        "credential_rollout_episodes",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("contract_key", String(80), nullable=False, unique=True),
        Column("ownerless_api_key_count", Integer, nullable=False),
        Column("owned_api_key_count", Integer, nullable=False),
        Column("long_lived_token_count", Integer, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("delivered_at", DateTime, nullable=True),
    )
    return sessions, episodes


async def _utc_auth_timestamps(conn):
    # Old PG auth columns are timestamptz; normalize the absolute instant to
    # stored UTC before removing tz awareness. Already-naive SQLite/PG values
    # are UTC and must not be shifted by the server/session timezone.
    if conn.dialect.name != "postgresql":
        return
    for table, columns in (
        ("auth_ephemeral_tokens", ("created_at", "expires_at", "used_at")),
        ("auth_rate_limit_events", ("occurred_at",)),
    ):
        for column in columns:
            kind = await conn.scalar(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
                ),
                {"table": table, "column": column},
            )
            if kind == "timestamp with time zone":
                await conn.execute(
                    text(
                        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP WITHOUT TIME ZONE "
                        f"USING {column} AT TIME ZONE 'UTC'"
                    )
                )


async def upgrade(conn):
    if await table_exists(conn, "users"):
        await _identity(conn, "users", "account_identity")
        for column in ("session_generation", "bambu_cloud_generation", "orca_cloud_generation"):
            await add_column(conn, "users", f"{column} BIGINT NOT NULL DEFAULT 0")
    for table in ("api_keys", "long_lived_tokens"):
        if not await table_exists(conn, table):
            continue
        if table == "long_lived_tokens":
            await _identity(conn, table, "token_identity")
        for definition in (
            "owner_identity VARCHAR(36)",
            "contract_version INTEGER NOT NULL DEFAULT 0",
            "retired_at TIMESTAMP",
            "retired_reason VARCHAR(64)",
        ):
            await add_column(conn, table, definition)
    if await table_exists(conn, "auth_ephemeral_tokens"):
        for definition in (
            "contract_version INTEGER NOT NULL DEFAULT 0",
            "account_identity VARCHAR(36)",
            "generation BIGINT",
            "sid VARCHAR(32)",
            "key_identity VARCHAR(36)",
            "token_identity VARCHAR(36)",
            "auth_source VARCHAR(20)",
        ):
            await add_column(conn, "auth_ephemeral_tokens", definition)
    if await table_exists(conn, "user_otp_codes"):
        for definition in (
            "account_identity VARCHAR(36)",
            "generation BIGINT",
            "sid VARCHAR(32)",
            "challenge_id VARCHAR(128)",
        ):
            await add_column(conn, "user_otp_codes", definition)
    if await table_exists(conn, "user_totp"):
        for definition in (
            "pending_account_identity VARCHAR(36)",
            "pending_generation BIGINT",
            "pending_sid VARCHAR(32)",
        ):
            await add_column(conn, "user_totp", definition)
    if await table_exists(conn, "user_filament_presets"):
        for column in ("bambu_pushed_owner_identity", "orca_pushed_owner_identity"):
            await add_column(conn, "user_filament_presets", f"{column} VARCHAR(36)")
    for table in _tables():
        await conn.run_sync(lambda sync_conn, definition=table: definition.create(sync_conn, checkfirst=True))
    if await table_exists(conn, "user_notifications"):
        if not await column_exists(conn, "user_notifications", "dedupe_key"):
            await add_column(conn, "user_notifications", "dedupe_key VARCHAR(160)")
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_notifications_dedupe_key ON user_notifications (dedupe_key)"
            )
        )
    await _utc_auth_timestamps(conn)
