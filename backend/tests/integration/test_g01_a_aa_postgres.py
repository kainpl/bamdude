"""AA4 exact historical-body upgrade on a real disposable PostgreSQL."""

import pytest
from sqlalchemy import delete

from backend.app.models.notification_template import NotificationTemplate
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.unit.test_migration_m218 import (
    test_old_defaults_upgrade_once_and_render_without_empty_credential_labels as assert_upgrade,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


async def test_all_historical_locales_and_flags_upgrade_on_postgres(db_session, monkeypatch):
    for variant in ("en_seed", "en", "uk"):
        for flag in (True, False):
            await assert_upgrade(db_session, monkeypatch, variant, flag)
            await db_session.execute(delete(NotificationTemplate))
            await db_session.commit()
