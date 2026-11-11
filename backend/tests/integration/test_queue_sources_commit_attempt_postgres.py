"""The commit-attempt rule (OrcaBamDude B1, addendum C1) on a disposable embedded PostgreSQL."""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401
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

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)
