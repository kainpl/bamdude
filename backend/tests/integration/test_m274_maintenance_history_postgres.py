"""m274 on a disposable embedded PostgreSQL: the FK swapped under its own name, the fill run once,
and a database with no FK yet (an SQLite import before ``conform_imported_schema``) migrated too."""

import pytest

from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_m274_maintenance_history import assert_m274_fills_once

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _on_postgres(monkeypatch):
    # The migration asks the configured backend; this test's engine is the embedded server.
    from backend.app.migrations import helpers, m274_maintenance_history_outlives as m274

    monkeypatch.setattr(m274, "is_postgres", lambda: True)
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)


async def test_m274_on_postgres(test_engine, db_session, printer_factory):  # noqa: F811
    await assert_m274_fills_once(test_engine, db_session, printer_factory)


async def test_m274_on_postgres_without_a_foreign_key_yet(test_engine, db_session, printer_factory):  # noqa: F811
    await assert_m274_fills_once(test_engine, db_session, printer_factory, with_fk=False)
