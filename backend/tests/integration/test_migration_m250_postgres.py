"""m250 on a disposable PostgreSQL: an upgraded database and a fresh install end with the same schema (TA29)."""

import pytest
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.migrations import helpers, m250_claim_proof_and_library_identity as m250
from backend.app.services import embedded_postgres as ep
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.slow]
pytest.importorskip("embedded_postgres")

ROWS = 1203

INDEXES = """
    SELECT i.relname, ix.indisunique
    FROM pg_index ix
    JOIN pg_class t ON t.oid = ix.indrelid
    JOIN pg_class i ON i.oid = ix.indexrelid
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
    WHERE t.relname = 'library_files' AND a.attname = 'recovery_identity'
"""


@pytest.fixture
async def pg_engine(live_settings, monkeypatch):  # noqa: F811
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)
    engine = None
    try:
        await ep.start()
        engine = create_async_engine(
            URL.create(
                "postgresql+asyncpg",
                username=ep.PG_USER,
                password="live-test-password",
                host=ep.PG_HOST,
                port=settings.embedded_pg_port,
                database=ep.PG_DATABASE,
            )
        )
        yield engine
    finally:
        if engine is not None:
            await engine.dispose()
        await ep.stop()


async def test_an_upgraded_database_gets_one_identity_per_row_and_a_unique_index(pg_engine):
    async with pg_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE print_queue (id SERIAL PRIMARY KEY, status VARCHAR(20))"))
        await conn.execute(text("CREATE TABLE library_files (id SERIAL PRIMARY KEY, file_path VARCHAR(500))"))
        await conn.execute(text("INSERT INTO print_queue (status) VALUES ('pending')"))
        await conn.execute(
            text("INSERT INTO library_files (file_path) VALUES (:path)"),
            [{"path": f"library/{n}.3mf"} for n in range(ROWS)],
        )
        await m250.upgrade(conn)
    async with pg_engine.begin() as conn:
        values = (await conn.execute(text("SELECT recovery_identity FROM library_files"))).scalars().all()
        assert len(values) == ROWS and len(set(values)) == ROWS and all(len(v) == 36 for v in values)
        assert (await conn.execute(text(INDEXES))).all() == [("ix_library_files_recovery_identity", True)]
        column_type = await conn.scalar(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'print_queue' AND column_name = 'deferred_claim_token'"
            )
        )
        assert column_type == "timestamp without time zone"
        await m250.upgrade(conn)
        assert sorted(
            (await conn.execute(text("SELECT recovery_identity FROM library_files"))).scalars().all()
        ) == sorted(values)


async def test_a_fresh_install_has_the_same_index_and_the_migration_adds_nothing(pg_engine):
    import_all_models()
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await m250.upgrade(conn)
        assert (await conn.execute(text(INDEXES))).all() == [("ix_library_files_recovery_identity", True)]
