"""N07 spec §7 T5: m264 on a SQLite database that already has rows.

The older shape is the current model minus the five N07 columns; after the
migration the rows read through the ORM with the defaults the spec names, and a
fresh ``create_all`` declares the same NOT NULL and defaults.
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.database import Base, import_all_models
from backend.app.migrations import m264_oidc_group_sync as m264

pytestmark = pytest.mark.asyncio

NEW = {
    "oidc_providers": {"group_claim", "group_mapping", "policy_revision"},
    "user_oidc_links": {"first_login_pending"},
    "auth_ephemeral_tokens": {"oidc_login_evidence"},
}


def _older_metadata() -> MetaData:
    import_all_models()
    older = MetaData()
    for name in ("users", "groups", "oidc_providers", "user_oidc_links", "auth_ephemeral_tokens"):
        table = Base.metadata.tables[name]
        Table(
            name,
            older,
            *[c.copy() for c in table.columns if c.name not in NEW.get(name, set())],
        )
    return older


async def test_existing_rows_read_through_the_orm_after_the_migration(tmp_path):
    from backend.app.models.auth_ephemeral import AuthEphemeralToken
    from backend.app.models.oidc_provider import OIDCProvider, UserOIDCLink

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm264.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_older_metadata().create_all)
            await conn.execute(
                text(
                    "INSERT INTO oidc_providers (id, name, issuer_url, client_id, client_secret, scopes, is_enabled,"
                    " auto_create_users, auto_link_existing_accounts, email_claim, require_email_verified,"
                    " is_autologin, is_env_managed) VALUES (1, 'old', 'https://id', 'c', '', 'openid', 1, 0, 0,"
                    " 'email', 1, 0, 0)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO users (id, account_identity, session_generation, bambu_cloud_generation,"
                    " orca_cloud_generation, username, role, auth_source, is_active) VALUES"
                    " (1, '00000000-0000-0000-0000-000000000001', 0, 0, 0, 'old-user', 'user', 'oidc', 1)"
                )
            )
            await conn.execute(
                text("INSERT INTO user_oidc_links (id, user_id, provider_id, provider_user_id) VALUES (1, 1, 1, 'sub')")
            )
            await conn.execute(
                text(
                    "INSERT INTO auth_ephemeral_tokens (id, token, token_type, contract_version, expires_at, created_at)"
                    " VALUES (1, 'old-exchange', 'oidc_exchange', 0, '2030-01-01 00:00:00', '2030-01-01 00:00:00')"
                )
            )
            await m264.upgrade(conn)
            await m264.upgrade(conn)  # repeat-safe (DEBUG replay)
            for table, columns in NEW.items():
                present = {row[1] for row in (await conn.exec_driver_sql(f"PRAGMA table_info({table})")).all()}
                assert columns <= present, table
            evidence_is_null = (
                await conn.execute(text("SELECT oidc_login_evidence IS NULL FROM auth_ephemeral_tokens WHERE id = 1"))
            ).scalar()
            assert evidence_is_null == 1

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            provider = await session.get(OIDCProvider, 1)
            assert provider.group_claim == "groups" and provider.group_mapping == {}
            assert provider.policy_revision == "0" and not provider.group_mapping_invalid
            link = await session.get(UserOIDCLink, 1)
            assert link.first_login_pending is False
            token = await session.scalar(select(AuthEphemeralToken).where(AuthEphemeralToken.id == 1))
            assert token.oidc_login_evidence is None

            # Any policy change replaces the constant initial revision.
            provider.is_enabled = False
            await session.commit()
            assert (await session.get(OIDCProvider, 1)).policy_revision not in {"0", ""}
    finally:
        await engine.dispose()


def test_the_fresh_schema_declares_the_same_not_null_and_defaults():
    import_all_models()
    columns = Base.metadata.tables["oidc_providers"].c
    assert not columns.group_claim.nullable and columns.group_claim.server_default.arg == "groups"
    assert not columns.group_mapping.nullable and columns.group_mapping.server_default.arg == "{}"
    assert not columns.policy_revision.nullable and columns.policy_revision.server_default.arg == "0"
    link = Base.metadata.tables["user_oidc_links"].c.first_login_pending
    assert not link.nullable and link.server_default.arg == "0"
    assert Base.metadata.tables["auth_ephemeral_tokens"].c.oidc_login_evidence.nullable
