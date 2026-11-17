"""An AS-IS import reaches m235 before create_all: its UNIQUE must be canonical."""

import asyncio

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import helpers, m235_manyfold_permissions_import_meta as migration
from backend.app.models.library_file_import_meta import LibraryFileImportMeta
from backend.tests.integration.test_postgres_scenarios import _async_url, _pg_url, _wipe

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def test_migration_created_import_metadata_has_the_model_unique_guard(monkeypatch):
    url = _pg_url()
    _wipe(url, all_objects=True)
    monkeypatch.setattr(helpers, "is_postgres", lambda: True)

    async def check():
        from backend.app.core.database import import_all_models

        import_all_models()
        engine = create_async_engine(_async_url(url))
        try:
            async with engine.begin() as conn:
                await conn.exec_driver_sql("CREATE TABLE library_files (id INTEGER PRIMARY KEY)")
                await conn.run_sync(LibraryFileImportMeta.__table__.create)
                expected = await conn.run_sync(
                    lambda sync: inspect(sync).get_unique_constraints("library_file_import_meta")
                )
                assert len(expected) == 1 and expected[0]["column_names"] == ["library_file_id"]
                await conn.exec_driver_sql("DROP TABLE library_file_import_meta")
                await migration.upgrade(conn)
                await migration.upgrade(conn)
                actual = await conn.run_sync(
                    lambda sync: inspect(sync).get_unique_constraints("library_file_import_meta")
                )
                assert actual == expected
                flags = (
                    await conn.execute(
                        text(
                            "SELECT c.convalidated,i.indisvalid,i.indisready FROM pg_constraint c "
                            "JOIN pg_index i ON i.indexrelid=c.conindid "
                            "WHERE c.conrelid='library_file_import_meta'::regclass AND c.contype='u'"
                        )
                    )
                ).all()
                assert flags == [(True, True, True)]
                # The model constraint, rather than the legacy additional index,
                # must itself enforce the guard the final verifier expects.
                await conn.exec_driver_sql("DROP INDEX ux_library_file_import_meta_library_file_id")
                await conn.exec_driver_sql("INSERT INTO library_files(id) VALUES(1)")
                insert = (
                    "INSERT INTO library_file_import_meta(library_file_id,source_type,source_model_id,imported_at) "
                    "VALUES(1,'manyfold','model-id',TIMESTAMP '2026-01-01 00:00:00')"
                )
                await conn.exec_driver_sql(insert)
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.exec_driver_sql(insert)
        finally:
            await engine.dispose()

    asyncio.run(check())
