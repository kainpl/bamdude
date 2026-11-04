"""The sweep's own release on a disposable PostgreSQL (spec gh109-o3 r2 A12): the same commits, the same CAS."""

import pytest
from sqlalchemy import event

from backend.app.core.database import _strip_tz_from_params
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401
from backend.tests.integration.test_reconcile_retires_its_own_run import (  # noqa: F401
    sweep_db,
    test_a_close_that_proves_less_keeps_the_context,
    test_a_context_that_moved_during_the_sweep_is_kept,
    test_an_outer_commit_that_did_not_complete_keeps_the_context,
    test_the_same_archive_id_under_another_claim_is_not_released,
    test_the_sweeps_own_close_releases_the_run_and_adds_no_pause,
)

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(autouse=True)
def postgres_dates(test_engine):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    yield
    event.remove(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params)
