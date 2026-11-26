"""N07 on a disposable embedded PostgreSQL (spec §7 T2, T3).

T3 — m263 raises the groups sequence above every remembered group number, refuses an
exhausted sequence even without carriers, and the SQLite import reserves the same bound.
T2 — the OIDC policy lock serializes a group deletion with an issuance that inserts a
membership in that group (the r2 FK cycle), and the callback's cleanup never waits for
exchange rows an account writer holds (S19, S23). Every pair runs with a bounded
``lock_timeout``, so a hang is a named failure.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app import migrations
from backend.app.core import db_portable, id_allocator, pg_sequences
from backend.app.core.config import settings
from backend.app.migrations import m263_group_id_never_reissued as m263
from backend.app.services import oidc_group_sync
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_p_id_postgres import _fill

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def migration_dialect(test_engine, monkeypatch):
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))


async def _migrations_at(engine, *versions):
    await migrations._ensure_migrations_table(engine)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM _migrations"))
        for v in versions:
            await conn.execute(text("INSERT INTO _migrations (version, name) VALUES (:v, 'x')"), {"v": v})


async def _next_group(engine) -> int:
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text("INSERT INTO groups (name, permissions, is_system) VALUES (:n, '[]', false) RETURNING id"),
                {"n": f"g-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar()


async def _raw(engine, sql, **params):
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL session_replication_role = replica"))  # FKs off for the fixture rows
        await conn.execute(text(sql), params)


# --- T3: m263 on PostgreSQL ------------------------------------------------------------------


async def test_m263_raises_the_sequence_above_every_remembered_group_number(test_engine):
    await _migrations_at(test_engine, *range(263))
    newest = await _next_group(test_engine)
    await _raw(test_engine, "DELETE FROM groups WHERE id = :i", i=newest)
    await _raw(test_engine, "INSERT INTO user_groups (user_id, group_id) VALUES (1, :g)", g=newest + 20)
    async with test_engine.begin() as conn:
        await _fill(conn, "telegram_chats", returning=False, group_id=newest + 30)
    async with test_engine.begin() as conn:
        await m263.upgrade(conn)
    assert await _next_group(test_engine) == newest + 31


async def test_m263_refuses_an_exhausted_sequence_without_carriers(test_engine):
    await _migrations_at(test_engine, *range(263))
    async with test_engine.begin() as conn:
        state = await pg_sequences.sequence_state(conn, "groups")
        await conn.execute(text("SELECT setval(:s, :v, true)"), {"s": state.name, "v": id_allocator.MAX_ID})
    with pytest.raises(pg_sequences.SequenceError, match="exhausted"):
        async with test_engine.begin() as conn:
            await m263.upgrade(conn)


async def test_the_import_reserves_the_numbers_a_pre_m263_file_remembers(test_engine, tmp_path):
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as db:
        db.executescript(
            "CREATE TABLE _migrations(id INTEGER PRIMARY KEY, version INTEGER NOT NULL UNIQUE);"
            "INSERT INTO _migrations VALUES (1, 51);"
            "CREATE TABLE groups(id INTEGER NOT NULL, name TEXT, PRIMARY KEY (id));"
            "INSERT INTO groups VALUES (3, 'Team');"
            "CREATE TABLE oidc_providers(id INTEGER PRIMARY KEY, default_group_id INTEGER);"
            "INSERT INTO oidc_providers VALUES (1, 77);"  # the default group was deleted; its number stays
        )
    model = MetaData()
    Table("groups", model, Column("id", Integer, primary_key=True), Column("name", String))
    Table("oidc_providers", model, Column("id", Integer, primary_key=True), Column("default_group_id", Integer))
    await db_portable.import_sqlite_to_postgres(test_engine, model, source)
    async with test_engine.begin() as conn:
        next_id = (await conn.execute(text("INSERT INTO groups DEFAULT VALUES RETURNING id"))).scalar()
    assert next_id == 78


# --- T2: two sessions --------------------------------------------------------------------------


async def _timeout(session, ms=5000):
    await session.execute(text(f"SET LOCAL lock_timeout = '{ms}ms'"))


async def test_a_group_deletion_waits_for_an_issuance_inserting_a_membership(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    group = await _next_group(test_engine)
    async with test_engine.begin() as conn:
        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (account_identity, username, role, auth_source, is_active, session_generation,"
                    " bambu_cloud_generation, orca_cloud_generation) VALUES (:a, :u, 'user', 'oidc', true, 0, 0, 0)"
                    " RETURNING id"
                ),
                {"a": str(uuid.uuid4()), "u": f"u-{uuid.uuid4().hex[:6]}"},
            )
        ).scalar()
        await conn.execute(
            text(
                "INSERT INTO oidc_providers (name, issuer_url, client_id, client_secret, default_group_id,"
                " scopes, is_enabled, auto_create_users, auto_link_existing_accounts, email_claim,"
                " require_email_verified, is_autologin, is_env_managed)"
                " VALUES ('pg', 'https://id', 'c', '', :g, 'openid', true, false, false, 'email', true, false, false)"
            ),
            {"g": group},
        )

    issuance = factory()
    await _timeout(issuance)
    await oidc_group_sync.lock_oidc_policy(issuance)  # account guard → policy lock, then the membership
    await issuance.execute(text("INSERT INTO user_groups (user_id, group_id) VALUES (:u, :g)"), {"u": user, "g": group})

    async def delete_group():
        async with factory() as deletion:
            await _timeout(deletion)
            await oidc_group_sync.lock_oidc_policy(deletion)  # the route's order: policy lock, then the row
            await deletion.execute(text("DELETE FROM groups WHERE id = :g"), {"g": group})
            await deletion.commit()

    task = asyncio.create_task(delete_group())
    await asyncio.sleep(0.5)
    assert not task.done()  # waiting on the policy lock, not on a row it would deadlock over
    await issuance.commit()
    await issuance.close()
    await asyncio.wait_for(task, 10)
    async with test_engine.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM groups WHERE id = :g"), {"g": group})).scalar() == 0
        assert (
            await conn.execute(text("SELECT count(*) FROM user_groups WHERE group_id = :g"), {"g": group})
        ).scalar() == 0
        default = (await conn.execute(text("SELECT default_group_id FROM oidc_providers WHERE name = 'pg'"))).scalar()
        assert default is None  # FK SET NULL, inside the deletion's transaction


async def test_the_cleanup_never_waits_for_exchange_rows_an_account_writer_holds(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    expired = datetime.utcnow() - timedelta(minutes=5)
    account = str(uuid.uuid4())
    async with test_engine.begin() as conn:
        for i in range(4):
            await conn.execute(
                text(
                    "INSERT INTO auth_ephemeral_tokens (token, token_type, auth_source, account_identity,"
                    " contract_version, expires_at, created_at) VALUES (:t, 'oidc_exchange', 'oidc', :a, 0, :e, :e)"
                ),
                {"t": f"held-{i}", "a": account if i < 2 else str(uuid.uuid4()), "e": expired},
            )

    unlink = factory()  # holds two of this account's rows, like unlink or stage_end_sessions
    await _timeout(unlink)
    await unlink.execute(text("DELETE FROM auth_ephemeral_tokens WHERE account_identity = :a"), {"a": account})

    from sqlalchemy import delete, select

    from backend.app.models.auth_ephemeral import AuthEphemeralToken

    async with factory() as cleanup:
        await _timeout(cleanup, 2000)
        ids = (
            (
                await cleanup.execute(
                    select(AuthEphemeralToken.id)
                    .where(
                        AuthEphemeralToken.token_type == "oidc_exchange",
                        AuthEphemeralToken.expires_at < datetime.utcnow(),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        await cleanup.execute(delete(AuthEphemeralToken).where(AuthEphemeralToken.id.in_(ids)))
        await cleanup.commit()  # done without waiting: the held rows were skipped
    assert len(ids) == 2
    await unlink.commit()
    await unlink.close()
    async with test_engine.begin() as conn:
        assert (
            await conn.execute(text("SELECT count(*) FROM auth_ephemeral_tokens WHERE token LIKE 'held-%'"))
        ).scalar() == 0
