"""Disposable independent connections for queue transaction tests.

An explicit PostgreSQL target uses a unique schema; default is temporary SQLite.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.credential_config import initialize_credential_config_anchor
from backend.app.core.database import Base, _strip_tz_from_params, configure_sqlite_connection, import_all_models


@pytest.fixture
async def test_engine(monkeypatch, tmp_path):
    url = os.environ.get("DISPATCH_TEST_POSTGRES_URL", f"sqlite+aiosqlite:///{tmp_path / 'dispatch.sqlite'}")
    schema = "dispatch_" + uuid4().hex if url.startswith("postgresql") else None
    if schema:
        monkeypatch.setattr(settings, "database_url", url)
    options = (
        {
            "connect_args": {
                "server_settings": {
                    "search_path": schema,
                    "timezone": "UTC",
                    "application_name": schema,
                }
            }
        }
        if schema
        else {}
    )
    engine = create_async_engine(url, **options)
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    else:
        event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    import_all_models()
    async with engine.begin() as connection:
        if schema:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
        # What ``init_db`` and conftest's engine create: a settings save without it is a 503.
        await initialize_credential_config_anchor(connection)
    monkeypatch.setattr("backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    async with engine.begin() as connection:
        if schema:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        else:
            await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    await asyncio.sleep(0.1)
