"""F's additive Remember Me migration on owned disposable native PostgreSQL."""

import pytest
from sqlalchemy import text

from backend.app.core.config import settings
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.unit.test_g01_f_migration import assert_legacy_remember_me_upgrade

pytestmark = [pytest.mark.integration, pytest.mark.slow]


async def test_native_postgres_legacy_preferences_are_nullable_and_upgrade_is_idempotent(test_engine, monkeypatch):
    # Patch settings, never DATABASE_URL: helpers and lazily imported consumers
    # must agree with the owned engine without leaking synthetic dialect funcs.
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))
    async with test_engine.begin() as conn:
        # The fixture makes today's schema; only this disposable table's new
        # column is removed to reproduce the actual pre-m220 upgrade boundary.
        await conn.execute(text("ALTER TABLE auth_ephemeral_tokens DROP COLUMN IF EXISTS remember_me"))
    await assert_legacy_remember_me_upgrade(test_engine)


# Run the actual signed callback and one-time consume/rotation contract on PG
# too; the fixture patches only the IdP HTTP transport, never the database.
from backend.tests.integration.test_g01_f_oidc import (  # noqa: E402
    oidc_flow,  # noqa: F401
    signing_key,  # noqa: F401
    test_oidc_choice_survives_factor_and_email_resend as assert_oidc_factor,
    test_oidc_cookie_and_rotation_use_state_choice as assert_oidc_rotation,
)


async def test_oidc_rotation_on_postgres(oidc_flow, async_client, db_session):
    await assert_oidc_rotation(True, 8, oidc_flow, async_client, db_session)


async def test_oidc_backup_on_postgres(oidc_flow, async_client, db_session, monkeypatch):
    await assert_oidc_factor(False, "backup", oidc_flow, async_client, db_session, monkeypatch)
