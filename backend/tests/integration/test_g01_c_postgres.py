"""C migration/timezone and lifetime serialization on a disposable real server."""

from datetime import datetime

import pytest
from sqlalchemy import text

from backend.app.core.config import settings
from backend.app.core.database import Base
from backend.app.migrations import m222_auth_lifetime_schema as schema, m223_retire_legacy_credentials as retire
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_g01_c_migrations import legacy_schema
from backend.tests.integration.test_g01_c_publication import (
    test_clone_batch_changed_membership_cannot_add_unpinned_work as assert_clone_batch_pin,
    test_final_writer_serializes_account_revocation as assert_serialization,
    test_key_new_clone_uses_current_immutable_owner as assert_clone_owner,
    test_revoke_after_capture_refuses_before_install_or_repair as assert_no_publication,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def migration_dialect(test_engine, monkeypatch):
    # Migration helpers use the configured dialect. The global unit fixture
    # deliberately pins SQLite; this owned engine is real PostgreSQL instead.
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))


async def test_lifetime_writer_serialization_on_postgres(test_engine):
    await assert_serialization(test_engine)


async def test_stale_capture_no_publication_on_postgres(test_engine, monkeypatch, tmp_path):
    await assert_no_publication(test_engine, monkeypatch, tmp_path)


async def test_key_clone_owner_on_postgres(test_engine):
    await assert_clone_owner(test_engine)


async def test_changed_clone_batch_on_postgres(test_engine, monkeypatch):
    await assert_clone_batch_pin(test_engine, monkeypatch)


async def test_legacy_migration_non_utc_is_absolute_and_idempotent(test_engine):
    async with test_engine.begin() as conn:
        # This fixture owns a disposable PostgreSQL instance. The existing
        # metadata contains unnamed FK cycles; DROP ... CASCADE avoids asking
        # SQLAlchemy to invent an ordering for those unrelated old tables.
        names = ", ".join(conn.dialect.identifier_preparer.quote(t.name) for t in Base.metadata.tables.values())
        await conn.execute(text(f"DROP TABLE IF EXISTS {names} CASCADE"))
        await legacy_schema(conn)
        await conn.execute(text("SET TIME ZONE 'Pacific/Auckland'"))
        for column in ("created_at", "expires_at", "used_at"):
            await conn.execute(text(f"ALTER TABLE auth_ephemeral_tokens ALTER COLUMN {column} TYPE TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE auth_rate_limit_events ALTER COLUMN occurred_at TYPE TIMESTAMPTZ"))
        await conn.execute(text("INSERT INTO auth_rate_limit_events VALUES (1, '2026-11-11 12:00:00+03')"))
        await conn.execute(
            text(
                "INSERT INTO auth_ephemeral_tokens VALUES "
                "(1, '2026-11-11 12:00:00+03', '2026-11-12 12:00:00+03', '2026-11-11 13:00:00+03')"
            )
        )
        await conn.execute(text("INSERT INTO users VALUES (1), (2)"))
        await conn.execute(text("INSERT INTO api_keys VALUES (1,NULL,'key-identity',true)"))
        await schema.upgrade(conn)
        await retire.upgrade(conn)
        await schema.upgrade(conn)
        await retire.upgrade(conn)
        row = (await conn.execute(text("SELECT created_at,expires_at,used_at FROM auth_ephemeral_tokens"))).one()
        assert tuple(row) == (datetime(2026, 11, 11, 9), datetime(2026, 11, 12, 9), datetime(2026, 11, 11, 10))
        assert await conn.scalar(text("SELECT occurred_at FROM auth_rate_limit_events")) == datetime(2026, 11, 11, 9)
        assert await conn.scalar(text("SELECT COUNT(DISTINCT account_identity) FROM users")) == 2
        assert await conn.scalar(text("SELECT ownerless_api_key_count FROM credential_rollout_episodes")) == 1
        assert await conn.scalar(text("SELECT enabled FROM api_keys")) is False
        assert await conn.scalar(text("SELECT connector_identity FROM api_keys")) == "key-identity"


@pytest.mark.parametrize("already_delivered", [False, True])
async def test_sqlite_move_preserves_rollout_and_cannot_replay_delivery(
    test_engine, tmp_path, monkeypatch, already_delivered
):
    from unittest.mock import AsyncMock

    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from backend.app.core import database
    from backend.app.core.db_portable import import_sqlite_to_postgres
    from backend.app.models.credential_rollout import CredentialRolloutEpisode
    from backend.app.models.user_notification import UserNotification
    from backend.app.services import credential_rollout as cr
    from backend.tests.unit.services.test_credential_rollout import seed

    path = tmp_path / "current.sqlite"
    sqlite = create_async_engine(f"sqlite+aiosqlite:///{path}")
    source = async_sessionmaker(sqlite, expire_on_commit=False)
    try:
        async with sqlite.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        with monkeypatch.context() as source_context:
            source_context.setattr(database, "async_session", source)
            async with source() as db:
                user, episode = await seed(db)
                identity, episode_id = user.account_identity, episode.id
            if already_delivered:
                assert await cr.CredentialRolloutRuntime()._attempt() is False
        await sqlite.dispose()
        await import_sqlite_to_postgres(test_engine, Base.metadata, path)
        factory = async_sessionmaker(test_engine, expire_on_commit=False)
        monkeypatch.setattr(database, "async_session", factory)
        async with test_engine.begin() as conn:
            await schema.upgrade(conn)
            await retire.upgrade(conn)
        live = AsyncMock()
        monkeypatch.setattr(cr, "dispatch_staged", live)
        runtime = cr.CredentialRolloutRuntime()
        assert await runtime._attempt() is False
        assert await runtime._attempt() is False
        async with factory() as db:
            saved = await db.get(CredentialRolloutEpisode, episode_id)
            assert saved.delivered_at is not None
            assert (saved.ownerless_api_key_count, saved.owned_api_key_count, saved.long_lived_token_count) == (2, 3, 4)
            rows = (await db.scalars(select(UserNotification))).all()
            assert len(rows) == 1
            assert rows[0].dedupe_key == f"{cr.EVENT}:{episode_id}:{identity}"
            assert await db.scalar(select(func.count()).select_from(CredentialRolloutEpisode)) == 1
        assert live.await_count == int(not already_delivered)
    finally:
        await sqlite.dispose()
