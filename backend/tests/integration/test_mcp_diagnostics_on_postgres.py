"""The same scoped diagnostic readers on a disposable PostgreSQL, per-test schemas.

Run through test_postgres_scenarios or set DIAGNOSTIC_TEST_POSTGRES_URL for an
owned disposable server. Never point it at a production or shared database.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, _strip_tz_from_params, import_all_models
from backend.tests.integration.test_diagnostic_visibility import *  # noqa: F403
from backend.tests.integration.test_mcp_energy_diagnostics import *  # noqa: F403
from backend.tests.integration.test_mcp_farm_diagnostics import *  # noqa: F403
from backend.tests.integration.test_mcp_log_secrets import *  # noqa: F403
from backend.tests.integration.test_mcp_logs import (
    test_exact_plug_oidc_and_ha_override_canaries,  # noqa: F401
    test_logs_http_authority_redaction_and_collector_failure,  # noqa: F401
)
from backend.tests.integration.test_mcp_period_summary import *  # noqa: F403
from backend.tests.integration.test_mcp_recovery_explain import *  # noqa: F403
from backend.tests.integration.test_mcp_timeline import *  # noqa: F403

pytestmark = pytest.mark.skipif(
    not os.environ.get("DIAGNOSTIC_TEST_POSTGRES_URL"), reason="requires disposable PostgreSQL"
)


@pytest.fixture
async def test_engine(monkeypatch):
    url = os.environ["DIAGNOSTIC_TEST_POSTGRES_URL"]
    schema = "mcp_diagnostic_" + uuid4().hex
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema, "timezone": "UTC"}})
    event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    import_all_models()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(
            "backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False)
        )
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
