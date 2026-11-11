"""The commit-attempt rule (OrcaBamDude B1, addendum C1) on a file-backed SQLite.

The unit module runs on the shared in-memory engine, where ``publish`` skips ``BEGIN IMMEDIATE``. A file database
takes the production path: SQLite's writer is opened before the hash lookup, and a rollback after a failed commit
releases it. The PostgreSQL run is ``test_queue_sources_commit_attempt_postgres``.
"""

import pytest
from sqlalchemy import event as sql_event
from sqlalchemy.ext.asyncio import create_async_engine

from backend.tests.unit.services.test_queue_sources_commit_attempt import (  # noqa: F401
    queue_id,
    test_a_commit_that_never_happened_leaves_the_object_to_the_gc,
    test_a_commit_that_went_through_then_raised_keeps_a_reused_object,
    test_a_commit_that_went_through_then_raised_keeps_the_new_object,
    test_a_failure_before_the_commit_still_removes_the_new_object,
    test_the_gc_keeps_the_object_a_committed_row_names,
)
from backend.tests.unit.services.test_queue_sources_gc import (  # noqa: F401
    clean_spool_state,
    clockface,
    sessions,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def test_engine(tmp_path):
    from backend.app.core.database import Base, configure_sqlite_connection, import_all_models

    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "commit-attempt.sqlite"))
    sql_event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    import_all_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
