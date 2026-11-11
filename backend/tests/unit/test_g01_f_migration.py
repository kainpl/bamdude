"""Remember Me upgrade preserves legacy NULL and explicit new preferences."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m220_auth_remember_me as migration

TOKEN_TYPES = ("oidc_state", "oidc_exchange", "pre_auth", "refresh", "password_reset", "camera_stream")


def legacy_table():
    """Historical columns, independent of future ORM defaults/required fields."""
    return Table(
        "auth_ephemeral_tokens",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("token", String(128), nullable=False, unique=True),
        Column("token_type", String(20), nullable=False),
        Column("username", String(150)),
        Column("provider_id", Integer),
        Column("nonce", String(128)),
        Column("code_verifier", String(128)),
        Column("challenge_id", String(128)),
        Column("used_at", DateTime(timezone=True)),
        Column("family_id", String(32)),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )


async def assert_legacy_remember_me_upgrade(engine, *, populated=True):
    """Shared assertions run against pre-column SQLite and native PG tables."""
    historical = legacy_table()
    when = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with engine.begin() as conn:
        if populated:
            await conn.execute(
                historical.insert(),
                [
                    {
                        "id": index,
                        "token": "legacy-" + kind,
                        "token_type": kind,
                        "username": None if kind == "oidc_state" else "legacy-person",
                        "provider_id": 7 if kind == "oidc_state" else None,
                        "nonce": "kept-nonce" if kind == "oidc_state" else None,
                        "code_verifier": "kept-verifier" if kind == "oidc_state" else None,
                        "challenge_id": "kept-challenge" if kind == "pre_auth" else None,
                        "used_at": when if kind == "refresh" else None,
                        "family_id": "kept-refresh-family" if kind == "refresh" else None,
                        "expires_at": when,
                        "created_at": when,
                    }
                    for index, kind in enumerate(TOKEN_TYPES, 1)
                ],
            )
        before = (await conn.execute(select(historical).order_by(historical.c.id))).all()
        await migration.upgrade(conn)
        columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("auth_ephemeral_tokens"))
        preference = next(column for column in columns if column["name"] == "remember_me")
        assert preference["nullable"] is True and preference["default"] is None
        assert isinstance(preference["type"], Boolean)
        assert (await conn.execute(select(historical).order_by(historical.c.id))).all() == before
        assert (await conn.execute(text("SELECT remember_me FROM auth_ephemeral_tokens"))).all() == [
            (None,) for _ in before
        ]

        # Existing old-style issuers remain distinguishable from explicit false.
        await conn.execute(
            historical.insert(),
            {
                "id": 100,
                "token": "legacy-after-upgrade",
                "token_type": "oidc_state",
                "expires_at": when,
                "created_at": when,
            },
        )
        upgraded = await conn.run_sync(lambda sync: Table("auth_ephemeral_tokens", MetaData(), autoload_with=sync))
        for index, choice in ((101, False), (102, True)):
            await conn.execute(
                upgraded.insert(),
                {
                    "id": index,
                    "token": f"explicit-{choice}",
                    "token_type": "refresh",
                    "expires_at": when,
                    "created_at": when,
                    "remember_me": choice,
                },
            )
        first = (await conn.execute(select(upgraded).order_by(upgraded.c.id))).all()
        await migration.upgrade(conn)
        assert (await conn.execute(select(upgraded).order_by(upgraded.c.id))).all() == first
        choices = dict((await conn.execute(select(upgraded.c.id, upgraded.c.remember_me))).all())
        assert choices[100] is None and choices[101] is False and choices[102] is True


@pytest.mark.parametrize("populated", [False, True])
async def test_sqlite_legacy_rows_and_explicit_preferences_survive_repeat_upgrade(populated):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(legacy_table().metadata.create_all)
        await assert_legacy_remember_me_upgrade(engine, populated=populated)
    finally:
        await engine.dispose()
