"""Legacy ownership is unknown; strict rollout must not guess from row ids or age."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m222_auth_lifetime_schema as schema, m223_retire_legacy_credentials as retire


async def legacy_schema(conn):
    for sql in (
        "CREATE TABLE users (id INTEGER PRIMARY KEY)",
        "CREATE TABLE api_keys (id INTEGER PRIMARY KEY, user_id INTEGER, connector_identity VARCHAR(36), enabled BOOLEAN)",
        "CREATE TABLE long_lived_tokens (id INTEGER PRIMARY KEY, user_id INTEGER, revoked_at TIMESTAMP)",
        "CREATE TABLE auth_ephemeral_tokens (id INTEGER PRIMARY KEY, created_at TIMESTAMP, expires_at TIMESTAMP, used_at TIMESTAMP)",
        "CREATE TABLE auth_rate_limit_events (id INTEGER PRIMARY KEY, occurred_at TIMESTAMP)",
        "CREATE TABLE user_otp_codes (id INTEGER PRIMARY KEY)",
        "CREATE TABLE user_totp (id INTEGER PRIMARY KEY)",
        "CREATE TABLE user_notifications (id INTEGER PRIMARY KEY)",
    ):
        await conn.execute(text(sql))


@pytest.fixture
async def engine(tmp_path):
    instance = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with instance.begin() as conn:
        await legacy_schema(conn)
    yield instance
    await instance.dispose()


@pytest.mark.asyncio
async def test_every_legacy_credential_retired_without_binding_reused_owner(engine):
    identities = [str(uuid.uuid4()) for _ in range(4)]
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO users (id) VALUES (1), (2)"))
        for key_id, owner, enabled in ((1, None, True), (2, 1, True), (3, 999, False), (4, 2, False)):
            await conn.execute(
                text("INSERT INTO api_keys VALUES (:id, :owner, :identity, :enabled)"),
                {"id": key_id, "owner": owner, "identity": identities[key_id - 1], "enabled": enabled},
            )
        await conn.execute(text("INSERT INTO long_lived_tokens VALUES (1, 1, NULL), (2, 999, '2020-01-01')"))
        await schema.upgrade(conn)
        await retire.upgrade(conn)
        keys = (
            await conn.execute(
                text("SELECT enabled, owner_identity, retired_reason, connector_identity FROM api_keys ORDER BY id")
            )
        ).all()
        assert all(not enabled and owner is None for enabled, owner, _, _ in keys)
        assert keys[0].retired_reason == "legacy_ownerless_reissue_required"
        assert all(row.retired_reason == "legacy_credential_reissue_required" for row in keys[1:])
        assert [row.connector_identity for row in keys] == identities
        ll = (
            await conn.execute(text("SELECT revoked_at, owner_identity, token_identity FROM long_lived_tokens"))
        ).all()
        assert all(row.revoked_at and row.owner_identity is None and uuid.UUID(row.token_identity) for row in ll)
        episode = (await conn.execute(text("SELECT * FROM credential_rollout_episodes"))).mappings().one()
        assert (
            episode["ownerless_api_key_count"],
            episode["owned_api_key_count"],
            episode["long_lived_token_count"],
        ) == (1, 3, 2)
        assert episode["delivered_at"] is None
        assert await conn.scalar(text("SELECT COUNT(*) FROM user_notifications")) == 0


@pytest.mark.asyncio
async def test_repeat_upgrade_keeps_identity_notice_facts_and_current_credential(engine):
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO users VALUES (1)"))
        await conn.execute(text("INSERT INTO api_keys VALUES (1, 1, :uuid, 1)"), {"uuid": str(uuid.uuid4())})
        await schema.upgrade(conn)
        await retire.upgrade(conn)
        account = await conn.scalar(text("SELECT account_identity FROM users WHERE id=1"))
        episode = (await conn.execute(text("SELECT * FROM credential_rollout_episodes"))).one()
        await conn.execute(text("UPDATE credential_rollout_episodes SET delivered_at='2026-11-12 12:00:00'"))
        await conn.execute(
            text(
                "INSERT INTO api_keys (id,user_id,connector_identity,enabled,owner_identity,contract_version) "
                "VALUES (2,1,:uuid,1,:owner,1)"
            ),
            {"uuid": str(uuid.uuid4()), "owner": account},
        )
        await schema.upgrade(conn)
        await retire.upgrade(conn)
        assert await conn.scalar(text("SELECT account_identity FROM users WHERE id=1")) == account
        now = (await conn.execute(text("SELECT * FROM credential_rollout_episodes"))).one()
        assert tuple(now[:6]) == tuple(episode[:6])
        assert now.delivered_at
        current = (await conn.execute(text("SELECT enabled,retired_at,retired_reason FROM api_keys WHERE id=2"))).one()
        assert tuple(current) == (1, None, None)


@pytest.mark.asyncio
async def test_fresh_schema_no_empty_episode_and_no_shared_account_default(engine):
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO users VALUES (1), (2), (3)"))
        await schema.upgrade(conn)
        await retire.upgrade(conn)
        values = (await conn.execute(text("SELECT account_identity, session_generation FROM users"))).all()
        assert len({str(uuid.UUID(row.account_identity)) for row in values}) == 3
        assert all(row.session_generation == 0 for row in values)
        assert await conn.scalar(text("SELECT COUNT(*) FROM credential_rollout_episodes")) == 0


@pytest.mark.asyncio
async def test_retirement_and_pending_episode_rollback_together(engine):
    async with engine.begin() as conn:
        await schema.upgrade(conn)
        await conn.execute(text("INSERT INTO api_keys (id,enabled) VALUES (1,1)"))
    async with engine.connect() as conn:
        await retire.upgrade(conn)
        assert await conn.scalar(text("SELECT COUNT(*) FROM credential_rollout_episodes")) == 1
        await conn.rollback()
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT enabled FROM api_keys")) == 1
        assert await conn.scalar(text("SELECT COUNT(*) FROM credential_rollout_episodes")) == 0


@pytest.mark.asyncio
async def test_nullable_dedupe_keeps_default_inbox_compatible(engine):
    async with engine.begin() as conn:
        await schema.upgrade(conn)
        await conn.execute(text("INSERT INTO user_notifications (id) VALUES (1), (2)"))
        await conn.execute(text("INSERT INTO user_notifications (id,dedupe_key) VALUES (3,'episode:owner')"))
        assert await conn.scalar(text("SELECT COUNT(*) FROM user_notifications")) == 3
        with pytest.raises(Exception, match="UNIQUE constraint"):
            await conn.execute(text("INSERT INTO user_notifications (id,dedupe_key) VALUES (4,'episode:owner')"))
